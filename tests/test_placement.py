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
