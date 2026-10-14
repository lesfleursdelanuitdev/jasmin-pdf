"""Preview assets for the editor and the print-ready PDF."""
from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pikepdf
import pypdfium2 as pdfium
from PIL import Image, ImageOps
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

from ..fonts import FontLibrary
from ..layout import layout_text, place_image
from . import engine

PREVIEW_DPI = 150


@dataclass
class UploadRef:
    path: Path
    width: int
    height: int


UploadResolver = Callable[[str], UploadRef | None]


def layer_slots(template: dict, page_index: int) -> dict[str, str]:
    return {e["layer"]: e["id"] for e in template["elements"] if e["page"] == page_index}


# ---------------------------------------------------------------------------
# Rasterising
# ---------------------------------------------------------------------------

def render_png(pdf: bytes | Path, page_index: int = 0, dpi: float = PREVIEW_DPI, transparent: bool = False) -> Image.Image:
    doc = pdfium.PdfDocument(pdf if isinstance(pdf, bytes) else str(pdf))
    try:
        page = doc[page_index]
        bitmap = page.render(
            scale=dpi / 72.0,
            fill_color=(255, 255, 255, 0) if transparent else (255, 255, 255, 255),
            may_draw_forms=True,
        )
        img = bitmap.to_pil()
        return img.convert("RGBA" if transparent else "RGB")
    finally:
        doc.close()


def _is_blank(img: Image.Image) -> bool:
    return img.mode == "RGBA" and img.getchannel("A").getbbox() is None


def build_template_assets(template: dict, pdf_path: Path, out_dir: Path, dpi: float = PREVIEW_DPI) -> list[dict]:
    """Render the z-ordered preview layers for every page.

    Returns one ``stack`` per page, bottom to top, e.g.
    ``[{"kind": "static", "src": "p0-seg0.png"}, {"kind": "element", "id": "photo"}, ...]``.
    Static slices are transparent PNGs; the editor paints white paper below.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stacks = []
    for page in template["pages"]:
        p = page["index"]
        slots = layer_slots(template, p)
        stats = engine.scan_page(pdf_path, p, slots)
        order = stats.order + [e["id"] for e in template["elements"] if e["page"] == p and e["id"] not in stats.order]
        stack: list[dict] = []
        for k in range(len(order) + 1):
            if stats.static_ops.get(k):
                data = engine.single_page_variant(pdf_path, p, slots, keep=lambda s, seg, k=k: s is None and seg == k)
                img = render_png(data, 0, dpi, transparent=True)
                if not _is_blank(img):
                    name = f"p{p}-seg{k}.png"
                    img.save(out_dir / name, optimize=True)
                    stack.append({"kind": "static", "src": name})
            if k < len(order):
                stack.append({"kind": "element", "id": order[k]})
        # The designer's original content of each layer, on its own.
        for el_id in order:
            data = engine.single_page_variant(pdf_path, p, slots, keep=lambda s, seg, el_id=el_id: s == el_id)
            render_png(data, 0, dpi, transparent=True).save(out_dir / f"p{p}-layer-{el_id}.png", optimize=True)
        render_png(pdf_path, p, dpi).save(out_dir / f"p{p}-original.jpg", quality=88)
        stacks.append({"page": p, "stack": stack})
    return stacks


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_values(template: dict, values: dict, uploads: UploadResolver, fonts: FontLibrary) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    for el in template["elements"]:
        v = values.get(el["id"]) or {}
        label = el.get("label", el["id"])
        if el["type"] == "text":
            text = v.get("text", el.get("default", "")) if v else el.get("default", "")
            text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n")
            if not text.strip():
                if el.get("required", True):
                    errors.append(f"{label}: please enter some text.")
                continue
            if len(text) > int(el.get("maxLength", 200)):
                errors.append(f"{label}: at most {el['maxLength']} characters.")
            lines = text.split("\n")
            if el.get("multiline") and len(lines) > int(el.get("maxLines", 1)):
                errors.append(f"{label}: at most {el['maxLines']} lines.")
            font = fonts.get(el["font"])
            if font is None:
                # Not printable as designed, and not the customer's doing: said once, plainly.
                errors.append(f"{label}: this design cannot be printed at the moment (a font is missing). Please contact the shop.")
                continue
            text_for_font = text.upper() if el.get("transform") == "uppercase" else text
            missing = font.unsupported(text_for_font)
            if missing:
                errors.append(f"{label}: these characters are not available in this font: {' '.join(missing)}")
            elif layout_text(el, text, font)["overflow"]:
                errors.append(f"{label}: the text is too long to fit. Please shorten it.")
        else:
            upload_id = v.get("upload") if v else None
            if not upload_id:
                if el.get("required", True) and not el.get("keepOriginalIfEmpty"):
                    errors.append(f"{label}: please add a photo.")
                continue
            ref = uploads(upload_id)
            if ref is None:
                errors.append(f"{label}: the photo could not be found; please upload it again.")
                continue
            placed = place_image(el["box"], ref.width, ref.height, v.get("zoom", 1), v.get("dx", 0), v.get("dy", 0))
            if placed["dpi"] < float(el.get("minDpi", 150)):
                warnings.append(
                    f"{label}: the photo is low resolution ({placed['dpi']:.0f} dpi at this size) and may print blurry."
                )
    return errors, warnings


# ---------------------------------------------------------------------------
# Print PDF
# ---------------------------------------------------------------------------

def _set_fill(c: rl_canvas.Canvas, color: dict) -> None:
    vals = color.get("values") or [0]
    space = color.get("space", "rgb")
    if space == "cmyk" and len(vals) == 4:
        c.setFillColorCMYK(*vals)
    elif space == "gray":
        c.setFillGray(vals[0])
    elif len(vals) == 3:
        c.setFillColorRGB(*vals)
    else:
        hexv = color.get("hex", "#000000").lstrip("#")
        c.setFillColorRGB(*(int(hexv[i : i + 2], 16) / 255 for i in (0, 2, 4)))


def _clip(c: rl_canvas.Canvas, el: dict, page_h: float) -> None:
    p = c.beginPath()
    if el.get("clipPath"):
        for seg in el["clipPath"]:
            op, pts = seg[0], seg[1:]
            flipped = [v if i % 2 == 0 else page_h - v for i, v in enumerate(pts)]
            if op == "m":
                p.moveTo(*flipped)
            elif op == "l":
                p.lineTo(*flipped)
            elif op == "c":
                p.curveTo(*flipped)
            elif op == "h":
                p.close()
    else:
        b = el["box"]
        p.rect(b["x"], page_h - b["y"] - b["h"], b["w"], b["h"])
    c.clipPath(p, stroke=0, fill=0)


def _draw_image(c: rl_canvas.Canvas, el: dict, value: dict, ref: UploadRef, page_h: float, max_dpi: int) -> None:
    placed = place_image(el["box"], ref.width, ref.height, value.get("zoom", 1), value.get("dx", 0), value.get("dy", 0))
    s = placed["scale"]
    box = el["box"]
    # Crop to the part of the photo that is visible in the slot (plus a pixel
    # of margin), then cap the resolution at max_dpi to keep files sensible.
    left = max(0, int((box["x"] - placed["x"]) / s) - 1)
    top = max(0, int((box["y"] - placed["y"]) / s) - 1)
    right = min(ref.width, int((box["x"] + box["w"] - placed["x"]) / s + 0.999) + 1)
    bottom = min(ref.height, int((box["y"] + box["h"] - placed["y"]) / s + 0.999) + 1)
    with Image.open(ref.path) as src:
        img = ImageOps.exif_transpose(src)
        img = img.crop((left, top, right, bottom))
    dest_x = placed["x"] + left * s
    dest_y = placed["y"] + top * s
    dest_w = (right - left) * s
    dest_h = (bottom - top) * s
    target_w = int(dest_w / 72 * max_dpi + 0.5)
    if img.width > target_w > 0:
        img = img.resize((target_w, max(1, round(img.height * target_w / img.width))), Image.LANCZOS)
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(buf, format="PNG")
        mask = "auto"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=92, subsampling=0)
        mask = None
    buf.seek(0)
    c.saveState()
    _clip(c, el, page_h)
    c.drawImage(ImageReader(buf), dest_x, page_h - dest_y - dest_h, dest_w, dest_h, mask=mask)
    c.restoreState()


def _draw_text(c: rl_canvas.Canvas, el: dict, text: str, fonts: FontLibrary, page_h: float) -> None:
    font = fonts.require(el["font"])
    lay = layout_text(el, text, font)
    c.saveState()
    _set_fill(c, el.get("color") or {})
    c.setFont(fonts.reportlab_name(font.name), lay["fontSize"])
    for line in lay["lines"]:
        if line["text"]:
            c.drawString(line["x"], page_h - line["y"], line["text"], charSpace=lay["letterSpacing"])
    c.restoreState()


def render_design_pdf(
    template: dict,
    pdf_path: Path,
    values: dict,
    uploads: UploadResolver,
    fonts: FontLibrary,
    max_dpi: int = 300,
) -> bytes:
    """Build the print-ready PDF for a design."""
    # 1. Customer content, one overlay page per element.
    overlay_buf = io.BytesIO()
    c = rl_canvas.Canvas(overlay_buf, pageCompression=1)
    overlay_pages: dict[str, int] = {}
    keep_original: set[str] = set()
    for el in template["elements"]:
        page = template["pages"][el["page"]]
        w, h = page["width"], page["height"]
        v = values.get(el["id"]) or {}
        if el["type"] == "image":
            ref = uploads(v["upload"]) if v.get("upload") else None
            if ref is None:
                if el.get("keepOriginalIfEmpty"):
                    keep_original.add(el["id"])
                continue
            c.setPageSize((w, h))
            _draw_image(c, el, v, ref, h, max_dpi)
        else:
            text = v.get("text", el.get("default", "")) if v else el.get("default", "")
            if not (text or "").strip():
                continue
            c.setPageSize((w, h))
            _draw_text(c, el, text, fonts, h)
        overlay_pages[el["id"]] = len(overlay_pages)
        c.showPage()
    c.save()

    # 2. Swap them into the designer's PDF at each layer's z-position.
    overlay = pikepdf.open(io.BytesIO(overlay_buf.getvalue())) if overlay_pages else None
    with pikepdf.open(pdf_path) as pdf:
        for page_def in template["pages"]:
            p = page_def["index"]
            page = pdf.pages[p]
            inserts = {}
            for el_id, idx in overlay_pages.items():
                if next(e for e in template["elements"] if e["id"] == el_id)["page"] == p:
                    form = overlay.pages[idx].as_form_xobject()
                    inserts[el_id] = pdf.copy_foreign(form)
            mb = page_def["mediaBox"]
            plan = engine.Plan(
                keep=lambda s, seg: s is None or s in keep_original,
                inserts=inserts,
                origin=(mb[0], mb[1]),
            )
            engine.transform_page(pdf, page, engine.slot_map(pdf, layer_slots(template, p)), plan)
        # Drop the designer's placeholder images/forms that are no longer drawn.
        pdf.remove_unreferenced_resources()
        pdf.docinfo["/Producer"] = "jasmin-pdf"
        out = io.BytesIO()
        pdf.save(out, compress_streams=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        if overlay is not None:
            overlay.close()
        return out.getvalue()
