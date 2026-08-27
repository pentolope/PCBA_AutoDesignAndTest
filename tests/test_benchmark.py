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


def _binding(board="a"):
    return {"board_file_sha256": board * 64,
            "toolkit_commit": "deadbeef",
            "physical_evidence": {
                "kind": "approved-catalog-finished-copper",
                "digest": "f" * 64,
                "detail": "approved JLCPCB catalog finished-copper "
                          "records"},
            "schema_version": benchmark.SCHEMA_VERSION}


def _report(board, metrics):
    return benchmark.report(_binding(board), metrics)


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


class TheComparatorRefusesMeaninglessComparisons(unittest.TestCase):

    def _measured(self, name, value):
        return benchmark.measured(
            name, "net", value, "mm", "geometry-derived",
            {"extractor": "pcbqa.extract"}, "track segments")

    def test_free_text_evidence_cannot_bind_a_report(self):
        loose = _binding()
        loose["physical_evidence"] = "approved catalog f7cd05e7"
        with self.assertRaises(BenchmarkError):
            benchmark.report(loose, [])

    def test_unknown_schema_fails_closed(self):
        report_a = _report("a", [self._measured("copper", 10.0)])
        report_b = _report("b", [self._measured("copper", 8.0)])
        report_b["binding"]["schema_version"] = "ab-metrics-99"
        with self.assertRaises(BenchmarkError):
            benchmark.compare_reports(report_a, report_b)

    def test_different_physical_evidence_refuses(self):
        """Two reports whose measurements consumed different physical
        evidence never meet numerically - a comparison across
        different physics is not a comparison."""
        report_a = _report("a", [self._measured("copper", 10.0)])
        report_b = _report("b", [self._measured("copper", 8.0)])
        report_b["binding"]["physical_evidence"] = {
            "kind": "approved-catalog-finished-copper",
            "digest": "0" * 64, "detail": "a different catalog"}
        with self.assertRaises(BenchmarkError):
            benchmark.compare_reports(report_a, report_b)

    def test_mismatched_units_refuse(self):
        report_a = _report("a", [self._measured("copper", 10.0)])
        other = benchmark.measured(
            "copper", "net", 8.0, "mil", "geometry-derived",
            {"extractor": "pcbqa.extract"}, "track segments")
        report_b = _report("b", [other])
        with self.assertRaises(BenchmarkError):
            benchmark.compare_reports(report_a, report_b)

    def test_pairs_blocks_and_asymmetries_stay_separate(self):
        """Measured pairs compare with a delta; a pair with an
        unmeasured side lands in blocked with its blocker and NO
        synthesized number; one-sided metrics are listed apart.
        Board SHAs differ - that is what A/B means."""
        report_a = _report("a", [
            self._measured("copper", 10.0),
            self._measured("only-here", 1.0),
            self._measured("clock", 5.0),
        ])
        report_b = _report("b", [
            self._measured("copper", 8.5),
            benchmark.unmeasured("clock", "net", "routing",
                                 "the candidate has not routed "
                                 "this net"),
        ])
        comparison = benchmark.compare_reports(report_a, report_b)
        self.assertEqual(len(comparison["compared"]), 1)
        pair = comparison["compared"][0]
        self.assertEqual(pair["name"], "copper")
        self.assertAlmostEqual(pair["delta_b_minus_a"], -1.5)
        self.assertEqual(len(comparison["blocked"]), 1)
        self.assertEqual(comparison["blocked"][0]["b_blocked_on"],
                         "routing")
        self.assertNotIn("b_value", comparison["blocked"][0])
        self.assertEqual(comparison["only_a"], ["net:only-here"])
        self.assertNotEqual(
            comparison["binding"]["board_file_sha256_a"],
            comparison["binding"]["board_file_sha256_b"])
