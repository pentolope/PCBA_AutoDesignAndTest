"""The critical-topology planner: verified local copper only -
process constraints enforced at generation, gates still the
authority afterwards."""

from __future__ import annotations

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                       # noqa: E402
from pcbqa import connectivity, critical_topology, geom  # noqa: E402
from pcbqa.critical_topology import TopologyPlanError    # noqa: E402
from tests import synth                             # noqa: E402


def _rules(**overrides):
    rules = {
        "layer": "F.Cu",
        "track_width_mm": 0.3,
        "clearance_mm": 0.15,
        "via_diameter_mm": 0.45,
        "via_drill_mm": 0.30,
        "hole_to_hole_mm": 0.25,
        "mask_annulus_target_mm": 0.4,
        "grid_step_mm": 0.05,
        "search_radius_mm": 4.0,
    }
    rules.update(overrides)
    return rules


def _stitch_board():
    """A guard-pad stitch scene: the stitched net's pad, a filled
    same-net plane on B.Cu, a foreign pad with a mask opening, and a
    do-not-allow-vias area hugging the pad."""
    board = synth.new_board(layers=2, size_mm=30.0)
    gnd = synth.add_net(board, "GNDX")
    other = synth.add_net(board, "SIG")
    synth.add_pad_footprint(board, "BAR", 10.0, 10.0,
                            pcbnew.PAD_SHAPE_RECT, (0.35, 1.5),
                            net=gnd)
    synth.add_pad_footprint(board, "OBST", 12.4, 10.0,
                            pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                            net=other)
    synth.add_zone(board, gnd, [pcbnew.B_Cu],
                   (4.0, 4.0, 22.0, 22.0), fill=True)
    keepout = synth.add_zone(board, None, [pcbnew.F_Cu],
                             (9.0, 8.6, 11.0, 11.4), fill=False)
    keepout.SetIsRuleArea(True)
    keepout.SetDoNotAllowVias(True)
    keepout.SetZoneName("via_guard")
    return board


class StitchesAreVerifiedAtGeneration(unittest.TestCase):

    def setUp(self):
        geom.configure(0.001)

    def test_stitch_respects_keepout_mask_and_lands_on_plane(self):
        board = _stitch_board()
        proposal = critical_topology.stitch_to_plane(
            board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
            geom.pad_copper_polygon)
        self.assertEqual(proposal["net"], "GNDX")
        self.assertEqual(len(proposal["vias"]), 1)
        via = proposal["vias"][0]
        # Outside the do-not-allow-vias area.
        self.assertFalse(9.0 <= via["x_mm"] <= 11.0
                         and 8.6 <= via["y_mm"] <= 11.4)
        # The mask annulus target holds against the foreign pad's
        # aperture (pad half-extent 0.5 mm, zero mask margin).
        edge_distance = math.hypot(
            max(abs(via["x_mm"] - 12.4) - 0.5, 0.0),
            max(abs(via["y_mm"] - 10.0) - 0.5, 0.0)) \
            - via["diameter_mm"] / 2.0
        self.assertGreaterEqual(edge_distance, 0.4 - 1e-6)
        added = critical_topology.apply_proposal(board, proposal)
        self.assertGreaterEqual(added, 2)
        record = connectivity.classify_net(
            board, "GNDX", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "connectivity-complete")

    def test_generated_segments_pass_exact_reverification(self):
        board = _stitch_board()
        proposal = critical_topology.stitch_to_plane(
            board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
            geom.pad_copper_polygon)
        from shapely.geometry import LineString
        obstacle = geom.pad_copper_polygon(
            [pad for fp in board.GetFootprints()
             if fp.GetReference() == "OBST"
             for pad in fp.Pads()][0], pcbnew.F_Cu)
        for segment in proposal["tracks"]:
            line = LineString([tuple(segment["start_mm"]),
                               tuple(segment["end_mm"])])
            self.assertGreaterEqual(
                line.distance(obstacle),
                segment["width_mm"] / 2.0 + 0.15 - 1e-6)

    def test_an_unfilled_plane_refuses(self):
        board = _stitch_board()
        for zone in board.Zones():
            if not zone.GetIsRuleArea():
                zone.UnFill()
        with self.assertRaises(TopologyPlanError):
            critical_topology.stitch_to_plane(
                board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
                geom.pad_copper_polygon)

    def test_a_walled_window_refuses_the_escape(self):
        board = _stitch_board()
        sig = None
        for net_name, net in board.GetNetsByName().items():
            if str(net_name) == "SIG":
                sig = net
        # Box the anchor in with foreign copper on all sides.
        for rect in ((8.6, 8.0, 8.9, 12.0), (11.1, 8.0, 11.4, 12.0),
                     (8.6, 8.0, 11.4, 8.3), (8.6, 11.7, 11.4, 12.0)):
            synth.add_track(board, (rect[0], rect[1]),
                            (rect[2], rect[3]), net=sig,
                            width_mm=0.3)
            synth.add_track(board, (rect[0], rect[3]),
                            (rect[2], rect[1]), net=sig,
                            width_mm=0.3)
        with self.assertRaises(TopologyPlanError):
            critical_topology.local_connect(
                board, "GNDX", (10.0, 10.0), (13.5, 13.5),
                _rules(search_radius_mm=3.0),
                geom.pad_copper_polygon)

    def test_rules_validate_strictly(self):
        with self.assertRaises(TopologyPlanError):
            critical_topology.validate_rules(
                _rules(clearance_mm=0.0))
        bad = _rules()
        del bad["mask_annulus_target_mm"]
        with self.assertRaises(TopologyPlanError):
            critical_topology.validate_rules(bad)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
