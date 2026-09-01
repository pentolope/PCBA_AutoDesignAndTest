"""Shared evidence and numeric-claim contracts.

An evidence fact answers the non-numeric part of an engineering question:
which phenomenon a model addresses, where the model came from, whether it
applies, and which assumptions or omissions qualify it.  A claim adds scope,
units and one of six numeric knowledge shapes.  Producers keep their physical
detail, but they do not invent another spelling of exactness, bounds,
applicability or PASS/FAIL/UNKNOWN.

Evidence classes remain phenomenon-specific.  This module never ranks an RTL
model against a propagation estimate or a measurement against geometry.  A
producer that has a meaningful local ordering may keep that ordering.
"""

from __future__ import annotations

import copy
import math


PHENOMENA = (
    "interconnect_dc",
    "propagation_delay",
    "characteristic_impedance",
    "coupling",
    "loop_inductance",
    "capacitance",
    "power_integrity",
    "functional_behavior",
    "device_electrical",
    "digital_io",
    "interconnect_si",
    "interconnect_geometry",
    "node_voltage",
)

EXACT = "exact"
LOWER_BOUND = "lower_bound"
UPPER_BOUND = "upper_bound"
INTERVAL = "interval"
APPROXIMATE = "approximate"
UNKNOWN = "unknown"
KNOWLEDGE = (EXACT, LOWER_BOUND, UPPER_BOUND, INTERVAL, APPROXIMATE, UNKNOWN)

APPLICABLE = "applicable"
UNSUPPORTED = "unsupported"
NOT_APPLICABLE = "not-applicable"
APPLICABILITY = (APPLICABLE, UNSUPPORTED, NOT_APPLICABLE)

DIRECT = "direct"
DERIVED = "derived"
ASSUMED = "assumed"
KNOWLEDGE_BASES = (DIRECT, DERIVED, ASSUMED)

PASS = "PASS"
FAIL = "FAIL"
UNKNOWN_RESULT = "UNKNOWN"

SCOPE_LEVELS = ("path", "net", "pair", "group", "board", "measurement",
                "traversal", "model")

_QUANTITY_FIELDS = {
    EXACT: {"value"},
    APPROXIMATE: {"value"},
    LOWER_BOUND: {"value"},
    UPPER_BOUND: {"value"},
    INTERVAL: {"lower", "upper"},
    UNKNOWN: set(),
}
_EVIDENCE_KEYS = {
    "phenomenon", "evidence_class", "provenance", "applicability",
    "assumptions", "omitted_contributions",
}
_CLAIM_KEYS = {
    "kind", "scope", "units", "knowledge", "quantity", "evidence",
    "knowledge_basis", "requirement", "significance",
}


class ClaimError(Exception):
    """Evidence or a claim is malformed.  Malformed evidence never passes."""


def _finite(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(float(value)):
        raise ClaimError("{} must be a finite number, not {!r}".format(
            label, value))
    return float(value)


def _statements(value, label):
    if not isinstance(value, list):
        raise ClaimError("{} must be a list".format(label))
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            continue
        if isinstance(entry, dict) and entry \
                and str(entry.get("detail") or "").strip():
            continue
        raise ClaimError(
            "{} entries are non-empty statements or records with detail".format(
                label))


def evidence(phenomenon, evidence_class, provenance, applicability=None,
             assumptions=(), omitted_contributions=()):
    """Build the evidence fact shared by claims and simulation models."""
    record = {
        "phenomenon": phenomenon,
        "evidence_class": evidence_class,
        "provenance": dict(provenance or {}),
        "applicability": dict(applicability or {
            "status": APPLICABLE, "detail": "model applies to this scope"}),
        "assumptions": list(assumptions),
        "omitted_contributions": list(omitted_contributions),
    }
    return validate_evidence(record)


def validate_evidence(record):
    if not isinstance(record, dict) or set(record) != _EVIDENCE_KEYS:
        raise ClaimError("evidence carries exactly {}".format(
            sorted(_EVIDENCE_KEYS)))
    if record["phenomenon"] not in PHENOMENA:
        raise ClaimError("phenomenon {!r} is not one of {}".format(
            record["phenomenon"], list(PHENOMENA)))
    applicability = record["applicability"]
    if not isinstance(applicability, dict) or \
            set(applicability) != {"status", "detail"}:
        raise ClaimError("applicability carries exactly status and detail")
    if applicability["status"] not in APPLICABILITY:
        raise ClaimError("applicability status {!r} is not one of {}".format(
            applicability["status"], list(APPLICABILITY)))
    if not str(applicability["detail"] or "").strip():
        raise ClaimError("applicability needs a non-empty detail")
    evidence_class = record["evidence_class"]
    if applicability["status"] == APPLICABLE:
        if not str(evidence_class or "").strip():
            raise ClaimError("applicable evidence names its evidence class")
    elif evidence_class is not None:
        raise ClaimError(
            "unsupported or not-applicable evidence has no evidence class")
    provenance = record["provenance"]
    if not isinstance(provenance, dict) or not provenance.get("source"):
        raise ClaimError("evidence whose origin is unstated is unusable")
    _statements(record["assumptions"], "assumptions")
    _statements(record["omitted_contributions"], "omitted_contributions")
    return record


def knowledge_basis(kind, detail):
    record = {"kind": kind, "detail": detail}
    return validate_knowledge_basis(record)


def validate_knowledge_basis(record):
    if record is None:
        return None
    if not isinstance(record, dict) or set(record) != {"kind", "detail"}:
        raise ClaimError("knowledge_basis carries exactly kind and detail")
    if record["kind"] not in KNOWLEDGE_BASES:
        raise ClaimError("knowledge basis {!r} is not one of {}".format(
            record["kind"], list(KNOWLEDGE_BASES)))
    if not str(record["detail"] or "").strip():
        raise ClaimError("knowledge_basis needs a non-empty detail")
    return record


def knowledge_declaration(kind, basis=None):
    """Declare numeric knowledge before a producer has supplied its value."""
    if kind not in KNOWLEDGE:
        raise ClaimError("knowledge {!r} is not one of {}".format(
            kind, list(KNOWLEDGE)))
    basis = copy.deepcopy(basis)
    validate_knowledge_basis(basis)
    if kind in (LOWER_BOUND, UPPER_BOUND, INTERVAL) and basis is None:
        raise ClaimError("bounded knowledge states whether it is derived or assumed")
    return {"kind": kind, "basis": basis}


def validate_knowledge_declaration(record):
    if not isinstance(record, dict) or set(record) != {"kind", "basis"}:
        raise ClaimError("a knowledge declaration carries exactly kind and basis")
    return knowledge_declaration(record["kind"], record["basis"])


def requirement(name, source, assertion):
    record = {"name": name, "source": source,
              "assertion": dict(assertion or {})}
    _validate_requirement(record)
    return record


def _validate_requirement(record):
    if not isinstance(record, dict) or \
            set(record) != {"name", "source", "assertion"}:
        raise ClaimError("a requirement carries exactly name, source and assertion")
    for field in ("name", "source"):
        if not str(record[field] or "").strip():
            raise ClaimError("requirement.{} is required".format(field))
    assertion = record["assertion"]
    if not isinstance(assertion, dict) or assertion.get("op") not in \
            ("<=", ">=", "within"):
        raise ClaimError("assertion op must be <=, >= or within")
    expected = {"op", "value", "tolerance"} if \
        assertion["op"] == "within" else {"op", "value"}
    if set(assertion) != expected:
        raise ClaimError("assertion {!r} carries exactly {}".format(
            assertion["op"], sorted(expected)))
    _finite(assertion["value"], "assertion.value")
    if assertion["op"] == "within":
        tolerance = _finite(assertion["tolerance"], "assertion.tolerance")
        if tolerance <= 0:
            raise ClaimError("assertion.tolerance must be positive")


def claim(scope_level, identity, units, knowledge, quantity, evidence,
          significance, knowledge_basis=None, requirement=None):
    """Build one numeric claim from shared evidence."""
    record = {
        "kind": "claim",
        "scope": {"level": scope_level, "identity": identity},
        "units": units,
        "knowledge": knowledge,
        "quantity": dict(quantity or {}),
        "evidence": copy.deepcopy(evidence),
        "knowledge_basis": copy.deepcopy(knowledge_basis),
        "requirement": copy.deepcopy(requirement),
        "significance": significance,
    }
    return validate(record)


def validate(record):
    if not isinstance(record, dict) or set(record) != _CLAIM_KEYS:
        raise ClaimError("a claim carries exactly {}".format(
            sorted(_CLAIM_KEYS)))
    if record["kind"] != "claim":
        raise ClaimError("kind must be 'claim'")
    scope = record["scope"]
    if not isinstance(scope, dict) or set(scope) != {"level", "identity"}:
        raise ClaimError("scope carries exactly level and identity")
    if scope["level"] not in SCOPE_LEVELS or \
            not str(scope["identity"] or "").strip():
        raise ClaimError("scope needs a known level and non-empty identity")
    if not str(record["units"] or "").strip():
        raise ClaimError("a claim states its units")
    knowledge = record["knowledge"]
    if knowledge not in KNOWLEDGE:
        raise ClaimError("knowledge {!r} is not one of {}".format(
            knowledge, list(KNOWLEDGE)))
    quantity = record["quantity"]
    if not isinstance(quantity, dict) or set(quantity) != _QUANTITY_FIELDS[knowledge]:
        raise ClaimError("knowledge {!r} populates exactly {}".format(
            knowledge, sorted(_QUANTITY_FIELDS[knowledge])))
    for name, value in quantity.items():
        _finite(value, "quantity." + name)
    if knowledge == INTERVAL and quantity["lower"] > quantity["upper"]:
        raise ClaimError("an interval's lower end exceeds its upper end")
    validate_evidence(record["evidence"])
    status = record["evidence"]["applicability"]["status"]
    if status != APPLICABLE and knowledge != UNKNOWN:
        raise ClaimError("unsupported or not-applicable evidence knows no number")
    validate_knowledge_basis(record["knowledge_basis"])
    if knowledge in (LOWER_BOUND, UPPER_BOUND, INTERVAL) and \
            record["knowledge_basis"] is None:
        raise ClaimError("bounded knowledge states whether it is derived or assumed")
    omissions = record["evidence"]["omitted_contributions"]
    assumptions = record["evidence"]["assumptions"]
    if knowledge == EXACT and omissions:
        raise ClaimError("exact knowledge cannot omit a contribution")
    if knowledge == APPROXIMATE and not (assumptions or omissions):
        raise ClaimError("an approximation states its assumptions or omissions")
    if record["requirement"] is not None:
        _validate_requirement(record["requirement"])
    if not str(record["significance"] or "").strip():
        raise ClaimError("a claim states what may be concluded from it")
    return record


def with_requirement(record, requirement_record):
    """Return a validated copy linked to a requirement."""
    validate(record)
    linked = copy.deepcopy(record)
    linked["requirement"] = copy.deepcopy(requirement_record)
    return validate(linked)


def bounds(record):
    """The finite endpoints a claim establishes; either may be None."""
    validate(record)
    knowledge, quantity = record["knowledge"], record["quantity"]
    if knowledge == EXACT:
        return quantity["value"], quantity["value"]
    if knowledge == LOWER_BOUND:
        return quantity["value"], None
    if knowledge == UPPER_BOUND:
        return None, quantity["value"]
    if knowledge == INTERVAL:
        return quantity["lower"], quantity["upper"]
    return None, None


def verdict(record):
    """One conservative PASS/FAIL/UNKNOWN result, or None if descriptive."""
    validate(record)
    required = record["requirement"]
    if required is None:
        return None
    knowledge = record["knowledge"]
    lower, upper = bounds(record)
    assertion = required["assertion"]
    op, target = assertion["op"], assertion["value"]
    result = UNKNOWN_RESULT
    if knowledge not in (APPROXIMATE, UNKNOWN):
        if op == "<=":
            if upper is not None and upper <= target:
                result = PASS
            elif lower is not None and lower > target:
                result = FAIL
        elif op == ">=":
            if lower is not None and lower >= target:
                result = PASS
            elif upper is not None and upper < target:
                result = FAIL
        else:
            tolerance = assertion["tolerance"]
            wanted_lower, wanted_upper = target - tolerance, target + tolerance
            if lower is not None and upper is not None \
                    and lower >= wanted_lower and upper <= wanted_upper:
                result = PASS
            elif (upper is not None and upper < wanted_lower) or \
                    (lower is not None and lower > wanted_upper):
                result = FAIL
    if knowledge == EXACT:
        basis = "exact"
    elif knowledge in (LOWER_BOUND, UPPER_BOUND):
        basis = "bound"
    else:
        basis = knowledge
    return {"result": result, "basis": basis,
            "exact": knowledge == EXACT and result != UNKNOWN_RESULT,
            "knowledge_basis": copy.deepcopy(record["knowledge_basis"])}


def comparable(one, other):
    validate(one)
    validate(other)
    left_evidence, right_evidence = one["evidence"], other["evidence"]
    checks = (
        ("phenomenon", left_evidence["phenomenon"],
         right_evidence["phenomenon"]),
        ("scope.level", one["scope"]["level"], other["scope"]["level"]),
        ("units", one["units"], other["units"]),
        ("knowledge", one["knowledge"], other["knowledge"]),
        ("evidence_class", left_evidence["evidence_class"],
         right_evidence["evidence_class"]),
        ("applicability", left_evidence["applicability"]["status"],
         right_evidence["applicability"]["status"]),
    )
    for label, left, right in checks:
        if left != right:
            return False, "{} differs: {!r} vs {!r}".format(label, left, right)
    return True, ("same phenomenon, scope level, units, knowledge, evidence "
                  "class and applicability")


def require_comparable(one, other):
    ok, detail = comparable(one, other)
    if not ok:
        raise ClaimError("these claims cannot be compared - " + detail)
    return True
