"""The typed A/B metric contract: shapes that cannot lie.

What these tests pin: an unmeasured metric can never carry a value
(absence never reads as zero), scopes are never conflated, and every
report binds the exact board, toolkit, physical evidence and schema
identity it was produced under.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import benchmark                        # noqa: E402
from pcbqa.benchmark import BenchmarkError         # noqa: E402


def _binding():
    return {"board_file_sha256": "a" * 64,
            "toolkit_commit": "deadbeef",
            "physical_evidence": "approved catalog f7cd05e75e1f9d6d",
            "schema_version": benchmark.SCHEMA_VERSION}


class MetricsCannotLieByShape(unittest.TestCase):

    def test_measured_and_unmeasured_shapes(self):
        measured = benchmark.measured(
            "copper_length_mm", "net", 15.0, "mm",
            "geometry-derived", {"extractor": "pcbqa.extract"},
            "track segments of one net")
        self.assertEqual(measured["status"], "measured")
        unmeasured = benchmark.unmeasured(
            "pdn_impedance", "board", "ngspice backend",
            "no PDN simulation backend is wired in yet")
        self.assertEqual(unmeasured["status"], "unmeasured")

    def test_an_unmeasured_metric_cannot_carry_a_value(self):
        with self.assertRaises(BenchmarkError):
            benchmark.validate_metric({
                "name": "pdn_impedance", "scope": "board",
                "status": "unmeasured", "blocked_on": "backend",
                "why_unmeasured": "not wired", "value": 0.0})

    def test_a_measured_metric_needs_everything(self):
        for missing in ("units", "evidence_class", "provenance",
                        "applicability"):
            metric = benchmark.measured(
                "x", "net", 1.0, "mm", "geometry-derived",
                {"a": 1}, "scope note")
            del metric[missing]
            with self.assertRaises(BenchmarkError):
                benchmark.validate_metric(metric)
        for value in (float("nan"), float("inf"), True):
            with self.assertRaises(BenchmarkError):
                benchmark.measured("x", "net", value, "mm",
                                   "geometry-derived", {"a": 1}, "s")

    def test_scopes_are_never_conflated(self):
        net_metric = benchmark.measured(
            "copper_length_mm", "net", 15.0, "mm",
            "geometry-derived", {"a": 1}, "one net")
        path_metric = benchmark.measured(
            "copper_length_mm", "electrical-path", 77.3, "mm",
            "geometry-derived", {"a": 1}, "one declared path")
        self.assertFalse(benchmark.comparable(net_metric,
                                              path_metric))
        same_scope = benchmark.measured(
            "copper_length_mm", "net", 12.0, "mm",
            "geometry-derived", {"a": 1}, "another board's net")
        self.assertTrue(benchmark.comparable(net_metric, same_scope))
        with self.assertRaises(BenchmarkError):
            benchmark.validate_metric(dict(net_metric,
                                           scope="everywhere"))

    def test_reports_bind_their_identity(self):
        metrics = [benchmark.measured(
            "copper_length_mm", "net", 15.0, "mm",
            "geometry-derived", {"a": 1}, "one net")]
        report = benchmark.report(_binding(), metrics)
        self.assertEqual(report["binding"]["schema_version"],
                         benchmark.SCHEMA_VERSION)
        bad = _binding()
        bad["schema_version"] = "ab-metrics-1"
        with self.assertRaises(BenchmarkError):
            benchmark.report(bad, metrics)
        bad = _binding()
        bad["board_file_sha256"] = "not-a-digest"
        with self.assertRaises(BenchmarkError):
            benchmark.report(bad, metrics)
        with self.assertRaises(BenchmarkError):
            benchmark.report(_binding(), metrics + metrics)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
