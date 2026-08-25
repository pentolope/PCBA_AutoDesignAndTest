"""The `fab` command group: acquisition, review, promotion, selection.

The only doorway to the network in this toolkit is `fab refresh`. Everything
else here reads state that is already on disk, and nothing under `validate`
or `release` calls any of it.

    fab refresh          fetch official sources -> newest observed attempt;
                         an identical result renews freshness, a differing
                         one asks for review, a failure supersedes nothing
                         approved but becomes the newest known source state
    fab ensure           refresh only when the approved knowledge is no
                         longer current; the polite scheduled entry point
    fab status           trust state of approved data; everything pending
    fab diff             semantic changes: approved vs newest observation
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

    ensure = commands.add_parser("ensure")
    _common(ensure)
    ensure.add_argument("--timeout", type=int,
                        default=_acquire.DEFAULT_TIMEOUT_S)
    ensure.add_argument("--max-age-days", type=float,
                        default=FRESHNESS_DAYS_DEFAULT)

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
    if arguments.command == "ensure":
        return _cmd_ensure(arguments, store)
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
        print("the acquisition attempt is now the newest observed state, "
              "and it is not usable: " + problem)
        print("the approved catalog is untouched; the raw evidence of the "
              "failure is preserved for reproduction")
        return 1
    print("observed snapshot recorded: normalized {}".format(
        snapshot["normalized_sha256"][:16]))
    approved = store.approved()
    if approved is None:
        print("no approved baseline exists yet; review the observation and "
              "promote it with --initial")
        return 0
    if approved["parser"] != snapshot["parser"]:
        print("NOTE: this observation was made by parser {} v{}; the "
              "approved catalog was made by {} v{}. Differences below may "
              "reflect the extractor, not the fabricator.".format(
                  snapshot["parser"].get("id"),
                  snapshot["parser"].get("version"),
                  approved["parser"].get("id"),
                  approved["parser"].get("version")))
    changes = _diff.semantic_diff(approved["normalized"],
                                  snapshot["normalized"])
    if not changes:
        record = store.record_verification(snapshot)
        print("semantically identical to the approved catalog ({}); "
              "freshness renewed - the approved semantics are verified "
              "current as of {}".format(
                  approved["normalized_sha256"][:16],
                  record["verified_utc"]))
        return 0
    print("REVIEW REQUIRED: {} semantic change(s) against the approved "
          "catalog. The approved data is unchanged. `fab diff` shows the "
          "changes; `fab promote` adopts them.".format(len(changes)))
    return 0


def _cmd_ensure(arguments, store):
    freshness = store.freshness(max_age_days=arguments.max_age_days)
    if freshness["state"] == "current" and not freshness["attention"]:
        print("approved knowledge is current ({} days old, limit {}); "
              "no network access needed".format(
                  freshness.get("age_days"), arguments.max_age_days))
        return 0
    print("state: {} - {}".format(freshness["state"],
                                  freshness.get("detail")))
    for item in freshness["attention"]:
        print("  attention: {}".format(item))
    if freshness["state"] == "no-baseline":
        print("run `fab refresh` and promote an initial baseline; `ensure` "
              "does not create trust on its own")
        return 1
    return _cmd_refresh(arguments, store)


def _cmd_status(arguments, store):
    freshness = store.freshness(max_age_days=arguments.max_age_days)
    print("approved: {}".format(freshness["state"]))
    for key in ("age_days", "evidence_utc", "verified_utc",
                "renewed_by_verification_utc", "detail"):
        if key in freshness:
            print("  {}: {}".format(key, freshness[key]))
    try:
        approved = store.approved()
    except StoreError as exc:
        print("  UNUSABLE: {}".format(exc))
        return 1
    if approved is not None:
        print("  normalized: {}".format(approved["normalized_sha256"][:16]))
        print("  parser: {} v{}".format(approved["parser"].get("id"),
                                        approved["parser"].get("version")))
    for item in freshness["attention"]:
        print("ATTENTION: {}".format(item))
    try:
        observed = store.observed()
    except StoreError as exc:
        print("newest attempt: UNUSABLE ({})".format(exc))
        return 1
    if observed is None:
        print("newest attempt: none recorded")
        return 0
    digest = observed.get("normalized_sha256")
    print("newest attempt: {} retrieved {}  [{}]".format(
        digest[:16] if digest else "-", observed["retrieved_utc"],
        observed["outcome"].upper()))
    if approved is not None and observed["outcome"] == "complete":
        changes = _diff.semantic_diff(approved["normalized"],
                                      observed["normalized"])
        print("pending semantic change(s): {}".format(len(changes)))
    return 0


def _cmd_diff(store):
    approved = store.approved()
    observed = store.observed()
    if observed is None:
        print("no observed snapshot; run `fab refresh` first")
        return 1
    if observed["outcome"] != "complete":
        print("the newest acquisition attempt is {} and cannot be compared "
              "meaningfully; its errors:".format(observed["outcome"]))
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
            and observed.get("outcome") == "complete":
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
    for item in freshness["attention"]:
        print("ATTENTION: {}".format(item))
    if freshness["state"] == "stale":
        print("ATTENTION: {}".format(freshness.get("detail")))
    print(json.dumps(result, indent=2))
    return 0 if result["feasible"] else 1


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
