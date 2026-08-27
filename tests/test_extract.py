"""Geometry extraction on synthetic boards: every number hand-derivable.

The fixtures are built in memory, so every expected length, count and
resistance below is analytic - the extractor cannot pass by having
been tuned to a real board. The honesty contract is tested alongside
the arithmetic: absent nets refuse, unstated copper thickness refuses,
the resistance field says exactly what kind of number it is.
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
from tests import synth                            # noqa: E402

COPPER = {"F.Cu": 0.035, "B.Cu": 0.035}


def _board_with_net():
    board = synth.new_board()
    net = synth.add_net(board, "SIG")
    synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                    layer=pcbnew.F_Cu, width_mm=0.2)
    synth.add_track(board, (10.0, 0.0), (10.0, 5.0), net=net,
                    layer=pcbnew.B_Cu, width_mm=0.4)
    synth.add_via(board, 10.0, 0.0, net=net)
    return board


class ExtractionIsAnalytic(unittest.TestCase):

    def test_segments_totals_and_resistance(self):
        record = extract.extract_net(_board_with_net(), "SIG",
                                     COPPER, 1.6)
        totals = record["totals"]
        self.assertAlmostEqual(totals["copper_length_mm"], 15.0,
                               places=6)
        self.assertAlmostEqual(totals["length_by_layer_mm"]["F.Cu"],
                               10.0, places=6)
        self.assertAlmostEqual(totals["length_by_layer_mm"]["B.Cu"],
                               5.0, places=6)
        self.assertEqual(totals["via_count"], 1)
        self.assertAlmostEqual(totals["via_barrel_estimate_mm"], 1.6,
                               places=6)
        # R = rho * L / (w * t), hand-derived per segment.
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
                                1.6)

    def test_unstated_copper_thickness_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.extract_net(_board_with_net(), "SIG",
                                {"F.Cu": 0.035}, 1.6)

    def test_non_finite_inputs_refuse(self):
        board = _board_with_net()
        for thickness in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ExtractionError):
                extract.extract_net(board, "SIG", COPPER, thickness)

    def test_the_baseline_report_is_identified_and_scoped(self):
        board = _board_with_net()
        directory = synth.tempdir("extract-baseline")
        board_file = os.path.join(directory, "fixture.kicad_pcb")
        synth.save(board, board_file)
        saved = pcbnew.LoadBoard(board_file)
        report = extract.baseline_report(
            board_file, saved, ["SIG"], COPPER, 1.6)
        self.assertEqual(report["kind"], "board-geometry-baseline")
        self.assertEqual(len(report["board_file_sha256"]), 64)
        self.assertIn("SIG", report["nets"])
        self.assertIsNone(report["interface_paths"])
        joined = " ".join(report["notes"])
        self.assertIn("no inductance or capacitance", joined)

    def test_paths_lift_requires_the_timing_gates(self):
        with self.assertRaises(ExtractionError):
            extract.paths_from_validation({"gates": []})
        lifted = extract.paths_from_validation({"gates": [
            {"gate": "TIMING.INTERCONNECT_DELAY", "status": "PASS",
             "measurements": {"paths": []}},
        ]})
        self.assertIn("TIMING.INTERCONNECT_DELAY", lifted)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
