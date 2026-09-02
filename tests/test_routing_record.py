"""The routing-run record must reject the ways a routed board goes untrue.

Each case here is a defect that leaves copper in the tree that nothing
judged, and that no amount of looking at the board can reveal. A test
passes by the record being REJECTED.
"""

from __future__ import annotations

import copy
import unittest

from pcbqa import routing_record


DIGEST_SOURCE = "a" * 64
DIGEST_ROUTED = "b" * 64
DIGEST_TIDIED = "c" * 64
DIGEST_OTHER = "d" * 64


def _stage(name, produced_by, digest, **extra):
    stage = {"stage": name, "produced_by": produced_by, "sha256": digest}
    stage.update(extra)
    return stage


def _record(**overrides):
    record = {
        "kind": routing_record.KIND,
        "source_sha256": DIGEST_SOURCE,
        "attempts": [{
            "attempt": 1,
            "source_sha256": DIGEST_SOURCE,
            "accepted": True,
            "stages": [
                _stage("routed", routing_record.ROUTER, DIGEST_ROUTED),
                _stage("tidied", routing_record.TRANSFORM, DIGEST_TIDIED,
                       transform="snap track ends onto same-net via centres",
                       effects={"endpoints_snapped": 4}),
            ],
        }],
        "accepted_attempt": 1,
        "adopted_sha256": DIGEST_TIDIED,
    }
    record.update(overrides)
    return record


class AcceptsAWellFormedRun(unittest.TestCase):
    def test_a_complete_record_validates(self):
        self.assertIsNotNone(routing_record.validate(_record()))

    def test_a_run_with_no_accepted_candidate_validates(self):
        record = _record()
        record["attempts"][0]["accepted"] = False
        record["accepted_attempt"] = None
        record["adopted_sha256"] = None
        self.assertIsNotNone(routing_record.validate(record))

    def test_transforms_are_reported(self):
        self.assertEqual(
            [stage["stage"] for stage in
             routing_record.transforms(_record())],
            ["tidied"])


class RejectsUntrueRuns(unittest.TestCase):
    def _refuses(self, record, fragment):
        with self.assertRaises(routing_record.RoutingRecordError) as caught:
            routing_record.validate(record)
        self.assertIn(fragment, str(caught.exception))

    def test_attempt_routed_on_a_previous_attempts_output(self):
        record = _record()
        record["attempts"].append({
            "attempt": 2,
            "source_sha256": DIGEST_ROUTED,
            "accepted": False,
            "stages": [_stage("routed", routing_record.ROUTER, DIGEST_OTHER)],
        })
        self._refuses(record, "routed on top of one another")

    def test_adoption_that_is_not_the_end_of_the_chain(self):
        record = _record(adopted_sha256=DIGEST_ROUTED)
        self._refuses(record, "unrecorded transform")

    def test_adoption_without_an_accepted_candidate(self):
        record = _record()
        record["attempts"][0]["accepted"] = False
        record["accepted_attempt"] = None
        self._refuses(record, "nothing may be recorded as adopted")

    def test_transform_without_a_description(self):
        record = _record()
        record["attempts"][0]["stages"][1].pop("transform")
        self._refuses(record, "stage.transform")

    def test_transform_without_measured_effects(self):
        record = _record()
        record["attempts"][0]["stages"][1]["effects"] = {}
        self._refuses(record, "unmeasured edit")

    def test_transform_before_the_router(self):
        record = _record()
        record["attempts"][0]["stages"].reverse()
        self._refuses(record, "router stage comes first")

    def test_two_router_stages_in_one_attempt(self):
        record = _record()
        record["attempts"][0]["stages"].append(
            _stage("rerouted", routing_record.ROUTER, DIGEST_OTHER))
        self._refuses(record, "exactly one router stage")

    def test_duplicate_attempt_numbers(self):
        record = _record()
        record["attempts"].append(copy.deepcopy(record["attempts"][0]))
        record["attempts"][1]["accepted"] = False
        self._refuses(record, "unique")

    def test_two_accepted_attempts(self):
        record = _record()
        second = copy.deepcopy(record["attempts"][0])
        second["attempt"] = 2
        record["attempts"].append(second)
        self._refuses(record, "at most one attempt may be accepted")


class BindsToTheBoardInTheTree(unittest.TestCase):
    def test_agreeing_board_reports_nothing(self):
        self.assertEqual(
            routing_record.compare_to_board(_record(), DIGEST_TIDIED), [])

    def test_board_that_is_a_later_derivative_is_caught(self):
        problems = routing_record.compare_to_board(_record(), DIGEST_OTHER)
        self.assertEqual(len(problems), 1)
        self.assertIn("not the candidate the record describes",
                      problems[0]["issue"])

    def test_unaccepted_run_leaves_no_recorded_board(self):
        record = _record()
        record["attempts"][0]["accepted"] = False
        record["accepted_attempt"] = None
        record["adopted_sha256"] = None
        problems = routing_record.compare_to_board(record, DIGEST_TIDIED)
        self.assertEqual(len(problems), 1)
        self.assertIn("no candidate was accepted", problems[0]["issue"])



class DesignRuleFloorsRefuseWeakening(unittest.TestCase):
    """A loosened rule is silent where a disabled one is loud."""

    def _findings(self, effective, declared):
        from pcbqa.core import GateResult
        from pcbqa.gates.g_checks import _compare_floor
        result = GateResult("T", "t")
        _compare_floor(result, "rules.min_clearance", effective, declared)
        return result.findings

    def test_equal_to_the_floor_is_accepted(self):
        self.assertEqual(self._findings(0.15, 0.15), [])

    def test_stronger_than_the_floor_is_accepted(self):
        self.assertEqual(self._findings(0.2, 0.15), [])

    def test_weaker_than_the_floor_is_refused(self):
        findings = self._findings(0.1442, 0.15)
        self.assertEqual(len(findings), 1)
        self.assertIn("weaker than the declared floor", findings[0]["issue"])

    def test_absent_rule_cannot_be_proven(self):
        findings = self._findings(None, 0.15)
        self.assertEqual(len(findings), 1)
        self.assertIn("cannot be proven", findings[0]["issue"])

    def test_non_numeric_rule_is_refused(self):
        findings = self._findings("0.15", 0.15)
        self.assertEqual(len(findings), 1)
        self.assertIn("not a number", findings[0]["issue"])


class ExtensionNeverSoftensTheContract(unittest.TestCase):
    def test_consumer_detail_is_allowed_under_context(self):
        record = _record()
        record["context"] = {"router": "krt 0.21.5", "options": ["--x"]}
        record["attempts"][0]["context"] = {"metrics": {"vias": 10}}
        self.assertIsNotNone(routing_record.validate(record))

    def test_an_unknown_top_level_key_is_refused(self):
        record = _record()
        record["routed_sha256"] = DIGEST_ROUTED
        with self.assertRaises(routing_record.RoutingRecordError) as caught:
            routing_record.validate(record)
        self.assertIn("unknown key", str(caught.exception))

    def test_a_missing_contract_key_is_refused(self):
        record = _record()
        record.pop("adopted_sha256")
        with self.assertRaises(routing_record.RoutingRecordError) as caught:
            routing_record.validate(record)
        self.assertIn("missing", str(caught.exception))

if __name__ == "__main__":
    unittest.main()
