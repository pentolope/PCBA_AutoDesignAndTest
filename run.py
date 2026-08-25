#!/usr/bin/env python
"""pcbqa - board-agnostic KiCad/JLCPCB verification.

    python run.py preflight [manifest]     diagnose the environment
    python run.py selftest [--jobs auto|N] run the validator's own test suite
    python run.py validate <manifest>      validate a board; nonzero if rejected
    python run.py release  <manifest>      clean-room release attempt
    python run.py coherence <manifest>     is the installed release one run?
    python run.py gates                    list gate IDs
    python run.py fab <cmd>                fabricator knowledge: refresh,
                                           status, diff, promote, select,
                                           export-stackup. The ONLY commands
                                           that may touch the network, and
                                           only `fab refresh` does.

<manifest> is a path. For the toolkit's own fixtures a bare name also works:
`portability`, `clean`, or a negative fixture's directory name.

Run everything with KiCad's own Python. pcbnew, Shapely and kicad-cli are
externally supplied prerequisites; this tool reports what it finds and never
installs or pins them.

Fail-closed: a gate that cannot be evaluated reports ERROR and blocks, and the
release command never produces a sealed package.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
    """Is `path` within `root`? False across drives, as Windows requires."""
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except ValueError:                       # different drives on Windows
        return False


def _output_base(manifest=None):
    """Where run artifacts go, in order of precedence.

    1. The parallel runner isolating a worker. This has to win: two workers
       sharing an output root is the failure `tests/test_runner.py` exists to
       prevent, and nothing else may reintroduce it.
    2. `PCBQA_OUTPUT_ROOT`, an explicit override for one invocation.
    3. A consumer board's own project. When the manifest lives outside this
       repository, its attempts and published candidates belong to it - not
       inside a toolkit that is very likely a submodule, and a submodule
       directory is one `git submodule update` away from being replaced.
    4. This repository, for a manifest that lives here. Fixtures depend on
       this: a fixture's runs must not land inside the fixture, because
       PROV.FIXTURE_INTEGRITY holds fixtures to an exact inventory.

    Deliberately decided here rather than by a manifest key. `output_root` in
    a manifest would be hashed into `configuration_identity`, so merely
    relocating output would unbind every report a board had already committed
    - a provenance break in exchange for a directory choice.
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
    filesystem-shaped exists until both of these succeed, and the layout is
    built from the validated manifest rather than from raw JSON, so no command
    is ever in a position to join untrusted text onto a path.
    """
    from pcbqa.core import load_manifest
    from pcbqa.layout import OutputLayout
    manifest = load_manifest(manifest_path)
    return manifest, OutputLayout.for_manifest(manifest,
                                               _output_base(manifest))


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


def cmd_validate(manifest_path, quiet=False):
    """Validate a board. Everything this run writes lives in its own attempt."""
    from pcbqa import core
    from pcbqa.core import Context, ManifestError
    from pcbqa.layout import LayoutError

    try:
        manifest, layout = open_board(manifest_path)
    except (ManifestError, LayoutError) as exc:
        return _refuse(exc), None, None

    _load_gates()
    attempt = layout.new_attempt()
    ctx = Context(manifest, attempt.work)
    try:
        ctx.tool_versions["kicad"] = ctx.kicad_version()
    except Exception as exc:                                   # noqa: BLE001
        ctx.tool_versions["kicad"] = "UNAVAILABLE: {}".format(exc)

    try:
        results = core.run_all(ctx)
        doc, jpath, mpath = _emit(ctx, results, "validation", attempt.path)
    except BaseException:
        # This attempt produced nothing usable; it owns its directory and
        # takes it with it. No sibling attempt and no published release is
        # any of its business.
        attempt.discard()
        raise

    if not quiet:
        print(core.to_markdown(doc))
        print(chr(10) + "attempt:  " + attempt.path)
        print("JSON:     " + jpath)
        print("Markdown: " + mpath)
    return (1 if doc["summary"]["blocking"] else 0), doc, ctx


def cmd_release(manifest_path):
    """Clean-room release.

    One invocation, one attempt directory, and nothing outside it is touched
    for any reason. The project is copied into the attempt, every previously
    generated output is purged *from that copy*, and ERC, DRC, Gerbers,
    drills, BOM, CPL and the fabrication archive are regenerated inside
    `<attempt>/build`. That directory is a candidate until the moment every
    mandatory gate has passed, at which point it is renamed into
    `published/<release_id>` - a name that did not exist before, so nothing is
    replaced and nothing has to be deleted to make room.

    A failed run removes its own build directory and leaves diagnostics. It
    does not remove a previous release, a sibling attempt, or anything else:
    a run that could not produce a release has learned nothing about the
    release that came before it.
    """
    from pcbqa import cleanroom, core
    from pcbqa.core import Context, ManifestError
    from pcbqa.layout import LayoutError, orderable_archives

    try:
        manifest, layout = open_board(manifest_path)
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
    if not manifest.has("release_generation"):
        print("RELEASE BLOCKED: manifest declares no release_generation block, "
              "so a clean-room run cannot be reproduced")
        return 1

    _load_gates()
    attempt = layout.new_attempt()
    published = False
    try:
        code = _release_attempt(manifest, layout, attempt, profile, mandatory)
        published = (code == 0)
        return code
    except KeyboardInterrupt:
        print(chr(10) + "RELEASE ABANDONED: interrupted before it could "
                        "complete")
        return 130
    except BaseException as exc:                              # fail closed
        print(chr(10) + "RELEASE BLOCKED by an unhandled {}: {}".format(
            type(exc).__name__, exc))
        return 1
    finally:
        if not published:
            attempt.discard_build()
        remaining = [a for a in orderable_archives(attempt.path)]
        if remaining:
            print("WARNING: archive(s) remain in the attempt: {}".format(
                remaining))


def _release_attempt(manifest, layout, attempt, profile, mandatory):
    from pcbqa import cleanroom, core
    from pcbqa.core import Context, Status

    source_ctx = Context(manifest, os.path.join(attempt.work, "driver"))
    run = cleanroom.CleanRun(source_ctx, os.path.join(attempt.work, "clean_run"),
                             attempt.build)

    derived = None
    try:
        derived = run.build()
    except cleanroom.CleanRoomError as exc:
        run.blockers.append(("release:cleanroom", "ERROR", str(exc)))
    except Exception as exc:                                   # fail closed
        run.blockers.append(("release:cleanroom", "ERROR",
                             f"{type(exc).__name__}: {exc}"))

    blockers = list(run.blockers)
    doc = jpath = mpath = None
    if derived is not None:
        ctx = Context(derived, os.path.join(attempt.work, "validation"),
                      kicad_cli=run.source_ctx.kicad_cli)
        results = core.run_all(ctx)
        doc, jpath, mpath = _emit(ctx, results, "release_validation")
        doc["clean_room"] = run.summary()
        with open(jpath, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        print(core.to_markdown(doc))

        by_id = {r.gate_id: r for r in results}
        for gate_id in mandatory:
            result = by_id.get(gate_id)
            if result is None:
                blockers.append((gate_id, "MISSING", "mandatory gate did not run"))
            elif result.status not in (Status.PASS, Status.ADVISORY):
                # ADVISORY is a decision the board wrote down: the gate ran, it
                # found what it found, and the manifest says with reasons that
                # this finding does not stop a release. It stays on the
                # mandatory list so it still has to run and still has to be
                # reported - deleting the manifest's advisory entry is all it
                # takes to make it block again.
                blockers.append((gate_id, result.status, result.reason[:110]))
        for r in results:
            if r.status in Status.BLOCKING and r.gate_id not in mandatory:
                blockers.append((r.gate_id, r.status, "non-mandatory gate blocked"))

    print(os.linesep + "Attempt:       " + attempt.path)
    print("Clean-room run: " + run.root)
    for entry in run.summary()["steps"]:
        if "exit" in entry:
            print("  {}: exit {}".format(entry["step"], entry["exit"]))
    print("  purged {} pre-existing output path(s) from the copy".format(
        len(run.summary()["purged"])))
    print("  {} authoritative path(s) proven inside the run".format(
        len(run.summary()["authoritative_paths"])))

    if blockers:
        _write_diagnostics(attempt, manifest, profile, blockers, jpath, mpath)
        print(chr(10) + "RELEASE BLOCKED by {} condition(s):".format(
            len(blockers)))
        for gate_id, status, why in blockers[:25]:
            print("  {}: {} - {}".format(gate_id, status, why))
        print("Diagnostics only: " + attempt.diagnostics)
        print("Published release created: NO")
        return 1

    # Every mandatory gate passed. Finish the candidate, then publish it by
    # renaming it into a name that has never existed.
    shutil.copytree(run.reports, os.path.join(attempt.build, "reports"),
                    dirs_exist_ok=True)
    shutil.copy2(jpath, os.path.join(attempt.build, "validation.json"))
    with open(os.path.join(attempt.build, "clean_room.json"), "w",
              encoding="utf-8") as fh:
        json.dump(run.summary(), fh, indent=2)
    with open(os.path.join(attempt.build, "UNSEALED.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(_unsealed_text(profile))

    # The package is complete only now, so this is the first moment an
    # inventory of it can be true. The receipt is written last and is what
    # makes the set checkable later: a validation report cannot carry its own
    # digest, so without a receipt nothing says which files belong together.
    from pcbqa import coherence
    names = coherence.member_names(manifest)
    coherence.write_receipt(attempt.build, {
        "board_id": manifest.board_id,
        "attempt_id": attempt.id,
        "verdict": doc["summary"]["verdict"],
        "source_closure_sha256": run.summary()["source_closure_sha256"],
        "members": names,
    })
    incoherent, facts = coherence.check(attempt.build, names)
    if incoherent:
        for problem in incoherent[:25]:
            blockers.append(("release:coherence", "ERROR",
                             "{}: {}".format(problem.get("file", "package"),
                                             problem["issue"])))
        _write_diagnostics(attempt, manifest, profile, blockers, jpath, mpath)
        print(chr(10) + "RELEASE BLOCKED: the assembled package does not agree "
                        "with itself")
        for gate_id, status, why in blockers[-25:]:
            print("  {}: {} - {}".format(gate_id, status, why))
        print("Published release created: NO")
        return 1
    print("  package coherent: {} files, one source closure {}".format(
        facts["files_in_package"],
        str(facts.get("source_closure_sha256"))[:16]))

    release_id, destination = attempt.publish()
    pointer = layout.write_latest(release_id, {
        "board_id": manifest.board_id,
        "attempt_id": attempt.id,
        "sealed": False,
    })
    print(chr(10) + "Published release: " + destination)
    print("Release id:        " + release_id)
    print("latest.json:       " + layout.latest_pointer)
    print("Sealed:            NO (sealing requires visual-review evidence)")
    return 0


def _unsealed_text(profile):
    return ("Release CANDIDATE, not sealed." + chr(10) + chr(10)
            + "Every artifact here was generated in one clean-room run from a "
              "pristine project copy with all prior output purged, and every "
              "mandatory gate passed against these exact artifacts before this "
              "directory was published. It was assembled under the attempt "
              "that produced it and moved here by a single rename, so it has "
              "never existed in a partly-written state." + chr(10) + chr(10)
            + "RECEIPT.json lists every other file here with its digest, "
              "written after the gates passed. PROV.RELEASE_COHERENCE checks "
              "that claim rather than asking you to take it: run "
              "`run.py coherence <manifest>` against an installed package and "
              "it will fail if any file came from a different run." + chr(10)
            + chr(10)
            + "Sealing additionally requires recorded visual-review evidence "
              "({}).".format(profile.get("visual_review_evidence"))
            + chr(10))


def _write_diagnostics(attempt, manifest, profile, blockers, jpath, mpath):
    """What a failed attempt is allowed to leave: reasons, never artifacts."""
    lines = ["UNSAFE DIAGNOSTIC OUTPUT - NOT A RELEASE", "",
             "board: " + manifest.board_id,
             "release profile: " + str(profile.get("id")),
             "attempt: " + attempt.id, "",
             "No fabrication archive was published. This attempt's build "
             "directory has been removed; any previously published release is "
             "untouched.", "", "blocking:"]
    for gate_id, status, why in blockers:
        lines.append("  {}: {} - {}".format(gate_id, status, why))
    lines += ["", "No orderable package was produced.", ""]
    with open(os.path.join(attempt.diagnostics, "DO_NOT_ORDER.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(chr(10).join(lines))
    if jpath:
        shutil.copy2(jpath, os.path.join(attempt.diagnostics,
                                         "validation.json"))
        shutil.copy2(mpath, os.path.join(attempt.diagnostics,
                                         "validation.md"))


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


def cmd_coherence(manifest_path):
    """Is the installed release package one run? Nonzero if not.

    The same check the release runs before publishing, pointed at whatever is
    installed now. Worth running on its own because a package can be made
    incoherent long after it was published - by refreshing one report, or by
    keeping one file from a previous release - and nothing else notices.
    """
    from pcbqa import coherence
    from pcbqa.core import ManifestError

    try:
        manifest, _layout = open_board(manifest_path)
    except (ManifestError, Exception) as exc:                # noqa: BLE001
        return _refuse(exc)
    names = coherence.member_names(manifest)
    root = os.path.dirname(manifest.resolve(manifest.get("archive.zip")))
    print("package: " + root)
    problems, facts = coherence.check(root, names)
    for key, value in sorted(facts.items()):
        print("  {:34s} {}".format(key, value))
    for problem in problems:
        print("  INCOHERENT  {}: {}".format(problem.get("file", "package"),
                                            problem["issue"]))
    if problems:
        print(chr(10) + "{} incoherence(s): this directory is not one "
                        "release.".format(len(problems)))
        return 1
    print(chr(10) + "Coherent: every file here came from one clean-room run.")
    return 0


def main(argv):
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
    if cmd == "fab":
        from pcbqa.fabricators import cli as fab_cli
        return fab_cli.main(argv[2:])
    if cmd in ("validate", "release", "coherence"):
        if len(argv) < 3:
            print("usage: run.py {} <manifest.json>".format(cmd))
            return 2
        path = _find_manifest(argv[2])
        if cmd == "validate":
            return cmd_validate(path)[0]
        if cmd == "coherence":
            return cmd_coherence(path)
        return cmd_release(path)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
