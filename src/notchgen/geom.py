"""Small 2D vector helpers. No ezdxf, no I/O — pure numpy."""

from __future__ import annotations

import numpy as np

Point = np.ndarray  # shape (2,), float


def vec(x: float, y: float) -> Point:
    return np.array([float(x), float(y)])


def norm(v) -> float:
    return float(np.hypot(v[0], v[1]))


def unit(v) -> Point:
    n = norm(v)
    if n == 0.0:
        raise ValueError("cannot normalise a zero-length vector")
    return np.asarray(v, dtype=float) / n


def perp(v) -> Point:
    """Rotate 90° counter-clockwise."""
    return np.array([-float(v[1]), float(v[0])])


def cross2(a, b) -> float:
    return float(a[0] * b[1] - a[1] * b[0])


def dot2(a, b) -> float:
    return float(a[0] * b[0] + a[1] * b[1])


def signed_dist_to_line(pts, origin, direction) -> np.ndarray:
    """Signed perpendicular distance of pts from the infinite line origin+t*direction.

    `direction` must be a unit vector. Positive is to the left of the direction.
    """
    d = np.asarray(pts, dtype=float) - np.asarray(origin, dtype=float)
    return d[..., 1] * direction[0] - d[..., 0] * direction[1]


def project_along(pts, origin, direction) -> np.ndarray:
    """Parameter of pts projected onto the line origin+t*direction (unit direction)."""
    d = np.asarray(pts, dtype=float) - np.asarray(origin, dtype=float)
    return d[..., 0] * direction[0] + d[..., 1] * direction[1]


def polyline_length(pts) -> float:
    pts = np.asarray(pts, dtype=float)
    if len(pts) < 2:
        return 0.0
    return float(np.sum(np.hypot(*np.diff(pts, axis=0).T)))


def signed_area(pts) -> float:
    """Signed area of the polygon through pts. Positive when counter-clockwise.

    The ring is treated as closed; a repeated final vertex is harmless.
    """
    p = np.asarray(pts, dtype=float)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def point_in_polygon(pt, pts) -> bool:
    """Even-odd ray-crossing test. Points exactly on the boundary are undefined."""
    p = np.asarray(pts, dtype=float)
    x, y = float(pt[0]), float(pt[1])
    x0, y0 = p[:, 0], p[:, 1]
    x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
    straddles = (y0 > y) != (y1 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xcross = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
    return bool(np.sum(straddles & (xcross > x)) % 2 == 1)


def segment_intersection(p0, p1, q0, q1, eps: float = 1e-12):
    """Intersection of segments p0-p1 and q0-q1, or None. Returns (point, t_p, t_q)."""
    r = np.asarray(p1, float) - np.asarray(p0, float)
    s = np.asarray(q1, float) - np.asarray(q0, float)
    den = cross2(r, s)
    if abs(den) < eps:
        return None
    w = np.asarray(q0, float) - np.asarray(p0, float)
    t = cross2(w, s) / den
    u = cross2(w, r) / den
    if t < -eps or t > 1 + eps or u < -eps or u > 1 + eps:
        return None
    return np.asarray(p0, float) + t * r, t, u


def polygons_overlap_area(a, b) -> float:
    """Area of the intersection of two convex-or-simple polygons, by Sutherland-Hodgman.

    `b` must be convex — we only ever clip against the notch cutter, which is convex by
    construction after `notch.build_cutter` orders its vertices.
    """
    poly = [np.asarray(p, float) for p in a]
    clip = [np.asarray(p, float) for p in b]
    if signed_area(clip) < 0:
        clip = clip[::-1]
    for i in range(len(clip)):
        c0, c1 = clip[i], clip[(i + 1) % len(clip)]
        edge = c1 - c0
        out: list[np.ndarray] = []
        if not poly:
            return 0.0
        for j in range(len(poly)):
            cur, nxt = poly[j], poly[(j + 1) % len(poly)]
            cur_in = cross2(edge, cur - c0) >= 0
            nxt_in = cross2(edge, nxt - c0) >= 0
            if cur_in:
                out.append(cur)
            if cur_in != nxt_in:
                den = cross2(edge, nxt - cur)
                if abs(den) > 1e-18:
                    t = cross2(edge, c0 - cur) / den
                    out.append(cur + t * (nxt - cur))
        poly = out
    if len(poly) < 3:
        return 0.0
    return abs(signed_area(poly))


def bbox(pts):
    p = np.asarray(pts, dtype=float)
    return p.min(axis=0), p.max(axis=0)


def bbox_diagonal(pts) -> float:
    lo, hi = bbox(pts)
    return float(np.hypot(*(hi - lo)))
