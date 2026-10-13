"""Content-stream surgery on the designer's PDF.

The walker replays a page's content stream (recursing into form XObjects),
tracking the CTM and the layer (optional content group) of every painting
operator.  A *plan* decides which painting operators survive, which lets one
engine produce everything we need:

* the **print file**: static artwork kept as-is (vectors, fonts, colour
  spaces untouched); each customizable layer's content is removed and the
  customer's content is inserted *at the same position in the paint order*,
  so decorations drawn above a photo stay above it;
* **preview segments**: the static artwork split into z-ordered slices
  (everything below the first editable element, between elements, ...),
  which the browser editor stacks around its live elements;
* **layer samples**: one layer on its own (the designer's placeholder photo).

Only painting operators are removed; state operators (``q``/``Q``/``cm``,
clipping, colours, text positioning) are always kept, so the graphics state
seen by the remaining content is unchanged.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import pikepdf

from . import geometry as g

PATH_PAINT = {"S", "s", "f", "F", "f*", "B", "B*", "b", "b*"}
TEXT_SHOW = {"Tj", "TJ", "'", '"'}

KeepFn = Callable[[str | None, int], bool]


def ocg_names(pdf: pikepdf.Pdf) -> dict[tuple[int, int], str]:
    """OCG object id -> layer name."""
    out: dict[tuple[int, int], str] = {}
    props = pdf.Root.get("/OCProperties")
    if props is None:
        return out
    for ocg in props.get("/OCGs", []):
        if ocg.is_indirect:
            out[ocg.objgen] = str(ocg.get("/Name", ""))
    return out


def slot_map(pdf: pikepdf.Pdf, layer_to_slot: dict[str, str]) -> dict[tuple[int, int], str]:
    return {objgen: layer_to_slot[name] for objgen, name in ocg_names(pdf).items() if name in layer_to_slot}


@dataclass
class Plan:
    keep: KeepFn
    # slot id -> form XObject (already owned by the target PDF) painted where
    # that slot's layer first appears.
    inserts: dict[str, pikepdf.Object] = field(default_factory=dict)
    # Translation from MediaBox origin to page user space for inserted forms.
    origin: tuple[float, float] = (0.0, 0.0)


@dataclass
class WalkStats:
    order: list[str] = field(default_factory=list)  # slots in first-paint order
    static_ops: dict[int, int] = field(default_factory=dict)  # segment -> kept static paint ops
    kept_ops: int = 0


class _Walker:
    def __init__(self, pdf: pikepdf.Pdf, slots: dict[tuple[int, int], str], plan: Plan):
        self.pdf = pdf
        self.slots = slots
        self.plan = plan
        self.stats = WalkStats()
        self._names = 0

    # -- helpers ---------------------------------------------------------
    def _slot_for_oc(self, oc: pikepdf.Object | None) -> str | None:
        if oc is None or not isinstance(oc, pikepdf.Dictionary):
            return None
        if oc.get("/Type") == "/OCMD":
            members = oc.get("/OCGs")
            if members is None:
                return None
            if isinstance(members, pikepdf.Dictionary):
                members = [members]
            for m in members:
                if m.is_indirect and m.objgen in self.slots:
                    return self.slots[m.objgen]
            return None
        return self.slots.get(oc.objgen) if oc.is_indirect else None

    def _fresh_name(self, resources: pikepdf.Dictionary, prefix: str) -> pikepdf.Name:
        xobjects = resources.get("/XObject")
        if xobjects is None:
            xobjects = resources.XObject = pikepdf.Dictionary()
        while True:
            self._names += 1
            name = pikepdf.Name(f"/{prefix}{self._names}")
            if name not in xobjects:
                return name

    def _first_paint(self, slot: str, resources: pikepdf.Dictionary, ctm: g.Matrix, out: list) -> None:
        """Record z-position of a slot and emit its replacement, if any."""
        if slot in self.stats.order:
            return
        self.stats.order.append(slot)
        form = self.plan.inserts.get(slot)
        if form is not None:
            inv = g.invert(ctm)
            if inv is not None:
                m = g.mul((1, 0, 0, 1, *self.plan.origin), inv)
                name = self._fresh_name(resources, "JasminSlot")
                resources.XObject[name] = form
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("q")))
                out.append(pikepdf.ContentStreamInstruction([round(v, 6) for v in m], pikepdf.Operator("cm")))
                out.append(pikepdf.ContentStreamInstruction([name], pikepdf.Operator("Do")))
                out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("Q")))
                self.stats.kept_ops += 1

    @property
    def segment(self) -> int:
        return len(self.stats.order)

    def _keep(self, slot: str | None) -> bool:
        keep = self.plan.keep(slot, self.segment)
        if keep:
            self.stats.kept_ops += 1
            if slot is None:
                self.stats.static_ops[self.segment] = self.stats.static_ops.get(self.segment, 0) + 1
        return keep

    # -- walking ---------------------------------------------------------
    def walk(
        self,
        instructions: Iterable,
        resources: pikepdf.Dictionary,
        ctm: g.Matrix,
        inherited: str | None,
        depth: int = 0,
    ) -> tuple[list, bool]:
        out: list = []
        changed = False
        ctm_stack: list[g.Matrix] = []
        mc_stack: list[str | None] = []

        def current() -> str | None:
            return next((s for s in reversed(mc_stack) if s), inherited)

        for ins in instructions:
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                if self._keep(current()):
                    out.append(ins)
                else:
                    changed = True
                continue
            op = str(ins.operator)
            operands = ins.operands
            if op == "q":
                ctm_stack.append(ctm)
            elif op == "Q":
                if ctm_stack:
                    ctm = ctm_stack.pop()
            elif op == "cm" and len(operands) == 6:
                ctm = g.mul([float(v) for v in operands], ctm)
            elif op == "BDC":
                slot = None
                if len(operands) == 2 and operands[0] == "/OC":
                    props = operands[1]
                    if isinstance(props, pikepdf.Name):
                        props = resources.get("/Properties", {}).get(props)
                    slot = self._slot_for_oc(props)
                    if slot is not None and slot not in self.stats.order:
                        self._first_paint(slot, resources, ctm, out)
                        changed = changed or slot in self.plan.inserts
                mc_stack.append(slot)
            elif op == "BMC":
                mc_stack.append(None)
            elif op == "EMC":
                if mc_stack:
                    mc_stack.pop()
            elif op in PATH_PAINT:
                if not self._keep(current()):
                    out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("n")))
                    changed = True
                    continue
            elif op in TEXT_SHOW:
                if not self._keep(current()):
                    changed = True
                    if op == "'":
                        out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
                    elif op == '"' and len(operands) == 3:
                        out.append(pikepdf.ContentStreamInstruction([operands[0]], pikepdf.Operator("Tw")))
                        out.append(pikepdf.ContentStreamInstruction([operands[1]], pikepdf.Operator("Tc")))
                        out.append(pikepdf.ContentStreamInstruction([], pikepdf.Operator("T*")))
                    continue
            elif op == "sh":
                if not self._keep(current()):
                    changed = True
                    continue
            elif op == "Do" and operands:
                before = len(out)
                new = self._do(ins, resources, ctm, current(), out, depth)
                if new is not ins or len(out) != before:
                    changed = True
                if new is not None:
                    out.append(new)
                continue
            out.append(ins)
        return out, changed

    def _do(self, original, resources, ctm, slot, out, depth):
        """Handle ``Do``: returns the instruction to emit (``original`` if
        unchanged, a replacement, or None to drop it)."""
        name = original.operands[0]
        xobjects = resources.get("/XObject")
        xobj = xobjects.get(name) if xobjects is not None else None
        if xobj is None:
            return original
        own_slot = self._slot_for_oc(xobj.get("/OC"))
        if own_slot is not None and own_slot not in self.stats.order:
            self._first_paint(own_slot, resources, ctm, out)
        ctx = own_slot or slot
        subtype = xobj.get("/Subtype")
        if subtype == "/Form" and depth < 12:
            matrix = [float(v) for v in xobj.get("/Matrix", [1, 0, 0, 1, 0, 0])]
            form_res = _own_resources(xobj.get("/Resources", resources))
            kept_before = self.stats.kept_ops
            new_ins, changed = self.walk(
                pikepdf.parse_content_stream(xobj), form_res, g.mul(matrix, ctm), ctx, depth + 1
            )
            if not changed:
                return original
            if self.stats.kept_ops == kept_before:
                return None  # nothing left to draw in this form
            new_form = self.pdf.make_stream(pikepdf.unparse_content_stream(new_ins))
            for key, value in xobj.items():
                if key not in ("/Length", "/Filter", "/DecodeParms"):
                    new_form[key] = value
            new_form.Resources = form_res
            new_name = self._fresh_name(resources, "JasminForm")
            resources.XObject[new_name] = self.pdf.make_indirect(new_form)
            return pikepdf.ContentStreamInstruction([new_name], pikepdf.Operator("Do"))
        # Images (and anything else) are painting operations.
        return original if self._keep(ctx) else None


def _own_resources(resources: pikepdf.Dictionary | None) -> pikepdf.Dictionary:
    """Shallow copy (with its own /XObject dict) so new names never leak into
    resource dictionaries shared with other pages or forms."""
    copy = pikepdf.Dictionary()
    if resources is not None:
        for key, value in resources.items():
            copy[key] = value
    xobjects = copy.get("/XObject")
    copy.XObject = pikepdf.Dictionary({k: v for k, v in xobjects.items()}) if xobjects is not None else pikepdf.Dictionary()
    return copy


def transform_page(pdf: pikepdf.Pdf, page: pikepdf.Page, slots: dict[tuple[int, int], str], plan: Plan) -> WalkStats:
    walker = _Walker(pdf, slots, plan)
    resources = _own_resources(page.obj.get("/Resources"))
    new, changed = walker.walk(pikepdf.parse_content_stream(page.obj), resources, g.IDENTITY, None)
    if changed:
        page.obj.Resources = resources
        page.obj.Contents = pdf.make_stream(pikepdf.unparse_content_stream(new))
    return walker.stats


def scan_page(pdf_path: Path, page_index: int, layer_to_slot: dict[str, str]) -> WalkStats:
    """Z-order of the slots on a page and which static segments have content."""
    with pikepdf.open(pdf_path) as pdf:
        return transform_page(pdf, pdf.pages[page_index], slot_map(pdf, layer_to_slot), Plan(keep=lambda s, k: True))


def single_page_variant(pdf_path: Path, page_index: int, layer_to_slot: dict[str, str], keep: KeepFn) -> bytes:
    """A one-page PDF containing only the painting operations ``keep`` accepts."""
    with pikepdf.open(pdf_path) as pdf:
        transform_page(pdf, pdf.pages[page_index], slot_map(pdf, layer_to_slot), Plan(keep=keep))
        for i in reversed(range(len(pdf.pages))):
            if i != page_index:
                del pdf.pages[i]
        buf = io.BytesIO()
        pdf.save(buf)
        return buf.getvalue()
