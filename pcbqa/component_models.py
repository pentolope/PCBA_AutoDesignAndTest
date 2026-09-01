"""Component-traversal delay claims.

An absent or deliberately omitted model establishes only a non-negative lower
bound. A sourced maximum establishes an interval. A sourced fixed model is
exact for the declared model. An unimplemented model is explicitly unsupported
and knows no number.
"""

from __future__ import annotations

from . import claim

MODEL_NONE = "none"
MODEL_FIXED = "fixed_delay"
IMPLEMENTED_MODELS = (MODEL_NONE, MODEL_FIXED)
UNMODELLED_EVIDENCE = "unmodelled-contribution"
DECLARED_MODEL_EVIDENCE = "declared-model"


class ComponentModelError(Exception):
    """A component model declaration cannot be used. Always blocks."""


def _finite_non_negative(value, field, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ComponentModelError(
            "{}: {} is {!r}, not a number".format(where, field, value))
    if value != value or value in (float("inf"), float("-inf")):
        raise ComponentModelError(
            "{}: {} is {!r}, which is not finite".format(where, field, value))
    if value < 0:
        raise ComponentModelError(
            "{}: {} is {}, and a passive traversal cannot advance a signal in "
            "time".format(where, field, value))


def _scope(reference):
    return "component traversal" if reference is None else reference


def _claim(where, knowledge, quantity, evidence, significance, basis=None):
    return claim.claim("traversal", where, "ps", knowledge, quantity,
                       evidence, significance, knowledge_basis=basis)


def evaluate(declaration, reference=None):
    """Turn one ``delay_model`` declaration into a shared delay claim."""
    where = _scope(reference)
    if declaration is None:
        detail = ("no delay model is declared for {}, so no delay is attributed; "
                  "passivity establishes only a non-negative lower bound".format(
                      where))
        return _claim(
            where, claim.LOWER_BOUND, {"value": 0.0},
            claim.evidence(
                "propagation_delay", UNMODELLED_EVIDENCE,
                {"source": "manifest: no component delay_model"},
                omitted_contributions=[{"detail": detail}]),
            detail,
            claim.knowledge_basis(claim.DERIVED,
                                  "a passive traversal cannot have negative delay"))

    if isinstance(declaration, str):
        raise ComponentModelError(
            "{}: delay_model is the bare string {!r}; implemented models carry "
            "a justification or provenance and are declared as objects".format(
                where, declaration))
    if not isinstance(declaration, dict):
        raise ComponentModelError(
            "{}: delay_model is a {}, not an object".format(
                where, type(declaration).__name__))
    model = declaration.get("model")
    if not model:
        raise ComponentModelError(
            "{}: delay_model declares no `model`".format(where))

    if model not in IMPLEMENTED_MODELS:
        detail = ("{} declares {!r}, which this toolkit does not implement; "
                  "implemented: {}".format(where, model,
                                             ", ".join(IMPLEMENTED_MODELS)))
        return _claim(
            where, claim.UNKNOWN, {},
            claim.evidence(
                "propagation_delay", None,
                {"source": "manifest component delay_model", "model": model},
                applicability={"status": claim.UNSUPPORTED, "detail": detail}),
            "no path total may be concluded from an unsupported traversal")

    if model == MODEL_NONE:
        justification = declaration.get("justification")
        bound = declaration.get("max_delay_ps")
        if not justification:
            raise ComponentModelError(
                "{}: the {!r} model requires a justification".format(
                    where, model))
        omission = {"detail": "delay omitted deliberately: " + justification}
        if bound is None:
            return _claim(
                where, claim.LOWER_BOUND, {"value": 0.0},
                claim.evidence(
                    "propagation_delay", UNMODELLED_EVIDENCE,
                    {"source": "manifest component delay_model", "model": model},
                    assumptions=[justification],
                    omitted_contributions=[omission]),
                "zero is a lower bound; the reason does not measure the part",
                claim.knowledge_basis(
                    claim.DERIVED,
                    "passivity establishes zero as a lower bound; prose does "
                    "not establish an upper bound"))
        _finite_non_negative(bound, "max_delay_ps", where)
        provenance = declaration.get("provenance")
        if not provenance:
            raise ComponentModelError(
                "{}: max_delay_ps requires provenance; a justification does "
                "not source the number".format(where))
        omission["upper_bound_ps"] = float(bound)
        return _claim(
            where, claim.INTERVAL, {"lower": 0.0, "upper": float(bound)},
            claim.evidence(
                "propagation_delay", UNMODELLED_EVIDENCE,
                {"source": provenance, "model": model},
                assumptions=[justification],
                omitted_contributions=[omission]),
            "the traversal lies between zero and the declared sourced maximum",
            claim.knowledge_basis(
                claim.ASSUMED,
                "the manifest supplies the sourced maximum for omitted delay"))

    delay = declaration.get("delay_ps")
    provenance = declaration.get("provenance")
    if delay is None:
        raise ComponentModelError(
            "{}: the {!r} model declares no `delay_ps`".format(where, model))
    _finite_non_negative(delay, "delay_ps", where)
    if not provenance:
        raise ComponentModelError(
            "{}: the {!r} model states no provenance".format(where, model))
    return _claim(
        where, claim.EXACT, {"value": float(delay)},
        claim.evidence(
            "propagation_delay", DECLARED_MODEL_EVIDENCE,
            {"source": provenance, "model": model}),
        "the traversal contributes the declared fixed-delay model value",
        claim.knowledge_basis(claim.DIRECT,
                              "the declared model directly supplies delay_ps"))


def validate(declaration, reference=None):
    """Validate at parse time. Unsupported is a valid fail-closed result."""
    evaluate(declaration, reference)
