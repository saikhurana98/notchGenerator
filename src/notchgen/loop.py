"""Stitch profile curves into an ordered closed loop, and cut pieces out of it.

The loop is addressed by a single scalar ``g = link_index + s`` where ``s`` is the
traversal parameter inside that link. That makes "remove the stretch between these two
points" a plain interval operation, including the wrap-around case.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .curves import Curve
from .geom import Point, norm, signed_area


@dataclass
class Link:
    """One curve as traversed by the loop, with its direction relative to the source."""

    curve: Curve
    flipped: bool

    def point(self, s: float) -> Point:
        return self.curve.point(1.0 - s if self.flipped else s)

    def start(self) -> Point:
        return self.point(0.0)

    def end(self) -> Point:
        return self.point(1.0)

    def flatten(self, chord_tol: float) -> np.ndarray:
        pts = self.curve.flatten(chord_tol)
        return pts[::-1] if self.flipped else pts

    def to_source_param(self, s: float) -> float:
        return 1.0 - s if self.flipped else s

    def sub_curve(self, s0: float, s1: float) -> Curve:
        """The piece of this link's curve between traversal parameters s0 < s1."""
        t0, t1 = self.to_source_param(s0), self.to_source_param(s1)
        if t0 > t1:
            t0, t1 = t1, t0
        c = self.curve
        if t1 < 1.0 - 1e-12:
            c = c.split(t1)[0]
            t0 = t0 / t1 if t1 > 0 else 0.0
        if t0 > 1e-12:
            c = c.split(t0)[1]
        return c


@dataclass
class Anchor:
    """A located point on the loop."""

    link_index: int
    s: float
    point: Point
    snapped_vertex: bool = False

    @property
    def g(self) -> float:
        return self.link_index + self.s


class LoopError(Exception):
    pass


@dataclass
class Loop:
    links: list[Link]
    closed: bool
    worst_gap: float
    """Largest gap bridged between consecutive curves."""

    close_gap: float = 0.0
    """Distance from the last curve's end back to the first curve's start."""

    bridged: float = 0.0
    """Closure gap that was accepted to make this a cycle, if any."""

    def __len__(self) -> int:
        return len(self.links)

    def ends(self) -> tuple[Point, Point]:
        return self.links[0].start(), self.links[-1].end()

    def flatten(self, chord_tol: float) -> np.ndarray:
        out: list[np.ndarray] = []
        for i, lk in enumerate(self.links):
            pts = lk.flatten(chord_tol)
            out.append(pts if i == 0 else pts[1:])
        return np.vstack(out)

    def vertices(self) -> list[tuple[int, int, Point]]:
        """Every link junction, as (link_index, side, point) with side 0=start, 1=end."""
        out = []
        for i, lk in enumerate(self.links):
            out.append((i, 0, lk.start()))
            out.append((i, 1, lk.end()))
        return out

    def point_at(self, g: float) -> Point:
        n = len(self.links)
        g = g % n
        i = int(g)
        return self.links[i].point(g - i)

    def area(self, chord_tol: float = 1e-3) -> float:
        return abs(signed_area(self.flatten(chord_tol)))

    # -- interval algebra ------------------------------------------------------

    def slice(self, g0: float, g1: float) -> list[tuple[int, float, float]]:
        """Pieces of the loop from g0 forward to g1, as (link_index, s0, s1)."""
        n = len(self.links)
        if g1 < g0:
            g1 += n
        out: list[tuple[int, float, float]] = []
        i = int(g0) % n
        s = g0 - int(g0)
        remaining_start = g0
        while True:
            link_end = int(remaining_start) + 1.0
            if g1 <= link_end:
                if g1 - remaining_start > 1e-12:
                    out.append((i, s, g1 - int(remaining_start)))
                break
            if 1.0 - s > 1e-12:
                out.append((i, s, 1.0))
            remaining_start = link_end
            i = (i + 1) % n
            s = 0.0
        return out

    def sample_slice(self, g0: float, g1: float, n: int = 60) -> np.ndarray:
        n_links = len(self.links)
        span = (g1 - g0) % n_links
        if span == 0:
            span = n_links
        gs = g0 + np.linspace(0.0, span, n)
        return np.array([self.point_at(g) for g in gs])

    def flatten_slice(self, g0: float, g1: float, chord_tol: float) -> np.ndarray:
        """Points along the loop from g0 forward to g1, in traversal order."""
        out: list[np.ndarray] = []
        for li, s0, s1 in self.slice(g0, g1):
            lk = self.links[li]
            piece = lk.sub_curve(s0, s1)
            pts = piece.flatten(chord_tol)
            if norm(pts[0] - lk.point(s0)) > norm(pts[-1] - lk.point(s0)):
                pts = pts[::-1]
            out.append(pts if not out else pts[1:])
        if not out:
            return np.zeros((0, 2))
        return np.vstack(out)

    def curves_for_slice(self, g0: float, g1: float) -> list[Curve]:
        out: list[Curve] = []
        for li, s0, s1 in self.slice(g0, g1):
            lk = self.links[li]
            if s0 <= 1e-12 and s1 >= 1.0 - 1e-12:
                out.append(lk.curve)
            else:
                out.append(lk.sub_curve(s0, s1))
        return out


def drop_duplicate_curves(
    curves: list[Curve], tol: float, chord_tol: float = 1e-3
) -> tuple[list[Curve], list[Curve], list[Curve]]:
    """Split curves into (kept, duplicates, degenerate).

    A profile carrying the same edge twice sends the stitcher down the copy and back,
    so the walk consumes everything and still ends up somewhere other than where it
    started. Zero-length entities cause the same kind of confusion.
    """
    kept: list[Curve] = []
    duplicates: list[Curve] = []
    degenerate: list[Curve] = []
    signatures: list[tuple[Point, Point, float]] = []
    for c in curves:
        a, b = c.start(), c.end()
        length = c.length(chord_tol)
        if length <= tol and norm(b - a) <= tol:
            degenerate.append(c)
            continue
        match = False
        for sa, sb, slen in signatures:
            if abs(slen - length) > max(tol, 1e-9):
                continue
            same = norm(sa - a) <= tol and norm(sb - b) <= tol
            flipped = norm(sa - b) <= tol and norm(sb - a) <= tol
            if same or flipped:
                match = True
                break
        if match:
            duplicates.append(c)
        else:
            signatures.append((a, b, length))
            kept.append(c)
    return kept, duplicates, degenerate


def build_loops(
    curves: list[Curve], stitch_tol: float, bridge_tol: float = 0.0
) -> tuple[list[Loop], float]:
    """Stitch curves end-to-end into loops.

    Returns the loops plus the worst junction gap actually used, so the caller can
    report how much slack the tolerance had to absorb. Each loop also carries its own
    `close_gap`, which is the number that matters when a chain fails to close —
    junction gaps can all be perfect while the two ends of the chain sit far apart.

    `bridge_tol`, when larger than `stitch_tol`, accepts a chain whose ends are that
    close as a cycle, recording the gap in `Loop.bridged`.
    """
    remaining = list(curves)
    loops: list[Loop] = []
    worst_overall = 0.0
    while remaining:
        seed = remaining.pop(0)
        links = [Link(seed, flipped=False)]
        worst = 0.0
        while True:
            tail = links[-1].end()
            best = None
            for k, c in enumerate(remaining):
                for flip, p in ((False, c.start()), (True, c.end())):
                    g = norm(p - tail)
                    if best is None or g < best[0]:
                        best = (g, k, flip)
            if best is None or best[0] > stitch_tol:
                break
            gap, k, flip = best
            worst = max(worst, gap)
            links.append(Link(remaining.pop(k), flipped=flip))
            if norm(links[-1].end() - links[0].start()) <= stitch_tol:
                break
        close_gap = norm(links[-1].end() - links[0].start())
        closed = close_gap <= stitch_tol
        bridged = 0.0
        if not closed and close_gap <= bridge_tol:
            closed = True
            bridged = close_gap
        worst = max(worst, close_gap if closed else 0.0)
        worst_overall = max(worst_overall, worst)
        loops.append(Loop(links, closed, worst, close_gap, bridged))
    return loops, worst_overall


def snap_to_vertex(loop: Loop, point: Point, tol: float) -> Anchor | None:
    """Nearest link junction within `tol`, as an Anchor sitting exactly on it.

    Tried before any curve intersection: a ray aimed at a corner is the common case in
    Fusion exports, where the extent line's x matches a profile vertex to ~1e-13 and the
    curve-crossing test would return zero or two roots depending on rounding.
    """
    best = None
    for li, side, v in loop.vertices():
        d = norm(v - point)
        if d <= tol and (best is None or d < best[0]):
            best = (d, li, side, v)
    if best is None:
        return None
    _, li, side, v = best
    return Anchor(li, float(side), v, snapped_vertex=True)


def normalise_anchor(loop: Loop, a: Anchor) -> Anchor:
    """Move an anchor sitting at s==1 onto the next link's s==0, so g is canonical."""
    if a.s >= 1.0 - 1e-12:
        return Anchor((a.link_index + 1) % len(loop.links), 0.0, a.point, a.snapped_vertex)
    return a


def complement_intervals(n_links: int, removals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Stretches of the loop left over after removing the given forward intervals."""
    spans = []
    for g0, g1 in removals:
        length = (g1 - g0) % n_links
        spans.append((g0 % n_links, length))
    spans.sort()
    keeps: list[tuple[float, float]] = []
    for i, (g0, length) in enumerate(spans):
        end = g0 + length
        nxt = spans[(i + 1) % len(spans)][0]
        gap = (nxt - end) % n_links
        if gap > 1e-9:
            keeps.append((end % n_links, nxt))
    return keeps


def intervals_overlap(n_links: int, removals: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """Pairs of removal intervals that overlap. Empty means the cuts are independent."""
    clashes = []
    for i in range(len(removals)):
        for j in range(i + 1, len(removals)):
            a0, a1 = removals[i]
            b0, b1 = removals[j]
            la = (a1 - a0) % n_links
            lb = (b1 - b0) % n_links
            # b0 inside a, or a0 inside b
            if ((b0 - a0) % n_links) < la - 1e-9 or ((a0 - b0) % n_links) < lb - 1e-9:
                clashes.append((i, j))
    return clashes
