# notchgen

Adds bend-relief notches to sheet-metal flat-pattern DXF files exported from Fusion 360.

A Fusion flat-pattern export contains the outer profile, interior profiles, bend lines, and bend
extent lines — but no relief cuts where a bend meets an outer edge. Without those cuts the material
tears or deforms at the bend. `notchgen` finds every bend line, pairs it with its two extent lines,
and cuts a notch into the outer profile at each end of the bend. The result is a cut-ready DXF
containing only the outer and interior profiles.

There is a browser portal with a before/after view, and a CLI for scripting.

## Quick start

```sh
./install.sh
```

That checks for Docker, starts the daemon if it is not running, builds the image, brings the service
up, and waits until it answers. Then open <http://localhost:8000>, drop in a DXF, confirm the layer
mapping, and download the result.

Anything needing root is announced before it runs, and nothing is enabled at boot unless you ask:

```sh
./install.sh --port 9000        # serve somewhere else
./install.sh --enable-docker    # also start Docker on boot
./install.sh --foreground       # stay attached and stream logs
./install.sh --rebuild          # ignore the build cache
./install.sh --local            # skip Docker: uv + uvicorn directly
```

If you would rather drive Compose yourself:

```sh
docker compose up --build
```

## Quick start (CLI)

```sh
uv sync
uv run notchgen "samples/front_suspension - 1 (2.5 mm).dxf" -o out.dxf
```

Inspect a file without changing it:

```sh
uv run notchgen part.dxf --inspect
```

Run the portal locally:

```sh
uv run uvicorn notchgen.api:app --reload
```

## How it works

For each bend line, and for each of its two endpoints:

1. **Pair the extents.** Keep `BEND_EXTENT` lines that are parallel to the bend *and* overlap it
   along its own direction, give each extent to whichever bend it is nearest, then require the two
   offsets to match — a bend line always sits mid-zone. Nearest-parallel-on-each-side alone is not
   enough: two bends 4 mm apart with 2.83 mm bend zones have interleaved extent lines, and one bend
   would silently steal the other's.
2. **Place the apex** `depth` mm inward from the bend endpoint, along the bend line.
3. **Take the inboard end** of each paired extent line. Those two points plus the apex are the notch.
4. **Find the edge.** Shoot a ray outward from each extent end. Profile *vertices* are tested first,
   because in a Fusion export an extent line's coordinate typically matches a profile vertex to about
   1e-13 — a curve-versus-line root find at that spot returns zero or two roots depending on
   rounding. Curve interiors are only consulted when no vertex lines up, and a crossing landing within
   `snap-tol` of a vertex is pulled onto it rather than splitting off a 0.005-long sliver.
5. **Cut.** Delete the stretch of profile the notch spans and splice the notch in. All notches are
   resolved against the untouched loop and applied in one pass, so the result does not depend on
   notch order.

Untouched entities are copied through unchanged, so surviving splines keep their exact original
control points. Only an entity the cut actually crosses is ever split.

Bend and bend-extent layers are dropped from the output. The download is named after the file you
uploaded, with ` with notch` appended — `bracket.dxf` comes back as `bracket with notch.dxf`.

### Object coordinate systems

`ARC`, `CIRCLE` and `LWPOLYLINE` store their geometry relative to the entity's extrusion vector
rather than in world coordinates, and Fusion writes an extrusion of `(0,0,-1)` whenever the flat
pattern comes off the far face of the sheet. In that coordinate system the x axis is mirrored, so
reading a centre point straight off the entity puts the hole on the wrong side of the part. `LINE`
and `SPLINE` have no such system, which is why getting this wrong leaves the outer profile correctly
placed while every hole jumps. notchgen undoes the mirror exactly, keeping arcs as arcs, and falls
back to world-coordinate flattening for the rare entity that is not in the XY plane at all. Block
references are expanded through their placement transform.

### Notch shape

| | |
| --- | --- |
| `--shape v` (default) | A single apex: two legs meeting at a point. |
| `--shape rect` | Flat bottomed: a relief of exactly bend-zone width. |

`--depth-from bend-end` (default) measures the depth from the bend line's endpoint, which is how the
tool was specified. Note that Fusion stops the bend line slightly short of the edge, so the relief
ends up marginally *deeper* than the number given — on the sample, a 2 mm depth yields 2.05–2.21 mm
of actual relief. Use `--depth-from edge` to get exactly `depth` mm of material removed regardless of
where the bend line stops.

## Layer mapping

Layer names are auto-detected — `OUTER*`/`*PROFILE*`, `INTERIOR*`/`INNER*`, `BEND`, `*EXTENT*` — and,
when the names give nothing away, by structure: the extent layer holds twice the lines of the bend
layer, and the outer profile is the largest layer left. Override with `--outer`, `--interior`,
`--bend`, `--extent`, or with the dropdowns in the portal.

Note that Fusion writes these layers *implicitly* — only layer `0` appears in the file's LAYER table.
`notchgen` creates proper layer records on the way out.

## Tolerances

| Flag | Default | Absorbs |
| --- | --- | --- |
| `--stitch-tol` | 1e-6 | gaps between adjacent outer-profile entity endpoints |
| `--snap-tol` | 0.02 | a ray hit snapping onto an existing profile vertex |
| `--sliver-tol` | 0.05 | shortest fragment worth emitting |
| `--chord-tol` | 1e-3 | sagitta when flattening curves for hit-testing and display |
| `--max-stub` | 1.0 | furthest an extent end may sit from the material edge |

These are calibrated against the real numbers in a Fusion export rather than picked round: junction
gaps come out around 4e-10, so `stitch-tol` has to be tiny; and `snap-tol` has to clear the 0.005
corner-fillet miss while staying well under the smallest genuine profile feature, which on the
sample is 0.079 long.

## What it refuses to do

Anything it cannot do safely is reported rather than guessed at, with a diagnostic code shown in the
portal. Among them: a bend with only one extent, or with asymmetric extents; a bend end that does not
reach a free edge (that needs a different relief topology and is left alone); a depth larger than half
the bend; two notches whose cuts overlap; an outer profile that does not stitch into one closed loop;
a notch edge crossing the profile somewhere other than its own anchors; and a rebuilt outline whose
area does not match the notches that were cut from it.

## Layout

```
src/notchgen/
  geom.py       2D vector and polygon helpers, no DXF knowledge
  curves.py     LINE/SPLINE/ARC/ELLIPSE/LWPOLYLINE behind one parametrised interface
  loop.py       stitching curves into a closed loop, and cutting intervals out of it
  bends.py      pairing bends with extents, and validating the pairing
  notch.py      notch construction, arc selection, profile surgery
  dxfio.py      the only module that touches an ezdxf document
  pipeline.py   inspect / process / save
  render.py     flattening geometry to polylines for the browser
  api.py        FastAPI routes and the session store
  cli.py        command line entry point
web/            the portal: one page, no build step, no dependencies
tests/          64 tests, including regressions pinned to the sample file
```

## Development

```sh
uv run pytest
```

The tests pin the sample file's expected outcome — four notches, a known set of consumed entities, the
exact resolved anchor coordinates, and cut areas that add up to the area actually lost. The expected
values were derived independently of this implementation, by evaluating the file's B-splines with
De Boor's algorithm and ray-casting the result.
