#!/usr/bin/env python
"""pcbqa - board-agnostic KiCad/JLCPCB verification.

    python run.py preflight [manifest]      diagnose the environment
    python run.py selftest [--jobs auto|N]  run the validator's own test suite
    python run.py build <manifest>          generate the fabrication outputs
    python run.py validate <manifest> [-w]  validate; nonzero if rejected
                       [--only=A,B]         run a selection of gates
                                            (diagnostic): exact IDs, patterns
                                            like ROUTE.*, or a class - design,
                                            release-artifact, fixture
    python run.py check-board <manifest>    sub-second integrity preflight:
                                            accepted routing, artifact
                                            freshness, foreign KiCad files
    python run.py release-check <manifest>  is this commit taggable as a release?
    python run.py gates                     list gate IDs and classes
                 [--missing <manifest>]     which gates the manifest has not
                                            enabled, and the enabling keys
    python run.py fab <cmd>                 JLCPCB knowledge: refresh, select,
                                            impedance, export-stackup. Only
                                            `fab refresh` may touch the
                                            network, and it writes nothing
                                            into the catalog.

<manifest> is a path. For the toolkit's own fixtures a bare name also works:
`portability`, `clean`, or a negative fixture's directory name.

A release is a Git tag. `build` writes the fabrication artifacts into the
working tree as ordinary files, `validate` judges the design and those exact
artifacts, and `release-check` proves the committed state is one a tag may name.
No command in this file creates, moves or deletes a Git tag.

Fail-closed: a gate that cannot be evaluated reports ERROR and blocks, and
`release-check` exits nonzero unless every requirement is met.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# Nothing that needs pcbnew or Shapely is imported at module scope: preflight
# has to be able to explain a broken environment, and it cannot do that if
# importing this file is what fails.


ENV_OUTPUT = "PCBQA_OUTPUT_ROOT"


def _inside(root, path):
    """Is `path` within `root`? Anything uncomparable is outside."""
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:                       # no common root to speak of
        return False


def _output_base(manifest=None):
    """Where run artifacts go, in order of precedence.

    1. The parallel runner isolating a worker. This has to win: two workers
       sharing an output root is the failure `tests/test_runner.py` exists to
       prevent, and nothing else may reintroduce it.
    2. `PCBQA_OUTPUT_ROOT`, an explicit override for one invocation.
    3. A consumer board's own project. When the manifest lives outside this
       repository, its scratch belongs to it - not inside a toolkit that is
       very likely a submodule, and a submodule directory is one
       `git submodule update` away from being replaced.
    4. This repository, for a manifest that lives here. Fixtures depend on
       this: a fixture's runs must not land inside the fixture, because
       PROV.FIXTURE_INTEGRITY holds fixtures to an exact inventory.

    Deliberately decided here rather than by a manifest key: `output_root` in a
    manifest would be hashed into the configuration identity, so relocating
    scratch would unbind every report a board had already committed.

    """
    from pcbqa.parallel import ENV_OUTPUT_ROOT
    worker_root = os.environ.get(ENV_OUTPUT_ROOT)
    if worker_root:
        return worker_root
    override = os.environ.get(ENV_OUTPUT)
    if override:
        return os.path.abspath(override)
    if manifest is not None and not _inside(HERE, manifest.path):
        return manifest.resolve(".")
    return HERE


def _load_gates():
    from pcbqa import gates
    gates.load()


def open_board(manifest_path):
    """Load and validate a manifest, then derive its output layout.

    The single entry point for every manifest-driven command. Nothing
    filesystem-shaped exists until both of these succeed, and the workspace is
    built from the validated manifest rather than from raw JSON, so no command
    is ever in a position to join untrusted text onto a path.
    """
    from pcbqa.core import load_manifest
    from pcbqa.layout import Workspace
    manifest = load_manifest(manifest_path)
    return manifest, Workspace.for_manifest(manifest, _output_base(manifest))


def _refuse(exc):
    print("REFUSED: " + str(exc))
    return 1


def _emit(ctx, results, tag, directory=None, extra=None):
    from pcbqa import core
    doc = core.to_json(results, ctx, extra=extra)
    directory = directory or ctx.workdir
    jpath = os.path.join(directory, tag + ".json")
    mpath = os.path.join(directory, tag + ".md")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(core.to_markdown(doc))
    return doc, jpath, mpath


def cmd_validate(manifest_path, write=False, quiet=False, only=None):
    """Validate a board: its sources and the exact artifacts committed with it.

    Read-only unless `write` is given, so ordinary development validation never
    touches the working tree. `--write` records the verdict at the path the
    manifest declares, which is the report a release commit carries.

    `only` restricts the run to a selection of gates - exact IDs, fnmatch
    patterns over IDs (`ROUTE.*`), or a gate class (`design`,
    `release-artifact`, `fixture`). That exists for one reason: a search -
    routing candidates, placement candidates - has to judge the DESIGN, and
    the release-artifact gates cannot be satisfied mid-search because the
    artifacts are generated from the design the search has not finished
    choosing. A partial run is a diagnostic, never a verdict: it refuses
    `--write`, and the document it produces is marked partial so nothing
    downstream can mistake it for a validation.
    """
    from pcbqa.core import ManifestError
    from pcbqa.layout import LayoutError

    try:
        manifest, workspace = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc), None, None

    if not write:
        return _validate(manifest, workspace, write, quiet, only)
    # A verdict that will be recorded in the tree is about a tree nothing
    # else is rewriting while it is read.
    try:
        with workspace.hold("validate --write"):
            return _validate(manifest, workspace, write, quiet, only)
    except LayoutError as exc:
        return _refuse(exc), None, None


def _validate(manifest, workspace, write, quiet, only):
    from pcbqa import artifacts, core
    from pcbqa.core import Context

    _load_gates()
    run = workspace.new_run()
    ctx = Context(manifest, run.work)
    try:
        ctx.tool_versions["kicad"] = ctx.kicad_version()
    except Exception as exc:                                   # noqa: BLE001
        ctx.tool_versions["kicad"] = "UNAVAILABLE: {}".format(exc)

    if only is not None:
        selection = list(only)
        only, unknown = core.select_gates(selection)
        if unknown:
            print("REFUSED: no such gate, pattern or class: {} "
                  "(run.py gates lists gates and their classes)"
                  .format(unknown))
            run.discard()
            return 2, None, ctx
        if not only:
            print("REFUSED: the selection names no gate at all")
            run.discard()
            return 2, None, ctx
        if write:
            print("REFUSED: a partial run is a diagnostic, not a verdict; "
                  "--only and --write are mutually exclusive")
            run.discard()
            return 2, None, ctx

    try:
        results = core.run_all(ctx, only=only)
        partial = None
        if only is not None:
            # Marked before the document is written, so the file on disk -
            # the thing downstream actually reads - carries the marker too.
            partial = {"partial": {
                "only": sorted(only),
                "meaning": "a subset of the registered gates was run; this "
                           "document is not a validation of the board"}}
        doc, jpath, mpath = _emit(ctx, results, "validation", run.path,
                                  extra=partial)
    except BaseException:
        # This run produced nothing usable; it owns its directory and takes it
        # with it.
        run.discard()
        raise

    recorded = None
    if write:
        recorded = artifacts.paths(manifest).get("validation_report")
        if not recorded:
            print("REFUSED: --write needs artifacts.validation_report in the "
                  "manifest to say where the verdict belongs")
            return 2, doc, ctx
        os.makedirs(os.path.dirname(recorded), exist_ok=True)
        shutil.copy2(jpath, recorded)

    if not quiet:
        print(core.to_markdown(doc))
        if only is not None:
            print(chr(10) + "PARTIAL RUN: {} of {} gate(s); this is a "
                  "diagnostic, not a validation".format(
                      len(only), len(core.registered())))
        print(chr(10) + "run:      " + run.path)
        print("JSON:     " + jpath)
        print("Markdown: " + mpath)
        if recorded:
            print("Recorded: " + recorded)
    return (1 if doc["summary"]["blocking"] else 0), doc, ctx


def cmd_build(manifest_path):
    """Generate the fabrication outputs and install them into the tree.

    One invocation, one scratch directory. KiCad runs against a private copy,
    so the design is never opened for writing, and nothing reaches the project
    until every generation step has succeeded: a build that could not produce a
    complete set installs none of it and leaves the previous outputs alone.
    """
    from pcbqa.core import ManifestError
    from pcbqa.layout import LayoutError

    try:
        manifest, workspace = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc)
    if not manifest.has("release_generation"):
        print("BUILD REFUSED: the manifest declares no release_generation "
              "block, so there is nothing that says how to generate anything")
        return 2
    if not manifest.has("artifacts.fabrication_manifest"):
        print("BUILD REFUSED: the manifest declares no "
              "artifacts.fabrication_manifest, so a build could not record "
              "what it produced")
        return 2

    try:
        with workspace.hold("build"):
            return _build(manifest, workspace)
    except LayoutError as exc:
        return _refuse(exc)


def _build(manifest, workspace):
    from pcbqa import build as build_mod
    from pcbqa.core import Context

    run = workspace.new_run()
    builder = build_mod.Build(Context(manifest, run.work), run.build)
    try:
        record = builder.run()
    except build_mod.BuildError as exc:
        builder.blockers.append(("build", "ERROR", str(exc)))
        record = None
    except KeyboardInterrupt:
        print(chr(10) + "BUILD ABANDONED: interrupted before it could complete")
        return 130
    except BaseException as exc:                              # fail closed
        print(chr(10) + "BUILD BLOCKED by an unhandled {}: {}".format(
            type(exc).__name__, exc))
        return 1

    for entry in builder.summary()["steps"]:
        if "exit" in entry:
            print("  {}: exit {}".format(entry["step"], entry["exit"]))
    if builder.blockers or record is None:
        print(chr(10) + "BUILD BLOCKED by {} condition(s):".format(
            len(builder.blockers)))
        for step, status, why in builder.blockers[:25]:
            print("  {}: {} - {}".format(step, status, why))
        print("Nothing was installed; the previous fabrication outputs are "
              "untouched.")
        print("What this build did produce, for diagnosis: " + run.path)
        return 1

    installed = builder.install()
    # The artifacts are in the tree and committed from there; the staged
    # design and the scratch it was built in have nothing left to say.
    run.discard()
    print(chr(10) + "Installed {} file(s) into the working tree:".format(
        len(installed)))
    root = manifest.resolve(".")
    for path in installed:
        print("  " + os.path.relpath(path, root))
    print("source closure: " + str(record["source_closure_sha256"])[:16])
    print(chr(10) + "These are ordinary files. Commit them, then run "
                    "`release-check`.")
    return 0


def cmd_release_check(manifest_path):
    """Is the committed state one a release tag may name?

    Three independent questions, all of which must answer yes:

      * Git can say the tree is exactly the commit, submodules included, and
        every release artifact and every piece of required evidence is tracked;
      * every mandatory gate passes right now against the committed sources and
        the committed artifacts;
      * the validation report committed beside them accepted this same design.

    Nothing is written and no Git state is changed. The tag is the user's to
    create, and this command's exit status is what says it may be.
    """
    from pcbqa import release
    from pcbqa.core import ManifestError, Status
    from pcbqa.layout import LayoutError

    try:
        manifest, _workspace = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc)

    profile = manifest.get("release_profile", None)
    if not profile:
        print("RELEASE BLOCKED: manifest declares no release_profile")
        return 1
    mandatory = list(profile.get("mandatory_gates", []))
    if not mandatory:
        print("RELEASE BLOCKED: release profile names no mandatory gates")
        return 1

    blockers = []
    try:
        problems, facts = release.readiness(manifest)
    except release.GitError as exc:
        problems, facts = [{"issue": str(exc)}], {}
    for key in sorted(facts):
        print("  {:26s} {}".format(key, facts[key]))
    for problem in problems:
        detail = problem["issue"]
        if problem.get("paths"):
            detail += ": " + ", ".join(problem["paths"])
        blockers.append(("git:" + str(problem.get("file", "repository")),
                         "ERROR", detail))

    code, doc, _ctx = cmd_validate(manifest_path, quiet=True)
    if doc is None:
        blockers.append(("validate", "ERROR", "validation could not run"))
    else:
        by_id = {entry["gate"]: entry for entry in doc["gates"]}
        for gate_id in mandatory:
            result = by_id.get(gate_id)
            if result is None:
                blockers.append((gate_id, "MISSING",
                                 "mandatory gate did not run"))
            elif result["status"] not in (Status.PASS, Status.ADVISORY):
                # ADVISORY is a decision the board wrote down: the gate ran, it
                # found what it found, and the manifest says with reasons that
                # this finding does not stop a release. It stays on the
                # mandatory list so it still has to run and still has to be
                # reported.
                blockers.append((gate_id, result["status"],
                                 str(result.get("reason", ""))[:110]))
        for entry in doc["gates"]:
            if entry["status"] in Status.BLOCKING and \
                    entry["gate"] not in mandatory:
                blockers.append((entry["gate"], entry["status"],
                                 "non-mandatory gate blocked"))
        blockers += _committed_verdict(manifest, doc)

    if blockers:
        print(chr(10) + "RELEASE BLOCKED by {} condition(s):".format(
            len(blockers)))
        for subject, status, why in blockers[:40]:
            print("  {}: {} - {}".format(subject, status, why))
        if len(blockers) > 40:
            print("  ... {} more".format(len(blockers) - 40))
        print(chr(10) + "No release tag should be created for this commit.")
        return 1

    commit = facts.get("commit", "HEAD")
    print(chr(10) + "RELEASE READY")
    print("  board:  " + manifest.board_id)
    print("  commit: " + str(commit))
    print("  source closure: "
          + str(doc.get("source_closure_sha256"))[:16])
    print(chr(10) + "This commit may be tagged. Creating the tag is a "
                    "deliberate act and is not done here:")
    print("  git tag -a <name> -m <message> " + str(commit))
    return 0


def _committed_verdict(manifest, doc):
    """The validation report committed beside the artifacts must accept them."""
    from pcbqa import artifacts

    blockers = []
    recorded = artifacts.paths(manifest).get("validation_report")
    if not recorded:
        return [("release:validation_report", "ERROR",
                 "the manifest declares no artifacts.validation_report, so no "
                 "verdict travels with the release")]
    if not os.path.isfile(recorded):
        return [("release:validation_report", "ERROR",
                 "no committed validation report at " + recorded)]
    try:
        with open(recorded, encoding="utf-8") as fh:
            committed = json.load(fh)
    except ValueError as exc:
        return [("release:validation_report", "ERROR",
                 "the committed validation report is not readable JSON: "
                 "{}".format(exc))]
    verdict = (committed.get("summary") or {}).get("verdict")
    if verdict != "ACCEPTED":
        blockers.append(("release:validation_report", "ERROR",
                         "the committed verdict is {!r}".format(verdict)))
    was = committed.get("source_closure_sha256")
    now = doc.get("source_closure_sha256")
    if was != now:
        blockers.append((
            "release:validation_report", "ERROR",
            "the committed verdict is about a different design ({} vs "
            "{})".format(str(was)[:16], str(now)[:16])))
    return blockers


def cmd_selftest(argv):
    from pcbqa import parallel
    parser = argparse.ArgumentParser(prog="run.py selftest")
    parser.add_argument("--jobs", default="auto",
                        help="worker processes: auto (default), 1, or a count")
    parser.add_argument("--timeout", type=int, default=1800,
                        help="seconds before a stalled worker is killed")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--output-root", default=None)
    args = parser.parse_args(argv[2:])
    code, _summary = parallel.run(os.path.join(HERE, "tests"), HERE,
                                  jobs=args.jobs, timeout_s=args.timeout,
                                  fail_fast=args.fail_fast,
                                  output_root=args.output_root)
    return code


def _find_manifest(name):
    """Resolve a manifest argument to a path.

    A consumer board always passes a real path. The short forms exist only for
    the toolkit's own fixtures, so `validate portability` and
    `validate <fixture-name>` work from a checkout without spelling out
    where the tests keep things.
    """
    if os.path.isfile(name):
        return name
    for candidate in (
            os.path.join(HERE, "tests", "manifests", name),
            os.path.join(HERE, "tests", "manifests", name + ".json"),
            os.path.join(HERE, "tests", "fixtures", "negative", name,
                         "manifest.json"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return name


def cmd_preflight(argv):
    from pcbqa import preflight
    kicad_cli = None
    if len(argv) > 2:
        manifest_path = _find_manifest(argv[2])
    else:
        manifest_path = os.path.join(HERE, "tests", "manifests",
                                     "portability.json")
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            kicad_cli = json.load(fh).get("tools", {}).get("kicad_cli")
    except (OSError, ValueError) as exc:
        print(f"could not read {manifest_path}: {exc}")
    ok, rows = preflight.environment(kicad_cli)
    print("pcbqa preflight")
    print(preflight.report(rows))
    notes = preflight.advice(rows)
    if notes:
        print("")
        for line in notes:
            print(line)
    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


def cmd_gates(argv):
    from pcbqa import core
    from pcbqa.core import ManifestError, _missing_label, _satisfied
    from pcbqa.layout import LayoutError
    _load_gates()

    args = argv[2:]
    if args and args[0] == "--missing":
        # Which gates a manifest has NOT enabled, and the key that would
        # enable each: the opt-in surface as a query instead of archaeology.
        if len(args) != 2:
            print("usage: run.py gates --missing <manifest.json>")
            return 2
        try:
            manifest, _workspace = open_board(_find_manifest(args[1]))
        except (ManifestError, LayoutError) as exc:
            return _refuse(exc)
        dark = set()
        for entry in core.registered():
            missing = [_missing_label(k) for k in entry["requires"]
                       if not _satisfied(manifest, k)]
            if not missing:
                continue
            dark.add(entry["id"])
            print("{:32s} [{}] {}".format(entry["id"], entry["class"],
                                          entry["title"]))
            print("{:32s} declare: {}".format("", ", ".join(missing)))
        total = len(core.registered())
        print("\n{} of {} gates not enabled by this manifest; key shapes are "
              "in schemas/manifest.v2.json".format(len(dark), total))

        # A gate can also decide NOT_APPLICABLE from what its declarations
        # contain, which no static key check can see. The recorded validation
        # already holds those verdicts with their reasons, so read them back
        # rather than pretending the static list is the whole answer.
        from pcbqa import artifacts
        recorded = artifacts.paths(manifest).get("validation_report")
        if recorded and os.path.isfile(recorded):
            try:
                with open(recorded, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except ValueError:
                doc = None
            rows = [g for g in (doc or {}).get("gates", [])
                    if g.get("status") == "NOT_APPLICABLE"
                    and g.get("gate") not in dark]
            if rows:
                print("\ndeclared, but NOT_APPLICABLE at the last recorded "
                      "validation:")
                for g in rows:
                    print("{:32s} {}".format(g["gate"],
                                             str(g.get("reason", ""))[:90]))
        elif recorded:
            print("\nno recorded validation at {}; runtime NOT_APPLICABLE "
                  "reasons appear there after `validate --write`"
                  .format(os.path.relpath(recorded)))
        return 0
    if args:
        print("usage: run.py gates [--missing <manifest.json>]")
        return 2

    for entry in core.registered():
        req = ", ".join(
            " OR ".join(key) if isinstance(key, tuple) else key
            for key in entry["requires"]) or "-"
        print("{:32s} [{}] {}".format(entry["id"], entry["class"],
                                      entry["title"]))
        print("{:32s} requires: {}".format("", req))
    return 0


def cmd_check_board(manifest_path):
    """Sub-second board-integrity preflight; no DRC, extraction or simulation.

    Answers three questions a long autonomous run should ask after anything
    external touched the tree, and a pre-commit hook can afford to ask every
    time: is the board in the tree the routing candidate that was accepted;
    were the committed fabrication artifacts generated from the design as it
    stands now (naming which input moved when not); and does any KiCad file
    the design does not reach - another project's artifact, an autosave - sit
    beside the design. Read-only; exits nonzero on any finding.
    """
    from pcbqa import closure as closure_mod
    from pcbqa import routing_record
    from pcbqa.core import (DESIGN_SUFFIXES, ManifestError, design_inputs,
                            sha256_file)
    from pcbqa.layout import LayoutError

    try:
        manifest, _workspace = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc)

    problems = []
    checked = []

    def report(area, issue):
        problems.append((area, issue))

    board = manifest.resolve(manifest.get("sources.pcb"))
    if not os.path.isfile(board):
        report("sources", "declared board does not exist: " + board)

    if manifest.has("routing.provenance") and os.path.isfile(board):
        checked.append("routing record")
        relative = manifest.get("routing.provenance")
        path = manifest.resolve(relative)
        if not os.path.isfile(path):
            report("routing", "routing record {} is declared but "
                              "absent".format(relative))
        else:
            try:
                with open(path, encoding="utf-8") as fh:
                    record = json.load(fh)
                for problem in routing_record.compare_to_board(
                        record, sha256_file(board)):
                    report("routing", problem["issue"])
            except (ValueError, routing_record.RoutingRecordError) as exc:
                report("routing", "routing record {}: {}".format(relative, exc))

    if manifest.has("artifacts.fabrication_manifest"):
        checked.append("artifact freshness")
        path = manifest.resolve(manifest.get("artifacts.fabrication_manifest"))
        if not os.path.isfile(path):
            report("fabrication", "no fabrication record at {}; the committed "
                                  "artifacts are unaccounted for".format(path))
        else:
            try:
                with open(path, encoding="utf-8") as fh:
                    record = json.load(fh)
                entries, now = closure_mod.current(manifest)
                was = record.get("source_closure_sha256")
                if was != now:
                    detail = ("committed artifacts were generated from a "
                              "different design ({} vs {})".format(
                                  str(was)[:16], now[:16]))
                    bound = record.get("source_closure")
                    if isinstance(bound, dict):
                        changed = sorted(k for k in set(bound) & set(entries)
                                         if bound[k] != entries[k])
                        added = sorted(set(entries) - set(bound))
                        removed = sorted(set(bound) - set(entries))
                        for label, names in (("changed", changed),
                                             ("added", added),
                                             ("removed", removed)):
                            if names:
                                detail += "; {}: {}".format(
                                    label, ", ".join(names[:6]))
                                if len(names) > 6:
                                    detail += " (+{})".format(len(names) - 6)
                    report("fabrication", detail)
            except ValueError as exc:
                report("fabrication", "fabrication record is not readable "
                                      "JSON: {}".format(exc))
            except Exception as exc:                           # noqa: BLE001
                report("fabrication", "{}: {}".format(
                    type(exc).__name__, exc))

    # KiCad files the design cannot reach, sitting where the design lives:
    # another project's artifact appearing here is how one session's work
    # silently became another board's input.
    root = os.path.realpath(manifest.resolve("."))
    try:
        known = set(design_inputs(manifest))
    except ManifestError as exc:
        known = set()
        report("sources", str(exc))
    directories = {os.path.dirname(os.path.join(root, rel)) for rel in known}
    for directory in sorted(directories):
        for name in sorted(os.listdir(directory)
                           if os.path.isdir(directory) else []):
            full = os.path.join(directory, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if os.path.isfile(full) and name.endswith(DESIGN_SUFFIXES) \
                    and rel not in known:
                report("foreign", "{} is a KiCad file the design does not "
                                  "reach - another project's artifact, or an "
                                  "autosave".format(rel))

    checked.append("foreign design files")
    for area, issue in problems:
        print("  {:12s} {}".format(area + ":", issue))
    if problems:
        print("\nBOARD CHECK FAILED: {} finding(s)".format(len(problems)))
        return 1
    skipped = [label for label, key in (
        ("routing record", "routing.provenance"),
        ("artifact freshness", "artifacts.fabrication_manifest"),
    ) if not manifest.has(key)]
    print("BOARD CHECK OK: checked {}{}".format(
        ", ".join(checked),
        "; not declared, so not checkable: " + ", ".join(skipped)
        if skipped else ""))
    return 0


def cmd_extract(argv):
    """Geometry baseline extraction: run.py extract <manifest> --out F
    --nets A,B (--copper L=mm,... --board-thickness mm |
    --approved-copper L=pos:oz,... --board-thickness mm |
    --physical-from-requirements FILE) [--validation F]
    """
    if len(argv) < 3:
        print("usage: run.py extract <manifest.json> --out FILE "
              "--nets N1,N2 --copper LAYER=mm,... "
              "--board-thickness MM [--validation FILE]")
        return 2
    import json as json_module
    from pcbqa import extract
    options = {}
    key = None
    for token in argv[3:]:
        if token.startswith("--"):
            key = token[2:]
            options[key] = ""
        elif key is not None:
            options[key] = token
            key = None
    from_requirements = options.get("physical-from-requirements")
    required_options = ["out", "nets"] if from_requirements         else ["out", "nets", "board-thickness"]
    for required in required_options:
        if not options.get(required):
            print("extract: missing --{}".format(required))
            return 2
    modes = [bool(options.get("copper")),
             bool(options.get("approved-copper")),
             bool(from_requirements)]
    if sum(modes) != 1:
        print("extract: exactly one of --copper (caller-declared), "
              "--approved-copper LAYER=position:oz,... (resolved "
              "from approved evidence), or "
              "--physical-from-requirements FILE (assignments AND "
              "board thickness derived from the board's declared "
              "fabrication requirements plus the approved catalog) "
              "must be given")
        return 2
    manifest, _layout = open_board(_find_manifest(argv[2]))
    board_file = manifest.resolve(manifest.get("sources.pcb"))
    import pcbnew
    from pcbqa import geom
    geom.configure(manifest.geometry_profile()
                   .tolerance("polygon_chord_error_mm").value)
    board = pcbnew.LoadBoard(board_file)
    thickness = None
    if from_requirements:
        import hashlib
        from pcbqa.fabricators.store import CatalogStore
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profiles", "jlcpcb")
        approved = CatalogStore(root).approved()
        if approved is None:
            print("extract: no approved catalog; refusing to invent "
                  "physical inputs")
            return 2
        with open(from_requirements, "rb") as handle:
            requirements_bytes = handle.read()
        requirements = json_module.loads(
            requirements_bytes.decode("utf-8"))
        stack = [board.GetLayerName(layer) for layer in
                 board.GetEnabledLayers().CuStack()]
        assignments = extract.copper_assignments_from_requirements(
            requirements, stack)
        copper = extract.approved_finished_copper(approved,
                                                  assignments)
        thickness = extract.requirements_board_thickness(
            requirements,
            hashlib.sha256(requirements_bytes).hexdigest())
    elif options.get("approved-copper"):
        from pcbqa.fabricators.store import CatalogStore
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profiles", "jlcpcb")
        approved = CatalogStore(root).approved()
        if approved is None:
            print("extract: no approved catalog; refusing to invent "
                  "physical inputs")
            return 2
        assignments = {}
        for pair in options["approved-copper"].split(","):
            layer, _, spec = pair.partition("=")
            position, _, weight = spec.partition(":")
            assignments[layer] = (position, float(weight))
        copper = extract.approved_finished_copper(approved,
                                                  assignments)
    else:
        declared = {}
        for pair in options["copper"].split(","):
            layer, _, value = pair.partition("=")
            declared[layer] = float(value)
        copper = extract.caller_declared_copper(declared)
    if thickness is None:
        thickness = extract.physical_parameter(
            float(options["board-thickness"]), "mm",
            "caller-declared", "caller-declared board thickness",
            applicability="through-via barrel estimates only")
    validation = None
    if options.get("validation"):
        with open(options["validation"], encoding="utf-8") as handle:
            validation = json_module.load(handle)
    report = extract.baseline_report(
        board_file, board, options["nets"].split(","), copper,
        thickness, validation)
    extract.write_report(report, options["out"])
    print("baseline: {} nets -> {}".format(len(report["nets"]),
                                           options["out"]))
    return 0


def main(argv):
    # No toolkit command may ever raise a dialog a human must
    # dismiss: an autonomous run freezes on an unwatched screen.
    from pcbqa import headless
    headless.suppress_blocking_ui()
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "preflight":
        return cmd_preflight(argv)
    if cmd == "selftest":
        return cmd_selftest(argv)
    if cmd == "gates":
        return cmd_gates(argv)
    if cmd == "check-board":
        rest = argv[2:]
        if len(rest) != 1:
            print("usage: run.py check-board <manifest.json>")
            return 2
        return cmd_check_board(_find_manifest(rest[0]))
    if cmd == "extract":
        return cmd_extract(argv)
    if cmd == "fab":
        from pcbqa.fabricators import cli as fab_cli
        return fab_cli.main(argv[2:])
    if cmd in ("validate", "build", "release-check"):
        rest = [a for a in argv[2:] if not a.startswith("-")]
        flags = [a for a in argv[2:] if a.startswith("-")]
        if len(rest) != 1:
            print("usage: run.py {} <manifest.json>{}".format(
                cmd, " [--write]" if cmd == "validate" else ""))
            return 2
        only = None
        kept = []
        for flag in flags:
            if cmd == "validate" and flag.startswith("--only="):
                only = [gate for gate in flag.split("=", 1)[1].split(",")
                        if gate]
                continue
            kept.append(flag)
        unknown = [f for f in kept
                   if not (cmd == "validate" and f in ("-w", "--write"))]
        if unknown:
            print("unknown option(s) for {}: {}".format(cmd, unknown))
            return 2
        if only is not None and not only:
            print("--only names no gate; run.py gates lists them")
            return 2
        path = _find_manifest(rest[0])
        if cmd == "validate":
            return cmd_validate(path, write=bool(kept), only=only)[0]
        if cmd == "build":
            return cmd_build(path)
        return cmd_release_check(path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
