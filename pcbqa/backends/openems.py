"""openEMS backend: the interface, and the reason it is not the default.

openEMS is a full-wave FDTD solver. Used properly it can answer questions the
analytic backend cannot - real discontinuities, real return paths, real
broadband loss - and produce S-parameters that a higher-fidelity timing layer
could consume. It is a legitimate destination for this architecture.

It is not, and must not become, a condition of running this toolkit.

    * The validator runs in KiCad's own Python. openEMS is a separately built
      native program with its own Python bindings; it is not in that
      interpreter and installing it there is not this toolkit's business.
    * A field solve is minutes to hours. Ordinary validation is seconds.
    * A board that never asked for EM extraction must never fail, slow down or
      warn because a solver is absent.

So this module is never imported at toolkit start-up, `pcbqa.preflight` does
not probe for it, and nothing here imports openEMS at module scope. The solver
is reached as an *executable*, through a subprocess, which keeps its dependency
tree entirely outside this process.

What exists today
-----------------
The availability probe and the shape of the interface. `extract` raises
`NotImplementedError`: geometry export, port definition, meshing, excitation,
de-embedding and S-parameter reduction are each substantial, and a stub that
returned a plausible number would be far worse than one that refuses.

What a working backend still needs, in the order it would be built:

    1. export the selected path's copper, its reference plane(s) and the
       physical stackup as a solver geometry;
    2. define ports at the path's endpoints and a de-embedding reference;
    3. mesh, choosing cell size from the highest frequency of interest;
    4. run the solver as a subprocess with a bounded time budget;
    5. reduce the S-parameters to a group delay over the band of interest;
    6. return that with the same record shape `pcbqa.propagation` produces, so
       the gates and a board's manifest do not change at all.

Step 6 is why this file exists now rather than later: the result shape is the
contract, and it is already fixed by the analytic backend.
"""

from __future__ import annotations

import os
import shutil

#: Environment variable naming the solver executable, for an installation that
#: is not on PATH. Read only when a board actually selects this backend.
ENV_EXECUTABLE = "PCBQA_OPENEMS"

#: What the probe looks for on PATH.
CANDIDATES = ("openEMS", "openems")


def executable(spec=None):
    """The solver binary this board would use, or None. Never raises."""
    spec = spec or {}
    declared = spec.get("executable") or os.environ.get(ENV_EXECUTABLE)
    if declared:
        return declared if os.path.isfile(declared) else None
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def available(spec=None):
    """(bool, detail). Probes the filesystem only; imports nothing."""
    found = executable(spec)
    if found:
        return True, "openEMS executable at {}".format(found)
    return False, (
        "no openEMS executable found on PATH or at {} / spec.executable. This "
        "is not a defect: openEMS is an optional high-fidelity backend and no "
        "part of ordinary validation needs it".format(ENV_EXECUTABLE))


def extract(resolved_path, stackup, spec=None):     # pragma: no cover - stub
    """Full-wave extraction for one path. Not implemented in this release."""
    raise NotImplementedError(
        "the openEMS backend is an interface, not an implementation: geometry "
        "export, port definition, meshing, excitation and S-parameter "
        "reduction are not written. A board that selects this backend is "
        "blocked here rather than given an analytic estimate labelled as a "
        "full-wave result")
