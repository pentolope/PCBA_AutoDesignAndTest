"""The `fab` command group: acquisition, review, selection.

The only doorway to the network in this toolkit is `fab refresh`. Everything
else reads what is already committed, and nothing under `validate` or
`release-check` calls any of it.

    fab refresh          fetch the official sources, parse them, and show
                         what changed against the committed catalog. Writes
                         the acquisition into a scratch directory and nothing
                         into the catalog: adopting a change is an ordinary
                         Git commit, reviewed like any other.
    fab select           choose a fabrication profile for a requirements file
    fab impedance        solve a controlled-impedance geometry
    fab export-stackup   write a board physical-stackup supplement from a
                         committed construction
"""

from __future__ import annotations

import argparse
import json
import os

from . import acquire as _acquire
from . import diff as _diff
from . import selection as _selection
from .store import CatalogStore, StoreError, write_catalog

_TOOLKIT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _store_for(arguments):
    root = arguments.root or os.path.join(_TOOLKIT_ROOT, "profiles", "jlcpcb")
    return CatalogStore(root)


def _common(parser):
    parser.add_argument("--root", default=None,
                        help="catalog root; defaults to the toolkit's "
                             "profiles/jlcpcb directory")


def main(argv):
    parser = argparse.ArgumentParser(prog="run.py fab")
    commands = parser.add_subparsers(dest="command", required=True)

    refresh = commands.add_parser("refresh")
    _common(refresh)
    refresh.add_argument("--timeout", type=int,
                         default=_acquire.DEFAULT_TIMEOUT_S)
    refresh.add_argument("--out", default=None,
                         help="directory to write the acquisition into; "
                              "defaults to a temporary one")

    select = commands.add_parser("select")
    _common(select)
    select.add_argument("requirements", help="path to a requirements JSON")

    impedance = commands.add_parser("impedance")
    _common(impedance)
    impedance.add_argument("requirements",
                           help="path to the board's fabrication "
                                "requirements JSON")
    impedance.add_argument("--stackup", required=True)
    impedance.add_argument("--layer", type=int, required=True,
                           help="1-based copper layer index in the "
                                "construction, outermost first")
    impedance.add_argument("--references", required=True,
                           help="comma-separated 1-based copper indices "
                                "of the declared reference plane(s)")
    impedance.add_argument("--mode", default="single-ended",
                           choices=["single-ended", "differential"])
    impedance.add_argument("--target", type=float, required=True,
                           help="target impedance in ohms")
    impedance.add_argument("--width-min", type=float, required=True)
    impedance.add_argument("--width-max", type=float, required=True)
    impedance.add_argument("--soldermask", required=True,
                           choices=["present", "absent"],
                           help="whether the routing layer is covered by "
                                "soldermask; the topology depends on it "
                                "and it is not guessed")

    export = commands.add_parser("export-stackup")
    _common(export)
    export.add_argument("requirements",
                        help="path to the board's fabrication requirements "
                             "JSON; the export re-runs selection and only "
                             "a construction compatible with the selected "
                             "profile can come out")
    export.add_argument("--stackup", default=None,
                        help="resolve a preserved candidate ambiguity; "
                             "must be one of the selection's own candidates")
    export.add_argument("--copper-layers", required=True,
                        help="comma-separated board copper layer names, "
                             "outermost first")
    export.add_argument("--out", default=None)

    arguments = parser.parse_args(argv)
    store = _store_for(arguments)
    try:
        return _dispatch(arguments, store)
    except (StoreError, _selection.SelectionError) as exc:
        print("REFUSED: {}".format(exc))
        return 1


def _dispatch(arguments, store):
    if arguments.command == "refresh":
        return _cmd_refresh(arguments, store)
    if arguments.command == "select":
        return _cmd_select(arguments, store)
    if arguments.command == "export-stackup":
        return _cmd_export(arguments, store)
    if arguments.command == "impedance":
        return _cmd_impedance(arguments, store)
    raise AssertionError(arguments.command)


def _cmd_refresh(arguments, store):
    """Fetch, parse, and report. Adopting the result is a Git commit."""
    import tempfile

    out = arguments.out or tempfile.mkdtemp(prefix="pcbqa_fab_refresh_")
    os.makedirs(out, exist_ok=True)
    result, problem = _acquire.acquire(timeout=arguments.timeout)
    raw = result.pop("raw")

    # Written in the committed layout, so adopting a reviewed refresh is a
    # copy and a commit rather than a state transition.
    write_catalog(out, result, raw)
    print("acquisition: {} ({})".format(result["outcome"], out))

    if problem:
        print("not usable: " + problem)
        print("the committed catalog is untouched; the raw bytes that failed "
              "are under " + os.path.join(out, "catalog", "evidence"))
        return 1

    approved = store.approved()
    if approved is None:
        print("no committed catalog exists yet; review the acquisition and "
              "commit it as profiles/jlcpcb/catalog/approved.json")
        return 0
    if approved["parser"] != result["parser"]:
        print("NOTE: this acquisition was parsed by {} v{}; the committed "
              "catalog by {} v{}. Differences below may reflect the "
              "extractor, not the fabricator.".format(
                  result["parser"].get("id"), result["parser"].get("version"),
                  approved["parser"].get("id"),
                  approved["parser"].get("version")))

    changes = _diff.semantic_diff(approved["normalized"],
                                  result["normalized"])
    if not changes:
        print("semantically identical to the committed catalog ({}). "
              "Nothing to review. Commit the refreshed evidence only if the "
              "retrieval date is worth recording.".format(
                  approved["normalized_sha256"][:16]))
        return 0
    print("{} semantic change(s) against the committed catalog:".format(
        len(changes)))
    for change in changes[:40]:
        print("  " + json.dumps(change, sort_keys=True))
    if len(changes) > 40:
        print("  ... {} more".format(len(changes) - 40))
    print("Review them. To adopt: copy {} over profiles/jlcpcb/catalog and "
          "commit. The commit is the approval, and `git log` is the record "
          "of it.".format(os.path.join(out, "catalog")))
    return 0


def _cmd_select(arguments, store):
    approved = store.approved()
    if approved is None:
        print("REFUSED: no approved catalog; selection never runs against "
              "unreviewed data")
        return 1
    with open(arguments.requirements, encoding="utf-8") as handle:
        requirements = json.load(handle)
    result = _selection.select(approved["normalized"], requirements)
    result["approved_normalized_sha256"] = approved["normalized_sha256"]
    print(json.dumps(result, indent=2))
    return 0 if result["feasible"] else 1


def _cmd_impedance(arguments, store):
    from . import impedance as _impedance
    approved = store.approved()
    if approved is None:
        print("REFUSED: no approved catalog; the solver never runs "
              "against unreviewed data")
        return 1
    with open(arguments.requirements, encoding="utf-8") as handle:
        requirements = json.load(handle)
    references = [int(value) for value in
                  arguments.references.split(",") if value.strip()]
    try:
        result = _impedance.solve(approved, {
            "requirements": requirements,
            "stackup": arguments.stackup,
            "copper_layer": arguments.layer,
            "reference_copper_layers": references,
            "mode": arguments.mode,
            "target_ohm": arguments.target,
            "width_search_mm": {"min": arguments.width_min,
                                "max": arguments.width_max},
            "soldermask_present": arguments.soldermask == "present",
        })
    except _impedance.ImpedanceError as exc:
        print("REFUSED: {}".format(exc))
        return 1
    control = result.get("fabrication_control") or {}
    if not control.get("impedance_control_selected"):
        print("NOTE: this profile does not select controlled impedance; "
              "a successful exit means a manufacturable geometry whose "
              "ANALYTIC nominal meets the target - it does not mean the "
              "fabricator will control the line to it")
    if result.get("enclosure"):
        print("NOTE: coated-microstrip yields a MODEL width enclosure "
              "- not a point solution, and not proven physical bounds "
              "on the fabricated line; geometry_feasible stays false "
              "by design and the exit status follows it")
    print(json.dumps(result, indent=2))
    # Exit 0 means geometry_feasible: an unambiguous analytic root whose
    # width the fabricator's published limits accept. Whether the target
    # is also a controlled fabrication requirement is a separate fact,
    # carried in fabrication_control and never implied by the exit code.
    return 0 if result.get("geometry_feasible") else 1


def _cmd_export(arguments, store):
    approved = store.approved()
    if approved is None:
        print("REFUSED: no approved catalog to export from")
        return 1
    names = [name.strip() for name in arguments.copper_layers.split(",")
             if name.strip()]
    with open(arguments.requirements, encoding="utf-8") as handle:
        requirements = json.load(handle)
    document = _selection.export_physical_stackup(
        approved, requirements, names, stackup_id=arguments.stackup)
    text = json.dumps(document, indent=2) + "\n"
    if arguments.out:
        with open(arguments.out, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print("written: " + arguments.out)
    else:
        print(text)
    return 0
