"""Synthetic geometry tests: shapes, rotation, mask margin, thresholds.

Every expected value here is derived analytically from the fixture, not from
any real board, so a checker cannot pass by having been tuned to one project.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pcbnew                                     # noqa: E402
from tests import paths                                                    # noqa: E402
from pcbqa import geom                            # noqa: E402
from pcbqa.core import Manifest                   # noqa: E402
from tests import synth                           # noqa: E402

# Polygonisation refuses to guess a chord error, so these unit tests install
# the same one a gate would - from a board's geometry profile.
geom.configure(Manifest(paths.REVA_MANIFEST)
               .geometry_profile().tolerance("polygon_chord_error_mm").value)


class PadOutlineTests(unittest.TestCase):
    """Pad copper and mask outlines under rotation, mirroring and margins."""

    def test_rectangular_pad_area_and_bbox(self):
        board = synth.new_board()
        _fp, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (2.0, 1.0))
        poly = geom.pad_copper_polygon(pad, pcbnew.F_Cu)
        self.assertAlmostEqual(poly.area, 2.0, places=3)
        minx, miny, maxx, maxy = poly.bounds
        self.assertAlmostEqual(maxx - minx, 2.0, places=3)
        self.assertAlmostEqual(maxy - miny, 1.0, places=3)

    def test_rotated_rectangle_bbox_matches_analytic(self):
        """A 2x1 pad at 30 degrees has a known axis-aligned bounding box."""
        board = synth.new_board()
        _fp, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (2.0, 1.0),
            rotation_deg=30.0)
        poly = geom.pad_copper_polygon(pad, pcbnew.F_Cu)
        minx, miny, maxx, maxy = poly.bounds
        w = 2.0 * abs(math.cos(math.radians(30))) + 1.0 * abs(math.sin(math.radians(30)))
        h = 2.0 * abs(math.sin(math.radians(30))) + 1.0 * abs(math.cos(math.radians(30)))
        self.assertAlmostEqual(maxx - minx, w, places=2)
        self.assertAlmostEqual(maxy - miny, h, places=2)
        self.assertAlmostEqual(poly.area, 2.0, places=2)

    def test_rotation_does_not_change_area(self):
        for angle in (0, 17.5, 45, 90, 137.25, 270):
            board = synth.new_board()
            _fp, pad = synth.add_pad_footprint(
                board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (1.6, 0.8),
                rotation_deg=angle)
            poly = geom.pad_copper_polygon(pad, pcbnew.F_Cu)
            self.assertAlmostEqual(poly.area, 1.28, places=2, msg=f"angle {angle}")

    def test_circle_and_oval(self):
        board = synth.new_board()
        _f, circle = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_CIRCLE, (1.0, 1.0))
        _f, oval = synth.add_pad_footprint(
            board, "P2", 105, 100, pcbnew.PAD_SHAPE_OVAL, (2.0, 1.0))
        self._conservative(geom.pad_copper_polygon(circle, pcbnew.F_Cu).area,
                           math.pi * 0.25)
        # stadium: rectangle 1x1 plus a circle of diameter 1
        self._conservative(geom.pad_copper_polygon(oval, pcbnew.F_Cu).area,
                           1.0 * 1.0 + math.pi * 0.25)

    def _conservative(self, measured, exact, tol=0.005):
        """Curved shapes are polygonised outward, so the area must be at least
        the exact value and no more than `tol` fraction above it."""
        self.assertGreaterEqual(measured, exact - 1e-9,
                                "approximation must never under-state a pad")
        self.assertLessEqual(measured, exact * (1.0 + tol),
                             "approximation error is larger than the bound")

    def test_roundrect_area_between_rect_and_inscribed(self):
        board = synth.new_board()
        _f, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_ROUNDRECT, (2.0, 1.0),
            roundrect_ratio=0.25)
        area = geom.pad_copper_polygon(pad, pcbnew.F_Cu).area
        r = 0.25 * 1.0
        exact = 2.0 * 1.0 - (4 - math.pi) * r * r
        self._conservative(area, exact)

    def test_mask_margin_grows_opening(self):
        board = synth.new_board()
        _f, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
            mask_margin_mm=0.1)
        opening = geom.pad_mask_opening(pad, pcbnew.F_Mask, board)
        minx, miny, maxx, maxy = opening.bounds
        self.assertAlmostEqual(maxx - minx, 1.2, places=2)
        self.assertAlmostEqual(maxy - miny, 1.2, places=2)

    def test_negative_mask_margin_shrinks_opening(self):
        board = synth.new_board()
        _f, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
            mask_margin_mm=-0.1)
        opening = geom.pad_mask_opening(pad, pcbnew.F_Mask, board)
        minx, _miny, maxx, _maxy = opening.bounds
        self.assertAlmostEqual(maxx - minx, 0.8, places=2)

    def test_flipped_footprint_moves_pad_to_back(self):
        board = synth.new_board()
        fp, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), flipped=True)
        self.assertTrue(fp.IsFlipped())
        self.assertTrue(pad.IsOnLayer(pcbnew.B_Cu))
        self.assertIsNotNone(geom.pad_mask_opening(pad, pcbnew.B_Mask, board))
        self.assertIsNone(geom.pad_mask_opening(pad, pcbnew.F_Mask, board))


class ViaMaskDistanceTests(unittest.TestCase):
    """Just-below, exactly-at and just-above each threshold."""

    def _survey(self, gap_mm, pad_rotation=0.0):
        """Place a via whose annulus edge is `gap_mm` from a 1x1 pad's opening."""
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT,
                                (1.0, 1.0), rotation_deg=pad_rotation, net=net)
        via_x = 100 + 0.5 + gap_mm + 0.225          # pad half + gap + annulus radius
        via = synth.add_via(board, via_x, 100, net=net)
        g = geom.BoardGeometry(board)
        return g, g.via_mask_report(g.vias[0], "front")

    def test_distance_is_measured_from_the_annulus_edge(self):
        _g, rep = self._survey(0.40)
        self.assertAlmostEqual(rep["annulus_to_opening_mm"], 0.40, places=3)
        # drill edge is further away than the annulus edge by the annular width
        self.assertAlmostEqual(rep["drill_to_opening_mm"], 0.40 + 0.075, places=3)
        self.assertAlmostEqual(rep["centre_to_opening_mm"], 0.40 + 0.225, places=3)

    def test_thresholds_just_below_at_and_above(self):
        for limit in (0.40, 0.35):
            for delta, expect_below in ((-0.005, True), (0.0, False), (0.005, False)):
                _g, rep = self._survey(limit + delta)
                measured = rep["annulus_to_opening_mm"]
                self.assertEqual(measured < limit - 1e-9, expect_below,
                                 msg=f"limit {limit} delta {delta} measured {measured}")

    def test_tangency_is_contact_but_not_strict_overlap(self):
        _g, rep = self._survey(0.0)
        self.assertTrue(rep["annulus_contacts_opening"])
        self.assertFalse(rep["annulus_overlaps_opening"])
        self.assertAlmostEqual(rep["annulus_to_opening_mm"], 0.0, places=4)

    def test_overlap_and_centre_inside(self):
        _g, rep = self._survey(-0.10)
        self.assertTrue(rep["annulus_overlaps_opening"])
        self.assertFalse(rep["centre_inside_opening"])
        _g, rep = self._survey(-0.40)
        self.assertTrue(rep["centre_inside_opening"])
        self.assertLess(rep["centre_to_opening_mm"], 0.0)

    def test_rotated_pad_overlap_is_detected(self):
        """A via that clears an axis-aligned pad but not the same pad rotated."""
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT,
                                (2.0, 0.4), rotation_deg=45.0, net=net)
        # On the pad's rotated long axis: 0.9 mm out along the 45 degree diagonal.
        d = 0.9 / math.sqrt(2)
        synth.add_via(board, 100 + d, 100 - d, net=net)
        g = geom.BoardGeometry(board)
        rep = g.via_mask_report(g.vias[0], "front")
        self.assertTrue(rep["annulus_contacts_opening"],
                        "rotated pad geometry was not honoured")

    def test_unrotated_equivalent_does_not_overlap(self):
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        synth.add_pad_footprint(board, "P1", 100, 100, pcbnew.PAD_SHAPE_RECT,
                                (2.0, 0.4), rotation_deg=0.0, net=net)
        d = 0.9 / math.sqrt(2)
        synth.add_via(board, 100 + d, 100 - d, net=net)
        g = geom.BoardGeometry(board)
        rep = g.via_mask_report(g.vias[0], "front")
        self.assertFalse(rep["annulus_contacts_opening"])


class FailClosedTests(unittest.TestCase):
    def test_unsupported_geometry_raises(self):
        with self.assertRaises(Exception):
            geom._polyset_to_polygons(pcbnew.SHAPE_POLY_SET())


if __name__ == "__main__":
    unittest.main()


class ChordErrorIsAConfiguredTolerance(unittest.TestCase):
    """The declared tolerance is applied, not decorative."""

    def tearDown(self):
        geom.configure(Manifest(paths.REVA_MANIFEST)
                       .geometry_profile()
                       .tolerance("polygon_chord_error_mm").value)

    def test_an_unconfigured_chord_error_refuses_to_approximate(self):
        geom._POLYGON_ERROR_IU = None
        with self.assertRaises(geom.UnsupportedGeometry):
            geom.polygon_error_iu()

    def test_a_coarser_tolerance_produces_a_measurably_larger_outline(self):
        board = synth.new_board()
        _f, pad = synth.add_pad_footprint(
            board, "P1", 100, 100, pcbnew.PAD_SHAPE_CIRCLE, (2.0, 2.0))
        geom.configure(0.001)
        fine = geom.pad_copper_polygon(pad, pcbnew.F_Cu)
        geom.configure(0.05)
        coarse = geom.pad_copper_polygon(pad, pcbnew.F_Cu)
        self.assertNotAlmostEqual(fine.area, coarse.area, places=4,
                                  msg="the chord error had no effect at all")
        # Outward approximation: coarser means larger, never smaller, so an
        # approximation can never over-state a clearance.
        self.assertGreater(coarse.area, fine.area)
        self.assertGreater(coarse.area, math.pi)
