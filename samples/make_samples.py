"""Generate the sample assets used for demos and tests.

* ``samples/wedding-invitation.pdf`` - a two-page, layered (optional content
  group) invitation, structured the way Illustrator / InDesign exports look:
  every top-level layer is an OCG, customizable layers are named
  ``<something>-customize``.  It deliberately exercises the tricky cases:

  - a photo clipped to an arch, with decorative flowers drawn *above* it
    (customer content must keep its z-order);
  - multi-line, letter-spaced text;
  - a layer attached to a form XObject via its ``/OC`` entry;
  - a back page whose whole content lives inside a form XObject that
    contains the layer markers;
  - bleed (MediaBox) around the trim (TrimBox).

* ``samples/photos/*.jpg`` - procedurally generated "customer photos".

    python samples/make_samples.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import pikepdf
from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib.colors import Color, HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
FONTS = ROOT / "fonts"

BLEED = 9  # 1/8 inch
TRIM_W, TRIM_H = 360, 504  # 5 x 7 inches
PAGE_W, PAGE_H = TRIM_W + 2 * BLEED, TRIM_H + 2 * BLEED
CX = PAGE_W / 2

GOLD = HexColor("#A8834A")
INK = HexColor("#4A3B30")
BLUSH = HexColor("#E8C4B8")
SAGE = HexColor("#9AAA8C")


# --------------------------------------------------------------------------
# Photos
# --------------------------------------------------------------------------

def make_photo(path: Path, size: tuple[int, int], palette: str, seed: int, quality=90):
    """A soft landscape: gradient sky, sun, layered hills, bokeh."""
    rnd = random.Random(seed)
    w, h = size
    palettes = {
        "sunset": [(255, 214, 170), (240, 150, 140), (130, 100, 150)],
        "ocean": [(200, 230, 245), (120, 180, 210), (40, 90, 130)],
        "meadow": [(235, 240, 210), (170, 205, 150), (70, 120, 80)],
        "dusk": [(250, 220, 200), (190, 150, 190), (60, 60, 110)],
    }
    top, mid, low = palettes[palette]
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        a, b, u = (top, mid, t * 2) if t < 0.5 else (mid, low, (t - 0.5) * 2)
        col = tuple(int(a[i] + (b[i] - a[i]) * u) for i in range(3))
        for x in range(w):
            px[x, y] = col
    draw = ImageDraw.Draw(img, "RGBA")
    sun_r = int(min(w, h) * 0.12)
    sx, sy = int(w * rnd.uniform(0.55, 0.75)), int(h * 0.38)
    draw.ellipse([sx - sun_r, sy - sun_r, sx + sun_r, sy + sun_r], fill=(255, 245, 220, 210))
    for layer in range(4):
        base = h * (0.55 + layer * 0.11)
        amp = h * (0.05 - layer * 0.008)
        freq = rnd.uniform(1.2, 2.6)
        phase = rnd.uniform(0, 6.28)
        shade = tuple(int(low[i] * (0.9 - layer * 0.17) + mid[i] * 0.1) for i in range(3))
        pts = [(0, h)]
        for x in range(0, w + 1, max(1, w // 120)):
            pts.append((x, base + amp * math.sin(freq * x / w * 6.28 + phase)))
        pts.append((w, h))
        draw.polygon(pts, fill=shade + (255,))
    bokeh = Image.new("RGBA", size, (0, 0, 0, 0))
    bd = ImageDraw.Draw(bokeh)
    for _ in range(40):
        r = rnd.randint(int(w * 0.01), int(w * 0.04))
        x, y = rnd.randint(0, w), rnd.randint(0, int(h * 0.6))
        bd.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 240, rnd.randint(30, 80)))
    bokeh = bokeh.filter(ImageFilter.GaussianBlur(radius=max(2, w // 300)))
    img = Image.alpha_composite(img.convert("RGBA"), bokeh).convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, quality=quality)


# --------------------------------------------------------------------------
# Layered PDF
# --------------------------------------------------------------------------

def register_fonts():
    for name in [
        "GreatVibes-Regular",
        "CormorantGaramond-Regular",
        "CormorantGaramond-SemiBold",
        "Montserrat-Medium",
        "PlayfairDisplay-Italic",
    ]:
        pdfmetrics.registerFont(TTFont(name, str(FONTS / f"{name}.ttf")))


def begin_layer(c: canvas.Canvas, tag: str):
    # reportlab has no optional-content API; we emit the marked-content
    # operators directly and wire up /Properties + /OCProperties afterwards.
    c._code.append(f"/OC /{tag} BDC")


def end_layer(c: canvas.Canvas):
    c._code.append("EMC")


def arch_path(c: canvas.Canvas, x0, y0, w, straight_h):
    """Rectangle with a semicircular top (an arch window)."""
    r = w / 2
    k = 0.5523 * r
    p = c.beginPath()
    p.moveTo(x0, y0)
    p.lineTo(x0 + w, y0)
    p.lineTo(x0 + w, y0 + straight_h)
    cx, cy = x0 + r, y0 + straight_h
    p.curveTo(x0 + w, cy + k, cx + k, cy + r, cx, cy + r)
    p.curveTo(cx - k, cy + r, x0, cy + k, x0, cy)
    p.close()
    return p


def flower(c: canvas.Canvas, x, y, r, petal: Color, center: Color, rot=0.0):
    c.saveState()
    c.translate(x, y)
    c.rotate(rot)
    c.setFillColor(SAGE)
    for ang in (35, 145):  # two leaves
        c.saveState()
        c.rotate(ang)
        p = c.beginPath()
        p.moveTo(r * 0.6, 0)
        p.curveTo(r * 1.2, r * 0.45, r * 2.0, r * 0.35, r * 2.3, 0)
        p.curveTo(r * 2.0, -r * 0.35, r * 1.2, -r * 0.45, r * 0.6, 0)
        c.drawPath(p, stroke=0, fill=1)
        c.restoreState()
    c.setFillColor(petal)
    for i in range(6):
        a = math.radians(i * 60)
        c.circle(math.cos(a) * r * 0.55, math.sin(a) * r * 0.55, r * 0.5, stroke=0, fill=1)
    c.setFillColor(center)
    c.circle(0, 0, r * 0.32, stroke=0, fill=1)
    c.restoreState()


def front_page(c: canvas.Canvas, photo: Path):
    # Layer: Background ------------------------------------------------
    begin_layer(c, "OC1")
    c.setFillColor(HexColor("#FBF6EF"))
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.6)
    c.rect(BLEED + 16, BLEED + 16, TRIM_W - 32, TRIM_H - 32, stroke=1, fill=0)
    end_layer(c)

    # Layer: couple-photo-customize (arch clip) ------------------------
    ax, ay, aw, ah = CX - 70, 300, 140, 80  # arch: 140 wide, top at 450
    begin_layer(c, "OC2")
    c.saveState()
    c.clipPath(arch_path(c, ax, ay, aw, ah), stroke=0, fill=0)
    # The sample photo is larger than the arch, as in a real clip group.
    c.drawImage(str(photo), ax - 20, ay - 10, aw + 40, 170)
    c.restoreState()
    end_layer(c)

    # Layer: Frame & Florals (drawn ABOVE the photo) -------------------
    begin_layer(c, "OC3")
    c.setStrokeColor(GOLD)
    c.setLineWidth(1.2)
    c.drawPath(arch_path(c, ax - 6, ay - 6, aw + 12, ah + 6), stroke=1, fill=0)
    flower(c, ax - 2, ay + 2, 14, BLUSH, GOLD, rot=10)
    flower(c, ax + aw + 2, ay + 2, 14, BLUSH, GOLD, rot=-25)
    flower(c, ax + 18, ay - 6, 9, HexColor("#F3DDD3"), GOLD, rot=40)
    end_layer(c)

    # Layer: Text (static wording) -------------------------------------
    begin_layer(c, "OC4")
    c.setFillColor(INK)
    c.setFont("Montserrat-Medium", 6.5)
    c.drawCentredString(CX, 474, "TOGETHER WITH THEIR FAMILIES", charSpace=1.4)
    c.setFont("CormorantGaramond-Regular", 11)
    c.drawCentredString(CX, 222, "request the pleasure of your company")
    c.drawCentredString(CX, 208, "at the celebration of their marriage")
    c.setFont("PlayfairDisplay-Italic", 9.5)
    c.setFillColor(GOLD)
    c.drawCentredString(CX, 74, "Dinner & dancing to follow")
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.5)
    c.line(CX - 40, 194, CX + 40, 194)
    end_layer(c)

    # Layer: couple-names-customize -------------------------------------
    begin_layer(c, "OC5")
    c.setFillColor(GOLD)
    c.setFont("GreatVibes-Regular", 38)
    c.drawCentredString(CX, 248, "Emma & James")
    end_layer(c)

    # Layer: date-customize (two lines) ---------------------------------
    begin_layer(c, "OC6")
    c.setFillColor(INK)
    c.setFont("CormorantGaramond-SemiBold", 12.5)
    c.drawCentredString(CX, 172, "Saturday, June 13, 2026")
    c.drawCentredString(CX, 156, "at four o'clock in the afternoon")
    end_layer(c)

    # Layer: venue-customize, as a form XObject with an /OC entry ---------
    c.beginForm("venue")
    c.setFillColor(INK)
    c.setFont("Montserrat-Medium", 7)
    c.drawCentredString(CX, 124, "THE GLASSHOUSE CONSERVATORY", charSpace=1.2)
    c.drawCentredString(CX, 112, "12 WILLOW LANE · HUDSON, NEW YORK", charSpace=1.2)
    c.endForm()
    c.doForm("venue")


def back_page(c: canvas.Canvas, photo: Path):
    # The whole back is one form XObject that contains the layer markers,
    # like some exporters produce.
    c.beginForm("back")
    begin_layer(c, "OC7")
    c.setFillColor(HexColor("#F6EAE4"))
    c.rect(0, 0, PAGE_W, PAGE_H, stroke=0, fill=1)
    flower(c, BLEED + 40, PAGE_H - BLEED - 40, 12, BLUSH, GOLD, rot=15)
    flower(c, PAGE_W - BLEED - 40, BLEED + 40, 12, BLUSH, GOLD, rot=-30)
    end_layer(c)

    begin_layer(c, "OC8")  # story-photo-customize (optional)
    c.saveState()
    p = c.beginPath()
    p.rect(CX - 80, 300, 160, 120)
    c.clipPath(p, stroke=0, fill=0)
    c.drawImage(str(photo), CX - 80, 300, 160, 120)
    c.restoreState()
    end_layer(c)

    begin_layer(c, "OC9")  # static text + photo frame
    c.setStrokeColor(GOLD)
    c.setLineWidth(0.8)
    c.rect(CX - 84, 296, 168, 128, stroke=1, fill=0)
    c.setFillColor(GOLD)
    c.setFont("GreatVibes-Regular", 30)
    c.drawCentredString(CX, 250, "Kindly reply")
    end_layer(c)

    begin_layer(c, "OC10")  # rsvp-customize
    c.setFillColor(INK)
    c.setFont("CormorantGaramond-Regular", 12)
    c.drawCentredString(CX, 222, "by the first of May")
    c.drawCentredString(CX, 206, "to Emma · (555) 123-4567")
    end_layer(c)
    c.endForm()
    c.doForm("back")


LAYERS = {
    "OC1": "Background",
    "OC2": "couple-photo-customize",
    "OC3": "Frame & Florals",
    "OC4": "Text",
    "OC5": "couple-names-customize",
    "OC6": "date-customize",
    "OC_VENUE": "venue-customize",
    "OC7": "Back Background",
    "OC8": "story-photo-customize",
    "OC9": "Back Text",
    "OC10": "rsvp-customize",
}


def add_optional_content(src: Path, dest: Path):
    pdf = pikepdf.open(src)
    ocgs = {
        tag: pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name=name))
        for tag, name in LAYERS.items()
    }
    props = pikepdf.Dictionary({f"/{t}": ref for t, ref in ocgs.items()})

    def wire(resources_owner):
        res = resources_owner.get("/Resources")
        if res is None:
            res = resources_owner.Resources = pikepdf.Dictionary()
        res.Properties = props

    for page in pdf.pages:
        page.trimbox = [BLEED, BLEED, PAGE_W - BLEED, PAGE_H - BLEED]
        page.bleedbox = [0, 0, PAGE_W, PAGE_H]
        wire(page.obj)
        for name, xobj in page.Resources.get("/XObject", {}).items():
            if xobj.get("/Subtype") != "/Form":
                continue
            wire(xobj)
            if "venue" in str(name):
                xobj.OC = ocgs["OC_VENUE"]

    order = pikepdf.Array(list(ocgs.values()))
    pdf.Root.OCProperties = pikepdf.Dictionary(
        OCGs=order,
        D=pikepdf.Dictionary(
            Name=pikepdf.String("Layers"),
            Order=order,
            ON=order,
            OFF=pikepdf.Array(),
            BaseState=pikepdf.Name.ON,
        ),
    )
    pdf.docinfo["/Title"] = "Wedding invitation (sample template)"
    pdf.docinfo["/Creator"] = "jasmin-pdf samples/make_samples.py"
    pdf.save(dest)


def make_template(dest: Path):
    register_fonts()
    photos = SAMPLES / "photos"
    tmp = dest.with_suffix(".tmp.pdf")
    c = canvas.Canvas(str(tmp), pagesize=(PAGE_W, PAGE_H), pageCompression=1)
    c.setTitle("Wedding invitation")
    front_page(c, photos / "_template-sample-arch.jpg")
    c.showPage()
    back_page(c, photos / "_template-sample-story.jpg")
    c.showPage()
    c.save()
    add_optional_content(tmp, dest)
    tmp.unlink()


def main():
    photos = SAMPLES / "photos"
    # Photos baked into the template (the designer's placeholders).
    make_photo(photos / "_template-sample-arch.jpg", (700, 850), "sunset", 1, quality=85)
    make_photo(photos / "_template-sample-story.jpg", (800, 600), "meadow", 2, quality=85)
    # Photos a customer might upload.
    make_photo(photos / "beach-landscape.jpg", (2400, 1600), "ocean", 3)
    make_photo(photos / "dusk-portrait.jpg", (1600, 2400), "dusk", 4)
    make_photo(photos / "low-res.jpg", (240, 180), "meadow", 5)
    make_template(SAMPLES / "wedding-invitation.pdf")
    print("wrote samples/wedding-invitation.pdf and samples/photos/")


if __name__ == "__main__":
    main()
