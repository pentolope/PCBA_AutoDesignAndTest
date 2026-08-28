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
        "hole_clearance_mm": 0.25,
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


def _min_pad_distance(proposal, pad_polygon_shape):
    """Closest approach of the proposal's copper to a pad polygon."""
    from shapely.geometry import LineString
    best = None
    for segment in proposal["tracks"]:
        line = LineString([tuple(segment["start_mm"]),
                           tuple(segment["end_mm"])])
        distance = line.buffer(
            segment["width_mm"] / 2.0).distance(pad_polygon_shape)
        if best is None or distance < best:
            best = distance
    return best


class ObstaclesHonorPerNetPairClearances(unittest.TestCase):
    """KiCad judges two objects at max(classA, classB). A planner
    verifying one scalar builds copper the DRC then rejects - the
    exact failure the 26 production collisions traced to."""

    def setUp(self):
        geom.configure(0.001)

    def _corridor_board(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        synth.add_net(board, "GNDX")
        power = synth.add_net(board, "PWR")
        # A POWER-class pad just below the straight line from the
        # connection's endpoints: the shortest detour hugs it.
        synth.add_pad_footprint(board, "PWRPAD", 11.75, 9.3,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=power)
        return board

    def test_scalar_clearance_reproduces_the_collision(self):
        board = self._corridor_board()
        proposal = critical_topology.local_connect(
            board, "GNDX", (10.0, 10.0), (13.5, 10.0), _rules(),
            geom.pad_copper_polygon)
        pad = [pad for fp in board.GetFootprints()
               if fp.GetReference() == "PWRPAD"
               for pad in fp.Pads()][0]
        distance = _min_pad_distance(
            proposal, geom.pad_copper_polygon(pad, pcbnew.F_Cu))
        # Honest at the scalar floor...
        self.assertGreaterEqual(distance, 0.15 * 0.999 - 1e-6)
        # ...but INSIDE the POWER class's 0.25 mm requirement: this
        # copper is exactly what the board's DRC rejected 26 times.
        self.assertLess(distance, 0.25)

    def test_net_clearances_keep_the_pairwise_max(self):
        board = self._corridor_board()
        proposal = critical_topology.local_connect(
            board, "GNDX", (10.0, 10.0), (13.5, 10.0), _rules(),
            geom.pad_copper_polygon,
            net_clearances={"GNDX": 0.15, "PWR": 0.25})
        pad = [pad for fp in board.GetFootprints()
               if fp.GetReference() == "PWRPAD"
               for pad in fp.Pads()][0]
        distance = _min_pad_distance(
            proposal, geom.pad_copper_polygon(pad, pcbnew.F_Cu))
        self.assertGreaterEqual(distance, 0.25 * 0.999 - 1e-6)


class SuppliedClearanceMapsMustBeComplete(unittest.TestCase):

    def setUp(self):
        geom.configure(0.001)

    def test_a_forgotten_net_refuses(self):
        board = synth.new_board(layers=2, size_mm=30.0)
        synth.add_net(board, "GNDX")
        power = synth.add_net(board, "PWR")
        synth.add_pad_footprint(board, "PWRPAD", 11.75, 9.3,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=power)
        with self.assertRaisesRegex(TopologyPlanError,
                                    "absent from the supplied"):
            critical_topology.local_connect(
                board, "GNDX", (10.0, 10.0), (13.5, 10.0),
                _rules(), geom.pad_copper_polygon,
                net_clearances={"GNDX": 0.15})


class ThroughViasClearEveryCopperLayer(unittest.TestCase):
    """A through via traverses ALL copper layers; internal foreign
    copper is not passable merely because the outer layers are
    clear."""

    def setUp(self):
        geom.configure(0.001)

    def test_via_avoids_internal_layer_blanket(self):
        from shapely.geometry import LineString, Point
        board = synth.new_board(layers=4, size_mm=30.0)
        gnd = synth.add_net(board, "GNDX")
        sig = synth.add_net(board, "SIG")
        synth.add_pad_footprint(board, "BAR", 10.0, 10.0,
                                pcbnew.PAD_SHAPE_RECT, (0.35, 1.5),
                                net=gnd)
        synth.add_zone(board, gnd, [pcbnew.B_Cu],
                       (4.0, 4.0, 22.0, 22.0), fill=True)
        # Blanket In1.Cu with foreign copper around the anchor: an
        # outer-layers-only obstacle model would drop the via
        # straight through it.
        blanket = []
        y = 8.6
        while y <= 11.4 + 1e-9:
            synth.add_track(board, (8.0, y), (12.0, y), net=sig,
                            layer=pcbnew.In1_Cu, width_mm=0.2)
            blanket.append(LineString([(8.0, y), (12.0, y)])
                           .buffer(0.1))
            y += 0.3
        proposal = critical_topology.stitch_to_plane(
            board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
            geom.pad_copper_polygon)
        self.assertEqual(len(proposal["vias"]), 1)
        via = proposal["vias"][0]
        barrel = Point(via["x_mm"], via["y_mm"]).buffer(
            via["diameter_mm"] / 2.0, quad_segs=32)
        for shape in blanket:
            self.assertGreaterEqual(
                barrel.distance(shape), 0.15 - 1e-6)


class InternalPadAnnuliObstructThroughVias(unittest.TestCase):
    """A padstack whose internal annulus is larger than its outer
    representation still collides with a through via: clear outer
    layers are never a license to intersect internal copper."""

    def setUp(self):
        geom.configure(0.001)

    def test_via_clears_the_larger_internal_annulus(self):
        board = synth.new_board(layers=4, size_mm=30.0)
        gnd = synth.add_net(board, "GNDX")
        sig = synth.add_net(board, "SIG")
        synth.add_pad_footprint(board, "BAR", 10.0, 10.0,
                                pcbnew.PAD_SHAPE_RECT, (0.35, 1.5),
                                net=gnd)
        synth.add_zone(board, gnd, [pcbnew.B_Cu],
                       (4.0, 4.0, 22.0, 22.0), fill=True)
        # A through-hole pad exactly where the unobstructed search
        # likes to land, with outer copper 0.8 mm but an In1
        # annulus of 1.6 mm.
        synth.add_through_hole_footprint(board, "J1", 9.2, 10.2,
                                         net=sig, pad_mm=0.8,
                                         drill_mm=0.4)
        pad = [p for fp in board.GetFootprints()
               if fp.GetReference() == "J1"
               for p in fp.Pads()][0]
        pad.Padstack().SetMode(
            pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
        pad.SetSize(pcbnew.F_Cu, pcbnew.VECTOR2I(
            pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        pad.SetSize(pcbnew.In1_Cu, pcbnew.VECTOR2I(
            pcbnew.FromMM(1.6), pcbnew.FromMM(1.6)))
        pad.SetSize(pcbnew.B_Cu, pcbnew.VECTOR2I(
            pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        field = critical_topology.LocalField(
            board, "GNDX", (10.0, 10.0), _rules(),
            geom.pad_copper_polygon)
        plane_fill = field.plane_fill_at("GNDX")
        # (10.3, 10.2): the via clears the 0.8 mm OUTER copper,
        # the hole reach AND the mask-annulus target - but sits
        # 0.075 mm from the 1.6 mm In1 annulus, far inside the
        # 0.15 mm clearance. Outer-layers-only obstacle collection
        # calls this site legal.
        ok, reason = field.via_site_ok(10.3, 10.2, plane_fill)
        self.assertIs(ok, False)
        self.assertIn("copper clearance", reason)
        # A site genuinely beyond the internal annulus stays legal.
        ok, _reason = field.via_site_ok(10.85, 9.93, plane_fill)
        self.assertIs(ok, True)
        # End to end, the stitch still lands clear of the annulus.
        proposal = critical_topology.stitch_to_plane(
            board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
            geom.pad_copper_polygon)
        via = proposal["vias"][0]
        from shapely.geometry import Point
        barrel = Point(via["x_mm"], via["y_mm"]).buffer(
            via["diameter_mm"] / 2.0, quad_segs=32)
        internal = geom.pad_copper_polygon(pad, pcbnew.In1_Cu)
        self.assertGreaterEqual(barrel.distance(internal),
                                0.15 - 1e-6)


class ForeignFilledZonesAreNotObstacles(unittest.TestCase):
    """Zone fills are recomputed geometry: the refill pulls the pour
    back around new copper under the zone's own rules, and the
    post-stage fabrication DRC on the refilled board remains the
    authority. Treating a window-covering foreign pour as fixed
    copper would refuse every plan."""

    def setUp(self):
        geom.configure(0.001)

    def test_stitch_succeeds_under_a_foreign_pour(self):
        board = _stitch_board()
        sig = None
        for net_name, net in board.GetNetsByName().items():
            if str(net_name) == "SIG":
                sig = net
        synth.add_zone(board, sig, [pcbnew.F_Cu],
                       (4.0, 4.0, 22.0, 22.0), fill=True)
        proposal = critical_topology.stitch_to_plane(
            board, "GNDX", (10.0, 10.0), "GNDX", _rules(),
            geom.pad_copper_polygon)
        self.assertEqual(len(proposal["vias"]), 1)
        self.assertGreaterEqual(len(proposal["tracks"]), 1)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
