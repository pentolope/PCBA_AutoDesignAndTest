"""No code path may raise a dialog a human must dismiss.

The canary triggers the KNOWN blocking wx assert (a via width read
without a layer argument) in a subprocess after arming the
suppression. If suppression ever regresses, the subprocess hangs on
a modal dialog and the timeout FAILS this suite - a red test instead
of a frozen pipeline on an unwatched screen. The subprocess is
killed on timeout, which also closes any dialog it opened.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import headless                          # noqa: E402

_CANARY = r"""
import sys
sys.path.insert(0, {toolkit!r})
from pcbqa import headless
applied = headless.suppress_blocking_ui()
import pcbnew
board = pcbnew.CreateEmptyBoard()
via = pcbnew.PCB_VIA(board)
via.SetWidth(pcbnew.FromMM(0.45))
via.SetDrill(pcbnew.FromMM(0.3))
board.Add(via)
value = via.GetWidth()  # the known dialog-raising misuse
print("CANARY-OK", value, applied["wx_asserts_disabled"])
"""


class NothingBlocksOnADialog(unittest.TestCase):

    def test_suppression_reports_what_it_applied(self):
        applied = headless.suppress_blocking_ui()
        self.assertIn("wx_asserts_disabled", applied)
        self.assertIn("windows_error_mode_set", applied)
        # Idempotent: a second call is harmless.
        headless.suppress_blocking_ui()

    def test_the_known_assert_cannot_raise_a_dialog(self):
        """The regression canary: under suppression, the known
        misuse completes immediately in a child process. A hang
        here means an autonomous run somewhere would freeze on a
        popup."""
        try:
            completed = subprocess.run(
                [sys.executable, "-c",
                 _CANARY.format(toolkit=HERE)],
                capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail(
                "the canary subprocess hung: a blocking dialog is "
                "reachable again; no pipeline is autonomous until "
                "this is fixed")
        self.assertEqual(completed.returncode, 0,
                         completed.stderr[-500:])
        self.assertIn("CANARY-OK", completed.stdout)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
