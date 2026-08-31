"""Clean-to-dirty mutations for ERC/DRC, waivers, freshness and the inventory.

Every test here starts from the frozen design, changes exactly one thing, and
asserts the change is noticed - and, where it matters, that it is noticed as the
*right kind* of problem. "The design is bad" and "we could not tell whether the
design is bad" are different answers, and a checker that conflates them cannot
be argued with.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                                    # noqa: E402
from pcbqa import canonical, closure as closure_mod, core, reports    # noqa: E402
from pcbqa.core import Context, Manifest, Status         # noqa: E402
from pcbqa.gates import g_checks, g_provenance           # noqa: E402,F401

REVA = paths.REVA_MANIFEST
FIXTURE_ROOT = paths.REVA_ROOT
FIXTURE = os.path.join(FIXTURE_ROOT, "project")


class _Copy:
    """A writable copy of the frozen fixture and a manifest that names it."""

    def __init__(self, tag, mutate=None, keep_fixture=False):
        self.work = tempfile.mkdtemp(prefix="pcbqa_mx_" + tag + "_")
        self.root = os.path.join(self.work, "fixture")
        shutil.copytree(FIXTURE_ROOT, self.root)
        self.project = os.path.join(self.root, "project")
        doc = json.load(open(REVA, encoding="utf-8"))
        doc["project_root"] = self.project
        if keep_fixture:
            doc["fixture"] = dict(doc["fixture"],
                                  attributes_file=paths.ATTRIBUTES)
        else:
            doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
        if mutate:
            mutate(doc, self.project, self.root)
        self.manifest_path = os.path.join(self.work, "manifest.json")
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

    def run(self, only):
        manifest = Manifest(self.manifest_path)
        ctx = Context(manifest, os.path.join(self.work, "wd"))
        return {r.gate_id: r for r in core.run_all(ctx, only=only)}

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


def _copy(case, tag, mutate=None, keep_fixture=False):
    box = _Copy(tag, mutate, keep_fixture)
    case.addCleanup(box.close)
    return box


FAKE_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fake_kicad_cli.py")


def _stub_cli(directory, name, *, exit_code=0, stderr="", runs=None):
    """A stand-in for kicad-cli that behaves exactly as badly as we need.

    A shebang-equipped shell script, marked executable. `runs` names a Python
    script the stub hands its arguments to, which is how a stub produces a
    report without KiCad; a stub that swallowed the helper's exit code would
    report a clean run whatever the helper did, so the `|| exit 1` is
    load-bearing.
    """
    path = os.path.join(directory, name + ".sh")
    lines = ["#!/bin/sh"]
    if stderr:
        lines.append("echo {} >&2".format(stderr))
    if runs:
        lines.append('"{}" "{}" "$@" || exit 1'.format(sys.executable, runs))
    lines.append("exit {}".format(exit_code))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o755)
    return path


def _use_fake_cli(doc, _project, root):
    """Point a copy at the fake tool instead of at this machine's KiCad."""
    doc["tools"]["kicad_cli"] = _stub_cli(root, "fake_cli", runs=FAKE_CLI)


# ---------------------------------------------------------------------------
# ERC / DRC: invocation failure is never a verdict on the design
# ---------------------------------------------------------------------------

class ToolFailureIsNotAVerdict(unittest.TestCase):
    def test_an_unexpected_exit_code_is_an_error_not_a_failure(self):
        def mutate(doc, _project, root):
            doc["tools"]["kicad_cli"] = _stub_cli(
                root, "broken_cli", exit_code=3,
                stderr="simulated tool crash")
        box = _copy(self, "toolfail", mutate)
        results = box.run({"DRC.AUTHORITATIVE", "ERC.AUTHORITATIVE"})
        for gate_id, result in results.items():
            self.assertEqual(
                result.status, Status.ERROR,
                "{} reported {} for a tool that never ran; a failed invocation "
                "must never look like a checked design".format(gate_id,
                                                               result.status))
            self.assertIn("invocation failed", result.reason)

    def test_the_documented_violations_exit_code_is_not_a_tool_failure(self):
        """Exit 5 means "ran, found things" - a verdict, not a crash."""
        def mutate(doc, _project, root):
            doc["tools"]["kicad_cli"] = _stub_cli(
                root, "violating_cli", exit_code=5)
        box = _copy(self, "exit5", mutate)
        result = box.run({"DRC.AUTHORITATIVE"})["DRC.AUTHORITATIVE"]
        # No report was written, so this is still an ERROR - but for the
        # missing report, not for the exit code.
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("produced no report", result.reason)
        self.assertNotIn("invocation failed", result.reason)

    def test_an_unsupported_report_schema_is_an_error(self):
        box = _copy(self, "schema", _use_fake_cli)
        with mock.patch.object(g_checks.reports, "parse_drc",
                               side_effect=reports.ReportSchemaError(
                                   "top-level `violations` is absent")):
            result = box.run({"DRC.AUTHORITATIVE"})["DRC.AUTHORITATIVE"]
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("unsupported DRC report schema", result.reason)

    def test_erc_findings_under_sheets_are_not_silently_empty(self):
        """The false PASS this framework exists to prevent."""
        doc = {"$schema": "https://schemas.kicad.org/erc.v1.json",
               "date": "2026-08-04T00:00:00", "kicad_version": "10.0.5",
               "source": "s.kicad_sch", "ignored_checks": [],
               "included_severities": ["error"],
               "coordinate_units": "mm",
               "sheets": [{"uuid_path": "/11111111-2222-3333-4444-555555555555",
                           "path": "/", "violations": [
                   {"type": "pin_not_connected", "severity": "error",
                    "description": "Pin not connected",
                    "items": [{"uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                               "description": "U1 pin 3",
                               "pos": {"x": 1.0, "y": 2.0}}]}]}]}
        findings, _meta = reports.parse_erc(doc)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["rule"], "pin_not_connected")
        with self.assertRaises(reports.ReportSchemaError):
            reports.parse_erc({**doc, "violations": []})


class IgnoredAndExcludedChecks(unittest.TestCase):
    """A check that did not run is not evidence that it would have passed."""

    @staticmethod
    def _project_edit(edit, also=None):
        def mutate(doc, project, root):
            path = os.path.join(project, "microphone_array_v2.kicad_pro")
            with open(path, encoding="utf-8") as fh:
                pro = json.load(fh)
            edit(pro)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(pro, fh, indent=2)
            if also:
                also(doc, project, root)
        return mutate

    def test_an_ignored_rule_fails_the_authoritative_gate(self):
        def edit(pro):
            pro["board"]["design_settings"]["rule_severities"][
                "copper_edge_clearance"] = "ignore"
        box = _copy(self, "ignored", self._project_edit(edit,
                                                        _use_fake_cli))
        result = box.run({"DRC.AUTHORITATIVE"})["DRC.AUTHORITATIVE"]
        self.assertEqual(result.status, Status.FAIL)
        ignored = next((f for f in result.findings
                        if "ignored one or more checks" in f.get("issue", "")), None)
        self.assertIsNotNone(ignored, result.findings)
        self.assertIn("copper_edge_clearance", ignored["ignored"])

    def test_a_stored_exclusion_fails_the_suppression_gate(self):
        def edit(pro):
            pro["board"].setdefault("drc_exclusions", []).append(
                "clearance|1000|2000|deadbeef|cafebabe")
        box = _copy(self, "exclusion", self._project_edit(edit))
        result = box.run({"DRC.NO_SUPPRESSED_RULES"})["DRC.NO_SUPPRESSED_RULES"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("stored exclusion" in f.get("issue", "")
                            for f in result.findings), result.findings)
        self.assertGreaterEqual(result.measurements["drc_exclusions"], 1)

    def test_restoring_a_severity_changes_the_measured_ignore_count(self):
        """The gate reads the real list, not a remembered number."""
        def edit(pro):
            sev = pro["board"]["design_settings"]["rule_severities"]
            for key, value in list(sev.items()):
                if value == "ignore":
                    sev[key] = "error"
        box = _copy(self, "unignored", self._project_edit(edit))
        result = box.run({"DRC.NO_SUPPRESSED_RULES"})["DRC.NO_SUPPRESSED_RULES"]
        self.assertEqual(result.measurements["drc_ignored_rules"], [],
                         "the gate is still reporting rules that are no longer "
                         "disabled")


# ---------------------------------------------------------------------------
# waivers: exact, hash-bound, and easy to invalidate
# ---------------------------------------------------------------------------

class WaiverMatching(unittest.TestCase):
    """A waiver describes an entire violation, or it describes nothing."""

    TOL = 0.001
    TRACK = "R5 pad 1"
    PAD = "U2 pad 20"

    def _finding(self, **over):
        items = over.pop("items", None)
        if items is None:
            items = [{"description": self.TRACK, "uuid": None,
                      "x_mm": 12.5, "y_mm": 34.75},
                     {"description": self.PAD, "uuid": None,
                      "x_mm": 40.0, "y_mm": 50.0}]
        base = {"category": "violations", "rule": "silk_over_copper",
                "severity": "warning", "description": "Silk over copper",
                "items": items,
                "canonical_items": reports.canonical_items(items),
                "objects": [i["description"] for i in items],
                "object": items[0]["description"] if items else "",
                "x_mm": items[0]["x_mm"] if items else None,
                "y_mm": items[0]["y_mm"] if items else None}
        base.update(over)
        return base

    def _waiver(self, **over):
        base = {"gate": "DRC.AUTHORITATIVE", "rule": "silk_over_copper",
                "category": "violations",
                "items": [{"description": self.TRACK, "location_mm": [12.5, 34.75]},
                          {"description": self.PAD, "location_mm": [40.0, 50.0]}],
                "reason": "legend clipped by 20 um on a test pad; reviewed on "
                          "the fabricator's DFM report",
                "reviewed_by": "A. Reviewer <a@example.invalid>",
                "reviewed_utc": "2026-07-30T12:00:00",
                "approved_source_sha256": "a" * 64,
                "approved_rules_sha256": "b" * 64,
                "approved_command_sha256": "c" * 64,
                "approved_report_sha256": "d" * 64}
        base.update(over)
        return base

    def test_an_exact_waiver_matches(self):
        self.assertIsNotNone(
            g_checks._waived(self._finding(), [self._waiver()], self.TOL))

    def test_a_waiver_naming_only_one_object_does_not_match(self):
        w = self._waiver(items=[{"description": self.TRACK,
                                 "location_mm": [12.5, 34.75]}])
        self.assertIsNone(g_checks._waived(self._finding(), [w], self.TOL))

    def test_moving_the_second_item_retires_the_waiver(self):
        """The defect this class exists for: only items[0] used to count."""
        moved = self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": 12.5, "y_mm": 34.75},
            {"description": self.PAD, "uuid": None, "x_mm": 41.0, "y_mm": 50.0}])
        self.assertIsNotNone(g_checks._waived(self._finding(), [self._waiver()],
                                              self.TOL),
                             "control: the unmoved finding must still match")
        self.assertIsNone(g_checks._waived(moved, [self._waiver()], self.TOL),
                          "a waiver survived its second affected item moving "
                          "1 mm")

    def test_adding_a_secondary_item_retires_the_waiver(self):
        extra = self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": 12.5, "y_mm": 34.75},
            {"description": self.PAD, "uuid": None, "x_mm": 40.0, "y_mm": 50.0},
            {"description": "V3", "uuid": None, "x_mm": 60.0, "y_mm": 70.0}])
        self.assertIsNone(g_checks._waived(extra, [self._waiver()], self.TOL))

    def test_removing_a_secondary_item_retires_the_waiver(self):
        fewer = self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": 12.5, "y_mm": 34.75}])
        self.assertIsNone(g_checks._waived(fewer, [self._waiver()], self.TOL))

    def test_reordering_the_items_does_not_change_the_outcome(self):
        swapped = self._finding(items=[
            {"description": self.PAD, "uuid": None, "x_mm": 40.0, "y_mm": 50.0},
            {"description": self.TRACK, "uuid": None, "x_mm": 12.5, "y_mm": 34.75}])
        self.assertIsNotNone(g_checks._waived(swapped, [self._waiver()], self.TOL),
                             "incidental ordering must not break a waiver")
        self.assertEqual(g_checks._report_digest([self._finding()]),
                         g_checks._report_digest([swapped]),
                         "incidental ordering must not change the digest")

    def test_a_moved_finding_does_not_match_an_old_waiver(self):
        moved = self._finding(items=[
            {"description": self.TRACK, "uuid": None,
             "x_mm": 12.5 + 10 * self.TOL, "y_mm": 34.75},
            {"description": self.PAD, "uuid": None, "x_mm": 40.0, "y_mm": 50.0}])
        self.assertIsNone(g_checks._waived(moved, [self._waiver()], self.TOL))

    def test_a_different_rule_at_the_same_place_does_not_match(self):
        other = self._finding(rule="courtyards_overlap")
        self.assertIsNone(g_checks._waived(other, [self._waiver()], self.TOL))

    def test_a_finding_with_no_location_can_never_be_waived(self):
        nowhere = self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": None, "y_mm": None},
            {"description": self.PAD, "uuid": None, "x_mm": 40.0, "y_mm": 50.0}])
        self.assertIsNone(g_checks._waived(nowhere, [self._waiver()], self.TOL))

    def test_a_broad_waiver_is_rejected_as_malformed(self):
        for field in ("rule", "items", "reason", "reviewed_by",
                      "approved_source_sha256"):
            w = self._waiver()
            w.pop(field)
            self.assertTrue(g_checks._waiver_defects(w, self.TOL),
                            "a waiver missing {!r} was accepted".format(field))
        self.assertTrue(g_checks._waiver_defects(self._waiver(rule="*"), self.TOL))
        self.assertTrue(g_checks._waiver_defects(self._waiver(items=[]), self.TOL))
        self.assertTrue(g_checks._waiver_defects(
            self._waiver(items=[{"description": "", "location_mm": [0, 0]}]),
            self.TOL))
        self.assertTrue(g_checks._waiver_defects(
            self._waiver(items=[{"description": "x", "location_mm": [0]}]),
            self.TOL))
        self.assertTrue(g_checks._waiver_defects(
            self._waiver(items=[{"description": "x",
                                 "location_mm": [float("nan"), 0]}]),
            self.TOL))
        self.assertFalse(g_checks._waiver_defects(self._waiver(), self.TOL))

    def test_a_changed_input_takes_the_waiver_out_of_service(self):
        bindings = {"approved_source_sha256": "a" * 64,
                    "approved_rules_sha256": "b" * 64,
                    "approved_command_sha256": "c" * 64,
                    "approved_report_sha256": "d" * 64}
        ctx = mock.Mock()
        ctx.manifest.get.return_value = [self._waiver()]
        live, rejected = g_checks._waivers_for(ctx, "DRC.AUTHORITATIVE",
                                               bindings, self.TOL)
        self.assertEqual(len(live), 1)
        self.assertEqual(rejected, [])

        for name in bindings:
            changed = dict(bindings, **{name: "e" * 64})
            live, rejected = g_checks._waivers_for(ctx, "DRC.AUTHORITATIVE",
                                                   changed, self.TOL)
            self.assertEqual(live, [],
                             "waiver survived a change to {}".format(name))
            self.assertEqual(rejected[0]["changed"], [name])

    def test_a_new_finding_changes_the_report_digest(self):
        one = [self._finding()]
        self.assertEqual(g_checks._report_digest(one),
                         g_checks._report_digest(list(one)))
        two = one + [self._finding(rule="track_dangling", items=[
            {"description": "T1", "uuid": None, "x_mm": 1.0, "y_mm": 2.0}])]
        self.assertNotEqual(g_checks._report_digest(one),
                            g_checks._report_digest(two),
                            "a new finding must invalidate every waiver bound "
                            "to the report the reviewer saw")
        moved = [self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": 99.0, "y_mm": 34.75},
            {"description": self.PAD, "uuid": None, "x_mm": 40.0, "y_mm": 50.0}])]
        self.assertNotEqual(g_checks._report_digest(one),
                            g_checks._report_digest(moved))

    def test_moving_only_the_second_item_changes_the_digest(self):
        moved = self._finding(items=[
            {"description": self.TRACK, "uuid": None, "x_mm": 12.5, "y_mm": 34.75},
            {"description": self.PAD, "uuid": None, "x_mm": 40.5, "y_mm": 50.0}])
        self.assertNotEqual(g_checks._report_digest([self._finding()]),
                            g_checks._report_digest([moved]),
                            "a digest that ignores items[1:] cannot retire a "
                            "waiver when items[1:] move")


# ---------------------------------------------------------------------------
# report freshness compares recomputed values
# ---------------------------------------------------------------------------

class ReportFreshness(unittest.TestCase):
    ATTRIBUTES = paths.ATTRIBUTES

    def _prepare(self, tag, doctor):
        """A fixture copy whose reports really are bound to its own sources.

        Rev A's committed reports predate this binding, so a freshness test
        against them could only ever observe the absence of a field. These
        tests need a genuinely fresh starting point in order to prove that a
        *substituted value* is what gets caught.
        """
        def drop_unrelated(_doc, project, _root):
            for stale in ("drc_pass1.json", "drc_routed.json"):
                path = os.path.join(project, "generated", stale)
                if os.path.isfile(path):
                    os.unlink(path)         # they describe a superseded board
        box = _Copy(tag, drop_unrelated)
        self.addCleanup(box.close)

        manifest = Manifest(box.manifest_path)
        policy = canonical.AttributePolicy.load(self.ATTRIBUTES)
        closure = closure_mod.source_closure(manifest, policy)
        digest = closure_mod.closure_digest(closure)
        pairs = (("erc.json", manifest.resolve(manifest.get("sources.schematic"))),
                 ("drc.json", manifest.resolve(manifest.get("sources.pcb"))))
        for name, source in pairs:
            path = os.path.join(box.project, "generated", name)
            with open(path, encoding="utf-8") as fh:
                report = json.load(fh)
            # Canonical, as the clean room binds it: the identity of a design
            # source must not depend on the checkout's line endings.
            report["source_sha256"] = canonical.digest(
                source, policy.classify(os.path.basename(source)))
            report["source_closure_sha256"] = digest
            report["source_closure"] = closure
            doctor(name, report)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
        return box

    def test_reports_bound_to_the_current_sources_are_fresh(self):
        box = self._prepare("fresh", lambda name, report: None)
        result = box.run({"PROV.REPORT_FRESHNESS"})["PROV.REPORT_FRESHNESS"]
        self.assertEqual(result.status, Status.PASS, result.findings)
        self.assertGreater(result.measurements["source_closure_files"], 1)

    def test_substituting_one_source_hash_makes_a_report_stale(self):
        """A real substitution, not a missing field."""
        def doctor(name, report):
            if name == "drc.json":
                report["source_sha256"] = "0" * 64
        box = self._prepare("substituted", doctor)
        result = box.run({"PROV.REPORT_FRESHNESS"})["PROV.REPORT_FRESHNESS"]
        self.assertEqual(result.status, Status.FAIL)
        finding = next(f for f in result.findings if f["file"].endswith("drc.json"))
        self.assertIn("does not match", finding["issue"])
        self.assertEqual(finding["recorded"], "0" * 16)
        self.assertNotEqual(finding["recomputed"], finding["recorded"])

    def test_a_changed_project_setting_invalidates_the_closure(self):
        """The board and schematic are untouched; the rules are not."""
        box = self._prepare("closure", lambda name, report: None)
        pro = os.path.join(box.project, "microphone_array_v2.kicad_pro")
        with open(pro, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc.setdefault("pcbqa_probe", {})["touched"] = True
        with open(pro, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        result = box.run({"PROV.REPORT_FRESHNESS"})["PROV.REPORT_FRESHNESS"]
        self.assertEqual(result.status, Status.FAIL,
                         "a design-rule change that leaves the board untouched "
                         "must still expire the reports")
        finding = result.findings[0]
        self.assertIn("closure changed", finding["issue"])
        self.assertTrue(any("kicad_pro" in c for c in finding["changed_inputs"]),
                        finding)


# ---------------------------------------------------------------------------
# the frozen inventory is exact
# ---------------------------------------------------------------------------

class FixtureInventory(unittest.TestCase):
    def _box(self, tag, mutate):
        return _copy(self, tag, mutate, keep_fixture=True)

    def test_the_untouched_fixture_passes(self):
        box = self._box("intact", None)
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS, result.findings)
        self.assertEqual(result.measurements["files_present"],
                         result.measurements["files_recorded"])

    def test_an_extra_file_fails(self):
        def mutate(_doc, project, _root):
            with open(os.path.join(project, "stray_export.gbr"), "w",
                      encoding="utf-8") as fh:
                fh.write("G04 nobody recorded this*\n")
        box = self._box("extra", mutate)
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any(f["file"] == "stray_export.gbr" for f in result.findings),
                        result.findings)

    def test_a_missing_file_fails(self):
        def mutate(_doc, project, _root):
            os.unlink(os.path.join(project, "README.md"))
        box = self._box("missing", mutate)
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any(f["file"] == "README.md" for f in result.findings),
                        result.findings)

    def test_a_changed_file_fails(self):
        def mutate(_doc, project, _root):
            path = os.path.join(project, "README.md")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("\nan extra line\n")
        box = self._box("changed", mutate)
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("content changed" in f.get("issue", "")
                            for f in result.findings), result.findings)

    def test_a_lock_file_fails(self):
        def mutate(_doc, project, _root):
            with open(os.path.join(project, "~microphone_array_v2.kicad_pro.lck"),
                      "w", encoding="utf-8") as fh:
                fh.write('{"pid": 1}')
        box = self._box("locked", mutate)
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("lock or scratch file" in f.get("issue", "")
                            for f in result.findings), result.findings)

    @unittest.skipUnless(hasattr(os, "symlink"), "no symlink support")
    def test_a_symlink_in_place_of_a_file_fails(self):
        def mutate(_doc, project, _root):
            target = os.path.join(project, "README.md")
            os.unlink(target)
            try:
                os.symlink(os.path.join(project, ".gitignore"), target)
            except (OSError, NotImplementedError) as exc:
                raise unittest.SkipTest("symlinks unavailable: {}".format(exc))
        try:
            box = self._box("symlink", mutate)
        except unittest.SkipTest:
            raise
        result = box.run({"PROV.FIXTURE_INTEGRITY"})["PROV.FIXTURE_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(
            any(f.get("issue", "").startswith("symbolic link")
                or f.get("issue") == "missing from frozen copy"
                for f in result.findings), result.findings)


if __name__ == "__main__":
    unittest.main()
