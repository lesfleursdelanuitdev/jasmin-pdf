"""Download the bundled OFL fonts from the Google Fonts repository and
write static TrueType instances into ``fonts/``.

reportlab (used for the print PDF) needs static TrueType fonts, so variable
fonts are instanced at a fixed weight with fontTools.

    python scripts/fetch_fonts.py
"""
from __future__ import annotations

import io
import sys
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "fonts"
BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/"

# Latin, Latin-1, Latin Extended-A, general punctuation, currency, and a few
# typographic symbols: enough for names, dates and addresses in European
# languages, while keeping the files small for the browser editor.
UNICODES = (
    list(range(0x20, 0x7F))
    + list(range(0xA0, 0x180))
    + list(range(0x2010, 0x2060))
    + list(range(0x20A0, 0x20C0))
    + [0x2122, 0x2190, 0x2192, 0x2665, 0x2661, 0x2764]
)

# (repo path, output file, weight or None for static fonts)
FONTS = [
    ("greatvibes/GreatVibes-Regular.ttf", "GreatVibes-Regular.ttf", None),
    ("playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf", "PlayfairDisplay-Regular.ttf", 400),
    ("playfairdisplay/PlayfairDisplay-Italic%5Bwght%5D.ttf", "PlayfairDisplay-Italic.ttf", 400),
    ("cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf", "CormorantGaramond-Regular.ttf", 400),
    ("cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf", "CormorantGaramond-SemiBold.ttf", 600),
    ("montserrat/Montserrat%5Bwght%5D.ttf", "Montserrat-Regular.ttf", 400),
    ("montserrat/Montserrat%5Bwght%5D.ttf", "Montserrat-Medium.ttf", 500),
]


def fetch(path: str) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=60) as resp:
        return resp.read()


def main() -> int:
    OUT.mkdir(exist_ok=True)
    cache: dict[str, bytes] = {}
    for src, dest, weight in FONTS:
        data = cache.get(src) or cache.setdefault(src, fetch(src))
        font = TTFont(io.BytesIO(data))
        if weight is not None:
            font = instancer.instantiateVariableFont(
                font, {"wght": weight}, updateFontNames=True
            )
        options = subset.Options()
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.notdef_outline = True
        subsetter = subset.Subsetter(options)
        subsetter.populate(unicodes=UNICODES)
        subsetter.subset(font)
        # Stable PostScript names; the analyzer matches PDF font names on them.
        font["name"].setName(Path(dest).stem, 6, 3, 1, 0x409)
        font.save(OUT / dest)
        print("wrote", dest)
    (OUT / "OFL.txt").write_bytes(fetch("greatvibes/OFL.txt"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
