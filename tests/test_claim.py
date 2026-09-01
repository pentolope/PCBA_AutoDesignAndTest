"""Behavioral tests for the shared evidence and numeric-claim contract."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import claim, component_models                    # noqa: E402
from pcbqa.claim import ClaimError                           # noqa: E402


def _evidence(status=claim.APPLICABLE, evidence_class="analytic",
              assumptions=(), omissions=()):
    return claim.evidence(
        "propagation_delay",
        evidence_class if status == claim.APPLICABLE else None,
        {"source": "test evidence"},
        applicability={"status": status, "detail": "test applicability"},
        assumptions=assumptions,
        omitted_contributions=omissions)


def _claim(knowledge=claim.EXACT, quantity=None, requirement=None,
           status=claim.APPLICABLE, evidence_class="analytic",
           assumptions=(), omissions=(), basis=None, units="ps",
           significance="usable only for this contract test"):
    if quantity is None:
        quantity = {} if knowledge == claim.UNKNOWN else {"value": 5.0}
    if basis is None and knowledge in (
            claim.LOWER_BOUND, claim.UPPER_BOUND, claim.INTERVAL,
            claim.APPROXIMATE):
        basis = claim.knowledge_basis(
            claim.DERIVED, "the test derives this bound mechanically")
    return claim.claim(
        "path", "CLK/U1", units, knowledge, quantity,
        _evidence(status, evidence_class, assumptions, omissions),
        significance, knowledge_basis=basis, requirement=requirement)


def _require(op, value, tolerance=None):
    assertion = {"op": op, "value": value}
    if tolerance is not None:
        assertion["tolerance"] = tolerance
    return claim.requirement("R1", "test requirement", assertion)


class VerdictsAreConservative(unittest.TestCase):

    def _result(self, knowledge, quantity, op, limit, tolerance=None):
        record = _claim(
            knowledge, quantity,
            requirement=_require(op, limit, tolerance),
            assumptions=["declared approximation"]
            if knowledge == claim.APPROXIMATE else [])
        return claim.verdict(record)

    def test_descriptive_claim_has_no_verdict(self):
        self.assertIsNone(claim.verdict(_claim()))

    def test_exact_results_preserve_exact_pass_and_fail(self):
        passed = self._result(claim.EXACT, {"value": 5}, "<=", 10)
        failed = self._result(claim.EXACT, {"value": 15}, "<=", 10)
        self.assertEqual(passed["result"], claim.PASS)
        self.assertTrue(passed["exact"])
        self.assertEqual(passed["basis"], "exact")
        self.assertEqual(failed["result"], claim.FAIL)
        self.assertTrue(failed["exact"])

    def test_upper_and_lower_bounds_only_conclude_conservatively(self):
        cases = (
            (claim.UPPER_BOUND, {"value": 5}, "<=", 10, claim.PASS),
            (claim.UPPER_BOUND, {"value": 15}, "<=", 10,
             claim.UNKNOWN_RESULT),
            (claim.UPPER_BOUND, {"value": 5}, ">=", 10, claim.FAIL),
            (claim.LOWER_BOUND, {"value": 15}, ">=", 10, claim.PASS),
            (claim.LOWER_BOUND, {"value": 5}, ">=", 10,
             claim.UNKNOWN_RESULT),
            (claim.LOWER_BOUND, {"value": 15}, "<=", 10, claim.FAIL),
        )
        for knowledge, quantity, op, limit, expected in cases:
            with self.subTest(knowledge=knowledge, op=op, limit=limit):
                verdict = self._result(knowledge, quantity, op, limit)
                self.assertEqual(verdict["result"], expected)
                self.assertFalse(verdict["exact"])
                self.assertEqual(verdict["basis"], "bound")
                self.assertEqual(
                    verdict["knowledge_basis"]["kind"], claim.DERIVED)

    def test_interval_and_within_require_the_whole_interval(self):
        span = {"lower": 4.0, "upper": 6.0}
        self.assertEqual(
            self._result(claim.INTERVAL, span, "<=", 10)["result"],
            claim.PASS)
        self.assertEqual(
            self._result(claim.INTERVAL, span, "<=", 5)["result"],
            claim.UNKNOWN_RESULT)
        self.assertEqual(
            self._result(claim.INTERVAL, span, "within", 5, 1)["result"],
            claim.PASS)
        self.assertEqual(
            self._result(claim.INTERVAL, span, "within", 5, 0.5)["result"],
            claim.UNKNOWN_RESULT)

    def test_approximate_and_unknown_never_manufacture_a_result(self):
        approximate = self._result(
            claim.APPROXIMATE, {"value": 5}, "<=", 10)
        unknown = self._result(claim.UNKNOWN, {}, "<=", 10)
        self.assertEqual(approximate["result"], claim.UNKNOWN_RESULT)
        self.assertEqual(unknown["result"], claim.UNKNOWN_RESULT)


class ClaimsRefuseOverstatement(unittest.TestCase):

    def test_units_source_and_significance_are_mandatory(self):
        for change in (
                lambda: _claim(units=""),
                lambda: claim.claim(
                    "path", "CLK/U1", "ps", claim.EXACT, {"value": 1},
                    claim.evidence(
                        "propagation_delay", "analytic", {"model": "x"}),
                    "test"),
                lambda: _claim(significance="")):
            with self.subTest(change=change):
                with self.assertRaises(ClaimError):
                    change()

    def test_exact_cannot_hide_an_omitted_contribution(self):
        with self.assertRaises(ClaimError):
            _claim(omissions=["via barrels"])

    def test_approximate_must_state_what_qualifies_it(self):
        with self.assertRaises(ClaimError):
            claim.claim(
                "path", "CLK/U1", "ps", claim.APPROXIMATE, {"value": 1},
                _evidence(assumptions=["declared approximation"]), "test",
                knowledge_basis=None)

    def test_exact_knowledge_cannot_rest_on_an_assumption(self):
        assumed = claim.knowledge_basis(
            claim.ASSUMED, "the value is only a design premise")
        with self.assertRaises(ClaimError):
            claim.knowledge_declaration(claim.EXACT, assumed)
        with self.assertRaises(ClaimError):
            _claim(claim.EXACT, {"value": 1}, basis=assumed)

    def test_bounded_knowledge_states_derived_or_assumed_justification(self):
        with self.assertRaises(ClaimError):
            claim.claim(
                "path", "CLK/U1", "ps", claim.UPPER_BOUND, {"value": 1},
                _evidence(), "test", knowledge_basis=None)
        assumed = _claim(
            claim.UPPER_BOUND, {"value": 1},
            basis=claim.knowledge_basis(claim.ASSUMED, "declared by owner"))
        self.assertEqual(assumed["knowledge_basis"]["kind"], claim.ASSUMED)

    def test_unsupported_and_not_applicable_are_distinct_and_numeric_unknown(self):
        unsupported = _claim(claim.UNKNOWN, {}, status=claim.UNSUPPORTED)
        outside_scope = _claim(
            claim.UNKNOWN, {}, status=claim.NOT_APPLICABLE)
        self.assertEqual(
            unsupported["evidence"]["applicability"]["status"],
            claim.UNSUPPORTED)
        self.assertEqual(
            outside_scope["evidence"]["applicability"]["status"],
            claim.NOT_APPLICABLE)
        with self.assertRaises(ClaimError):
            _claim(claim.EXACT, {"value": 1}, status=claim.UNSUPPORTED)

    def test_knowledge_shape_and_interval_order_validate(self):
        for knowledge, quantity in (
                (claim.INTERVAL, {"value": 1}),
                (claim.EXACT, {"lower": 1, "upper": 2}),
                (claim.UNKNOWN, {"value": 1}),
                (claim.INTERVAL, {"lower": 9, "upper": 1})):
            with self.subTest(knowledge=knowledge, quantity=quantity):
                with self.assertRaises(ClaimError):
                    _claim(knowledge, quantity)

    def test_comparability_requires_matching_physical_semantics(self):
        self.assertTrue(claim.require_comparable(
            _claim(), _claim(quantity={"value": 20})))
        with self.assertRaises(ClaimError):
            claim.require_comparable(_claim(), _claim(units="mm"))
        with self.assertRaises(ClaimError):
            claim.require_comparable(
                _claim(), _claim(evidence_class="measured"))
        with self.assertRaises(ClaimError):
            claim.require_comparable(
                _claim(claim.UNKNOWN, {}, status=claim.UNSUPPORTED),
                _claim(claim.UNKNOWN, {}, status=claim.NOT_APPLICABLE))


class ComponentModelsProduceSharedClaims(unittest.TestCase):

    def test_fixed_delay_is_exact_for_the_declared_model(self):
        record = component_models.evaluate({
            "model": "fixed_delay", "delay_ps": 42,
            "provenance": "component data sheet"}, "U1.1->U1.2")
        self.assertEqual(record["knowledge"], claim.EXACT)
        self.assertEqual(record["quantity"], {"value": 42.0})
        self.assertEqual(
            record["evidence"]["phenomenon"], "propagation_delay")

    def test_deliberate_omission_is_a_sourced_assumed_interval(self):
        record = component_models.evaluate({
            "model": "none", "justification": "bounded switch delay",
            "max_delay_ps": 80, "provenance": "switch data sheet"}, "SW1")
        self.assertEqual(record["knowledge"], claim.INTERVAL)
        self.assertEqual(record["quantity"], {"lower": 0.0, "upper": 80.0})
        self.assertEqual(record["knowledge_basis"]["kind"], claim.ASSUMED)

    def test_unsupported_model_remains_unknown_and_unsupported(self):
        record = component_models.evaluate({"model": "wavefront"}, "U2")
        self.assertEqual(record["knowledge"], claim.UNKNOWN)
        self.assertEqual(
            record["evidence"]["applicability"]["status"],
            claim.UNSUPPORTED)


if __name__ == "__main__":                    # pragma: no cover
    unittest.main()
