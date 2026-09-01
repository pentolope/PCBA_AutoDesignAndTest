"""The parallelism inventory measures geometry exactly and claims
nothing electrical."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                       # noqa: E402
from pcbqa import claim, coupling_geometry          # noqa: E402
from pcbqa.coupling_geometry import CouplingGeometryError  # noqa: E402
from tests import synth                             # noqa: E402


class ParallelRunsAreMeasuredExactly(unittest.TestCase):

    def _board(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        a = synth.add_net(board, "AGGR")
        b = synth.add_net(board, "VICT")
        c = synth.add_net(board, "FARAWAY")
        # 8 mm side-by-side run: centerlines 0.4 mm apart, widths
        # 0.2 mm -> edge separation 0.2 mm.
        synth.add_track(board, (95.0, 100.0), (103.0, 100.0),
                        net=a, width_mm=0.2)
        synth.add_track(board, (95.0, 100.4), (103.0, 100.4),
                        net=b, width_mm=0.2)
        synth.add_track(board, (95.0, 110.0), (103.0, 110.0),
                        net=c, width_mm=0.2)
        return board

    def test_the_coupled_length_and_separation_are_geometric(self):
        board = self._board()
        records = coupling_geometry.parallelism_inventory(
            board, ["AGGR", "VICT", "FARAWAY"], 0.3)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["evidence"]["phenomenon"], "coupling")
        self.assertIn("AGGR||VICT", record["scope"]["identity"])
        self.assertAlmostEqual(record["quantity"]["value"], 8.0,
                               delta=0.7)
        self.assertAlmostEqual(
            record["evidence"]["provenance"][
                "minimum_edge_separation_mm"],
            0.2, delta=0.02)
        self.assertEqual(record["evidence"]["evidence_class"],
                         "geometry-only")
        self.assertIn("not a crosstalk voltage",
                      record["significance"])
        # Descriptive: no requirement linkage, no verdict.
        self.assertIsNone(
            claim.verdict(record))

    def test_distant_pairs_produce_nothing(self):
        board = self._board()
        records = coupling_geometry.parallelism_inventory(
            board, ["AGGR", "FARAWAY"], 0.3)
        self.assertEqual(records, [])

    def test_inputs_validate(self):
        board = self._board()
        with self.assertRaises(CouplingGeometryError):
            coupling_geometry.parallelism_inventory(
                board, ["AGGR", "VICT"], 0.0)
        with self.assertRaises(CouplingGeometryError):
            coupling_geometry.parallelism_inventory(
                board, ["AGGR"], 0.3)
        with self.assertRaises(CouplingGeometryError):
            coupling_geometry.parallelism_inventory(
                board, ["AGGR", "VICT"], 0.3,
                layers=["No.Such.Layer"])


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
