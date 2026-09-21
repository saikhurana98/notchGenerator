"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import dxfio, pipeline
from .config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notchgen",
        description="Add bend-relief notches to a sheet-metal flat-pattern DXF.",
    )
    p.add_argument(
        "input", nargs="+", help="flat-pattern DXF(s) exported from Fusion 360 or Onshape"
    )
    p.add_argument("-o", "--output", help="where to write the notched DXF (one input only)")
    p.add_argument(
        "-O",
        "--out-dir",
        help="write '<name> with notch.dxf' into this directory, for any number of inputs",
    )
    p.add_argument("--inspect", action="store_true", help="list layers and exit")

    g = p.add_argument_group("layer roles (auto-detected when omitted)")
    g.add_argument("--outer", help="layer holding the outer profile")
    g.add_argument("--interior", help="layer holding the interior profiles")
    g.add_argument(
        "--bend",
        action="append",
        help="layer holding the bend lines; repeat for several (Onshape splits them UP/DOWN)",
    )
    g.add_argument(
        "--extent",
        help="layer holding the bend extent (tangent) lines; when there is none the bend "
        "zone is read off the outline",
    )
    g.add_argument(
        "--bend-zone",
        type=float,
        help="bend zone width to use when there is no extent layer, instead of reading it "
        "off the outline",
    )

    g = p.add_argument_group("notch shape")
    g.add_argument("--depth", type=float, default=2.0, help="notch depth (default 2.0)")
    g.add_argument(
        "--depth-from",
        choices=("bend-end", "edge"),
        default="bend-end",
        help="measure depth from the bend line end (default) or from the material edge",
    )
    g.add_argument(
        "--shape",
        choices=("v", "rect"),
        default="v",
        help="v-shaped notch (default) or flat-bottomed relief",
    )
    g.add_argument("--thickness", type=float, help="sheet thickness, for a depth sanity check")

    g = p.add_argument_group("tolerances")
    for name, default, help_text in (
        ("stitch-tol", 1e-6, "max gap when stitching the profile into a loop"),
        ("snap-tol", 0.02, "snapping a ray hit onto an existing profile vertex"),
        ("sliver-tol", 0.05, "shortest fragment worth emitting"),
        ("chord-tol", 1e-3, "sagitta when flattening curves"),
        ("bridge-tol", 0.01, "close an outline left open by less than this"),
        ("angle-tol", 1e-3, "parallelism test between bend and extent lines"),
        ("max-stub", 1.0, "furthest an extent end may sit from the material edge"),
    ):
        g.add_argument(f"--{name}", type=float, default=default, help=help_text)
    g.add_argument(
        "--merge-overlapping",
        action="store_true",
        help="allow notches whose cuts overlap instead of failing",
    )

    g = p.add_argument_group("output")
    g.add_argument(
        "--multi-layer",
        action="store_true",
        help="keep the outer and interior profiles on separate layers (default: single layer)",
    )
    return p


def _shown(layers) -> str:
    if not layers:
        return "(none)"
    return layers if isinstance(layers, str) else " + ".join(layers)


def _inspect(path: str) -> int:
    found = pipeline.inspect(path)
    print(f"{path}\n")
    print(f"{'layer':28s} {'entities':>8s}  contents")
    for info in found.layers:
        contents = ", ".join(f"{n}x{t}" for t, n in sorted(info.counts.items()))
        print(f"{info.name:28s} {info.total:8d}  {contents}")
    print("\nsuggested mapping:")
    for role in dxfio.ROLES:
        print(f"  {role:9s} -> {_shown(found.suggested.get(role))}")
    if found.report.items:
        print("\n" + found.report.text())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.inspect:
        for i, path in enumerate(args.input):
            if i:
                print()
            _inspect(path)
        return 0

    if args.output and len(args.input) > 1:
        parser.error("-o/--output takes a single input; use -O/--out-dir for several")
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    worst = 0
    for i, path in enumerate(args.input):
        if i:
            print("\n" + "-" * 72 + "\n")
        if len(args.input) > 1:
            print(f"{path}\n")
        destination = args.output
        if out_dir:
            destination = str(out_dir / f"{Path(path).stem} with notch.dxf")
        worst = max(worst, _one(path, destination, args))
    return worst


def _one(path: str, destination: str | None, args) -> int:
    found = pipeline.inspect(path)
    mapping = dict(found.suggested)
    for role in dxfio.ROLES:
        override = getattr(args, role, None)
        if override:
            mapping[role] = override[0] if isinstance(override, list) and len(override) == 1 else override
    mapping = {k: v for k, v in mapping.items() if v}

    cfg = Config(
        depth=args.depth,
        depth_from=args.depth_from,
        shape=args.shape,
        stitch_tol=args.stitch_tol,
        snap_tol=args.snap_tol,
        sliver_tol=args.sliver_tol,
        chord_tol=args.chord_tol,
        bridge_tol=args.bridge_tol,
        angle_tol=args.angle_tol,
        max_stub=args.max_stub,
        thickness=args.thickness,
        bend_zone=args.bend_zone,
        merge_overlapping=args.merge_overlapping,
    )

    print("layer mapping:")
    for role in dxfio.ROLES:
        print(f"  {role:9s} -> {_shown(mapping.get(role))}")
    print()

    result = pipeline.process(path, mapping, cfg)
    print(result.report.text())

    if not result.ok:
        print("\nno output written.", file=sys.stderr)
        return 1

    print(f"\n{len(result.notches)} notch(es):")
    for n in result.notches:
        print(
            f"  bend {n.bend_index} end {n.end_index}: apex "
            f"({n.apex[0]:.4f}, {n.apex[1]:.4f}), removed {n.cut_area:.4f} square units"
        )

    if destination:
        pipeline.save(result, destination, single_layer=not args.multi_layer)
        print(f"\nwrote {destination}")
    else:
        print("\nno --output/--out-dir given; nothing written.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
