"""Environment diagnostics.

The supported environment is Linux with KiCad installed as a distribution
package: `pcbnew` lands in the system interpreter's `dist-packages` and
Shapely is its own distribution package, so both are *externally owned* -
this module reports what is present and whether it behaves as the validator
needs, and never installs, upgrades, downgrades or pins anything.

Nothing heavy is imported at module load. Diagnostics must be able to explain a
broken environment, which they cannot do if importing them is what fails.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys

# Interpreter floor: the language features the framework actually uses.
MIN_PYTHON = (3, 11)

# KiCad major.minor whose report schemas and pcbnew APIs were verified. A
# different build is reported, not rejected, unless an API probe actually fails.
TESTED_KICAD = ("10.0",)


def _probe_python():
    """Judge the interpreter by its version; pcbnew is probed directly."""
    ok = tuple(sys.version_info[:2]) >= MIN_PYTHON
    return {"name": "python", "present": True,
            "version": ".".join(str(v) for v in sys.version_info[:3]),
            "path": sys.executable, "ok": ok,
            "detail": f"requires >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"}


def _probe_pcbnew():
    try:
        import pcbnew
    except ImportError as exc:
        return {"name": "pcbnew", "present": False, "version": None, "path": None,
                "ok": False,
                "detail": f"not importable ({exc}); pcbnew ships with KiCad - "
                          f"install the kicad distribution package"}
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
                "detail": f"not importable ({exc}); install the "
                          f"python3-shapely distribution package",
                "ownership": "distribution package"}
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
            "ownership": "distribution package; never installed or pinned by this framework"}


def resolve_tool(name_or_path):
    """An external tool named either by path or by bare command name.

    A manifest that spells out an absolute path pins one exact binary, which
    is what a reproducibility argument sometimes wants. A manifest that names
    `kicad-cli` wants whichever one this machine installed, which is what
    makes a fixture runnable on a machine other than the one that wrote it.
    Both are honoured: anything containing a separator is taken as given, a
    bare name is looked up on PATH, and a name that resolves to nothing is
    returned unchanged so the probe below reports "not found" rather than
    this function raising somewhere less explicable.
    """
    if not name_or_path or os.sep in name_or_path:
        return name_or_path
    return shutil.which(name_or_path) or name_or_path


def _probe_kicad_cli(path):
    resolved = resolve_tool(path)
    if not resolved or not os.path.isfile(resolved):
        return {"name": "kicad-cli", "present": False, "version": None,
                "path": path, "ok": False,
                "detail": "not found on PATH or at that path; set "
                          "tools.kicad_cli in the board manifest"}
    path = resolved
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
    """What to do about a broken environment. Never mutates anything.

    Every prerequisite here is externally owned - a distribution package
    this framework reports on and never installs - so the advice is the
    apt line a human runs, not something the validator does for them.
    """
    out = []
    by_name = {r["name"]: r for r in rows}
    if not by_name.get("pcbnew", {}).get("present", True):
        out.append("pcbnew is not importable. It ships inside the kicad "
                   "distribution package, which installs it into this "
                   "interpreter's dist-packages:")
        out.append("  sudo apt install kicad")
    if not by_name.get("shapely", {}).get("present", True):
        out.append("Shapely is a distribution package:")
        out.append("  sudo apt install python3-shapely")
    if not by_name.get("kicad-cli", {}).get("present", True):
        out.append("kicad-cli is not on PATH and tools.kicad_cli names no "
                   "existing file. It ships in the kicad package:")
        out.append("  sudo apt install kicad")
    for r in rows:
        if r.get("present") and not r["ok"]:
            out.append(f"{r['name']} is present but unusable: {r['detail']}")
    return out
