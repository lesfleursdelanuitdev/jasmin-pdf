// Text layout and photo placement: a line-for-line port of app/layout.py.
// Both sides use the same font advance tables, so the live preview matches
// the print PDF. Coordinates: points, origin top-left of the MediaBox.

export const MAX_ZOOM = 8;
const EPS = 1e-6;

export class FontMetrics {
  constructor(info) {
    this.name = info.name;
    this.ascent = info.ascent;
    this.descent = info.descent;
    this.advances = new Map(Object.entries(info.advances).map(([cp, adv]) => [Number(cp), adv]));
    this.ranges = info.unicodeRanges;
  }

  advance(ch) {
    const adv = this.advances.get(ch.codePointAt(0));
    return adv === undefined ? 0.5 : adv;
  }

  unsupported(text) {
    const missing = new Set();
    for (const ch of text) {
      if (ch === "\n" || ch === "\r" || ch === "\t") continue;
      const cp = ch.codePointAt(0);
      if (!this.ranges.some(([lo, hi]) => cp >= lo && cp <= hi)) missing.add(ch);
    }
    return [...missing];
  }
}

export function prepareText(element, text) {
  text = (text || "").normalize("NFC").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (element.transform === "uppercase") text = text.toUpperCase();
  if (!element.multiline) text = text.split("\n").join(" ");
  return text.split("\n");
}

export function layoutText(element, text, font) {
  const box = element.box;
  const lines = prepareText(element, text);
  const lsEm = Number(element.letterSpacing || 0);
  const lineHeight = Number(element.lineHeight ?? 1.2);
  let size = Number(element.fontSize);
  const minSize = Number(element.minFontSize ?? size);
  const autoFit = element.autoFit !== false;

  const units = lines.map((line) => {
    let sum = 0;
    let n = 0;
    for (const ch of line) {
      sum += font.advance(ch);
      n += 1;
    }
    return sum + lsEm * Math.max(0, n - 1);
  });
  const maxUnits = units.length ? Math.max(...units) : 0;
  const blockEm = font.ascent - font.descent + (lines.length - 1) * lineHeight;

  if (autoFit) {
    if (maxUnits * size > box.w + EPS) size = box.w / maxUnits;
    if (blockEm * size > box.h + EPS) size = Math.min(size, box.h / blockEm);
    size = Math.max(size, minSize);
  }
  const overflow = maxUnits * size > box.w + 0.01 || blockEm * size > box.h + 0.01;

  const blockH = blockEm * size;
  let top;
  if (element.valign === "top") top = box.y;
  else if (element.valign === "bottom") top = box.y + box.h - blockH;
  else top = box.y + (box.h - blockH) / 2;
  const baseline = top + font.ascent * size;

  const align = element.align || "center";
  const outLines = lines.map((line, i) => {
    const width = units[i] * size;
    let x;
    if (align === "left") x = box.x;
    else if (align === "right") x = box.x + box.w - width;
    else x = box.x + (box.w - width) / 2;
    return { text: line, x, y: baseline + i * lineHeight * size, width };
  });
  return { fontSize: size, letterSpacing: lsEm * size, lines: outLines, overflow };
}

// x position of every character of a laid-out line (exact, no kerning).
export function glyphPositions(line, font, size, letterSpacing) {
  const xs = [];
  let x = line.x;
  for (const ch of line.text) {
    xs.push(x);
    x += font.advance(ch) * size + letterSpacing;
  }
  return xs;
}

export function placeImage(box, imgW, imgH, zoom = 1, dx = 0, dy = 0) {
  zoom = Math.min(Math.max(Number(zoom) || 1, 1), MAX_ZOOM);
  const s = Math.max(box.w / imgW, box.h / imgH) * zoom;
  const w = imgW * s;
  const h = imgH * s;
  const maxDx = (w - box.w) / 2;
  const maxDy = (h - box.h) / 2;
  dx = Math.min(Math.max(Number(dx) || 0, -maxDx), maxDx);
  dy = Math.min(Math.max(Number(dy) || 0, -maxDy), maxDy);
  return {
    x: box.x + (box.w - w) / 2 + dx,
    y: box.y + (box.h - h) / 2 + dy,
    w,
    h,
    scale: s,
    dpi: 72 / s,
    zoom,
    dx,
    dy,
  };
}

export function pathData(segments) {
  return segments
    .map((seg) => {
      const [op, ...v] = seg;
      if (op === "m") return `M${v[0]} ${v[1]}`;
      if (op === "l") return `L${v[0]} ${v[1]}`;
      if (op === "c") return `C${v[0]} ${v[1]} ${v[2]} ${v[3]} ${v[4]} ${v[5]}`;
      return "Z";
    })
    .join(" ");
}
