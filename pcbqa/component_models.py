"""What a component contributes to an electrical path - and what it does not.

A path that crosses a part crosses something with its own transit delay. That
delay is usually small next to the copper either side of it, and it is never
zero, and this toolkit has no way to know it from the board file. So the
question a component traversal has to answer is not "how much" but "on whose
authority", and there are exactly three honest answers:

``unmodelled``
    The board declared no model. Nothing is attributed. The path's total is
    then a **lower bound** rather than a value: some positive, unknown amount
    is missing from it. A lower bound can prove a maximum is exceeded; it can
    never prove one is met, and the gates treat it that way.

``implemented``
    The board declared a model this release evaluates, and it was evaluated.
    Two exist. ``none`` attributes zero *deliberately*, on a stated
    justification - which is a different claim from having no model at all,
    and the difference is exactly who is answerable for it. ``fixed_delay``
    attributes a stated number with stated provenance.

``unsupported``
    The board declared a model this release cannot evaluate. This is the case
    the whole module exists for: a declaration must never make an
    unimplemented model look like a valid zero. Nothing is attributed, the
    delay is not derivable at all, and the gate says so.

The names under `RESERVED_MODELS` are deliberately recognised-but-refused. A
board that writes `ibis` today gets told this release does not implement it,
rather than being told the model name is a typo - and the spelling is fixed
now, so declaring one later is not a schema change.

Nothing here invents electrical behaviour for a generic two-pin part, and
nothing here knows what kind of part it is looking at.
"""

from __future__ import annotations

from .propagation import (DECLARED_MODEL, GEOMETRY_ONLY,       # noqa: F401
                          UNKNOWN_CONTRIBUTION)

# How a traversal's contribution was arrived at.
UNMODELLED = "unmodelled"
IMPLEMENTED = "implemented"
UNSUPPORTED = "unsupported"

# Models this release evaluates.
MODEL_NONE = "none"
MODEL_FIXED = "fixed_delay"
IMPLEMENTED_MODELS = (MODEL_NONE, MODEL_FIXED)

#: Recognised but not implemented. Listed so a board asking for one is refused
#: by name rather than by "unknown model", and so the spelling is settled
#: before anything depends on it.
RESERVED_MODELS = ("package_delay", "ibis", "touchstone", "sparameter",
                   "cable", "transmission_line", "device")


class ComponentModelError(Exception):
    """A component model declaration cannot be used. Always blocks."""


class Contribution:
    """One component traversal's contribution to a path, and its standing."""

    __slots__ = ("kind", "model", "delay_ps", "fidelity", "reason",
                 "parameters")

    def __init__(self, kind, model, delay_ps, fidelity, reason,
                 parameters=None):
        self.kind = kind
        self.model = model
        self.delay_ps = delay_ps
        self.fidelity = fidelity
        self.reason = reason
        self.parameters = dict(parameters or {})

    @property
    def evaluable(self):
        """Can a total containing this traversal be computed at all?"""
        return self.kind != UNSUPPORTED

    @property
    def exact(self):
        """Is the contribution a value rather than an acknowledged omission?

        False for an unmodelled traversal, which is what makes a path total
        containing one a lower bound.
        """
        return self.kind == IMPLEMENTED

    def to_dict(self):
        return {"model": self.model, "model_status": self.kind,
                "delay_ps": self.delay_ps, "fidelity": self.fidelity,
                "reason": self.reason,
                **({"parameters": self.parameters} if self.parameters else {})}


def evaluate(declaration, reference=None):
    """Turn a step's `delay_model` declaration into a `Contribution`.

    `declaration` is whatever the manifest put there: absent, or an object
    naming a model. A bare string is accepted for the two implemented models
    only when they need no parameters - which is neither of them, because both
    require a justification or a provenance - so in practice a model is always
    an object. That is deliberate: a model with nobody's name on it is a
    number from nowhere.
    """
    where = "component traversal" if reference is None else reference

    if declaration is None:
        return Contribution(
            UNMODELLED, None, 0.0, UNKNOWN_CONTRIBUTION,
            "no delay model is declared for {}, so nothing is attributed to "
            "it and any total containing this path is a lower bound".format(
                where))

    if isinstance(declaration, str):
        raise ComponentModelError(
            "{}: delay_model is the bare string {!r}. Every implemented model "
            "carries something a person has to sign for - a justification or a "
            "provenance - so a model is declared as an object, for example "
            "{{\"model\": \"none\", \"justification\": \"...\"}}".format(
                where, declaration))

    if not isinstance(declaration, dict):
        raise ComponentModelError(
            "{}: delay_model is a {}, not an object".format(
                where, type(declaration).__name__))

    model = declaration.get("model")
    if not model:
        raise ComponentModelError(
            "{}: delay_model declares no `model`".format(where))

    if model in RESERVED_MODELS:
        return Contribution(
            UNSUPPORTED, model, None, GEOMETRY_ONLY,
            "{} declares the {!r} delay model, which this release recognises "
            "but does not implement. No delay is attributed and none is "
            "guessed: a declaration must not make an unimplemented model look "
            "like a valid zero".format(where, model))

    if model not in IMPLEMENTED_MODELS:
        raise ComponentModelError(
            "{}: delay_model {!r} is not a model this validator knows. "
            "Implemented: {}. Recognised but not implemented: {}".format(
                where, model, ", ".join(IMPLEMENTED_MODELS),
                ", ".join(RESERVED_MODELS)))

    if model == MODEL_NONE:
        justification = declaration.get("justification")
        if not justification:
            raise ComponentModelError(
                "{}: the {!r} model attributes zero delay deliberately, so it "
                "requires a `justification`. Without one it is "
                "indistinguishable from having declared no model at all, and "
                "the two mean different things".format(where, model))
        return Contribution(
            IMPLEMENTED, model, 0.0, DECLARED_MODEL,
            "zero attributed on a declared justification: {}".format(
                justification),
            {"justification": justification})

    # MODEL_FIXED
    delay = declaration.get("delay_ps")
    provenance = declaration.get("provenance")
    if delay is None:
        raise ComponentModelError(
            "{}: the {!r} model declares no `delay_ps`".format(where, model))
    if isinstance(delay, bool) or not isinstance(delay, (int, float)):
        raise ComponentModelError(
            "{}: delay_ps is {!r}, not a number".format(where, delay))
    if delay != delay or delay in (float("inf"), float("-inf")):
        raise ComponentModelError(
            "{}: delay_ps is {!r}, which is not a finite number".format(
                where, delay))
    if delay < 0:
        raise ComponentModelError(
            "{}: delay_ps is {}, and a passive traversal cannot advance a "
            "signal in time".format(where, delay))
    if not provenance:
        raise ComponentModelError(
            "{}: the {!r} model states no `provenance`; a delay nobody can "
            "trace to a datasheet or a measurement is a guess with a units "
            "label".format(where, model))
    return Contribution(
        IMPLEMENTED, model, float(delay), DECLARED_MODEL,
        "declared fixed delay, {}".format(provenance),
        {"delay_ps": float(delay), "provenance": provenance})


def validate(declaration, reference=None):
    """Check a declaration at parse time, so a bad one fails before any board.

    Returns nothing; raises `ComponentModelError`. A model that is recognised
    but unimplemented is *not* an error here - it is a legitimate declaration
    whose refusal belongs to evaluation, where it can be reported per path.
    """
    evaluate(declaration, reference)
