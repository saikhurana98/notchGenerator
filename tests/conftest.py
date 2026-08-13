from __future__ import annotations

from pathlib import Path

import ezdxf
import pytest

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "front_suspension - 1 (2.5 mm).dxf"

OUTER = "OUTER_PROFILES"
INTERIOR = "INTERIOR_PROFILES"
BEND = "BEND"
EXTENT = "BEND_EXTENT"

MAPPING = {"outer": OUTER, "interior": INTERIOR, "bend": BEND, "extent": EXTENT}


@pytest.fixture
def sample_path() -> str:
    if not SAMPLE.exists():  # pragma: no cover
        pytest.skip(f"sample file missing: {SAMPLE}")
    return str(SAMPLE)


def make_dxf(
    tmp_path: Path,
    outline: list[tuple[float, float]],
    bends: list[tuple[tuple[float, float], tuple[float, float]]],
    extents: list[tuple[tuple[float, float], tuple[float, float]]],
    name: str = "case.dxf",
) -> str:
    """A minimal flat pattern: a closed straight-edged outline plus bend and extent lines.

    Built from LINE entities only, so a test can reason about the geometry directly
    without spline parameterisation getting in the way.
    """
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(len(outline)):
        a, b = outline[i], outline[(i + 1) % len(outline)]
        msp.add_line(a, b, dxfattribs={"layer": OUTER})
    for a, b in bends:
        msp.add_line(a, b, dxfattribs={"layer": BEND})
    for a, b in extents:
        msp.add_line(a, b, dxfattribs={"layer": EXTENT})
    path = tmp_path / name
    doc.saveas(path)
    return str(path)


def rect_one_bend(tmp_path: Path, half: float = 3.0, inset: float = 0.0, **kw) -> str:
    """A 40x20 plate with a single vertical bend at x=20 and extents at 20 +/- half."""
    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    bends = [((20.0, inset), (20.0, 20.0 - inset))]
    extents = [
        ((20.0 - half, inset), (20.0 - half, 20.0 - inset)),
        ((20.0 + half, 20.0 - inset), (20.0 + half, inset)),
    ]
    return make_dxf(tmp_path, outline, bends, extents, **kw)
