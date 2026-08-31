"""The parasitic contract: omissions never make exact claims,
out-of-domain metrics refuse, descriptive metrics never gate, and
unmatched fidelity never compares."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import parasitics                        # noqa: E402
from pcbqa.parasitics import ParasiticsError        # noqa: E402


def _metric(**overrides):
    base = {
        "kind": "parasitic-metric",
        "phenomenon": "interconnect_dc",
        "scope": {"level": "path", "identity": "A.1->B.2"},
        "quantity": {"semantics": "exact", "value": 0.0125,
                     "bound": None, "interval": None,
                     "units": "ohm"},
        "model": {"name": "traversal-series-dc",
                  "fidelity": "geometry-derived"},
        "provenance": {"source": "test fixture"},
        "assumptions": [],
        "omitted_contributions": [],
        "applicability": {"applicable": True,
                          "detail": "two-terminal traversal"},
        "requirement_linkage": None,
        "decision_significance": "descriptive",
    }
    base.update(overrides)
    return base


def _linked(op, limit):
    return {"requirement": "max-path-resistance",
            "source": "test constraints",
            "assertion": {"op": op, "value": limit}}


class OmissionsNeverMakeExactClaims(unittest.TestCase):

    def test_exact_with_an_omission_refuses(self):
        with self.assertRaisesRegex(ParasiticsError,
                                    "never exact"):
            parasitics.validate_metric(_metric(
                omitted_contributions=["via barrel resistance"]))

    def test_a_lower_bound_with_omissions_cannot_pass_upward(self):
        """The optimistic direction stays UNKNOWN: a lower-bound
        resistance under a <= requirement can FAIL (already too
        big) but never PASS (the omission could push it over)."""
        record = _metric(
            quantity={"semantics": "bound", "value": None,
                      "bound": {"direction": "lower",
                                "value": 0.010},
                      "interval": None, "units": "ohm"},
            omitted_contributions=["via barrel resistance"],
            requirement_linkage=_linked("<=", 0.020))
        self.assertEqual(parasitics.requirement_verdict(record),
                         "UNKNOWN")
        record["quantity"]["bound"]["value"] = 0.025
        self.assertEqual(parasitics.requirement_verdict(record),
                         "FAIL")
        upper = _metric(
            quantity={"semantics": "bound", "value": None,
                      "bound": {"direction": "upper",
                                "value": 0.015},
                      "interval": None, "units": "ohm"},
            omitted_contributions=["contact resistance spread"],
            requirement_linkage=_linked("<=", 0.020))
        self.assertEqual(parasitics.requirement_verdict(upper),
                         "PASS")

    def test_approximations_never_decide(self):
        record = _metric(
            quantity={"semantics": "approximate", "value": 0.012,
                      "bound": None, "interval": None,
                      "units": "ohm"},
            assumptions=["uniform copper thickness"],
            requirement_linkage=_linked("<=", 100.0))
        self.assertEqual(parasitics.requirement_verdict(record),
                         "UNKNOWN")

    def test_unexplained_approximation_refuses(self):
        with self.assertRaises(ParasiticsError):
            parasitics.validate_metric(_metric(
                quantity={"semantics": "approximate",
                          "value": 0.012, "bound": None,
                          "interval": None, "units": "ohm"}))


class ApplicabilityIsAHardDomain(unittest.TestCase):

    def test_out_of_domain_metrics_refuse(self):
        with self.assertRaisesRegex(ParasiticsError,
                                    "applicability"):
            parasitics.validate_metric(_metric(
                applicability={"applicable": False,
                               "detail": "geometry outside the "
                                         "solver's domain"}))

    def test_blockage_records_name_what_is_needed(self):
        record = parasitics.blocked(
            "propagation_delay", "path", "clock-branch-3",
            "the path crosses a series component whose "
            "propagation contribution has no model",
            "a component traversal model, or a copper-only bound")
        self.assertEqual(record["kind"], "parasitic-blocked")
        self.assertIn("series component", record["reason"])
        with self.assertRaises(ParasiticsError):
            parasitics.blocked("vibes", "path", "x", "r", "n")


class DescriptiveMetricsNeverGate(unittest.TestCase):

    def test_no_linkage_yields_no_verdict(self):
        self.assertIsNone(parasitics.requirement_verdict(
            _metric()))

    def test_linked_exact_decides(self):
        record = _metric(requirement_linkage=_linked("<=", 0.020))
        self.assertEqual(parasitics.requirement_verdict(record),
                         "PASS")
        record = _metric(requirement_linkage=_linked("<=", 0.010))
        self.assertEqual(parasitics.requirement_verdict(record),
                         "FAIL")

    def test_straddling_intervals_stay_unknown(self):
        record = _metric(
            quantity={"semantics": "interval", "value": None,
                      "bound": None,
                      "interval": {"lower": 0.010,
                                   "upper": 0.030},
                      "units": "ohm"},
            requirement_linkage=_linked("<=", 0.020))
        self.assertEqual(parasitics.requirement_verdict(record),
                         "UNKNOWN")


class ComparisonsRefuseUnmatchedFidelity(unittest.TestCase):

    def test_matched_metrics_compare(self):
        self.assertTrue(parasitics.require_comparable(
            _metric(), _metric(
                quantity={"semantics": "exact", "value": 0.02,
                          "bound": None, "interval": None,
                          "units": "ohm"})))

    def test_mismatches_refuse_by_name(self):
        with self.assertRaisesRegex(ParasiticsError, "evidence_class"):
            parasitics.require_comparable(_metric(), _metric(
                model={"name": "traversal-series-dc",
                       "fidelity": "field-solved"}))
        with self.assertRaisesRegex(ParasiticsError, "knowledge"):
            parasitics.require_comparable(_metric(), _metric(
                quantity={"semantics": "bound", "value": None,
                          "bound": {"direction": "lower",
                                    "value": 0.01},
                          "interval": None, "units": "ohm"},
                omitted_contributions=["via barrels"]))
        with self.assertRaisesRegex(ParasiticsError, "units"):
            parasitics.require_comparable(_metric(), _metric(
                quantity={"semantics": "exact", "value": 12.5,
                          "bound": None, "interval": None,
                          "units": "mm"}))


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
