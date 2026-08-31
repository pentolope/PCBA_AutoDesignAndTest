"""One representation for "what do we know about this quantity, and how well".

Five producers in this toolkit answer that question about different physics:
copper geometry, interconnect propagation, component traversals, simulation
coverage, and simulated measurements. Each grew its own words for the same
handful of facts - `exact` meant four different things, `fidelity` named a
ranked ladder in one module and an unordered vocabulary in another, `bound`
described three different data shapes - and a reader had to learn each one
before they could compare anything.

The distinctions are all real. The duplication is not. This module is the
shared shape they adapt into, and the place the conservative rule lives.

**Only `pcbqa.parasitics` has been migrated.** `propagation`, `component_models`,
`sim/fidelity` and `sim/scenario` still carry their own vocabularies, and
`sim/scenario.classify_assertion` is still a second conservative-verdict
machine. Migrating them means changing the timing and simulation plumbing, and
that is not done. Until it is, this is one shared shape beside four private
ones - an improvement over five private ones, and not yet the unification it
is meant to be.

The shape:

    phenomenon             what physical quantity is being described
    scope                  what it is about: a path, a net, a pair, a board
    units                  mandatory, because a number without them decides
                           nothing
    knowledge              exact | lower_bound | upper_bound | interval |
                           approximate | unknown
    evidence_class         how it was obtained - a name, never a rank. There
                           is no universal ladder across phenomena, and this
                           module deliberately declines to invent one; where a
                           ladder is meaningful it stays inside the producer
                           that can order it.
    provenance             where it came from, with a source
    applicability          whether the producing model applies here at all
    assumptions            what had to be granted
    omitted_contributions  what is knowingly not in the number
    requirement            optional: the assertion this claim is judged
                           against, or None for a descriptive claim
    significance           what a reader may conclude

`verdict()` is the one conservative rule they all share: a bound decides only
in the direction it establishes, an interval only when it decides entirely,
an approximation never, and an unknown never. A claim with no requirement is
descriptive and never becomes a gate.
"""

from __future__ import annotations

import math

#: What a claim can be about. Open by construction: a producer names its own
#: phenomenon, and this list is the set anything in this toolkit measures.
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
)

#: How much is known about the number. The single axis that replaces
#: `semantics`, `exact`, `delay_is_lower_bound`, `value_bound.direction` and
#: `model_status` - each of which was a different spelling of this.
EXACT = "exact"
LOWER_BOUND = "lower_bound"
UPPER_BOUND = "upper_bound"
INTERVAL = "interval"
APPROXIMATE = "approximate"
UNKNOWN = "unknown"
KNOWLEDGE = (EXACT, LOWER_BOUND, UPPER_BOUND, INTERVAL, APPROXIMATE, UNKNOWN)

#: Which knowledge kinds populate which value fields. Anything else refuses.
_VALUE_FIELDS = {
    EXACT: {"value"},
    APPROXIMATE: {"value"},
    LOWER_BOUND: {"value"},
    UPPER_BOUND: {"value"},
    INTERVAL: {"lower", "upper"},
    UNKNOWN: set(),
}

SCOPE_LEVELS = ("path", "net", "pair", "group", "board", "measurement",
                "traversal")

_REQUIRED_KEYS = {
    "kind", "phenomenon", "scope", "units", "knowledge", "quantity",
    "evidence_class", "provenance", "applicability", "assumptions",
    "omitted_contributions", "requirement", "significance",
}

KIND = "claim"


class ClaimError(Exception):
    """A claim that cannot be read is never a claim that passed."""


def _finite(value, label):
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or not math.isfinite(float(value)):
        raise ClaimError("{} must be a finite number, not {!r}".format(
            label, value))
    return float(value)


def claim(phenomenon, scope_level, identity, units, knowledge, quantity,
          evidence_class, provenance, significance, applicability=None,
          assumptions=(), omitted_contributions=(), requirement=None):
    """Build one claim. Every argument that decides anything is required."""
    record = {
        "kind": KIND,
        "phenomenon": phenomenon,
        "scope": {"level": scope_level, "identity": identity},
        "units": units,
        "knowledge": knowledge,
        "quantity": dict(quantity or {}),
        "evidence_class": evidence_class,
        "provenance": dict(provenance or {}),
        "applicability": dict(applicability
                              or {"applicable": True, "detail": ""}),
        "assumptions": list(assumptions),
        "omitted_contributions": list(omitted_contributions),
        "requirement": requirement,
        "significance": significance,
    }
    validate(record)
    return record


def validate(record):
    """Raise ClaimError unless the record is a well-formed claim."""
    if not isinstance(record, dict):
        raise ClaimError("a claim is an object, not a {}".format(
            type(record).__name__))
    if set(record) != _REQUIRED_KEYS:
        raise ClaimError(
            "a claim carries exactly {}; got {}".format(
                sorted(_REQUIRED_KEYS), sorted(record)))
    if record["kind"] != KIND:
        raise ClaimError("kind must be {!r}".format(KIND))
    if record["phenomenon"] not in PHENOMENA:
        raise ClaimError("phenomenon {!r} is not one of {}".format(
            record["phenomenon"], list(PHENOMENA)))

    scope = record["scope"]
    if not isinstance(scope, dict) or set(scope) != {"level", "identity"}:
        raise ClaimError("scope carries exactly level and identity")
    if scope["level"] not in SCOPE_LEVELS:
        raise ClaimError("scope level {!r} is not one of {}".format(
            scope["level"], list(SCOPE_LEVELS)))
    if not str(scope["identity"] or "").strip():
        raise ClaimError("a claim about nothing in particular is not a claim")

    if not str(record["units"] or "").strip():
        raise ClaimError(
            "a claim states its units; a number without them decides nothing")

    knowledge = record["knowledge"]
    if knowledge not in KNOWLEDGE:
        raise ClaimError("knowledge {!r} is not one of {}".format(
            knowledge, list(KNOWLEDGE)))
    quantity = record["quantity"]
    if not isinstance(quantity, dict):
        raise ClaimError("quantity is an object")
    expected = _VALUE_FIELDS[knowledge]
    if set(quantity) != expected:
        raise ClaimError(
            "knowledge {!r} populates exactly {}, not {}".format(
                knowledge, sorted(expected) or "nothing", sorted(quantity)))
    for name, value in sorted(quantity.items()):
        _finite(value, "quantity." + name)
    if knowledge == INTERVAL and quantity["lower"] > quantity["upper"]:
        raise ClaimError("an interval's lower end exceeds its upper end")

    if not str(record["evidence_class"] or "").strip():
        raise ClaimError(
            "a claim names how it was obtained; without that a reader cannot "
            "tell a measurement from an estimate")
    provenance = record["provenance"]
    if not isinstance(provenance, dict) or not provenance.get("source"):
        raise ClaimError(
            "a claim whose origin is unstated is not usable evidence")

    applicability = record["applicability"]
    if not isinstance(applicability, dict) or \
            set(applicability) != {"applicable", "detail"}:
        raise ClaimError("applicability carries exactly applicable and detail")
    if not isinstance(applicability["applicable"], bool):
        raise ClaimError("applicability.applicable is a boolean")
    if not applicability["applicable"] and knowledge != UNKNOWN:
        raise ClaimError(
            "a claim from outside its model's applicability domain knows "
            "nothing; it is {!r}, not {!r}".format(UNKNOWN, knowledge))

    for field in ("assumptions", "omitted_contributions"):
        entries = record[field]
        if not isinstance(entries, list) or \
                any(not str(e or "").strip() for e in entries):
            raise ClaimError("{} is a list of non-empty statements".format(
                field))
    if knowledge == EXACT and record["omitted_contributions"]:
        raise ClaimError(
            "exact knowledge with omitted contributions refuses: an omission "
            "makes the value a bound or an approximation, never exact")
    if knowledge == APPROXIMATE and not (record["assumptions"]
                                         or record["omitted_contributions"]):
        raise ClaimError(
            "an unexplained approximation is just an unaudited number")

    requirement = record["requirement"]
    if requirement is not None:
        if not isinstance(requirement, dict) or \
                set(requirement) != {"requirement", "source", "assertion"}:
            raise ClaimError(
                "a requirement carries exactly requirement, source and "
                "assertion")
        for field in ("requirement", "source"):
            if not str(requirement[field] or "").strip():
                raise ClaimError("requirement.{} is required".format(field))
        assertion = requirement["assertion"]
        if not isinstance(assertion, dict) or set(assertion) != {"op", "value"}:
            raise ClaimError("an assertion carries exactly op and value")
        if assertion["op"] not in ("<=", ">="):
            raise ClaimError(
                "assertion op {!r} is not <= or >=".format(assertion["op"]))
        _finite(assertion["value"], "assertion.value")

    if not str(record["significance"] or "").strip():
        raise ClaimError(
            "a claim states what may be concluded from it; a number with no "
            "stated significance invites the reader to invent one")
    return record


def verdict(record):
    """PASS / FAIL / UNKNOWN, or None for a claim with no requirement.

    The one conservative rule all five producers share. A bound decides only
    in the direction it establishes; an interval only when it decides
    entirely; an approximation and an unknown never decide at all.
    """
    validate(record)
    requirement = record["requirement"]
    if requirement is None:
        return None
    op = requirement["assertion"]["op"]
    limit = requirement["assertion"]["value"]
    knowledge = record["knowledge"]
    quantity = record["quantity"]

    if knowledge == EXACT:
        value = quantity["value"]
        return "PASS" if (value <= limit if op == "<=" else value >= limit) \
            else "FAIL"
    if knowledge in (APPROXIMATE, UNKNOWN):
        return "UNKNOWN"
    if knowledge == UPPER_BOUND:
        value = quantity["value"]
        if op == "<=":
            return "PASS" if value <= limit else "UNKNOWN"
        return "FAIL" if value < limit else "UNKNOWN"
    if knowledge == LOWER_BOUND:
        value = quantity["value"]
        if op == "<=":
            return "FAIL" if value > limit else "UNKNOWN"
        return "PASS" if value >= limit else "UNKNOWN"
    lower, upper = quantity["lower"], quantity["upper"]
    if op == "<=":
        if upper <= limit:
            return "PASS"
        return "FAIL" if lower > limit else "UNKNOWN"
    if lower >= limit:
        return "PASS"
    return "FAIL" if upper < limit else "UNKNOWN"


def comparable(one, other):
    """Whether two claims may be compared numerically at all.

    Same phenomenon, same scope level, same units, same knowledge kind and the
    same evidence class. Comparing across unmatched evidence is how a
    geometric estimate ends up ranked against a measurement.
    """
    validate(one)
    validate(other)
    for path in (("phenomenon",), ("units",), ("knowledge",),
                 ("evidence_class",), ("scope", "level")):
        left, right = one, other
        for step in path:
            left, right = left[step], right[step]
        if left != right:
            return False, "{} differs: {!r} vs {!r}".format(
                ".".join(path), left, right)
    return True, "same phenomenon, scope level, units, knowledge and evidence"


def require_comparable(one, other):
    ok, detail = comparable(one, other)
    if not ok:
        raise ClaimError(
            "these two claims cannot be compared - {}".format(detail))
    return True


# ---------------------------------------------------------------------------
# adapters
#
# A producer keeps its own record - the physics is not the same and the detail
# is load-bearing where it is produced. What it stops keeping is a private
# answer to "how well is this known".
#
# There is one adapter, because one producer has been migrated. Adapters for
# the others are not written until the producer that needs one is migrated
# with it: an adapter no producer calls is reserved architecture, and this
# toolkit does not reserve architecture.
# ---------------------------------------------------------------------------

def from_parasitic_metric(record):
    """`pcbqa.parasitics` metrics, whose shape this model generalises."""
    quantity = record["quantity"]
    semantics = quantity["semantics"]
    if semantics == "exact":
        knowledge, values = EXACT, {"value": quantity["value"]}
    elif semantics == "approximate":
        knowledge, values = APPROXIMATE, {"value": quantity["value"]}
    elif semantics == "bound":
        bound = quantity["bound"]
        knowledge = (UPPER_BOUND if bound["direction"] == "upper"
                     else LOWER_BOUND)
        values = {"value": bound["value"]}
    else:
        knowledge = INTERVAL
        values = {"lower": quantity["interval"]["lower"],
                  "upper": quantity["interval"]["upper"]}
    return claim(
        record["phenomenon"], record["scope"]["level"],
        record["scope"]["identity"], quantity["units"], knowledge, values,
        record["model"]["fidelity"], record["provenance"],
        record["decision_significance"],
        applicability=record["applicability"],
        assumptions=record["assumptions"],
        omitted_contributions=record["omitted_contributions"],
        requirement=record["requirement_linkage"])
