"""Tolerances and options, all in model units (mm for a Fusion flat-pattern export).

The defaults are calibrated against the real numbers in a Fusion export: junction gaps
come out at ~4e-10, an extent line's coordinate matches a profile vertex to ~1e-13 or
misses a corner fillet by ~0.005, and the smallest genuine profile feature is 0.079 long.
So the stitch tolerance has to be tiny and the vertex-snap tolerance has to sit between
0.005 and 0.079/2.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Config:
    depth: float = 2.0
    """Notch depth, measured per `depth_from`."""

    depth_from: str = "bend-end"
    """"bend-end" (the literal spec) or "edge" (constant relief depth in material)."""

    shape: str = "v"
    """"v" = single apex; "rect" = flat-bottomed relief of bend-zone width."""

    stitch_tol: float = 1e-6
    """Max gap tolerated when stitching profile entities into a loop."""

    snap_tol: float = 0.02
    """Ray hit or extent endpoint snapping onto an existing profile vertex."""

    sliver_tol: float = 0.05
    """Shortest fragment we are willing to emit; below this we snap instead of split."""

    chord_tol: float = 1e-3
    """Sagitta used when flattening curves for hit-testing, area and display."""

    bridge_tol: float = 0.01
    """A profile left open by less than this is treated as closed, with a warning.

    Sits above the ~1e-10 junction gaps a clean export produces and far below the
    smallest genuine profile feature, so it only ever rescues a sketch that failed to
    close by a rounding-scale amount."""

    angle_tol: float = 1e-3
    """Parallelism test on unit vectors, via |cross|."""

    overlap_frac: float = 0.6
    """Required overlap of an extent with its bend, along the bend direction."""

    symmetry_tol_abs: float = 0.05
    symmetry_tol_rel: float = 0.05
    """A bend line sits mid-zone, so |s_left| and |s_right| must agree."""

    max_stub: float = 1.0
    """Beyond this, the bend end is not reaching a free edge and we skip it."""

    max_endpoint_skew: float = 1.0
    """How far an extent's end may sit from the bend end, along the bend."""

    max_cut_frac: float = 0.25
    """A single notch removing more than this share of the part means a wrong arc."""

    thickness: float | None = None
    """Sheet thickness, if known. Only used to warn about under-thickness relief."""

    merge_overlapping: bool = False
    """Allow notches whose cuts overlap to be merged instead of erroring."""

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "Config":
        data = data or {}
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in fields and v is not None})
