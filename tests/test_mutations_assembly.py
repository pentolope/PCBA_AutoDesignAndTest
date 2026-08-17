"""Clean-to-dirty mutations for the schematic-authoritative BOM and CPL.

Each test changes one thing in a copy of the frozen design or its packaged
assembly data and asserts the disagreement is found. Rev A's own BOM and CPL are
correct, so every failure here is caused by the mutation and nothing else.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                                    # noqa: E402
from pcbqa import core                                   # noqa: E402
from pcbqa.core import Context, Manifest, Status         # noqa: E402
from pcbqa.gates import g_assembly                       # noqa: E402,F401

REVA = paths.REVA_MANIFEST
FIXTURE = paths.REVA_PROJECT
ATTRIBUTES = paths.ATTRIBUTES

BOM_REL = os.path.join("generated", "release", "bom.csv")
CPL_REL = os.path.join("generated", "release", "cpl.csv")

# Column names come from the manifest, never from a literal: the packaged CSVs
# use JLCPCB's headers and a clean-room run uses kicad-cli's, and a test that
# hard-coded either would only ever exercise one of them.
_MANIFEST = json.load(open(REVA, encoding="utf-8"))
BOM_COL = _MANIFEST["assembly"]["bom_fields"]
CPL_COL = _MANIFEST["artifacts"]["cpl_fields"]


class _Copy:
    def __init__(self, tag):
        self.work = tempfile.mkdtemp(prefix="pcbqa_as_" + tag + "_")
        self.project = os.path.join(self.work, "project")
        shutil.copytree(FIXTURE, self.project)
        self.bom = os.path.join(self.project, BOM_REL)
        self.cpl = os.path.join(self.project, CPL_REL)

    def manifest(self):
        doc = json.load(open(REVA, encoding="utf-8"))
        doc["project_root"] = self.project
        doc["fixture"] = {"attributes_file": ATTRIBUTES}
        path = os.path.join(self.work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        return path

    def run(self, only):
        manifest = Manifest(self.manifest())
        ctx = Context(manifest, os.path.join(self.work, "wd"))
        return {r.gate_id: r for r in core.run_all(ctx, only=only)}

    # -- csv helpers -------------------------------------------------------
    @staticmethod
    def _read(path):
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            return reader.fieldnames, list(reader)

    @staticmethod
    def _write(path, fieldnames, rows):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def edit_bom(self, fn):
        names, rows = self._read(self.bom)
        rows = fn(rows) or rows
        self._write(self.bom, names, rows)

    def edit_cpl(self, fn):
        names, rows = self._read(self.cpl)
        rows = fn(rows) or rows
        self._write(self.cpl, names, rows)

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


class _Base(unittest.TestCase):
    GATE = None

    def _copy(self, tag):
        box = _Copy(tag)
        self.addCleanup(box.close)
        return box

    def _assert_finds(self, box, needle, refs=()):
        result = box.run({self.GATE})[self.GATE]
        self.assertEqual(result.status, Status.FAIL,
                         "{} did not notice the mutation".format(self.GATE))
        issues = [f.get("issue", "") for f in result.findings]
        self.assertTrue(any(needle in i for i in issues),
                        "expected an issue mentioning {!r}, got {}".format(
                            needle, issues[:10]))
        for ref in refs:
            self.assertTrue(any(f.get("reference") == ref for f in result.findings),
                            "no finding named {}: {}".format(ref, result.findings[:6]))
        return result

    def _baseline_findings(self, box):
        """How many disagreements the unmutated copy already has.

        Rev A has 21 board-only footprints, so "did it fail" is not evidence on
        its own; these tests check the mutation adds its own specific finding.
        """
        return box.run({self.GATE})[self.GATE].findings


class BomMutations(_Base):
    GATE = "BOM.NATIVE_PARITY"

    def test_an_invented_part_number_is_caught(self):
        """A part number that exists in the order but in no symbol."""
        box = self._copy("lcsc")

        def mutate(rows):
            rows[0][BOM_COL["LCSC"]] = "C99999999"
        box.edit_bom(mutate)
        self._assert_finds(box, "LCSC disagrees with the schematic")

    def test_a_blank_part_number_does_not_act_as_a_wildcard(self):
        box = self._copy("blanklcsc")

        def mutate(rows):
            rows[0][BOM_COL["LCSC"]] = ""
        box.edit_bom(mutate)
        self._assert_finds(box, "blank in the BOM")

    def test_a_missing_footprint_is_caught(self):
        box = self._copy("nofp")

        def mutate(rows):
            rows[1][BOM_COL["footprint"]] = ""
        box.edit_bom(mutate)
        self._assert_finds(box, "names no footprint")

    def test_a_wrong_footprint_is_caught(self):
        box = self._copy("wrongfp")

        def mutate(rows):
            rows[1][BOM_COL["footprint"]] = "R_0805_2012Metric"
        box.edit_bom(mutate)
        self._assert_finds(box, "footprint mismatch")

    def test_a_wrong_value_is_caught(self):
        box = self._copy("value")

        def mutate(rows):
            rows[0][BOM_COL["value"]] = "47R"
        box.edit_bom(mutate)
        self._assert_finds(box, "value mismatch")

    def test_a_missing_reference_is_caught(self):
        box = self._copy("missingref")
        dropped = {}

        def mutate(rows):
            refs = [r.strip() for r in rows[0][BOM_COL["designators"]].split(",")]
            dropped["ref"] = refs[0]
            rows[0][BOM_COL["designators"]] = ",".join(refs[1:])
            rows[0][BOM_COL["quantity"]] = str(len(refs) - 1)
        box.edit_bom(mutate)
        self._assert_finds(box, "missing from the BOM", refs=[dropped["ref"]])

    def test_a_duplicated_reference_is_caught(self):
        box = self._copy("dupref")
        repeated = {}

        def mutate(rows):
            refs = [r.strip() for r in rows[0][BOM_COL["designators"]].split(",")]
            repeated["ref"] = refs[0]
            rows[1][BOM_COL["designators"]] = (rows[1][BOM_COL["designators"]]
                                       + "," + refs[0])
            rows[1][BOM_COL["quantity"]] = str(len(
                [r for r in rows[1][BOM_COL["designators"]].split(",")
                 if r.strip()]))
        box.edit_bom(mutate)
        self._assert_finds(box, "more than one BOM line",
                           refs=[repeated["ref"]])

    def test_a_wrong_quantity_is_caught(self):
        box = self._copy("qty")

        def mutate(rows):
            rows[0][BOM_COL["quantity"]] = str(int(rows[0][BOM_COL["quantity"]]) + 1)
        box.edit_bom(mutate)
        result = box.run({self.GATE})[self.GATE]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("quantity does not match" in f.get("issue", "")
                            for f in result.findings), result.findings[:8])

    def test_a_dnp_flag_changed_on_the_board_disagrees_with_the_schematic(self):
        """The schematic decides what is populated; the board must agree."""
        box = self._copy("dnp")
        import pcbnew
        path = os.path.join(box.project, "microphone_array_v2.kicad_pcb")
        board = pcbnew.LoadBoard(path)
        target = next(fp for fp in board.Footprints()
                      if fp.GetReference().startswith("C")
                      and not fp.IsDNP() and not fp.IsExcludedFromBOM())
        reference = target.GetReference()
        target.SetDNP(True)
        board.Save(path)
        self._assert_finds(box, "do-not-populate differs", refs=[reference])

    def test_the_unmutated_copy_reports_nothing(self):
        """Every failure above is caused by its mutation, not by the fixture.

        This used to require the fixture's mounting holes, test pads and
        hand-fitted connectors to be reported as absent from the schematic.
        They never were absent: the comparison read the schematic through a
        BOM export, which cannot carry a symbol marked exclude-from-BOM, so
        every such part looked missing. With that corrected the unmutated copy
        has nothing to report, and the mutation tests above are the only thing
        producing findings - which is what this test is really for.
        """
        box = self._copy("baseline")
        findings = self._baseline_findings(box)
        self.assertEqual(findings, [],
                         "the unmutated fixture has an unexpected BOM "
                         "disagreement: {}".format(findings))


class CplMutations(_Base):
    GATE = "CPL.NATIVE_PARITY"

    def test_the_unmutated_copy_passes(self):
        box = self._copy("cplbase")
        result = box.run({self.GATE})[self.GATE]
        self.assertEqual(result.status, Status.PASS, result.findings[:6])

    def test_a_side_change_is_caught(self):
        box = self._copy("side")

        def mutate(rows):
            rows[0][CPL_COL["side"]] = ("Bottom"
                                       if rows[0][CPL_COL["side"]].lower() == "top"
                                       else "Top")
        box.edit_cpl(mutate)
        self._assert_finds(box, "side mismatch")

    def test_a_coordinate_change_is_caught(self):
        box = self._copy("coord")

        def mutate(rows):
            rows[0][CPL_COL["x"]] = str(float(rows[0][CPL_COL["x"]]) + 0.5)
        box.edit_cpl(mutate)
        result = self._assert_finds(box, "x mismatch")
        delta = next(f["delta_mm"] for f in result.findings
                     if f.get("issue") == "x mismatch")
        self.assertAlmostEqual(delta, 0.5, places=3)

    def test_a_rotation_change_is_caught(self):
        box = self._copy("rot")

        def mutate(rows):
            rows[0][CPL_COL["rotation"]] = str(
                (float(rows[0][CPL_COL["rotation"]]) + 90.0) % 360.0)
        box.edit_cpl(mutate)
        self._assert_finds(box, "rotation mismatch")

    def test_a_missing_row_is_caught(self):
        box = self._copy("cplmissing")
        dropped = {}

        def mutate(rows):
            dropped["ref"] = rows[0][CPL_COL["designator"]]
            return rows[1:]
        box.edit_cpl(mutate)
        self._assert_finds(box, "absent from the CPL", refs=[dropped["ref"]])

    def test_a_duplicated_row_is_caught(self):
        box = self._copy("cpldup")

        def mutate(rows):
            return rows + [dict(rows[0])]
        box.edit_cpl(mutate)
        self._assert_finds(box, "appears twice")

    def test_a_row_for_a_part_that_is_not_populated_is_caught(self):
        box = self._copy("cplextra")

        def mutate(rows):
            ghost = dict(rows[0])
            ghost[CPL_COL["designator"]] = "X999"
            return rows + [ghost]
        box.edit_cpl(mutate)
        self._assert_finds(box, "not populated in the schematic", refs=["X999"])


if __name__ == "__main__":
    unittest.main()
