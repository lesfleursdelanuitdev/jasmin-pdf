"""Small 2D helpers shared by the analyzer and the renderer.

Matrices use the PDF convention: a 6-tuple ``(a, b, c, d, e, f)`` that maps
``(x, y)`` to ``(a*x + c*y + e, b*x + d*y + f)``.  ``mul(m1, m2)`` means
"apply m1, then m2" (the PDF ``CTM' = M x CTM`` rule for ``M cm``).

Paths are lists of segments in absolute coordinates:
``("m", x, y)``, ``("l", x, y)``, ``("c", x1, y1, x2, y2, x3, y3)``, ``("h",)``.
"""
from __future__ import annotations

from typing import Iterable, Sequence

Matrix = tuple[float, float, float, float, float, float]
Rect = tuple[float, float, float, float]  # x0, y0, x1, y1
Segment = tuple

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mul(m1: Sequence[float], m2: Sequence[float]) -> Matrix:
    a1, b1, c1, d1, e1, f1 = (float(v) for v in m1)
    a2, b2, c2, d2, e2, f2 = (float(v) for v in m2)
    return (
        a1 * a2 + b1 * c2,
        a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2,
        c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2,
        e1 * b2 + f1 * d2 + f2,
    )


def invert(m: Sequence[float]) -> Matrix | None:
    a, b, c, d, e, f = (float(v) for v in m)
    det = a * d - b * c
    if abs(det) < 1e-12:
        return None
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))


def apply(m: Sequence[float], x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


def rect_points(r: Rect) -> list[tuple[float, float]]:
    x0, y0, x1, y1 = r
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def bbox_of_points(points: Iterable[tuple[float, float]]) -> Rect | None:
    xs, ys = [], []
    for x, y in points:
        xs.append(x)
        ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def transform_rect(m: Sequence[float], r: Rect) -> Rect:
    return bbox_of_points(apply(m, x, y) for x, y in rect_points(r))  # type: ignore[return-value]


def union(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def intersect(a: Rect | None, b: Rect | None) -> Rect | None:
    if a is None:
        return b
    if b is None:
        return a
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    if r[2] <= r[0] or r[3] <= r[1]:
        return None
    return r


def area(r: Rect | None) -> float:
    if r is None:
        return 0.0
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def path_points(path: Sequence[Segment]) -> list[tuple[float, float]]:
    pts = []
    for seg in path:
        coords = seg[1:]
        pts.extend(zip(coords[0::2], coords[1::2]))
    return pts


def path_bbox(path: Sequence[Segment]) -> Rect | None:
    # Control points bound the curve, so this is a (tight enough) superset.
    return bbox_of_points(path_points(path))


def transform_path(m: Sequence[float], path: Sequence[Segment]) -> list[Segment]:
    out = []
    for seg in path:
        op, coords = seg[0], seg[1:]
        pts = []
        for x, y in zip(coords[0::2], coords[1::2]):
            pts.extend(apply(m, x, y))
        out.append((op, *pts))
    return out


def is_axis_aligned_rect(path: Sequence[Segment], tol: float = 0.01) -> bool:
    """True if the path is a single closed axis-aligned rectangle."""
    if any(seg[0] == "c" for seg in path):
        return False
    pts = path_points(path)
    if len(pts) > 1 and abs(pts[0][0] - pts[-1][0]) < tol and abs(pts[0][1] - pts[-1][1]) < tol:
        pts = pts[:-1]
    if len(pts) != 4 or sum(1 for s in path if s[0] == "m") != 1:
        return False
    xs = sorted({round(x / tol) for x, _ in pts})
    ys = sorted({round(y / tol) for _, y in pts})
    return len(xs) == 2 and len(ys) == 2


def flip_to_top_left(path: Sequence[Segment], x0: float, top: float) -> list[Segment]:
    """PDF user space (y up) -> template space (origin top-left, y down)."""
    return transform_path((1, 0, 0, -1, -x0, top), path)


def round_path(path: Sequence[Segment], nd: int = 3) -> list[list]:
    return [[seg[0], *(round(v, nd) for v in seg[1:])] for seg in path]


def normalize_path(path: Sequence[Segment]) -> list[Segment]:
    """Rewrite the ``v`` / ``y`` curve shorthands as full ``c`` segments."""
    out: list[Segment] = []
    cur = (0.0, 0.0)
    start = (0.0, 0.0)
    for seg in path:
        op = seg[0]
        if op == "m":
            cur = start = (seg[1], seg[2])
            out.append(("m", *cur))
        elif op == "l":
            cur = (seg[1], seg[2])
            out.append(("l", *cur))
        elif op == "c":
            out.append(tuple(seg))
            cur = (seg[5], seg[6])
        elif op == "v":  # first control point = current point
            out.append(("c", cur[0], cur[1], seg[1], seg[2], seg[3], seg[4]))
            cur = (seg[3], seg[4])
        elif op == "y":  # second control point = end point
            out.append(("c", seg[1], seg[2], seg[3], seg[4], seg[3], seg[4]))
            cur = (seg[3], seg[4])
        elif op == "h":
            out.append(("h",))
            cur = start
    return out
