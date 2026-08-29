"""Compute accounting: disjoint categories, sums that must add up,
classifications that are stated rather than guessed."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import compute                           # noqa: E402
from pcbqa.compute import ComputeError              # noqa: E402


def _entry(label, category, seconds, classification=None):
    entry = {"label": label, "category": category,
             "seconds": seconds}
    if classification is not None:
        entry["classification"] = classification
    return entry


class TheLedgerIsHonestOrRefused(unittest.TestCase):

    def test_a_consistent_ledger_summarises(self):
        record = compute.summarize(
            [_entry("probe a", "probe-routing", 60.0, "diagnostic"),
             _entry("route a", "full-routing", 240.0,
                    "useful-finalist")],
            measured_total_seconds=300.0)
        self.assertEqual(record["categorized_total_seconds"], 300.0)
        self.assertEqual(record["by_category"]["probe-routing"],
                         60.0)
        self.assertEqual(
            record["by_classification"]["diagnostic"], 60.0)

    def test_double_counting_refuses(self):
        with self.assertRaisesRegex(ComputeError, "double-count"):
            compute.summarize(
                [_entry("probe", "probe-routing", 60.0),
                 _entry("same seconds again", "full-routing", 60.0)],
                measured_total_seconds=60.0)

    def test_unaccounted_time_refuses(self):
        with self.assertRaisesRegex(ComputeError, "unaccounted"):
            compute.summarize(
                [_entry("probe", "probe-routing", 10.0)],
                measured_total_seconds=100.0)

    def test_an_unknown_category_refuses(self):
        with self.assertRaisesRegex(ComputeError, "not in the "
                                    "declared set"):
            compute.summarize([_entry("x", "vibes", 1.0)])

    def test_an_unknown_classification_refuses(self):
        with self.assertRaisesRegex(ComputeError, "classification"):
            compute.summarize(
                [_entry("x", "repair", 1.0, "probably-fine")])

    def test_negative_and_boolean_seconds_refuse(self):
        with self.assertRaisesRegex(ComputeError, "non-negative"):
            compute.summarize([_entry("x", "repair", -1.0)])
        with self.assertRaisesRegex(ComputeError, "non-negative"):
            compute.summarize([_entry("x", "repair", True)])

    def test_a_repeated_category_name_refuses(self):
        with self.assertRaisesRegex(ComputeError, "disjoint"):
            compute.summarize([], categories=("a", "a"))

    def test_no_measured_total_still_sums_but_asserts_nothing(self):
        record = compute.summarize(
            [_entry("probe", "probe-routing", 10.0)])
        self.assertIsNone(record["measured_total_seconds"])
        self.assertNotIn("difference_seconds", record)
        # And the record SAYS it asserted nothing: a summary that
        # claimed reconciliation it never performed would be the
        # module's own failure mode.
        self.assertIn("NO measured total", record["meaning"])
        self.assertIn("unaccounted", record["meaning"])

    def test_consumer_declared_categories_replace_the_default(self):
        record = compute.summarize(
            [_entry("x", "meshing", 5.0)],
            categories=("meshing", "solving"))
        self.assertEqual(record["by_category"], {"meshing": 5.0})


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
