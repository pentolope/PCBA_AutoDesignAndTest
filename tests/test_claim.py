"""The shared evidence model, and that every producer reaches it.

Five vocabularies said the same handful of things in different words. This
checks the one that replaced them: the conservative verdict rule in full, the
refusals that must survive, and an adapter from each producer - because a
shared model nothing adapts into is a sixth vocabulary, not a unification.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import claim, parasitics                            # noqa: E402
from pcbqa.claim import ClaimError                             # noqa: E402


def _claim(**overrides):
    base = dict(
        phenomenon="propagation_delay", scope_level="path", identity="CLK/U1",
        units="ps", knowledge=claim.EXACT, quantity={"value": 100.0},
        evidence_class="analytic", provenance={"source": "a test"},
        significance="for this test only")
    base.update(overrides)
    return claim.claim(**base)


class TheVerdictIsConservative(unittest.TestCase):
    """The rule every producer now shares, in full."""

    def _verdict(self, knowledge, quantity, op, limit):
        return claim.verdict(_claim(
            knowledge=knowledge, quantity=quantity,
            assumptions=["under test"] if knowledge == claim.APPROXIMATE
            else [],
            applicability={"applicable": knowledge != claim.UNKNOWN,
                           "detail": ""},
            requirement={"requirement": "R1", "source": "the test",
                         "assertion": {"op": op, "value": limit}}))

    def test_a_descriptive_claim_never_becomes_a_gate(self):
        self.assertIsNone(claim.verdict(_claim()))

    def test_exact_decides_both_ways(self):
        self.assertEqual(self._verdict(claim.EXACT, {"value": 5.0}, "<=", 10),
                         "PASS")
        self.assertEqual(self._verdict(claim.EXACT, {"value": 15.0}, "<=", 10),
                         "FAIL")
        self.assertEqual(self._verdict(claim.EXACT, {"value": 15.0}, ">=", 10),
                         "PASS")

    def test_an_upper_bound_decides_only_downwards(self):
        self.assertEqual(
            self._verdict(claim.UPPER_BOUND, {"value": 5.0}, "<=", 10), "PASS")
        self.assertEqual(
            self._verdict(claim.UPPER_BOUND, {"value": 15.0}, "<=", 10),
            "UNKNOWN", "an upper bound above the limit proves nothing")
        self.assertEqual(
            self._verdict(claim.UPPER_BOUND, {"value": 5.0}, ">=", 10), "FAIL")

    def test_a_lower_bound_decides_only_upwards(self):
        self.assertEqual(
            self._verdict(claim.LOWER_BOUND, {"value": 15.0}, ">=", 10),
            "PASS")
        self.assertEqual(
            self._verdict(claim.LOWER_BOUND, {"value": 5.0}, ">=", 10),
            "UNKNOWN")
        self.assertEqual(
            self._verdict(claim.LOWER_BOUND, {"value": 15.0}, "<=", 10),
            "FAIL")

    def test_an_interval_decides_only_when_it_decides_entirely(self):
        span = {"lower": 4.0, "upper": 6.0}
        self.assertEqual(self._verdict(claim.INTERVAL, span, "<=", 10), "PASS")
        self.assertEqual(self._verdict(claim.INTERVAL, span, "<=", 5),
                         "UNKNOWN", "a limit inside the interval is undecided")
        self.assertEqual(self._verdict(claim.INTERVAL, span, "<=", 3), "FAIL")

    def test_an_approximation_never_decides(self):
        for op, limit in (("<=", 10), (">=", 10), ("<=", 0)):
            self.assertEqual(
                self._verdict(claim.APPROXIMATE, {"value": 5.0}, op, limit),
                "UNKNOWN")

    def test_an_unknown_never_decides(self):
        self.assertEqual(self._verdict(claim.UNKNOWN, {}, "<=", 10), "UNKNOWN")


class TheRefusalsSurvive(unittest.TestCase):

    def test_a_claim_without_units_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(units="")

    def test_a_claim_without_a_source_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(provenance={"model": "something"})

    def test_a_claim_with_no_stated_significance_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(significance="")

    def test_exact_with_an_omission_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(omitted_contributions=["via barrels"])

    def test_an_unexplained_approximation_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(knowledge=claim.APPROXIMATE, quantity={"value": 1.0})

    def test_an_inapplicable_claim_must_know_nothing(self):
        with self.assertRaises(ClaimError):
            _claim(applicability={"applicable": False, "detail": "outside"})
        self.assertTrue(_claim(
            knowledge=claim.UNKNOWN, quantity={},
            applicability={"applicable": False, "detail": "outside"}))

    def test_the_wrong_value_fields_for_the_knowledge_kind_refuse(self):
        with self.assertRaises(ClaimError):
            _claim(knowledge=claim.INTERVAL, quantity={"value": 1.0})
        with self.assertRaises(ClaimError):
            _claim(knowledge=claim.EXACT,
                   quantity={"lower": 1.0, "upper": 2.0})
        with self.assertRaises(ClaimError):
            _claim(knowledge=claim.UNKNOWN, quantity={"value": 1.0})

    def test_an_inverted_interval_refuses(self):
        with self.assertRaises(ClaimError):
            _claim(knowledge=claim.INTERVAL,
                   quantity={"lower": 9.0, "upper": 1.0})

    def test_a_comparison_across_unmatched_evidence_refuses(self):
        with self.assertRaises(ClaimError):
            claim.require_comparable(_claim(),
                                     _claim(evidence_class="measured"))
        with self.assertRaises(ClaimError):
            claim.require_comparable(_claim(), _claim(units="mm"))
        self.assertTrue(claim.require_comparable(
            _claim(), _claim(quantity={"value": 200.0})))


class TheMigratedProducerReachesTheSharedModel(unittest.TestCase):
    """The adapter must preserve everything its producer knew."""

    def test_a_parasitic_metric(self):
        metric = {
            "kind": "parasitic-metric", "phenomenon": "coupling",
            "scope": {"level": "pair", "identity": "A||B"},
            "quantity": {"semantics": "exact", "value": 3.0, "bound": None,
                         "interval": None, "units": "mm"},
            "model": {"name": "parallelism-inventory",
                      "fidelity": "geometry-only"},
            "provenance": {"source": "pcbqa.coupling_geometry"},
            "assumptions": [], "omitted_contributions": [],
            "applicability": {"applicable": True, "detail": "one layer"},
            "requirement_linkage": None,
            "decision_significance": "ranking only"}
        parasitics.validate_metric(metric)
        record = claim.from_parasitic_metric(metric)
        self.assertEqual(record["knowledge"], claim.EXACT)
        self.assertEqual(record["units"], "mm")
        self.assertIsNone(claim.verdict(record))


class OnlyMigratedProducersHaveAdapters(unittest.TestCase):
    """An adapter no producer calls is reserved architecture.

    This toolkit deleted a backend-dispatch package for exactly that reason in
    the same pass. One producer is migrated, so there is one adapter; the next
    adapter is written with the producer that uses it.
    """

    def test_there_is_an_adapter_for_every_production_caller(self):
        adapters = {name for name in dir(claim) if name.startswith("from_")}
        self.assertEqual(adapters, {"from_parasitic_metric"}, adapters)

    def test_the_unmigrated_producers_are_named_honestly(self):
        """The module says which producers still carry their own vocabulary."""
        doc = " ".join(claim.__doc__.split())
        self.assertIn("Only `pcbqa.parasitics` has been migrated", doc)
        for producer in ("propagation", "component_models", "sim/fidelity",
                         "sim/scenario"):
            self.assertIn(producer, doc)


class ThereIsOneVerdictImplementation(unittest.TestCase):

    def test_parasitics_delegates_rather_than_repeating_it(self):
        import inspect
        source = inspect.getsource(parasitics.requirement_verdict)
        self.assertIn("claim", source)
        for repeated in ('"PASS"', '"FAIL"'):
            self.assertNotIn(repeated, source,
                             "the conservative rule is implemented twice")


if __name__ == "__main__":
    unittest.main()
