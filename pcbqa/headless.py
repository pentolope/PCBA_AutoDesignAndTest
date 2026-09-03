"""Headless discipline: no code path may raise a dialog a human must
dismiss.

An autonomous pipeline dies quietly when a MODAL dialog appears on a
screen nobody is watching: wxWidgets turns API-misuse asserts (for
example ``PCB_VIA::GetWidth()`` called without a layer argument) into
a blocking "wxWidgets Debug Alert" box whenever a GUI application
object exists and a display is reachable.

``suppress_blocking_ui()`` closes that door for the CURRENT process
WITHOUT silencing the underlying errors - and without demanding a
display:

  * When no wx application object exists, a ``wx.AppConsole`` is
    created. A console application needs no display anywhere - a GUI
    ``wx.App`` terminates the whole process outright when $DISPLAY is
    unset or unreachable, before any exception handler runs, which is
    exactly the wrong failure for a CLI - and its assert handler has
    no dialog branch at all: the failed assert's file, line and
    message are printed to stderr and execution continues along the
    same path the dialog's "continue" button always took. Verified
    identical with no DISPLAY, an unreachable DISPLAY, and a live
    one. An assert is a REPORT point, not a control-flow guard
    (``wxCHECK`` guards keep their early returns regardless), so no
    previously-unreachable code becomes reachable.
  * When a GUI application already exists - this interpreter is
    embedded in KiCad itself - its assert mode is switched to
    ``APP_ASSERT_LOG``, which makes the dialog branch unreachable in
    wx's own dispatch while keeping the report.
  * In both cases wx log output is pinned to stderr, so no logger can
    raise a message box of its own.
  * Only when neither is possible does the fallback drop to
    ``wx.DisableAsserts()`` - nonblocking but unreported, and
    recorded as exactly that.

Every long-running entry point calls this first; ``run.py`` does it
for every toolkit command. The function is idempotent, never raises,
and returns a record of what it applied so a run log can show the
protection was in place. None of this is permission to misuse APIs:
the canary in the test suite asserts the protection state BEFORE
triggering the known blocking call, then demands BOTH that it does
not block AND that the assert text was reported.
"""

from __future__ import annotations

#: The wx application created for assert routing must outlive every
#: later assert; a garbage-collected one would drop the protection.
_WX_APP = None

#: protection_state()["strategy"] values that make the modal path
#: unreachable.
CONSOLE_APP = "console-app: no dialog branch exists"
GUI_LOG_MODE = "gui-app in log mode: the dialog branch is unreachable"


def suppress_blocking_ui():
    """Disarm modal dialogs for this process.

    Safe everywhere: without wx available the step is recorded as
    skipped rather than failing, and no display is ever required.
    """
    global _WX_APP
    applied = {"strategy": None, "detail": []}
    try:
        import wx
    except Exception as error:  # wx absent from this interpreter
        applied["strategy"] = "wx-absent"
        applied["detail"].append(
            "wx assert handling unavailable: {}".format(error))
        return applied
    try:
        app = wx.GetApp()
        if app is None:
            _WX_APP = wx.AppConsole()
            app = _WX_APP
        if hasattr(app, "SetAssertMode"):
            # A GUI application from an embedding process: its assert
            # dispatch has a dialog branch, so route it to the log.
            app.SetAssertMode(wx.APP_ASSERT_LOG)
        wx.Log.SetActiveTarget(wx.LogStderr())
        state = protection_state()
        applied["strategy"] = state["strategy"]
        if not state["modal_unreachable"]:
            applied["detail"].append(
                "the modal path could not be proven unreachable")
    except Exception as error:
        try:
            wx.DisableAsserts()
            applied["strategy"] = (
                "suppressed (fallback): nonblocking but UNREPORTED - "
                "{}".format(error))
        except Exception as worse:
            applied["strategy"] = "unprotected"
            applied["detail"].append(str(worse))
    return applied


def protection_state():
    """The protection as QUERYABLE state - never inferred from time.

    wx exposes its live configuration, so a consumer (and the
    suite's canary) asserts directly whether a modal path is
    reachable before triggering anything:

      * a console application has no dialog branch to reach;
      * a GUI application is safe exactly when its assert mode is
        ``APP_ASSERT_LOG``.

    A test that checks this state BEFORE triggering fails in
    milliseconds by name when protection regresses - and never causes
    the very dialog it guards against.
    """
    state = {"modal_unreachable": None, "strategy": None,
             "wx_assert_mode_value": None}
    try:
        import wx
        app = wx.GetApp()
        if app is None:
            state["modal_unreachable"] = False
            state["strategy"] = "no wx application: nothing routes asserts"
        elif hasattr(app, "GetAssertMode"):
            state["wx_assert_mode_value"] = int(app.GetAssertMode())
            state["modal_unreachable"] = (
                app.GetAssertMode() == wx.APP_ASSERT_LOG)
            state["strategy"] = (
                GUI_LOG_MODE if state["modal_unreachable"]
                else "gui-app NOT in log mode: the dialog branch is live")
        else:
            state["modal_unreachable"] = True
            state["strategy"] = CONSOLE_APP
    except Exception:
        pass
    return state
