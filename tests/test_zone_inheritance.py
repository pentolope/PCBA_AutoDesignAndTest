"""The zone-inheritance policy is executable, not decorative:
changing an accepted policy changes behavior or refuses."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                       # noqa: E402
from pcbqa import zone_inheritance                  # noqa: E402
from pcbqa.zone_inheritance import ZonePolicyError  # noqa: E402
from tests import synth                             # noqa: E402


def _policy():
    return {
        "kind": "candidate-zone-inheritance-policy",
        "policy_version": "1",
        "unmatched_zone_policy": "refuse: unclassified zones block",
        "rules": [
            {"match": {"kind": "fill", "net": "GNDX",
                       "layers": ["F.Cu", "B.Cu"]},
             "decision": "inherited-architecture"},
            {"match": {"kind": "rule_area", "name": "guard"},
             "decision": "derived-from-placement"},
        ],
    }


def _board_with_zones():
    board = synth.new_board()
    net = synth.add_net(board, "GNDX")
    synth.add_zone(board, net, [pcbnew.F_Cu],
                   (0.0, 0.0, 10.0, 10.0), fill=True)
    for name, rect in (("guard", (1.0, 1.0, 3.0, 3.0)),
                       ("guard", (14.0, 14.0, 16.0, 16.0))):
        area = synth.add_zone(board, None, [pcbnew.F_Cu], rect,
                              fill=False)
        area.SetIsRuleArea(True)
        area.SetZoneName(name)
    return board


class PolicyDrivesBehavior(unittest.TestCase):

    def test_inheritance_and_pruning_follow_the_rules(self):
        board = _board_with_zones()
        outcome = zone_inheritance.apply_policy(
            board, _policy(), [[0.0, 0.0, 4.0, 4.0]])
        self.assertEqual(outcome["kept"], 2)
        self.assertEqual(outcome["deleted"], 1)
        remaining = [zone for zone in board.Zones()]
        self.assertEqual(len(remaining), 2)

    def test_an_unmatched_zone_refuses(self):
        board = _board_with_zones()
        policy = _policy()
        policy["rules"] = policy["rules"][:1]
        with self.assertRaises(ZonePolicyError):
            zone_inheritance.apply_policy(board, policy,
                                          [[0.0, 0.0, 4.0, 4.0]])

    def test_an_altered_decision_changes_behavior(self):
        """The same board under a changed policy behaves
        differently: switching the guard rule to
        inherited-architecture keeps the stale keepout that
        derived-from-placement deletes."""
        board = _board_with_zones()
        policy = _policy()
        policy["rules"][1]["decision"] = "inherited-architecture"
        outcome = zone_inheritance.apply_policy(
            board, policy, [[0.0, 0.0, 4.0, 4.0]])
        self.assertEqual(outcome["deleted"], 0)
        self.assertEqual(outcome["kept"], 3)

    def test_an_unknown_decision_refuses_validation(self):
        policy = _policy()
        policy["rules"][0]["decision"] = "keep-quietly"
        with self.assertRaises(ZonePolicyError):
            zone_inheritance.validate_policy(policy)

    def test_a_silent_unmatched_policy_refuses_validation(self):
        policy = _policy()
        policy["unmatched_zone_policy"] = "inherit silently"
        with self.assertRaises(ZonePolicyError):
            zone_inheritance.validate_policy(policy)
