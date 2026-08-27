"""Simulation scenarios: one declared experiment, strictly validated.

A scenario is the caller's complete statement of what to simulate and
what would count as passing. It deliberately separates the things a
result must keep separate: the circuit and stimulus (what runs), the
analyses (how it runs), the measurements and assertions (what is
checked numerically), the model substitutions (what evidence stands in
where), and the required fidelity (the weakest evidence class the
caller accepts). Unknown keys refuse everywhere - a misspelled input
silently ignored is an input never applied.
"""

from __future__ import annotations

from .fidelity import SimulationError, rank

_KNOWN_SCENARIO_KEYS = {
    "name", "description", "elements", "analyses", "measurements",
    "operating_conditions", "substitutions", "required_fidelity",
}
_REQUIRED_SCENARIO_KEYS = {"name", "elements", "analyses",
                           "measurements"}

#: Primitive cards the deterministic deck generator understands.
#: Anything else must come through a registered model's subcircuit.
_ELEMENT_KINDS = ("resistor", "capacitor", "inductor",
                  "vsource_dc", "vsource_pulse", "model_instance")

_ANALYSIS_KINDS = ("op", "tran")

_MEASUREMENT_KINDS = ("op_voltage", "tran_final_voltage")

_ASSERTION_OPS = ("<=", ">=", "within")


def _require(condition, message):
    if not condition:
        raise SimulationError(message)


def validate_scenario(scenario):
    """Validate one scenario dict, strictly and completely."""
    _require(isinstance(scenario, dict), "a scenario must be a dict")
    unknown = sorted(set(scenario) - _KNOWN_SCENARIO_KEYS)
    _require(not unknown,
             "scenario carries unknown key(s) {}".format(unknown))
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
        _require(kind in _ELEMENT_KINDS,
                 "element kind {!r} is not one of {}".format(
                     kind, list(_ELEMENT_KINDS)))
        name = element.get("name")
        _require(isinstance(name, str) and name,
                 "each element needs a nonempty name")
        _require(name not in names,
                 "element name {!r} appears twice".format(name))
        names.add(name)
        nodes = element.get("nodes")
        _require(isinstance(nodes, list) and
                 all(isinstance(n, str) and n for n in nodes),
                 "element {!r} needs a list of node names".format(name))
        if kind in ("resistor", "capacitor", "inductor", "vsource_dc"):
            _require(len(nodes) == 2,
                     "element {!r} needs exactly two nodes".format(name))
            value = element.get("value")
            _require(isinstance(value, (int, float))
                     and not isinstance(value, bool)
                     and value == value
                     and value not in (float("inf"), float("-inf")),
                     "element {!r} needs a finite numeric "
                     "value".format(name))
        if kind == "vsource_pulse":
            _require(len(nodes) == 2,
                     "element {!r} needs exactly two nodes".format(name))
            pulse = element.get("pulse")
            _require(isinstance(pulse, dict) and
                     set(pulse) == {"v1", "v2", "delay_s", "rise_s",
                                    "fall_s", "width_s", "period_s"},
                     "element {!r} needs a complete pulse "
                     "spec".format(name))
        if kind == "model_instance":
            _require(isinstance(element.get("model"), str)
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
        _require(kind in _ANALYSIS_KINDS,
                 "analysis kind {!r} is not one of {}".format(
                     kind, list(_ANALYSIS_KINDS)))
        kinds.add(kind)
        if kind == "tran":
            _require(isinstance(analysis.get("step_s"), (int, float))
                     and isinstance(analysis.get("stop_s"),
                                    (int, float)),
                     "tran analysis needs numeric step_s and stop_s")

    for measurement in scenario["measurements"]:
        _require(isinstance(measurement, dict),
                 "each measurement is a dict")
        _require(isinstance(measurement.get("name"), str)
                 and measurement["name"],
                 "each measurement needs a name")
        kind = measurement.get("kind")
        _require(kind in _MEASUREMENT_KINDS,
                 "measurement kind {!r} is not one of {}".format(
                     kind, list(_MEASUREMENT_KINDS)))
        needed = "op" if kind == "op_voltage" else "tran"
        _require(needed in kinds,
                 "measurement {!r} needs the {!r} analysis, which the "
                 "scenario does not declare".format(
                     measurement["name"], needed))
        _require(isinstance(measurement.get("node"), str)
                 and measurement["node"],
                 "measurement {!r} needs a node".format(
                     measurement["name"]))
        assertion = measurement.get("assertion")
        if assertion is not None:
            _require(isinstance(assertion, dict)
                     and assertion.get("op") in _ASSERTION_OPS,
                     "assertion op must be one of {}".format(
                         list(_ASSERTION_OPS)))
            if assertion["op"] == "within":
                _require(isinstance(assertion.get("value"),
                                    (int, float))
                         and isinstance(assertion.get("tolerance"),
                                        (int, float)),
                         "a within assertion needs value and "
                         "tolerance")
            else:
                _require(isinstance(assertion.get("value"),
                                    (int, float)),
                         "an inequality assertion needs a value")

    required = scenario.get("required_fidelity")
    if required is not None:
        rank(required)
    return scenario


def referenced_models(scenario):
    """Model identities the scenario instantiates, sorted, unique."""
    return sorted({element["model"]
                   for element in scenario["elements"]
                   if element["kind"] == "model_instance"})


def check_assertion(assertion, value):
    """Evaluate one assertion against a measured value."""
    if assertion is None:
        return None
    if assertion["op"] == "<=":
        return value <= assertion["value"]
    if assertion["op"] == ">=":
        return value >= assertion["value"]
    return abs(value - assertion["value"]) <= assertion["tolerance"]
