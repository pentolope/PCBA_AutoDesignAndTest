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
        # The bare token is caught by parse_constant ...
        path = os.path.join(self.work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2, "board_id": "phase0_case", '
                     '"waivers": [{"limit": Infinity}]}')
        with self.assertRaisesRegex(ManifestError, "non-JSON constant"):
            Manifest(path)
        # ... but 1e400 is ordinary JSON grammar that json.loads turns into
        # inf without asking parse_constant, and an x_ subtree is stripped
        # before the validator's own finite check could ever visit it.
        # Finiteness is a property of loading, wherever the number sits.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"schema_version": 2, "board_id": "phase0_case", '
                     '"x_probe": {"v": 1e400}}')
        with self.assertRaisesRegex(ManifestError, "finite"):
            Manifest(path)

    def test_formerly_wide_open_nodes_now_refuse_by_name(self):
        # Seven schema nodes were {} or bare objects: everything inside
        # them - misspelled step keys, prose consumed as a declared layer,
        # a boolean threshold - was silently accepted.
        cases = (
            ({"timing": {"propagation": {"declared_layers": {
                "note": {"ps_per_mm": 6.7}}}}}, "data"),
            ({"timing": {"propagation": {"declared_layers": {
                "F.Cu": {"ps_per_mm": 6.7, "epsilon_eff": 3.9}}}}},
             "epsilon_eff"),
            ({"timing": {"interfaces": {"i": {
                "required_component_crossings": True}}}},
             "required_component_crossings"),
            ({"timing": {"device_timing": {"receivers": {"U9": {
                "setup": 1.0}}}}}, "setup"),
            ({"timing": {"interfaces": {"i": {"routes": {"paths": [
                {"id": "p", "steps": [{"kind": "copper", "net": "N",
                                       "form": "a", "to": "b"}]}]}}}}},
             "form"),
            ({"timing": {"physical_stackup": {"supplement": "/etc/pwn"}}},
             "supplement"),
            # The two formerly-allowlisted loose nodes: a misspelled scope
            # key in a discontinuity entry silently WIDENED the assumption
            # (a typo'd signal_layers stopped narrowing it) - the exact
            # fail-open the preflight exists to prevent.
            ({"timing": {"propagation": {"via_delay_model": {
                "model": "geometric", "max_delay_pss": 5}}}},
             "max_delay_pss"),
            ({"timing": {"propagation": {"reference_discontinuity": [{
                "treatment": "assume_continuous", "up_to_mm": 1.0,
                "justification": "j", "signal_layer": ["F.Cu"]}]}}},
             "signal_layer"),
            ({"timing": {"propagation": {"reference_discontinuity": {
                "up_to_mm": 1.0}}}}, "treatment"),
        )
        for extra, needle in cases:
            doc = minimal_manifest(extra)
            with self.assertRaisesRegex(ManifestError, needle):
                Manifest(write_manifest(doc, self.work))

    def test_the_orientation_registry_carries_only_read_fields(self):
        # Review provenance the toolkit never consumes moved under x_ keys;
        # the old spellings are refused by name like any other dead key.
        entry = {"lcsc": "C1", "mpn": "M", "package": "P",
                 "kicad_footprint": "F", "offset_deg": 0.0,
                 "review_status": "reviewed", "evidence_file": "e.json",
                 "raw_file": "r.json", "evidence_sha256": "0" * 64,
                 "x_review_basis": "kept as board-local provenance"}
        doc = minimal_manifest({"release_generation": {"cpl_orientation": {
            "registry": [entry],
            "reproduction_inputs": {"required_globs": []}},
            "fab_format": {"cpl": {
                "columns": [{"from": "Ref", "label": "Designator"},
                            {"from": "Rot", "label": "Rotation"}],
                "field_map": {"designator": "Designator",
                              "rotation": "Rotation"}}}}})
        Manifest(write_manifest(doc, self.work))
        dead = dict(entry)
        dead["review_basis"] = "no longer schema vocabulary"
        doc = minimal_manifest({"release_generation": {"cpl_orientation": {
            "registry": [dead]}}})
        with self.assertRaisesRegex(ManifestError, "review_basis"):
            Manifest(write_manifest(doc, self.work))

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
        # The candidate's NAME must never be the thing that fails it: the
        # staged copy carries the declared source name by design, so the
        # report naming that staged file is correct, not a mismatch.
        self.assertFalse(
            [f for f in result.findings
             if "different source" in str(f.get("issue", ""))],
            result.findings)

    def test_a_candidates_filename_never_changes_the_verdict(self):
        # Byte-identical copper under a different filename judges
        # identically to the declared board - the panel demonstrated a
        # FAIL-vs-PASS split on the filename alone.
        from pcbqa import gates
        renamed = os.path.join(self.work, "differently_named.kicad_pcb")
        shutil.copy2(os.path.join(self.work, "case.kicad_pcb"), renamed)
        declared = gates.evaluate(self.manifest,
                                  only="DRC.AUTHORITATIVE")[0]
        judged = gates.evaluate(self.manifest, only="DRC.AUTHORITATIVE",
                                board_path=renamed)[0]
        self.assertEqual(judged.status, declared.status,
                         (declared.reason, judged.reason))
        self.assertFalse(
            [f for f in judged.findings
             if "different source" in str(f.get("issue", ""))],
            judged.findings)


_HOLDER_CHILD = r"""
import sys, time
sys.path.insert(0, {toolkit!r})
from pcbqa.layout import Workspace
hold = Workspace("phase0_case", {base!r}).hold("doomed").__enter__()
print("HELD", flush=True)
time.sleep(120)
"""

_RACE_WORKER = r"""
import os, sys, time
sys.path.insert(0, {toolkit!r})
from pcbqa.layout import Workspace, LayoutError
ws = Workspace("phase0_case", {base!r})
sentinel = os.path.join({base!r}, "sentinel")
deadline = time.monotonic() + 3.0
held = 0
while time.monotonic() < deadline:
    try:
        with ws.hold("race"):
            try:
                fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                print("VIOLATION: two processes inside one hold")
                sys.exit(1)
            os.close(fd)
            held += 1
            os.unlink(sentinel)
    except LayoutError:
        continue
print("HELD-TIMES", held)
sys.exit(0 if held else 2)
"""


class WorkspaceHold(unittest.TestCase):
    """Tree-writing commands hold a kernel lock; a crash releases it."""

    def setUp(self):
        base = tempfile.mkdtemp(prefix="pcbqa_phase0_hold_")
        self.addCleanup(shutil.rmtree, base, True)
        self.base = base
        self.workspace = Workspace("phase0_case", base)
        self.hold_path = os.path.join(self.workspace.board, ".hold")

    def _holding_child(self):
        child = subprocess.Popen(
            [PYTHON, "-c", _HOLDER_CHILD.format(toolkit=HERE,
                                                base=self.base)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "HELD")
        return child

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

    def test_a_crashed_holders_lock_releases_with_it(self):
        # No staleness heuristic and no takeover protocol: the kernel
        # releases a dead holder's flock with its process, immediately.
        child = self._holding_child()
        child.kill()
        child.wait()
        with self.workspace.hold("takeover"):
            pass

    def test_a_leftover_lock_file_is_inert(self):
        # A crash leaves the file behind, unlocked. Whatever it holds -
        # garbage, a live-looking record, nothing - the next claimant
        # reuses it, because exclusion is the kernel lock, not the file.
        os.makedirs(self.workspace.board, exist_ok=True)
        for content in ("", "not json",
                        json.dumps({"pid": os.getpid(), "host": "here",
                                    "purpose": "looks live"})):
            with open(self.hold_path, "w", encoding="utf-8") as fh:
                fh.write(content)
            with self.workspace.hold("recover"):
                pass

    def test_exclusion_never_depends_on_the_lock_files_content(self):
        # Corrupting or truncating the FILE must not break a live hold:
        # the refusal has to stand on the lock alone.
        self._holding_child()
        with open(self.hold_path, "w", encoding="utf-8") as fh:
            fh.write("")
        with self.assertRaises(LayoutError):
            with self.workspace.hold("intruder"):
                pass

    def test_two_processes_never_hold_one_workspace_together(self):
        # The panel demonstrated 13 double-holds in 8000 attempts against
        # the unlink-and-reclaim design, with no crash and no hostile
        # input. Two unmodified workers hammer claim/release for a few
        # seconds; an O_EXCL sentinel inside the hold is the detector.
        code = _RACE_WORKER.format(toolkit=HERE, base=self.base)
        workers = [subprocess.Popen([PYTHON, "-c", code],
                                    stdout=subprocess.PIPE, text=True)
                   for _ in range(2)]
        for worker in workers:
            out, _ = worker.communicate(timeout=60)
            self.assertNotIn("VIOLATION", out)
            self.assertEqual(worker.returncode, 0,
                             "a worker never acquired at all: " + out)

    def test_the_hold_follows_the_tree_not_the_output_root(self):
        # Two invocations differing only in their output root write the
        # same project tree, so they contend for the same tree-anchored
        # lock - and two boards in one tree do not exclude each other.
        from pcbqa import layout as layout_mod
        project = tempfile.mkdtemp(prefix="pcbqa_phase0_tree_")
        self.addCleanup(shutil.rmtree, project, True)
        with layout_mod.tree_hold(project, "board_x", "writer one"):
            with self.assertRaisesRegex(LayoutError, "writer one"):
                with layout_mod.tree_hold(project, "board_x", "writer two"):
                    pass
            with layout_mod.tree_hold(project, "board_y", "other board"):
                pass
        with layout_mod.tree_hold(project, "board_x", "after release"):
            pass

    def test_an_unwritable_directory_is_a_refusal_not_a_traceback(self):
        from pcbqa import layout as layout_mod
        sealed = tempfile.mkdtemp(prefix="pcbqa_phase0_sealed_")
        self.addCleanup(shutil.rmtree, sealed, True)
        self.addCleanup(os.chmod, sealed, 0o755)
        os.chmod(sealed, 0o555)
        with self.assertRaisesRegex(LayoutError, "cannot be created"):
            with layout_mod.tree_hold(sealed, "board_x", "writer"):
                pass

    def test_release_never_strips_a_file_it_no_longer_owns(self):
        hold = self.workspace.hold("mine").__enter__()
        os.unlink(self.hold_path)
        with open(self.hold_path, "w", encoding="utf-8") as fh:
            fh.write("foreign")
        hold.__exit__(None, None, None)
        with open(self.hold_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "foreign")


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

    def test_a_manifest_without_sources_is_a_refusal_not_a_traceback(self):
        doc = {"schema_version": 2, "board_id": "phase0_case",
               "project_root": "."}
        manifest = write_manifest(doc, self.work)
        proc = self._run(manifest)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("BOARD CHECK REFUSED", proc.stdout)
        self.assertIn("sources.pcb", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)

    def test_an_unrecorded_file_in_a_release_directory_is_a_finding(self):
        release = os.path.join(self.project, "release")
        os.makedirs(release, exist_ok=True)
        with open(os.path.join(release, "mystery.gbr"), "w",
                  encoding="utf-8") as fh:
            fh.write("nobody recorded me")
        fab = {"source_closure_sha256": HEX64, "artifacts": {}}
        with open(os.path.join(self.project, "fab.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(fab, fh)
        # One level down and a dotfile beside it: the nested file must be
        # reported (the build's enumeration can never account for it, so it
        # ships unseen forever otherwise) and the dotfile must NOT be (the
        # record is produced through glob, which can never match it, so
        # flagging it would be a finding no rebuild could clear).
        os.makedirs(os.path.join(release, "sub"), exist_ok=True)
        with open(os.path.join(release, "sub", "nested.gbr"), "w",
                  encoding="utf-8") as fh:
            fh.write("nested and unrecorded")
        with open(os.path.join(release, ".gitkeep"), "w",
                  encoding="utf-8") as fh:
            fh.write("")
        proc = self._run(self._manifest({
            "artifacts": {"fabrication_manifest": "fab.json",
                          "gerber_dir": "release"},
            "reports": {"files": [], "source_closure": ["*.kicad_pcb"]},
        }))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("does not account for", proc.stdout)
        self.assertIn("mystery.gbr", proc.stdout)
        self.assertIn("nested.gbr", proc.stdout)
        self.assertNotIn(".gitkeep", proc.stdout)

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


class PartialRunGuards(unittest.TestCase):
    """--only never becomes a verdict: refused with --write, stamped on
    disk, and the stamps survive into both emitted files."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_partial_")
        self.addCleanup(shutil.rmtree, self.work, True)
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_pad_footprint(board, "P2", 110, 100,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        synth.add_track(board, (100, 100), (110, 100), net=net)
        synth.save(board, os.path.join(self.work, "case.kicad_pcb"))
        doc = minimal_manifest({"routing": {"hygiene": {}}})
        self.manifest = write_manifest(doc, self.work)
        self.out_root = os.path.join(self.work, "scratch")

    def _run(self, *args):
        environment = dict(os.environ)
        # The worker isolation variable outranks the override, so both are
        # pointed at this test's own scratch.
        environment["PCBQA_TEST_OUTPUT_ROOT"] = self.out_root
        environment["PCBQA_OUTPUT_ROOT"] = self.out_root
        return subprocess.run(
            [PYTHON, os.path.join(HERE, "run.py"), "validate",
             self.manifest] + list(args),
            capture_output=True, text=True, cwd=HERE, env=environment,
            timeout=600)

    def test_only_with_write_is_refused_at_the_cli(self):
        proc = self._run("--only=ROUTE.GEOMETRY_HYGIENE", "--write")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("mutually exclusive", proc.stdout)

    def test_the_partial_stamp_reaches_both_emitted_files(self):
        proc = self._run("--only=ROUTE.GEOMETRY_HYGIENE")
        self.assertIn("PARTIAL RUN", proc.stdout)
        jpath = [line.split(":", 1)[1].strip()
                 for line in proc.stdout.splitlines()
                 if line.startswith("JSON:")][0]
        with open(jpath, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertIn("partial", doc)
        self.assertEqual(doc["partial"]["only"],
                         ["ROUTE.GEOMETRY_HYGIENE"])
        with open(jpath[:-len(".json")] + ".md", encoding="utf-8") as fh:
            rendered = fh.read()
        self.assertNotIn("## Verdict", rendered)
        self.assertIn("Diagnostic - NOT a validation", rendered)


class TreeHoldAtTheCli(unittest.TestCase):
    """The write paths contend for the tree's own lock, not the output
    root's - a different PCBQA_OUTPUT_ROOT is no longer a bypass."""

    def test_validate_write_is_refused_while_the_tree_is_held(self):
        from pcbqa import layout as layout_mod
        work = tempfile.mkdtemp(prefix="pcbqa_phase0_treecli_")
        self.addCleanup(shutil.rmtree, work, True)
        project = os.path.join(work, "project")
        shutil.copytree(paths.PORTABILITY_FIXTURE, project)
        with open(paths.PORTABILITY_MANIFEST, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["project_root"] = project
        doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
        manifest = write_manifest(doc, work)
        environment = dict(os.environ)
        # A DIFFERENT output root than the holder's: under the old design
        # that took a different lock and both writers proceeded.
        environment["PCBQA_OUTPUT_ROOT"] = os.path.join(work, "other_root")
        with layout_mod.tree_hold(project, doc["board_id"], "the holder"):
            proc = subprocess.run(
                [PYTHON, os.path.join(HERE, "run.py"), "validate",
                 manifest, "--write"],
                capture_output=True, text=True, cwd=HERE, env=environment,
                timeout=600)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("REFUSED", proc.stdout)
        self.assertIn("the holder", proc.stdout)


class ReleaseCheckLeavesNothing(unittest.TestCase):
    """A read-only command abandons no run directory."""

    def test_a_quiet_discarded_validate_removes_its_run(self):
        import run as run_cli
        work = tempfile.mkdtemp(prefix="pcbqa_phase0_norun_")
        self.addCleanup(shutil.rmtree, work, True)
        with open(os.path.join(work, "case.kicad_pcb"), "w",
                  encoding="utf-8") as fh:
            fh.write("(kicad_pcb)")
        manifest = write_manifest(minimal_manifest(), work)
        out_root = os.path.join(work, "scratch")
        with mock.patch.dict(os.environ,
                             {"PCBQA_OUTPUT_ROOT": out_root,
                              "PCBQA_TEST_OUTPUT_ROOT": out_root}):
            code, doc, _ctx = run_cli.cmd_validate(manifest, quiet=True,
                                                   keep_run=False)
        self.assertIsNotNone(doc)
        board_dir = os.path.join(out_root, "out", "phase0_case")
        self.assertTrue(os.path.isdir(board_dir),
                        "the run must actually have used this root")
        runs = [name for name in os.listdir(board_dir)
                if os.path.isdir(os.path.join(board_dir, name))]
        self.assertEqual(runs, [], "the run directory must be discarded")


class CommittedVerdictBinding(unittest.TestCase):
    """release-check's reader honours the stamps and never compares errors."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_verdict_")
        self.addCleanup(shutil.rmtree, self.work, True)
        doc = minimal_manifest(
            {"artifacts": {"validation_report": "validation.json"}})
        self.manifest = Manifest(write_manifest(doc, self.work))

    def _blockers(self, committed, current_closure):
        import run as run_cli
        with open(os.path.join(self.work, "validation.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(committed, fh)
        return run_cli._committed_verdict(
            self.manifest, {"source_closure_sha256": current_closure})

    def _good(self):
        return {"summary": {"verdict": "ACCEPTED"},
                "source_closure_sha256": "a" * 64,
                "manifest": {"sha256": self.manifest.sha256}}

    def test_a_clean_binding_raises_no_blocker(self):
        self.assertEqual(self._blockers(self._good(), "a" * 64), [])

    def test_two_identical_error_strings_are_not_one_design(self):
        # core degrades an uncomputable closure to "UNAVAILABLE: ...", and
        # the same failure on both sides produces the same string. Equal
        # error messages must block, not pass.
        unavailable = "UNAVAILABLE: ClosureError: no line-ending policy"
        committed = self._good()
        committed["source_closure_sha256"] = unavailable
        blockers = self._blockers(committed, unavailable)
        self.assertTrue(any("not a digest" in why
                            for _s, _st, why in blockers), blockers)

    def test_the_partial_and_override_stamps_block_by_name(self):
        for marker in ("partial", "board_override"):
            committed = self._good()
            committed[marker] = {"meaning": "diagnostic"}
            blockers = self._blockers(committed, "a" * 64)
            self.assertTrue(any(marker in why for _s, _st, why in blockers),
                            (marker, blockers))

    def test_a_verdict_about_another_manifest_blocks(self):
        committed = self._good()
        committed["manifest"] = {"sha256": "b" * 64}
        blockers = self._blockers(committed, "a" * 64)
        self.assertTrue(any("different manifest" in why
                            for _s, _st, why in blockers), blockers)


class MarkdownHonesty(unittest.TestCase):
    """The human-facing file carries the stamps and the whole matrix."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_markdown_")
        self.addCleanup(shutil.rmtree, self.work, True)
        manifest = Manifest(write_manifest(minimal_manifest(), self.work))
        self.ctx = Context(manifest, os.path.join(self.work, "ctx"))

    def test_a_partial_document_never_reads_as_a_verdict(self):
        doc = core.to_json([], self.ctx)
        doc["partial"] = {"only": ["ROUTE.GEOMETRY_HYGIENE"],
                         "meaning": "subset"}
        rendered = core.to_markdown(doc)
        self.assertNotIn("## Verdict", rendered)
        self.assertIn("Diagnostic - NOT a validation", rendered)
        self.assertIn("ROUTE.GEOMETRY_HYGIENE", rendered)

    def test_an_override_document_names_the_judged_path(self):
        doc = core.to_json([], self.ctx)
        doc["board_override"] = {"board_path": "/tmp/cand.kicad_pcb",
                                 "board_sha256": None, "meaning": "diag"}
        rendered = core.to_markdown(doc)
        self.assertNotIn("## Verdict", rendered)
        self.assertIn("cand.kicad_pcb", rendered)

    def test_advisory_gates_render_in_table_and_findings(self):
        demoted = GateResult("T.ADVISORY", "demoted gate")
        demoted.failed("a measured problem")
        demoted.finding(issue="the measured detail")
        demoted.advisory("the board accepts this on the record")
        doc = core.to_json([demoted], self.ctx)
        rendered = core.to_markdown(doc)
        self.assertIn("| ADVISORY | 1 |", rendered)
        self.assertIn("the measured detail", rendered)


class InstallDiscipline(unittest.TestCase):
    """install proves every destination before a byte moves."""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="pcbqa_phase0_install_")
        self.addCleanup(shutil.rmtree, self.work, True)

    def _builder(self, artifacts_extra=None):
        doc = minimal_manifest({
            "artifacts": dict({"fabrication_manifest": "fab.json"},
                              **(artifacts_extra or {})),
            "release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx")),
            os.path.join(self.work, "run"))
        staged_dir = os.path.join(self.work, "staged_files")
        os.makedirs(staged_dir, exist_ok=True)
        self.staged = {}
        for name in ("bom_staged.csv", "cpl_staged.csv", "record.json"):
            path = os.path.join(staged_dir, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("content of " + name)
            self.staged[name] = path
        builder.record_staged = self.staged["record.json"]
        return manifest, builder

    def test_a_directory_in_place_of_an_artifact_installs_nothing(self):
        manifest, builder = self._builder()
        os.makedirs(os.path.join(self.work, "bom.csv"))
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("bom.csv"),
            self.staged["cpl_staged.csv"]: manifest.resolve("cpl.csv"),
        }
        with self.assertRaisesRegex(build_mod.BuildError,
                                    "not a plain file"):
            builder.install()
        self.assertFalse(os.path.exists(manifest.resolve("cpl.csv")),
                         "the prove pass refused, so NOTHING may install")
        self.assertFalse(os.path.exists(manifest.resolve("fab.json")))

    def test_a_symlink_destination_is_refused(self):
        manifest, builder = self._builder()
        with open(os.path.join(self.work, "real.csv"), "w",
                  encoding="utf-8") as fh:
            fh.write("previous")
        os.symlink(os.path.join(self.work, "real.csv"),
                   os.path.join(self.work, "bom.csv"))
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("bom.csv")}
        with self.assertRaisesRegex(build_mod.BuildError,
                                    "not a plain file"):
            builder.install()

    def test_two_staged_files_cannot_share_one_destination(self):
        manifest, builder = self._builder()
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("shared.csv"),
            self.staged["cpl_staged.csv"]: manifest.resolve("shared.csv"),
        }
        with self.assertRaisesRegex(build_mod.BuildError,
                                    "one destination"):
            builder.install()

    def test_manifest_level_duplicate_roles_refuse_before_generation(self):
        doc = minimal_manifest({
            "artifacts": {"bom": "same.csv", "cpl": "same.csv",
                          "fabrication_manifest": "fab.json"},
            "release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        self.assertTrue(build_mod.duplicate_destinations(manifest))
        builder = build_mod.Build(
            Context(manifest, os.path.join(self.work, "ctx2")),
            os.path.join(self.work, "run2"))
        with self.assertRaises(build_mod.BuildError):
            builder.run()
        self.assertTrue(any("both install to" in why
                            for _s, _st, why in builder.blockers),
                        builder.blockers)

    def test_a_clean_install_lands_everything_and_leaves_no_temp(self):
        manifest, builder = self._builder()
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("bom.csv")}
        installed = builder.install()
        self.assertEqual(len(installed), 2)
        with open(manifest.resolve("bom.csv"), encoding="utf-8") as fh:
            self.assertIn("bom_staged", fh.read())
        self.assertTrue(os.path.isfile(manifest.resolve("fab.json")))
        leftovers = [name for name in os.listdir(self.work)
                     if ".pcbqa-installing" in name]
        self.assertEqual(leftovers, [])

    def test_the_installers_own_temp_suffix_is_a_reserved_name(self):
        # A destination spelled `<other>.pcbqa-installing` sits inside the
        # commit phase's namespace: the artifact would land by rename and
        # then be deleted by the very cleanup that guards a failed commit -
        # a silently incomplete set behind exit 0.
        manifest, builder = self._builder()
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("bom.csv"),
            self.staged["cpl_staged.csv"]:
                manifest.resolve("bom.csv" + build_mod.TEMP_SUFFIX),
        }
        with self.assertRaisesRegex(build_mod.BuildError, "reserves"):
            builder.install()
        self.assertFalse(os.path.exists(manifest.resolve("bom.csv")))

    def test_the_success_path_cleanup_never_touches_installed_files(self):
        manifest, builder = self._builder()
        builder.destination_map = {
            self.staged["bom_staged.csv"]: manifest.resolve("bom.csv"),
            self.staged["cpl_staged.csv"]: manifest.resolve("cpl.csv"),
        }
        installed = builder.install()
        for path in installed:
            self.assertTrue(os.path.isfile(path), path)

    def test_the_committed_verdict_is_never_a_build_destination(self):
        doc = minimal_manifest({
            "artifacts": {"bom": "validation.json",
                          "validation_report": "validation.json",
                          "fabrication_manifest": "fab.json"},
            "release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        hits = build_mod.clobbered_inputs(manifest)
        self.assertIn(("bom", "the committed validation report"), hits)

    def test_a_verdict_inside_a_pruned_directory_refuses_the_build(self):
        doc = minimal_manifest({
            "artifacts": {"reports_dir": "reports",
                          "validation_report": "reports/validation.json",
                          "fabrication_manifest": "fab.json"},
            "release_generation": {}})
        manifest = Manifest(write_manifest(doc, self.work))
        hits = build_mod.clobbered_inputs(manifest)
        self.assertIn(("reports_dir", "the committed validation report"),
                      hits)


class ClosureCoversTheDesign(unittest.TestCase):
    """Every design input the toolkit stages is a closure member."""

    def _project(self):
        work = tempfile.mkdtemp(prefix="pcbqa_phase0_closure_")
        self.addCleanup(shutil.rmtree, work, True)
        for name, content in (("case.kicad_pcb", "(kicad_pcb)"),
                              ("case.kicad_prl", "{}")):
            with open(os.path.join(work, name), "w",
                      encoding="utf-8") as fh:
                fh.write(content)
        shutil.copy2(paths.ATTRIBUTES, os.path.join(work, ".gitattributes"))
        return work

    def test_design_inputs_join_the_closure_unconditionally(self):
        work = self._project()
        doc = minimal_manifest({
            "closure": {"attributes_file": ".gitattributes"},
            # The globs do NOT reach the .kicad_prl: a design input is a
            # member regardless of what the globs select.
            "reports": {"files": [], "source_closure": ["*.kicad_pcb"]}})
        manifest = Manifest(write_manifest(doc, work))
        entries, _digest = closure.current(manifest)
        self.assertIn("case.kicad_prl", entries)
        self.assertIn("case.kicad_pcb", entries)

    def test_an_exclusion_matching_a_design_input_is_refused(self):
        # Silently overriding the exclusion would leave a declaration that
        # reads as evaluated but does nothing; the honest answer is a
        # refusal naming the file.
        work = self._project()
        doc = minimal_manifest({
            "closure": {"attributes_file": ".gitattributes"},
            "reports": {"files": [], "source_closure": ["*.kicad_pcb"],
                        "source_closure_exclude": ["*.kicad_prl"]}})
        manifest = Manifest(write_manifest(doc, work))
        with self.assertRaisesRegex(closure.ClosureError,
                                    "case.kicad_prl"):
            closure.current(manifest)


class RequiredStepsAreGenerated(unittest.TestCase):
    """A required report a build step never generates is refused, not
    silently dropped from every result."""

    def test_a_step_without_an_output_raises_by_name(self):
        from pcbqa import artifacts
        work = tempfile.mkdtemp(prefix="pcbqa_phase0_steps_")
        self.addCleanup(shutil.rmtree, work, True)
        doc = minimal_manifest({
            "reports": {"files": [],
                        "required_steps": ["erc", "ipc_netlist"]},
            "release_generation": {"erc": {"output": "erc.json"}}})
        manifest = Manifest(write_manifest(doc, work))
        with self.assertRaisesRegex(ManifestError, "ipc_netlist"):
            artifacts.report_files(manifest)
        doc["reports"]["required_steps"] = ["erc"]
        manifest = Manifest(write_manifest(doc, work))
        self.assertEqual(artifacts.report_files(manifest), ["erc.json"])


class SchemaVocabulary(unittest.TestCase):
    """The schema is exactly as wide as what the toolkit reads."""

    #: Object nodes deliberately left undescribed. Empty: the last two
    #: (via_delay_model, reference_discontinuity) were typed after a
    #: misspelled scope key was shown to silently WIDEN an assumption.
    ALLOWED_LOOSE = set()

    @staticmethod
    def _schema():
        path = os.path.join(HERE, "schemas", "manifest.v2.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _corpus():
        chunks = []
        for base in (os.path.join(HERE, "pcbqa"),
                     os.path.join(HERE, "profiles")):
            for dirpath, _dirs, names in os.walk(base):
                for name in names:
                    if name.endswith(".py"):
                        with open(os.path.join(dirpath, name),
                                  encoding="utf-8") as fh:
                            chunks.append(fh.read())
        with open(os.path.join(HERE, "run.py"), encoding="utf-8") as fh:
            chunks.append(fh.read())
        return "\n".join(chunks)

    def test_every_admitted_key_is_read_somewhere(self):
        names = set()

        def walk(node):
            if not isinstance(node, dict):
                return
            for key, sub in (node.get("properties") or {}).items():
                names.add(key)
                walk(sub)
            for sub in (node.get("patternProperties") or {}).values():
                walk(sub)
            for sub in (node.get("definitions") or {}).values():
                walk(sub)
            if isinstance(node.get("additionalProperties"), dict):
                walk(node["additionalProperties"])
            if isinstance(node.get("items"), dict):
                walk(node["items"])

        walk(self._schema())
        corpus = self._corpus()
        missing = sorted(name for name in names if name not in corpus)
        self.assertEqual(
            missing, [],
            "the schema admits key(s) no code reads; delete them or move "
            "them under x_: {}".format(missing))

    def test_no_wide_open_object_nodes(self):
        loose = []

        def admits_objects(node):
            declared = node.get("type")
            if declared is None:
                return not node.get("enum")
            types = declared if isinstance(declared, list) else [declared]
            return "object" in types

        def walk(node, where):
            if not isinstance(node, dict):
                return
            # additionalProperties: true is exactly as wide open as {} -
            # only false (closed) or a schema (typed map) describes a node.
            additional = node.get("additionalProperties")
            described = ("properties" in node or "patternProperties" in node
                         or additional is False
                         or isinstance(additional, dict) or "$ref" in node
                         or "enum" in node)
            if admits_objects(node) and not described \
                    and where.split(".")[-1] not in self.ALLOWED_LOOSE \
                    and where != "$":
                loose.append(where)
            for key, sub in (node.get("properties") or {}).items():
                walk(sub, where + "." + key)
            for key, sub in (node.get("definitions") or {}).items():
                walk(sub, where + "." + key)
            for sub in (node.get("patternProperties") or {}).values():
                walk(sub, where + ".<pattern>")
            if isinstance(node.get("additionalProperties"), dict):
                walk(node["additionalProperties"], where + ".<any>")
            if isinstance(node.get("items"), dict):
                walk(node["items"], where + "[]")

        walk(self._schema(), "$")
        self.assertEqual(
            loose, [],
            "wide-open object node(s) suspend the named-refusal promise "
            "inside them: {}".format(loose))


if __name__ == "__main__":
    unittest.main()
