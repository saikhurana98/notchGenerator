"""End-to-end: DXF in, notched DXF out, with diagnostics and drawable geometry."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import dxfio, render
from .bends import pair_bends
from .config import Config
from .geom import cross2, dot2, norm
from .curves import LineCurve, SplineCurve
from .loop import build_loops, drop_duplicate_curves
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


def _contaminating_lines(outer_curves, segments, tol: float = 1e-6) -> int:
    """Outer-profile curves that lie along a bend or extent line.

    A bend or extent line sitting on the outer layer dead-ends in the middle of the part, so
    the trace breaks into pieces there. Worth naming, because the fix is a layer change
    rather than anything to do with tolerances.
    """
    if not segments:
        return 0
    hits = 0
    for curve in outer_curves:
        if not isinstance(curve, LineCurve):
            continue
        a, b = curve.start(), curve.end()
        for seg in segments:
            direction = seg.b - seg.a
            length = norm(direction)
            if length == 0:
                continue
            u = direction / length
            # Same infinite line, and overlapping it rather than merely pointing along it.
            if abs(cross2(u, a - seg.a)) > tol or abs(cross2(u, b - seg.a)) > tol:
                continue
            ta, tb = dot2(a - seg.a, u), dot2(b - seg.a, u)
            if min(ta, tb) < length + tol and max(ta, tb) > -tol:
                hits += 1
                break
    return hits


def _contamination_hint(outer_curves, bends, extents) -> str:
    count = _contaminating_lines(outer_curves, [*bends, *extents])
    if not count:
        return ""
    return (
        f" The outer layer also holds {count} line(s) lying along your bend or bend-extent "
        f"lines. Those belong on their own layers only — on the outer layer they dead-end "
        f"inside the part and break the outline into pieces."
    )


# A gap this small is a CAD rounding artefact, not a modelling mistake: it is two orders of
# magnitude below a laser kerf, so closing it silently is the honest thing to do. Anything
# larger gets a warning, because at that point it might be a real sketch problem.
NEGLIGIBLE_GAP = 1e-3


def _report_gap(report: Report, gap: float, cfg: Config) -> None:
    if gap <= NEGLIGIBLE_GAP:
        report.info(
            "profile-gap-closed",
            f"The outer profile had a {gap:.3g} gap in it, which is rounding noise from CAD and "
            f"far below any cutting tolerance; closed silently.",
            gap=gap,
        )
        return
    report.warn(
        "profile-gap-bridged",
        f"The outer profile had a {gap:.4g} gap in it. That is under the {cfg.bridge_tol:g} "
        f"bridging limit so it was closed, but it is large enough to be worth a look at the "
        f"sketch.",
        gap=gap,
    )


def _skipped_hint(report: Report) -> str:
    """A skipped entity leaves a hole in the profile, which looks exactly like a sketch gap."""
    skipped = [d for d in report.items if d.code == "unsupported-entity"]
    if not skipped:
        return ""
    kinds = sorted({str(d.context.get("dxftype", "?")) for d in skipped})
    return (
        f" Note that {len(skipped)} entity/entities of type {', '.join(kinds)} on this layer "
        f"were skipped, which would leave exactly this kind of hole in the outline."
    )


def _stitch_outer(outer_curves, cfg: Config, report: Report, bends=(), extents=()):
    """Turn the outer-profile curves into one closed loop, or explain why we cannot."""
    curves, duplicates, degenerate = drop_duplicate_curves(
        outer_curves, cfg.stitch_tol, cfg.chord_tol
    )
    if degenerate:
        report.warn(
            "degenerate-profile-entity",
            f"Ignored {len(degenerate)} zero-length entity/entities on the outer profile.",
            count=len(degenerate),
        )
    if duplicates:
        report.warn(
            "duplicate-profile-entity",
            f"Ignored {len(duplicates)} duplicated edge(s) on the outer profile. A profile "
            f"carrying the same edge twice cannot be traced into a single outline.",
            count=len(duplicates),
        )

    # CAD rounding can leave a gap anywhere, not only at the seam where the outline closes.
    # Try the strict tolerance first, then once more at the bridging limit, so a rounding-scale
    # gap between two curves in the middle of the chain is tolerated the same way.
    attempts = [cfg.stitch_tol]
    if cfg.bridge_tol > cfg.stitch_tol:
        attempts.append(cfg.bridge_tol)
    loops, worst_gap, used_tol = [], 0.0, cfg.stitch_tol
    for tol in attempts:
        loops, worst_gap = build_loops(curves, tol, cfg.bridge_tol)
        used_tol = tol
        if len(loops) == 1 and loops[0].closed:
            break
    closed = [lp for lp in loops if lp.closed]

    if len(closed) == 1 and len(loops) == 1:
        loop = closed[0]
        slack = max(loop.bridged, worst_gap if used_tol > cfg.stitch_tol else 0.0)
        if slack:
            _report_gap(report, slack, cfg)
        report.info(
            "loop-stitched",
            f"Outer profile stitched into one closed loop of {len(loop)} curves; worst junction "
            f"gap {worst_gap:.3e}.",
            curves=len(loop),
            worst_gap=worst_gap,
        )
        return loop

    # Failure. Report the number that actually explains it — the closure gap and where the
    # loose ends are — rather than the junction gap, which is usually perfect.
    if len(loops) == 1 and not closed:
        chain = loops[0]
        start, end = chain.ends()
        report.error(
            "profile-not-closed",
            f"The outer profile traces a single open chain of {len(chain)} curves: every "
            f"junction matched (worst {worst_gap:.3e}) but the two ends are {chain.close_gap:.4g} "
            f"apart, at ({start[0]:.4f}, {start[1]:.4f}) and ({end[0]:.4f}, {end[1]:.4f}). "
            f"Close the sketch at that point, or raise the bridging limit above "
            f"{chain.close_gap:.4g} if the gap is not real."
            + _skipped_hint(report)
            + _contamination_hint(outer_curves, bends, extents),
            close_gap=chain.close_gap,
            worst_gap=worst_gap,
            open_at=[start, end],
            curves=len(chain),
        )
        return None

    detail = ", ".join(
        f"{len(lp)} curve(s) {'closed' if lp.closed else f'open by {lp.close_gap:.4g}'}"
        for lp in loops
    )
    report.error(
        "profile-not-one-loop",
        f"The outer profile stitched into {len(loops)} separate chains ({detail}) at a "
        f"tolerance of {cfg.stitch_tol:g}. Exactly one closed outline is required — check "
        f"whether the layer also holds interior geometry or a second part."
        + _skipped_hint(report)
        + _contamination_hint(outer_curves, bends, extents),
        loops=len(loops),
        closed=len(closed),
        worst_gap=worst_gap,
    )
    return None


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

    bends = dxfio.segments_on_layer(doc, mapping["bend"], report)
    extents = dxfio.segments_on_layer(doc, mapping["extent"], report)

    loop = _stitch_outer(outer_curves, cfg, report, bends, extents)
    if loop is None:
        return result
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


def save(result: Result, out_path: str, single_layer: bool = True) -> None:
    if result.surgery is None:
        raise ValueError("nothing to save: processing did not produce a result")
    dxfio.write_result(
        result.doc,
        result.mapping,
        result.surgery.kept,
        result.surgery.added,
        out_path,
        result.report,
        single_layer=single_layer,
    )
