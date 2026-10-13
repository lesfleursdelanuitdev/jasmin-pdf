"""Font library.

Customer text is re-typeset with a *full* font file: fonts embedded in the
designer's PDF are almost always subsets that only contain the glyphs of the
sample text.  The library holds the bundled OFL fonts plus fonts uploaded by
the designer, and matches PDF font names (``ABCDEF+GreatVibes-Regular``)
against them.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTFont as FTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont as RLTTFont

DEFAULT_FONT = "Montserrat-Regular"
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_STYLE_WORDS = ("regular", "roman", "book", "normal", "mt", "ps")


class FontError(ValueError):
    pass


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _strip_style(norm: str) -> str:
    for word in _STYLE_WORDS:
        if norm.endswith(word) and len(norm) > len(word):
            return norm[: -len(word)]
    return norm


def strip_subset_prefix(name: str) -> str:
    return _SUBSET_PREFIX.sub("", name.lstrip("/"))


def _ranges(codepoints: set[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for cp in sorted(codepoints):
        if out and cp == out[-1][1] + 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return out


@dataclass
class FontInfo:
    name: str  # PostScript name; also the id used in templates
    family: str
    style: str
    path: Path
    units_per_em: int
    ascent: float  # fraction of the em (hhea)
    descent: float  # fraction of the em, negative
    codepoints: set[int] = field(repr=False)
    # Advance width per code point, as a fraction of the em.  The browser
    # editor receives the same table, so both sides lay out text identically.
    advances: dict[int, float] = field(repr=False, default_factory=dict)
    source: str = "bundled"

    def public(self, url_prefix: str = "/fonts") -> dict:
        return {
            "name": self.name,
            "family": self.family,
            "style": self.style,
            "source": self.source,
            "url": f"{url_prefix}/{self.name}.ttf",
            "ascent": round(self.ascent, 5),
            "descent": round(self.descent, 5),
            "unicodeRanges": _ranges(self.codepoints),
        }

    def metrics(self) -> dict:
        """Everything the editor needs to lay out text exactly like the PDF."""
        return {
            **self.public(),
            "advances": {str(cp): round(adv, 5) for cp, adv in sorted(self.advances.items())},
        }

    def advance(self, ch: str) -> float:
        return self.advances.get(ord(ch), 0.5)

    def text_width(self, text: str, size: float, letter_spacing: float = 0.0) -> float:
        """Width of a single line, without kerning (matching reportlab output)."""
        n = len(text)
        return sum(self.advance(ch) for ch in text) * size + letter_spacing * max(0, n - 1)

    def unsupported(self, text: str) -> list[str]:
        return sorted({ch for ch in text if ch not in "\n\r\t" and ord(ch) not in self.codepoints})


def read_font(path: Path, source: str) -> FontInfo:
    try:
        ft = FTFont(str(path), lazy=True)
    except Exception as exc:  # noqa: BLE001 - fontTools raises many types
        raise FontError(f"Not a readable font file: {exc}") from exc
    if "glyf" not in ft:
        raise FontError(
            "Only TrueType-outline fonts (.ttf) are supported; this font has "
            "PostScript/CFF outlines (.otf). Export or download a .ttf version."
        )
    if "fvar" in ft:
        raise FontError("Variable fonts are not supported; upload a static instance (e.g. Regular).")
    names = ft["name"]
    ps = names.getDebugName(6) or path.stem
    family = names.getDebugName(16) or names.getDebugName(1) or ps
    style = names.getDebugName(17) or names.getDebugName(2) or "Regular"
    upem = ft["head"].unitsPerEm
    hhea = ft["hhea"]
    cmap = ft.getBestCmap() or {}
    hmtx = ft["hmtx"]
    advances = {cp: hmtx[glyph][0] / upem for cp, glyph in cmap.items() if glyph in hmtx.metrics}
    return FontInfo(
        name=ps,
        family=family,
        style=style,
        path=path,
        units_per_em=upem,
        ascent=hhea.ascent / upem,
        descent=hhea.descent / upem,
        codepoints=set(cmap.keys()),
        advances=advances,
        source=source,
    )


class FontLibrary:
    def __init__(self, bundled_dir: Path, upload_dir: Path):
        self.bundled_dir = bundled_dir
        self.upload_dir = upload_dir
        self._fonts: dict[str, FontInfo] = {}
        self._registered: set[str] = set()
        self._lock = threading.Lock()
        self.refresh()

    def refresh(self) -> None:
        fonts: dict[str, FontInfo] = {}
        for directory, source in ((self.bundled_dir, "bundled"), (self.upload_dir, "uploaded")):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.ttf")):
                try:
                    info = read_font(path, source)
                except FontError:
                    continue
                fonts[info.name] = info
        self._fonts = fonts

    def all(self) -> list[FontInfo]:
        return sorted(self._fonts.values(), key=lambda f: (f.family.lower(), f.style))

    def get(self, name: str) -> FontInfo | None:
        return self._fonts.get(name)

    def require(self, name: str) -> FontInfo:
        info = self._fonts.get(name) or self._fonts.get(DEFAULT_FONT)
        if info is None:
            raise FontError(f"Font {name!r} is not installed")
        return info

    def match(self, pdf_font_name: str) -> FontInfo | None:
        """Find the library font for a (possibly subset-prefixed) PDF font name."""
        wanted = _norm(strip_subset_prefix(pdf_font_name))
        if not wanted:
            return None
        by_norm = {_norm(f.name): f for f in self._fonts.values()}
        if wanted in by_norm:
            return by_norm[wanted]
        stripped = {_strip_style(k): f for k, f in by_norm.items()}
        if _strip_style(wanted) in stripped:
            return stripped[_strip_style(wanted)]
        # Family match, e.g. "GreatVibes" or "Great Vibes,Regular".
        for f in self.all():
            fam = _norm(f.family)
            if wanted.startswith(fam) and _strip_style(wanted[len(fam):]) in ("", _strip_style(_norm(f.style))):
                return f
        return None

    def add_upload(self, filename: str, data: bytes) -> FontInfo:
        if not filename.lower().endswith(".ttf"):
            raise FontError("Upload a .ttf file")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.upload_dir / f".upload-{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        try:
            info = read_font(tmp, "uploaded")
        except FontError:
            tmp.unlink(missing_ok=True)
            raise
        safe = re.sub(r"[^A-Za-z0-9_.-]", "", info.name) or "font"
        dest = self.upload_dir / f"{safe}.ttf"
        tmp.replace(dest)
        self.refresh()
        return self._fonts[info.name]

    def reportlab_name(self, name: str) -> str:
        """Register the font with reportlab (once) and return its name there."""
        info = self.require(name)
        key = f"jasmin-{info.name}"
        with self._lock:
            if key not in self._registered:
                pdfmetrics.registerFont(RLTTFont(key, str(info.path)))
                self._registered.add(key)
        return key
