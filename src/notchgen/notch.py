"""Build the relief notch at each end of each bend line, and cut it into the profile.

Per bend end: place the apex, take the inboard end of each paired extent line, shoot a
ray outward from each to find the material edge, then delete the stretch of profile the
notch spans and splice the notch in.

The ray step is the fragile one. In a Fusion export an extent line's coordinate matches a
profile vertex to ~1e-13, so a curve-vs-line root find returns zero or two roots there
depending on rounding. Vertices are therefore tested first and curve crossings only
consulted when no vertex lines up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .bends import BendPair
from .config import Config
from .curves import Curve, LineCurve
from .geom import (
    Point,
    cross2,
    dot2,
    norm,
    point_in_polygon,
    segment_intersection,
    signed_area,
    unit,
)
from .loop import Anchor, Loop, complement_intervals, intervals_overlap, normalise_anchor
from .report import Report


@dataclass
class Notch:
    bend_index: int
    end_index: int
    P: Point
    """The bend line endpoint this notch sits at."""

    u: Point
    """Outward unit vector, from the bend midpoint towards P."""

    bend_mid: Point
    """Middle of the bend line — deep inside the part, so it survives every notch."""

    apex: Point
    shoulders: list[Point]
    """The notch outline between its two profile anchors, excluding the anchors."""

    anchor_left: Anchor
    anchor_right: Anchor
    removal: tuple[float, float] = (0.0, 0.0)
    """Forward loop interval to delete, as (g_start, g_end)."""

    cut_area: float = 0.0

    @property
    def chain(self) -> list[Point]:
        return [self.anchor_left.point, *self.shoulders, self.anchor_right.point]

    def as_dict(self) -> dict:
        return {
            "bend": self.bend_index,
            "end": self.end_index,
            "apex": [float(self.apex[0]), float(self.apex[1])],
            "chain": [[float(p[0]), float(p[1])] for p in self.chain],
            "cut_area": float(self.cut_area),
        }


def _forward_param(pt: Point, origin: Point, u: Point) -> float:
    return dot2(pt - origin, u)


def find_anchor(
    loop: Loop, E: Point, u: Point, cfg: Config, report: Report, label: str
) -> Anchor | None:
    """Where the ray from E along u meets the profile.

    Vertices win over curve interiors, and a curve crossing that lands within snap_tol of
    a vertex is pulled onto it — splitting off a 0.005-long sliver would be worse geometry
    than moving the anchor by 0.005.
    """
    best: tuple[float, Anchor] | None = None
    for li, side, v in loop.vertices():
        perp_dist = abs(cross2(u, v - E))
        along = _forward_param(v, E, u)
        if perp_dist <= cfg.snap_tol and -cfg.snap_tol <= along <= cfg.max_stub:
            if best is None or along < best[0]:
                best = (along, Anchor(li, float(side), v.copy(), snapped_vertex=True))
    if best is not None:
        return normalise_anchor(loop, best[1])

    hits: list[tuple[float, int, float, Point]] = []
    for li, lk in enumerate(loop.links):
        for t, pt in lk.curve.line_crossings(E, u):
            along = _forward_param(pt, E, u)
            if -cfg.snap_tol <= along <= cfg.max_stub:
                s = lk.to_source_param(t)
                hits.append((along, li, s, pt))
    if not hits:
        near = min(
            (lk.curve.min_line_distance(E, u) for lk in loop.links),
            default=float("inf"),
        )
        report.warn(
            "no-edge-hit",
            f"{label}: no profile edge found within {cfg.max_stub} of the extent line end. "
            f"Closest approach was {near:.4f}. The bend end probably does not reach a free "
            f"edge; this notch is skipped.",
            closest_approach=near,
        )
        return None

    hits.sort()
    along, li, s, pt = hits[0]
    anchor = Anchor(li, s, pt)
    lk = loop.links[li]
    for side, v in ((0.0, lk.start()), (1.0, lk.end())):
        if norm(v - pt) <= max(cfg.snap_tol, cfg.sliver_tol):
            anchor = Anchor(li, side, v.copy(), snapped_vertex=True)
            break
    return normalise_anchor(loop, anchor)


def _extent_end(pair: BendPair, side, P: Point, cfg: Config, report: Report, label: str):
    """The end of `side`'s extent line that belongs to bend endpoint P."""
    d = pair.d
    want = dot2(P - pair.mid, d)
    a_along = dot2(side.extent.a - pair.mid, d)
    b_along = dot2(side.extent.b - pair.mid, d)
    E = side.extent.a if (a_along * want > b_along * want) else side.extent.b
    skew = abs(dot2(E - P, d))
    if skew > cfg.max_endpoint_skew:
        report.warn(
            "extent-end-skew",
            f"{label}: the extent line ends {skew:.4f} away from the bend end along the "
            f"bend; it may not belong to this bend end.",
            skew=skew,
        )
    return E


def build_notch(
    loop: Loop,
    pair: BendPair,
    end_index: int,
    cfg: Config,
    report: Report,
) -> Notch | None:
    P = pair.bend.a if end_index == 0 else pair.bend.b
    u = unit(P - pair.mid)
    d = pair.d
    label = f"bend {pair.index} end {end_index}"

    E_left = _extent_end(pair, pair.left, P, cfg, report, label)
    E_right = _extent_end(pair, pair.right, P, cfg, report, label)

    a_left = find_anchor(loop, E_left, u, cfg, report, label + " (left)")
    a_right = find_anchor(loop, E_right, u, cfg, report, label + " (right)")
    if a_left is None or a_right is None:
        return None

    if cfg.depth_from == "edge":
        ref = max(_forward_param(a_left.point, P, u), _forward_param(a_right.point, P, u))
    else:
        ref = 0.0
    apex = P + u * (ref - cfg.depth)

    for name, anchor, E in (("left", a_left, E_left), ("right", a_right, E_right)):
        if _forward_param(anchor.point, apex, u) <= 0:
            report.error(
                "notch-inverted",
                f"{label}: the {name} shoulder is not outboard of the apex — depth "
                f"{cfg.depth} is too large for this flange and the cut would self-cross.",
                bend=pair.index,
            )
            return None

    shoulders: list[Point] = []
    stub_left = norm(E_left - a_left.point) > cfg.sliver_tol
    stub_right = norm(E_right - a_right.point) > cfg.sliver_tol
    if stub_left:
        shoulders.append(E_left)
    if cfg.shape == "rect":
        base_left = E_left if stub_left else a_left.point
        base_right = E_right if stub_right else a_right.point
        shoulders.append(base_left + d * dot2(apex - base_left, d))
        shoulders.append(base_right + d * dot2(apex - base_right, d))
    else:
        shoulders.append(apex)
    if stub_right:
        shoulders.append(E_right)

    notch = Notch(
        bend_index=pair.index,
        end_index=end_index,
        P=P,
        u=u,
        bend_mid=pair.mid.copy(),
        apex=apex,
        shoulders=shoulders,
        anchor_left=a_left,
        anchor_right=a_right,
    )
    if not _choose_removal(loop, notch, pair, cfg, report, label):
        return None
    return notch


def _choose_removal(
    loop: Loop, notch: Notch, pair: BendPair, cfg: Config, report: Report, label: str
) -> bool:
    """Decide which of the two arcs between the anchors lies under the notch."""
    gl, gr = notch.anchor_left.g, notch.anchor_right.g
    band = max(abs(pair.left.offset), abs(pair.right.offset)) + cfg.snap_tol
    scores = []
    for g0, g1 in ((gl, gr), (gr, gl)):
        pts = loop.sample_slice(g0, g1, 80)
        inner = pts[1:-1] if len(pts) > 2 else pts
        in_band = np.abs((inner - pair.mid) @ pair.n) <= band
        outboard = ((inner - notch.apex) @ notch.u) > -cfg.snap_tol
        scores.append(float(np.mean(in_band & outboard)) if len(inner) else 0.0)

    if max(scores) < 0.5:
        report.error(
            "no-arc-under-notch",
            f"{label}: neither stretch of profile between the two anchors lies under the "
            f"notch (scores {scores[0]:.2f} / {scores[1]:.2f}).",
            bend=pair.index,
        )
        return False
    if min(scores) >= 0.5:
        report.error(
            "ambiguous-arc",
            f"{label}: both stretches of profile between the anchors look like they lie "
            f"under the notch (scores {scores[0]:.2f} / {scores[1]:.2f}).",
            bend=pair.index,
        )
        return False

    notch.removal = (gl, gr) if scores[0] > scores[1] else (gr, gl)
    # The cut is bounded by the removed arc on one side and the notch chain on the other,
    # so the chain has to be walked *back* from where the arc ends.
    arc = loop.flatten_slice(*notch.removal, cfg.chord_tol)
    notch.cut_area = abs(signed_area(np.vstack([arc, _oriented_chain(notch, arc[-1])])))
    return True


def _oriented_chain(notch: Notch, start_near: Point) -> np.ndarray:
    """The notch chain, ordered to begin at whichever end is nearest `start_near`."""
    chain = np.array(notch.chain)
    return chain if norm(chain[0] - start_near) <= norm(chain[-1] - start_near) else chain[::-1]


def check_notch_legs(loop: Loop, notch: Notch, cfg: Config, report: Report) -> None:
    """A notch leg may only touch the profile at its own two anchors."""
    kept = loop.flatten_slice(notch.removal[1], notch.removal[0], cfg.chord_tol)
    chain = np.array(notch.chain)
    ends = (chain[0], chain[-1])
    for i in range(len(chain) - 1):
        for j in range(len(kept) - 1):
            hit = segment_intersection(chain[i], chain[i + 1], kept[j], kept[j + 1])
            if hit is None:
                continue
            pt = hit[0]
            if min(norm(pt - e) for e in ends) <= cfg.sliver_tol:
                continue
            report.error(
                "notch-crosses-profile",
                f"Bend {notch.bend_index} end {notch.end_index}: a notch edge crosses the "
                f"profile at ({pt[0]:.4f}, {pt[1]:.4f}), away from its own anchors.",
                bend=notch.bend_index,
                at=pt,
            )
            return


@dataclass
class Surgery:
    kept: list[Curve] = field(default_factory=list)
    added: list[Curve] = field(default_factory=list)
    notches: list[Notch] = field(default_factory=list)
    area_before: float = 0.0
    area_after: float = 0.0

    @property
    def all_curves(self) -> list[Curve]:
        return [*self.kept, *self.added]


def apply_notches(
    loop: Loop, notches: list[Notch], cfg: Config, report: Report
) -> Surgery | None:
    """Cut every notch out of the original loop in one pass.

    All removals are resolved against the untouched loop and applied together, so the
    result does not depend on notch order and merely-adjacent cuts work for free.
    """
    surgery = Surgery(notches=notches)
    if not notches:
        return surgery

    n = len(loop.links)
    removals = [nt.removal for nt in notches]
    clashes = intervals_overlap(n, removals)
    if clashes and not cfg.merge_overlapping:
        for i, j in clashes:
            report.error(
                "overlapping-cuts",
                f"The cuts for bend {notches[i].bend_index} end {notches[i].end_index} and "
                f"bend {notches[j].bend_index} end {notches[j].end_index} overlap on the "
                f"profile. Reduce the depth, or allow merging.",
            )
        return None

    area_before = loop.area(cfg.chord_tol)
    total_cut = sum(nt.cut_area for nt in notches)
    if total_cut > cfg.max_cut_frac * area_before:
        report.error(
            "cut-too-large",
            f"The notches would remove {total_cut:.2f} of {area_before:.2f} square units "
            f"({100 * total_cut / area_before:.1f}%), over the {100 * cfg.max_cut_frac:.0f}% "
            f"limit. That usually means the wrong stretch of profile was selected.",
            removed=total_cut,
            total=area_before,
        )
        return None

    for nt in notches:
        check_notch_legs(loop, nt, cfg, report)
    if report.has_errors:
        return None

    for g0, g1 in complement_intervals(n, removals):
        surgery.kept.extend(loop.curves_for_slice(g0, g1))

    for nt in notches:
        chain = nt.chain
        for i in range(len(chain) - 1):
            if norm(chain[i + 1] - chain[i]) > 1e-9:
                surgery.added.append(LineCurve(-1, None, chain[i].copy(), chain[i + 1].copy()))

    ring = _result_ring(loop, notches, cfg)
    surgery.area_before = area_before
    surgery.area_after = abs(signed_area(ring))

    # Each notch's own cut area is measured independently of the rebuilt outline, so the
    # two must agree. They diverge if an arc was mis-oriented or a cut counted twice.
    drift = abs((area_before - surgery.area_after) - total_cut)
    if drift > max(1e-4, 1e-6 * area_before):
        report.error(
            "area-mismatch",
            f"The rebuilt outline lost {area_before - surgery.area_after:.4f} square units but "
            f"the notches account for {total_cut:.4f} (off by {drift:.4g}). The profile surgery "
            f"is inconsistent.",
            drift=drift,
        )
        return None

    if surgery.area_after >= area_before:
        report.error(
            "no-material-removed",
            f"The result is not smaller than the input ({surgery.area_after:.3f} vs "
            f"{area_before:.3f}); the notches did not cut into the part.",
        )
        return None
    # The bend endpoint itself is legitimately inside the cut, so the containment check
    # uses the bend midpoint, which is deep in the part and must survive every notch.
    for nt in notches:
        if not point_in_polygon(nt.bend_mid, ring):
            report.error(
                "part-interior-lost",
                f"The middle of bend {nt.bend_index} fell outside the result outline, so the "
                f"wrong stretch of profile was removed.",
                bend=nt.bend_index,
            )
            return None
    return surgery


def _result_ring(loop: Loop, notches: list[Notch], cfg: Config) -> np.ndarray:
    """The result outline as one closed polyline, for area and containment checks."""
    n = len(loop.links)
    ordered = sorted(notches, key=lambda nt: nt.removal[0] % n)
    parts: list[np.ndarray] = []
    for i, nt in enumerate(ordered):
        # Walking in loop order: the kept profile arrives at removal[0], the notch chain
        # carries the outline across to removal[1], then the next kept stretch resumes.
        parts.append(_oriented_chain(nt, loop.point_at(nt.removal[0])))
        parts.append(loop.flatten_slice(nt.removal[1], ordered[(i + 1) % len(ordered)].removal[0], cfg.chord_tol))
    return np.vstack(parts)


def build_all(
    loop: Loop, pairs: list[BendPair], cfg: Config, report: Report
) -> tuple[list[Notch], Surgery | None]:
    notches: list[Notch] = []
    for pair in pairs:
        for end_index in (0, 1):
            nt = build_notch(loop, pair, end_index, cfg, report)
            if nt is not None:
                if _already_notched(loop, nt, cfg):
                    report.info(
                        "already-notched",
                        f"Bend {pair.index} end {end_index} already has a vertex at the notch "
                        f"apex; skipping so a re-run does not cut twice.",
                        bend=pair.index,
                    )
                    continue
                notches.append(nt)
    if report.has_errors:
        return notches, None
    return notches, apply_notches(loop, notches, cfg, report)


def _already_notched(loop: Loop, nt: Notch, cfg: Config) -> bool:
    return any(norm(v - nt.apex) <= cfg.sliver_tol for _, _, v in loop.vertices())
