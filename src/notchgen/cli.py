"""Command line entry point."""

from __future__ import annotations

import argparse
import sys

from . import dxfio, pipeline
from .config import Config


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notchgen",
        description="Add bend-relief notches to a sheet-metal flat-pattern DXF.",
    )
    p.add_argument("input", help="DXF exported from Fusion 360")
    p.add_argument("-o", "--output", help="where to write the notched DXF")
    p.add_argument("--inspect", action="store_true", help="list layers and exit")

    g = p.add_argument_group("layer roles (auto-detected when omitted)")
    g.add_argument("--outer", help="layer holding the outer profile")
    g.add_argument("--interior", help="layer holding the interior profiles")
    g.add_argument("--bend", help="layer holding the bend lines")
    g.add_argument("--extent", help="layer holding the bend extent lines")

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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.inspect:
        found = pipeline.inspect(args.input)
        print(f"{args.input}\n")
        print(f"{'layer':24s} {'entities':>8s}  contents")
        for info in found.layers:
            contents = ", ".join(f"{n}x{t}" for t, n in sorted(info.counts.items()))
            print(f"{info.name:24s} {info.total:8d}  {contents}")
        print("\nsuggested mapping:")
        for role in dxfio.ROLES:
            print(f"  {role:9s} -> {found.suggested.get(role) or '(none)'}")
        if found.report.items:
            print("\n" + found.report.text())
        return 0

    found = pipeline.inspect(args.input)
    mapping = dict(found.suggested)
    for role in dxfio.ROLES:
        override = getattr(args, role, None)
        if override:
            mapping[role] = override
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
        merge_overlapping=args.merge_overlapping,
    )

    print("layer mapping:")
    for role in dxfio.ROLES:
        print(f"  {role:9s} -> {mapping.get(role) or '(none)'}")
    print()

    result = pipeline.process(args.input, mapping, cfg)
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

    if args.output:
        pipeline.save(result, args.output)
        print(f"\nwrote {args.output}")
    else:
        print("\nno --output given; nothing written.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
