"""The `fab` command group: acquisition, review, promotion, selection.

The only doorway to the network in this toolkit is `fab refresh`. Everything
else here reads state that is already on disk, and nothing under `validate`
or `release` calls any of it.

    fab refresh          fetch official sources -> observed snapshot
    fab status           freshness of approved data; pending observations
    fab diff             semantic changes: approved vs observed
    fab promote          make the reviewed observation the approved state
    fab select           choose a fabrication profile for a requirements file
    fab export-stackup   write a board physical-stackup supplement from an
                         approved construction
"""

from __future__ import annotations

import argparse
import json
import os

from . import FABRICATORS, adapter
from . import acquire as _acquire
from . import diff as _diff
from . import selection as _selection
from .store import FRESHNESS_DAYS_DEFAULT, CatalogStore, StoreError

_TOOLKIT_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


def _store_for(arguments):
    root = arguments.root or os.path.join(_TOOLKIT_ROOT, "profiles",
                                          arguments.fabricator)
    return CatalogStore(root, arguments.fabricator)


def _common(parser):
    parser.add_argument("--fabricator", default="jlcpcb",
                        choices=list(FABRICATORS))
    parser.add_argument("--root", default=None,
                        help="catalog root; defaults to the toolkit's "
                             "profiles/<fabricator> directory")


def main(argv):
    parser = argparse.ArgumentParser(prog="run.py fab")
    commands = parser.add_subparsers(dest="command", required=True)

    refresh = commands.add_parser("refresh")
    _common(refresh)
    refresh.add_argument("--timeout", type=int,
                         default=_acquire.DEFAULT_TIMEOUT_S)

    status = commands.add_parser("status")
    _common(status)
    status.add_argument("--max-age-days", type=float,
                        default=FRESHNESS_DAYS_DEFAULT)

    diff = commands.add_parser("diff")
    _common(diff)

    promote = commands.add_parser("promote")
    _common(promote)
    promote.add_argument("--observed", required=True,
                         help="at least 12 characters of the reviewed "
                              "observed snapshot's normalized digest")
    promote.add_argument("--initial", action="store_true")
    promote.add_argument("--allow-older", action="store_true")
    promote.add_argument("--note", default=None)

    select = commands.add_parser("select")
    _common(select)
    select.add_argument("requirements", help="path to a requirements JSON")

    export = commands.add_parser("export-stackup")
    _common(export)
    export.add_argument("--stackup", required=True)
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
    if arguments.command == "status":
        return _cmd_status(arguments, store)
    if arguments.command == "diff":
        return _cmd_diff(store)
    if arguments.command == "promote":
        return _cmd_promote(arguments, store)
    if arguments.command == "select":
        return _cmd_select(arguments, store)
    if arguments.command == "export-stackup":
        return _cmd_export(arguments, store)
    raise AssertionError(arguments.command)


def _cmd_refresh(arguments, store):
    snapshot, problem = _acquire.acquire(arguments.fabricator, store.root,
                                         timeout=arguments.timeout)
    if problem:
        print("refresh did not produce a usable observation: " + problem)
        print("the approved catalog is untouched")
        return 1
    print("observed snapshot recorded: normalized {}".format(
        snapshot["normalized_sha256"][:16]))
    approved = store.approved()
    if approved is None:
        print("no approved baseline exists yet; review the observation and "
              "promote it with --initial")
        return 0
    changes = _diff.semantic_diff(approved["normalized"],
                                  snapshot["normalized"])
    if not changes:
        print("semantically identical to the approved catalog "
              "({}); no review needed".format(
                  approved["normalized_sha256"][:16]))
        return 0
    print("REVIEW REQUIRED: {} semantic change(s) against the approved "
          "catalog. The approved data is unchanged. `fab diff` shows the "
          "changes; `fab promote` adopts them.".format(len(changes)))
    return 0


def _cmd_status(arguments, store):
    freshness = store.freshness(max_age_days=arguments.max_age_days)
    print("approved: {}".format(freshness["state"]))
    for key in ("age_days", "retrieved_utc", "detail"):
        if key in freshness:
            print("  {}: {}".format(key, freshness[key]))
    approved = store.approved()
    if approved is not None:
        print("  normalized: {}".format(approved["normalized_sha256"][:16]))
        print("  parser: {} v{}".format(approved["parser"].get("id"),
                                        approved["parser"].get("version")))
    try:
        observed = store.observed()
    except StoreError as exc:
        print("observed: UNUSABLE ({})".format(exc))
        return 1
    if observed is None:
        print("observed: none recorded")
        return 0
    print("observed: {} retrieved {}{}".format(
        observed["normalized_sha256"][:16], observed["retrieved_utc"],
        "" if observed.get("complete") else "  [INCOMPLETE]"))
    if approved is not None and observed.get("complete"):
        changes = _diff.semantic_diff(approved["normalized"],
                                      observed["normalized"])
        print("pending semantic change(s): {}".format(len(changes)))
        if approved["parser"] != observed["parser"]:
            print("  NOTE: parser identity differs between approved ({} "
                  "v{}) and observed ({} v{}); differences may reflect the "
                  "extractor, not the fabricator".format(
                      approved["parser"].get("id"),
                      approved["parser"].get("version"),
                      observed["parser"].get("id"),
                      observed["parser"].get("version")))
    return 0


def _cmd_diff(store):
    approved = store.approved()
    observed = store.observed()
    if observed is None:
        print("no observed snapshot; run `fab refresh` first")
        return 1
    if not observed.get("complete"):
        print("the observed snapshot is incomplete and cannot be compared "
              "meaningfully; its errors:")
        for error in observed.get("errors", []):
            print("  {}: {}".format(error.get("source"), error.get("error")))
        return 1
    if approved is None:
        print("no approved baseline; the whole observation ({}) is new".format(
            observed["normalized_sha256"][:16]))
        print("review it and promote with --initial")
        return 0
    changes = _diff.semantic_diff(approved["normalized"],
                                  observed["normalized"])
    print("approved {}  vs  observed {}".format(
        approved["normalized_sha256"][:16],
        observed["normalized_sha256"][:16]))
    print(_diff.render(changes))
    return 0 if not changes else 2


def _cmd_promote(arguments, store):
    approved = store.approved()
    observed = store.observed()
    changes = []
    if approved is not None and observed is not None \
            and observed.get("complete"):
        changes = _diff.semantic_diff(approved["normalized"],
                                      observed["normalized"])
    promoted = store.promote(arguments.observed, changes,
                             initial=arguments.initial,
                             allow_older=arguments.allow_older,
                             note=arguments.note)
    print("promoted: approved catalog is now {} (retrieved {}, {} semantic "
          "change(s) adopted)".format(promoted["normalized_sha256"][:16],
                                      promoted["retrieved_utc"],
                                      len(changes)))
    print("audit record appended: " + store.promotions_path)
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
    freshness = store.freshness()
    result["approved_normalized_sha256"] = approved["normalized_sha256"]
    result["approved_freshness"] = freshness
    print(json.dumps(result, indent=2))
    return 0 if result["feasible"] else 1


def _cmd_export(arguments, store):
    approved = store.approved()
    if approved is None:
        print("REFUSED: no approved catalog to export from")
        return 1
    names = [name.strip() for name in arguments.copper_layers.split(",")
             if name.strip()]
    document = _selection.export_physical_stackup(approved,
                                                  arguments.stackup, names)
    text = json.dumps(document, indent=2) + "\n"
    if arguments.out:
        with open(arguments.out, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        print("written: " + arguments.out)
    else:
        print(text)
    return 0
