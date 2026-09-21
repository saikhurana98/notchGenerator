"""Flatten geometry into plain polylines for the browser to draw as SVG."""

from __future__ import annotations

import numpy as np

from .curves import Curve, curves_from_entity, UnsupportedEntity
from .report import Report


def polyline(pts: np.ndarray, decimals: int = 4) -> list[list[float]]:
    return [[round(float(x), decimals), round(float(y), decimals)] for x, y in pts]


def curves_payload(
    curves: list[Curve], role: str, chord_tol: float, layer: str | None = None
) -> list[dict]:
    out = []
    for c in curves:
        pts = c.flatten(chord_tol)
        if len(pts) < 2:
            continue
        item = {"role": role, "pts": polyline(pts)}
        if layer is not None:
            item["layer"] = layer
        out.append(item)
    return out


def roles_of(mapping: dict) -> dict[str, str]:
    """Layer name -> role. A role may name several layers, and a layer may fill only one."""
    out: dict[str, str] = {}
    for role, layers in (mapping or {}).items():
        for layer in [layers] if isinstance(layers, str) else layers or []:
            # First role wins, so a layer shared by outer and interior draws as the outline.
            out.setdefault(layer, role)
    return out


def document_payload(
    doc, mapping: dict, chord_tol: float, include_unmapped: bool = False
) -> list[dict]:
    """The source document flattened, each piece tagged with its layer and its role.

    `include_unmapped` keeps layers no role points at, with an empty role. The portal wants
    those: it draws every layer so that picking a different one for a role is an instant
    recolour rather than another round trip.
    """
    role_of = roles_of(mapping)
    out: list[dict] = []
    sink = Report()
    for eid, e in enumerate(doc.modelspace()):
        layer = e.dxf.layer
        role = role_of.get(layer)
        if role is None and not include_unmapped:
            continue
        try:
            pieces = curves_from_entity(e, eid)
        except UnsupportedEntity:
            continue
        out.extend(curves_payload(pieces, role or "", chord_tol, layer=layer))
    _ = sink
    return out


def bounds(payload: list[dict]) -> dict:
    pts = [p for item in payload for p in item["pts"]]
    if not pts:
        return {"min": [0.0, 0.0], "max": [1.0, 1.0]}
    arr = np.asarray(pts, dtype=float)
    lo, hi = arr.min(axis=0), arr.max(axis=0)
    return {"min": [float(lo[0]), float(lo[1])], "max": [float(hi[0]), float(hi[1])]}
