import io

import pytest
from reportlab.pdfgen import canvas

from app.pdf.analyze import AnalysisError, analyze_pdf, customizable_base

from .conftest import SAMPLE_PDF


@pytest.mark.parametrize(
    "name, base",
    [
        ("couple-photo-customize", "couple-photo"),
        ("name-customize", "name"),
        ("Names Customise", "Names"),
        ("customize: date", "date"),
        ("date_customize", "date"),
        ("Background", None),
        ("customize", None),
        ("Customizer notes", None),
    ],
)
def test_layer_naming(name, base):
    assert customizable_base(name) == base


def test_sample_elements(fonts):
    result = analyze_pdf(SAMPLE_PDF, fonts)
    assert result["warnings"] == []
    els = {e["id"]: e for e in result["elements"]}
    assert set(els) == {"couple-photo", "couple-names", "date", "venue", "story-photo", "rsvp"}

    photo = els["couple-photo"]
    assert photo["type"] == "image" and photo["page"] == 0
    assert photo["box"] == {"x": 119.0, "y": 72.0, "w": 140.0, "h": 150.0}
    assert photo["clipPath"] and any(seg[0] == "c" for seg in photo["clipPath"])  # the arch

    story = els["story-photo"]
    assert story["page"] == 1 and story["clipPath"] is None  # rectangular clip

    names = els["couple-names"]
    assert names["default"] == "Emma & James"
    assert names["font"] == "GreatVibes-Regular"
    assert names["fontSize"] == 38.0
    assert names["align"] == "center"
    assert names["color"]["hex"] == "#a8834a"

    date = els["date"]
    assert date["multiline"] and date["maxLines"] == 2
    assert date["default"].split("\n") == ["Saturday, June 13, 2026", "at four o'clock in the afternoon"]
    assert date["lineHeight"] == pytest.approx(16 / 12.5, abs=1e-3)

    venue = els["venue"]  # attached via the form XObject's /OC entry
    assert venue["transform"] == "uppercase"
    assert venue["letterSpacing"] == pytest.approx(1.2 / 7, abs=1e-3)

    layer_names = [layer["name"] for layer in result["layers"]]
    assert "Frame & Florals" in layer_names and "rsvp-customize" in layer_names


def test_pdf_without_layers(fonts, tmp_path):
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 700, "Hello")
    c.save()
    path = tmp_path / "plain.pdf"
    path.write_bytes(buf.getvalue())
    result = analyze_pdf(path, fonts)
    assert result["elements"] == []
    assert any("no layers" in w for w in result["warnings"])


def test_not_a_pdf(fonts, tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"%PDF-1.4 garbage")
    with pytest.raises(AnalysisError):
        analyze_pdf(path, fonts)


def test_spot_colour_text_is_flagged(fonts, tmp_path):
    import pikepdf
    from reportlab.lib.colors import CMYKColorSep

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(300, 200))
    c._code.append("/OC /L1 BDC")
    c.setFillColor(CMYKColorSep(0, 0.2, 0.6, 0.3, spotName="PANTONE 871 C"))
    c.setFont("Helvetica", 20)
    c.drawCentredString(150, 100, "Gold names")
    c._code.append("EMC")
    c.save()
    pdf = pikepdf.open(io.BytesIO(buf.getvalue()))
    ocg = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.OCG, Name="names-customize"))
    pdf.pages[0].Resources.Properties = pikepdf.Dictionary(L1=ocg)
    pdf.Root.OCProperties = pikepdf.Dictionary(OCGs=[ocg], D=pikepdf.Dictionary(ON=[ocg]))
    path = tmp_path / "spot.pdf"
    pdf.save(path)

    result = analyze_pdf(path, fonts)
    (el,) = result["elements"]
    assert el["default"] == "Gold names"
    assert el["color"]["hex"] == "#000000"  # not white, which a tint-as-gray reading would give
    assert any("Separation colour" in w for w in result["warnings"])
