"""Propagation backends, resolved by name and never imported speculatively.

The analytic backend is `pcbqa.propagation`: closed-form transmission-line
arithmetic, pure Python, no dependency beyond what the validator already needs.
It is always available.

Any other backend is an electromagnetic solver, and a solver has a completely
different dependency profile from the rest of this toolkit. The toolkit runs
inside KiCad's own Python, whose packages are supplied by KiCad and a KiCad
add-on; an EM solver is a separately built native program with its own Python
bindings, its own meshing libraries and its own install story. Making one a
condition of importing a gate module would mean an ordinary DRC run on an
ordinary board failed because a field solver was not installed.

So nothing here is imported at module load, `preflight` does not probe for a
solver, and a solver is reached through a subprocess rather than through an
import. What this module owns is the decision, and one rule:

  * a board that asks for a backend and does not get it is an ERROR;
  * a board that asks for the analytic backend gets it;
  * a board that declares `"required": false` gets its fallback, and every
    result records which backend actually ran and that it was a fallback.

Nothing silently downgrades: the *only* route to a different backend than the
one requested is the board having said so in writing, and `Selection` carries
that fact into the report rather than leaving the two cases looking alike.
"""

from __future__ import annotations

ANALYTIC = "analytic"

#: Backends this release knows how to look for. A name not listed here is
#: refused rather than attempted, so a typo in a manifest is a blocked run and
#: not a quiet fall-through to the cheap model.
KNOWN = (ANALYTIC, "openems")


class BackendError(Exception):
    """A requested backend cannot be used. Always blocks."""


class BackendUnavailable(BackendError):
    """The backend is implemented here but not installed on this machine."""


def available(name, spec=None):
    """Is this backend usable right now? (bool, detail). Never raises."""
    if name == ANALYTIC:
        return True, "closed-form analytic model, always available"
    if name == "openems":
        from . import openems
        return openems.available(spec or {})
    return False, "no backend named {!r} is implemented; this release has " \
                  "{}".format(name, ", ".join(KNOWN))


class Selection:
    """Which backend a board asked for, which one ran, and why.

    A separate object rather than a bare name because "the analytic model ran"
    and "the analytic model ran because the solver this board would have
    preferred is not installed" are different facts, and a report that cannot
    tell them apart is a report that hides a downgrade.
    """

    __slots__ = ("requested", "used", "detail", "fell_back")

    def __init__(self, requested, used, detail, fell_back=False):
        self.requested = requested
        self.used = used
        self.detail = detail
        self.fell_back = fell_back

    def to_dict(self):
        return {"backend_requested": self.requested,
                "backend_used": self.used,
                "backend_fell_back": self.fell_back,
                "backend_detail": self.detail}

    def __repr__(self):
        return "<Selection {}->{}{}>".format(
            self.requested, self.used, " (fallback)" if self.fell_back else "")


def select(name, spec=None):
    """Choose the backend to run, honouring what the board actually asked for.

    Three cases, and the difference between them is the board's own
    declaration rather than what happens to be installed:

      * the requested backend is available - it is used;
      * it is unavailable and the board requires it - this raises, because a
        board that asked for a field solver did not ask for first-order
        arithmetic wearing its name;
      * it is unavailable and the board declared `"required": false` - the
        fallback runs, and the result records that it did.

    A board that declares nothing gets `required: true`. Silence is not
    permission to substitute something cheaper.
    """
    spec = spec or {}
    if name not in KNOWN:
        raise BackendError(
            "propagation backend {!r} is not implemented; this release has "
            "{}".format(name, ", ".join(KNOWN)))
    ok, detail = available(name, spec)
    if ok:
        return Selection(name, name, detail)

    if spec.get("required", True):
        raise BackendUnavailable(
            "this board selects the {!r} propagation backend and it is not "
            "usable here: {}. Refusing to substitute another, because a board "
            "that asked for a field solver did not ask for first-order "
            "arithmetic wearing its name. Declare "
            "`\"required\": false` with a fallback if an estimate is "
            "acceptable".format(name, detail))

    fallback = spec.get("fallback", ANALYTIC)
    if fallback not in KNOWN:
        raise BackendError(
            "the fallback backend {!r} is not implemented; this release has "
            "{}".format(fallback, ", ".join(KNOWN)))
    if fallback == name:
        raise BackendError(
            "backend {!r} names itself as its own fallback, which cannot "
            "resolve".format(name))
    ok, fallback_detail = available(fallback, spec)
    if not ok:
        raise BackendUnavailable(
            "this board permits falling back from {!r} to {!r}, and neither is "
            "usable here: {}; {}".format(name, fallback, detail,
                                         fallback_detail))
    return Selection(
        name, fallback,
        "{} is unavailable ({}); this board permits a fallback, so {} ran "
        "instead".format(name, detail, fallback),
        fell_back=True)


def require(name, spec=None):
    """`select`, for a caller that will not accept a fallback.

    Kept separate so the strict intent is visible at the call site rather than
    buried in a spec key.
    """
    selection = select(name, dict(spec or {}, required=True))
    return selection.used


def describe():
    """What each known backend is and whether it is here. For preflight/report."""
    rows = []
    for name in KNOWN:
        ok, detail = available(name)
        rows.append({"backend": name, "available": bool(ok), "detail": detail})
    return rows
