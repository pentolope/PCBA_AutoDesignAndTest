"""Simulation-model registry using the shared evidence contract.

A model describes SOME phenomena and is silent about the rest, and the
registry never lets strength in one domain stand in for another: RTL
says nothing about pin electrical behavior, IBIS says nothing about
internal logic, a full-wave interconnect extraction says nothing about
device function, and a DC-resistance extraction is not a transmission
line. Each model therefore carries shared ``pcbqa.claim`` evidence facts, and
a scenario requirement names, per phenomenon, the SET of evidence
classes it accepts. Satisfaction is computed per phenomenon; a model
with no entry for a phenomenon never satisfies it, whatever else it
covers. There is deliberately NO ordering across phenomena and no
default acceptability within one: the requirement spells out what it
accepts, so acceptability is reviewable instead of implied.

There is no simulation-local spelling of provenance, applicability,
assumptions, omissions, unsupported or not-applicable.
"""

from __future__ import annotations

from .. import claim


class SimulationError(Exception):
    """The simulation subsystem cannot proceed as asked. Always blocks."""


#: Evidence classes a simulation model may carry. A vocabulary, not a
#: ladder: no cross-phenomenon comparison exists, and within one
#: phenomenon a requirement lists exactly the classes it accepts.
MODEL_EVIDENCE_CLASSES = (
    "measured",
    "vendor-spice",
    "vendor-ibis",
    "rtl",
    "behavioral-contract",
    "full-wave-extracted",
    "quasi-static-extracted",
    "analytic",
    "geometry-derived",
    "datasheet-behavioral",
    "assumed-behavioral",
)

_REQUIRED_MODEL_KEYS = {"identity", "kind", "evidence"}
_KNOWN_MODEL_KEYS = _REQUIRED_MODEL_KEYS | {"ports", "spice", "notes",
                                            "conditions",
                                            "derivation"}

#: Operating conditions the subsystem understands today. Deliberately
#: small: temperature is the one condition a backend already applies.
#: Supply voltage, corners, frequency ranges and load conditions join
#: this tuple when a model class genuinely declares them.
CONDITIONS = ("temperature_c",)

_CONDITION_KINDS = ("fixed-reference", "parameterized")
_CONDITION_KEYS = {
    "fixed-reference": {"kind", "value", "units", "source"},
    "parameterized": {"kind", "range", "units", "source"},
}


def _condition_finite(label, value):
    if isinstance(value, bool) or not isinstance(value, (int, float))             or value != value             or value in (float("inf"), float("-inf")):
        raise SimulationError(
            "{} must be a finite number, not {!r}".format(label,
                                                          value))
    return value


def validate_conditions(conditions):
    """A model's operating-condition declarations, strictly.

    Each entry states how the model relates to ONE condition:
    ``fixed-reference`` (the model's numbers are valid at exactly the
    stated value - using them at any other requested value is NOT
    covered) or ``parameterized`` (the model genuinely responds to
    the condition across the stated closed range). A missing entry is
    an honest absence: condition coverage treats it as NOT covered,
    never as insensitive.
    """
    if not isinstance(conditions, dict) or not conditions:
        raise SimulationError(
            "conditions must be a nonempty dict of "
            "condition name -> declaration")
    for name, declaration in conditions.items():
        if name not in CONDITIONS:
            raise SimulationError(
                "condition {!r} is not one of {}".format(
                    name, list(CONDITIONS)))
        if not isinstance(declaration, dict):
            raise SimulationError(
                "condition {!r} declaration must be a dict".format(
                    name))
        kind = declaration.get("kind")
        if kind not in _CONDITION_KINDS:
            raise SimulationError(
                "condition {!r} kind {!r} is not one of {}".format(
                    name, kind, list(_CONDITION_KINDS)))
        expected = _CONDITION_KEYS[kind]
        unknown = sorted(set(declaration) - expected)
        missing = sorted(expected - set(declaration))
        if unknown or missing:
            raise SimulationError(
                "condition {!r} declaration must carry exactly keys "
                "{} (unknown {}, missing {})".format(
                    name, sorted(expected), unknown, missing))
        for key in ("units", "source"):
            if not isinstance(declaration[key], str)                     or not declaration[key]:
                raise SimulationError(
                    "condition {!r} needs a nonempty {}".format(
                        name, key))
        if kind == "fixed-reference":
            _condition_finite("condition {!r} value".format(name),
                              declaration["value"])
        else:
            bounds = declaration["range"]
            if not (isinstance(bounds, list) and len(bounds) == 2):
                raise SimulationError(
                    "condition {!r} range must be [low, high]".format(
                        name))
            low = _condition_finite(
                "condition {!r} range low".format(name), bounds[0])
            high = _condition_finite(
                "condition {!r} range high".format(name), bounds[1])
            if not low < high:
                raise SimulationError(
                    "condition {!r} range must satisfy "
                    "low < high".format(name))
    return conditions


def validate_model_evidence(records):
    """A nonempty, one-fact-per-phenomenon evidence set."""
    if not isinstance(records, list) or not records:
        raise SimulationError("model evidence must be a nonempty list")
    seen = set()
    for record in records:
        try:
            claim.validate_evidence(record)
        except claim.ClaimError as exc:
            raise SimulationError(str(exc)) from exc
        phenomenon = record["phenomenon"]
        if phenomenon in seen:
            raise SimulationError(
                "model evidence names phenomenon {!r} twice".format(
                    phenomenon))
        seen.add(phenomenon)
        evidence_class = record["evidence_class"]
        if evidence_class is not None and \
                evidence_class not in MODEL_EVIDENCE_CLASSES:
            raise SimulationError(
                "model evidence class {!r} is not one of {}".format(
                    evidence_class, list(MODEL_EVIDENCE_CLASSES)))
    return records


def validate_requirement(requirement):
    """A requirement: phenomenon -> nonempty list of accepted classes."""
    if not isinstance(requirement, dict) or not requirement:
        raise SimulationError(
            "required_coverage must be a nonempty dict of "
            "phenomenon -> accepted evidence classes")
    for phenomenon, accepted in requirement.items():
        if phenomenon not in claim.PHENOMENA:
            raise SimulationError(
                "required phenomenon {!r} is not one of {}".format(
                    phenomenon, list(claim.PHENOMENA)))
        if not isinstance(accepted, list) or not accepted:
            raise SimulationError(
                "requirement for {!r} must list at least one accepted "
                "evidence class".format(phenomenon))
        for evidence in accepted:
            if evidence not in MODEL_EVIDENCE_CLASSES:
                raise SimulationError(
                    "accepted class {!r} is not one of {}".format(
                    evidence, list(MODEL_EVIDENCE_CLASSES)))
    return requirement


def validate_model(record):
    """One model record, strictly."""
    if not isinstance(record, dict):
        raise SimulationError(
            "a model record must be a dict, not {!r}".format(
                type(record).__name__))
    unknown = sorted(set(record) - _KNOWN_MODEL_KEYS)
    if unknown:
        raise SimulationError(
            "model record carries unknown key(s) {}; unknown keys "
            "refuse rather than being ignored".format(unknown))
    missing = sorted(_REQUIRED_MODEL_KEYS - set(record))
    if missing:
        raise SimulationError(
            "model record is missing required key(s) {}".format(missing))
    if not isinstance(record["identity"], str) or not record["identity"]:
        raise SimulationError("model identity must be a nonempty string")
    validate_model_evidence(record["evidence"])
    if "spice" in record and not isinstance(record["spice"], str):
        raise SimulationError(
            "model {!r} spice text must be a string".format(
                record["identity"]))
    if "conditions" in record:
        validate_conditions(record["conditions"])
    if "derivation" in record and \
            not isinstance(record["derivation"], dict):
        raise SimulationError(
            "model {!r} derivation must be a dict recording the "
            "evidence chain".format(record["identity"]))
    return record


class ModelRegistry:
    """The set of models a scenario may reference. Fail-closed lookup."""

    def __init__(self, records=()):
        self._models = {}
        for record in records:
            self.add(record)

    def add(self, record):
        validate_model(record)
        identity = record["identity"]
        if identity in self._models:
            raise SimulationError(
                "model {!r} is already registered; a silent overwrite "
                "could swap evidence classes unnoticed".format(identity))
        self._models[identity] = record
        return record

    def get(self, identity):
        try:
            return self._models[identity]
        except KeyError:
            raise SimulationError(
                "model {!r} is not registered; the simulation refuses "
                "before any simulator runs, because a missing model "
                "cannot be defaulted".format(identity)) from None

    def identities(self):
        return sorted(self._models)

    def coverage_report(self, identities, requirement=None):
        """Per-phenomenon coverage of the referenced model set.

        For each phenomenon of an (optional) requirement: which models
        satisfy it with an ACCEPTED evidence class, which referenced
        models cover the phenomenon at an unaccepted class, and
        whether the requirement is met. A model never satisfies a
        phenomenon it does not cover - there is no substitution across
        domains, by construction.
        """
        models = {name: self.get(name) for name in identities}
        summary = {
            name: {"kind": model["kind"],
                   "evidence": sorted(model["evidence"],
                                      key=lambda e: e["phenomenon"])}
            for name, model in sorted(models.items())
        }
        report = {"models": summary, "requirement": None,
                  "satisfied": None, "per_phenomenon": None}
        if requirement is None:
            return report
        validate_requirement(requirement)
        per_phenomenon = {}
        satisfied = True
        for phenomenon, accepted in sorted(requirement.items()):
            facts = {name: _evidence_by_phenomenon(model).get(phenomenon)
                     for name, model in models.items()}
            satisfying = sorted(
                name for name, fact in facts.items()
                if fact is not None
                and fact["applicability"]["status"] == claim.APPLICABLE
                and fact["evidence_class"] in accepted)
            covering_unaccepted = sorted(
                name for name, fact in facts.items()
                if fact is not None and name not in satisfying)
            met = bool(satisfying)
            satisfied = satisfied and met
            per_phenomenon[phenomenon] = {
                "accepted_classes": list(accepted),
                "satisfied_by": satisfying,
                "covered_at_unaccepted_class": covering_unaccepted,
                "met": met,
            }
        report.update({"requirement": requirement,
                       "satisfied": satisfied,
                       "per_phenomenon": per_phenomenon})
        return report


def _evidence_by_phenomenon(model):
    return {record["phenomenon"]: record for record in model["evidence"]}
