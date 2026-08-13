"""Turning the outer-profile entities into one closed outline.

The failure that matters here is a chain whose junctions are all perfect but whose two
ends do not meet. Reporting the worst junction gap in that case is useless — it will read
as about 1e-14 — so these tests pin the closure gap and the loose-end coordinates instead.
"""

from __future__ import annotations

import ezdxf
import pytest

from conftest import BEND, EXTENT, OUTER, make_dxf
from notchgen import pipeline
from notchgen.config import Config
from notchgen.curves import curves_from_entity
from notchgen.loop import build_loops, drop_duplicate_curves

PLATE_MAPPING = {"outer": OUTER, "bend": BEND, "extent": EXTENT}
SQUARE = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
BEND_AT_20 = [((20.0, 0.0), (20.0, 20.0))]
EXTENTS_AT_20 = [((17.0, 0.0), (17.0, 20.0)), ((23.0, 20.0), (23.0, 0.0))]


def run(path, **overrides):
    return pipeline.process(str(path), PLATE_MAPPING, Config(**overrides))


def codes(result):
    return {d.code for d in result.report.items}


def plate(tmp_path, **kw):
    return make_dxf(tmp_path, SQUARE, BEND_AT_20, EXTENTS_AT_20, **kw)


def outer_edges(doc):
    curves = []
    for eid, e in enumerate(doc.modelspace()):
        if e.dxf.layer == OUTER:
            curves.extend(curves_from_entity(e, eid))
    return curves


# -- the diagnostic itself -------------------------------------------------------


def open_at_the_seed(path, x: float):
    """Pull back the start of the first outer edge, leaving one open chain.

    The break has to be at the seed's start: shortening an edge's *end* instead splits the
    profile into two chains, which is a different diagnostic.
    """
    doc = ezdxf.readfile(path)
    bottom = next(e for e in doc.modelspace() if e.dxf.layer == OUTER)
    bottom.dxf.start = (x, 0.0)
    doc.saveas(path)


def test_an_open_chain_reports_the_closure_gap_not_the_junction_gap(tmp_path):
    path = plate(tmp_path)
    open_at_the_seed(path, 3.0)

    result = run(path)
    assert not result.ok
    assert "profile-not-closed" in codes(result)
    detail = next(d for d in result.report.errors if d.code == "profile-not-closed")
    assert detail.context["close_gap"] == pytest.approx(3.0, abs=1e-6)
    assert detail.context["worst_gap"] < 1e-9
    # The message has to name where to look, not just that something is wrong.
    assert "(3.0000, 0.0000)" in detail.message
    assert "(0.0000, 0.0000)" in detail.message
    assert len(result.notches) == 0


def open_mid_chain(path, gap: float):
    """Open a junction in the middle of the chain rather than at the seam.

    This is the case a closure-only fix misses: the trace breaks in two here, so it fails
    with a separate-chains error instead of an unclosed-outline one.
    """
    doc = ezdxf.readfile(path)
    right = next(
        e for e in doc.modelspace()
        if e.dxf.layer == OUTER and e.dxf.start.x == 40.0 and e.dxf.end.x == 40.0
    )
    right.dxf.start = (40.0, gap)
    doc.saveas(path)


@pytest.mark.parametrize("where", ["seam", "mid"])
def test_rounding_noise_is_closed_silently_wherever_it_sits(tmp_path, where):
    """A 4.5e-5 gap is two orders below a laser kerf, so it must not need a decision."""
    path = plate(tmp_path, name=f"noise-{where}.dxf")
    if where == "seam":
        open_at_the_seed(path, 4.484e-05)
    else:
        open_mid_chain(path, 4.484e-05)

    result = run(path)
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert "profile-gap-closed" in codes(result)
    # Nothing the operator has to act on, so nothing the portal shows.
    assert not [d for d in result.report.items if d.level in ("warn", "error")]


@pytest.mark.parametrize("where", ["seam", "mid"])
def test_a_gap_worth_looking_at_is_warned_about(tmp_path, where):
    path = plate(tmp_path, name=f"warn-{where}.dxf")
    if where == "seam":
        open_at_the_seed(path, 0.005)
    else:
        open_mid_chain(path, 0.005)

    result = run(path)
    assert result.ok, result.report.text()
    assert "profile-gap-bridged" in codes(result)
    detail = next(d for d in result.report.items if d.code == "profile-gap-bridged")
    assert detail.level == "warn"
    assert detail.context["gap"] == pytest.approx(0.005, abs=1e-6)


def test_a_mid_chain_gap_was_previously_a_hard_failure(tmp_path):
    """bridge_tol=0 is the old behaviour: the same file fails as two separate chains."""
    path = plate(tmp_path)
    open_mid_chain(path, 4.484e-05)
    assert "profile-not-one-loop" in codes(run(path, bridge_tol=0.0))
    assert run(path).ok


def test_a_rounding_scale_gap_is_bridged_with_a_warning(tmp_path):
    path = plate(tmp_path)
    open_at_the_seed(path, 0.004)

    result = run(path)
    assert result.ok, result.report.text()
    assert "profile-gap-bridged" in codes(result)
    detail = next(d for d in result.report.items if d.code == "profile-gap-bridged")
    assert detail.context["gap"] == pytest.approx(0.004, abs=1e-6)
    assert len(result.notches) == 2


def test_a_gap_above_the_bridging_limit_is_refused(tmp_path):
    path = plate(tmp_path)
    open_at_the_seed(path, 0.5)

    assert "profile-not-closed" in codes(run(path))
    # ...unless the operator says the gap is not real.
    assert run(path, bridge_tol=1.0).ok


def test_two_separate_outlines_are_reported_as_such(tmp_path):
    path = plate(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    ring = [(60.0, 0.0), (80.0, 0.0), (80.0, 10.0), (60.0, 10.0)]
    for i in range(4):
        msp.add_line(ring[i], ring[(i + 1) % 4], dxfattribs={"layer": OUTER})
    doc.saveas(path)

    result = run(path)
    assert not result.ok
    assert "profile-not-one-loop" in codes(result)
    detail = next(d for d in result.report.errors if d.code == "profile-not-one-loop")
    assert detail.context["loops"] == 2
    assert "2 separate chains" in detail.message


# -- entities that break a naive trace ------------------------------------------


def test_a_duplicated_edge_is_dropped_and_reported(tmp_path):
    """The same edge twice sends a greedy trace down the copy and back again."""
    path = plate(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (40.0, 0.0), dxfattribs={"layer": OUTER})
    doc.saveas(path)

    result = run(path)
    assert result.ok, result.report.text()
    assert "duplicate-profile-entity" in codes(result)
    assert len(result.notches) == 2


def test_a_reversed_duplicate_is_also_caught(tmp_path):
    path = plate(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    msp.add_line((40.0, 0.0), (0.0, 0.0), dxfattribs={"layer": OUTER})
    doc.saveas(path)

    result = run(path)
    assert result.ok, result.report.text()
    assert "duplicate-profile-entity" in codes(result)


def test_a_zero_length_entity_is_dropped_and_reported(tmp_path):
    path = plate(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    msp.add_line((40.0, 0.0), (40.0, 0.0), dxfattribs={"layer": OUTER})
    doc.saveas(path)

    result = run(path)
    assert result.ok, result.report.text()
    assert "degenerate-profile-entity" in codes(result)
    assert len(result.notches) == 2


def test_deduplication_keeps_genuinely_distinct_edges(tmp_path):
    """Two edges of equal length that merely share a vertex must both survive."""
    path = plate(tmp_path)
    curves = outer_edges(ezdxf.readfile(path))
    kept, duplicates, degenerate = drop_duplicate_curves(curves, 1e-6)
    assert len(kept) == 4
    assert duplicates == [] and degenerate == []


# -- the sample must be unaffected ----------------------------------------------


def test_the_sample_still_stitches_without_any_warning(sample_path):
    doc = ezdxf.readfile(sample_path)
    curves = outer_edges(doc)
    kept, duplicates, degenerate = drop_duplicate_curves(curves, 1e-6)
    assert len(kept) == 22
    assert duplicates == [] and degenerate == []

    loops, worst = build_loops(kept, 1e-6, 0.01)
    assert len(loops) == 1
    assert loops[0].closed
    assert loops[0].bridged == 0.0
    assert loops[0].close_gap < 1e-9
    assert worst < 1e-9


# -- bend and extent lines on the wrong layer -----------------------------------


def test_bend_lines_on_the_outer_layer_are_named_in_the_error(tmp_path):
    """They dead-end inside the part, so the fix is a layer change, not a tolerance."""
    path = plate(tmp_path)
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    for x in (17.0, 20.0, 23.0):
        msp.add_line((x, 0.0), (x, 20.0), dxfattribs={"layer": OUTER})
    doc.saveas(path)

    result = run(path)
    assert not result.ok
    detail = next(d for d in result.report.errors if d.code.startswith("profile-"))
    assert "bend or bend-extent" in detail.message


def test_a_clean_file_is_not_accused_of_contamination(sample_path):
    from notchgen import dxfio
    from notchgen.pipeline import _contaminating_lines

    report = pipeline.Report()
    doc = dxfio.read(sample_path)
    curves, _ = dxfio.collect_curves(doc, OUTER, report)
    bends = dxfio.segments_on_layer(doc, "BEND", report)
    extents = dxfio.segments_on_layer(doc, "BEND_EXTENT", report)
    assert _contaminating_lines(curves, [*bends, *extents]) == 0
