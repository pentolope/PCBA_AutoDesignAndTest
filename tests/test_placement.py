"""The placement-constraint vocabulary: strict, generic, fail-closed.

These tests pin the contract an optimizer consumes: every kind has an
exact key set, values are finite where numbers are required, and
contradictions (a component fixed twice) refuse instead of averaging.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import placement                        # noqa: E402
from pcbqa.placement import PlacementError         # noqa: E402


class ConstraintsValidateStrictly(unittest.TestCase):

    def test_a_representative_set_validates(self):
        constraints = [
            {"kind": "fixed", "reference": "J1",
             "position_mm": [4.0, 60.0], "rotation_deg": 90.0,
             "reason": "host connector is mechanical"},
            {"kind": "board_edge", "reference": "J1",
             "max_distance_mm": 1.0},
            {"kind": "functional_block", "name": "clock-buffer",
             "members": ["U2", "RC1", "RC2"], "max_spread_mm": 12.0},
            {"kind": "proximity", "reference": "C7", "anchor": "U2",
             "max_distance_mm": 2.0, "pin": "VCC"},
            {"kind": "ordering", "path": "usb-esd",
             "references": ["J1", "D1", "U1"]},
            {"kind": "separation", "group_a": ["U3", "C9"],
             "group_b": ["MK1", "MK2"], "min_distance_mm": 8.0},
            {"kind": "orientation", "reference": "U2",
             "allowed_rotations_deg": [0.0, 180.0]},
            {"kind": "swap_group", "name": "buffer-outputs",
             "references": ["RC1", "RC2", "RC3"]},
        ]
        placement.validate_constraint_set(constraints)

    def test_unknown_kind_and_keys_refuse(self):
        with self.assertRaises(PlacementError):
            placement.validate_constraint({"kind": "gravity"})
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "board_edge", "reference": "J1",
                 "max_distance_mm": 1.0, "surprise": True})

    def test_numeric_sanity_is_enforced(self):
        for distance in (0, -1.0, float("nan"), float("inf"), True):
            with self.assertRaises(PlacementError):
                placement.validate_constraint(
                    {"kind": "proximity", "reference": "C1",
                     "anchor": "U1", "max_distance_mm": distance})
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "fixed", "reference": "J1",
                 "position_mm": [1.0, float("nan")]})

    def test_member_lists_are_real_groups(self):
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "swap_group", "name": "one",
                 "references": ["RC1"]})
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "functional_block", "name": "dup",
                 "members": ["U1", "U1"]})

    def test_cross_constraint_contradictions_refuse(self):
        with self.assertRaises(PlacementError):
            placement.validate_constraint_set([
                {"kind": "fixed", "reference": "U2",
                 "position_mm": [10.0, 10.0], "rotation_deg": 90.0},
                {"kind": "orientation", "reference": "U2",
                 "allowed_rotations_deg": [0.0, 180.0]},
            ])
        placement.validate_constraint_set([
            {"kind": "fixed", "reference": "U2",
             "position_mm": [10.0, 10.0], "rotation_deg": 180.0},
            {"kind": "orientation", "reference": "U2",
             "allowed_rotations_deg": [0.0, 180.0]},
        ])
        with self.assertRaises(PlacementError):
            placement.validate_constraint_set([
                {"kind": "swap_group", "name": "a",
                 "references": ["RC1", "RC2"]},
                {"kind": "swap_group", "name": "b",
                 "references": ["RC2", "RC3"]},
            ])

    def test_self_referential_constraints_refuse(self):
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "proximity", "reference": "C1",
                 "anchor": "C1", "max_distance_mm": 1.0})
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "separation", "group_a": ["U1", "U2"],
                 "group_b": ["U2", "U3"], "min_distance_mm": 5.0})

    def test_non_finite_coordinates_and_rotations_refuse(self):
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "fixed", "reference": "J1",
                 "position_mm": [1.0, float("inf")]})
        with self.assertRaises(PlacementError):
            placement.validate_constraint(
                {"kind": "fixed", "reference": "J1",
                 "position_mm": [1.0, 1.0],
                 "rotation_deg": float("nan")})

    def test_fixing_a_component_twice_refuses(self):
        constraints = [
            {"kind": "fixed", "reference": "J1",
             "position_mm": [0.0, 0.0]},
            {"kind": "fixed", "reference": "J1",
             "position_mm": [5.0, 5.0]},
        ]
        with self.assertRaises(PlacementError):
            placement.validate_constraint_set(constraints)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()


class EvaluationJudgesActualPositions(unittest.TestCase):

    def _positions(self):
        return {
            "U1": {"x_mm": 0.0, "y_mm": 0.0, "rotation_deg": 0.0},
            "C1": {"x_mm": 1.0, "y_mm": 0.0, "rotation_deg": 90.0},
            "C2": {"x_mm": 30.0, "y_mm": 0.0, "rotation_deg": 0.0},
            "J1": {"x_mm": 55.0, "y_mm": 0.0, "rotation_deg": 0.0},
        }

    def test_violations_are_detected_with_measurements(self):
        outcome = placement.evaluate_placement(self._positions(), [
            {"kind": "proximity", "reference": "C1", "anchor": "U1",
             "max_distance_mm": 2.0},
            {"kind": "proximity", "reference": "C2", "anchor": "U1",
             "max_distance_mm": 2.0},
            {"kind": "separation", "group_a": ["U1", "C1"],
             "group_b": ["C2", "J1"], "min_distance_mm": 10.0},
        ])
        statuses = [entry["status"]
                    for entry in outcome["results"]]
        self.assertEqual(statuses,
                         ["satisfied", "violated", "satisfied"])
        self.assertEqual(outcome["summary"]["violated"], [1])
        self.assertFalse(outcome["summary"]["ok"])
        self.assertEqual(
            outcome["results"][1]["measured"]["distance_mm"], 30.0)

    def test_a_missing_reference_refuses_the_evaluation(self):
        with self.assertRaises(PlacementError):
            placement.evaluate_placement(self._positions(), [
                {"kind": "proximity", "reference": "GHOST",
                 "anchor": "U1", "max_distance_mm": 2.0}])

    def test_board_edge_without_an_outline_blocks(self):
        outcome = placement.evaluate_placement(self._positions(), [
            {"kind": "board_edge", "reference": "J1",
             "max_distance_mm": 5.0}])
        self.assertEqual(outcome["results"][0]["status"],
                         "not_evaluable")
        self.assertFalse(outcome["summary"]["ok"])
        with_outline = placement.evaluate_placement(
            self._positions(), [
                {"kind": "board_edge", "reference": "J1",
                 "max_distance_mm": 6.0}],
            outline={"kind": "circle", "center_mm": [0.0, 0.0],
                     "radius_mm": 60.0})
        self.assertEqual(with_outline["results"][0]["status"],
                         "satisfied")

    def test_ordering_projects_onto_the_axis(self):
        ordered = placement.evaluate_placement(self._positions(), [
            {"kind": "ordering", "path": "esd-then-connector",
             "references": ["U1", "C2", "J1"]}])
        self.assertEqual(ordered["results"][0]["status"],
                         "satisfied")
        scrambled = placement.evaluate_placement(self._positions(), [
            {"kind": "ordering", "path": "esd-then-connector",
             "references": ["C2", "U1", "J1"]}])
        self.assertEqual(scrambled["results"][0]["status"],
                         "violated")

    def test_functional_block_reports_spread(self):
        outcome = placement.evaluate_placement(self._positions(), [
            {"kind": "functional_block", "name": "regulator",
             "members": ["U1", "C1"], "max_spread_mm": 5.0},
            {"kind": "functional_block", "name": "loose",
             "members": ["U1", "C2"]},
        ])
        self.assertEqual(outcome["results"][0]["status"],
                         "satisfied")
        self.assertEqual(outcome["results"][1]["status"],
                         "unthresholded")
        self.assertEqual(
            outcome["results"][1]["measured"]["max_pairwise_mm"],
            30.0)


class OverlapIsCheckedNotAssumed(unittest.TestCase):

    def test_overlaps_and_touches_are_distinguished(self):
        pairs = placement.overlapping_pairs({
            "A": [0.0, 0.0, 2.0, 2.0],
            "B": [1.0, 1.0, 3.0, 3.0],
            "C": [2.0, 0.0, 4.0, 1.5],
        })
        self.assertEqual(pairs, [("A", "B"), ("B", "C")])
        touching = placement.overlapping_pairs({
            "A": [0.0, 0.0, 2.0, 2.0],
            "B": [2.0, 0.0, 4.0, 2.0],
        })
        self.assertEqual(touching, [])

    def test_degenerate_boxes_refuse(self):
        with self.assertRaises(PlacementError):
            placement.overlapping_pairs({
                "A": [0.0, 0.0, 0.0, 2.0]})
