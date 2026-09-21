"""Onshape flat-pattern exports, which are laid out differently from Fusion's.

Three differences matter. The holes share a layer with the outline (SHEETMETAL_CUT_LINES).
Bend lines are split by direction over two layers. And tangent lines — Onshape's name for
bend extents — are only present when they were switched on for the export, so more often
than not the bend zone has to be read off the outline instead.
"""

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
import numpy as np
import pytest
from fastapi.testclient import TestClient

from notchgen import cli, dxfio, pipeline
from notchgen.config import Config
from notchgen.curves import ArcCurve
from notchgen.loop import build_loops, drop_duplicate_curves

CUT = "SHEETMETAL_CUT_LINES"
UP = "SHEETMETAL_BEND_LINES_UP"
DOWN = "SHEETMETAL_BEND_LINES_DOWN"
TANGENT = "SHEETMETAL_BEND_TANGENT_LI"  # Onshape cuts the name short at 26 characters

BENDS = [UP, DOWN]

# A 50x20 plate. One bend up at x=20 and one down at x=35, each with a 6 wide bend zone,
# and the outline carries a vertex wherever a zone edge meets it, as Onshape's does.
ZONE_X = (17.0, 23.0, 32.0, 38.0)
OUTLINE = (
    [(0.0, 0.0), *[(x, 0.0) for x in ZONE_X], (50.0, 0.0)]
    + [(50.0, 20.0), *[(x, 20.0) for x in reversed(ZONE_X)], (0.0, 20.0)]
)


def onshape_dxf(
    tmp_path: Path,
    outline=OUTLINE,
    tangents: bool = False,
    holes: bool = True,
    name: str = "onshape.dxf",
) -> str:
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(len(outline)):
        msp.add_line(outline[i], outline[(i + 1) % len(outline)], dxfattribs={"layer": CUT})
    if holes:
        msp.add_circle((8.0, 10.0), 2.0, dxfattribs={"layer": CUT})
        msp.add_circle((44.0, 10.0), 2.0, dxfattribs={"layer": CUT})
        slot = [(26.0, 8.0), (30.0, 8.0), (30.0, 12.0), (26.0, 12.0)]
        for i in range(4):
            msp.add_line(slot[i], slot[(i + 1) % 4], dxfattribs={"layer": CUT})
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": UP})
    msp.add_line((35.0, 20.0), (35.0, 0.0), dxfattribs={"layer": DOWN})
    if tangents:
        for x in ZONE_X:
            msp.add_line((x, 0.0), (x, 20.0), dxfattribs={"layer": TANGENT})
    path = tmp_path / name
    doc.saveas(path)
    return str(path)


def suggested(path: str) -> dict:
    return {k: v for k, v in pipeline.inspect(path).suggested.items() if v}


def codes(result) -> set[str]:
    return {d.code for d in result.report.items}


def chains(result) -> list:
    return sorted(
        (n.bend_index, n.end_index, [(round(float(x), 6), round(float(y), 6)) for x, y in n.chain])
        for n in result.notches
    )


# -- layer detection -------------------------------------------------------------


def test_onshape_layers_are_recognised(tmp_path):
    found = pipeline.inspect(onshape_dxf(tmp_path, tangents=True))
    assert found.suggested == {
        "outer": CUT,
        "interior": None,
        "bend": sorted(BENDS),
        "extent": TANGENT,
    }
    assert not [d for d in found.report.items if d.level == "warn"]


def test_a_single_bend_layer_stays_a_plain_string(tmp_path):
    layers = [dxfio.LayerInfo(CUT, {"LINE": 9}), dxfio.LayerInfo(UP, {"LINE": 2})]
    assert dxfio.suggest_mapping(layers)["bend"] == UP


def test_missing_tangent_lines_are_not_worth_a_warning(tmp_path):
    found = pipeline.inspect(onshape_dxf(tmp_path))
    assert found.suggested["extent"] is None
    assert not [d for d in found.report.items if d.level == "warn"]
    assert "no-extent-layer" in {d.code for d in found.report.items}


def test_leftover_onshape_layers_are_not_taken_for_interior_profiles():
    layers = [
        dxfio.LayerInfo(CUT, {"LINE": 9}),
        dxfio.LayerInfo(UP, {"LINE": 2}),
        dxfio.LayerInfo("SHEETMETAL_FORM_UP", {"LINE": 4}),
    ]
    assert dxfio.suggest_mapping(layers)["interior"] is None


# -- holes on the outline's layer ------------------------------------------------


def test_the_outline_is_picked_out_from_among_the_holes(tmp_path):
    path = onshape_dxf(tmp_path, tangents=True)
    result = pipeline.process(path, suggested(path), Config())
    assert result.ok, result.report.text()
    assert "holes-on-outer-layer" in codes(result)
    assert result.surgery.area_before == pytest.approx(50.0 * 20.0)
    assert len(result.notches) == 4
    assert {item["role"] for item in result.after} == {"outer", "notch", "interior"}


def test_holes_come_out_the_other_side_untouched(tmp_path):
    path = onshape_dxf(tmp_path)
    result = pipeline.process(path, suggested(path), Config())
    out = tmp_path / "out.dxf"
    pipeline.save(result, str(out))

    msp = ezdxf.readfile(out).modelspace()
    assert {e.dxf.layer for e in msp} == {CUT}
    circles = sorted((e.dxf.center.x, e.dxf.radius) for e in msp.query("CIRCLE"))
    assert circles == [(8.0, 2.0), (44.0, 2.0)]
    slot_edges = [
        e for e in msp.query("LINE")
        if 26.0 <= min(e.dxf.start.x, e.dxf.end.x) and max(e.dxf.start.x, e.dxf.end.x) <= 30.0
        and 8.0 <= min(e.dxf.start.y, e.dxf.end.y) and max(e.dxf.start.y, e.dxf.end.y) <= 12.0
    ]
    assert len(slot_edges) == 4


def test_a_circle_is_one_closed_loop_not_two_duplicate_halves(tmp_path):
    msp = ezdxf.new("R2000").modelspace()
    a = msp.add_circle((0.0, 0.0), 3.0)
    b = msp.add_circle((10.0, 0.0), 3.0)
    curves = [c for i, e in enumerate((a, b)) for c in dxfio.curves_from_entity(e, i)]
    kept, duplicates, degenerate = drop_duplicate_curves(curves, 1e-6)
    assert len(kept) == 2 and not duplicates and not degenerate
    loops, _ = build_loops(kept, 1e-6)
    assert [lp.closed for lp in loops] == [True, True]
    assert loops[0].area() == pytest.approx(math.pi * 9.0, rel=1e-3)


def test_two_arcs_between_the_same_points_are_not_duplicates():
    """The two ends of a hole drawn as half-arcs share endpoints and length."""
    c = np.array([0.0, 0.0])
    halves = [ArcCurve(0, None, c, 3.0, 0.0, math.pi), ArcCurve(1, None, c, 3.0, math.pi, math.pi)]
    kept, duplicates, _ = drop_duplicate_curves(halves, 1e-6)
    assert len(kept) == 2 and not duplicates


def test_two_parts_side_by_side_are_still_refused(tmp_path):
    path = onshape_dxf(tmp_path, holes=False)
    doc = ezdxf.readfile(path)
    square = [(60.0, 0.0), (70.0, 0.0), (70.0, 10.0), (60.0, 10.0)]
    for i in range(4):
        doc.modelspace().add_line(square[i], square[(i + 1) % 4], dxfattribs={"layer": CUT})
    doc.saveas(path)
    result = pipeline.process(path, suggested(path), Config())
    assert not result.ok
    assert "profile-not-one-loop" in codes(result)


# -- bend zone without tangent lines ---------------------------------------------


def test_bends_on_both_layers_are_notched(tmp_path):
    path = onshape_dxf(tmp_path)
    result = pipeline.process(path, suggested(path), Config())
    assert result.ok, result.report.text()
    assert sorted({n.bend_index for n in result.notches}) == [0, 1]
    assert len(result.notches) == 4


def test_inferred_zone_gives_the_same_notches_as_real_tangent_lines(tmp_path):
    with_lines = onshape_dxf(tmp_path, tangents=True, name="with.dxf")
    without = onshape_dxf(tmp_path, tangents=False, name="without.dxf")
    real = pipeline.process(with_lines, suggested(with_lines), Config())
    inferred = pipeline.process(without, suggested(without), Config())
    assert real.ok and inferred.ok, inferred.report.text()
    assert "extents-inferred" in codes(inferred)
    assert "extents-inferred" not in codes(real)
    assert chains(inferred) == chains(real)
    # base 6 (the bend zone), height 2 (the depth), four times over
    assert inferred.surgery.area_after == pytest.approx(1000.0 - 4 * 6.0)


def test_inferred_extents_are_drawn_in_the_before_view(tmp_path):
    path = onshape_dxf(tmp_path)
    result = pipeline.process(path, suggested(path), Config())
    assert sum(item["role"] == "extent" for item in result.before) == 4


def test_a_zone_vertex_on_one_side_only_backs_a_width_seen_elsewhere(tmp_path):
    """The edge runs straight on past the zone on one side, so there is no vertex there."""
    outline = [p for p in OUTLINE if p[0] != 38.0]
    path = onshape_dxf(tmp_path, outline=outline, holes=False)
    result = pipeline.process(path, suggested(path), Config())
    assert result.ok, result.report.text()
    assert "bend-zone-assumed" not in codes(result)
    assert all(n.cut_area == pytest.approx(6.0) for n in result.notches)


def test_a_bend_with_no_zone_vertices_borrows_the_typical_width(tmp_path):
    outline = [p for p in OUTLINE if p[0] not in (32.0, 38.0)]
    path = onshape_dxf(tmp_path, outline=outline, holes=False)
    result = pipeline.process(path, suggested(path), Config())
    assert result.ok, result.report.text()
    assert "bend-zone-assumed" in codes(result)
    assert len(result.notches) == 4


def test_lone_vertices_alone_do_not_establish_a_width(tmp_path):
    """A corner near a bend end looks just like a one-sided zone vertex."""
    outline = [p for p in OUTLINE if p[0] not in (23.0, 38.0)]
    path = onshape_dxf(tmp_path, outline=outline, holes=False)
    result = pipeline.process(path, suggested(path), Config())
    assert not result.ok
    assert "no-bend-zone" in codes(result)


def test_no_zone_evidence_at_all_is_an_error_that_names_the_fix(tmp_path):
    plain = [(0.0, 0.0), (50.0, 0.0), (50.0, 20.0), (0.0, 20.0)]
    path = onshape_dxf(tmp_path, outline=plain, holes=False)
    result = pipeline.process(path, suggested(path), Config())
    assert not result.ok
    assert "no-bend-zone" in codes(result)


def test_an_explicit_bend_zone_needs_no_evidence(tmp_path):
    plain = [(0.0, 0.0), (50.0, 0.0), (50.0, 20.0), (0.0, 20.0)]
    path = onshape_dxf(tmp_path, outline=plain, holes=False)
    result = pipeline.process(path, suggested(path), Config(bend_zone=4.0))
    assert result.ok, result.report.text()
    assert all(n.cut_area == pytest.approx(0.5 * 4.0 * 2.0) for n in result.notches)


# -- the front ends ----------------------------------------------------------------


def test_cli_end_to_end_on_an_onshape_file(tmp_path, capsys):
    path = onshape_dxf(tmp_path)
    out = tmp_path / "cli.dxf"
    assert cli.main([path, "-o", str(out)]) == 0
    assert f"{DOWN} + {UP}" in capsys.readouterr().out
    assert out.exists()


def test_cli_takes_several_bend_layers(tmp_path):
    path = onshape_dxf(tmp_path)
    assert cli.main([path, "--bend", UP, "--bend", DOWN]) == 0
    assert cli.main([path, "--bend", UP]) == 0


def test_portal_accepts_a_role_spread_over_several_layers(tmp_path, monkeypatch):
    from notchgen import api

    monkeypatch.setattr(api, "DATA_DIR", tmp_path / "sessions")
    client = TestClient(api.app)
    with open(onshape_dxf(tmp_path), "rb") as handle:
        body = client.post("/api/upload", files={"file": ("tray.dxf", handle)}).json()
    assert body["suggested_mapping"]["bend"] == sorted(BENDS)

    mapping = {k: v for k, v in body["suggested_mapping"].items() if v}
    response = client.post(
        "/api/process", json={"session_id": body["session_id"], "mapping": mapping}
    )
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["ok"] and done["download_ready"]
    assert len(done["notches"]) == 4


# -- shallow notches where the edge crosses the bend zone at a slant ----------------

# The bottom edge steps 0.6 into the material across the bend zone, so the right-hand
# shoulder sits 0.6 behind the bend line's end. Onshape parts do this at most flange ends.
SLANTED = [(0.0, 0.0), (17.0, 0.0), (23.0, 0.6), (50.0, 0.6), (50.0, 20.0), (23.0, 20.0), (17.0, 20.0), (0.0, 20.0)]


def slanted_dxf(tmp_path: Path, tangents: bool, name: str) -> str:
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    for i in range(len(SLANTED)):
        msp.add_line(SLANTED[i], SLANTED[(i + 1) % len(SLANTED)], dxfattribs={"layer": CUT})
    msp.add_line((20.0, 0.0), (20.0, 20.0), dxfattribs={"layer": UP})
    if tangents:
        msp.add_line((17.0, 0.0), (17.0, 20.0), dxfattribs={"layer": TANGENT})
        msp.add_line((23.0, 0.6), (23.0, 20.0), dxfattribs={"layer": TANGENT})
    path = tmp_path / name
    doc.saveas(path)
    return str(path)


@pytest.mark.parametrize("tangents", [True, False])
def test_a_notch_shallower_than_the_setback_still_reaches_below_both_shoulders(tmp_path, tangents):
    """0.4 deep from the bend end would leave the apex outboard of the shoulder at 0.6."""
    path = slanted_dxf(tmp_path, tangents, f"slanted-{tangents}.dxf")
    result = pipeline.process(path, suggested(path), Config(depth=0.4))
    assert result.ok, result.report.text()
    assert "apex-deepened" in codes(result)
    by_end = {n.end_index: n for n in result.notches}
    assert [round(float(v), 6) for v in by_end[0].apex] == [20.0, 1.0]
    assert [round(float(v), 6) for v in by_end[1].apex] == [20.0, 19.6]
    assert chains(result)[0][2] == [(23.0, 0.6), (20.0, 1.0), (17.0, 0.0)]


def test_the_depth_is_kept_below_the_inner_shoulder_at_any_depth(tmp_path):
    """Not only when the notch would otherwise invert: 2 deep means 2 of relief everywhere."""
    path = slanted_dxf(tmp_path, True, "deep.dxf")
    result = pipeline.process(path, suggested(path), Config(depth=2.0))
    assert result.ok, result.report.text()
    by_end = {n.end_index: n for n in result.notches}
    assert [round(float(v), 6) for v in by_end[0].apex] == [20.0, 2.6]
    assert [round(float(v), 6) for v in by_end[1].apex] == [20.0, 18.0]
