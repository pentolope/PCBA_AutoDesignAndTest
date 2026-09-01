"""Generic board-level electrical simulation foundation.

Architecture, and the honesty rules that bind it:

  * ``model_registry`` - fail-closed model lookup and per-phenomenon
    evidence-class requirements. Model evidence uses ``pcbqa.claim``;
    there is no cross-phenomenon quality ranking and no silent model
    substitution.
  * ``scenario`` - the declaration of one simulation: circuit
    elements, stimulus, operating conditions, analyses, measurements
    with machine-readable assertions, model substitutions and the
    evidence classes the caller accepts. Unknown keys refuse, in the
    toolkit's usual strict-input style.
  * ``ngspice`` - the primary automated SPICE backend: deterministic
    deck generation, offline batch execution, captured simulator
    identity, deterministic parsing, and shared claims and verdicts for
    numerical assertions. Simulator convergence and evidence coverage
    remain separate applicability facts; neither can silently become a
    numerical pass.
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

from . import model_registry, scenario, ngspice, digital  # noqa: F401
