#!/usr/bin/env python
"""pcbqa - board-agnostic KiCad/JLCPCB verification.

    python run.py preflight [manifest]      diagnose the environment
    python run.py selftest [--jobs auto|N]  run the validator's own test suite
    python run.py build <manifest>          generate the fabrication outputs
    python run.py validate <manifest> [-w]  validate; nonzero if rejected
    python run.py release-check <manifest>  is this commit taggable as a release?
    python run.py gates                     list gate IDs
    python run.py fab <cmd>                 fabricator knowledge: refresh,
                                            status, diff, promote, select,
                                            export-stackup. The ONLY commands
                                            that may touch the network, and
                                            only `fab refresh` does.

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
    from pcbqa.gates import (g_provenance, g_checks, g_geometry,   # noqa: F401
                             g_contracts, g_assembly, g_export_parity,
                             g_fabrication, g_orientation, g_timing)


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


def _emit(ctx, results, tag, directory=None):
    from pcbqa import core
    doc = core.to_json(results, ctx)
    directory = directory or ctx.workdir
    jpath = os.path.join(directory, tag + ".json")
    mpath = os.path.join(directory, tag + ".md")
    with open(jpath, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
    with open(mpath, "w", encoding="utf-8") as fh:
        fh.write(core.to_markdown(doc))
    return doc, jpath, mpath


def cmd_validate(manifest_path, write=False, quiet=False):
    """Validate a board: its sources and the exact artifacts committed with it.

    Read-only unless `write` is given, so ordinary development validation never
    touches the working tree. `--write` records the verdict at the path the
    manifest declares, which is the report a release commit carries.
    """
    from pcbqa import artifacts, core
    from pcbqa.core import Context, ManifestError
    from pcbqa.layout import LayoutError

    try:
        manifest, workspace = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc), None, None

    _load_gates()
    run = workspace.new_run()
    ctx = Context(manifest, run.work)
    try:
        ctx.tool_versions["kicad"] = ctx.kicad_version()
    except Exception as exc:                                   # noqa: BLE001
        ctx.tool_versions["kicad"] = "UNAVAILABLE: {}".format(exc)

    try:
        results = core.run_all(ctx)
        doc, jpath, mpath = _emit(ctx, results, "validation", run.path)
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
    from pcbqa import build as build_mod
    from pcbqa.core import Context, ManifestError
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
    # The artifacts are in the tree and committed from there; the staging copy
    # of the whole project has nothing left to say.
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


def cmd_gates():
    from pcbqa import core
    _load_gates()
    for entry in core.registered():
        req = ", ".join(entry["requires"]) or "-"
        print("{:32s} {}".format(entry["id"], entry["title"]))
        print("{:32s} requires: {}".format("", req))
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
        fabricator = options.get("fabricator") or "jlcpcb"
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profiles", fabricator)
        approved = CatalogStore(root, fabricator).approved()
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
        fabricator = options.get("fabricator") or "jlcpcb"
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "profiles", fabricator)
        approved = CatalogStore(root, fabricator).approved()
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
        return cmd_gates()
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
        unknown = [f for f in flags
                   if not (cmd == "validate" and f in ("-w", "--write"))]
        if unknown:
            print("unknown option(s) for {}: {}".format(cmd, unknown))
            return 2
        path = _find_manifest(rest[0])
        if cmd == "validate":
            return cmd_validate(path, write=bool(flags))[0]
        if cmd == "build":
            return cmd_build(path)
        return cmd_release_check(path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
