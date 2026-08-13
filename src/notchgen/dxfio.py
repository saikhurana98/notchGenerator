"""Reading and writing DXF. The only module that touches ezdxf documents directly.

Editing happens in the loaded document rather than in a fresh one, so every untouched
entity keeps its exact original representation — splines in particular are never
re-interpolated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf
import numpy as np

from .curves import ArcCurve, Curve, LineCurve, PolylineCurve, SplineCurve, UnsupportedEntity, curves_from_entity
from .report import Report

ROLES = ("outer", "interior", "bend", "extent")

_PATTERNS = {
    "outer": (r"^outer", r"outer.*profile", r"^out\b", r"profile"),
    "interior": (r"^interior", r"^inner", r"interior.*profile", r"^hole"),
    "bend": (r"^bend$", r"^bend[_\- ]?lines?$", r"^bends$"),
    "extent": (r"extent", r"bend.*zone", r"tangent"),
}

# Most specific first: "BEND_EXTENT" must be claimed before anything reaches for "BEND",
# and "INTERIOR_PROFILES" before the loose "profile" pattern that finds the outer layer.
_MATCH_ORDER = ("extent", "bend", "interior", "outer")

CURVE_TYPES = ("LINE", "SPLINE", "ARC", "ELLIPSE", "LWPOLYLINE", "POLYLINE", "CIRCLE")

_MM_INSUNITS = {0, 4}  # 0 = unitless, 4 = millimetres


@dataclass
class LayerInfo:
    name: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def as_dict(self) -> dict:
        return {"name": self.name, "counts": self.counts, "total": self.total}


def layer_census(doc) -> list[LayerInfo]:
    layers: dict[str, LayerInfo] = {}
    for e in doc.modelspace():
        info = layers.setdefault(e.dxf.layer, LayerInfo(e.dxf.layer))
        info.counts[e.dxftype()] = info.counts.get(e.dxftype(), 0) + 1
    return sorted(layers.values(), key=lambda i: i.name)


def suggest_mapping(layers: list[LayerInfo]) -> dict[str, str | None]:
    """Guess which layer plays which role, by name first and by structure afterwards."""
    names = [i.name for i in layers]
    out: dict[str, str | None] = {r: None for r in ROLES}
    taken: set[str] = set()
    for role in _MATCH_ORDER:
        for pattern in _PATTERNS[role]:
            for name in names:
                if name in taken:
                    continue
                if re.search(pattern, name, re.IGNORECASE):
                    out[role] = name
                    taken.add(name)
                    break
            if out[role]:
                break

    # Structure, when the names give nothing away: every bend has exactly two extent
    # lines, so the extent layer holds twice the lines of the bend layer.
    if out["bend"] is None or out["extent"] is None:
        line_only = sorted(
            (i for i in layers if i.name not in taken and set(i.counts) == {"LINE"}),
            key=lambda i: i.total,
        )
        if len(line_only) >= 2 and line_only[1].total == 2 * line_only[0].total:
            for role, info in (("bend", line_only[0]), ("extent", line_only[1])):
                if out[role] is None:
                    out[role] = info.name
                    taken.add(info.name)

    # The outer profile is the biggest thing left; interior profiles are whatever remains.
    leftovers = sorted(
        (i for i in layers if i.name not in taken), key=lambda i: i.total, reverse=True
    )
    if out["outer"] is None and leftovers:
        out["outer"] = leftovers[0].name
        taken.add(leftovers[0].name)
        leftovers = leftovers[1:]
    if out["interior"] is None and leftovers:
        out["interior"] = leftovers[0].name
    return out


def read(path: str):
    return ezdxf.readfile(path)


def check_units(doc, report: Report) -> None:
    insunits = doc.header.get("$INSUNITS", 0)
    if insunits not in _MM_INSUNITS:
        report.warn(
            "units",
            f"$INSUNITS is {insunits}, not millimetres. Depth and tolerances are "
            f"interpreted in the file's own units.",
            insunits=insunits,
        )


def collect_curves(doc, layer: str, report: Report) -> tuple[list[Curve], dict[int, object]]:
    """Curves for one layer, plus the eid -> source entity map used when writing back."""
    curves: list[Curve] = []
    sources: dict[int, object] = {}
    for eid, e in enumerate(doc.modelspace()):
        if e.dxf.layer != layer:
            continue
        sources[eid] = e
        if e.dxftype() not in CURVE_TYPES:
            report.warn(
                "unsupported-entity",
                f"Layer {layer!r} contains a {e.dxftype()}, which carries no profile "
                f"geometry this tool understands; ignored.",
                layer=layer,
                dxftype=e.dxftype(),
            )
            continue
        try:
            pieces = curves_from_entity(e, eid)
        except UnsupportedEntity as exc:
            report.error(
                "unsupported-entity",
                f"Layer {layer!r} contains an unsupported {exc} entity.",
                layer=layer,
                dxftype=str(exc),
            )
            continue
        if len(pieces) > 1 or (pieces and not pieces[0].is_whole):
            report.info(
                "exploded-entity",
                f"A {e.dxftype()} on layer {layer!r} was expanded into "
                f"{len(pieces)} straight and circular pieces; the output will contain those "
                f"pieces rather than the original entity.",
                layer=layer,
                dxftype=e.dxftype(),
            )
        curves.extend(pieces)
    return curves, sources


def segments_on_layer(doc, layer: str, report: Report):
    """LINE entities on a layer, as (start, end) pairs. Bend and extent layers only."""
    from .bends import Segment

    out = []
    for e in doc.modelspace():
        if e.dxf.layer != layer:
            continue
        if e.dxftype() != "LINE":
            report.warn(
                "non-line-on-bend-layer",
                f"Layer {layer!r} contains a {e.dxftype()}; only LINE entities are used here.",
                layer=layer,
                dxftype=e.dxftype(),
            )
            continue
        out.append(
            Segment(
                np.array([e.dxf.start.x, e.dxf.start.y]),
                np.array([e.dxf.end.x, e.dxf.end.y]),
            )
        )
    return out


def _add_curve(msp, curve: Curve, layer: str) -> None:
    attribs = {"layer": layer}
    if isinstance(curve, LineCurve):
        msp.add_line(tuple(curve.a), tuple(curve.b), dxfattribs=attribs)
    elif isinstance(curve, SplineCurve):
        spline = msp.add_spline(dxfattribs=attribs)
        spline.apply_construction_tool(curve.bs)
    elif isinstance(curve, ArcCurve):
        import math

        start = math.degrees(curve.start_angle) % 360
        end = math.degrees(curve.start_angle + curve.sweep) % 360
        if curve.sweep < 0:
            start, end = end, start
        msp.add_arc(tuple(curve.center), curve.radius, start, end, dxfattribs=attribs)
    elif isinstance(curve, PolylineCurve):
        msp.add_lwpolyline([tuple(p) for p in curve.pts], dxfattribs=attribs)
    else:  # pragma: no cover - every Curve subclass is handled above
        raise TypeError(f"cannot write {type(curve).__name__}")


def write_result(
    doc,
    mapping: dict[str, str],
    kept: list[Curve],
    added: list[Curve],
    out_path: str,
    report: Report,
) -> None:
    """Rewrite the document in place to hold only the notched outer profile and the holes."""
    msp = doc.modelspace()
    outer_layer = mapping["outer"]
    interior_layer = mapping.get("interior")
    survivors = {c.eid for c in kept if c.is_whole}

    doomed = []
    for eid, e in enumerate(msp):
        layer = e.dxf.layer
        if layer == outer_layer:
            if eid not in survivors:
                doomed.append(e)
        elif interior_layer and layer == interior_layer:
            continue
        else:
            doomed.append(e)
    for e in doomed:
        msp.delete_entity(e)

    for curve in kept:
        if not curve.is_whole:
            _add_curve(msp, curve, outer_layer)
    for curve in added:
        _add_curve(msp, curve, outer_layer)

    for role, layer in (("outer", outer_layer), ("interior", interior_layer)):
        if layer and layer not in doc.layers:
            doc.layers.add(layer, color=7 if role == "outer" else 5)
    for layer in (mapping.get("bend"), mapping.get("extent")):
        if layer and layer in doc.layers:
            doc.layers.remove(layer)

    doc.saveas(out_path)
    # Only the basename goes into the report — the full path is a server detail that has no
    # business being shown in the browser.
    report.info(
        "written",
        f"Wrote {Path(out_path).name}: {len(list(msp))} entities on "
        f"{len({e.dxf.layer for e in msp})} layer(s).",
    )
