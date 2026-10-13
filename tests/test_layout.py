import json
import shutil
import subprocess

import pytest

from app.layout import layout_text, place_image

from .conftest import ROOT

BOX = {"x": 10.0, "y": 20.0, "w": 200.0, "h": 50.0}


def text_el(**kw):
    el = {
        "box": dict(BOX),
        "fontSize": 30.0,
        "minFontSize": 12.0,
        "lineHeight": 1.2,
        "letterSpacing": 0.0,
        "align": "center",
        "valign": "middle",
        "autoFit": True,
        "multiline": False,
        "transform": "none",
    }
    el.update(kw)
    return el


def test_short_text_keeps_size_and_centres(fonts):
    font = fonts.get("GreatVibes-Regular")
    lay = layout_text(text_el(), "Ana", font)
    assert lay["fontSize"] == 30.0 and not lay["overflow"]
    line = lay["lines"][0]
    assert line["x"] + line["width"] / 2 == pytest.approx(BOX["x"] + BOX["w"] / 2)


def test_long_text_shrinks_to_fit(fonts):
    font = fonts.get("GreatVibes-Regular")
    lay = layout_text(text_el(), "Alexandra Josephine & Benjamin Maximilian", font)
    assert 12.0 <= lay["fontSize"] < 30.0
    assert lay["lines"][0]["width"] == pytest.approx(BOX["w"])
    assert not lay["overflow"]


def test_overflow_when_below_min_size(fonts):
    font = fonts.get("GreatVibes-Regular")
    lay = layout_text(text_el(minFontSize=28.0), "A very very long line of text that cannot fit", font)
    assert lay["fontSize"] == 28.0 and lay["overflow"]


def test_uppercase_and_single_line(fonts):
    font = fonts.get("Montserrat-Medium")
    lay = layout_text(text_el(transform="uppercase"), "rosewood\nfarm", font)
    assert [ln["text"] for ln in lay["lines"]] == ["ROSEWOOD FARM"]


def test_place_image_cover_and_clamp():
    box = {"x": 0, "y": 0, "w": 100, "h": 100}
    p = place_image(box, 2000, 1000)  # landscape photo in a square slot
    assert p["h"] == pytest.approx(100) and p["w"] == pytest.approx(200)
    assert p["dpi"] == pytest.approx(72 * 1000 / 100)
    p = place_image(box, 2000, 1000, zoom=1, dx=500, dy=500)  # clamped
    assert p["dx"] == pytest.approx(50) and p["dy"] == 0
    assert p["x"] == pytest.approx(0)


CASES = [
    ("GreatVibes-Regular", {}, "Emma & James"),
    ("GreatVibes-Regular", {}, "Alexandra Josephine & Benjamin Maximilian"),
    ("Montserrat-Medium", {"transform": "uppercase", "letterSpacing": 0.17, "multiline": True}, "Villa Été\nRue des Fleurs"),
    ("CormorantGaramond-Regular", {"align": "left", "valign": "top", "multiline": True, "lineHeight": 1.4}, "Zoë\nKristóf"),
    ("PlayfairDisplay-Italic", {"align": "right", "valign": "bottom"}, "Dinner & dancing"),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_port_matches_python(fonts, tmp_path):
    """static/js/layout.js must produce the same layout as app/layout.py."""
    cases = []
    for font_name, overrides, text in CASES:
        el = text_el(**overrides)
        cases.append({"font": fonts.get(font_name).metrics(), "el": el, "text": text})
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    script = tmp_path / "run.mjs"
    script.write_text(
        f"""
import {{ readFileSync }} from "node:fs";
import {{ FontMetrics, layoutText, placeImage }} from "{(ROOT / 'static/js/layout.js').as_uri()}";
const cases = JSON.parse(readFileSync({json.dumps(str(tmp_path / 'cases.json'))}, "utf8"));
const out = cases.map((c) => layoutText(c.el, c.text, new FontMetrics(c.font)));
out.push(placeImage({{x: 3, y: 4, w: 120, h: 90}}, 1600, 2400, 1.7, 13, -40));
console.log(JSON.stringify(out));
"""
    )
    result = json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)
    for (font_name, overrides, text), js in zip(CASES, result):
        py = layout_text(text_el(**overrides), text, fonts.get(font_name))
        assert js["fontSize"] == pytest.approx(py["fontSize"], abs=1e-9)
        assert js["overflow"] == py["overflow"]
        for a, b in zip(js["lines"], py["lines"]):
            assert a["text"] == b["text"]
            assert (a["x"], a["y"], a["width"]) == pytest.approx((b["x"], b["y"], b["width"]), abs=1e-9)
    py_img = place_image({"x": 3, "y": 4, "w": 120, "h": 90}, 1600, 2400, 1.7, 13, -40)
    assert {k: pytest.approx(v) for k, v in py_img.items()} == result[-1]
