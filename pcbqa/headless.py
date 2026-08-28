"""Headless discipline: no code path may raise a dialog a human must
dismiss.

An autonomous pipeline dies quietly when a MODAL dialog appears on a
screen nobody is watching: KiCad's Windows builds turn wxWidgets
API-misuse asserts (for example ``PCB_VIA::GetWidth()`` called
without a layer argument) into a blocking "wxWidgets Debug Alert"
box, and Windows itself can raise crash/error boxes for a dying
child process. Either one stalls a run until a person clicks.

``suppress_blocking_ui()`` closes both doors for the CURRENT process
and, via inherited error mode, for child processes it spawns:

  * ``wx.DisableAsserts()`` - KiCad's Python ships wxPython bound to
    the SAME wx runtime pcbnew uses, so disabling asserts here
    silences the C++ dialog path process-wide (the misused call then
    simply returns, as verified against the known via/width case);
  * ``SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX |
    SEM_NOOPENFILEERRORBOX)`` - Windows hard-failure boxes become
    silent failures, and children created afterwards inherit it.

Every long-running entry point calls this first; ``run.py`` does it
for every toolkit command. The function is idempotent, never raises,
and returns a record of what it applied so a run log can show the
protection was in place. Suppression is NOT permission to misuse
APIs: the assert canary in the test suite exists precisely so a
regression here fails the suite instead of freezing a pipeline.
"""

from __future__ import annotations

_SEM_FAILCRITICALERRORS = 0x0001
_SEM_NOGPFAULTERRORBOX = 0x0002
_SEM_NOOPENFILEERRORBOX = 0x8000


def suppress_blocking_ui():
    """Disarm modal dialogs for this process and its children.

    Safe everywhere: on non-Windows or without wx available, the
    corresponding step is recorded as skipped rather than failing.
    """
    applied = {"wx_asserts_disabled": False,
               "windows_error_mode_set": False,
               "detail": []}
    try:
        import wx
        wx.DisableAsserts()
        applied["wx_asserts_disabled"] = True
    except Exception as error:  # wx absent outside KiCad's python
        applied["detail"].append(
            "wx assert suppression unavailable: {}".format(error))
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        previous = kernel32.SetErrorMode(
            _SEM_FAILCRITICALERRORS | _SEM_NOGPFAULTERRORBOX
            | _SEM_NOOPENFILEERRORBOX)
        kernel32.SetErrorMode(
            previous | _SEM_FAILCRITICALERRORS
            | _SEM_NOGPFAULTERRORBOX | _SEM_NOOPENFILEERRORBOX)
        applied["windows_error_mode_set"] = True
    except Exception as error:  # not Windows, or no ctypes access
        applied["detail"].append(
            "Windows error-mode suppression unavailable: "
            "{}".format(error))
    return applied
