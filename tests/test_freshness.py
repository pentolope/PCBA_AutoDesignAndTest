"""Producer closures: stale artifacts refuse, unrelated changes do
not invalidate, tampered records never compare."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import freshness                        # noqa: E402
from pcbqa.freshness import FreshnessError         # noqa: E402


def _components(producer_text="def produce(): return 1",
                board_digest="a" * 64):
    return {
        "producer-script": {"text": producer_text},
        "board": {"digest": board_digest},
        "schema": {"text": "ab-metrics-4"},
    }


class ClosuresAreDeliberateAndFailClosed(unittest.TestCase):

    def test_a_changed_producer_invalidates_and_is_named(self):
        recorded = freshness.closure(_components())
        verdict = freshness.verify(
            recorded, _components(
                producer_text="def produce(): return 2"))
        self.assertIs(verdict["fresh"], False)
        self.assertEqual(verdict["moved"], ["producer-script"])

    def test_a_changed_input_invalidates(self):
        recorded = freshness.closure(_components())
        verdict = freshness.verify(
            recorded, _components(board_digest="b" * 64))
        self.assertIs(verdict["fresh"], False)
        self.assertEqual(verdict["moved"], ["board"])

    def test_unrelated_changes_do_not_invalidate(self):
        """The closure is deliberate: only named dependencies count,
        so a doc or an untouched module changing elsewhere leaves
        the artifact fresh."""
        recorded = freshness.closure(_components())
        verdict = freshness.verify(recorded, _components())
        self.assertIs(verdict["fresh"], True)
        self.assertEqual(verdict["moved"], [])

    def test_a_tampered_record_refuses_instead_of_comparing(self):
        recorded = freshness.closure(_components())
        recorded["components"]["board"] = "c" * 64
        with self.assertRaises(FreshnessError):
            freshness.verify(recorded, _components())

    def test_component_kinds_validate(self):
        with self.assertRaises(FreshnessError):
            freshness.closure({"x": {"digest": "short"}})
        with self.assertRaises(FreshnessError):
            freshness.closure({})
        with self.assertRaises(FreshnessError):
            freshness.closure({"x": {"path": os.path.join(
                HERE, "no-such-file-exists")}})


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
