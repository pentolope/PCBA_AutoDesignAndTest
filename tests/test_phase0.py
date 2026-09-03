"""Program-enabler behavior: identity refusals, gate selection, manifest
schema preflight, build coherence, workspace holds, addressable findings,
the gates library, and the check-board preflight."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                                     # noqa: E402

from tests import paths, synth                                    # noqa: E402
from pcbqa import closure, core, geom, manifest_schema            # noqa: E402
from pcbqa import build as build_mod                              # noqa: E402
from pcbqa.core import Context, GateResult, Manifest, ManifestError  # noqa: E402
from pcbqa.layout import LayoutError, Workspace                   # noqa: E402

PYTHON = sys.executable
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{4}-[0-9a-f]{12}$")
HEX64 = "0" * 64


def minimal_manifest(extra=None, project_root="."):
    doc = {
        "schema_version": 2,
        "board_id": "phase0_case",
        "constraint_version": "v1",
        "project_root": project_root,
        "tools": {"kicad_cli": "kicad-cli"},
        "sources": {"pcb": "case.kicad_pcb"},
        "board_origin_mm": [0.0, 0.0],
        "documentation_globs": [],
        "waivers": [],
    }
    if extra:
        doc.update(extra)
    return doc


def write_manifest(doc, directory):
    path = os.path.join(directory, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    return path


class ImplementationIdentityRefuses(unittest.TestCase):
    """Git being unable to answer is a refusal, never a clean tree."""

    def test_unknown_dirtiness_is_not_clean(self):
        record = {"commit": "a" * 40, "working_tree_dirty": None,
                  "detail": "unrecorded: git status failed: boom"}
        with mock.patch.object(core, "toolkit_identity", return_value=record):
            with self.assertRaisesRegex(closure.ClosureError, "dirty"):
                closure.implementation_identity()

    def test_failed_git_status_is_recorded_as_such(self):
        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "status" in args:
                proc = mock.Mock()
                proc.returncode = 128
                proc.stdout = ""
                proc.stderr = "fatal: not answering"
                return proc
            return real_run(args, **kwargs)

        with mock.patch("subprocess.run", side_effect=fake_run):
            record = core.toolkit_identity()
        self.assertIsNone(record["working_tree_dirty"])
        self.assertIn("git status failed", record["detail"])


class GateSelection(unittest.TestCase):
    """Selection tokens: classes, patterns, exact IDs; nothing silently empty."""

    @classmethod
    def setUpClass(cls):
        from pcbqa import gates
        gates.load()

    def test_every_gate_declares_a_valid_class(self):
        for entry in core.registered():
            self.assertIn(entry["class"], core.GATE_CLASSES, entry["id"])

    def test_classes_partition_the_registry(self):
        every = {entry["id"] for entry in core.registered()}
        union = set()
        for name in core.GATE_CLASSES:
            ids, unknown = core.select_gates([name])
            self.assertEqual(unknown, [])
            union |= set(ids)
        self.assertEqual(union, every)

    def test_design_excludes_release_artifact_gates(self):
        ids, _ = core.select_gates(["design"])
        self.assertIn("ROUTE.GEOMETRY_HYGIENE", ids)
        self.assertNotIn("ARCH.PROVENANCE", ids)
        self.assertNotIn("PROV.FIXTURE_INTEGRITY", ids)

    def test_patterns_and_exact_ids_expand_without_duplicates(self):
        ids, unknown = core.select_gates(
            ["ROUTE.*", "ROUTE.TINY_SEGMENTS", "ARCH.CONTENTS"])
        self.assertEqual(unknown, [])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("ROUTE.TINY_SEGMENTS", ids)
        self.assertIn("ARCH.CONTENTS", ids)

    def test_a_selector_that_names_nothing_is_reported(self):
        ids, unknown = core.select_gates(["NOPE.*", "bogus"])
        self.assertEqual(ids, [])
        self.assertEqual(unknown, ["NOPE.*", "bogus"])

    def test_an_unclassified_gate_cannot_register(self):
        with self.assertRaises(ValueError):
            core.gate("T.EST", "t", "no-such-class")


class ManifestSchemaPreflight(unittest.TestCase):
    """A key the toolkit does not implement is refused by name."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_schema_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_the_minimal_onboarding_manifest_loads(self):
        Manifest(write_manifest(minimal_manifest(), self.work))

    def test_an_unimplemented_top_level_key_is_refused_by_name(self):
        doc = minimal_manifest({"simulaton": {}})
        with self.assertRaisesRegex(ManifestError, "simulaton"):
            Manifest(write_manifest(doc, self.work))

    def test_a_misnested_timing_key_is_refused_where_it_sits(self):
        # The exact failure that cost board 03 a validation cycle: template
        # declared at the interface level instead of inside routes.
        doc = minimal_manifest({"timing": {"interfaces": {
            "iface_a": {"template": {"id": "t", "steps": []},
                        "routes": {"paths": []}}}}})
        with self.assertRaisesRegex(ManifestError, r"iface_a.*template"):
            Manifest(write_manifest(doc, self.work))

    def test_board_local_keys_live_under_the_extension_prefix(self):
        refused = minimal_manifest(
            {"routing": {"acceptance_gates": ["A.B"]}})
        with self.assertRaisesRegex(ManifestError, "acceptance_gates"):
            Manifest(write_manifest(refused, self.work))
        allowed = minimal_manifest(
            {"routing": {"x_acceptance_gates": ["A.B"],
                         "min_segment_mm": 0.1},
             "x_derived_from": {"anything": ["goes", 1, None]}})
        Manifest(write_manifest(allowed, self.work))

    def test_annotations_are_strings(self):
        doc = minimal_manifest({"routing": {"note": ["not", "a", "string"]}})
        with self.assertRaisesRegex(ManifestError, "note"):
            Manifest(write_manifest(doc, self.work))
        doc = minimal_manifest({"routing": {"note": "prose is fine",
                                            "min_segment_mm": 0.1}})
        Manifest(write_manifest(doc, self.work))

    def test_a_wrongly_typed_value_is_refused(self):
        doc = minimal_manifest({"routing": {"min_segment_mm": "0.1"}})
        with self.assertRaisesRegex(ManifestError, "min_segment_mm"):
            Manifest(write_manifest(doc, self.work))

    def test_annotations_inside_an_open_key_map_are_data_and_refused(self):
        # `connector_gender_tokens` is iterated wholesale: a stripped `note`
        # would otherwise become a gender whose tokens are prose characters.
        for extra in ({"connector_gender_tokens": {"note": ["prose"]}},
                      {"connector_gender_tokens": {"note": "prose"}},
                      {"sources": {"x_spare": "/etc/passwd"}}):
            doc = minimal_manifest(extra)
            with self.assertRaisesRegex(ManifestError, "open-key map"):
                Manifest(write_manifest(doc, self.work))

    def test_non_finite_numbers_cannot_enter_the_manifest(self):
        # Even inside a subtree the schema types as an opaque object, where
        # the validator's finite check never visits.
        path = os.path.join(self.work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2, "board_id": "phase0_case", '
                     '"waivers": [{"limit": Infinity}]}')
        with self.assertRaisesRegex(ManifestError, "non-JSON constant"):
            Manifest(path)

    def test_a_misspelled_waiver_field_is_refused_not_ignored(self):
        # A waiver is the one manifest object that WEAKENS a check, so a
        # typo silently ignored is a waiver that never applies - or worse,
        # one that cannot be audited. Every field is enumerated.
        waiver = {"gate": "DRC.AUTHORITATIVE", "rule": "clearance",
                  "category": "design_rules", "reason": "reviewed",
                  "reviewed_by": "someone", "reviewed_utc": "2026-01-01",
                  "approved_source_sha256": "0" * 64,
                  "approved_rules_sha256": "0" * 64,
                  "approved_command_sha256": "0" * 64,
                  "approved_report_sha256": "0" * 64,
                  "items": [{"description": "item",
                             "location_mm": [1.0, 2.0]}]}
        Manifest(write_manifest(minimal_manifest({"waivers": [waiver]}),
                                self.work))
        for typo in ({"gaet": "DRC.AUTHORITATIVE"}, {"rul": "clearance"}):
            broken = dict(waiver)
            broken.update(typo)
            doc = minimal_manifest({"waivers": [broken]})
            with self.assertRaisesRegex(ManifestError,
                                        next(iter(typo))):
                Manifest(write_manifest(doc, self.work))

    def test_an_unread_leaf_the_schema_once_permitted_is_now_refused(self):
        for extra in ({"reports": {"tolerance_seconds": 0}},
                      {"via_mask": {"contact_semantics": "prose"}},
                      {"fixture": {"inventory_policy": "prose"}}):
            doc = minimal_manifest(extra)
            with self.assertRaises(ManifestError):
                Manifest(write_manifest(doc, self.work))


class BuildCoherence(unittest.TestCase):
    """A declared capability no build step would apply refuses the build."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_build_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def _manifest(self, release_generation):
        doc = minimal_manifest({"release_generation": release_generation})
        return Manifest(write_manifest(doc, self.work))

    def test_orientation_without_a_consuming_step_is_named(self):
        manifest = self._manifest({"cpl_orientation": {"registry": []}})
        pairs = build_mod.incoherent(manifest)
        needed = {need for _declared, need, _why in pairs}
        self.assertIn("release_generation.fab_format.cpl.columns", needed)
        self.assertIn(
            "release_generation.cpl_orientation.reproduction_inputs", needed)

    def test_a_coherent_declaration_raises_nothing(self):
        manifest = self._manifest({
            "cpl_orientation": {
                "registry": [],
                "reproduction_inputs": {"required_globs": []}},
            "fab_format": {"cpl": {
                "columns": [{"from": "Ref", "label": "Designator"},
                            {"from": "Rot", "label": "Rotation"}],
                "field_map": {"designator": "Designator",
                              "rotation": "Rotation"}}}})
        self.assertEqual(build_mod.incoherent(manifest), [])

    def test_a_format_without_the_offset_columns_is_incoherent(self):
        # Columns alone relabel the file; applying an offset also needs to
        # know which columns carry the designator and the rotation.
        manifest = self._manifest({
            "cpl_orientation": {
                "registry": [],
                "reproduction_inputs": {"required_globs": []}},
            "fab_format": {"cpl": {
                "columns": [{"from": "Ref", "label": "Designator"}]}}})
        needed = {need for _d, need, _w in build_mod.incoherent(manifest)}
        self.assertIn("release_generation.fab_format.cpl.field_map.designator",
                      needed)
        self.assertIn("release_generation.fab_format.cpl.field_map.rotation",
                      needed)

    def test_an_empty_fab_format_entry_is_refused_outright(self):
        # `fab_format.cpl: {}` used to pass the schema while the build's
        # format step skipped falsey rules - the T3 trap with one more door.
        doc = minimal_manifest({"release_generation": {
            "fab_format": {"cpl": {}}}})
        with self.assertRaisesRegex(ManifestError, "columns"):
            Manifest(write_manifest(doc, self.work))

    def test_the_build_refuses_before_touching_anything(self):
        manifest = self._manifest({"cpl_orientation": {"registry": []}})
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx")),
            os.path.join(self.work, "run"))
        with self.assertRaises(build_mod.BuildError):
            builder.run()
        self.assertTrue(any(step == "build:coherence"
                            for step, _status, _why in builder.blockers))
        self.assertTrue(any("fab_format" in why
                            for _step, _status, why in builder.blockers))


class OutputPathSafety(unittest.TestCase):
    """A declared output name can neither leave scratch nor destroy a source."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_outputs_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_an_absolute_artifact_path_is_refused_by_the_schema(self):
        for key, value in (("bom", "/tmp/evil.csv"),
                           ("gerber_dir", "C:/evil"),
                           ("fabrication_manifest", "\\\\host\\share\\f.json")):
            doc = minimal_manifest({"artifacts": {key: value}})
            with self.assertRaisesRegex(ManifestError, key):
                Manifest(write_manifest(doc, self.work))

    def test_a_scratch_output_cannot_escape_its_own_directory(self):
        doc = minimal_manifest({"release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx")),
            os.path.join(self.work, "run"))
        inside = builder._scratch(builder.reports, "drc.json", "drc output")
        self.assertTrue(inside.startswith(os.path.realpath(builder.reports)))
        # Not merely inside the run: `../bom.csv` from the gerber directory
        # would land on another step's staged output and be read back as if
        # that step's tool wrote it.
        for hostile in ("/etc/passwd", "../../../elsewhere.json",
                        "../bom.csv"):
            with self.assertRaises(build_mod.BuildError):
                builder._scratch(builder.gerbers, hostile, "fab file name")

    def test_two_steps_cannot_declare_one_output_file(self):
        doc = minimal_manifest({
            "sources": {"pcb": "case.kicad_pcb",
                        "schematic": "case.kicad_sch"},
            "artifacts": {"gerber_export_flags": []},
            "release_generation": {
                "erc": {"output": "erc.json"}, "drc": {"output": "drc.json"},
                "drill": {"flags": []},
                "cpl": {"output": "same.csv", "flags": []},
                "bom": {"output": "same.csv", "fields": [], "labels": [],
                        "group_by": [], "flags": []},
                "archive": {"zip": "fab.zip"}, "lock_file_globs": []}})
        manifest = Manifest(write_manifest(doc, self.work))
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx")),
            os.path.join(self.work, "run"))
        with self.assertRaisesRegex(build_mod.BuildError, "same output"):
            builder.generate()
        # The archive is a declared output like any other: a zip named after
        # the BOM would truncate it and ship an incomplete set as success.
        doc["release_generation"]["bom"]["output"] = "bom.csv"
        doc["release_generation"]["archive"]["zip"] = "bom.csv"
        manifest = Manifest(write_manifest(doc, self.work))
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx2")),
            os.path.join(self.work, "run2"))
        with self.assertRaisesRegex(build_mod.BuildError, "same output"):
            builder.declared_outputs()

    def test_a_symlinked_input_inside_an_install_directory_is_protected(self):
        # `_prune` unlinks lexically; the guard must therefore protect the
        # lexical spelling too, not only what the symlink resolves to.
        target = os.path.join(self.work, "real.kicad_pcb")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("(kicad_pcb)")
        os.makedirs(os.path.join(self.work, "out"))
        os.symlink(target, os.path.join(self.work, "out", "case.kicad_pcb"))
        doc = minimal_manifest({
            "sources": {"pcb": "out/case.kicad_pcb"},
            "artifacts": {"gerber_dir": "out"}})
        manifest = Manifest(write_manifest(doc, self.work))
        hits = build_mod.clobbered_inputs(manifest)
        self.assertIn(("gerber_dir", "out/case.kicad_pcb"), hits)

    def test_validate_write_refuses_escapes_and_design_inputs(self):
        import run as run_cli
        with open(os.path.join(self.work, "case.kicad_pcb"), "w",
                  encoding="utf-8") as fh:
            fh.write("(kicad_pcb)")
        manifest = Manifest(write_manifest(minimal_manifest(), self.work))
        self.assertIn("outside the project", run_cli._unwritable(
            manifest, os.path.join(self.work, "..", "escape.json")))
        self.assertIn("design input", run_cli._unwritable(
            manifest, os.path.join(self.work, "case.kicad_pcb")))
        self.assertIsNone(run_cli._unwritable(
            manifest, os.path.join(self.work, "generated", "v.json")))

    def test_a_destination_that_is_a_design_input_refuses_the_build(self):
        with open(os.path.join(self.work, "case.kicad_pcb"), "w",
                  encoding="utf-8") as fh:
            fh.write("(kicad_pcb)")
        doc = minimal_manifest({
            "artifacts": {"bom": "case.kicad_pcb",
                          "fabrication_manifest": "fab.json"},
            "release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        hits = build_mod.clobbered_inputs(manifest)
        self.assertEqual([role for role, _rel in hits], ["bom"])
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx")),
            os.path.join(self.work, "run"))
        with self.assertRaises(build_mod.BuildError):
            builder.run()
        self.assertTrue(any(step == "build:destinations"
                            for step, _s, _w in builder.blockers))

    def test_an_install_directory_holding_a_design_input_is_refused(self):
        # The prune step deletes whatever the build did not produce, so a
        # design input inside gerber_dir would be destroyed on install.
        with open(os.path.join(self.work, "case.kicad_pcb"), "w",
                  encoding="utf-8") as fh:
            fh.write("(kicad_pcb)")
        doc = minimal_manifest({"artifacts": {"gerber_dir": "."}})
        manifest = Manifest(write_manifest(doc, self.work))
        hits = build_mod.clobbered_inputs(manifest)
        self.assertIn(("gerber_dir", "case.kicad_pcb"), hits)


class CandidateBindings(unittest.TestCase):
    """A candidate run binds hashes to the candidate, not the declared board."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_bindings_")
        self.addCleanup(shutil.rmtree, self.work, True)
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.save(board, os.path.join(self.work, "case.kicad_pcb"))
        synth.add_track(board, (100, 100), (105, 100), net=net)
        self.candidate = synth.save(
            board, os.path.join(self.work, "candidate.kicad_pcb"))
        with open(os.path.join(self.work, "case.kicad_pro"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        doc = minimal_manifest({
            "sources": {"pcb": "case.kicad_pcb", "project": "case.kicad_pro"},
            "geometry_profile": {"tolerances": {
                "waiver_location_mm": {"value": 0.1, "units": "mm"}}},
        })
        self.manifest = write_manifest(doc, self.work)

    def test_drc_hashes_the_board_it_actually_judged(self):
        from pcbqa import gates
        from pcbqa.core import sha256_file
        result = gates.evaluate(self.manifest, only="DRC.AUTHORITATIVE",
                                board_path=self.candidate)[0]
        recorded = result.measurements.get("source_sha256")
        self.assertEqual(recorded, sha256_file(self.candidate),
                         result.reason)
        self.assertNotEqual(
            recorded,
            sha256_file(os.path.join(self.work, "case.kicad_pcb")))
        self.assertEqual(recorded,
                         result.measurements.get("checked_copy_sha256"))


class WorkspaceHold(unittest.TestCase):
    """Tree-writing commands hold the workspace; stale holds are broken."""

    def setUp(self):
        base = tempfile.mkdtemp(prefix="pcbqa_phase0_hold_")
        self.addCleanup(shutil.rmtree, base, True)
        self.workspace = Workspace("phase0_case", base)

    def test_a_second_holder_is_refused_by_name(self):
        with self.workspace.hold("first"):
            with self.assertRaisesRegex(LayoutError, "first"):
                with self.workspace.hold("second"):
                    pass

    def test_release_makes_way(self):
        with self.workspace.hold("first"):
            pass
        with self.workspace.hold("second"):
            pass

    def test_a_dead_holder_on_this_host_is_broken(self):
        gone = subprocess.Popen([PYTHON, "-c", "pass"])
        gone.wait()
        import socket
        os.makedirs(self.workspace.board, exist_ok=True)
        with open(os.path.join(self.workspace.board, ".hold"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pid": gone.pid, "host": socket.gethostname(),
                       "purpose": "crashed"}, fh)
        with self.workspace.hold("takeover"):
            pass

    def test_an_unreadable_hold_does_not_wedge_forever(self):
        os.makedirs(self.workspace.board, exist_ok=True)
        with open(os.path.join(self.workspace.board, ".hold"), "w",
                  encoding="utf-8") as fh:
            fh.write("not json")
        with self.workspace.hold("recover"):
            pass

    def test_a_hold_is_never_observable_empty(self):
        # An unreadable hold counts as abandoned, so the claim must appear
        # atomically with its content or a live hold can be broken mid-claim.
        with self.workspace.hold("atomic"):
            with open(os.path.join(self.workspace.board, ".hold"),
                      encoding="utf-8") as fh:
                record = json.load(fh)
            self.assertEqual(record["pid"], os.getpid())

    def test_losing_the_break_race_is_a_refusal_not_a_traceback(self):
        gone = subprocess.Popen([PYTHON, "-c", "pass"])
        gone.wait()
        import socket
        os.makedirs(self.workspace.board, exist_ok=True)
        with open(os.path.join(self.workspace.board, ".hold"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pid": gone.pid, "host": socket.gethostname(),
                       "purpose": "crashed"}, fh)
        from pcbqa import layout as layout_mod
        with mock.patch.object(layout_mod.os, "link",
                               side_effect=FileExistsError):
            with self.assertRaisesRegex(LayoutError, "another writer"):
                with self.workspace.hold("racer"):
                    pass

    def test_two_breakers_serialize_on_the_break_lock(self):
        # Both observe the same stale hold; without the break lock the
        # second breaker unlinks the first one's freshly installed LIVE
        # hold and both write into one tree.
        gone = subprocess.Popen([PYTHON, "-c", "pass"])
        gone.wait()
        import socket
        os.makedirs(self.workspace.board, exist_ok=True)
        with open(os.path.join(self.workspace.board, ".hold"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pid": gone.pid, "host": socket.gethostname(),
                       "purpose": "crashed"}, fh)
        with open(os.path.join(self.workspace.board, ".hold-break"), "w",
                  encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "host": socket.gethostname(),
                       "purpose": "breaking a stale hold"}, fh)
        with self.assertRaisesRegex(LayoutError, "already breaking"):
            with self.workspace.hold("second-breaker"):
                pass
        os.unlink(os.path.join(self.workspace.board, ".hold-break"))

    def test_a_dead_breakers_lock_is_cleared_and_the_break_proceeds(self):
        gone = subprocess.Popen([PYTHON, "-c", "pass"])
        gone.wait()
        import socket
        os.makedirs(self.workspace.board, exist_ok=True)
        for name in (".hold", ".hold-break"):
            with open(os.path.join(self.workspace.board, name), "w",
                      encoding="utf-8") as fh:
                json.dump({"pid": gone.pid, "host": socket.gethostname(),
                           "purpose": "crashed"}, fh)
        with self.workspace.hold("recover"):
            pass
        self.assertFalse(os.path.exists(
            os.path.join(self.workspace.board, ".hold-break")))

    def test_release_leaves_a_hold_that_is_no_longer_ours(self):
        import socket
        hold = self.workspace.hold("mine").__enter__()
        foreign = {"pid": 1, "host": socket.gethostname(),
                   "purpose": "replacement"}
        with open(os.path.join(self.workspace.board, ".hold"), "w",
                  encoding="utf-8") as fh:
            json.dump(foreign, fh)
        hold.__exit__(None, None, None)
        with open(os.path.join(self.workspace.board, ".hold"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), foreign)


class AddressableFindings(unittest.TestCase):
    """A geometry finding names its item and its board-frame location."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_findings_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def _project(self, dangling):
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_pad_footprint(board, "P2", 110, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_track(board, (100, 100), (110, 100), net=net)
        if dangling:
            synth.add_track(board, (100, 105), (105, 105), net=net)
        return synth.save(board, os.path.join(self.work, "case.kicad_pcb"))

    def _context(self, origin):
        doc = minimal_manifest({"routing": {"hygiene": {}},
                                "board_origin_mm": origin})
        manifest = Manifest(write_manifest(doc, self.work))
        return Context(manifest, os.path.join(self.work, "ctx"))

    def test_item_id_is_the_kiid_string(self):
        board = pcbnew.LoadBoard(self._project(dangling=True))
        ids = [geom.item_id(t) for t in board.GetTracks()]
        self.assertEqual(len(ids), len(set(ids)))
        for value in ids:
            self.assertRegex(value, UUID_RE)

    def test_a_dangling_end_names_its_track_and_board_frame_place(self):
        from pcbqa.gates import g_geometry
        self._project(dangling=True)
        res = GateResult("ROUTE.GEOMETRY_HYGIENE", "t")
        g_geometry.route_hygiene(self._context([90.0, 90.0]), res)
        self.assertEqual(res.status, core.Status.FAIL)
        dangling = [f for f in res.findings
                    if f["issue"] == "dangling track end"]
        self.assertTrue(dangling)
        finding = dangling[0]
        self.assertRegex(finding["kiid"], UUID_RE)
        # KiCad frame (100, 105) against a declared origin of (90, 90),
        # y up: board x = 10, board y = -15. The raw fields stay as before.
        self.assertEqual(finding["board_x_mm"], 10.0)
        self.assertEqual(finding["board_y_mm"], -15.0)
        self.assertEqual(finding["x_mm"], 100.0)
        self.assertEqual(finding["y_mm"], -105.0)


class GatesAsALibrary(unittest.TestCase):
    """`gates.evaluate` judges a candidate with the release's own gates."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_evaluate_")
        self.addCleanup(shutil.rmtree, self.work, True)
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_pad_footprint(board, "P2", 110, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_track(board, (100, 100), (110, 100), net=net)
        synth.save(board, os.path.join(self.work, "case.kicad_pcb"))
        synth.add_track(board, (100, 105), (105, 105), net=net)
        self.candidate = synth.save(
            board, os.path.join(self.work, "candidate.kicad_pcb"))
        doc = minimal_manifest({"routing": {"hygiene": {}}})
        self.manifest = write_manifest(doc, self.work)

    def test_a_selector_that_names_nothing_raises(self):
        from pcbqa import gates
        with self.assertRaises(ValueError):
            gates.evaluate(self.manifest, only="no.such.gate")

    def test_the_declared_board_passes_and_the_candidate_fails(self):
        from pcbqa import gates
        passed = gates.evaluate(self.manifest,
                                only="ROUTE.GEOMETRY_HYGIENE")
        self.assertEqual([r.status for r in passed], [core.Status.PASS])
        judged = gates.evaluate(self.manifest, only="ROUTE.GEOMETRY_HYGIENE",
                                board_path=self.candidate)
        self.assertEqual([r.status for r in judged], [core.Status.FAIL])

    def test_an_override_is_stamped_into_the_emitted_document(self):
        # The override changes WHAT was judged; a document that hid it would
        # claim the closure of copper the gates never read.
        from pcbqa.core import sha256_file
        ctx = Context(Manifest(self.manifest),
                      os.path.join(self.work, "ctx"),
                      board_path=self.candidate)
        doc = core.to_json([], ctx)
        marker = doc.get("board_override")
        self.assertIsNotNone(marker)
        self.assertEqual(marker["board_sha256"], sha256_file(self.candidate))
        self.assertIn("diagnostic", marker["meaning"])
        plain = core.to_json([], Context(Manifest(self.manifest),
                                         os.path.join(self.work, "ctx2")))
        self.assertNotIn("board_override", plain)


class CheckBoardCommand(unittest.TestCase):
    """The sub-second integrity preflight, end to end."""

    def _run(self, manifest):
        return subprocess.run(
            [PYTHON, os.path.join(HERE, "run.py"), "check-board", manifest],
            capture_output=True, text=True, cwd=HERE)

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_checkboard_")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.project = os.path.join(self.work, "project")
        shutil.copytree(paths.PORTABILITY_FIXTURE, self.project)

    def _manifest(self, extra=None):
        with open(paths.PORTABILITY_MANIFEST, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["project_root"] = self.project
        doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
        if extra:
            doc.update(extra)
        return write_manifest(doc, self.work)

    def test_the_untouched_fixture_checks_clean(self):
        proc = self._run(paths.PORTABILITY_MANIFEST)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("BOARD CHECK OK", proc.stdout)

    def test_a_foreign_kicad_file_beside_the_design_is_a_finding(self):
        with open(os.path.join(self.project, "intruder.kicad_prl"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        proc = self._run(self._manifest())
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("intruder.kicad_prl", proc.stdout)
        self.assertIn("foreign", proc.stdout)

    def test_a_board_that_is_not_the_accepted_candidate_is_named(self):
        record = {
            "kind": "routing-run", "source_sha256": HEX64,
            "attempts": [{"attempt": 1, "source_sha256": HEX64,
                          "accepted": True,
                          "stages": [{"stage": "route",
                                      "produced_by": "router",
                                      "sha256": HEX64}]}],
            "accepted_attempt": 1, "adopted_sha256": HEX64,
        }
        with open(os.path.join(self.project, "routing.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(record, fh)
        proc = self._run(self._manifest(
            {"routing": {"provenance": "routing.json"}}))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not the candidate the record describes", proc.stdout)

    def test_missing_lists_runtime_not_applicable_from_the_record(self):
        # STACK.PHYSICAL's static requirement is satisfied here, so only the
        # recorded validation can say why it did not apply.
        with open(os.path.join(self.project, "validation.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"gates": [{
                "gate": "STACK.PHYSICAL", "status": "NOT_APPLICABLE",
                "reason": "the board file carries no physical stackup"}]}, fh)
        manifest = self._manifest({
            "timing": {"physical_stackup": {}},
            "artifacts": {"validation_report": "validation.json"}})
        proc = subprocess.run(
            [PYTHON, os.path.join(HERE, "run.py"), "gates", "--missing",
             manifest],
            capture_output=True, text=True, cwd=HERE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        recorded = proc.stdout.split("NOT_APPLICABLE at the last recorded")
        self.assertEqual(len(recorded), 2, proc.stdout)
        self.assertIn("STACK.PHYSICAL", recorded[1])
        self.assertIn("no physical stackup", recorded[1])
        self.assertNotIn("STACK.PHYSICAL", recorded[0])
        # The stub carries no manifest identity, so the reasons are labelled
        # as recorded against a different manifest rather than believed.
        self.assertIn("different manifest", recorded[1])

    def test_a_stale_fabrication_record_names_the_input_that_moved(self):
        board_rel = "widget_b.kicad_pcb"
        fab = {"source_closure_sha256": HEX64,
               "source_closure": {board_rel: HEX64}}
        with open(os.path.join(self.project, "fab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(fab, fh)
        proc = self._run(self._manifest({
            "artifacts": {"fabrication_manifest": "fab.json"},
            "reports": {"files": [], "source_closure": ["*.kicad_pcb"]},
        }))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("different design", proc.stdout)
        self.assertIn(board_rel, proc.stdout)

    def test_a_tampered_committed_artifact_is_a_finding(self):
        # The closure cannot see an edited gerber - no source moved - so the
        # recorded artifact digests are checked too.
        with open(os.path.join(self.project, "shipped.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("tampered bytes")
        fab = {"source_closure_sha256": HEX64,
               "artifacts": {"shipped.txt": HEX64, "gone.txt": HEX64}}
        with open(os.path.join(self.project, "fab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(fab, fh)
        proc = self._run(self._manifest({
            "artifacts": {"fabrication_manifest": "fab.json"},
            "reports": {"files": [], "source_closure": ["*.kicad_pcb"]},
        }))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("shipped.txt has changed", proc.stdout)
        self.assertIn("gone.txt is absent", proc.stdout)


if __name__ == "__main__":
    unittest.main()
