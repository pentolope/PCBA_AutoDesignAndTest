"""Producer closures: stale artifacts refuse, unrelated changes do
not invalidate, tampered records never compare, canonical kinds
ignore byte conventions, and freshness is transitive link by link."""

from __future__ import annotations

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import freshness                        # noqa: E402
from pcbqa.freshness import FreshnessError         # noqa: E402
from tests import synth                            # noqa: E402


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


class CanonicalKindsIgnoreByteConventions(unittest.TestCase):
    """An artifact's identity is its CONTENT: line endings, JSON
    indentation and key order are storage conventions, not
    changes - while any content change still moves the closure."""

    def test_json_path_is_eol_indent_and_order_independent(self):
        root = synth.tempdir("fresh-json")
        a = os.path.join(root, "a.json")
        b = os.path.join(root, "b.json")
        with open(a, "w", encoding="utf-8",
                  newline="\n") as handle:
            handle.write('{\n "x": 1,\n "y": [2, 3]\n}\n')
        with open(b, "w", encoding="utf-8",
                  newline="\r\n") as handle:
            handle.write('{"y":[2,3],"x":1}')
        first = freshness.closure({"art": {"json_path": a}})
        second = freshness.closure({"art": {"json_path": b}})
        self.assertEqual(first["digest"], second["digest"])
        with open(b, "w", encoding="utf-8") as handle:
            handle.write('{"y":[2,3],"x":2}')
        verdict = freshness.verify(
            first, {"art": {"json_path": b}})
        self.assertIs(verdict["fresh"], False)
        self.assertEqual(verdict["moved"], ["art"])

    def test_json_path_refuses_corrupt_artifacts(self):
        root = synth.tempdir("fresh-json-bad")
        bad = os.path.join(root, "bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write('{"x": 1,')
        with self.assertRaises(FreshnessError):
            freshness.closure({"art": {"json_path": bad}})

    def test_text_path_normalizes_line_endings_only(self):
        root = synth.tempdir("fresh-text")
        a = os.path.join(root, "a.py")
        b = os.path.join(root, "b.py")
        with open(a, "wb") as handle:
            handle.write(b"def f():\n    return 1\n")
        with open(b, "wb") as handle:
            handle.write(b"def f():\r\n    return 1\r\n")
        first = freshness.closure({"src": {"text_path": a}})
        second = freshness.closure({"src": {"text_path": b}})
        self.assertEqual(first["digest"], second["digest"])
        with open(b, "wb") as handle:
            handle.write(b"def f():\r\n    return 2\r\n")
        verdict = freshness.verify(
            first, {"src": {"text_path": b}})
        self.assertIs(verdict["fresh"], False)
        self.assertEqual(verdict["moved"], ["src"])

    def test_text_path_refuses_binary_content(self):
        root = synth.tempdir("fresh-text-bin")
        binary = os.path.join(root, "blob.bin")
        with open(binary, "wb") as handle:
            handle.write(b"\xff\xfe\x00\x01")
        with self.assertRaises(FreshnessError):
            freshness.closure({"src": {"text_path": binary}})


class FreshnessIsTransitive(unittest.TestCase):
    """evidence -> decision -> search: each artifact's closure names
    its upstream artifact by canonical content, so replacing a link
    invalidates everything downstream of it - and only that."""

    def _write(self, path, content):
        with open(path, "w", encoding="utf-8",
                  newline="\n") as handle:
            json.dump(content, handle, indent=1)
            handle.write("\n")

    def _chain(self, root):
        evidence = os.path.join(root, "evidence.json")
        decision = os.path.join(root, "decision.json")
        search = os.path.join(root, "search.json")
        return evidence, decision, search

    def _produce_decision(self, evidence, decision):
        content = {
            "kind": "decision",
            "verdict": "accept-for-comparison",
            "producer_closure": freshness.closure(
                {"evidence": {"json_path": evidence}}),
        }
        self._write(decision, content)

    def _produce_search(self, decision, search):
        content = {
            "kind": "search-decision",
            "best": "candidate",
            "producer_closure": freshness.closure(
                {"decision": {"json_path": decision}}),
        }
        self._write(search, content)

    def test_a_changed_link_invalidates_all_downstream(self):
        root = synth.tempdir("fresh-chain")
        evidence, decision, search = self._chain(root)
        self._write(evidence, {"vout": 4.9997})
        self._produce_decision(evidence, decision)
        self._produce_search(decision, search)

        def link_fresh(artifact, upstream_name, upstream_path):
            with open(artifact, encoding="utf-8") as handle:
                record = json.load(handle)
            return freshness.verify(
                record["producer_closure"],
                {upstream_name: {"json_path": upstream_path}})

        # Reformatting upstream bytes changes nothing.
        with open(evidence, "w", encoding="utf-8",
                  newline="\r\n") as handle:
            handle.write('{"vout":   4.9997}')
        self.assertIs(link_fresh(decision, "evidence",
                                 evidence)["fresh"], True)
        self.assertIs(link_fresh(search, "decision",
                                 decision)["fresh"], True)

        # A content change breaks the first link...
        self._write(evidence, {"vout": 4.9})
        self.assertIs(link_fresh(decision, "evidence",
                                 evidence)["fresh"], False)
        # ...while the untouched decision file still satisfies
        # the search's closure: each verifier answers for its
        # OWN link, so the chain must be walked from the root.
        self.assertIs(link_fresh(search, "decision",
                                 decision)["fresh"], True)

        # Regenerating the decision moves its canonical content
        # (its embedded closure changed), so the next link now
        # reports stale - transitivity, link by link.
        self._produce_decision(evidence, decision)
        self.assertIs(link_fresh(decision, "evidence",
                                 evidence)["fresh"], True)
        self.assertIs(link_fresh(search, "decision",
                                 decision)["fresh"], False)

        # Regenerating the search closes the chain again.
        self._produce_search(decision, search)
        self.assertIs(link_fresh(search, "decision",
                                 decision)["fresh"], True)

    def test_unrelated_files_never_enter_the_chain(self):
        root = synth.tempdir("fresh-chain-doc")
        evidence, decision, search = self._chain(root)
        self._write(evidence, {"vout": 4.9997})
        self._produce_decision(evidence, decision)
        self._produce_search(decision, search)
        with open(os.path.join(root, "notes.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("# unrelated documentation\n")
        with open(search, encoding="utf-8") as handle:
            record = json.load(handle)
        verdict = freshness.verify(
            record["producer_closure"],
            {"decision": {"json_path": decision}})
        self.assertIs(verdict["fresh"], True)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
