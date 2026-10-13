"""Text layout and photo placement.

This is the single source of truth for *where* customer content goes.  The
browser editor has a line-for-line port in ``static/js/layout.js``; both use
the font advance tables from :mod:`app.fonts`, so the live preview and the
print PDF agree to the hundredth of a point.

Coordinates are template space: points, origin at the top-left of the
page's MediaBox, y pointing down.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .fonts import FontInfo

MAX_ZOOM = 8.0
EPS = 1e-6


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    @classmethod
    def of(cls, d: dict) -> "Box":
        return cls(float(d["x"]), float(d["y"]), float(d["w"]), float(d["h"]))


def prepare_text(element: dict, text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    if element.get("transform") == "uppercase":
        text = text.upper()
    if not element.get("multiline"):
        text = " ".join(part for part in text.split("\n"))
    return text.split("\n")


def layout_text(element: dict, text: str, font: FontInfo) -> dict:
    box = Box.of(element["box"])
    lines = prepare_text(element, text)
    ls_em = float(element.get("letterSpacing", 0.0))  # fraction of font size
    line_height = float(element.get("lineHeight", 1.2))
    size = float(element["fontSize"])
    min_size = float(element.get("minFontSize", size))
    auto_fit = element.get("autoFit", True)

    units = [sum(font.advance(ch) for ch in line) + ls_em * max(0, len(line) - 1) for line in lines]
    max_units = max(units) if units else 0.0
    block_em = (font.ascent - font.descent) + (len(lines) - 1) * line_height

    if auto_fit:
        if max_units * size > box.w + EPS:
            size = box.w / max_units
        if block_em * size > box.h + EPS:
            size = min(size, box.h / block_em)
        size = max(size, min_size)
    overflow = max_units * size > box.w + 0.01 or block_em * size > box.h + 0.01

    block_h = block_em * size
    valign = element.get("valign", "middle")
    if valign == "top":
        top = box.y
    elif valign == "bottom":
        top = box.y + box.h - block_h
    else:
        top = box.y + (box.h - block_h) / 2
    baseline = top + font.ascent * size

    align = element.get("align", "center")
    out_lines = []
    for i, (line, u) in enumerate(zip(lines, units)):
        width = u * size
        if align == "left":
            x = box.x
        elif align == "right":
            x = box.x + box.w - width
        else:
            x = box.x + (box.w - width) / 2
        out_lines.append({"text": line, "x": x, "y": baseline + i * line_height * size, "width": width})
    return {
        "fontSize": size,
        "letterSpacing": ls_em * size,
        "lines": out_lines,
        "overflow": overflow,
    }


def place_image(box_d: dict, img_w: int, img_h: int, zoom: float = 1.0, dx: float = 0.0, dy: float = 0.0) -> dict:
    """Cover-fit an image in the slot box, then apply the customer's zoom/pan.

    ``dx``/``dy`` are offsets of the image centre from the slot centre, in
    points; they are clamped so the photo always covers the whole slot.
    """
    box = Box.of(box_d)
    zoom = min(max(float(zoom or 1.0), 1.0), MAX_ZOOM)
    s = max(box.w / img_w, box.h / img_h) * zoom
    w, h = img_w * s, img_h * s
    max_dx, max_dy = (w - box.w) / 2, (h - box.h) / 2
    dx = min(max(float(dx or 0.0), -max_dx), max_dx)
    dy = min(max(float(dy or 0.0), -max_dy), max_dy)
    return {
        "x": box.x + (box.w - w) / 2 + dx,
        "y": box.y + (box.h - h) / 2 + dy,
        "w": w,
        "h": h,
        "scale": s,  # points per pixel
        "dpi": 72.0 / s,
        "zoom": zoom,
        "dx": dx,
        "dy": dy,
    }
