"""Geometry extraction on synthetic boards: every number hand-derivable.

The fixtures are built in memory, so every expected length, count and
resistance below is analytic. The provenance contract is tested
alongside the arithmetic: bare numbers cannot masquerade as physical
evidence, approved-evidence resolution mirrors the impedance solver's
finished-copper semantics exactly, absent nets refuse, and the
derived interconnect model covers only the phenomenon the extraction
actually supplies.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                      # noqa: E402
from pcbqa import extract                          # noqa: E402
from pcbqa.extract import ExtractionError          # noqa: E402
from pcbqa.sim import fidelity                     # noqa: E402
from tests import synth                            # noqa: E402
from tests.test_fabricators import _raw_sources    # noqa: E402
from pcbqa.fabricators import jlcpcb, model        # noqa: E402

COPPER = extract.caller_declared_copper({"F.Cu": 0.035,
                                         "B.Cu": 0.035})
THICKNESS = extract.physical_parameter(
    1.6, "mm", "caller-declared", "test fixture board thickness")


def _snapshot():
    catalog = jlcpcb.parse(_raw_sources())
    return {"normalized": catalog,
            "normalized_sha256": model.normalized_digest(catalog)}


def _board_with_net():
    board = synth.new_board()
    net = synth.add_net(board, "SIG")
    synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                    layer=pcbnew.F_Cu, width_mm=0.2)
    synth.add_track(board, (10.0, 0.0), (10.0, 5.0), net=net,
                    layer=pcbnew.B_Cu, width_mm=0.4)
    synth.add_via(board, 10.0, 0.0, net=net)
    return board


class PhysicalInputsCarryProvenance(unittest.TestCase):

    def test_bare_numbers_cannot_masquerade_as_evidence(self):
        board = _board_with_net()
        with self.assertRaises(ExtractionError):
            extract.extract_net(board, "SIG",
                                {"F.Cu": 0.035, "B.Cu": 0.035},
                                THICKNESS)
        with self.assertRaises(ExtractionError):
            extract.extract_net(board, "SIG", COPPER, 1.6)

    def test_caller_declared_is_marked_forever(self):
        for record in COPPER.values():
            self.assertEqual(record["source_type"], "caller-declared")
            self.assertIsNone(record["digest"])

    def test_approved_copper_mirrors_the_impedance_resolution(self):
        """Finished copper resolved from approved evidence: external
        1 oz is 1.6 mil = 0.04064 mm and internal 0.5 oz is 0.6 mil =
        0.01524 mm - the finished thicknesses, NOT the 35 um nominal
        foil - each carrying the record identity and the catalog
        digest."""
        snapshot = _snapshot()
        parameters = extract.approved_finished_copper(snapshot, {
            "F.Cu": ("external", 1.0),
            "In1.Cu": ("internal", 0.5),
        })
        outer = parameters["F.Cu"]
        self.assertEqual(outer["value"], 0.04064)
        self.assertEqual(outer["source_type"], "approved-evidence")
        self.assertEqual(outer["source"],
                         "finished_copper_external_1oz_mil")
        self.assertEqual(outer["digest"],
                         snapshot["normalized_sha256"])
        self.assertEqual(parameters["In1.Cu"]["value"], 0.01524)
        with self.assertRaises(ExtractionError):
            extract.approved_finished_copper(snapshot, {
                "F.Cu": ("external", 9.0)})

    def test_parameter_validation_is_strict(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ExtractionError):
                extract.physical_parameter(value, "mm",
                                           "caller-declared", "x")
        with self.assertRaises(ExtractionError):
            extract.physical_parameter(1.0, "mm", "guessed", "x")
        with self.assertRaises(ExtractionError):
            extract.physical_parameter(1.0, "mm", "derived", "")


class ExtractionIsAnalytic(unittest.TestCase):

    def test_segments_totals_and_resistance(self):
        record = extract.extract_net(_board_with_net(), "SIG",
                                     COPPER, THICKNESS)
        totals = record["totals"]
        self.assertAlmostEqual(totals["copper_length_mm"], 15.0,
                               places=6)
        self.assertEqual(totals["via_count"], 1)
        self.assertAlmostEqual(totals["via_barrel_estimate_mm"], 1.6,
                               places=6)
        rho = extract.IACS_RESISTIVITY_OHM_M
        expected = rho * 0.010 / (0.0002 * 0.000035) \
            + rho * 0.005 / (0.0004 * 0.000035)
        self.assertAlmostEqual(
            record["dc"]["segment_resistance_sum_ohm"], expected,
            places=9)
        self.assertIn("no two-terminal claim",
                      record["dc"]["meaning"])
        self.assertIn("IEC 60028", record["dc"]["resistivity_source"])

    def test_an_absent_net_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.extract_net(_board_with_net(), "GHOST", COPPER,
                                THICKNESS)

    def test_unstated_copper_thickness_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.extract_net(
                _board_with_net(), "SIG",
                extract.caller_declared_copper({"F.Cu": 0.035}),
                THICKNESS)

    def test_the_baseline_report_carries_physical_inputs(self):
        board = _board_with_net()
        directory = synth.tempdir("extract-baseline")
        board_file = os.path.join(directory, "fixture.kicad_pcb")
        synth.save(board, board_file)
        saved = pcbnew.LoadBoard(board_file)
        report = extract.baseline_report(
            board_file, saved, ["SIG"], COPPER, THICKNESS)
        self.assertEqual(report["kind"], "board-geometry-baseline")
        self.assertEqual(len(report["board_file_sha256"]), 64)
        inputs = report["physical_inputs"]
        self.assertEqual(
            inputs["copper_thickness_mm"]["F.Cu"]["source_type"],
            "caller-declared")
        self.assertEqual(inputs["board_thickness_mm"]["value"], 1.6)

    def test_paths_lift_requires_the_timing_gates(self):
        with self.assertRaises(ExtractionError):
            extract.paths_from_validation({"gates": []})


class ExtractedInterconnectsEnterSimulationHonestly(unittest.TestCase):

    def _net_record(self):
        return extract.extract_net(_board_with_net(), "SIG", COPPER,
                                   THICKNESS)

    def test_the_model_covers_only_dc(self):
        """The derived model registers cleanly, covers EXACTLY
        interconnect_dc at geometry-derived, and cannot satisfy an
        interconnect_si requirement - geometry-derived DC data never
        becomes a transmission-line model by renaming."""
        record = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64)
        registry = fidelity.ModelRegistry([record])
        self.assertEqual(record["coverage"],
                         {"interconnect_dc": "geometry-derived"})
        self.assertIn("inductance", record["omissions"])
        report = registry.coverage_report(
            [record["identity"]],
            {"interconnect_si": ["full-wave-extracted",
                                 "quasi-static-extracted"]})
        self.assertFalse(report["satisfied"])
        report = registry.coverage_report(
            [record["identity"]],
            {"interconnect_dc": ["geometry-derived"]})
        self.assertTrue(report["satisfied"])

    def test_spice_needs_the_two_terminal_assertion(self):
        without = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64)
        self.assertNotIn("spice", without)
        with_assertion = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64,
            two_terminal_asserted_by="test author")
        self.assertIn(".subckt", with_assertion["spice"])
        self.assertEqual(
            with_assertion["provenance"][
                "two_terminal_asserted_by"], "test author")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
