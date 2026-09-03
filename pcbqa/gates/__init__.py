"""The gate registry as a library.

`evaluate` answers the question a search loop has to ask - "would this board
pass these gates?" - in-process, over a board file that need not be the
authoritative one, judged with the project's own rules. It is the same
registry `run.py validate` runs; there is no second, looser set of gates.

A run with a selection or a board override is a diagnostic, never a verdict:
nothing here writes into the tree, and the document commands emit for a
partial run is marked as one.
"""

from __future__ import annotations

import shutil
import tempfile


def load():
    """Import every gate module, populating the registry exactly once."""
    from . import (g_provenance, g_checks, g_geometry,        # noqa: F401
                   g_contracts, g_assembly, g_export_parity,
                   g_fabrication, g_orientation, g_timing,
                   g_simulation)


def evaluate(manifest, only=None, board_path=None, workdir=None):
    """Run gates in-process and return their GateResult list, in run order.

    `manifest` is a manifest path or a loaded Manifest. `only` selects gates
    the way `validate --only` does: exact IDs, fnmatch patterns, or a gate
    class (`design` / `release-artifact` / `fixture`); None runs everything.
    A selector that names no registered gate raises ValueError rather than
    silently running nothing.

    `board_path` substitutes a candidate board for the declared one: every
    gate that reads the board - natively or through a staged check - judges
    the candidate against the project's own rules and library tables. Gates
    that judge committed artifacts still look at the tree, which is why a
    candidate run selects the `design` class.

    `workdir` is where staging and tool output go; when omitted a private
    temporary directory is used and removed afterwards - pass one to keep the
    run's intermediate output.
    """
    from ..core import Context, load_manifest, run_all, select_gates

    load()
    if isinstance(manifest, str):
        manifest = load_manifest(manifest)

    ids = None
    if only is not None:
        tokens = [only] if isinstance(only, str) else list(only)
        ids, unknown = select_gates(tokens)
        if unknown:
            raise ValueError(
                "no such gate, pattern or class: {}".format(unknown))
        if not ids:
            raise ValueError("the selection names no gate at all")

    owned = None
    if workdir is None:
        owned = tempfile.mkdtemp(prefix="pcbqa_gates_")
        workdir = owned
    try:
        ctx = Context(manifest, workdir, board_path=board_path)
        try:
            ctx.tool_versions["kicad"] = ctx.kicad_version()
        except Exception as exc:                               # noqa: BLE001
            ctx.tool_versions["kicad"] = "UNAVAILABLE: {}".format(exc)
        return run_all(ctx, only=ids)
    finally:
        if owned:
            shutil.rmtree(owned, ignore_errors=True)
