"""Generic board-level electrical simulation foundation.

Architecture, and the honesty rules that bind it:

  * ``fidelity`` - the model registry. Every device or interconnect
    model carries explicit provenance and a fidelity class from one
    shared vocabulary, so a simulation result can state exactly what
    class of evidence produced it. There is no default model and no
    silent substitution: an unregistered reference refuses.
  * ``scenario`` - the declaration of one simulation: circuit
    elements, stimulus, operating conditions, analyses, measurements
    with machine-readable assertions, model substitutions and the
    minimum fidelity the caller requires. Unknown keys refuse, in the
    toolkit's usual strict-input style.
  * ``ngspice`` - the primary automated SPICE backend: deterministic
    deck generation, offline batch execution, captured simulator
    identity, deterministic parsing, and a result contract that keeps
    FOUR verdicts separate - simulator convergence, numerical
    assertion pass/fail, model coverage, and release significance. A
    simulation PASS never implies stronger model coverage than the
    registry actually provided, and ``release_grade`` is false at
    this layer unconditionally.
  * ``digital`` - the board-level digital contract foundation
    (behavioral SystemVerilog models and the Verilator harness); see
    its docstring for the repository-ownership rules.

Backends are OPTIONAL. Production validation never requires them: a
missing backend is reported as exactly that (``backend-unavailable``),
which is neither a pass nor a fabricated failure, and policy decides
what an absent backend means. A present backend that fails to run is a
failure. A missing model is a refusal BEFORE any simulator runs.

Nothing in this package knows any particular board; board specifics
belong to the board repository's manifests and scenarios.
"""

from . import fidelity, scenario, ngspice, digital  # noqa: F401
