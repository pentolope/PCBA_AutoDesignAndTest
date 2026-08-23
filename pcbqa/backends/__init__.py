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
  * a board that says a solver is optional gets the analytic backend, and the
    result records which one it actually was.

Nothing silently downgrades. `evaluate` on an unavailable backend raises.
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


def require(name, spec=None):
    """The backend, or an exception saying why not.

    Called by a gate only when a board actually selected a non-analytic
    backend, which is what keeps a solver out of the import path of an
    ordinary run.
    """
    spec = spec or {}
    if name not in KNOWN:
        raise BackendError(
            "propagation backend {!r} is not implemented; this release has "
            "{}".format(name, ", ".join(KNOWN)))
    ok, detail = available(name, spec)
    if ok:
        return name
    if spec.get("required", True):
        raise BackendUnavailable(
            "this board selects the {!r} propagation backend and it is not "
            "usable here: {}. Refusing to substitute the analytic model, "
            "because a board that asked for a field solver did not ask for "
            "first-order arithmetic wearing its name".format(name, detail))
    raise BackendUnavailable(detail)


def describe():
    """What each known backend is and whether it is here. For preflight/report."""
    rows = []
    for name in KNOWN:
        ok, detail = available(name)
        rows.append({"backend": name, "available": bool(ok), "detail": detail})
    return rows
