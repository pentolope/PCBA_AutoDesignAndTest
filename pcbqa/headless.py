"""Headless discipline: no code path may raise a dialog a human must
dismiss.

An autonomous pipeline dies quietly when a MODAL dialog appears on a
screen nobody is watching: wxWidgets turns API-misuse asserts (for
example ``PCB_VIA::GetWidth()`` called without a layer argument) into
a blocking "wxWidgets Debug Alert" box, and on a GTK build that box
appears whenever a display is reachable. That stalls a run until a
person clicks.

``suppress_blocking_ui()`` closes that door for the CURRENT process
WITHOUT silencing the underlying errors:

  * wx asserts are switched to LOG mode (``wx.App`` +
    ``APP_ASSERT_LOG``): the failed assert's file, line and message
    are printed to stderr - which every recorded invocation in this
    ecosystem captures - and execution continues along the same path
    the dialog's "continue" button always took. An assert is a
    REPORT point, not a control-flow guard (``wxCHECK`` guards keep
    their early returns regardless), so no previously-unreachable
    code becomes reachable; what changes is that misuse is reported
    in the log instead of freezing the run. All wx log output is
    pinned to stderr so the App's default GUI logger can never
    raise a message box of its own. Only when no ``wx.App`` can be
    created does the fallback drop to ``wx.DisableAsserts()`` -
    nonblocking but unreported, and recorded as exactly that.
Every long-running entry point calls this first; ``run.py`` does it
for every toolkit command. The function is idempotent, never raises,
and returns a record of what it applied so a run log can show the
protection was in place. None of this is permission to misuse APIs:
the canary in the test suite triggers the known blocking call and
demands BOTH that it does not block AND that the assert text was
reported.
"""

from __future__ import annotations

#: The wx.App created for assert routing must outlive every later
#: assert; a garbage-collected App would drop the installed mode.
_WX_APP = None


def suppress_blocking_ui():
    """Disarm modal dialogs for this process.

    Safe everywhere: without wx available the step is recorded as
    skipped rather than failing.
    """
    global _WX_APP
    applied = {"wx_assert_mode": None, "detail": []}
    try:
        import wx
        try:
            app = wx.GetApp()
            if app is None:
                _WX_APP = wx.App(redirect=False)
                app = _WX_APP
            app.SetAssertMode(wx.APP_ASSERT_LOG)
            wx.Log.SetActiveTarget(wx.LogStderr())
            applied["wx_assert_mode"] = (
                "log: asserts print file/line/message to stderr "
                "and continue; nothing blocks, nothing is silent")
        except Exception as error:
            wx.DisableAsserts()
            applied["wx_assert_mode"] = (
                "suppressed (fallback): nonblocking but "
                "UNREPORTED - log mode unavailable: "
                "{}".format(error))
    except Exception as error:  # wx absent from this interpreter
        applied["detail"].append(
            "wx assert handling unavailable: {}".format(error))
    return applied


def protection_state():
    """The protection as QUERYABLE state - never inferred from time.

    wx exposes its live configuration, so a consumer (and the
    suite's canary) asserts directly whether a modal path is
    reachable: the assert mode must be APP_ASSERT_LOG, which makes
    the dialog branch unreachable in wx's own dispatch. A test that
    checks this state BEFORE triggering anything fails fast and by
    name when protection regresses - and never causes the very
    dialog it guards against.
    """
    state = {"wx_assert_mode_is_log": None,
             "wx_assert_mode_value": None}
    try:
        import wx
        app = wx.GetApp()
        if app is not None and hasattr(app, "GetAssertMode"):
            state["wx_assert_mode_value"] = int(
                app.GetAssertMode())
            state["wx_assert_mode_is_log"] = (
                app.GetAssertMode() == wx.APP_ASSERT_LOG)
    except Exception:
        pass
    return state
