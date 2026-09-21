"""Pair each bend line with the extent line on either side of it.

Nearest-parallel-on-each-side alone is not safe: two bends 4 mm apart, each with a
2.83 mm bend zone, would let one bend steal the other's inboard extent and silently
produce a too-narrow notch. So candidates must also overlap the bend along its own
direction, each extent is owned by whichever bend it is closest to, and the resulting
pair has to be symmetric — Fusion always puts the bend line mid-zone.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .geom import Point, cross2, dot2, norm, perp, unit
from .report import Report


@dataclass
class Segment:
    a: Point
    b: Point
    inferred: bool = False
    """A stand-in extent read off the outline, rather than a line from the file."""

    @property
    def mid(self) -> Point:
        return 0.5 * (self.a + self.b)

    @property
    def length(self) -> float:
        return norm(self.b - self.a)

    @property
    def direction(self) -> Point:
        return unit(self.b - self.a)


@dataclass
class Side:
    offset: float
    """Signed distance of the extent from the bend line, along the bend normal."""

    extent: Segment
    extent_index: int


@dataclass
class BendPair:
    index: int
    bend: Segment
    left: Side
    """The side at negative offset along the bend normal."""

    right: Side
    """The side at positive offset."""

    @property
    def d(self) -> Point:
        return self.bend.direction

    @property
    def n(self) -> Point:
        return perp(self.bend.direction)

    @property
    def mid(self) -> Point:
        return self.bend.mid

    @property
    def half_width(self) -> float:
        return 0.5 * (abs(self.left.offset) + abs(self.right.offset))


def _overlap_along(bend: Segment, extent: Segment) -> float:
    d = bend.direction
    L = bend.length
    e0 = dot2(extent.a - bend.a, d)
    e1 = dot2(extent.b - bend.a, d)
    lo, hi = min(e0, e1), max(e0, e1)
    return max(0.0, min(hi, L) - max(lo, 0.0))


def _zone_candidates(
    P: Point, u: Point, n: Point, verts: list[Point], cfg: Config
) -> tuple[list[tuple[float, float, float]], list[float]]:
    """Bend-zone evidence at bend end P: symmetric vertex pairs, and lone vertices.

    Where a bend meets a free edge the flat-pattern outline has a vertex at each edge of
    the bend zone — the two points a tangent line would have ended on. They sit at equal
    and opposite offsets from the bend line, which is what picks them out, and come back
    as (half_width, along_neg, along_pos).

    When the edge runs straight on past the bend zone on one side there is no vertex
    there, only on the other. Those lone offsets come back separately. On their own they
    prove nothing — any corner near a bend end looks the same — so they are only used to
    choose between zone widths that symmetric pairs elsewhere in the file have established.
    """
    neg: list[tuple[float, float]] = []
    pos: list[tuple[float, float]] = []
    # One side of the outline is sometimes set back from the bend end by more than the
    # skew a real extent line is allowed before it draws a warning.
    back = max(cfg.max_endpoint_skew, cfg.max_setback)
    for v in verts:
        along = dot2(v - P, u)
        if not -back <= along <= cfg.max_stub:
            continue
        offset = dot2(v - P, n)
        if cfg.snap_tol < abs(offset) <= 0.5 * cfg.max_zone:
            (neg if offset < 0 else pos).append((abs(offset), along))
    best: dict[int, tuple[float, float, float, float]] = {}
    for wn, an in neg:
        for wp, ap in pos:
            if abs(wn - wp) > cfg.snap_tol:
                continue
            w = 0.5 * (wn + wp)
            score = abs(an) + abs(ap)
            key = round(w / cfg.snap_tol)
            if key not in best or score < best[key][0]:
                best[key] = (score, w, an, ap)
    lone = sorted(w for w, along in (*neg, *pos) if abs(along) <= cfg.sliver_tol)
    return sorted((w, an, ap) for _, w, an, ap in best.values()), lone


def _match_width(cands: list[tuple[float, float, float]], w: float, tol: float):
    hits = [c for c in cands if abs(c[0] - w) <= tol]
    return min(hits, key=lambda c: abs(c[0] - w)) if hits else None


def infer_extents(
    bends: list[Segment], verts: list[Point], cfg: Config, report: Report
) -> list[Segment]:
    """Stand-in extent lines for a file that has none, read off the outline's vertices.

    Each bend gets the narrowest zone width that shows up as a symmetric vertex pair at
    both of its ends (or at the one end that reaches an edge). A bend that shows none
    takes a width the other bends established — the one a lone vertex at its own end
    points to if there is one, otherwise the typical one. `cfg.bend_zone` overrides all
    of that.
    """
    tol = cfg.snap_tol
    per_bend: list[tuple[list, list]] = []
    lone_at: list[list[float]] = []
    widths: list[float | None] = []
    for bend in bends:
        if bend.length == 0:
            per_bend.append(([], []))
            lone_at.append([])
            widths.append(None)
            continue
        d = bend.direction
        n = perp(d)
        (pairs_a, lone_a), (pairs_b, lone_b) = (
            _zone_candidates(bend.a, -d, n, verts, cfg),
            _zone_candidates(bend.b, d, n, verts, cfg),
        )
        ends = (pairs_a, pairs_b)
        per_bend.append(ends)
        lone_at.append([*lone_a, *lone_b])
        if cfg.bend_zone:
            widths.append(0.5 * cfg.bend_zone)
            continue
        common = [c[0] for c in pairs_a if _match_width(pairs_b, c[0], tol)]
        either = [c[0] for c in (*pairs_a, *pairs_b)]
        widths.append(min(common) if common else min(either) if either else None)

    known = sorted(w for w in widths if w is not None)
    if not known:
        report.error(
            "no-bend-zone",
            "There is no bend-extent (tangent line) layer, and the bend zone could not be "
            "read off the outline either: no bend end has a matching pair of outline "
            "vertices either side of it. Give the bend zone width explicitly, or re-export "
            "with tangent lines switched on.",
        )
        return []
    fallback = known[len(known) // 2]

    out: list[Segment] = []
    assumed = 0
    for bend, ends, lone, w in zip(bends, per_bend, lone_at, widths):
        if bend.length == 0:
            continue
        if w is None:
            backed = [k for k in known if any(abs(k - x) <= tol for x in lone)]
            if backed:
                w = min(backed)
            else:
                w = fallback
                assumed += 1
        d = bend.direction
        n = perp(d)
        sides: dict[int, list[Point]] = {-1: [], 1: []}
        for P, u, cands in ((bend.a, -d, ends[0]), (bend.b, d, ends[1])):
            hit = _match_width(cands, w, tol)
            for sign, along in ((-1, hit[1] if hit else 0.0), (1, hit[2] if hit else 0.0)):
                # Exactly w off the bend line, so the stand-in is exactly parallel to it;
                # the outline vertex is within snap_tol of this and the anchor snaps onto it.
                sides[sign].append(P + along * u + sign * w * n)
        out.append(Segment(*sides[-1], inferred=True))
        out.append(Segment(*sides[1], inferred=True))

    distinct = sorted({round(2 * w, 3) for w in known})
    shown = ", ".join(f"{w:g}" for w in distinct[:6]) + (", …" if len(distinct) > 6 else "")
    source = "given explicitly" if cfg.bend_zone else "read off the outline"
    report.info(
        "extents-inferred",
        f"No bend-extent layer, so the bend zone was {source}: width {shown}.",
        widths=distinct,
    )
    if assumed:
        report.warn(
            "bend-zone-assumed",
            f"{assumed} bend(s) showed no bend-zone vertices on the outline, so the typical "
            f"width of {2 * fallback:.4g} was assumed for them.",
            count=assumed,
            width=2 * fallback,
        )
    return out


def pair_bends(
    bends: list[Segment], extents: list[Segment], cfg: Config, report: Report
) -> list[BendPair]:
    """Assign two extents to every bend, or report why a bend had to be skipped."""
    if not bends:
        report.error("no-bend-lines", "The bend layer contains no lines.")
        return []

    # Step 1: which (bend, extent) combinations are even admissible.
    admissible: dict[int, list[tuple[float, int]]] = {i: [] for i in range(len(bends))}
    for ei, ext in enumerate(extents):
        if ext.length == 0:
            report.warn("degenerate-extent", f"Extent line {ei} has zero length; ignored.")
            continue
        claims: list[tuple[float, int, float]] = []
        for bi, bend in enumerate(bends):
            if bend.length == 0:
                continue
            if abs(cross2(bend.direction, ext.direction)) > cfg.angle_tol:
                continue
            overlap = _overlap_along(bend, ext)
            if overlap < cfg.overlap_frac * bend.length:
                continue
            offset = dot2(ext.mid - bend.a, perp(bend.direction))
            claims.append((abs(offset), bi, offset))
        if not claims:
            report.warn(
                "orphan-extent",
                f"Extent line {ei} is not parallel to, or does not overlap, any bend line.",
                extent=ei,
            )
            continue
        # Step 2: ownership goes to the nearest bend, so a neighbouring bend cannot steal it.
        claims.sort()
        _, bi, offset = claims[0]
        admissible[bi].append((offset, ei))

    pairs: list[BendPair] = []
    for bi, bend in enumerate(bends):
        owned = admissible[bi]
        negatives = sorted((o for o in owned if o[0] < 0), key=lambda o: -o[0])
        positives = sorted((o for o in owned if o[0] > 0), key=lambda o: o[0])
        zeros = [o for o in owned if o[0] == 0]
        for _, ei in zeros:
            report.warn(
                "extent-on-bend",
                f"Extent line {ei} is coincident with bend line {bi}; zero-width relief.",
                bend=bi,
                extent=ei,
            )
        if not negatives or not positives:
            # One odd bend must not cost the other thirty their relief cuts, so this skips
            # the bend and says so rather than failing the whole file.
            report.warn(
                "unpaired-bend",
                f"Bend line {bi} has {len(negatives)} extent(s) on one side and "
                f"{len(positives)} on the other; expected one each. Skipped — no notches were "
                f"cut at this bend.",
                bend=bi,
            )
            continue
        if len(negatives) > 1 or len(positives) > 1:
            report.warn(
                "extra-extents",
                f"Bend line {bi} owns {len(owned)} extent lines; using the nearest on each side.",
                bend=bi,
            )
        left = Side(negatives[0][0], extents[negatives[0][1]], negatives[0][1])
        right = Side(positives[0][0], extents[positives[0][1]], positives[0][1])

        # Step 3: the bend line sits mid-zone, so the two offsets must match.
        skew = abs(abs(left.offset) - abs(right.offset))
        limit = max(cfg.symmetry_tol_abs, cfg.symmetry_tol_rel * max(abs(left.offset), abs(right.offset)))
        if skew > limit:
            report.warn(
                "asymmetric-pairing",
                f"Bend line {bi} paired with extents {abs(left.offset):.4f} and "
                f"{abs(right.offset):.4f} from it. A bend line should be mid-zone, so this "
                f"pairing is probably picking up a neighbouring bend's extent. Skipped.",
                bend=bi,
                left_offset=abs(left.offset),
                right_offset=abs(right.offset),
                limit=limit,
            )
            continue
        if bend.length < 2 * cfg.depth:
            report.warn(
                "bend-too-short",
                f"Bend line {bi} is {bend.length:.3f} long, shorter than two notch depths "
                f"({2 * cfg.depth:.3f}); its two notches would collide. Skipped.",
                bend=bi,
            )
            continue
        pairs.append(BendPair(bi, bend, left, right))

    skipped = len(bends) - len(pairs)
    if skipped and pairs:
        report.warn(
            "bends-skipped",
            f"{skipped} of {len(bends)} bend lines could not be paired with an extent line on "
            f"each side and were skipped; the other {len(pairs)} were notched.",
            skipped=skipped,
            total=len(bends),
        )
    if not pairs:
        report.error(
            "no-bends-paired",
            f"None of the {len(bends)} bend lines could be paired with an extent line on each "
            f"side, from {len(extents)} extent line(s) found. Check that the bend and extent "
            f"layers are mapped the right way round.",
            bends=len(bends),
            extents=len(extents),
        )
    elif len(extents) != 2 * len(bends):
        report.info(
            "extent-count",
            f"{len(extents)} extent lines for {len(bends)} bend lines; expected {2 * len(bends)}.",
        )
    return pairs
