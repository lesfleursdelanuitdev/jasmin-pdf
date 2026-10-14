"""Google Fonts: search, and import into the studio. Google itself is played by a fake."""
from __future__ import annotations

import json

import httpx
import pytest

from app.config import ROOT
from app.google_fonts import GoogleFonts, GoogleFontsError, style_label

CATALOGUE = {
    "familyMetadataList": [
        {"family": "Great Vibes", "category": "Handwriting", "popularity": 90, "fonts": {"400": {}}},
        {"family": "Montserrat", "category": "Sans Serif", "popularity": 5, "fonts": {"400": {}, "400i": {}, "500": {}, "700": {}}},
        {"family": "Cormorant Garamond", "category": "Serif", "popularity": 60, "fonts": {"400": {}, "600": {}}},
        {"family": "Vibes", "category": "Display", "popularity": 300, "fonts": {"400": {}}},
        {"family": "Closed Sans", "category": "Sans Serif", "popularity": 1, "isOpenSource": False, "fonts": {"400": {}}},
    ]
}
FILES = {  # which bundled file stands in for which Google file
    ("Great Vibes", "0,400"): "GreatVibes-Regular.ttf",
    ("Montserrat", "0,500"): "Montserrat-Medium.ttf",
    ("Cormorant Garamond", "0,600"): "CormorantGaramond-SemiBold.ttf",
}


class FakeGoogle:
    def __init__(self):
        self.calls = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if self.down:
            raise httpx.ConnectError("no route", request=request)
        if request.url.host == "fonts.google.com":
            return httpx.Response(200, text=")]}'\n" + json.dumps(CATALOGUE))
        if request.url.host == "fonts.googleapis.com":
            family, _, style = request.url.params["family"].partition(":ital,wght@")
            name = FILES.get((family, style))
            if name is None:
                return httpx.Response(400, text="missing")
            return httpx.Response(200, text=f"@font-face {{ src: url(https://fonts.gstatic.com/s/x/v1/{name}) format('truetype'); }}")
        if request.url.host == "fonts.gstatic.com":
            return httpx.Response(200, content=(ROOT / "fonts" / request.url.path.rsplit("/", 1)[1]).read_bytes())
        return httpx.Response(404)


@pytest.fixture
def google(app):
    fake = FakeGoogle()
    app.state.studio.google.transport = httpx.MockTransport(fake)
    return fake


def test_style_names():
    assert [style_label(s) for s in ("400", "400i", "700", "600i", "100")] == ["Regular", "Italic", "Bold", "SemiBold Italic", "Thin"]
    with pytest.raises(GoogleFontsError):
        style_label("450")


def test_search_puts_the_most_used_first_and_names_that_start_with_the_query_before_the_rest():
    fake = FakeGoogle()
    g = GoogleFonts(httpx.MockTransport(fake))
    assert [f["family"] for f in g.search()["families"]] == ["Montserrat", "Cormorant Garamond", "Great Vibes", "Vibes"]  # not the closed one
    assert [f["family"] for f in g.search("vibes")["families"]] == ["Vibes", "Great Vibes"]
    assert [f["family"] for f in g.search(category="Serif")["families"]] == ["Cormorant Garamond"]
    assert g.search("  MONT ")["families"][0]["styles"] == [
        {"id": "400", "label": "Regular"}, {"id": "500", "label": "Medium"}, {"id": "700", "label": "Bold"}, {"id": "400i", "label": "Italic"},
    ]
    assert g.search(limit=2)["total"] == 4 and g.categories() == ["Display", "Handwriting", "Sans Serif", "Serif"]
    assert len(fake.calls) == 1  # the catalogue is fetched once and kept
    fake.down = True
    g._fetched = 0  # the kept list has gone stale and Google is away: the old list still serves
    assert g.search("vibes")["total"] == 2


def test_google_being_away_is_said_plainly(admin, google):
    google.down = True
    r = admin.get("/admin/api/google-fonts?q=vibes")
    assert r.status_code == 502 and "cannot be reached" in r.json()["detail"]


def test_a_font_can_only_be_fetched_from_googles_font_host():
    def evil(request):
        if request.url.host == "fonts.google.com":
            return httpx.Response(200, text=json.dumps(CATALOGUE))
        if request.url.host == "fonts.googleapis.com":
            return httpx.Response(200, text="src: url(https://internal.example/secret.ttf)")
        raise AssertionError(f"fetched {request.url}")

    with pytest.raises(GoogleFontsError, match="did not give a file"):
        GoogleFonts(httpx.MockTransport(evil)).download("Great Vibes", "400")


def test_the_admin_searches_and_sees_what_is_already_in_the_studio(admin, client, google):
    assert client.get("/admin/api/google-fonts").status_code == 401
    out = admin.get("/admin/api/google-fonts?q=montserrat").json()
    styles = {s["label"]: s["installed"] for s in out["families"][0]["styles"]}
    assert styles == {"Regular": True, "Medium": True, "Bold": False, "Italic": False}  # the bundled ones count
    assert "Serif" in out["categories"]


def test_importing_adds_new_styles_and_leaves_fonts_that_are_there_alone(app, admin, google, svc):
    svc.fonts.bundled_dir, kept = svc.fonts.bundled_dir, svc.fonts.get("GreatVibes-Regular").path
    r = admin.post("/admin/api/google-fonts", json={"family": "Great Vibes", "styles": ["400", "400"]})
    assert r.status_code == 200 and r.json() == {"added": [], "already": ["Great Vibes Regular"]}
    assert svc.fonts.get("GreatVibes-Regular").path == kept and not list(svc.storage.local("fonts").glob("*.ttf"))

    # A font the studio does not have yet: take the bundled copies away from this library first.
    svc.fonts.bundled_dir = svc.storage.local("nowhere")
    svc.fonts.refresh()
    r = admin.post("/admin/api/google-fonts", json={"family": "Cormorant Garamond", "styles": ["600"]})
    assert r.status_code == 200, r.text
    added = r.json()["added"]
    assert [a["name"] for a in added] == ["CormorantGaramond-SemiBold"] and added[0]["source"] == "uploaded"
    assert svc.storage.has("fonts/CormorantGaramond-SemiBold.ttf")
    assert admin.get("/fonts/CormorantGaramond-SemiBold.ttf").status_code == 200
    assert any(f["name"] == "CormorantGaramond-SemiBold" for f in admin.get("/admin/api/fonts").json())


def test_asking_for_what_google_does_not_have_changes_nothing(admin, google, svc):
    before = len(svc.fonts.all())
    assert admin.post("/admin/api/google-fonts", json={"family": "Nope", "styles": ["400"]}).status_code == 502
    assert admin.post("/admin/api/google-fonts", json={"family": "Great Vibes", "styles": ["700"]}).status_code == 502
    assert admin.post("/admin/api/google-fonts", json={"family": "Great Vibes", "styles": []}).status_code == 422
    assert admin.post("/admin/api/google-fonts", json={"family": "Montserrat", "styles": ["400"] * 2 + ["100", "200", "300", "500", "600", "700", "800"]}).status_code == 422
    assert admin.post("/admin/api/google-fonts", json={"family": "Great Vibes", "styles": ["bold"]}).status_code == 502
    assert len(svc.fonts.all()) == before
