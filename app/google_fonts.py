"""Google Fonts: search the catalogue, and fetch a font file to put in the studio.

The fonts there are open-licensed (OFL, Apache, UFL), so they may be embedded in
print PDFs and shown in the editor. The catalogue is Google's own list of families;
a file is asked for one style at a time, which gives a static TrueType file even
for families that are published as variable fonts.
"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx

CATALOGUE_URL = "https://fonts.google.com/metadata/fonts"
CSS_URL = "https://fonts.googleapis.com/css2"
FILE_HOST = "fonts.gstatic.com"
CATALOGUE_SECONDS = 24 * 3600
MAX_FONT_BYTES = 12 * 1024 * 1024
MAX_STYLES = 8  # per import
WEIGHTS = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium", 600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}
_STYLE = re.compile(r"([1-9]00)(i?)")
_FILE = re.compile(r"url\((https://fonts\.gstatic\.com/[A-Za-z0-9_./-]+\.ttf)\)")


class GoogleFontsError(Exception):
    """Google could not be reached, or did not have what was asked for."""


def style_label(style: str) -> str:
    """``700i`` -> ``Bold Italic``."""
    m = _STYLE.fullmatch(style)
    if not m:
        raise GoogleFontsError(f"{style!r} is not a font style")
    name = WEIGHTS[int(m.group(1))]
    if not m.group(2):
        return name
    return "Italic" if name == "Regular" else f"{name} Italic"


class GoogleFonts:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self.transport = transport
        self._families: list[dict] = []
        self._fetched = 0.0
        self._lock = threading.Lock()

    def _get(self, url: str, **kw) -> httpx.Response:
        try:
            # No browser is named: Google then answers with TrueType files, which is what printing needs.
            with httpx.Client(transport=self.transport, timeout=20.0, headers={"User-Agent": "jasmin-studio"}) as client:
                response = client.get(url, **kw)
        except httpx.HTTPError as exc:
            raise GoogleFontsError("Google Fonts cannot be reached right now. Try again in a moment.") from exc
        if response.status_code == 400:
            raise GoogleFontsError("Google Fonts does not have that font.")
        if response.status_code != 200:
            raise GoogleFontsError("Google Fonts did not answer as expected. Try again in a moment.")
        return response

    def families(self) -> list[dict]:
        with self._lock:
            if self._families and time.time() - self._fetched < CATALOGUE_SECONDS:
                return self._families
            try:
                text = self._get(CATALOGUE_URL).text
                listed = json.loads(text[text.index("{") :])["familyMetadataList"]
            except (GoogleFontsError, ValueError, KeyError):
                if self._families:  # yesterday's list is better than none
                    return self._families
                raise GoogleFontsError("Google Fonts cannot be reached right now. Try again in a moment.") from None
            families = []
            for f in listed:
                styles = sorted((s for s in f.get("fonts", {}) if _STYLE.fullmatch(s)), key=lambda s: (s.endswith("i"), s))
                if not styles or not f.get("isOpenSource", True):
                    continue
                families.append(
                    {
                        "family": f["family"],
                        "category": f.get("category", ""),
                        "popularity": f.get("popularity") or 99999,
                        "styles": [{"id": s, "label": style_label(s)} for s in styles],
                    }
                )
            families.sort(key=lambda f: f["popularity"])
            self._families, self._fetched = families, time.time()
            return families

    def categories(self) -> list[str]:
        return sorted({f["category"] for f in self.families() if f["category"]})

    def search(self, query: str = "", category: str = "", limit: int = 12) -> dict:
        """The most used families first; with a query, those whose name starts with it come before those that contain it."""
        q = re.sub(r"\s+", " ", query).strip().lower()
        found = [f for f in self.families() if (not category or f["category"] == category) and q in f["family"].lower()]
        if q:
            found.sort(key=lambda f: (not f["family"].lower().startswith(q), f["popularity"]))
        return {"total": len(found), "families": [{k: f[k] for k in ("family", "category", "styles")} for f in found[:limit]]}

    def family(self, name: str) -> dict:
        found = next((f for f in self.families() if f["family"] == name), None)
        if found is None:
            raise GoogleFontsError(f"Google Fonts has no font called {name}.")
        return found

    def download(self, family: str, style: str) -> bytes:
        """The TrueType file of one style of a family."""
        known = self.family(family)
        if style not in {s["id"] for s in known["styles"]}:
            raise GoogleFontsError(f"{family} has no {style_label(style)} style.")
        m = _STYLE.fullmatch(style)
        css = self._get(CSS_URL, params={"family": f"{family}:ital,wght@{1 if m.group(2) else 0},{m.group(1)}"}).text
        urls = _FILE.findall(css)
        if len(urls) != 1:
            raise GoogleFontsError(f"Google Fonts did not give a file for {family} {style_label(style)}.")
        # Only ever fetched from Google's own font host (the pattern above allows nothing else).
        data = self._get(urls[0]).content
        if len(data) > MAX_FONT_BYTES:
            raise GoogleFontsError(f"{family} {style_label(style)} is too large to import ({len(data) // (1024 * 1024)} MB).")
        return data
