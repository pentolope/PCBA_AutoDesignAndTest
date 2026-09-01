"""Simulation scenarios: one declared experiment, strictly validated.

The governing rule, stated once: ANYTHING this contract accepts either
affects execution and result provenance, or is refused. There is no
key that validates and is then silently ignored - an AI must never
believe it simulated a condition the deck dropped. Consequently:

  * ``operating_conditions`` is accepted with exactly one field today,
    ``temperature_c``, because the ngspice backend genuinely applies
    it (as the simulator temperature) and records it in the result;
  * ``substitutions`` is NOT accepted: model substitution is not
    implemented yet, so declaring it refuses instead of pretending;
  * every nested record - elements, analyses, measurements,
    assertions, pulse definitions - validates its exact key set, and
    every numeric field rejects bools, NaN, the infinities and
    physically senseless ranges.

Nothing is silently normalized; a malformed request is the caller's to
fix.
"""

from __future__ import annotations

from .. import claim
from .model_registry import SimulationError, validate_requirement

#: The reference node. Contribution closure does not propagate
#: through it: two subcircuits that share only the reference are
#: electrically independent for the measurements this contract
#: supports (ideal sources pin their nodes), and over-merging them
#: would let one subcircuit's strong model bless another's weak one.
GROUND_NODE = "0"

_KNOWN_SCENARIO_KEYS = {
    "name", "description", "elements", "analyses", "measurements",
    "operating_conditions", "required_coverage", "assumptions",
}

_ASSUMPTION_KEYS = {"stands_in_for", "accepted_for_design_decision"}
_REQUIRED_SCENARIO_KEYS = {"name", "elements", "analyses",
                           "measurements"}

_ELEMENT_KEYS = {
    "resistor": {"kind", "name", "nodes", "value"},
    "capacitor": {"kind", "name", "nodes", "value"},
    "inductor": {"kind", "name", "nodes", "value"},
    "vsource_dc": {"kind", "name", "nodes", "value"},
    "vsource_pulse": {"kind", "name", "nodes", "pulse"},
    "model_instance": {"kind", "name", "nodes", "model"},
}

_ANALYSIS_KEYS = {"op": {"kind"},
                  "tran": {"kind", "step_s", "stop_s"}}

_MEASUREMENT_KEYS = {"name", "kind", "node", "assertion", "knowledge"}
_MEASUREMENT_KINDS = ("op_voltage", "tran_final_voltage")

_ASSERTION_KEYS = {"<=": {"op", "value"},
                   ">=": {"op", "value"},
                   "within": {"op", "value", "tolerance"}}

_PULSE_KEYS = {"v1", "v2", "delay_s", "rise_s", "fall_s", "width_s",
               "period_s"}

_OPERATING_KEYS = {"temperature_c"}


def _require(condition, message):
    if not condition:
        raise SimulationError(message)


def _finite(label, value, minimum=None, strict_minimum=None):
    _require(isinstance(value, (int, float))
             and not isinstance(value, bool)
             and value == value
             and value not in (float("inf"), float("-inf")),
             "{} must be a finite number, not {!r}".format(label,
                                                           value))
    if minimum is not None:
        _require(value >= minimum,
                 "{} must be at least {}".format(label, minimum))
    if strict_minimum is not None:
        _require(value > strict_minimum,
                 "{} must be greater than {}".format(label,
                                                     strict_minimum))
    return value


def _exact_keys(label, record, expected):
    unknown = sorted(set(record) - expected)
    _require(not unknown,
             "{} carries unknown key(s) {}".format(label, unknown))
    missing = sorted(expected - set(record))
    _require(not missing,
             "{} is missing key(s) {}".format(label, missing))


def validate_scenario(scenario):
    """Validate one scenario dict, strictly and completely."""
    _require(isinstance(scenario, dict), "a scenario must be a dict")
    unknown = sorted(set(scenario) - _KNOWN_SCENARIO_KEYS)
    _require(not unknown,
             "scenario carries unknown key(s) {}; features the "
             "backend does not consume (substitutions among them) "
             "refuse until they are implemented".format(unknown))
    missing = sorted(_REQUIRED_SCENARIO_KEYS - set(scenario))
    _require(not missing,
             "scenario is missing required key(s) {}".format(missing))
    _require(isinstance(scenario["name"], str) and scenario["name"],
             "scenario name must be a nonempty string")

    elements = scenario["elements"]
    _require(isinstance(elements, list) and elements,
             "scenario elements must be a nonempty list")
    names = set()
    for element in elements:
        _require(isinstance(element, dict), "each element is a dict")
        kind = element.get("kind")
        _require(kind in _ELEMENT_KEYS,
                 "element kind {!r} is not one of {}".format(
                     kind, sorted(_ELEMENT_KEYS)))
        _exact_keys("{} element".format(kind), element,
                    _ELEMENT_KEYS[kind])
        name = element["name"]
        _require(isinstance(name, str) and name,
                 "each element needs a nonempty name")
        _require(name not in names,
                 "element name {!r} appears twice".format(name))
        names.add(name)
        nodes = element["nodes"]
        _require(isinstance(nodes, list) and len(nodes) >= 2 and
                 all(isinstance(n, str) and n for n in nodes),
                 "element {!r} needs a list of node names".format(name))
        if kind in ("resistor", "capacitor", "inductor"):
            _require(len(nodes) == 2,
                     "element {!r} needs exactly two nodes".format(name))
            _finite("element {!r} value".format(name),
                    element["value"], strict_minimum=0.0)
        if kind == "vsource_dc":
            _require(len(nodes) == 2,
                     "element {!r} needs exactly two nodes".format(name))
            _finite("element {!r} value".format(name), element["value"])
        if kind == "vsource_pulse":
            _require(len(nodes) == 2,
                     "element {!r} needs exactly two nodes".format(name))
            pulse = element["pulse"]
            _require(isinstance(pulse, dict), "pulse must be a dict")
            _exact_keys("pulse of {!r}".format(name), pulse,
                        _PULSE_KEYS)
            _finite("pulse v1", pulse["v1"])
            _finite("pulse v2", pulse["v2"])
            _finite("pulse delay_s", pulse["delay_s"], minimum=0.0)
            _finite("pulse rise_s", pulse["rise_s"],
                    strict_minimum=0.0)
            _finite("pulse fall_s", pulse["fall_s"],
                    strict_minimum=0.0)
            _finite("pulse width_s", pulse["width_s"],
                    strict_minimum=0.0)
            _finite("pulse period_s", pulse["period_s"],
                    strict_minimum=0.0)
            _require(pulse["rise_s"] + pulse["width_s"]
                     + pulse["fall_s"] <= pulse["period_s"],
                     "pulse of {!r}: rise + width + fall must fit "
                     "inside one period".format(name))
        if kind == "model_instance":
            _require(isinstance(element["model"], str)
                     and element["model"],
                     "element {!r} needs a registered model "
                     "identity".format(name))

    analyses = scenario["analyses"]
    _require(isinstance(analyses, list) and analyses,
             "scenario analyses must be a nonempty list")
    kinds = set()
    for analysis in analyses:
        _require(isinstance(analysis, dict), "each analysis is a dict")
        kind = analysis.get("kind")
        _require(kind in _ANALYSIS_KEYS,
                 "analysis kind {!r} is not one of {}".format(
                     kind, sorted(_ANALYSIS_KEYS)))
        _exact_keys("{} analysis".format(kind), analysis,
                    _ANALYSIS_KEYS[kind])
        _require(kind not in kinds,
                 "analysis kind {!r} appears twice".format(kind))
        kinds.add(kind)
        if kind == "tran":
            step = _finite("tran step_s", analysis["step_s"],
                           strict_minimum=0.0)
            stop = _finite("tran stop_s", analysis["stop_s"],
                           strict_minimum=0.0)
            _require(step < stop,
                     "tran step_s must be smaller than stop_s")

    measurements = scenario["measurements"]
    _require(isinstance(measurements, list) and measurements,
             "scenario measurements must be a nonempty list")
    measurement_names = set()
    for measurement in measurements:
        _require(isinstance(measurement, dict),
                 "each measurement is a dict")
        keys = set(measurement)
        _require(keys <= _MEASUREMENT_KEYS,
                 "measurement carries unknown key(s) {}".format(
                     sorted(keys - _MEASUREMENT_KEYS)))
        _require({"name", "kind", "node"} <= keys,
                 "each measurement needs name, kind and node")
        name = measurement["name"]
        _require(isinstance(name, str) and name,
                 "each measurement needs a nonempty name")
        _require(name not in measurement_names,
                 "measurement name {!r} appears twice".format(name))
        measurement_names.add(name)
        kind = measurement["kind"]
        _require(kind in _MEASUREMENT_KINDS,
                 "measurement kind {!r} is not one of {}".format(
                     kind, list(_MEASUREMENT_KINDS)))
        needed = "op" if kind == "op_voltage" else "tran"
        _require(needed in kinds,
                 "measurement {!r} needs the {!r} analysis, which the "
                 "scenario does not declare".format(name, needed))
        _require(isinstance(measurement["node"], str)
                 and measurement["node"],
                 "measurement {!r} needs a node".format(name))
        _require(measurement["node"] != GROUND_NODE,
                 "measurement {!r} measures the reference node, "
                 "which is identically zero; a meaningless "
                 "measurement refuses instead of trivially "
                 "passing".format(name))
        assertion = measurement.get("assertion")
        if assertion is not None:
            _require(isinstance(assertion, dict)
                     and assertion.get("op") in _ASSERTION_KEYS,
                     "assertion op must be one of {}".format(
                         sorted(_ASSERTION_KEYS)))
            _exact_keys("assertion of {!r}".format(name), assertion,
                        _ASSERTION_KEYS[assertion["op"]])
            _finite("assertion value", assertion["value"])
            if assertion["op"] == "within":
                _finite("assertion tolerance", assertion["tolerance"],
                        strict_minimum=0.0)
        knowledge = measurement.get("knowledge")
        if knowledge is not None:
            try:
                claim.validate_knowledge_declaration(knowledge)
            except claim.ClaimError as exc:
                raise SimulationError(str(exc)) from exc
            _require(knowledge["kind"] != claim.INTERVAL,
                     "measurement {!r} declares interval knowledge, but a "
                     "simulator measurement produces one value rather than "
                     "interval endpoints".format(name))

    conditions = scenario.get("operating_conditions")
    if conditions is not None:
        _require(isinstance(conditions, dict), "operating_conditions "
                                               "must be a dict")
        _exact_keys("operating_conditions", conditions,
                    _OPERATING_KEYS)
        _finite("temperature_c", conditions["temperature_c"],
                minimum=-273.15)

    required = scenario.get("required_coverage")
    if required is not None:
        validate_requirement(required)

    assumptions = scenario.get("assumptions")
    if assumptions is not None:
        _require(isinstance(assumptions, dict) and assumptions,
                 "assumptions must be a nonempty dict of "
                 "element name -> assumption record")
        ideal_names = {element["name"]
                       for element in scenario["elements"]
                       if element["kind"] != "model_instance"}
        for name, record in assumptions.items():
            _require(name in ideal_names,
                     "assumption {!r} names no ideal element in "
                     "this scenario; assumptions describe ideal "
                     "primitives, and a registered model's evidence "
                     "is the coverage contract's business".format(
                         name))
            _require(isinstance(record, dict), "each assumption is "
                                               "a dict")
            _exact_keys("assumption {!r}".format(name), record,
                        _ASSUMPTION_KEYS)
            _require(isinstance(record["stands_in_for"], str)
                     and record["stands_in_for"],
                     "assumption {!r} needs a nonempty "
                     "stands_in_for".format(name))
            _require(isinstance(
                record["accepted_for_design_decision"], bool),
                "assumption {!r} accepted_for_design_decision must "
                "be a bool".format(name))
    return scenario


def referenced_models(scenario):
    """Model identities the scenario instantiates, sorted, unique."""
    return sorted({element["model"]
                   for element in scenario["elements"]
                   if element["kind"] == "model_instance"})


def derive_measurement_knowledge(sim_scenario, measurement, registry):
    """Mechanical measurement knowledge for supported monotonic
    templates; None wherever the circuit is not one of them.

    Supported today: the series divider -

        ideal voltage source (A, ground)
        -> ONE two-terminal series element (A, measured)
        -> ONE positive resistive load (measured, ground)

    with nothing else touching either non-ground node. There, the
    measured load voltage is monotonically DECREASING in the
    series resistance, so a lower-bound series R gives an
    upper-bound voltage and an upper-bound series R a lower-bound
    voltage; an exact series R gives an exact value. Anything
    outside the template returns None: an unsupported or
    non-monotonic circuit needs an explicit ASSUMED bound and
    never receives theorem-level provenance."""
    measured = measurement["node"]
    touching_measured = []
    sources = []
    others = []
    for element in sim_scenario["elements"]:
        nodes = element["nodes"]
        if element["kind"] == "vsource_dc":
            sources.append(element)
        elif measured in nodes:
            touching_measured.append(element)
        else:
            others.append(element)
    if len(sources) != 1 or measured in sources[0]["nodes"]:
        return None
    source_node = next((node for node in sources[0]["nodes"]
                        if node != GROUND_NODE), None)
    if source_node is None:
        return None
    if len(touching_measured) != 2 or others:
        return None
    series = load = None
    for element in touching_measured:
        nodes = set(element["nodes"])
        if nodes == {source_node, measured}:
            series = element
        elif nodes == {measured, GROUND_NODE}:
            load = element
    if series is None or load is None:
        return None
    if load["kind"] != "resistor" or load.get("value", 0) <= 0:
        return None
    if series["kind"] == "resistor":
        resistance_knowledge = (claim.EXACT
                                if series.get("value", 0) > 0 else None)
    elif series["kind"] == "model_instance":
        record = registry.get(series["model"])
        derivation = record.get("derivation") or {}
        resistance_claim = derivation.get("resistance_claim")
        if resistance_claim is None:
            return None
        try:
            claim.validate(resistance_claim)
        except claim.ClaimError:
            return None
        resistance_knowledge = resistance_claim["knowledge"]
    else:
        return None
    measurement_knowledge = {
        claim.LOWER_BOUND: claim.UPPER_BOUND,
        claim.UPPER_BOUND: claim.LOWER_BOUND,
        claim.EXACT: claim.EXACT,
    }.get(resistance_knowledge)
    if measurement_knowledge is None:
        return None
    basis = None if measurement_knowledge == claim.EXACT else \
        claim.knowledge_basis(
            claim.DERIVED,
            "series-divider voltage is monotonically decreasing in series "
            "resistance; resistance knowledge is {!r}".format(
                resistance_knowledge))
    return claim.knowledge_declaration(measurement_knowledge, basis)


def measurement_contributors(scenario, measurement):
    """The elements whose behavior can reach one measured node.

    Connectivity closure from the measurement's node through every
    non-reference node: an element touching a reachable node
    contributes, and its other non-reference nodes become reachable.
    This deliberately over-includes (an ideal source does isolate its
    sides; inclusion is the conservative direction - it can only
    demand MORE evidence, never less) and never propagates through
    the reference node, so independent subcircuits stay independent.
    """
    adjacency = {}
    for element in scenario["elements"]:
        for node in element["nodes"]:
            if node != GROUND_NODE:
                adjacency.setdefault(node, []).append(element)
    frontier = [measurement["node"]]
    seen_nodes = set()
    contributors = {}
    while frontier:
        node = frontier.pop()
        if node in seen_nodes:
            continue
        seen_nodes.add(node)
        for element in adjacency.get(node, []):
            if element["name"] in contributors:
                continue
            contributors[element["name"]] = element
            for other in element["nodes"]:
                if other != GROUND_NODE and other not in seen_nodes:
                    frontier.append(other)
    return [contributors[name] for name in sorted(contributors)]


def contributor_coverage_report(registry, scenario):
    """Per-measurement, per-contributor coverage. No blessing.

    The invariant this enforces: every model whose behavior
    contributes to a claimed measurement must INDIVIDUALLY satisfy
    the required evidence policy for each phenomenon it covers, and
    each measurement needs at least one acceptable provider per
    required phenomenon among ITS OWN contributors. Satisfaction is
    therefore never existential over the whole scenario: one strong
    vendor model can neither mask a weak model in the same path nor
    stand in for evidence a different measurement's path lacks.
    Ideal primitive elements (resistors, sources...) are part of the
    declared question, exact by declaration; they are listed so a
    reviewer sees them, and they neither provide nor require
    phenomenon evidence.
    """
    validate_scenario(scenario)
    requirement = scenario.get("required_coverage")
    per_measurement = {}
    satisfied = None if requirement is None else True
    for measurement in scenario["measurements"]:
        contributors = measurement_contributors(scenario, measurement)
        models = {}
        ideal = {}
        for element in contributors:
            if element["kind"] == "model_instance":
                models[element["name"]] = registry.get(
                    element["model"])
            else:
                ideal[element["name"]] = element["kind"]
        entry = {
            "contributing_elements": sorted(
                element["name"] for element in contributors),
            "ideal_elements": dict(sorted(ideal.items())),
            "models": {name: model["identity"]
                       for name, model in sorted(models.items())},
            "per_phenomenon": None,
            "met": None,
        }
        if requirement is not None:
            per_phenomenon = {}
            all_met = True
            for phenomenon, accepted in sorted(requirement.items()):
                providers = {}
                violating = []
                not_applicable = []
                unaccounted = []
                for name, model in sorted(models.items()):
                    facts = {fact["phenomenon"]: fact
                             for fact in model["evidence"]}
                    fact = facts.get(phenomenon)
                    if fact is None:
                        # Silence is not irrelevance: a contributor
                        # that does not account for a required
                        # phenomenon blocks, exactly like one that
                        # covers it badly.
                        unaccounted.append(name)
                    elif fact["applicability"]["status"] == \
                            claim.NOT_APPLICABLE:
                        not_applicable.append(name)
                    elif fact["applicability"]["status"] == claim.UNSUPPORTED:
                        violating.append(name)
                    else:
                        evidence_class = fact["evidence_class"]
                        providers[name] = {
                            "identity": model["identity"],
                            "evidence": fact,
                            "accepted": evidence_class in accepted,
                        }
                        if evidence_class not in accepted:
                            violating.append(name)
                met = bool(providers) and not violating \
                    and not unaccounted
                all_met = all_met and met
                per_phenomenon[phenomenon] = {
                    "accepted_classes": list(accepted),
                    "providers": providers,
                    "violating": violating,
                    "not_applicable": not_applicable,
                    "unaccounted": unaccounted,
                    "met": met,
                    "why": ("every contributing model accounts for "
                            "the phenomenon and every provider is "
                            "individually acceptable" if met else
                            "contributor(s) {} do not account for "
                            "this required phenomenon; silence is "
                            "not irrelevance".format(unaccounted)
                            if unaccounted else
                            "contributor(s) {} cover this phenomenon "
                            "at an unaccepted class or declare it "
                            "unsupported".format(violating)
                            if violating else
                            "no contributing model provides this "
                            "phenomenon at an accepted class"),
                }
            entry["per_phenomenon"] = per_phenomenon
            entry["met"] = all_met
            satisfied = satisfied and all_met
        per_measurement[measurement["name"]] = entry
    return {
        "requirement": requirement,
        "satisfied": satisfied,
        "per_measurement": per_measurement,
        "meaning": "coverage is judged per measurement over its own "
                   "contribution closure: every contributing model "
                   "covering a required phenomenon must be "
                   "individually acceptable, and each measurement "
                   "needs at least one acceptable provider of each "
                   "required phenomenon among its own contributors",
    }


def assumption_dependencies(scenario):
    """Which ideal primitives each measurement depends on, and how
    each is accounted for.

    An ideal element is exact by declaration, but exactness is not
    evidence about the real part it stands in for. Every ideal
    contributor is therefore either DECLARED (the scenario's
    assumptions name it, say what it stands in for, and state
    whether the assumption is accepted for a design decision) or
    UNDECLARED - and an undeclared or unaccepted assumption makes
    the result unusable for the requested decision, structurally.
    A measurement whose shared knowledge basis is ASSUMED is also an
    assumption dependency. The knowledge declaration has no independent
    design-acceptance field, so it remains visible and blocks top-level
    design usability rather than silently borrowing acceptance from an
    ideal element or model.
    """
    validate_scenario(scenario)
    declared = scenario.get("assumptions") or {}
    per_measurement = {}
    measurement_knowledge_assumptions = {}
    all_accepted = True
    any_ideal = False
    for measurement in scenario["measurements"]:
        knowledge = measurement.get("knowledge")
        basis = None if knowledge is None else knowledge["basis"]
        if basis is not None and basis["kind"] == claim.ASSUMED:
            measurement_knowledge_assumptions[measurement["name"]] = {
                "knowledge": knowledge["kind"],
                "detail": basis["detail"],
                "accepted_for_design_decision": False,
            }
            all_accepted = False
        contributors = measurement_contributors(scenario,
                                                measurement)
        entries = {}
        for element in contributors:
            if element["kind"] == "model_instance":
                continue
            any_ideal = True
            record = declared.get(element["name"])
            if record is None:
                entries[element["name"]] = {
                    "kind": element["kind"],
                    "declared": False,
                    "accepted_for_design_decision": False,
                    "detail": "ideal primitive with no declared "
                              "assumption; exact by declaration is "
                              "not evidence about the real part",
                }
                all_accepted = False
            else:
                entries[element["name"]] = {
                    "kind": element["kind"],
                    "declared": True,
                    "stands_in_for": record["stands_in_for"],
                    "accepted_for_design_decision":
                        record["accepted_for_design_decision"],
                }
                if not record["accepted_for_design_decision"]:
                    all_accepted = False
        per_measurement[measurement["name"]] = entries
    return {
        "per_measurement": per_measurement,
        "measurement_knowledge_assumptions":
            measurement_knowledge_assumptions,
        "assumption_dependent":
            any_ideal or bool(measurement_knowledge_assumptions),
        "all_assumptions_accepted_for_design_decision":
            all_accepted,
        "meaning": "results built on ideal primitives are usable "
                   "for a design decision only when every such "
                   "primitive's assumption is declared and "
                   "accepted; usable-under-assumption is never "
                   "evidence about the real source or load. A "
                   "measurement with an ASSUMED numeric basis is "
                   "reported here and blocks design usability",
    }


def condition_coverage(registry, scenario):
    """How each referenced model relates to the requested conditions.

    A condition a simulator applies (the ngspice backend genuinely
    sets the simulator temperature) is NOT thereby covered by every
    model in the scenario: a model fixed at a reference value is only
    valid there, and a model that declares nothing about a condition
    is NOT covered - undeclared never reads as insensitive. Ideal
    scenario-declared elements are exact at their declared values by
    definition and carry no condition dependence, which is stated
    here rather than assumed silently.
    """
    validate_scenario(scenario)
    requested_conditions = scenario.get("operating_conditions")
    if requested_conditions is None:
        return {"conditions": {}, "fully_covered": None,
                "meaning": "no operating conditions were requested; "
                           "nothing is claimed about any"}
    models = {name: registry.get(name)
              for name in referenced_models(scenario)}
    conditions = {}
    fully_covered = True
    for name, requested in sorted(requested_conditions.items()):
        per_model = {}
        for identity, model in sorted(models.items()):
            declared = (model.get("conditions") or {}).get(name)
            if declared is None:
                per_model[identity] = {
                    "kind": "undeclared",
                    "matches_requested": None,
                    "detail": "the model declares nothing about this "
                              "condition; undeclared is not covered "
                              "and never reads as insensitive"}
                fully_covered = False
            elif declared["kind"] == "parameterized":
                low, high = declared["range"]
                inside = low <= requested <= high
                per_model[identity] = {
                    "kind": "parameterized",
                    "range": [low, high],
                    "matches_requested": inside}
                fully_covered = fully_covered and inside
            else:
                matches = declared["value"] == requested
                per_model[identity] = {
                    "kind": "fixed-reference",
                    "reference_value": declared["value"],
                    "matches_requested": matches,
                    "detail": None if matches else
                    "the model is fixed at {} {} and the scenario "
                    "requests {}; the result does NOT represent the "
                    "requested condition for this model".format(
                        declared["value"], declared["units"],
                        requested)}
                fully_covered = fully_covered and matches
        conditions[name] = {"requested": requested,
                            "models": per_model}
    return {
        "conditions": conditions,
        "fully_covered": fully_covered,
        "meaning": "a simulator-applied condition never implies "
                   "model condition coverage; fully_covered is true "
                   "only when every referenced model is parameterized "
                   "over, or fixed exactly at, every requested "
                   "condition (ideal declared elements are exact by "
                   "declaration and are not model claims)",
    }
