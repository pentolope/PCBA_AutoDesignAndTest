# Prerequisites

This framework runs on **the system Python 3, with KiCad installed as a
distribution package**. The constraint that matters has not changed and never
will: the checks read the design through `pcbnew`, a SWIG binding compiled
against one specific KiCad build. A `pcbnew` from one KiCad and a `kicad-cli`
from another do not agree about geometry, and there is no version of this
framework that can make them.

On Linux one package satisfies both ends of that pairing. `kicad` installs
`pcbnew.py` into this interpreter's `dist-packages` and `kicad-cli` onto PATH,
built together, so the interpreter you already have is the supported one. There
is no separate bundled interpreter to hunt for - and the old heuristic that
looked for "kicad" in the interpreter's path reported the *working* setup as
suspicious, which is why it is gone.

Run everything as:

```bash
python3 run.py preflight
```

## What must already be present

| Component | Supplied by | How it is obtained |
|---|---|---|
| Python ≥ 3.11 | the distribution | `python3` |
| `pcbnew` | the `kicad` package | `sudo apt install kicad` |
| `kicad-cli` | the `kicad` package | same package; named by the board manifest's `tools.kicad_cli`, which may be a bare command name resolved on PATH or an absolute path pinning one binary |
| Shapely | the `python3-shapely` package | `sudo apt install python3-shapely` |

Everything else the framework uses is in the Python standard library. There is
no `requirements.txt`, no virtual environment and no lockfile, because there is
no environment here that this project owns.

## What this framework will never do

**It will not install, upgrade, downgrade or pin anything.** `pcbnew` and
Shapely belong to distribution packages and are therefore owned by the system
package manager. Pinning them here — even to the version that happens to be
present — would mean this project quietly fighting `apt` over a shared
installation, and the first `pip install` that "fixed" a version would be the
one that shadowed the `pcbnew` its `kicad-cli` was built with.

If a prerequisite is missing or unusable, `run.py preflight` says so, names the
component, prints where it looked, and exits nonzero. It then stops.

## How compatibility is actually decided

Not by comparing version strings. `preflight` probes the specific APIs and
behaviours the checks rely on:

- `pcbnew`: `BOARD.Tracks`, `PCB_VIA.GetWidth(layer)`,
  `PAD.TransformShapeToPolygon`, `ERROR_OUTSIDE`, `TENTING_MODE_TENTED`,
  `FOOTPRINT.GetFPIDAsString`
- Shapely: edge-touching polygons must report `distance() == 0` and zero
  intersection area, `buffer()` must grow a polygon, and `STRtree.query` must
  work
- `kicad-cli`: must answer `--version`

A build that passes those probes is usable whatever it calls itself; a build
that fails one is rejected with the failing probe named, whatever its version
number says.

### Version ranges that genuinely matter

- **KiCad 10.0.x** — the ERC and DRC JSON report *schemas* are version-specific.
  KiCad 10 puts DRC findings under a top-level `violations` key and ERC findings
  under `sheets[].violations`. `pcbqa/reports.py` implements that schema and
  raises `ReportSchemaError` rather than guessing at another one, so a report
  from a different major version is reported as unsupported, not
  misinterpreted.
- **Python ≥ 3.11** — the language features the framework uses.
- **Shapely 2.x** — `STRtree.query` returns indices in 2.x and geometries in
  1.x. The predicate probe catches the difference.

Every validation and release report records the resolved version **and the
module path** of each component under `tooling.components`, so a result can
always be traced back to the environment that produced it.
