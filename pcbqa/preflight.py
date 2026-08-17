"""Environment diagnostics.

The supported environment is KiCad's own Python. `pcbnew` ships with KiCad and
Shapely is supplied by an installed KiCad add-on, so both are *externally
owned*: this module reports what is present and whether it behaves as the
validator needs, and never installs, upgrades, downgrades or pins anything.

Nothing heavy is imported at module load. Diagnostics must be able to explain a
broken environment, which they cannot do if importing them is what fails.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

# Interpreter floor: the language features the framework actually uses.
MIN_PYTHON = (3, 11)

# KiCad major.minor whose report schemas and pcbnew APIs were verified. A
# different build is reported, not rejected, unless an API probe actually fails.
TESTED_KICAD = ("10.0",)


def _probe_python():
    ok = tuple(sys.version_info[:2]) >= MIN_PYTHON
    looks_like_kicad = "kicad" in sys.executable.lower()
    detail = f"requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    if not looks_like_kicad:
        detail += "; this does not look like KiCad's interpreter"
    return {"name": "python", "present": True,
            "version": ".".join(str(v) for v in sys.version_info[:3]),
            "path": sys.executable, "ok": ok, "detail": detail,
            "looks_like_kicad_python": looks_like_kicad}


def _probe_pcbnew():
    try:
        import pcbnew
    except ImportError as exc:
        return {"name": "pcbnew", "present": False, "version": None, "path": None,
                "ok": False,
                "detail": f"not importable ({exc}); pcbnew ships with KiCad and "
                          f"is only importable from KiCad's own Python"}
    probes = [
        ("BOARD.Tracks", lambda: pcbnew.BOARD().Tracks),
        ("PCB_VIA.GetWidth(layer)",
         lambda: pcbnew.PCB_VIA(pcbnew.BOARD()).GetWidth(pcbnew.F_Cu)),
        ("PAD.TransformShapeToPolygon",
         lambda: pcbnew.PAD(pcbnew.FOOTPRINT(pcbnew.BOARD())).TransformShapeToPolygon),
        ("ERROR_OUTSIDE", lambda: pcbnew.ERROR_OUTSIDE),
        ("TENTING_MODE_TENTED", lambda: pcbnew.TENTING_MODE_TENTED),
        ("FOOTPRINT.GetFPIDAsString",
         lambda: pcbnew.FOOTPRINT(pcbnew.BOARD()).GetFPIDAsString),
    ]
    missing = []
    for label, probe in probes:
        try:
            probe()
        except Exception as exc:                       # noqa: BLE001 - diagnostic
            missing.append(f"{label} ({type(exc).__name__})")
    return {"name": "pcbnew", "present": True,
            "version": pcbnew.GetBuildVersion(),
            "path": getattr(pcbnew, "__file__", None),
            "ok": not missing, "tested_against": list(TESTED_KICAD),
            "detail": ("all required APIs present" if not missing
                       else "missing or broken APIs: " + ", ".join(missing))}


def _probe_shapely():
    try:
        shapely = importlib.import_module("shapely")
        from shapely.geometry import Polygon
        from shapely.ops import unary_union                  # noqa: F401
        from shapely.strtree import STRtree
        from shapely.affinity import translate               # noqa: F401
    except ImportError as exc:
        return {"name": "shapely", "present": False, "version": None, "path": None,
                "ok": False,
                "detail": f"not importable ({exc}); Shapely is provided by a "
                          f"KiCad add-on, install it through KiCad's Plugin and "
                          f"Content Manager",
                "ownership": "KiCad add-on"}
    problems = []
    try:
        square = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        touching = Polygon([(1, 0), (2, 0), (2, 1), (1, 1)])
        if square.distance(touching) != 0.0:
            problems.append("distance() of edge-touching polygons is not 0")
        if square.intersection(touching).area != 0.0:
            problems.append("intersection area of edge-touching polygons is not 0")
        if square.buffer(0.1, join_style=2).area <= square.area:
            problems.append("buffer() did not grow a polygon")
        STRtree([square, touching]).query(square)
    except Exception as exc:                                # noqa: BLE001
        problems.append(f"{type(exc).__name__}: {exc}")
    return {"name": "shapely", "present": True,
            "version": getattr(shapely, "__version__", "unknown"),
            "path": getattr(shapely, "__file__", None),
            "ok": not problems,
            "detail": ("required predicates behave as expected" if not problems
                       else "; ".join(problems)),
            "ownership": "KiCad add-on; never installed or pinned by this framework"}


def _probe_kicad_cli(path):
    if not path or not os.path.isfile(path):
        return {"name": "kicad-cli", "present": False, "version": None,
                "path": path, "ok": False,
                "detail": "not found; set tools.kicad_cli in the board manifest"}
    try:
        proc = subprocess.run([path, "--version"], capture_output=True,
                              text=True, timeout=120)
        build = (proc.stdout or proc.stderr).strip().splitlines()[0]
        ok = proc.returncode == 0
        detail = "responds to --version" if ok else f"exit {proc.returncode}"
    except Exception as exc:                                # noqa: BLE001
        build, ok, detail = None, False, f"{type(exc).__name__}: {exc}"
    return {"name": "kicad-cli", "present": True, "version": build, "path": path,
            "ok": ok, "detail": detail, "tested_against": list(TESTED_KICAD)}


def environment(kicad_cli=None):
    """Structured description of the environment actually in use."""
    rows = [_probe_python(), _probe_pcbnew(), _probe_shapely()]
    if kicad_cli is not None:
        rows.append(_probe_kicad_cli(kicad_cli))
    return all(r["ok"] for r in rows), rows


def report(rows):
    width = max(len(r["name"]) for r in rows)
    lines = []
    for r in rows:
        mark = "ok  " if r["ok"] else "FAIL"
        lines.append(f"  [{mark}] {r['name'].ljust(width)}  {r['version'] or '-'}")
        if r.get("path"):
            lines.append(f"         {'':{width}}  path: {r['path']}")
        lines.append(f"         {'':{width}}  {r['detail']}")
    return "\n".join(lines)


def advice(rows):
    """What to do about a broken environment. Never mutates anything."""
    out = []
    by_name = {r["name"]: r for r in rows}
    if not by_name.get("pcbnew", {}).get("present", True):
        if not by_name.get("python", {}).get("looks_like_kicad_python", False):
            out.append("This must be run with KiCad's own Python - the "
                       "python executable inside your KiCad installation's "
                       "bin directory, not the system interpreter:")
            out.append("  <KiCad install>/bin/python  verification/run.py "
                       "preflight")
        else:
            out.append("pcbnew is missing from this KiCad installation; "
                       "repair or reinstall KiCad.")
    if not by_name.get("shapely", {}).get("present", True):
        out.append("Shapely is provided by a KiCad add-on. Install it through "
                   "KiCad's Plugin and Content Manager. This framework does not "
                   "install into an environment it does not own.")
    for r in rows:
        if r.get("present") and not r["ok"]:
            out.append(f"{r['name']} is present but unusable: {r['detail']}")
    return out
