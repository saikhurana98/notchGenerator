"""Bend and extent geometry that is not written as LINE entities.

Fusion does not always export these as plain lines. A bend or an extent can arrive as a
two-vertex LWPOLYLINE, several of them can share a single polyline, and being polylines they
carry an object coordinate system that has to be undone like any other. Discarding them leaves
bends with nothing to pair against, which surfaces as a wall of "0 extent(s) on one side".
"""

from __future__ import annotations

import ezdxf
import pytest

from conftest import BEND, EXTENT, OUTER
from notchgen import dxfio, pipeline
from notchgen.config import Config

MAPPING = {"outer": OUTER, "bend": BEND, "extent": EXTENT}
SQUARE = [(0.0, 0.0), (40.0, 0.0), (40.0, 20.0), (0.0, 20.0)]


def build(tmp_path, bend_writer, extent_writer, name="case.dxf"):
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(len(SQUARE)):
        msp.add_line(SQUARE[i], SQUARE[(i + 1) % len(SQUARE)], dxfattribs={"layer": OUTER})
    bend_writer(msp)
    extent_writer(msp)
    path = tmp_path / name
    doc.saveas(path)
    return str(path)


def as_lines(msp):
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})


def extents_as_lines(msp):
    msp.add_line((17.0, 0.0), (17.0, 20.0), dxfattribs={"layer": EXTENT})
    msp.add_line((23.0, 20.0), (23.0, 0.0), dxfattribs={"layer": EXTENT})


def extents_as_polylines(msp, **attribs):
    for x in (17.0, 23.0):
        msp.add_lwpolyline(
            [(x, 0.0), (x, 20.0)], dxfattribs={"layer": EXTENT, **attribs}
        )


def run(path, **overrides):
    return pipeline.process(path, MAPPING, Config(**overrides))


def codes(result):
    return {d.code for d in result.report.items}


EXPECTED_CHAIN = [(23.0, 0.0), (20.0, 2.0), (17.0, 0.0)]


def bottom_chain(result):
    notch = min(result.notches, key=lambda n: n.apex[1])
    return [(round(float(x), 6), round(float(y), 6)) for x, y in notch.chain]


# -- the regression --------------------------------------------------------------


def test_extents_written_as_polylines_are_used(tmp_path):
    path = build(tmp_path, as_lines, extents_as_polylines)
    result = run(path)
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert bottom_chain(result) == EXPECTED_CHAIN
    assert "unpaired-bend" not in codes(result)


def test_bend_written_as_a_polyline_is_used(tmp_path):
    def bend(msp):
        msp.add_lwpolyline([(20.0, 0.0), (20.0, 20.0)], dxfattribs={"layer": BEND})

    result = run(build(tmp_path, bend, extents_as_lines))
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert bottom_chain(result) == EXPECTED_CHAIN


def test_both_extents_on_one_multi_segment_polyline(tmp_path):
    """A single polyline can carry several extents as separate straight runs."""

    def extents(msp):
        # Down one extent, across, and back up the other: three runs, two of them extents.
        msp.add_lwpolyline(
            [(17.0, 20.0), (17.0, 0.0), (23.0, 0.0), (23.0, 20.0)],
            dxfattribs={"layer": EXTENT},
        )

    result = run(build(tmp_path, as_lines, extents))
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert bottom_chain(result) == EXPECTED_CHAIN


def test_polyline_extents_with_a_flipped_extrusion_are_not_mirrored(tmp_path):
    """A polyline carries an OCS, so a -Z extrusion mirrors x just as it does for a hole."""

    def extents(msp):
        for x in (-17.0, -23.0):
            msp.add_lwpolyline(
                [(x, 0.0), (x, 20.0)],
                dxfattribs={"layer": EXTENT, "extrusion": (0, 0, -1)},
            )

    result = run(build(tmp_path, as_lines, extents))
    assert result.ok, result.report.text()
    assert bottom_chain(result) == EXPECTED_CHAIN


def test_a_curved_segment_on_the_bend_layer_is_reported(tmp_path):
    def extents(msp):
        extents_as_lines(msp)
        msp.add_arc((5.0, 5.0), 2.0, 0.0, 90.0, dxfattribs={"layer": EXTENT})

    result = run(build(tmp_path, as_lines, extents))
    assert "curved-on-bend-layer" in codes(result)
    # The straight extents still pair up, so the part is still notched.
    assert result.ok, result.report.text()
    assert len(result.notches) == 2


def test_text_on_the_extent_layer_is_reported_not_treated_as_a_line(tmp_path):
    def extents(msp):
        extents_as_lines(msp)
        msp.add_text("note", dxfattribs={"layer": EXTENT, "insert": (5.0, 5.0)})

    result = run(build(tmp_path, as_lines, extents))
    assert "unusable-on-bend-layer" in codes(result)
    assert result.ok, result.report.text()


# -- one bad bend must not sink the rest ----------------------------------------


def test_an_unpairable_bend_is_skipped_and_the_others_are_notched(tmp_path):
    """A large part with many bends must not fail wholesale over one odd bend."""

    def bends(msp):
        msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})
        msp.add_line((32.0, 0.0), (32.0, 20.0), dxfattribs={"layer": BEND})  # no extents

    result = run(build(tmp_path, bends, extents_as_lines))
    assert result.ok, result.report.text()
    assert len(result.notches) == 2
    assert "unpaired-bend" in codes(result)
    summary = next(d for d in result.report.items if d.code == "bends-skipped")
    assert summary.context == {"skipped": 1, "total": 2}


def test_when_no_bend_can_be_paired_that_is_a_hard_error(tmp_path):
    def bends(msp):
        msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})

    def extents(msp):
        # The layer exists but holds nothing usable, so pairing runs and finds nothing.
        msp.add_text("note", dxfattribs={"layer": EXTENT, "insert": (5.0, 5.0)})

    result = run(build(tmp_path, bends, extents))
    assert not result.ok
    detail = next(d for d in result.report.errors if d.code == "no-bends-paired")
    assert detail.context == {"bends": 1, "extents": 0}


def test_an_empty_extent_layer_is_reported_before_pairing(tmp_path):
    def bends(msp):
        msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": BEND})

    result = run(build(tmp_path, bends, lambda msp: None))
    assert not result.ok
    assert "unknown-layer" in {d.code for d in result.report.errors}


def test_swapped_bend_and_extent_layers_are_called_out(tmp_path):
    """The two extent lines on the bend layer, and the bend line on the extent layer."""

    def bends(msp):
        for x in (17.0, 23.0):
            msp.add_line((x, 0.0), (x, 20.0), dxfattribs={"layer": BEND})

    def extents(msp):
        msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": EXTENT})

    # Each "bend" sees the single "extent" on one side only, so neither can pair.
    result = run(build(tmp_path, bends, extents))
    assert not result.ok
    assert "no-bends-paired" in {d.code for d in result.report.errors}
    assert "layers are mapped the right way round" in next(
        d.message for d in result.report.errors if d.code == "no-bends-paired"
    )


# -- the sample must be unaffected ----------------------------------------------


def test_the_sample_still_finds_two_bends_and_four_extents(sample_path):
    report = pipeline.Report()
    doc = dxfio.read(sample_path)
    assert len(dxfio.segments_on_layer(doc, "BEND", report)) == 2
    assert len(dxfio.segments_on_layer(doc, "BEND_EXTENT", report)) == 4
    assert report.items == []
