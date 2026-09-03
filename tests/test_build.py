"""Generating the fabrication outputs a release commit carries.

A build runs KiCad against a private copy and installs the result into the
paths the manifest declares. Two properties matter and both are tested by
breaking them: the design itself is never written to, and a build that could
not produce a complete set installs none of it.

These are slow - each build runs ERC, DRC and four exports - so they are
written as independently addressable methods the parallel runner can spread.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run as run_cli                                             # noqa: E402
from tests import paths                                           # noqa: E402
from pcbqa import artifacts, build as build_mod, core             # noqa: E402
from pcbqa.core import Context, Status                            # noqa: E402
from pcbqa.gates import (g_provenance, g_checks, g_geometry,      # noqa: E402,F401
                         g_contracts, g_assembly, g_export_parity)
from pcbqa.parallel import ENV_OUTPUT_ROOT                        # noqa: E402

REVA = paths.REVA_MANIFEST
FIXTURE = paths.REVA_PROJECT

RELEASE = "generated/release"


def digest_tree(root, skip=()):
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(rel.startswith(s) for s in skip):
                continue
            with open(full, "rb") as fh:
                out[rel] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _origin(tag, mutate=None):
    """A writable copy of the frozen project, plus a manifest naming it.

    The frozen fixture is never touched: a mutation test that edited it would
    corrupt the one thing the whole suite is anchored to.
    """
    work = tempfile.mkdtemp(prefix="pcbqa_build_" + tag + "_")
    project = os.path.join(work, "project")
    shutil.copytree(FIXTURE, project)
    with open(REVA, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["board_id"] = "build-" + tag
    doc["project_root"] = project
    # The copy is not the frozen inventory, but canonical hashing still needs
    # the .gitattributes policy.
    doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
    doc["artifacts"]["reports_dir"] = RELEASE + "/reports"
    doc["artifacts"]["validation_report"] = RELEASE + "/validation.json"
    doc["reports"]["files"] = [RELEASE + "/reports/*.json"]
    doc["reports"]["required_steps"] = ["erc", "drc"]
    if mutate:
        mutate(doc, project)
    path = os.path.join(work, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return work, project, path


class _Base(unittest.TestCase):
    def setUp(self):
        self._work = []

    def tearDown(self):
        for path in self._work:
            shutil.rmtree(path, ignore_errors=True)

    def origin(self, tag, mutate=None):
        work, project, path = _origin(tag, mutate)
        self._work.append(work)
        return work, project, path

    def build(self, manifest_path, work):
        """Run the command exactly as the CLI does, capturing its output."""
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = work
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = run_cli.cmd_build(manifest_path)
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved
        return code, captured.getvalue()


class ABuildInstallsIntoTheTree(_Base):

    def test_it_installs_exactly_what_it_recorded(self):
        work, project, path = self.origin("install")
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])

        manifest = core.load_manifest(path)
        record_path = os.path.join(project, RELEASE, "fabrication.json")
        self.assertTrue(os.path.isfile(record_path), output[-2000:])
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)

        present = {artifacts.record_key(manifest, p)
                   for p in artifacts.generated_files(manifest)}
        self.assertEqual(sorted(record["artifacts"]), sorted(present),
                         "the record and the release directory disagree about "
                         "which files the build produced")
        for name, digest in record["artifacts"].items():
            with open(os.path.join(project, RELEASE, name), "rb") as fh:
                self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest,
                                 name)

    def test_the_record_binds_the_design_it_was_generated_from(self):
        from pcbqa import closure
        work, project, path = self.origin("binding")
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])
        with open(os.path.join(project, RELEASE, "fabrication.json"),
                  encoding="utf-8") as fh:
            record = json.load(fh)
        entries, now = closure.current(core.load_manifest(path))
        self.assertEqual(record["source_closure_sha256"], now)
        # The member map travels with the record, so a later mismatch can
        # name which input moved instead of printing two digests.
        self.assertEqual(record["source_closure"], entries)

    def test_the_record_names_no_machine_and_no_checkout(self):
        """Two developers building one design must record the same provenance."""
        work, project, path = self.origin("portable")
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])
        with open(os.path.join(project, RELEASE, "fabrication.json"),
                  encoding="utf-8") as fh:
            body = fh.read()
        for absolute in (work, project, HERE, os.path.sep + "usr"):
            self.assertNotIn(absolute, body,
                             "the fabrication record names a path on this "
                             "machine")

    def test_the_design_itself_is_never_written_to(self):
        """DRC refills zones and saves the board. Not this board."""
        work, project, path = self.origin("readonly")
        before = digest_tree(project, skip=(RELEASE,))
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])
        self.assertEqual(digest_tree(project, skip=(RELEASE,)), before,
                         "a build modified the design it was generating from")

    def test_a_file_a_previous_build_left_behind_is_removed(self):
        work, project, path = self.origin("prune")
        stray = os.path.join(project, RELEASE, "gerbers", "microphone_array_v2.OLD")
        os.makedirs(os.path.dirname(stray), exist_ok=True)
        with open(stray, "w", encoding="utf-8") as fh:
            fh.write("G04 a layer an earlier export produced*\n")
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])
        self.assertFalse(os.path.exists(stray),
                         "a stale export survived into the new release set")

    def test_a_corrupt_pre_existing_artifact_is_irrelevant(self):
        """The outputs are regenerated, not read: corrupting them changes nothing."""
        def corrupt(_doc, project):
            release = os.path.join(project, RELEASE)
            with open(os.path.join(
                    release, "microphone_array_v2-revA-fabrication.zip"),
                    "wb") as fh:
                fh.write(b"this is not a zip file at all")
            with open(os.path.join(release, "bom.csv"), "w",
                      encoding="utf-8") as fh:
                fh.write("destroyed\n")

        work, project, path = self.origin("corrupt", corrupt)
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])
        manifest = core.load_manifest(path)
        ctx = Context(manifest, os.path.join(work, "validation"))
        results = {r.gate_id: r for r in core.run_all(
            ctx, only={"ARCH.CONTENTS", "ARCH.PROVENANCE"})}
        for gate_id, result in results.items():
            self.assertEqual(result.status, Status.PASS,
                             "{}: {}".format(gate_id, result.reason))


class ABlockedBuildInstallsNothing(_Base):

    def _unchanged(self, project, before, output):
        self.assertEqual(digest_tree(os.path.join(project, RELEASE)), before,
                         "a blocked build installed something anyway:\n"
                         + output[-2000:])

    def test_a_lock_file_beside_the_design_refuses(self):
        def plant(_doc, project):
            with open(os.path.join(project, "microphone_array_v2.kicad_prl-lock"),
                      "w", encoding="utf-8") as fh:
                fh.write("held")

        work, project, path = self.origin("lock", plant)
        before = digest_tree(os.path.join(project, RELEASE))
        code, output = self.build(path, work)
        self.assertNotEqual(code, 0)
        self.assertIn("lock file", output)
        self._unchanged(project, before, output)

    def test_a_lock_inside_a_never_copied_directory_does_not(self):
        """`*-lock` is a KiCad glob; Rust leaves a .cargo-lock in the router."""
        def plant(_doc, project):
            router = os.path.join(project, "KiCadRoutingTools", "target")
            os.makedirs(router)
            with open(os.path.join(router, ".cargo-lock"), "w",
                      encoding="utf-8") as fh:
                fh.write("")

        work, project, path = self.origin("cargolock", plant)
        code, output = self.build(path, work)
        self.assertEqual(code, 0, output[-3000:])

    def test_a_generation_failure_installs_nothing(self):
        work, project, path = self.origin("genfail")
        before = digest_tree(os.path.join(project, RELEASE))
        real = build_mod.Build.generate

        def broken(self):
            self.blockers.append(("generate:erc", "ERROR", "injected"))

        build_mod.Build.generate = broken
        try:
            code, output = self.build(path, work)
        finally:
            build_mod.Build.generate = real
        self.assertNotEqual(code, 0)
        self.assertIn("BUILD BLOCKED", output)
        self._unchanged(project, before, output)

    def test_a_disallowed_fabrication_layer_installs_nothing(self):
        def deny_copper(doc, _project):
            doc["archive"]["allow"] = [
                entry for entry in doc["archive"]["allow"]
                if entry.get("file_function") != "Soldermask,Top"]

        work, project, path = self.origin("allowlist", deny_copper)
        before = digest_tree(os.path.join(project, RELEASE))
        code, output = self.build(path, work)
        self.assertNotEqual(code, 0)
        self.assertIn("allowlist", output)
        self._unchanged(project, before, output)

    def test_an_unreviewed_placement_angle_installs_nothing(self):
        def drop_entry(doc, _project):
            spec = doc["release_generation"].get("cpl_orientation")
            if not spec:
                raise unittest.SkipTest("fixture declares no orientation registry")
            spec["registry"] = spec["registry"][:-1]

        try:
            work, project, path = self.origin("orient", drop_entry)
        except unittest.SkipTest:
            self.skipTest("fixture declares no orientation registry")
        before = digest_tree(os.path.join(project, RELEASE))
        code, output = self.build(path, work)
        self.assertNotEqual(code, 0)
        self._unchanged(project, before, output)


class InstallationCannotEscapeTheProject(_Base):

    def test_a_destination_outside_the_project_is_refused(self):
        work, project, path = self.origin("escape")
        manifest = core.load_manifest(path)
        builder = build_mod.Build(Context(manifest, os.path.join(work, "w")),
                                  os.path.join(work, "b"))
        for hostile in (os.path.join(project, "..", "victim"), "/etc/passwd",
                        project, os.path.sep):
            with self.subTest(path=hostile):
                with self.assertRaises(build_mod.BuildError):
                    builder._installable(hostile)
        inside = builder._installable(os.path.join(project, RELEASE, "x.gbr"))
        self.assertTrue(inside.startswith(os.path.realpath(project)))


if __name__ == "__main__":
    unittest.main()
