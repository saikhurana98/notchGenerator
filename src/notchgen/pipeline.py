"""End-to-end: DXF in, notched DXF out, with diagnostics and drawable geometry."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import dxfio, render
from .bends import pair_bends
from .config import Config
from .curves import SplineCurve
from .loop import build_loops
from .notch import Notch, Surgery, build_all
from .report import Report


@dataclass
class Inspection:
    layers: list[dxfio.LayerInfo]
    suggested: dict[str, str | None]
    geometry: list[dict]
    bounds: dict
    report: Report

    def as_dict(self) -> dict:
        return {
            "layers": [i.as_dict() for i in self.layers],
            "suggested_mapping": self.suggested,
            "geometry": self.geometry,
            "bounds": self.bounds,
            "diagnostics": self.report.as_list(),
        }


@dataclass
class Result:
    report: Report
    notches: list[Notch] = field(default_factory=list)
    surgery: Surgery | None = None
    before: list[dict] = field(default_factory=list)
    after: list[dict] = field(default_factory=list)
    bounds: dict = field(default_factory=dict)
    doc: object | None = None
    mapping: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.surgery is not None and not self.report.has_errors

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "before": self.before,
            "after": self.after,
            "bounds": self.bounds,
            "notches": [n.as_dict() for n in self.notches],
            "diagnostics": self.report.as_list(),
            "area_before": getattr(self.surgery, "area_before", 0.0),
            "area_after": getattr(self.surgery, "area_after", 0.0),
        }


def inspect(path: str, chord_tol: float = 1e-3) -> Inspection:
    """Read a file and report what is in it, without changing anything."""
    report = Report()
    doc = dxfio.read(path)
    dxfio.check_units(doc, report)
    layers = dxfio.layer_census(doc)
    suggested = dxfio.suggest_mapping(layers)
    mapping = {k: v for k, v in suggested.items() if v}
    geometry = render.document_payload(doc, mapping, chord_tol)
    for role in dxfio.ROLES:
        if not suggested.get(role):
            report.warn(
                "unmapped-role",
                f"Could not guess which layer holds the {role} geometry; pick it manually.",
                role=role,
            )
    return Inspection(layers, suggested, geometry, render.bounds(geometry), report)


def process(path: str, mapping: dict[str, str], cfg: Config) -> Result:
    report = Report()
    doc = dxfio.read(path)
    dxfio.check_units(doc, report)

    present = {i.name for i in dxfio.layer_census(doc)}
    for role in ("outer", "bend", "extent"):
        layer = mapping.get(role)
        if not layer:
            report.error("missing-role", f"No layer chosen for the {role} geometry.", role=role)
        elif layer not in present:
            report.error(
                "unknown-layer",
                f"Layer {layer!r} chosen for {role} holds no entities in this file.",
                role=role,
                layer=layer,
            )
    result = Result(report=report, mapping=dict(mapping), doc=doc)
    result.before = render.document_payload(doc, mapping, cfg.chord_tol)
    result.bounds = render.bounds(result.before)
    if report.has_errors:
        return result

    outer_curves, _ = dxfio.collect_curves(doc, mapping["outer"], report)
    if not outer_curves:
        report.error("empty-outer", f"Layer {mapping['outer']!r} contains no profile geometry.")
        return result

    for c in outer_curves:
        if isinstance(c, SplineCurve):
            dev = c.chord_deviation_bound()
            if dev < cfg.chord_tol:
                report.info(
                    "near-straight-spline",
                    f"A spline on the outer profile strays at most {dev:.5f} from a straight "
                    f"line; treated as curved anyway, and left untouched unless removed.",
                    deviation=dev,
                )

    loops, worst_gap = build_loops(outer_curves, cfg.stitch_tol)
    closed = [lp for lp in loops if lp.closed]
    if len(loops) != 1 or not closed:
        report.error(
            "profile-not-one-loop",
            f"The outer profile stitched into {len(loops)} chain(s), of which {len(closed)} "
            f"closed, at a tolerance of {cfg.stitch_tol:g}. The worst junction gap was "
            f"{worst_gap:.3e}. A single closed outline is required.",
            loops=len(loops),
            closed=len(closed),
            worst_gap=worst_gap,
        )
        return result
    loop = closed[0]
    report.info(
        "loop-stitched",
        f"Outer profile stitched into one closed loop of {len(loop)} curves; worst junction "
        f"gap {worst_gap:.3e}.",
        curves=len(loop),
        worst_gap=worst_gap,
    )

    bends = dxfio.segments_on_layer(doc, mapping["bend"], report)
    extents = dxfio.segments_on_layer(doc, mapping["extent"], report)
    if cfg.thickness and cfg.depth < cfg.thickness:
        report.warn(
            "depth-under-thickness",
            f"A notch depth of {cfg.depth} is less than the sheet thickness "
            f"{cfg.thickness}; the relief may be too shallow to prevent tearing.",
        )

    pairs = pair_bends(bends, extents, cfg, report)
    if report.has_errors:
        return result

    notches, surgery = build_all(loop, pairs, cfg, report)
    result.notches = notches
    result.surgery = surgery
    if surgery is None:
        return result

    result.after = [
        *render.curves_payload(surgery.kept, "outer", cfg.chord_tol),
        *render.curves_payload(surgery.added, "notch", cfg.chord_tol),
        *[item for item in result.before if item["role"] == "interior"],
    ]
    report.info(
        "notches-built",
        f"Cut {len(notches)} notch(es); area went from {surgery.area_before:.3f} to "
        f"{surgery.area_after:.3f} square units.",
        count=len(notches),
    )
    return result


def save(result: Result, out_path: str) -> None:
    if result.surgery is None:
        raise ValueError("nothing to save: processing did not produce a result")
    dxfio.write_result(
        result.doc,
        result.mapping,
        result.surgery.kept,
        result.surgery.added,
        out_path,
        result.report,
    )
