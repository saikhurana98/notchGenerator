"""Diagnostics. Every rejected or suspicious case gets a code, so the UI can group them."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Diagnostic:
    level: str  # "error" | "warn" | "info"
    code: str
    message: str
    context: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "context": {k: _plain(v) for k, v in self.context.items()},
        }


def _plain(v):
    try:
        import numpy as np

        if isinstance(v, np.ndarray):
            return [round(float(x), 6) for x in v.ravel()]
        if isinstance(v, (np.floating, np.integer)):
            return float(v)
    except ImportError:
        pass
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


class Report:
    def __init__(self) -> None:
        self.items: list[Diagnostic] = []

    def error(self, code: str, message: str, **ctx) -> None:
        self.items.append(Diagnostic("error", code, message, ctx))

    def warn(self, code: str, message: str, **ctx) -> None:
        self.items.append(Diagnostic("warn", code, message, ctx))

    def info(self, code: str, message: str, **ctx) -> None:
        self.items.append(Diagnostic("info", code, message, ctx))

    @property
    def errors(self) -> list[Diagnostic]:
        return [d for d in self.items if d.level == "error"]

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    def as_list(self) -> list[dict]:
        return [d.as_dict() for d in self.items]

    def text(self) -> str:
        if not self.items:
            return "no diagnostics"
        return "\n".join(f"[{d.level.upper():5s}] {d.code}: {d.message}" for d in self.items)
