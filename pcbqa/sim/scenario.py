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

from .fidelity import SimulationError, validate_requirement

_KNOWN_SCENARIO_KEYS = {
    "name", "description", "elements", "analyses", "measurements",
    "operating_conditions", "required_coverage",
}
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

_MEASUREMENT_KEYS = {"name", "kind", "node", "assertion"}
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
