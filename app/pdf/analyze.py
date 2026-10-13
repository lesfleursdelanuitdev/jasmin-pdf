"""Turn a designer's layered PDF into editable template elements.

Every glyph, image and path on a page is tagged with the optional content
groups (layers) it is drawn in and the clip path in effect.  Layers named
``<something>-customize`` become elements:

* a layer containing an image becomes an **image slot**: its box is the
  visible part of the image, its shape the clip path (e.g. an arch);
* a layer containing text becomes a **text slot**: default text, font, size,
  colour, letter spacing, line height and alignment are read from the glyphs;
* a layer with only vector shapes becomes an image slot shaped like them
  (a "drop your photo here" frame).

The output is a starting point the designer reviews in the admin UI; the
structured template, not the PDF, drives the editor.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pdfminer.converter import PDFLayoutAnalyzer
from pdfminer.layout import LTChar
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFObjRef, dict_value, resolve1, stream_value
from pdfminer.psparser import PSLiteral, literal_name

from ..fonts import DEFAULT_FONT, FontInfo, FontLibrary, strip_subset_prefix
from . import geometry as g

CUSTOMIZE_RE = re.compile(
    r"^(?:(?P<a>.+?)[\s_\-:.]*customi[sz]e|customi[sz]e[\s_\-:.]+(?P<b>.+))$", re.IGNORECASE
)
IMAGE_HINT = re.compile(r"(photo|image|img|picture|pic)$", re.IGNORECASE)
TEXT_HINT = re.compile(r"(text|name|names|title|date|line)$", re.IGNORECASE)


class AnalysisError(ValueError):
    pass


def customizable_base(layer_name: str) -> str | None:
    """``couple-photo-customize`` -> ``couple-photo``; ``Background`` -> None."""
    m = CUSTOMIZE_RE.match(layer_name.strip())
    if not m:
        return None
    base = (m.group("a") or m.group("b") or "").strip(" -_:.")
    return base or None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "element"


ACRONYMS = {"rsvp", "qr", "url", "id", "dob", "pm", "am"}


def humanize(slug: str) -> str:
    words = [w.upper() if w.lower() in ACRONYMS else w for w in re.sub(r"[-_]+", " ", slug).split()]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


# ---------------------------------------------------------------------------
# pdfminer instrumentation
# ---------------------------------------------------------------------------

@dataclass
class Mark:
    kind: str  # "char" | "image" | "path"
    ocgs: tuple[int, ...]  # enclosing OCG object ids, outermost first
    bbox: g.Rect  # page space: origin at MediaBox lower-left, y up
    seq: int
    clips: tuple[list, ...]  # clip paths in effect (page space)
    data: dict[str, Any] = field(default_factory=dict)


class _Device(PDFLayoutAnalyzer):
    def __init__(self, rsrcmgr: PDFResourceManager):
        super().__init__(rsrcmgr, laparams=None)
        self.marks: list[Mark] = []
        self.oc_stack: list[tuple[int, ...]] = []
        self.clips: tuple[list, ...] = ()
        self.inherited_clips: tuple[list, ...] = ()
        self._charspace = 0.0
        self._scaling = 1.0

    # marked content ------------------------------------------------------
    def push_oc(self, ocgs: tuple[int, ...]) -> None:
        self.oc_stack.append(ocgs)

    def pop_oc(self) -> None:
        if self.oc_stack:
            self.oc_stack.pop()

    def _ocgs(self) -> tuple[int, ...]:
        return tuple(o for entry in self.oc_stack for o in entry)

    def _add(self, kind: str, bbox: g.Rect, **data: Any) -> None:
        self.marks.append(Mark(kind, self._ocgs(), bbox, len(self.marks), self.clips, data))

    # painting ------------------------------------------------------------
    def render_string(self, textstate, seq, ncs, graphicstate):  # type: ignore[override]
        self._charspace = float(textstate.charspace or 0.0)
        self._scaling = float(textstate.scaling or 100) * 0.01
        return super().render_string(textstate, seq, ncs, graphicstate)

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):  # type: ignore[override]
        adv = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        item = self.cur_item._objs[-1]
        if isinstance(item, LTChar):
            a, b, c, d, e, f = matrix
            hscale = (a * a + b * b) ** 0.5
            vscale = (c * c + d * d) ** 0.5
            self._add(
                "char",
                item.bbox,
                text=item.get_text(),
                font=item.fontname,
                size=float(fontsize) * vscale,
                origin=(e, f),
                upright=item.upright,
                charspace=self._charspace * self._scaling * hscale,
                color=_color(ncs, graphicstate.ncolor),
                ascent=font.get_ascent() * 0.001 if hasattr(font, "get_ascent") else 0.8,
                descent=font.get_descent() * 0.001 if hasattr(font, "get_descent") else -0.2,
            )
        return adv

    def render_image(self, name, stream):  # type: ignore[override]
        super().render_image(name, stream)
        bbox = g.transform_rect(self.ctm, (0, 0, 1, 1))
        width = resolve1(stream.get("Width")) or 0
        height = resolve1(stream.get("Height")) or 0
        self._add("image", bbox, name=name, width=int(width), height=int(height))

    def paint_path(self, gstate, stroke, fill, evenodd, path):  # type: ignore[override]
        tp = g.transform_path(self.ctm, g.normalize_path(path))
        bbox = g.path_bbox(tp)
        if bbox is not None:
            self._add("path", bbox, path=tp, fill=bool(fill), stroke=bool(stroke))


class _Interpreter(PDFPageInterpreter):
    device: _Device

    def init_state(self, ctm):  # type: ignore[override]
        super().init_state(ctm)
        self.clips = self.device.inherited_clips
        self.device.clips = self.clips

    def get_current_state(self):  # type: ignore[override]
        return (*super().get_current_state(), self.clips)

    def set_current_state(self, state):  # type: ignore[override]
        super().set_current_state(state[:3])
        self.clips = state[3] if len(state) > 3 else ()
        self.device.clips = self.clips

    def _set_clip(self) -> None:
        if self.curpath:
            clip = g.transform_path(self.ctm, g.normalize_path(self.curpath))
            self.clips = self.clips + (clip,)
            self.device.clips = self.clips

    def do_W(self) -> None:
        self._set_clip()

    def do_W_a(self) -> None:
        self._set_clip()

    def _oc_ids(self, spec: Any) -> tuple[int, ...]:
        obj = spec
        if isinstance(spec, PSLiteral):
            props = dict_value(self.resources.get("Properties")) if self.resources else {}
            obj = props.get(spec.name)
        return _ocg_ids(obj)

    def do_BDC(self, tag, props) -> None:  # type: ignore[override]
        is_oc = isinstance(tag, PSLiteral) and tag.name == "OC"
        self.device.push_oc(self._oc_ids(props) if is_oc else ())

    def do_BMC(self, tag) -> None:  # type: ignore[override]
        self.device.push_oc(())

    def do_EMC(self) -> None:
        self.device.pop_oc()

    def do_Do(self, xobjid_arg) -> None:  # type: ignore[override]
        xobjid = literal_name(xobjid_arg)
        ocgs: tuple[int, ...] = ()
        try:
            xobj = stream_value(self.xobjmap[xobjid])
            ocgs = _ocg_ids(xobj.get("OC"))
        except Exception:  # noqa: BLE001 - let the base class report it
            pass
        self.device.push_oc(ocgs)
        self.device.inherited_clips = self.clips
        try:
            super().do_Do(xobjid_arg)
        finally:
            self.device.pop_oc()
            self.device.clips = self.clips


def _ocg_ids(obj: Any) -> tuple[int, ...]:
    """Object ids of the OCGs an /OC entry (OCG or OCMD) refers to."""
    if obj is None:
        return ()
    ref = obj.objid if isinstance(obj, PDFObjRef) else None
    try:
        d = dict_value(obj)
    except Exception:  # noqa: BLE001
        return ()
    kind = d.get("Type")
    kind = kind.name if isinstance(kind, PSLiteral) else kind
    if kind == "OCMD":
        members = d.get("OCGs")
        if isinstance(members, PDFObjRef):
            return (members.objid,)
        return tuple(m.objid for m in (resolve1(members) or []) if isinstance(m, PDFObjRef))
    return (ref,) if ref is not None else ()


DEVICE_LIKE = {"DeviceGray", "DeviceRGB", "DeviceCMYK", "ICCBased", "CalGray", "CalRGB", "Lab"}


def _color(ncs: Any, value: Any) -> dict:
    name = getattr(ncs, "name", None)
    if name and name not in DEVICE_LIKE:
        # Spot (Separation/DeviceN), Pattern or Indexed colour: the component
        # values are tints/indices, not a colour we can reproduce. Use black and
        # let the analyzer tell the designer to pick a colour.
        return {"space": "gray", "values": [0.0], "hex": "#000000", "approximate": name}
    if value is None:
        comps: list[float] = [0.0]
    elif isinstance(value, (int, float)):
        comps = [float(value)]
    else:
        try:
            comps = [float(v) for v in value]
        except (TypeError, ValueError):
            comps = [0.0]
    n = getattr(ncs, "ncomponents", len(comps)) if ncs is not None else len(comps)
    if len(comps) == 1 or n == 1:
        v = comps[0]
        space, rgb = "gray", (v, v, v)
    elif len(comps) == 4:
        c, m, y, k = comps
        space, rgb = "cmyk", ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    elif len(comps) == 3:
        space, rgb = "rgb", tuple(comps)
    else:
        space, rgb, comps = "gray", (0.0, 0.0, 0.0), [0.0]
    hexv = "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)
    return {"space": space, "values": [round(c, 4) for c in comps], "hex": hexv}


# ---------------------------------------------------------------------------
# Element construction
# ---------------------------------------------------------------------------

class _Page:
    def __init__(self, index: int, page: PDFPage):
        self.index = index
        x0, y0, x1, y1 = (float(v) for v in page.mediabox)
        self.media = (x0, y0, x1, y1)
        self.width, self.height = x1 - x0, y1 - y0

        def box(name: str) -> g.Rect:
            raw = resolve1(page.attrs.get(name))
            if not raw:
                return self.media
            bx = [float(resolve1(v)) for v in raw]
            return (min(bx[0], bx[2]), min(bx[1], bx[3]), max(bx[0], bx[2]), max(bx[1], bx[3]))

        self.trim = box("TrimBox")
        self.bleed = box("BleedBox")

    def to_template_rect(self, r: g.Rect) -> dict:
        """Page-space rect (relative to MediaBox origin, y up) -> template box."""
        return {
            "x": round(r[0], 3),
            "y": round(self.height - r[3], 3),
            "w": round(r[2] - r[0], 3),
            "h": round(r[3] - r[1], 3),
        }

    def abs_to_template_rect(self, r: g.Rect) -> dict:
        x0, y0 = self.media[0], self.media[1]
        return self.to_template_rect((r[0] - x0, r[1] - y0, r[2] - x0, r[3] - y0))

    def to_template_path(self, path: list) -> list:
        return g.round_path(g.flip_to_top_left(path, 0.0, self.height))

    def describe(self) -> dict:
        return {
            "index": self.index,
            "width": round(self.width, 3),
            "height": round(self.height, 3),
            "mediaBox": [round(v, 3) for v in self.media],
            "trimBox": self.abs_to_template_rect(self.trim),
            "bleedBox": self.abs_to_template_rect(self.bleed),
        }


def _image_element(page: _Page, marks: list[Mark], warnings: list[str], layer: str) -> dict | None:
    images = [m for m in marks if m.kind == "image"]
    shape = None
    if images:
        def visible(m: Mark) -> g.Rect | None:
            clip = None
            for c in m.clips:
                clip = g.intersect(clip, g.path_bbox(c)) if clip is not None else g.path_bbox(c)
            return g.intersect(m.bbox, clip) if clip is not None else m.bbox

        best = max(images, key=lambda m: g.area(visible(m)))
        if len(images) > 1:
            warnings.append(f"Layer '{layer}' contains {len(images)} images; using the largest one as the photo slot.")
        box = visible(best) or best.bbox
        for clip in reversed(best.clips):
            cb = g.path_bbox(clip)
            if g.area(g.intersect(cb, best.bbox)) < g.area(best.bbox) * 0.995:
                if not g.is_axis_aligned_rect(clip):
                    shape = clip
                break
        sample = {"width": best.data["width"], "height": best.data["height"]}
    else:
        paths = [m for m in marks if m.kind == "path"]
        if not paths:
            return None
        box = None
        for m in paths:
            box = g.union(box, m.bbox)
        filled = [m for m in paths if m.data.get("fill")]
        if filled:
            big = max(filled, key=lambda m: g.area(m.bbox))
            if g.area(big.bbox) >= g.area(box) * 0.9 and not g.is_axis_aligned_rect(big.data["path"]):
                shape = big.data["path"]
        sample = None
    element = {
        "type": "image",
        "box": page.to_template_rect(box),
        "clipPath": page.to_template_path(shape) if shape else None,
        "fit": "cover",
        "minDpi": 150,
        "required": True,
        "keepOriginalIfEmpty": False,
    }
    if sample:
        element["sample"] = sample
    return element


def _group_lines(chars: list[Mark]) -> list[list[Mark]]:
    lines: list[list[Mark]] = []
    for ch in sorted(chars, key=lambda m: m.seq):
        y = ch.data["origin"][1]
        for line in lines:
            ref = line[0]
            if abs(ref.data["origin"][1] - y) < 0.35 * max(ref.data["size"], 1):
                line.append(ch)
                break
        else:
            lines.append([ch])
    for line in lines:
        line.sort(key=lambda m: m.data["origin"][0])
    lines.sort(key=lambda line: -line[0].data["origin"][1])  # top of page first
    return lines


def _line_text(line: list[Mark]) -> str:
    out = ""
    prev: Mark | None = None
    for ch in line:
        t = ch.data["text"]
        if prev is not None and not out.endswith(" ") and not t.isspace():
            gap = ch.bbox[0] - prev.bbox[2] - prev.data["charspace"]
            if gap > 0.22 * ch.data["size"]:
                out += " "
        out += t
        prev = ch
    return re.sub(r"\s+", " ", out).strip()


def _text_element(
    page: _Page, marks: list[Mark], fonts: FontLibrary, warnings: list[str], layer: str
) -> dict | None:
    chars = [m for m in marks if m.kind == "char" and m.data.get("upright", True)]
    if not chars:
        return None
    lines = [ln for ln in _group_lines(chars) if _line_text(ln)]
    if not lines:
        return None
    texts = [_line_text(ln) for ln in lines]

    font_names = Counter(strip_subset_prefix(m.data["font"]) for m in chars if not m.data["text"].isspace())
    pdf_font = font_names.most_common(1)[0][0]
    if len(font_names) > 1:
        warnings.append(
            f"Layer '{layer}' mixes fonts ({', '.join(font_names)}); customer text uses {pdf_font}."
        )
    info: FontInfo | None = fonts.match(pdf_font)
    if info is None:
        info = fonts.require(DEFAULT_FONT)
        warnings.append(
            f"Font '{pdf_font}' (layer '{layer}') is not in the font library; using {info.name}. "
            "Upload the .ttf in Admin > Fonts and pick it for this element."
        )

    size = statistics.median(m.data["size"] for m in chars)
    spacing = statistics.median(m.data["charspace"] for m in chars)
    colors = Counter(tuple(m.data["color"]["values"]) for m in chars)
    top_color = colors.most_common(1)[0][0]
    color = next(m.data["color"] for m in chars if tuple(m.data["color"]["values"]) == top_color)
    if color.get("approximate"):
        warnings.append(
            f"Layer '{layer}' uses a {color['approximate']} colour; customer text will print in black "
            "until you choose a colour for this element."
        )
        color = {k: v for k, v in color.items() if k != "approximate"}

    baselines = [ln[0].data["origin"][1] for ln in lines]  # page space, y up
    if len(lines) > 1:
        line_height = statistics.median(baselines[i] - baselines[i + 1] for i in range(len(lines) - 1)) / size
    else:
        line_height = 1.2

    lefts = [ln[0].bbox[0] for ln in lines]
    rights = [ln[-1].bbox[2] for ln in lines]  # glyph box; excludes trailing letter spacing
    centers = [(a + b) / 2 for a, b in zip(lefts, rights)]
    page_cx = (page.trim[0] + page.trim[2]) / 2 - page.media[0]
    if len(lines) > 1:
        spread = {
            "center": max(centers) - min(centers),
            "left": max(lefts) - min(lefts),
            "right": max(rights) - min(rights),
        }
        align = min(("center", "left", "right"), key=lambda k: (spread[k], k != "center"))
    elif abs(centers[0] - page_cx) < 0.02 * page.width:
        align = "center"
    else:
        align = "left" if centers[0] < page_cx else "right"

    letter_spacing = spacing / size if size else 0.0
    widths = [
        (sum(info.advance(ch) for ch in t) + letter_spacing * max(0, len(t) - 1)) * size for t in texts
    ]
    orig_widths = [b - a for a, b in zip(lefts, rights)]
    need = max(max(widths), max(orig_widths)) * 1.02
    want = max(widths) * 1.4 + 2 * size
    margin = 0.06 * page.width
    lo = page.trim[0] - page.media[0] + margin
    hi = page.trim[2] - page.media[0] - margin
    if align == "center":
        cx = statistics.mean(centers)
        half = max(need / 2, min(want / 2, cx - lo, hi - cx))
        x0, x1 = cx - half, cx + half
    elif align == "left":
        x0 = min(lefts)
        x1 = x0 + max(need, min(want, hi - x0))
    else:
        x1 = max(rights)
        x0 = x1 - max(need, min(want, x1 - lo))

    top_baseline = page.height - baselines[0]  # template space
    box_h = ((info.ascent - info.descent) + (len(lines) - 1) * line_height) * size
    default = "\n".join(texts)
    letters = [c for c in default if c.isalpha()]
    uppercase = len(letters) >= 2 and all(c.isupper() for c in letters)

    return {
        "type": "text",
        "default": default,
        "placeholder": texts[0],
        "font": info.name,
        "sourceFont": pdf_font,
        "fontSize": round(size, 3),
        "minFontSize": round(size * 0.6, 2),
        "lineHeight": round(line_height, 4),
        "letterSpacing": round(letter_spacing, 4),
        "color": color,
        "align": align,
        "valign": "middle",
        "autoFit": True,
        "transform": "uppercase" if uppercase else "none",
        "multiline": len(lines) > 1,
        "maxLines": max(1, len(lines)),
        "maxLength": max(30, int(len(default) * 1.6)),
        "required": True,
        "box": {
            "x": round(x0, 3),
            "y": round(top_baseline - info.ascent * size, 3),
            "w": round(x1 - x0, 3),
            "h": round(box_h, 3),
        },
    }


def analyze_pdf(path: Path, fonts: FontLibrary) -> dict:
    """Return ``{"pages": [...], "layers": [...], "elements": [...], "warnings": [...]}``."""
    warnings: list[str] = []
    try:
        with open(path, "rb") as fp:
            doc = PDFDocument(PDFParser(fp))
            catalog = doc.catalog
            ocprops = dict_value(catalog.get("OCProperties")) if catalog.get("OCProperties") else {}
            ocg_names: dict[int, str] = {}
            for ref in resolve1(ocprops.get("OCGs")) or []:
                if isinstance(ref, PDFObjRef):
                    name = resolve1(dict_value(ref).get("Name"))
                    if isinstance(name, bytes):
                        name = _decode_pdf_text(name)
                    ocg_names[ref.objid] = str(name or f"Layer {ref.objid}")

            rsrcmgr = PDFResourceManager(caching=True)
            pages: list[dict] = []
            layers: list[dict] = []
            elements: list[dict] = []
            used_ids: set[str] = set()
            for index, pdfpage in enumerate(PDFPage.create_pages(doc)):
                if (pdfpage.rotate or 0) % 360:
                    raise AnalysisError(
                        f"Page {index + 1} uses /Rotate {pdfpage.rotate}; export the design unrotated."
                    )
                page = _Page(index, pdfpage)
                device = _Device(rsrcmgr)
                _Interpreter(rsrcmgr, device).process_page(pdfpage)
                pages.append(page.describe())

                counts: Counter[int] = Counter()
                for m in device.marks:
                    for o in set(m.ocgs):
                        counts[o] += 1
                custom = {o for o in counts if o in ocg_names and customizable_base(ocg_names[o])}
                by_layer: dict[int, list[Mark]] = {o: [] for o in custom}
                for m in device.marks:
                    owner = next((o for o in reversed(m.ocgs) if o in custom), None)
                    if owner is not None:
                        by_layer[owner].append(m)

                for o in sorted(counts, key=lambda o: min(m.seq for m in device.marks if o in m.ocgs)):
                    name = ocg_names.get(o, f"Layer {o}")
                    base = customizable_base(name)
                    entry = {"page": index, "name": name, "customizable": bool(base), "element": None}
                    layers.append(entry)
                    if not base:
                        continue
                    marks = by_layer[o]
                    has_image = any(m.kind == "image" for m in marks)
                    has_text = any(m.kind == "char" for m in marks)
                    if has_image or (not has_text and IMAGE_HINT.search(base)):
                        el = _image_element(page, marks, warnings, name)
                    elif has_text:
                        el = _text_element(page, marks, fonts, warnings, name)
                    else:
                        el = _image_element(page, marks, warnings, name)
                    if el is None:
                        warnings.append(f"Layer '{name}' is marked customizable but has no visible content.")
                        continue
                    el_id = slugify(base)
                    if el_id in used_ids:
                        el_id = f"{el_id}-p{index + 1}"
                    n = 2
                    while el_id in used_ids:
                        el_id = f"{slugify(base)}-{n}"
                        n += 1
                    used_ids.add(el_id)
                    el = {"id": el_id, "label": humanize(base), "layer": name, "page": index, **el}
                    entry["element"] = el_id
                    elements.append(el)
    except AnalysisError:
        raise
    except Exception as exc:  # noqa: BLE001 - pdfminer raises many types
        raise AnalysisError(f"Could not read the PDF: {exc}") from exc

    if not ocg_names:
        warnings.append(
            "This PDF has no layers. Export it with layers preserved (Illustrator: 'Create Acrobat "
            "Layers from Top-Level Layers'; InDesign: 'Create Acrobat Layers')."
        )
    elif not elements:
        warnings.append("No customizable layers found. Name editable layers like 'couple-names-customize'.")
    return {"pages": pages, "layers": layers, "elements": elements, "warnings": warnings}


def _decode_pdf_text(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    return raw.decode("latin-1")
