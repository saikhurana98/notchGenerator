"""Synthetic cases: one straight-edged plate per behaviour, so the numbers are checkable by hand."""

from __future__ import annotations

import math

import pytest

from conftest import BEND, EXTENT, OUTER, make_dxf, rect_one_bend
from notchgen import pipeline
from notchgen.config import Config

PLATE_MAPPING = {"outer": OUTER, "bend": BEND, "extent": EXTENT}


def run(path: str, **overrides):
    return pipeline.process(path, PLATE_MAPPING, Config(**overrides))


def codes(result) -> set[str]:
    return {d.code for d in result.report.items}


def chain_of(result, apex_y: float):
    for notch in result.notches:
        if abs(notch.apex[1] - apex_y) < 1e-9:
            return [(round(float(x), 6), round(float(y), 6)) for x, y in notch.chain]
    raise AssertionError(f"no notch with apex y={apex_y}")


# -- the happy path on a plate whose bend runs edge to edge ----------------------


def test_flush_bend_gives_a_plain_v_with_no_stubs(tmp_path):
    """Bend and extents reach the edge, so each notch is just two legs and an apex."""
    result = run(rect_one_bend(tmp_path))
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    # The chain always runs from the negative-offset side (x=23 here) to the positive one.
    assert chain_of(result, 2.0) == [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]
    assert chain_of(result, 18.0) == [(23.0, 20.0), (20.0, 18.0), (17.0, 20.0)]


def test_flush_bend_cut_area_is_the_triangle(tmp_path):
    result = run(rect_one_bend(tmp_path))
    # base 6 (the 3 mm bend zone either side), height 2 (the depth)
    for notch in result.notches:
        assert notch.cut_area == pytest.approx(0.5 * 6.0 * 2.0)
    assert result.surgery.area_before == pytest.approx(40.0 * 20.0)
    assert result.surgery.area_after == pytest.approx(800.0 - 12.0)


def test_the_spanned_edge_is_split_not_deleted(tmp_path):
    """Both anchors land mid-edge, so that one LINE becomes two fragments."""
    result = run(rect_one_bend(tmp_path))
    fragments = [c for c in result.surgery.kept if not c.is_whole]
    assert len(fragments) == 4  # two per notch: left of the cut and right of it
    spans = sorted(
        (round(float(min(c.a[0], c.b[0])), 6), round(float(max(c.a[0], c.b[0])), 6))
        for c in fragments
    )
    assert spans == [(0.0, 17.0), (0.0, 17.0), (23.0, 40.0), (23.0, 40.0)]


def test_inset_bend_grows_stubs_out_to_the_edge(tmp_path):
    """When the extents stop short of the edge, the notch gets a stub at each shoulder."""
    result = run(rect_one_bend(tmp_path, inset=0.5))
    assert result.ok, result.report.text()
    assert chain_of(result, 2.5) == [
        (23.0, 0.0),
        (23.0, 0.5),
        (20.0, 2.5),
        (17.0, 0.5),
        (17.0, 0.0),
    ]


def test_depth_from_edge_ignores_how_far_the_bend_stops_short(tmp_path):
    """depth-from-edge puts the apex 2 mm into the material regardless of the inset."""
    flush = run(rect_one_bend(tmp_path, inset=0.0, name="a.dxf"), depth_from="edge")
    inset = run(rect_one_bend(tmp_path, inset=0.5, name="b.dxf"), depth_from="edge")
    assert chain_of(flush, 2.0)[1] == (20.0, 2.0)
    assert chain_of(inset, 2.0)[2] == (20.0, 2.0)


def test_rect_shape_gives_a_flat_bottomed_relief(tmp_path):
    result = run(rect_one_bend(tmp_path), shape="rect")
    assert chain_of(result, 2.0) == [(23.0, 0.0), (23.0, 2.0), (17.0, 2.0), (17.0, 0.0)]
    for notch in result.notches:
        assert notch.cut_area == pytest.approx(6.0 * 2.0)


def test_output_profile_still_closes(tmp_path):
    from notchgen import dxfio
    from notchgen.loop import build_loops

    result = run(rect_one_bend(tmp_path))
    out = tmp_path / "out.dxf"
    pipeline.save(result, str(out))
    report = pipeline.Report()
    curves, _ = dxfio.collect_curves(dxfio.read(str(out)), OUTER, report)
    loops, gap = build_loops(curves, Config().stitch_tol)
    assert len(loops) == 1 and loops[0].closed and gap < 1e-9
    assert loops[0].area() == pytest.approx(788.0)


# -- the pairing trap the reviewer flagged ---------------------------------------


def test_neighbouring_bend_cannot_donate_its_extent(tmp_path):
    """Two bends 4 mm apart with 3 mm zones: their extent lines interleave.

    Without the ownership and symmetry checks, the bend at x=20 would grab the extent at
    x=21 (which belongs to the bend at x=24) and cut a 4 mm notch instead of a 6 mm one.
    """
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (44.0, 0.0), (44.0, 20.0), (0.0, 20.0)],
        [((20.0, 0.0), (20.0, 20.0)), ((24.0, 0.0), (24.0, 20.0))],
        [
            ((17.0, 0.0), (17.0, 20.0)),
            ((21.0, 0.0), (21.0, 20.0)),
            ((23.0, 0.0), (23.0, 20.0)),
            ((27.0, 0.0), (27.0, 20.0)),
        ],
    )
    result = run(path)
    assert not result.ok
    assert "asymmetric-pairing" in codes(result)
    detail = next(d for d in result.report.errors if d.code == "asymmetric-pairing")
    assert {detail.context["left_offset"], detail.context["right_offset"]} == {1.0, 3.0}


def test_a_bend_with_only_one_extent_is_refused(tmp_path):
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((20.0, 0.0), (20.0, 20.0))],
        [((17.0, 0.0), (17.0, 20.0))],
    )
    result = run(path)
    assert not result.ok
    assert "unpaired-bend" in codes(result)


def test_a_non_parallel_extent_is_not_adopted(tmp_path):
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((20.0, 0.0), (20.0, 20.0))],
        [((17.0, 0.0), (17.0, 20.0)), ((23.0, 0.0), (25.0, 20.0))],
    )
    result = run(path)
    assert not result.ok
    assert "orphan-extent" in codes(result)
    assert "unpaired-bend" in codes(result)


def test_a_parallel_extent_that_does_not_overlap_is_not_adopted(tmp_path):
    """A short extent alongside only the top of the bend belongs to some other feature."""
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((20.0, 0.0), (20.0, 20.0))],
        [((17.0, 0.0), (17.0, 20.0)), ((23.0, 18.0), (23.0, 20.0))],
    )
    result = run(path)
    assert not result.ok
    assert "orphan-extent" in codes(result)


# -- geometry that must be refused rather than mangled --------------------------


def test_depth_larger_than_half_the_bend_is_refused(tmp_path):
    result = run(rect_one_bend(tmp_path), depth=11.0)
    assert not result.ok
    assert "bend-too-short" in codes(result)


def test_a_bend_that_does_not_reach_an_edge_is_skipped(tmp_path):
    """A bend ending mid-plate needs a different relief topology, so it is left alone."""
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((20.0, 5.0), (20.0, 15.0))],
        [((17.0, 5.0), (17.0, 15.0)), ((23.0, 5.0), (23.0, 15.0))],
    )
    result = run(path)
    assert "no-edge-hit" in codes(result)
    assert result.notches == []
    assert result.ok  # nothing was cut, and nothing was broken


def test_overlapping_cuts_are_refused(tmp_path):
    """Two bends whose notches land on the same stretch of edge must not be cut blindly."""
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((18.0, 0.0), (18.0, 20.0)), ((22.0, 0.0), (22.0, 20.0))],
        [
            ((14.0, 0.0), (14.0, 20.0)),
            ((22.0, 0.0), (22.0, 20.0)),
            ((18.0, 0.0), (18.0, 20.0)),
            ((26.0, 0.0), (26.0, 20.0)),
        ],
    )
    result = run(path)
    assert not result.ok
    assert codes(result) & {"overlapping-cuts", "extent-on-bend", "asymmetric-pairing"}


# -- direction handling ---------------------------------------------------------


def test_reversed_outline_entities_are_stitched(tmp_path):
    """Entity direction in the file is arbitrary; the loop must cope and stay untouched."""
    import ezdxf

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    corners = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    for i in range(4):
        a, b = corners[i], corners[(i + 1) % 4]
        if i % 2:  # write every other edge backwards
            a, b = b, a
        msp.add_line(a, b, dxfattribs={"layer": OUTER})
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})
    msp.add_line((17.0, 0.0), (17.0, 20.0), dxfattribs={"layer": EXTENT})
    msp.add_line((23.0, 20.0), (23.0, 0.0), dxfattribs={"layer": EXTENT})
    path = tmp_path / "reversed.dxf"
    doc.saveas(path)

    result = run(str(path))
    assert result.ok, result.report.text()
    assert chain_of(result, 2.0) == [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]


@pytest.mark.parametrize("degrees", [0.0, 37.0, 90.0, 143.0, -61.0])
def test_result_is_the_same_whatever_angle_the_part_sits_at(tmp_path, degrees):
    """Nothing in the algorithm may assume axis-aligned bends.

    The whole plate is rotated, so the notch chain must come back as the same points,
    rotated by the same amount.
    """
    theta = math.radians(degrees)
    cos, sin = math.cos(theta), math.sin(theta)

    def rot(p):
        return (p[0] * cos - p[1] * sin, p[0] * sin + p[1] * cos)

    outline = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]
    bend = ((20.0, 0.0), (20.0, 20.0))
    extents = [((17.0, 0.0), (17.0, 20.0)), ((23.0, 20.0), (23.0, 0.0))]
    path = make_dxf(
        tmp_path,
        [rot(p) for p in outline],
        [(rot(bend[0]), rot(bend[1]))],
        [(rot(a), rot(b)) for a, b in extents],
        name=f"rot{degrees}.dxf",
    )
    result = run(path)
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    for notch in result.notches:
        assert notch.cut_area == pytest.approx(0.5 * 6.0 * 2.0, rel=1e-9)

    apex_near_origin = min(result.notches, key=lambda n: n.apex[0] ** 2 + n.apex[1] ** 2)
    expected = [rot(p) for p in [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]]
    for got, want in zip(apex_near_origin.chain, expected):
        assert got[0] == pytest.approx(want[0], abs=1e-9)
        assert got[1] == pytest.approx(want[1], abs=1e-9)


# -- inspection -----------------------------------------------------------------


def test_bend_and_extent_layers_are_guessed_from_their_counts(tmp_path):
    """Even with unhelpful names, the extent layer has twice the lines of the bend layer."""
    import ezdxf

    doc = ezdxf.new("R2000")
    msp = doc.modelspace()
    for i in range(4):
        msp.add_line((i, 0), (i + 1, 1), dxfattribs={"layer": "PROFILE_OUT"})
    msp.add_line((0, 0), (0, 1), dxfattribs={"layer": "LayerA"})
    for i in range(2):
        msp.add_line((i, 0), (i, 1), dxfattribs={"layer": "LayerB"})
    path = tmp_path / "odd-names.dxf"
    doc.saveas(path)

    found = pipeline.inspect(str(path))
    assert found.suggested["outer"] == "PROFILE_OUT"
    assert found.suggested["bend"] == "LayerA"
    assert found.suggested["extent"] == "LayerB"


def test_a_gappy_profile_is_reported_not_guessed_at(tmp_path):
    path = make_dxf(
        tmp_path,
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        [((20.0, 0.0), (20.0, 20.0))],
        [((17.0, 0.0), (17.0, 20.0)), ((23.0, 0.0), (23.0, 20.0))],
    )
    import ezdxf

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    victim = next(e for e in msp if e.dxf.layer == OUTER)
    msp.delete_entity(victim)
    doc.saveas(path)

    result = run(path)
    assert not result.ok
    # A missing edge leaves one open chain, so the closure gap is the useful number.
    assert "profile-not-closed" in codes(result)
    detail = next(d for d in result.report.errors if d.code == "profile-not-closed")
    assert detail.context["close_gap"] > 1.0
    assert detail.context["worst_gap"] < 1e-9


# -- entity types other than LINE on the outer profile ---------------------------


def test_an_arc_in_the_outer_profile_is_handled(tmp_path):
    """A slot-shaped plate: two straight edges and two semicircular ends."""
    import ezdxf

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_line((0.0, 0.0), (40.0, 0.0), dxfattribs={"layer": OUTER})
    msp.add_arc((40.0, 10.0), 10.0, -90.0, 90.0, dxfattribs={"layer": OUTER})
    msp.add_line((40.0, 20.0), (0.0, 20.0), dxfattribs={"layer": OUTER})
    msp.add_arc((0.0, 10.0), 10.0, 90.0, 270.0, dxfattribs={"layer": OUTER})
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})
    msp.add_line((17.0, 0.0), (17.0, 20.0), dxfattribs={"layer": EXTENT})
    msp.add_line((23.0, 20.0), (23.0, 0.0), dxfattribs={"layer": EXTENT})
    path = tmp_path / "slot.dxf"
    doc.saveas(path)

    result = run(str(path))
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert chain_of(result, 2.0) == [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]
    # The straight edges are split; both arcs are far from the cuts and survive whole.
    arcs = [c for c in result.surgery.kept if type(c).__name__ == "ArcCurve"]
    assert len(arcs) == 2 and all(c.is_whole for c in arcs)


def test_an_lwpolyline_outer_profile_is_exploded_and_reported(tmp_path):
    """A single closed LWPOLYLINE outline loses its identity, and the user is told."""
    import ezdxf

    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)],
        close=True,
        dxfattribs={"layer": OUTER},
    )
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})
    msp.add_line((17.0, 0.0), (17.0, 20.0), dxfattribs={"layer": EXTENT})
    msp.add_line((23.0, 20.0), (23.0, 0.0), dxfattribs={"layer": EXTENT})
    path = tmp_path / "poly.dxf"
    doc.saveas(path)

    result = run(str(path))
    assert result.ok, result.report.text()
    assert "exploded-entity" in codes(result)
    assert len(result.notches) == 2
    assert chain_of(result, 2.0) == [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]

    out = tmp_path / "poly-out.dxf"
    pipeline.save(result, str(out))
    written = ezdxf.readfile(out)
    assert not [e for e in written.modelspace() if e.dxftype() == "LWPOLYLINE"]
    assert {e.dxftype() for e in written.modelspace()} == {"LINE"}
