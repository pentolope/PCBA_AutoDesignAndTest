"""The parasitic result contract: every extracted electrical
quantity says what it is, how well it is known, and what may be
decided from it.

A parasitic metric is not a number - it is a CLAIM with declared
semantics:

  * ``exact``       - the model accounts for every contribution it
                      names; an omitted contribution with exact
                      semantics REFUSES at validation, because that
                      is precisely how optimistic numbers become
                      requirements evidence;
  * ``bound``       - a one-sided limit with a direction, the
                      honest shape for omitted positive physics;
  * ``interval``    - two-sided, when both ends are established;
  * ``approximate`` - a value with declared assumptions or
                      omissions, usable descriptively, never able
                      to PASS or FAIL a requirement by itself.

Requirement linkage is explicit and optional: a metric with no
linked requirement is DESCRIPTIVE - it may rank candidates in A/B
comparison but never becomes an invented gate. A linked metric
yields PASS / FAIL / UNKNOWN under conservative rules: a bound may
only decide in the direction it actually establishes, an interval
decides only when it decides entirely, and an approximation never
decides.

Producers outside their model's applicability domain REFUSE rather
than emitting a plausible record; a blockage record exists so "we
cannot measure this yet, because X" is itself machine-readable
evidence.
"""

from __future__ import annotations


class ParasiticsError(Exception):
    """The record cannot be accepted or produced as declared."""


PHENOMENA = (
    "interconnect_dc",
    "propagation_delay",
    "characteristic_impedance",
    "coupling",
    "loop_inductance",
    "capacitance",
    "power_integrity",
)

SEMANTICS = ("exact", "bound", "interval", "approximate")

_SCOPE_LEVELS = ("path", "net", "pair", "group", "board")

_REQUIRED_KEYS = {
    "kind", "phenomenon", "scope", "quantity", "model",
    "provenance", "assumptions", "omitted_contributions",
    "applicability", "requirement_linkage",
    "decision_significance",
}


def _require(condition, message):
    if not condition:
        raise ParasiticsError(message)


def _finite_number(value):
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == value
            and value not in (float("inf"), float("-inf")))


def validate_metric(record):
    """Strict shape and semantics check; fail-closed everywhere."""
    _require(isinstance(record, dict)
             and set(record) == _REQUIRED_KEYS,
             "a parasitic metric carries exactly {}".format(
                 sorted(_REQUIRED_KEYS)))
    _require(record["kind"] == "parasitic-metric",
             "kind must be 'parasitic-metric'")
    _require(record["phenomenon"] in PHENOMENA,
             "phenomenon {!r} is not one of {}".format(
                 record["phenomenon"], list(PHENOMENA)))
    scope = record["scope"]
    _require(isinstance(scope, dict)
             and set(scope) == {"level", "identity"}
             and scope["level"] in _SCOPE_LEVELS
             and isinstance(scope["identity"], str)
             and scope["identity"],
             "scope carries exactly level (one of {}) and a "
             "nonempty identity".format(list(_SCOPE_LEVELS)))
    quantity = record["quantity"]
    _require(isinstance(quantity, dict)
             and set(quantity) == {"semantics", "value", "bound",
                                   "interval", "units"},
             "quantity carries exactly semantics, value, bound, "
             "interval and units")
    _require(quantity["semantics"] in SEMANTICS,
             "quantity.semantics must be one of {}".format(
                 list(SEMANTICS)))
    _require(isinstance(quantity["units"], str)
             and quantity["units"],
             "quantity.units must be a nonempty string")
    semantics = quantity["semantics"]
    populated = {name for name in ("value", "bound", "interval")
                 if quantity[name] is not None}
    expected = {"exact": {"value"}, "approximate": {"value"},
                "bound": {"bound"}, "interval": {"interval"}}
    _require(populated == expected[semantics],
             "semantics {!r} populates exactly {}, not {}".format(
                 semantics, sorted(expected[semantics]),
                 sorted(populated)))
    if quantity["value"] is not None:
        _require(_finite_number(quantity["value"]),
                 "quantity.value must be a finite number")
    if quantity["bound"] is not None:
        bound = quantity["bound"]
        _require(isinstance(bound, dict)
                 and set(bound) == {"direction", "value"}
                 and bound["direction"] in ("lower", "upper")
                 and _finite_number(bound["value"]),
                 "quantity.bound carries direction (lower/upper) "
                 "and a finite value")
    if quantity["interval"] is not None:
        interval = quantity["interval"]
        _require(isinstance(interval, dict)
                 and set(interval) == {"lower", "upper"}
                 and _finite_number(interval["lower"])
                 and _finite_number(interval["upper"])
                 and interval["lower"] <= interval["upper"],
                 "quantity.interval needs finite lower <= upper")
    model = record["model"]
    _require(isinstance(model, dict)
             and set(model) == {"name", "fidelity"}
             and all(isinstance(model[k], str) and model[k]
                     for k in ("name", "fidelity")),
             "model carries exactly a nonempty name and fidelity")
    provenance = record["provenance"]
    _require(isinstance(provenance, dict) and provenance
             and isinstance(provenance.get("source"), str)
             and provenance["source"],
             "provenance must be a nonempty dict naming its "
             "source")
    for key in ("assumptions", "omitted_contributions"):
        value = record[key]
        _require(isinstance(value, list)
                 and all(isinstance(item, str) and item
                         for item in value),
                 "{} must be a list of nonempty strings".format(
                     key))
    _require(not (semantics == "exact"
                  and record["omitted_contributions"]),
             "exact semantics with omitted contributions refuses: "
             "an omission makes the value a bound or an "
             "approximation, never exact")
    _require(not (semantics == "approximate"
                  and not record["assumptions"]
                  and not record["omitted_contributions"]),
             "approximate semantics must declare at least one "
             "assumption or omitted contribution - an unexplained "
             "approximation is just an unaudited number")
    applicability = record["applicability"]
    _require(isinstance(applicability, dict)
             and set(applicability) == {"applicable", "detail"}
             and applicability["applicable"] is True
             and isinstance(applicability["detail"], str)
             and applicability["detail"],
             "a parasitic METRIC is only emitted inside its "
             "model's applicability domain (applicable must be "
             "True with a nonempty detail); outside the domain "
             "the producer refuses or emits a blockage record")
    linkage = record["requirement_linkage"]
    if linkage is not None:
        _require(isinstance(linkage, dict)
                 and set(linkage) == {"requirement", "source",
                                      "assertion"},
                 "requirement_linkage carries exactly "
                 "requirement, source and assertion")
        _require(isinstance(linkage["requirement"], str)
                 and linkage["requirement"]
                 and isinstance(linkage["source"], str)
                 and linkage["source"],
                 "requirement_linkage needs a nonempty "
                 "requirement name and source")
        assertion = linkage["assertion"]
        _require(isinstance(assertion, dict)
                 and assertion.get("op") in ("<=", ">=")
                 and _finite_number(assertion.get("value")),
                 "requirement_linkage.assertion needs op <= or >= "
                 "with a finite value")
    _require(isinstance(record["decision_significance"], str)
             and record["decision_significance"],
             "decision_significance must be a nonempty string")
    return record


def blocked(phenomenon, scope_level, scope_identity, reason,
            needed):
    """A machine-readable 'cannot measure this yet, because X'."""
    _require(phenomenon in PHENOMENA,
             "phenomenon {!r} is not one of {}".format(
                 phenomenon, list(PHENOMENA)))
    _require(isinstance(reason, str) and reason,
             "a blockage needs a nonempty reason")
    _require(isinstance(needed, str) and needed,
             "a blockage names what must be modeled next")
    return {
        "kind": "parasitic-blocked",
        "phenomenon": phenomenon,
        "scope": {"level": scope_level,
                  "identity": scope_identity},
        "reason": reason,
        "needed_next": needed,
    }


def requirement_verdict(record):
    """PASS / FAIL / UNKNOWN for a requirement-linked metric;
    None for a descriptive one - a metric with no requirement
    never becomes a gate.

    Conservative by construction: a bound decides only in the
    direction it establishes, an interval decides only when it
    decides entirely, and an approximation never decides.
    """
    validate_metric(record)
    linkage = record["requirement_linkage"]
    if linkage is None:
        return None
    op = linkage["assertion"]["op"]
    limit = linkage["assertion"]["value"]
    quantity = record["quantity"]
    semantics = quantity["semantics"]
    if semantics == "exact":
        value = quantity["value"]
        satisfied = value <= limit if op == "<=" else value >= limit
        return "PASS" if satisfied else "FAIL"
    if semantics == "approximate":
        return "UNKNOWN"
    if semantics == "bound":
        direction = quantity["bound"]["direction"]
        value = quantity["bound"]["value"]
        if op == "<=":
            if direction == "upper" and value <= limit:
                return "PASS"
            if direction == "lower" and value > limit:
                return "FAIL"
            return "UNKNOWN"
        if direction == "lower" and value >= limit:
            return "PASS"
        if direction == "upper" and value < limit:
            return "FAIL"
        return "UNKNOWN"
    interval = quantity["interval"]
    if op == "<=":
        if interval["upper"] <= limit:
            return "PASS"
        if interval["lower"] > limit:
            return "FAIL"
        return "UNKNOWN"
    if interval["lower"] >= limit:
        return "PASS"
    if interval["upper"] < limit:
        return "FAIL"
    return "UNKNOWN"


def require_comparable(one, other):
    """Refuse before any two parasitic metrics meet in a
    comparison: same phenomenon, same scope level, same units,
    same semantics, same model name and fidelity. Unmatched
    fidelity never yields an A-versus-B statement."""
    validate_metric(one)
    validate_metric(other)
    for path in (("phenomenon",), ("scope", "level"),
                 ("quantity", "units"), ("quantity", "semantics"),
                 ("model", "name"), ("model", "fidelity")):
        a, b = one, other
        for key in path:
            a, b = a[key], b[key]
        if a != b:
            raise ParasiticsError(
                "metrics are not comparable: {} differs "
                "({!r} vs {!r}); a comparison across unmatched "
                "semantics or fidelity is refused".format(
                    ".".join(path), a, b))
    return True
