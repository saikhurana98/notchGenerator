"""Flatten geometry into plain polylines for the browser to draw as SVG."""

from __future__ import annotations

import numpy as np

from .curves import Curve, curves_from_entity, UnsupportedEntity
from .report import Report


def polyline(pts: np.ndarray, decimals: int = 4) -> list[list[float]]:
    return [[round(float(x), decimals), round(float(y), decimals)] for x, y in pts]


def curves_payload(curves: list[Curve], role: str, chord_tol: float) -> list[dict]:
    return [
        {"role": role, "pts": polyline(c.flatten(chord_tol))}
        for c in curves
        if len(c.flatten(chord_tol)) >= 2
    ]


def document_payload(doc, mapping: dict[str, str | None], chord_tol: float) -> list[dict]:
    """Every mapped layer in the source document, flattened and tagged with its role."""
    role_of = {layer: role for role, layer in mapping.items() if layer}
    out: list[dict] = []
    sink = Report()
    for eid, e in enumerate(doc.modelspace()):
        role = role_of.get(e.dxf.layer)
        if role is None:
            continue
        try:
            pieces = curves_from_entity(e, eid)
        except UnsupportedEntity:
            continue
        out.extend(curves_payload(pieces, role, chord_tol))
    _ = sink
    return out


def bounds(payload: list[dict]) -> dict:
    pts = [p for item in payload for p in item["pts"]]
    if not pts:
        return {"min": [0.0, 0.0], "max": [1.0, 1.0]}
    arr = np.asarray(pts, dtype=float)
    lo, hi = arr.min(axis=0), arr.max(axis=0)
    return {"min": [float(lo[0]), float(lo[1])], "max": [float(hi[0]), float(hi[1])]}
