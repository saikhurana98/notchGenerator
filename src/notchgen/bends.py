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
