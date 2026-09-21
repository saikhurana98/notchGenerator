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


# ARC, CIRCLE and LWPOLYLINE store their geometry in an object coordinate system derived
# from the entity's extrusion vector, not in world coordinates. Fusion writes an extrusion
# of (0,0,-1) whenever the flat pattern comes off the far face of the sheet, and in that
# OCS the x axis is mirrored — so reading dxf.center straight off the entity puts a hole on
# the wrong side of the part. LINE and SPLINE have no OCS and are always world coordinates,
# which is why an unfixed profile stays put while its holes jump.
_PLUS_Z, _MINUS_Z, _ARBITRARY = "+z", "-z", "arbitrary"


def _extrusion_kind(entity) -> str:
    if not entity.dxf.hasattr("extrusion"):
        return _PLUS_Z
    ex, ey, ez = (float(c) for c in entity.dxf.extrusion)
    if abs(ex) < 1e-9 and abs(ey) < 1e-9:
        return _PLUS_Z if ez > 0 else _MINUS_Z
    return _ARBITRARY


def _mirror_x(p: Point) -> Point:
    """OCS to WCS for an extrusion of (0,0,-1): the x axis points the other way."""
    return np.array([-float(p[0]), float(p[1])])


def _flattened_wcs(entity, eid: int, chord_tol: float = 1e-4) -> "PolylineCurve":
    """World-coordinate fallback for geometry that does not lie in the XY plane."""
    import ezdxf.path

    path = ezdxf.path.make_path(entity)
    pts = np.array([[v.x, v.y] for v in path.flattening(chord_tol)])
    return PolylineCurve(eid, None, pts)


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
        extrusion = _extrusion_kind(entity)
        if extrusion is _ARBITRARY:
            return [_flattened_wcs(entity, eid)]
        a0 = math.radians(entity.dxf.start_angle)
        a1 = math.radians(entity.dxf.end_angle)
        sweep = (a1 - a0) % (2 * math.pi)
        if sweep == 0:
            sweep = 2 * math.pi
        center = _xy(entity.dxf.center)
        if extrusion == _MINUS_Z:
            # Mirroring x maps an OCS angle t to pi - t, which also reverses the sweep.
            center = _mirror_x(center)
            a0, sweep = math.pi - a0, -sweep
        return [ArcCurve(eid, entity, center, float(entity.dxf.radius), a0, sweep)]
    if kind == "ELLIPSE":
        # An ELLIPSE keeps its centre and axes in world coordinates, so no OCS to undo.
        pts = np.array([[p.x, p.y] for p in entity.flattening(1e-4)])
        return [PolylineCurve(eid, entity, pts)]
    if kind in ("LWPOLYLINE", "POLYLINE"):
        if _extrusion_kind(entity) is _ARBITRARY:
            return [_flattened_wcs(entity, eid)]
        out: list[Curve] = []
        # Exploded arcs inherit the polyline's extrusion and stay in OCS, so the recursion
        # is what corrects them; exploded lines come back already in world coordinates.
        for sub in entity.virtual_entities():
            out.extend(curves_from_entity(sub, eid))
        for c in out:
            c.src = None  # exploded pieces are no longer the whole entity
        return out
    if kind == "CIRCLE":
        extrusion = _extrusion_kind(entity)
        if extrusion is _ARBITRARY:
            return [_flattened_wcs(entity, eid)]
        c = _xy(entity.dxf.center)
        if extrusion == _MINUS_Z:
            c = _mirror_x(c)
        r = float(entity.dxf.radius)
        # One full-sweep arc that starts and ends at the same point: a closed loop all by
        # itself, and still the whole entity, so an untouched hole is written back as the
        # CIRCLE it came in as. Onshape keeps its holes on the cut layer with the outline,
        # so circles do go through the stitcher.
        return [ArcCurve(eid, entity, c, r, 0.0, 2.0 * math.pi)]
    if kind == "INSERT":
        # Block references carry their own placement transform; virtual_entities applies it.
        out = []
        for sub in entity.virtual_entities():
            out.extend(curves_from_entity(sub, eid))
        for c in out:
            c.src = None
        return out
    raise UnsupportedEntity(kind)
