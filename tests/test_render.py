import pikepdf
import pytest
from PIL import Image, ImageChops
from pdfminer.high_level import extract_text

from app.pdf import render
from app.pdf.analyze import analyze_pdf

from .conftest import PHOTOS, SAMPLE_PDF


@pytest.fixture(scope="module")
def definition(fonts):
    d = analyze_pdf(SAMPLE_PDF, fonts)
    for el in d["elements"]:
        if el["id"] == "story-photo":
            el["required"] = False
            el["keepOriginalIfEmpty"] = True
    return d


def uploads(upload_id):
    path = PHOTOS / upload_id
    if not path.exists():
        return None
    with Image.open(path) as im:
        return render.UploadRef(path, im.width, im.height)


def images_on_page(page):
    found = set()

    def walk(res, depth=0):
        for _, xobj in (res.get("/XObject") or {}).items():
            if xobj.get("/Subtype") == "/Image":
                found.add((int(xobj.Width), int(xobj.Height)))
            elif depth < 6:
                walk(xobj.get("/Resources") or {}, depth + 1)

    walk(page.Resources)
    return found


def test_print_pdf_swaps_content_in_place(definition, fonts):
    values = {
        "couple-photo": {"upload": "dusk-portrait.jpg", "zoom": 1.2},
        "couple-names": {"text": "Sofia & Mateo"},
        "venue": {"text": "Rosewood Farm\n88 Orchard Road"},
    }
    data = render.render_design_pdf(definition, SAMPLE_PDF, values, uploads, fonts, max_dpi=300)
    text = extract_text(__import__("io").BytesIO(data))
    assert "Sofia & Mateo" in text and "Emma & James" not in text
    assert "ROSEWOOD FARM" in text and "GLASSHOUSE" not in text
    assert "request the pleasure of your company" in text  # static wording untouched

    with pikepdf.open(__import__("io").BytesIO(data)) as pdf:
        front, back = pdf.pages
        assert [float(v) for v in front.trimbox] == [9, 9, 369, 513]  # boxes preserved
        imgs = images_on_page(front)
        assert (700, 850) not in imgs  # designer's sample photo removed
        (w, h), = imgs
        assert w == pytest.approx(140 / 72 * 300, abs=3)  # capped at 300 dpi
        assert (800, 600) in images_on_page(back)  # optional slot kept original

        # z-order: the photo is painted before the "Frame & Florals" layer.
        ops = [str(i.operator) + ":" + ",".join(map(str, i.operands)) for i in pikepdf.parse_content_stream(front)]
        photo_at = next(i for i, op in enumerate(ops) if op.startswith("Do:/JasminSlot"))
        frame_props = next(k for k, v in front.Resources.Properties.items() if str(v.Name) == "Frame & Florals")
        frame_at = next(i for i, op in enumerate(ops) if op == f"BDC:/OC,{frame_props}")
        assert photo_at < frame_at


def test_unchanged_text_lands_where_the_designer_put_it(definition, fonts):
    keep_images = {**definition, "elements": [
        {**el, "keepOriginalIfEmpty": True} if el["type"] == "image" else el for el in definition["elements"]
    ]}
    data = render.render_design_pdf(keep_images, SAMPLE_PDF, {}, uploads, fonts)
    for page in (0, 1):
        a = render.render_png(SAMPLE_PDF, page, 150).convert("L")
        b = render.render_png(data, page, 150).convert("L")
        diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 80 else 0)
        bbox = diff.getbbox()
        assert bbox is None or (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < 400


def test_validation(definition, fonts):
    errors, _ = render.validate_values(definition, {}, uploads, fonts)
    assert any("Couple photo" in e for e in errors)
    values = {
        "couple-photo": {"upload": "low-res.jpg"},
        "couple-names": {"text": "这是中文"},
        "date": {"text": "x" * 20 + "\n" + "y" * 20 + "\nthird line"},
        "rsvp": {"text": "   "},
    }
    errors, warnings = render.validate_values(definition, values, uploads, fonts)
    assert any("not available in this font" in e for e in errors)
    assert any("at most 2 lines" in e for e in errors)
    assert any("RSVP" in e or "Rsvp" in e for e in errors)
    assert any("low resolution" in w for w in warnings)


def test_preview_assets(definition, tmp_path):
    stacks = render.build_template_assets(definition, SAMPLE_PDF, tmp_path)
    front = [item.get("src") or item.get("id") for item in stacks[0]["stack"]]
    assert front == ["p0-seg0.png", "couple-photo", "p0-seg1.png", "couple-names", "date", "venue"]
    back = [item.get("src") or item.get("id") for item in stacks[1]["stack"]]
    assert back == ["p1-seg0.png", "story-photo", "p1-seg1.png", "rsvp"]
    seg1 = Image.open(tmp_path / "p0-seg1.png")
    assert seg1.mode == "RGBA" and seg1.getpixel((5, 5))[3] == 0  # transparent slice
    assert (tmp_path / "p0-layer-couple-photo.png").exists()
