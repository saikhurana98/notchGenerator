# notchgen

Adds bend-relief notches to sheet-metal flat-pattern DXF files exported from Fusion 360 or
Onshape.

A Fusion flat-pattern export contains the outer profile, interior profiles, bend lines, and bend
extent lines — but no relief cuts where a bend meets an outer edge. Without those cuts the material
tears or deforms at the bend. `notchgen` finds every bend line, pairs it with its two extent lines,
and cuts a notch into the outer profile at each end of the bend. The result is a cut-ready DXF
containing only the outer and interior profiles.

An Onshape export is laid out differently — holes on the same layer as the outline, bend lines
split over two layers, tangent lines only when asked for — and is handled without any extra setup;
see [Onshape exports](#onshape-exports).

There is a browser portal with a before/after view, and a CLI for scripting. Both take any
number of files at once.

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

## Quick start (Windows, no Docker)

```powershell
irm https://raw.githubusercontent.com/SanchakGarg/notchGenerator/main/install.ps1 | iex
```

That clones the repo, installs `uv` if needed, syncs the virtualenv, registers `notchgen` and
`notchgen-web` as commands on your `PATH`, then starts the portal and opens
<http://localhost:8000> in your browser. No Docker involved.

Options:

```powershell
./install.ps1 -Port 9000        # serve somewhere else
./install.ps1 -NoBrowser        # don't open a browser tab
```

Once installed, start the portal again any time from a new terminal with:

```powershell
notchgen-web
```

## The portal

Drop in one file or twenty. Each gets a tab along the bottom, with a status dot once it has run.
The left panel is the layer tree: every layer in the file, what it holds, and which role it plays.
Hovering a layer lights it up in the view and dims everything else, so you can see what you are
about to call the outer profile before committing to it; clicking keeps it lit. **Apply to all**
copies the current file's mapping onto every other open file sharing those layer names.

The view stays a single pane until there is something to compare; once a run has happened it
splits into before and after, side by side on a wide screen and stacked — before over after — on a
tall one.

**Generate notches** runs the whole batch. The download button then hands back a single DXF, or a
zip of every result when there is more than one; each tab also offers its own file.

The portal is dark by default and carries a light mode; the choice is remembered per browser. It is
drawn in the Dotmatrix design system — green-tinted near-blacks, one accent, one paper, with Doto,
Instrument Sans and JetBrains Mono self-hosted under `web/fonts/` so it looks right on a LAN with no
route to the internet.

## Quick start (CLI)

```sh
uv sync
uv run notchgen "samples/front_suspension - 1 (2.5 mm).dxf" -o out.dxf
```

Any number of inputs, into a directory:

```sh
uv run notchgen parts/*.dxf --depth 0.4 -O out/
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

The edge does not always cross the bend zone square-on. On an Onshape part one shoulder of the notch
routinely sits behind the bend line's end — by 0.5 mm, sometimes 1.5 mm — and a shallow notch measured
from that end would put its apex *outboard* of that shoulder: no relief on that side, and a cut that
crosses itself. So the apex is never placed less than `depth` inboard of the innermost shoulder; an
`apex-deepened` note says where that happened. A 0.4 mm notch is 0.4 mm of relief at every bend end.

## Bend and extent geometry

These are not always exported as `LINE` entities. A bend or an extent can arrive as a two-vertex
`LWPOLYLINE`, and one polyline can carry several of them as separate straight runs. All of it goes
through the same conversion the profile uses, so polylines are expanded into their straight runs and
object coordinate systems are undone on the way. Curved segments and non-geometric entities on those
layers are reported and ignored, since a bend and its extents have to be straight.

A bend that cannot be paired with an extent on each side is skipped with a warning naming it, and the
remaining bends are still notched — one odd bend on a thirty-bend part should not cost the other
twenty-nine their relief cuts. If *no* bend can be paired, that is an error, and it usually means the
bend and extent layers are mapped the wrong way round.

## Onshape exports

Right-click the flat pattern in Onshape and export it as DXF. The file differs from Fusion's in three
ways, all detected from the layer names:

| Onshape layer | What notchgen does with it |
| --- | --- |
| `SHEETMETAL_CUT_LINES` | Outline *and* holes share this layer. The loops are stitched separately, the largest closed one that encloses the rest is taken as the outer profile, and the holes are carried through untouched — a `CIRCLE` stays a `CIRCLE`. Two parts side by side, or an open chain, are still refused. |
| `SHEETMETAL_BEND_LINES_UP`, `SHEETMETAL_BEND_LINES_DOWN` | Both are bend lines. A role can be mapped to several layers: `--bend A --bend B` on the command line, a combined entry in the portal's dropdown. |
| `SHEETMETAL_BEND_TANGENT_LI` | The bend extents, if they are there. (Onshape itself cuts the name short at 26 characters.) They only appear when tangent lines were switched on for the export. |

**Without tangent lines** the bend zone is read off the outline. Where a bend meets a free edge,
Onshape's outline has a vertex at each edge of the bend zone — exactly the two points a tangent line
would have ended on — sitting at equal and opposite offsets from the bend line. Each bend takes the
narrowest width that shows up as such a pair; a bend that shows none (the edge ran straight on past
the zone, leaving a vertex on one side only, or none) takes a width the other bends established, with
a warning when that was a guess. On the real exports this was checked against, the notches built this
way match the ones built from the same part's real tangent lines to within 0.001 mm.

If a file gives no evidence at all, say so explicitly with `--bend-zone WIDTH` (or *Bend zone width*
under the portal's advanced settings), or re-export with tangent lines on.

## Layer mapping

Layer names are auto-detected — `OUTER*`/`*PROFILE*`/`*CUT_LINES`, `INTERIOR*`/`INNER*`, `BEND`/`*BEND_LINES_UP`/`_DOWN`,
`*EXTENT*`/`*TANGENT*` — and,
when the names give nothing away, by structure: the extent layer holds twice the lines of the bend
layer, and the outer profile is the largest layer left. Override with `--outer`, `--interior`,
`--bend`, `--extent`, or with the dropdowns in the portal. Only the outer and bend layers are
required: with no interior layer the holes are looked for on the outer layer, and with no extent
layer the bend zone is read off the outline.

Note that Fusion writes these layers *implicitly* — only layer `0` appears in the file's LAYER table.
`notchgen` creates proper layer records on the way out.

## Tolerances

| Flag | Default | Absorbs |
| --- | --- | --- |
| `--stitch-tol` | 1e-6 | gaps between adjacent outer-profile entity endpoints |
| `--snap-tol` | 0.02 | a ray hit snapping onto an existing profile vertex |
| `--sliver-tol` | 0.05 | shortest fragment worth emitting |
| `--chord-tol` | 1e-3 | sagitta when flattening curves for hit-testing and display |
| `--bridge-tol` | 0.01 | an outline left open by less than this is closed, with a warning |
| `--max-stub` | 1.0 | furthest an extent end may sit from the material edge |
| `--bend-zone` | auto | bend zone width, for a file with no extent layer |

These are calibrated against the real numbers in a Fusion export rather than picked round: junction
gaps come out around 4e-10, so `stitch-tol` has to be tiny; and `snap-tol` has to clear the 0.005
corner-fillet miss while staying well under the smallest genuine profile feature, which on the
sample is 0.079 long.

## When the outline will not close

The outer profile has to trace exactly one closed outline. When it does not, the number that
explains why is the **closure gap** — the distance between the two loose ends of the chain — not the
junction gap, which in a clean export sits around 1e-10 whether or not the outline closes. notchgen
reports the closure gap along with the coordinates of both loose ends, so you can go straight to that
point in the sketch.

A gap smaller than `--bridge-tol` is closed for you, wherever it sits — at the seam where the outline
closes, or at any junction in between. Below 1e-3 it is closed silently, since a gap that small is CAD
rounding noise two orders of magnitude under a laser kerf and there is nothing to decide; above that
you get a warning worth reading.

Duplicated edges and zero-length entities are dropped with a warning, since either one derails a
trace. An entity skipped for carrying no usable geometry — a `TEXT`, say — is called out, because a
skipped entity leaves a hole that looks exactly like a sketch gap. And bend or bend-extent lines
sitting on the outer layer are named specifically: they dead-end inside the part, so the fix is a
layer change rather than anything to do with tolerances.

## What it refuses to do

Anything it cannot do safely is reported rather than guessed at, with a diagnostic code shown in the
portal. Among them: a bend with only one extent, or with asymmetric extents; a bend end that does not
reach a free edge (that needs a different relief topology and is left alone); a depth larger than half
the bend; two notches whose cuts overlap; an outer profile that does not stitch into one closed loop;
a notch edge crossing the profile somewhere other than its own anchors; and a rebuilt outline whose
area does not match the notches that were cut from it.

## Deployment

`deploy/bootstrap.sh` provisions a Debian host: a `notchgen` service account, `uv`, a systemd unit,
an nginx reverse proxy, and a sudo rule narrow enough to permit exactly one privileged verb
(`systemctl restart notchgen`).

```sh
ssh root@host 'bash -s' < deploy/bootstrap.sh
```

`deploy/deploy.sh` publishes a checkout: rsync into place, `uv sync --frozen`, restart, then poll
`/healthz`. If the new release will not answer, it puts the previous one back and restarts that
instead, so a deploy that builds but does not start cannot take the portal down. `/api/version`
reports the release that is actually serving.

CI runs on every push and pull request: tests on a GitHub runner, then — for `main` only — a deploy
on a self-hosted runner on the target host, followed by a smoke test through nginx.

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
  api.py        FastAPI routes and the batch session store
  cli.py        command line entry point
web/            the portal: one page, no build step, no dependencies
deploy/         bootstrap, systemd unit, nginx site, health-gated deploy
tests/          136 tests, including regressions pinned to the sample file
```

## Development

```sh
uv run pytest
```

The tests pin the sample file's expected outcome — four notches, a known set of consumed entities, the
exact resolved anchor coordinates, and cut areas that add up to the area actually lost. The expected
values were derived independently of this implementation, by evaluating the file's B-splines with
De Boor's algorithm and ray-casting the result.
