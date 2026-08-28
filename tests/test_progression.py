"""Candidate progression: correctness classes in order, no proxy for
board-wide truth, no scalar overriding a class."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import progression                       # noqa: E402
from pcbqa.progression import ProgressionError      # noqa: E402


def _record(**overrides):
    base = {
        "netlist_parity": {"ok": True,
                           "detail": "authoritative contract"},
        "placement_policy_ok": True,
        "critical": {"nets_connected": True,
                     "paths_resolved": True,
                     "topology_valid": True},
        "board_required_connectivity": {"complete": 83, "total": 83},
        "benchmark_connectivity": {"complete": 46, "total": 46},
        "fabrication_geometry": {"ok": True,
                                 "detail": "stage DRC geometry"},
        "blocking_gates": {"evaluated": True, "failing": []},
        "quality_gates": {"evaluated": True, "failing": []},
        "electrical_evidence": {"usable_results": 1},
        "optimization": {"copper_mm": 2000.0},
    }
    base.update(overrides)
    return base


class BenchmarkIsNotTheBoard(unittest.TestCase):

    def test_benchmark_completion_never_reads_as_board_completion(
            self):
        """46/46 on the benchmark set with 79/83 board-wide is NOT
        fully connected, and progression stops at
        board-connectivity."""
        outcome = progression.assess(_record(
            board_required_connectivity={"complete": 79,
                                         "total": 83}))
        self.assertIs(outcome["fully_connected"], False)
        self.assertEqual(outcome["progress_class"],
                         "board-connectivity")
        self.assertIs(outcome["candidate_ready_for_next_stage"],
                      False)
        detail = outcome["classes"]["board-connectivity"]
        self.assertEqual(detail["board_required"]["complete"], 79)
        self.assertEqual(detail["benchmark"]["complete"], 46)

    def test_rank_separates_board_completion_from_benchmark(self):
        """A board-complete candidate outranks a benchmark-complete
        one whatever its later metrics."""
        complete = progression.assess(_record(
            electrical_evidence={"usable_results": 0},
            optimization={"copper_mm": 9999.0}))
        benchmark_only = progression.assess(_record(
            board_required_connectivity={"complete": 79,
                                         "total": 83},
            optimization={"copper_mm": 1.0}))
        self.assertGreater(complete["rank_key"],
                           benchmark_only["rank_key"])


class CriticalTruthIsPolicyOwned(unittest.TestCase):

    def test_connected_but_invalid_topology_is_not_critical_valid(
            self):
        outcome = progression.assess(_record(
            critical={"nets_connected": True,
                      "paths_resolved": True,
                      "topology_valid": False}))
        self.assertEqual(
            outcome["classes"]["critical-structures"]["status"],
            "fail")
        self.assertEqual(outcome["progress_class"],
                         "critical-structures")

    def test_unknown_path_truth_is_not_credited(self):
        outcome = progression.assess(_record(
            critical={"nets_connected": True,
                      "paths_resolved": "unknown",
                      "topology_valid": True}))
        self.assertEqual(
            outcome["classes"]["critical-structures"]["status"],
            "unknown")
        self.assertIs(outcome["candidate_ready_for_next_stage"],
                      False)
        # rank_key: (parity, placement, nets, paths, ...) -
        # the unknown path truth must read False, never True.
        self.assertIn(False, [outcome["rank_key"][3]])

    def test_comparison_worth_is_not_board_validity(self):
        outcome = progression.assess(_record(
            board_required_connectivity={"complete": 79,
                                         "total": 83},
            critical={"nets_connected": True,
                      "paths_resolved": False,
                      "topology_valid": False}))
        self.assertIs(outcome["accept_for_comparison"], True)
        self.assertIs(outcome["candidate_ready_for_next_stage"],
                      False)


class NoScalarOverridesACorrectnessClass(unittest.TestCase):

    def test_fabrication_invalid_cannot_win_on_copper(self):
        """A fabrication-invalid candidate with beautiful copper
        loses to a fabrication-valid peer: optimization metrics are
        recorded, never ranked."""
        valid = progression.assess(_record(
            optimization={"copper_mm": 5000.0}))
        invalid = progression.assess(_record(
            fabrication_geometry={"ok": False,
                                  "detail": "clearance below the "
                                            "declared minimum"},
            optimization={"copper_mm": 1000.0}))
        self.assertGreater(valid["rank_key"], invalid["rank_key"])
        self.assertNotIn("copper_mm", str(valid["rank_key"]))
        self.assertEqual(invalid["progress_class"],
                         "fabrication-geometry")

    def test_unknown_fabrication_truth_blocks_readiness(self):
        outcome = progression.assess(_record(
            fabrication_geometry={"ok": "unknown",
                                  "detail": "stage check did not "
                                            "run"}))
        self.assertIs(outcome["candidate_ready_for_next_stage"],
                      False)
        self.assertEqual(outcome["progress_class"],
                         "fabrication-geometry")


class ParityLeadsAndEligibilityIsSplit(unittest.TestCase):

    def test_failed_parity_stops_everything(self):
        """A candidate that does not implement the product intent
        is a different product: nothing downstream may outrank
        that, and it is not even comparison-worthy."""
        outcome = progression.assess(_record(
            netlist_parity={"ok": False,
                            "detail": "net assignments differ"}))
        self.assertEqual(outcome["progress_class"],
                         "netlist-parity")
        self.assertIs(outcome["accept_for_comparison"], False)
        self.assertIs(outcome["search_winner_eligible"], False)
        perfect_but_wrong = outcome["rank_key"]
        honest_incomplete = progression.assess(_record(
            board_required_connectivity={"complete": 1,
                                         "total": 83},
            electrical_evidence={"usable_results": 0}))["rank_key"]
        self.assertGreater(honest_incomplete, perfect_but_wrong)

    def test_unknown_parity_is_not_credited(self):
        outcome = progression.assess(_record(
            netlist_parity={"ok": "unknown",
                            "detail": "contract not evaluated"}))
        self.assertEqual(outcome["progress_class"],
                         "netlist-parity")
        self.assertIs(outcome["candidate_ready_for_next_stage"],
                      False)
        self.assertIs(outcome["accept_for_comparison"], False)

    def test_failed_paths_are_measurable_but_never_best(self):
        """The eligibility split: measured for comparison, refused
        as winner - a search winner must never outrank an
        unresolved or failed correctness class."""
        outcome = progression.assess(_record(
            critical={"nets_connected": True,
                      "paths_resolved": False,
                      "topology_valid": True}))
        self.assertIs(outcome["accept_for_comparison"], True)
        self.assertIs(outcome["search_winner_eligible"], False)
        unresolved = progression.assess(_record(
            critical={"nets_connected": True,
                      "paths_resolved": "unknown",
                      "topology_valid": True}))
        self.assertIs(unresolved["accept_for_comparison"], True)
        self.assertIs(unresolved["search_winner_eligible"], False)
        clean = progression.assess(_record())
        self.assertIs(clean["search_winner_eligible"], True)


class InputsValidateStrictly(unittest.TestCase):

    def test_parity_shape_validates(self):
        with self.assertRaises(ProgressionError):
            progression.assess(_record(netlist_parity={"ok": True}))
        with self.assertRaises(ProgressionError):
            progression.assess(_record(
                netlist_parity={"ok": "yes", "detail": "x"}))

    def test_unknown_keys_refuse(self):
        record = _record()
        record["extra"] = 1
        with self.assertRaises(ProgressionError):
            progression.assess(record)

    def test_nets_connected_may_not_be_unknown(self):
        with self.assertRaises(ProgressionError):
            progression.assess(_record(
                critical={"nets_connected": "unknown",
                          "paths_resolved": True,
                          "topology_valid": True}))

    def test_counts_validate(self):
        with self.assertRaises(ProgressionError):
            progression.assess(_record(
                board_required_connectivity={"complete": 90,
                                             "total": 83}))


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
