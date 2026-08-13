"""Uniform curve abstraction over the DXF entity types that can carry a profile.

Every curve is parametrised on a normalised ``t`` in [0, 1] running from its start point
to its end point in the *source entity's own direction*. Entity direction is never
rewritten — the loop stores an orientation flag instead — so an untouched entity is
re-emitted by copying the original, bit for bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .geom import Point, norm, polyline_length, signed_dist_to_line, vec


class UnsupportedEntity(Exception):
    pass


def _xy(p) -> Point:
    return np.array([float(p.x), float(p.y)])


@dataclass
class Curve:
    """One profile curve. Subclasses implement the geometry."""

    eid: int
    """Index of the source entity in the modelspace listing. Shared by exploded pieces."""

    src: object | None = None
    """The source ezdxf entity when this curve *is* the whole entity, else None."""

    def point(self, t: float) -> Point:
        raise NotImplementedError

    def split(self, t: float) -> tuple["Curve", "Curve"]:
        raise NotImplementedError

    def flatten(self, chord_tol: float) -> np.ndarray:
        raise NotImplementedError

    @property
    def is_whole(self) -> bool:
        return self.src is not None

    def start(self) -> Point:
        return self.point(0.0)

    def end(self) -> Point:
        return self.point(1.0)

    def length(self, chord_tol: float = 1e-3) -> float:
        return polyline_length(self.flatten(chord_tol))

    def sample(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        ts = np.linspace(0.0, 1.0, n)
        return ts, np.array([self.point(t) for t in ts])

    # -- ray intersection ------------------------------------------------------

    def line_crossings(
        self, origin, direction, samples: int = 200, refine_tol: float = 1e-12
    ) -> list[tuple[float, Point]]:
        """Parameters where this curve crosses the *infinite* line origin+s*direction.

        Found by sign changes of the signed perpendicular distance on a sample grid,
        then bisected. Tangential touches produce no sign change and are deliberately
        not reported here — callers rely on vertex snapping for those (see notch.py).
        """
        ts, pts = self.sample(samples)
        f = signed_dist_to_line(pts, origin, direction)
        out: list[tuple[float, Point]] = []
        for i in np.where(np.diff(np.sign(f)) != 0)[0]:
            lo, hi = float(ts[i]), float(ts[i + 1])
            flo = f[i]
            for _ in range(100):
                mid = 0.5 * (lo + hi)
                fmid = signed_dist_to_line(self.point(mid), origin, direction)
                if abs(fmid) < refine_tol or hi - lo < 1e-15:
                    lo = hi = mid
                    break
                if (flo < 0) != (fmid < 0):
                    hi = mid
                else:
                    lo, flo = mid, fmid
            t = 0.5 * (lo + hi)
            out.append((t, self.point(t)))
        return out

    def min_line_distance(self, origin, direction, samples: int = 400) -> float:
        _, pts = self.sample(samples)
        return float(np.min(np.abs(signed_dist_to_line(pts, origin, direction))))


@dataclass
class LineCurve(Curve):
    a: Point = field(default_factory=lambda: vec(0, 0))
    b: Point = field(default_factory=lambda: vec(0, 0))

    def point(self, t: float) -> Point:
        return self.a + float(t) * (self.b - self.a)

    def split(self, t: float) -> tuple[Curve, Curve]:
        m = self.point(t)
        return (
            LineCurve(self.eid, None, self.a.copy(), m),
            LineCurve(self.eid, None, m, self.b.copy()),
        )

    def flatten(self, chord_tol: float) -> np.ndarray:
        return np.array([self.a, self.b])

    def length(self, chord_tol: float = 1e-3) -> float:
        return norm(self.b - self.a)

    def sample(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        ts = np.linspace(0.0, 1.0, n)
        return ts, self.a + ts[:, None] * (self.b - self.a)


@dataclass
class ArcCurve(Curve):
    center: Point = field(default_factory=lambda: vec(0, 0))
    radius: float = 1.0
    start_angle: float = 0.0  # radians
    sweep: float = 0.0  # radians, signed

    def point(self, t: float) -> Point:
        a = self.start_angle + float(t) * self.sweep
        return self.center + self.radius * np.array([math.cos(a), math.sin(a)])

    def split(self, t: float) -> tuple[Curve, Curve]:
        mid = self.start_angle + t * self.sweep
        return (
            ArcCurve(self.eid, None, self.center.copy(), self.radius, self.start_angle, t * self.sweep),
            ArcCurve(self.eid, None, self.center.copy(), self.radius, mid, (1 - t) * self.sweep),
        )

    def flatten(self, chord_tol: float) -> np.ndarray:
        if self.radius <= 0:
            return np.array([self.point(0.0), self.point(1.0)])
        ratio = max(-1.0, min(1.0, 1.0 - chord_tol / self.radius))
        step = 2.0 * math.acos(ratio) if ratio < 1.0 else abs(self.sweep)
        n = max(2, int(math.ceil(abs(self.sweep) / max(step, 1e-6))) + 1)
        _, pts = self.sample(n)
        return pts

    def length(self, chord_tol: float = 1e-3) -> float:
        return abs(self.sweep) * self.radius


@dataclass
class SplineCurve(Curve):
    bs: object = None  # ezdxf.math.BSpline

    def point(self, t: float) -> Point:
        return _xy(self.bs.point(float(t) * self.bs.max_t))

    def split(self, t: float) -> tuple[Curve, Curve]:
        left, right = self.bs.split(float(t) * self.bs.max_t)
        return SplineCurve(self.eid, None, left), SplineCurve(self.eid, None, right)

    def flatten(self, chord_tol: float) -> np.ndarray:
        return np.array([[p.x, p.y] for p in self.bs.flattening(chord_tol, segments=4)])

    def sample(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        ts = np.linspace(0.0, 1.0, n)
        mt = self.bs.max_t
        return ts, np.array([_xy(self.bs.point(t * mt)) for t in ts])

    def chord_deviation_bound(self) -> float:
        """Upper bound on how far this spline strays from its own chord.

        Uses the control-point convex hull, which bounds the curve. Cheap way to spot
        the near-straight flange-edge splines Fusion emits.
        """
        cps = np.array([[p.x, p.y] for p in self.bs.control_points])
        a, b = cps[0], cps[-1]
        d = b - a
        n = norm(d)
        if n == 0:
            return float(np.max(np.hypot(*(cps - a).T)))
        u = d / n
        return float(np.max(np.abs(signed_dist_to_line(cps, a, u))))


@dataclass
class PolylineCurve(Curve):
    """A straight-segment run, used for a spline we could not split exactly."""

    pts: np.ndarray = field(default_factory=lambda: np.zeros((2, 2)))

    def point(self, t: float) -> Point:
        p = self.pts
        seg = np.hypot(*np.diff(p, axis=0).T)
        total = seg.sum()
        if total == 0:
            return p[0].copy()
        cum = np.concatenate([[0.0], np.cumsum(seg)]) / total
        t = min(max(float(t), 0.0), 1.0)
        i = int(np.clip(np.searchsorted(cum, t) - 1, 0, len(p) - 2))
        span = cum[i + 1] - cum[i]
        f = 0.0 if span == 0 else (t - cum[i]) / span
        return p[i] + f * (p[i + 1] - p[i])

    def split(self, t: float) -> tuple[Curve, Curve]:
        m = self.point(t)
        p = self.pts
        seg = np.hypot(*np.diff(p, axis=0).T)
        cum = np.concatenate([[0.0], np.cumsum(seg)]) / max(seg.sum(), 1e-18)
        i = int(np.clip(np.searchsorted(cum, t) - 1, 0, len(p) - 2))
        return (
            PolylineCurve(self.eid, None, np.vstack([p[: i + 1], m])),
            PolylineCurve(self.eid, None, np.vstack([m, p[i + 1 :]])),
        )

    def flatten(self, chord_tol: float) -> np.ndarray:
        return self.pts.copy()

    def sample(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        ts = np.linspace(0.0, 1.0, n)
        return ts, np.array([self.point(t) for t in ts])


def curves_from_entity(entity, eid: int) -> list[Curve]:
    """Convert one DXF entity into one or more Curves.

    LWPOLYLINE and POLYLINE are exploded into lines and arcs, so they lose their
    single-entity identity on output. Everything else maps one-to-one.
    """
    kind = entity.dxftype()
    if kind == "LINE":
        return [LineCurve(eid, entity, _xy(entity.dxf.start), _xy(entity.dxf.end))]
    if kind == "SPLINE":
        return [SplineCurve(eid, entity, entity.construction_tool())]
    if kind == "ARC":
        a0 = math.radians(entity.dxf.start_angle)
        a1 = math.radians(entity.dxf.end_angle)
        sweep = (a1 - a0) % (2 * math.pi)
        if sweep == 0:
            sweep = 2 * math.pi
        return [ArcCurve(eid, entity, _xy(entity.dxf.center), float(entity.dxf.radius), a0, sweep)]
    if kind == "ELLIPSE":
        pts = np.array([[p.x, p.y] for p in entity.flattening(1e-4)])
        return [PolylineCurve(eid, entity, pts)]
    if kind in ("LWPOLYLINE", "POLYLINE"):
        out: list[Curve] = []
        for sub in entity.virtual_entities():
            out.extend(curves_from_entity(sub, eid))
        for c in out:
            c.src = None  # exploded pieces are no longer the whole entity
        return out
    if kind == "CIRCLE":
        c = _xy(entity.dxf.center)
        r = float(entity.dxf.radius)
        return [
            ArcCurve(eid, None, c, r, 0.0, math.pi),
            ArcCurve(eid, None, c, r, math.pi, math.pi),
        ]
    raise UnsupportedEntity(kind)
