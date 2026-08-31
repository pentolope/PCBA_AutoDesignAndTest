"""Validation documents expose the implementation that judged them:
a verdict binds to a toolkit commit, not just to a board."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import core                              # noqa: E402


class TheImplementationIdentityIsStamped(unittest.TestCase):

    def test_the_identity_names_the_executing_commit(self):
        record = core.toolkit_identity()
        self.assertEqual(record["toolkit_root"], HERE)
        head = subprocess.run(
            ["git", "-C", HERE, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
        if head.returncode != 0:
            self.assertIsNone(record["commit"])
            self.assertIn("unrecorded", record["detail"])
            return
        self.assertEqual(record["commit"], head.stdout.strip())
        self.assertIn(record["working_tree_dirty"], (True, False))

    def test_the_tooling_block_carries_the_identity(self):
        """to_json's tooling block is where every consumer already
        looks for environment truth; the implementation identity
        lives beside it, not in a side channel."""
        import types
        context = types.SimpleNamespace(
            kicad_cli="unavailable-for-this-test",
            tool_versions={})
        block = core._tooling(context)
        identity = block["validation_implementation"]
        self.assertEqual(identity["toolkit_root"], HERE)
        self.assertEqual(identity, core.toolkit_identity())


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
