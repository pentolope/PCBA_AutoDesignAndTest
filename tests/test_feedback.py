"""Downstream failures become structured placement feedback: pad
geometry judged against the real outline, refusals carrying their
movables, and a strict record shape."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                       # noqa: E402
from pcbqa import feedback, geom                    # noqa: E402
from pcbqa.feedback import FeedbackError            # noqa: E402
from tests import synth                             # noqa: E402


def _outline():
    return {"kind": "circle", "center_mm": [100.0, 100.0],
            "radius_mm": 15.0}


class EdgeFindingsMeasurePadGeometry(unittest.TestCase):

    def setUp(self):
        geom.configure(0.001)

    def test_a_rim_pad_is_found_with_its_margins(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        net = synth.add_net(board, "SIG")
        # Pad centre 0.4 mm inside the rim; a 1 mm pad reaches to
        # 0.1 mm FROM the edge - inside a 0.3 mm clearance - while
        # the component origin alone looks fine.
        synth.add_pad_footprint(board, "TPX", 114.6, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "TPY", 100.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        findings = feedback.edge_clearance_findings(
            board, _outline(), 0.3, geom.pad_copper_polygon)
        self.assertEqual(len(findings), 1)
        record = findings[0]
        self.assertEqual(record["kind"], "board-edge-clearance")
        self.assertEqual(record["references"], ["TPX"])
        self.assertEqual(record["pads"], ["TPX.1"])
        self.assertEqual(record["required_margin_mm"], 0.3)
        self.assertAlmostEqual(record["observed_margin_mm"], -0.1,
                               delta=0.02)
        self.assertEqual(record["movement_domain"]["kind"],
                         "radial-inward")
        self.assertEqual(
            record["suggested_movable_references"], ["TPX"])

    def test_a_clear_board_yields_no_findings(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        net = synth.add_net(board, "SIG")
        synth.add_pad_footprint(board, "TPY", 100.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        self.assertEqual(feedback.edge_clearance_findings(
            board, _outline(), 0.3, geom.pad_copper_polygon), [])

    def test_non_circular_outlines_refuse(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        with self.assertRaises(FeedbackError):
            feedback.edge_clearance_findings(
                board, {"kind": "rectangle"}, 0.3,
                geom.pad_copper_polygon)


class RecordsValidateStrictly(unittest.TestCase):

    def test_escape_refusals_carry_their_movables(self):
        record = feedback.escape_refusal_record(
            "R7.1", "CLKNET", (12.0, 34.0),
            "no escape path exists at the declared values",
            ["R7"], {"kind": "planner-outcome",
                     "identity": "derivation:critical_topology"},
            0.25)
        self.assertEqual(record["kind"], "escape-refused")
        self.assertEqual(record["references"], ["R7"])
        self.assertIsNone(record["observed_margin_mm"])
        self.assertIn("no escape path", record["no_route_reason"])

    def test_margin_and_reason_are_mutually_exclusive(self):
        base = feedback.escape_refusal_record(
            "R7.1", "CLKNET", (12.0, 34.0), "refused", ["R7"],
            {"kind": "planner-outcome", "identity": "x"}, 0.25)
        both = dict(base, observed_margin_mm=0.1)
        with self.assertRaises(FeedbackError):
            feedback.validate_feedback(both)
        neither = dict(base, no_route_reason=None)
        with self.assertRaises(FeedbackError):
            feedback.validate_feedback(neither)

    def test_unknown_kinds_and_shapes_refuse(self):
        base = feedback.escape_refusal_record(
            "R7.1", "CLKNET", (12.0, 34.0), "refused", ["R7"],
            {"kind": "planner-outcome", "identity": "x"}, 0.25)
        with self.assertRaises(FeedbackError):
            feedback.validate_feedback(dict(base, kind="vibes"))
        missing = dict(base)
        del missing["movement_domain"]
        with self.assertRaises(FeedbackError):
            feedback.validate_feedback(missing)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
