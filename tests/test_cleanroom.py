"""Integration tests for the clean-room release.

The property under test is narrow and important: a release verdict must depend
only on artifacts the release run generated itself. Each test breaks that in one
specific way and checks the run notices - or, for the first one, checks that it
correctly does *not* care.

These are slow (each build runs ERC, DRC and four exports), so they are written
as independently addressable methods that the parallel runner can spread across
workers.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                                    # noqa: E402
from pcbqa import cleanroom, core                       # noqa: E402
from pcbqa.core import Context, Manifest, Status        # noqa: E402
from pcbqa.gates import (g_provenance, g_checks, g_geometry,   # noqa: E402,F401
                         g_contracts, g_assembly, g_export_parity)

REVA = paths.REVA_MANIFEST
FIXTURE = paths.REVA_PROJECT


def _origin(tag, mutate=None):
    """A writable copy of the frozen project, plus a manifest naming it.

    The frozen fixture is never touched: a mutation test that edited it would
    corrupt the one thing the whole suite is anchored to.
    """
    work = tempfile.mkdtemp(prefix="pcbqa_cr_" + tag + "_")
    project = os.path.join(work, "project")
    shutil.copytree(FIXTURE, project)
    doc = json.load(open(REVA, encoding="utf-8"))
    doc["project_root"] = project
    # The copy is not the frozen inventory, but canonical hashing still
    # needs the .gitattributes policy.
    doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
    if mutate:
        mutate(doc, project)
    path = os.path.join(work, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return work, project, path


def _build(manifest_path, work):
    """A clean-room run with an explicit build directory it owns."""
    manifest = core.load_manifest(manifest_path)
    ctx = Context(manifest, os.path.join(work, "driver"))
    run = cleanroom.CleanRun(ctx, os.path.join(work, "clean_run"),
                             os.path.join(work, "build"))
    derived = run.build()
    return run, derived


def _validate(derived, run, only):
    ctx = Context(derived, os.path.join(run.root, "validation_" + "_".join(
        sorted(only))[:40]))
    return {r.gate_id: r for r in core.run_all(ctx, only=only)}


class CleanRoomIsolation(unittest.TestCase):
    """Nothing that was already in the tree may reach the verdict."""

    def setUp(self):
        self._work = []

    def tearDown(self):
        for path in self._work:
            shutil.rmtree(path, ignore_errors=True)

    def _origin(self, tag, mutate=None):
        work, project, path = _origin(tag, mutate)
        self._work.append(work)
        return work, project, path

    # -- 1: a corrupted pre-existing artifact must be irrelevant -----------
    def test_a_corrupt_pre_existing_artifact_is_ignored(self):
        """The old packaged zip is deleted, not read: corrupting it changes nothing."""
        def corrupt(_doc, project):
            zpath = os.path.join(project, "generated", "release",
                                 "microphone_array_v2-revA-fabrication.zip")
            with open(zpath, "wb") as fh:
                fh.write(b"this is not a zip file at all")
            gerber = os.path.join(project, "generated", "release", "gerbers",
                                  "microphone_array_v2-F_Cu.gbr")
            with open(gerber, "w", encoding="utf-8") as fh:
                fh.write("G04 deliberately destroyed*\n")

        work, project, path = self._origin("corrupt", corrupt)
        run, derived = _build(path, work)
        self.assertFalse(run.blockers, run.blockers)

        # The corrupted files were purged, and nothing authoritative points at
        # the directory they lived in.
        self.assertTrue(
            os.path.isdir(os.path.join(project, "generated", "release",
                                       "gerbers")),
            "the purge must happen in the copy; the origin is read-only input")
        for entry in run.summary()["authoritative_paths"]:
            # The attempt owns two roots: the output tree, and the staging area
            # the release package is assembled in before anything has passed.
            self.assertTrue(
                run.owns(entry["path"]),
                "{} escaped the clean run".format(entry["key"]))

        results = _validate(derived, run, {"ARCH.CONTENTS", "STACK.GERBER_PARITY"})
        for gate_id, result in results.items():
            self.assertEqual(result.status, Status.PASS,
                             "{} read a corrupted artifact that the clean run "
                             "regenerated: {}".format(gate_id, result.reason))

    # -- 2: a newly generated artifact altered after the fact must fail -----
    def test_altering_a_newly_generated_artifact_fails(self):
        work, _project, path = self._origin("altered")
        run, derived = _build(path, work)
        # The layer as the package carries it, which is what would be shipped
        # and what the gates read - not the export directory it was copied
        # from. Tampering has to be done to the artifact under validation.
        from pcbqa.gates.g_contracts import _classify
        shipped = derived.resolve(derived.get("artifacts.gerber_dir"))
        target = None
        for name in sorted(os.listdir(shipped)):
            path = os.path.join(shipped, name)
            with open(path, "rb") as fh:
                _kind, function, _empty = _classify(name, fh.read())
            if function.startswith("Copper,L2"):
                target = path
                break
        self.assertIsNotNone(target, os.listdir(shipped))
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("X150000000Y150000000D03*\n")      # one extra flash

        results = _validate(derived, run, {"STACK.GERBER_PARITY"})
        gate = results["STACK.GERBER_PARITY"]
        self.assertEqual(gate.status, Status.FAIL,
                         "a candidate copper layer changed after generation "
                         "must not validate")

    # -- 3: an input pointed outside the clean run must fail ----------------
    def test_an_input_outside_the_clean_run_is_refused(self):
        work, project, path = self._origin("escape")
        run, derived = _build(path, work)
        # Repoint one authoritative path back at the original project.
        doc = json.load(open(run.manifest_path, encoding="utf-8"))
        doc["artifacts"]["gerber_dir"] = os.path.join(
            project, "generated", "release", "gerbers")
        with open(run.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        escaped = Manifest(run.manifest_path)

        run.blockers = []
        problems = run.assert_isolated(escaped)
        self.assertTrue(problems, "an artifact path outside the run went "
                                  "unnoticed")
        self.assertTrue(any(p["key"] == "artifacts.gerber_dir" for p in problems),
                        problems)
        self.assertTrue(run.blockers, "the escape must block the release")

    # -- 4: a mandatory artifact that was never generated must block --------
    def test_omitting_the_bom_blocks_a_mandatory_gate(self):
        work, _project, path = self._origin("nobom")
        run, derived = _build(path, work)
        bom = os.path.join(run.release, run.cfg["bom"]["output"])
        cpl = os.path.join(run.release, run.cfg["cpl"]["output"])
        self.assertTrue(os.path.isfile(bom) and os.path.isfile(cpl))
        os.unlink(bom)
        os.unlink(cpl)

        results = _validate(derived, run, {"BOM.NATIVE_PARITY", "CPL.NATIVE_PARITY"})
        for gate_id in ("BOM.NATIVE_PARITY", "CPL.NATIVE_PARITY"):
            self.assertIn(results[gate_id].status, Status.BLOCKING,
                          "{} must block when the artifact was never "
                          "generated".format(gate_id))
        mandatory = json.load(open(REVA, encoding="utf-8"))["release_profile"][
            "mandatory_gates"]
        self.assertIn("BOM.NATIVE_PARITY", mandatory)
        self.assertIn("CPL.NATIVE_PARITY", mandatory)

    # -- 5: a layer nobody approved must fail archive validation ------------
    def test_a_disallowed_fabrication_layer_fails_archive_validation(self):
        work, _project, path = self._origin("courtyard")
        run, derived = _build(path, work)
        # A courtyard layer is documentation, not fabrication data. Put one in
        # the archive the way a careless export step would.
        source = os.path.join(run.gerbers, "microphone_array_v2-F_Cu.gbr")
        with open(source, encoding="utf-8") as fh:
            body = fh.read()
        body = body.replace("Copper,L1,Top", "Courtyard,Top")
        zpath = os.path.join(run.release, run.cfg["archive"]["zip"])
        with zipfile.ZipFile(zpath, "a", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("microphone_array_v2-F_Courtyard.gbr", body)

        results = _validate(derived, run, {"ARCH.CONTENTS"})
        gate = results["ARCH.CONTENTS"]
        self.assertEqual(gate.status, Status.FAIL,
                         "a courtyard layer must not be orderable fabrication "
                         "data")
        self.assertTrue(
            any("allowlist" in f.get("issue", "") for f in gate.findings),
            gate.findings)

    # -- and the allowlist must also refuse it at packaging time ------------
    def test_packaging_refuses_an_unapproved_layer(self):
        work, _project, path = self._origin("packaging")
        run, _derived = _build(path, work)
        source = os.path.join(run.gerbers, "microphone_array_v2-F_Cu.gbr")
        with open(source, encoding="utf-8") as fh:
            body = fh.read().replace("Copper,L1,Top", "Courtyard,Top")
        with open(os.path.join(run.gerbers, "extra-F_Courtyard.gbr"), "w",
                  encoding="utf-8") as fh:
            fh.write(body)

        run.blockers = []
        run.package()
        self.assertTrue(
            any(b[0] == "release:fabrication_allowlist" for b in run.blockers),
            run.blockers)
        with zipfile.ZipFile(os.path.join(run.release,
                                          run.cfg["archive"]["zip"])) as zf:
            self.assertNotIn("extra-F_Courtyard.gbr", zf.namelist())


class TheLockScanLooksOnlyAtTheDesign(unittest.TestCase):
    """A lock file means KiCad may have the DESIGN open, so the bytes
    on disk are not necessarily the design and the release refuses.
    That reasoning does not reach into directories that hold no
    design - and one of them now ships with the toolkit, whose Rust
    build tree leaves a `.cargo-lock` that the `*-lock` glob written
    for `*.kicad_prl-lock` happily matches."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="pcbqa_lockscan_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.globs = ["*.lck", "~*.lck", ".#*", "*-lock",
                      "*.kicad_prl-lock"]

    def _touch(self, *parts):
        full = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write("")
        return full

    def test_a_lock_beside_the_design_is_still_found(self):
        self._touch("~board.kicad_pro.lck")
        self._touch("sub", "board.kicad_prl-lock")
        self.assertEqual(
            cleanroom._find(self.root, self.globs),
            ["sub/board.kicad_prl-lock", "~board.kicad_pro.lck"])

    def test_a_lock_inside_a_never_copied_directory_is_not(self):
        for directory in core.NEVER_COPY:
            self._touch(directory, "rust_router", "target", ".cargo-lock")
            self._touch(directory, "stale.lck")
        self.assertEqual(cleanroom._find(self.root, self.globs), [],
                         "a directory that holds no design cannot "
                         "hold a finding about one")

    def test_the_vendored_router_specifically_cannot_block_a_release(self):
        """The case that actually happened: Rust's build lock, at the
        depth the submodule really sits at, under a name the KiCad
        glob matches."""
        self._touch("tooling", "PCBA_AutoDesignAndTest", "tooling",
                    "KiCadRoutingTools", "rust_router", "target",
                    "release", ".cargo-lock")
        self.assertEqual(cleanroom._find(self.root, self.globs), [])
        # ...while a real one at the same depth still blocks.
        self._touch("tooling", "PCBA_AutoDesignAndTest", "tooling",
                    "elsewhere", "board.kicad_prl-lock")
        self.assertEqual(
            cleanroom._find(self.root, self.globs),
            ["tooling/PCBA_AutoDesignAndTest/tooling/elsewhere/"
             "board.kicad_prl-lock"])


class CleanRoomHygiene(unittest.TestCase):
    """Preparation refuses to work from a tree it cannot trust."""

    def setUp(self):
        self._work = []

    def tearDown(self):
        for path in self._work:
            shutil.rmtree(path, ignore_errors=True)

    def test_a_lock_file_refuses_the_release(self):
        """KiCad holding the project open means the bytes may not be the design."""
        def add_lock(_doc, project):
            with open(os.path.join(project,
                                   "~microphone_array_v2.kicad_pro.lck"),
                      "w", encoding="utf-8") as fh:
                fh.write('{"pid": 1234}')

        work, _project, path = _origin("lock", add_lock)
        self._work.append(work)
        manifest = core.load_manifest(path)
        ctx = Context(manifest, os.path.join(work, "driver"))
        run = cleanroom.CleanRun(ctx, os.path.join(work, "clean_run"),
                                 os.path.join(work, "build"))
        with self.assertRaises(cleanroom.CleanRoomError):
            run.isolate()
        self.assertTrue(any(b[0] == "release:lock_files" for b in run.blockers),
                        run.blockers)
        self.assertFalse(os.path.isdir(run.project),
                         "nothing may be copied out of a locked project")

    def test_previous_output_is_purged_before_anything_is_generated(self):
        work, _project, path = _origin("purge")
        self._work.append(work)
        manifest = core.load_manifest(path)
        ctx = Context(manifest, os.path.join(work, "driver"))
        run = cleanroom.CleanRun(ctx, os.path.join(work, "clean_run"),
                                 os.path.join(work, "build"))
        run.isolate()
        self.assertTrue(run.removed, "the fixture ships generated output; the "
                                     "clean run must have removed it")
        leftovers = cleanroom._find(run.project, run.cfg["purge_globs"])
        self.assertEqual(leftovers, [],
                         "generated output survived into the clean copy")
        self.assertTrue(os.path.isfile(os.path.join(
            run.project, manifest.get("sources.pcb"))),
            "the purge must not take the design with it")


if __name__ == "__main__":
    unittest.main()
