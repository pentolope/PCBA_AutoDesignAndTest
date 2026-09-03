"""No code path may raise a dialog a human must dismiss - proven by
STATE, never inferred from time.

The protection is queryable: the wx assert mode is read back
directly, so a regression fails in milliseconds with the offending
state named. Only after the state PROVES the modal path unreachable
does the canary trigger the known misuse (a via width read without a
layer argument) - to verify the report channel: the assert's file/line/message must reach stderr,
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
if state["modal_unreachable"] is not True:
    # Refuse to trigger anything: with the modal path not provably
    # unreachable, the trigger itself could raise the very box this
    # test exists to prevent. The state IS the failure.
    print("STATE-BAD:", state["strategy"])
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
        applied = headless.suppress_blocking_ui()
        state = headless.protection_state()
        self.assertIs(state["modal_unreachable"], True, state)
        self.assertIn(applied["strategy"],
                      (headless.CONSOLE_APP, headless.GUI_LOG_MODE))

    def test_commands_run_with_no_display_at_all(self):
        """A GUI wx.App exits the process when $DISPLAY is unset or
        unreachable - before any exception handler runs - which made
        every CLI command die in a genuinely headless environment.
        The console strategy must hold with no display, a dead one,
        and - when the environment offers one - a live one alike."""
        displays = [None, ":63.7"]
        if os.environ.get("DISPLAY"):
            displays.append(os.environ["DISPLAY"])
        for display in displays:
            environment = dict(os.environ)
            environment.pop("DISPLAY", None)
            if display is not None:
                environment["DISPLAY"] = display
            completed = subprocess.run(
                [sys.executable, os.path.join(HERE, "run.py"), "gates"],
                capture_output=True, text=True, timeout=120,
                env=environment, cwd=HERE)
            self.assertEqual(completed.returncode, 0,
                             (display, completed.stderr[-400:]))
            self.assertIn("ROUTE.GEOMETRY_HYGIENE", completed.stdout)

    def test_the_gates_library_arms_the_protection_itself(self):
        """`gates.load()`/`evaluate()` are entry points for search loops
        that never touch run.py - the panel demonstrated gates.load()
        freezing on a modal assert inside a dialog-mode wx.App. The
        library must arm the protection without being asked."""
        probe = ("import sys; sys.path.insert(0, {toolkit!r}); "
                 "from pcbqa import gates, headless; gates.load(); "
                 "state = headless.protection_state(); "
                 "sys.exit(0 if state['modal_unreachable'] else 3)"
                 ).format(toolkit=HERE)
        environment = dict(os.environ)
        environment.pop("DISPLAY", None)
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=120, env=environment)
        self.assertEqual(completed.returncode, 0,
                         completed.stderr[-400:])

    def test_wx_log_output_is_pinned_to_stderr(self):
        """A logger that can raise a message box is the same freeze by
        another door; the pinning line had no coverage at all."""
        headless.suppress_blocking_ui()
        import wx
        self.assertIsInstance(wx.Log.GetActiveTarget(), wx.LogStderr)

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
