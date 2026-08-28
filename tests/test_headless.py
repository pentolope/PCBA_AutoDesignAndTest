"""No code path may raise a dialog a human must dismiss - proven by
STATE, never inferred from time.

The protection is queryable: the wx assert mode and the Windows
error mode are read back directly, so a regression fails in
milliseconds with the offending state named. Only after the state
PROVES the modal path unreachable does the canary trigger the known
misuse (a via width read without a layer argument) - to verify the
report channel: the assert's file/line/message must reach stderr,
because nonblocking was never meant to mean silent. No dialog can
appear even when this test fails; the subprocess timeout below is
ordinary containment for a child process, not the detector.
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
state = headless.protection_state()
print("STATE", state)
if state["wx_assert_mode_is_log"] is not True:
    # Refuse to trigger anything: with an unknown or dialog assert
    # mode, the trigger itself could raise the modal box this test
    # exists to prevent. The state IS the failure.
    print("STATE-BAD: wx assert mode is not log")
    sys.exit(3)
if not state["windows_error_mode_ok"]:
    print("STATE-BAD: Windows error mode bits missing")
    sys.exit(3)
import pcbnew
board = pcbnew.CreateEmptyBoard()
via = pcbnew.PCB_VIA(board)
via.SetWidth(pcbnew.FromMM(0.45))
via.SetDrill(pcbnew.FromMM(0.3))
board.Add(via)
value = via.GetWidth()  # the known misuse; state proved non-modal
print("CANARY-OK", value)
"""


class NothingBlocksOnADialog(unittest.TestCase):

    def test_protection_is_queryable_state(self):
        headless.suppress_blocking_ui()
        state = headless.protection_state()
        self.assertIs(state["wx_assert_mode_is_log"], True)
        self.assertIs(state["windows_error_mode_ok"], True)

    def test_state_first_canary_demands_the_report(self):
        """State is asserted BEFORE the trigger, so a protection
        regression fails fast and by name without any dialog ever
        appearing; the trigger then proves the assert is REPORTED
        on stderr, not silenced."""
        completed = subprocess.run(
            [sys.executable, "-c", _CANARY.format(toolkit=HERE)],
            capture_output=True, text=True,
            timeout=60)  # containment for a child, not detection
        self.assertNotIn("STATE-BAD", completed.stdout,
                         completed.stdout[-400:])
        self.assertEqual(completed.returncode, 0,
                         completed.stderr[-500:])
        self.assertIn("CANARY-OK", completed.stdout)
        self.assertIn("GetWidth", completed.stderr,
                      "the assert fired but its report never "
                      "reached stderr; nonblocking must not mean "
                      "silent")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
