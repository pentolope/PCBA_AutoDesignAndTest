"""The ngspice backend: deterministic decks, honest results, and a
real engine wherever one exists.

The result contract keeps its verdicts separate, because conflating
them is how a simulation quietly overstates itself:

  * ``backend`` - whether an ngspice engine exists here at all, which
    one, and its version. Discovery order: the ``NGSPICE_LIBRARY``
    environment variable (an explicit path to the shared library),
    the ``ngspice`` binary on PATH, then a shared library - beside
    the running interpreter, or wherever the dynamic linker says
    ``libngspice`` lives. An absent
    backend yields status ``backend-unavailable``: not a pass, not a
    fabricated failure - policy decides what an optional backend's
    absence means.
  * ``converged`` - whether the simulator itself completed.
  * ``measurements`` - the numerical values and their assertion
    verdicts, meaningful only when the run converged.
  * ``model_coverage`` - the contributor-scoped coverage report:
    which models, on which measurement's contribution closure, at
    which evidence classes. Enforcement is per contributor - one
    strong model never blesses a weak one - and the run refuses
    BEFORE any simulator starts when the requirement is not met.
  * ``condition_coverage`` - how each referenced model relates to the
    requested operating conditions. A simulator-applied temperature
    never implies every model represents that temperature; a model
    fixed at its reference is flagged whenever the request differs.

Decks are generated deterministically (sorted, fixed formatting) and
hashed. The shared-library engine executes the same netlist portion
of the same deck with explicitly derived analysis commands and reads
result vectors from the engine's own plot storage; the deck artifact
and its hash stay identical across both execution modes.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys

from .. import claim as claim_module
from .model_registry import SimulationError
from . import scenario as scenario_module

NGSPICE_BINARY = "ngspice"

_SHARED_LIBRARY_NAMES = ("libngspice.so.0", "libngspice.so")


def _shared_library_candidates():
    override = os.environ.get("NGSPICE_LIBRARY")
    if override:
        yield override
        return
    executable_dir = os.path.dirname(os.path.abspath(sys.executable))
    for name in _SHARED_LIBRARY_NAMES:
        yield os.path.join(executable_dir, name)
    # Then wherever the dynamic linker keeps it. On a distribution install
    # libngspice belongs to its own package under a multiarch directory, so
    # nothing is beside the interpreter and this is the branch that finds it.
    from ctypes.util import find_library
    found = find_library("ngspice")
    if found:
        yield found if os.path.isabs(found) else _resolve_soname(found)


def _resolve_soname(soname):
    """An absolute path for a bare soname, or the soname unchanged.

    ``find_library`` answers with a filename; the candidate loop tests each
    candidate with ``os.path.isfile``, so a bare name would always miss.
    """
    for directory in ("/usr/lib/x86_64-linux-gnu", "/usr/lib", "/usr/local/lib",
                      "/lib/x86_64-linux-gnu"):
        candidate = os.path.join(directory, soname)
        if os.path.isfile(candidate):
            return candidate
    return soname


class _SharedNgspice:
    """One in-process libngspice engine, loaded lazily, reused.

    The library keeps global state, so exactly one instance exists
    per process; each run removes the previous circuit first. Runs
    execute in the foreground with no timeout - callers keep
    scenarios bounded, which the op/tran vocabulary already does.
    """

    _instance = None
    _load_error = None

    @classmethod
    def instance(cls):
        if cls._instance is not None or cls._load_error is not None:
            return cls._instance, cls._load_error
        for candidate in _shared_library_candidates():
            if os.path.isfile(candidate):
                try:
                    cls._instance = cls(candidate)
                except OSError as exc:
                    cls._load_error = (
                        "shared library {} exists but failed to "
                        "load: {}".format(candidate, exc))
                return cls._instance, cls._load_error
        cls._load_error = "no ngspice shared library was found"
        return None, cls._load_error

    def __init__(self, library_path):
        import ctypes
        self._ctypes = ctypes
        self._lib = ctypes.CDLL(library_path)
        self.library_path = library_path
        self._log = []
        self._exited = False

        send_char_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
            ctypes.c_void_p)
        exit_type = ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_int, ctypes.c_bool, ctypes.c_bool,
            ctypes.c_int, ctypes.c_void_p)

        def _collect(line, _identifier, _user):
            if line:
                self._log.append(
                    line.decode("utf-8", errors="replace"))
            return 0

        def _controlled_exit(_status, _immediate, _quit, _identifier,
                             _user):
            self._exited = True
            return 0

        # Kept as attributes: ctypes callbacks must outlive the
        # library that holds pointers to them.
        self._send_char = send_char_type(_collect)
        self._send_stat = send_char_type(lambda *_args: 0)
        self._controlled_exit = exit_type(_controlled_exit)

        init = self._lib.ngSpice_Init
        init.restype = ctypes.c_int
        init.argtypes = [send_char_type, send_char_type, exit_type,
                         ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p]
        init(self._send_char, self._send_stat, self._controlled_exit,
             None, None, None, None)

        self._command = self._lib.ngSpice_Command
        self._command.restype = ctypes.c_int
        self._command.argtypes = [ctypes.c_char_p]

        class _VectorInfo(ctypes.Structure):
            _fields_ = [
                ("v_name", ctypes.c_char_p),
                ("v_type", ctypes.c_int),
                ("v_flags", ctypes.c_short),
                ("v_realdata", ctypes.POINTER(ctypes.c_double)),
                ("v_compdata", ctypes.c_void_p),
                ("v_length", ctypes.c_int),
            ]

        self._vector_info = _VectorInfo
        self._get_vector = self._lib.ngGet_Vec_Info
        self._get_vector.restype = ctypes.POINTER(_VectorInfo)
        self._get_vector.argtypes = [ctypes.c_char_p]

        self._circ = self._lib.ngSpice_Circ
        self._circ.restype = ctypes.c_int

        self._log = []
        self._command(b"version -s")
        joined = "\n".join(self._log)
        match = re.search(r"ngspice-[0-9][^\s,)]*", joined)
        self.version = match.group(0) if match else "unknown"

    def run(self, netlist_lines, commands):
        """Load one circuit, run the given commands, return the log.

        Raises when the engine reported a controlled exit earlier -
        a dead engine is reported, never silently reused.
        """
        ctypes = self._ctypes
        if self._exited:
            raise SimulationError(
                "the in-process ngspice engine has exited; it cannot "
                "be reused within this process")
        self._log = []
        self._command(b"remcirc")
        self._log = []
        encoded = [line.encode("utf-8") for line in netlist_lines]
        array_type = ctypes.c_char_p * (len(encoded) + 1)
        array = array_type(*encoded, None)
        load_status = self._circ(array)
        command_status = {}
        values = {}
        if load_status == 0:
            for command, vector_names in commands:
                command_status[command] = self._command(
                    command.encode("utf-8"))
                if command_status[command] != 0:
                    continue
                # Vectors are read immediately after their own
                # analysis: each command replaces the current plot,
                # so a later analysis must never answer for an
                # earlier one's measurements.
                for name, reduction in vector_names:
                    values[(name, reduction)] = self.real_value(
                        name, reduction)
        return {"load_status": load_status,
                "command_status": command_status,
                "values": values,
                "log": list(self._log)}

    def real_value(self, vector_name, reduction):
        """One reduction of a result vector's real data, or None."""
        pointer = self._get_vector(vector_name.encode("utf-8"))
        if not pointer:
            return None
        vector = pointer.contents
        if vector.v_length <= 0 or not vector.v_realdata:
            return None
        if reduction == "last":
            return float(vector.v_realdata[vector.v_length - 1])
        return _reduce([float(vector.v_realdata[index])
                        for index in range(vector.v_length)], reduction)

    def last_real_value(self, vector_name):
        """The final real value of one result vector, or None."""
        return self.real_value(vector_name, "last")


def _version_from_banner(text):
    """The version line out of `ngspice --version`.

    ngspice opens its banner with a rule of asterisks, so taking the first
    line verbatim recorded "******" as the engine's identity - a provenance
    field that names nothing. The first line actually mentioning ngspice is
    the one carrying the version; the leading "** " decoration is stripped.
    """
    for line in text.strip().splitlines():
        stripped = line.strip().lstrip("*").strip()
        if "ngspice" in stripped.lower():
            return stripped
    # A banner that names no ngspice identifies nothing. Returning its first
    # line would put a plausible-looking string in a provenance field - the
    # one place a guess is worse than an admission.
    return "unknown"


def backend_identity():
    """Discover an ngspice engine: what exists, where, which version."""
    import shutil
    override = os.environ.get("NGSPICE_LIBRARY")
    if not override:
        binary = shutil.which(NGSPICE_BINARY)
        if binary is not None:
            try:
                probe = subprocess.run(
                    [binary, "--version"], capture_output=True,
                    text=True, timeout=30)
                version = _version_from_banner(
                    (probe.stdout or probe.stderr or ""))
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"name": "ngspice", "available": False,
                        "mode": "binary", "path": binary,
                        "version": None,
                        "detail": "ngspice exists but did not answer "
                                  "--version: {}".format(exc)}
            return {"name": "ngspice", "available": True,
                    "mode": "binary", "path": binary,
                    "version": version, "detail": "discovered on PATH"}
    engine, load_error = _SharedNgspice.instance()
    if engine is not None:
        return {"name": "ngspice", "available": True,
                "mode": "shared-library",
                "path": engine.library_path,
                "version": engine.version,
                "detail": "in-process shared-library engine"}
    return {"name": "ngspice", "available": False, "mode": None,
            "path": None, "version": None,
            "detail": "no ngspice binary on PATH and {}".format(
                load_error)}


def _format_value(value):
    return repr(float(value))


def generate_deck(registry, sim_scenario):
    """A deterministic ngspice batch deck for one validated scenario.

    Element cards appear in the scenario's declared order (the order
    is part of the declaration); measurement outputs are written with
    wrdata to fixed filenames; models are inlined as subcircuits from
    the registry. Refuses on any unregistered model.
    """
    scenario_module.validate_scenario(sim_scenario)
    lines = ["* scenario: {}".format(sim_scenario["name"]),
             "* deterministic deck generated by pcbqa.sim.ngspice"]
    model_names = scenario_module.referenced_models(sim_scenario)
    for name in model_names:
        model = registry.get(name)
        spice = model.get("spice")
        if not spice:
            raise SimulationError(
                "model {!r} carries no spice text; it cannot be "
                "instantiated by this backend".format(name))
        facts = sorted(model["evidence"], key=lambda fact: fact["phenomenon"])
        lines.append("* model {} evidence={} source={}".format(
            name, ",".join("{}:{}".format(
                fact["phenomenon"], fact["evidence_class"] or
                fact["applicability"]["status"]) for fact in facts),
            ",".join(sorted({str(fact["provenance"]["source"])
                             for fact in facts}))))
        lines.extend(line.rstrip() for line in spice.splitlines())
    for element in sim_scenario["elements"]:
        kind = element["kind"]
        nodes = " ".join(element["nodes"])
        if kind == "resistor":
            lines.append("R{} {} {}".format(
                element["name"], nodes,
                _format_value(element["value"])))
        elif kind == "capacitor":
            lines.append("C{} {} {}".format(
                element["name"], nodes,
                _format_value(element["value"])))
        elif kind == "inductor":
            lines.append("L{} {} {}".format(
                element["name"], nodes,
                _format_value(element["value"])))
        elif kind == "vsource_dc":
            lines.append("V{} {} DC {}".format(
                element["name"], nodes,
                _format_value(element["value"])))
        elif kind == "vsource_pulse":
            pulse = element["pulse"]
            lines.append(
                "V{} {} PULSE({} {} {} {} {} {} {})".format(
                    element["name"], nodes,
                    _format_value(pulse["v1"]),
                    _format_value(pulse["v2"]),
                    _format_value(pulse["delay_s"]),
                    _format_value(pulse["rise_s"]),
                    _format_value(pulse["fall_s"]),
                    _format_value(pulse["width_s"]),
                    _format_value(pulse["period_s"])))
        else:  # model_instance
            lines.append("X{} {} {}".format(
                element["name"], nodes, element["model"]))
    conditions = sim_scenario.get("operating_conditions")
    if conditions is not None:
        # The declared condition genuinely reaches the simulator: this
        # is what licenses the scenario contract to accept it at all.
        # Whether each MODEL represents that condition is a separate
        # question, answered by condition_coverage - never here.
        lines.append(".options temp={}".format(
            _format_value(conditions["temperature_c"])))
    lines.append(".control")
    lines.append("set filetype=ascii")
    for command in _analysis_commands(sim_scenario):
        lines.append(command)
        prefix = "op" if command == "op" else "tran"
        for measurement in _measurements_of(sim_scenario, command):
            lines.append("wrdata {}_{}.data v({})".format(
                prefix, measurement["name"], measurement["node"]))
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")
    return "\n".join(lines) + "\n"


def _measurements_of(sim_scenario, command):
    """The measurements one analysis command answers for.

    A transient carries several reductions of the same run, so the
    mapping is by analysis family rather than by a single kind: an
    excursion and an endpoint are two questions about one waveform.
    """
    family = "op" if command == "op" else "tran"
    return [measurement for measurement in sim_scenario["measurements"]
            if _family_of(measurement["kind"]) == family]


def _family_of(kind):
    return "op" if kind == "op_voltage" else "tran"


def _reduction_of(measurement):
    return scenario_module.MEASUREMENT_REDUCTIONS[measurement["kind"]]


def _analysis_commands(sim_scenario):
    """The interactive commands one scenario's analyses map to."""
    commands = []
    for analysis in sim_scenario["analyses"]:
        if analysis["kind"] == "op":
            commands.append("op")
        else:
            commands.append("tran {} {}".format(
                _format_value(analysis["step_s"]),
                _format_value(analysis["stop_s"])))
    return commands


def _netlist_lines(deck):
    """The netlist portion of a deck: everything before .control,
    terminated with .end - what the in-process engine loads."""
    lines = []
    for line in deck.splitlines():
        if line == ".control":
            break
        lines.append(line)
    lines.append(".end")
    return lines


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_column_value(path, reduction):
    values = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) >= 2:
                values.append(float(parts[-1]))
    if not values:
        raise SimulationError(
            "wrdata output {} carried no data rows".format(
                os.path.basename(path)))
    return _reduce(values, reduction)


def _reduce(values, reduction):
    if reduction == "last":
        return values[-1]
    if reduction == "min":
        return min(values)
    if reduction == "max":
        return max(values)
    raise SimulationError(
        "unknown measurement reduction {!r}".format(reduction))


def _read_last_column_value(path):
    return _read_column_value(path, "last")


def _assemble_measurements(sim_scenario, value_of):
    """Shared measurement claims from a value lookup; missing values refuse."""
    measurements = {}
    for measurement in sim_scenario["measurements"]:
        value = value_of(measurement)
        if value is None:
            raise SimulationError(
                "the engine produced no result vector for "
                "measurement {!r}; a missing result is a failure, "
                "never a default".format(measurement["name"]))
        assertion = measurement.get("assertion")
        declared = measurement.get("knowledge") or \
            claim_module.knowledge_declaration(claim_module.EXACT)
        knowledge = declared["kind"]
        quantity = ({} if knowledge == claim_module.UNKNOWN else
                    {"value": value})
        basis = declared["basis"]
        assumptions = ([basis["detail"]]
                       if basis and basis["kind"] == claim_module.ASSUMED
                       else [])
        omissions = ([{"detail": basis["detail"]}]
                     if basis and knowledge in (
                         claim_module.LOWER_BOUND,
                         claim_module.UPPER_BOUND,
                         claim_module.APPROXIMATE) else [])
        required = (None if assertion is None else claim_module.requirement(
            "scenario measurement " + measurement["name"],
            "scenario declaration", assertion))
        numeric_claim = claim_module.claim(
            "measurement", measurement["name"], "V", knowledge, quantity,
            claim_module.evidence(
                "node_voltage", "circuit-simulation",
                {"source": "ngspice execution",
                 "measurement_kind": measurement["kind"],
                 "node": measurement["node"]},
                assumptions=assumptions,
                omitted_contributions=omissions),
            "node voltage under the scenario's model evidence, conditions "
            "and declared ideal assumptions",
            knowledge_basis=basis, requirement=required)
        measurements[measurement["name"]] = {
            "claim": numeric_claim,
            "verdict": claim_module.verdict(numeric_claim),
        }
    return measurements


def run_scenario(registry, sim_scenario, workdir):
    """Run one scenario. Every outcome is explicit; nothing is faked."""
    scenario_module.validate_scenario(sim_scenario)
    coverage = scenario_module.contributor_coverage_report(
        registry, sim_scenario)
    if coverage["requirement"] is not None \
            and not coverage["satisfied"]:
        unmet = sorted(
            name for name, entry in
            coverage["per_measurement"].items() if not entry["met"])
        raise SimulationError(
            "scenario coverage is not satisfied for measurement(s) "
            "{}: every model contributing to a measurement must "
            "individually satisfy the required evidence policy, and "
            "the run refuses before any simulator starts".format(
                unmet))
    conditions = scenario_module.condition_coverage(registry,
                                                    sim_scenario)
    assumptions = scenario_module.assumption_dependencies(
        sim_scenario)
    _refuse_undeclared_knowledge(registry, sim_scenario)
    _verify_derived_knowledge(registry, sim_scenario)
    deck = generate_deck(registry, sim_scenario)
    backend = backend_identity()
    result = {
        "scenario": sim_scenario["name"],
        "backend": backend,
        "deck_sha256": _sha256_text(deck),
        "model_coverage": coverage,
        "operating_conditions_applied":
            sim_scenario.get("operating_conditions"),
        "condition_coverage": conditions,
        "assumption_dependencies": assumptions,
        "significance": {
            "release_grade": False,
            "meaning": "a numerical result under exactly the stated "
                       "model coverage and condition coverage; "
                       "convergence and assertion verdicts never "
                       "imply stronger evidence than those blocks "
                       "record, and a simulator-applied operating "
                       "condition is not model condition coverage",
        },
    }
    if not backend["available"]:
        result.update({"status": "backend-unavailable",
                       "converged": None, "measurements": None,
                       "unsupported": []})
        return _attach_result_policy(result, coverage)
    os.makedirs(workdir, exist_ok=True)
    deck_path = os.path.join(workdir, "deck.cir")
    with open(deck_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(deck)
    if backend["mode"] == "binary":
        result = _run_with_binary(backend, sim_scenario, workdir,
                                  deck_path, result)
    else:
        result = _run_with_shared_library(sim_scenario, deck,
                                          workdir, result)
    return _attach_result_policy(result, coverage)


def _verify_derived_knowledge(registry, sim_scenario):
    """Derived knowledge is a theorem claim: the
    deriver must reproduce it mechanically right now, or the run
    refuses. An unsupported circuit cannot smuggle theorem-level
    provenance through prose - it declares 'assumed' instead."""
    for measurement in sim_scenario["measurements"]:
        declared = measurement.get("knowledge")
        if declared is None or declared["basis"] is None or \
                declared["basis"]["kind"] != claim_module.DERIVED:
            continue
        derived = scenario_module.derive_measurement_knowledge(
            sim_scenario, measurement, registry)
        if derived is None:
            raise SimulationError(
                "measurement {!r} declares DERIVED knowledge "
                "but the circuit matches no supported monotonic "
                "template; declare an assumed knowledge basis or restructure "
                "the scenario - "
                "unsupported circuits never receive theorem-level "
                "provenance".format(measurement["name"]))
        if derived["kind"] != declared["kind"]:
            raise SimulationError(
                "measurement {!r} declares DERIVED knowledge "
                "kind {!r} but the mechanical derivation "
                "gives {!r}; a wrong theorem claim refuses rather "
                "than classifying".format(
                    measurement["name"], declared["kind"],
                    derived["kind"]))


def _refuse_undeclared_knowledge(registry, sim_scenario):
    """A model that declares its value a BOUND poisons every
    assertion downstream of it: without declared measurement knowledge the
    verdict would default to the STRONGEST claim (exact). Silence
    therefore refuses before any simulator runs - the scenario
    author must translate the model's knowledge into the measurement's
    direction, or drop the assertion."""
    for measurement in sim_scenario["measurements"]:
        if measurement.get("assertion") is None:
            continue
        if measurement.get("knowledge") is not None:
            continue
        contributors = scenario_module.measurement_contributors(
            sim_scenario, measurement)
        for element in contributors:
            if element.get("kind") != "model_instance":
                continue
            record = registry.get(element["model"])
            resistance_claim = (record.get("derivation") or {}).get(
                "resistance_claim")
            if resistance_claim is not None and \
                    resistance_claim["knowledge"] != claim_module.EXACT:
                raise SimulationError(
                    "measurement {!r} asserts on a value fed by "
                    "model {!r}, whose record declares "
                    "non-exact resistance knowledge {!r}; declare the "
                    "measurement's knowledge (its direction "
                    "follows from the circuit) or remove the "
                    "assertion - silence never defaults to an "
                    "exact claim".format(
                        measurement["name"], element["model"],
                        resistance_claim["knowledge"]))


def _attach_result_policy(result, coverage):
    """One structured answer to "may an agent act on this?".

    An autonomous consumer must never weigh measurement.passed
    against condition_coverage.fully_covered by intuition. The
    policy separates: whether the numbers passed their assertions,
    whether the numbers represent the REQUESTED conditions, whether
    the result is usable for a design decision (ran + declared
    coverage requirement satisfied + conditions applicable - a
    numerically passing but condition-inapplicable result is NOT),
    and release usability, which is unconditionally false at this
    layer.
    """
    ran = result.get("status") == "ran"
    claimable = None
    if ran:
        asserted = [measurement for measurement
                    in result["measurements"].values()
                    if measurement["claim"]["requirement"] is not None]
        # A run with NO declared assertions has nothing to claim:
        # None, never a vacuous True - absence of assertions must
        # not read as more confident than an unresolved verdict.
        claimable = None if not asserted else all(
            measurement["verdict"]["result"] == claim_module.PASS
            for measurement in asserted)
    fully_covered = result["condition_coverage"]["fully_covered"]
    applicable = None if fully_covered is None else fully_covered
    assumptions = result["assumption_dependencies"]
    assumptions_ok = assumptions[
        "all_assumptions_accepted_for_design_decision"]
    result["result_policy"] = {
        "assertions_claimable": claimable,
        "result_applicable_to_requested_conditions": applicable,
        "assumption_dependent":
            assumptions["assumption_dependent"],
        "measurement_knowledge_assumptions":
            assumptions["measurement_knowledge_assumptions"],
        "assumptions_accepted_for_design_decision": assumptions_ok,
        "usable_for_design_decision": bool(
            ran and coverage["satisfied"] is True
            and applicable is not False
            and assumptions_ok),
        "usable_for_release": False,
        "meaning": "usable_for_design_decision requires a run under "
                   "a declared and satisfied coverage requirement, "
                   "models representing the requested operating "
                   "conditions, and every contributing ideal "
                   "primitive declared and accepted as an assumption, and "
                   "no measurement relying on an ASSUMED numeric basis; a "
                   "shared claim verdict never overrides "
                   "any of those. assertions_claimable is the "
                   "actionable assertion truth: True only when "
                   "every declared assertion's VERDICT is a PASS "
                   "class, None when nothing was asserted (absence "
                   "of assertions is never a claim)",
    }
    return result


def _run_with_binary(backend, sim_scenario, workdir, deck_path,
                     result):
    run = subprocess.run(
        [backend["path"], "-b", deck_path], capture_output=True,
        text=True, timeout=600, cwd=workdir)
    log = (run.stdout or "") + "\n" + (run.stderr or "")
    result["raw_log_sha256"] = _sha256_text(log)
    converged = run.returncode == 0 and "error" not in log.lower()
    result["converged"] = converged
    if not converged:
        result.update({"status": "simulation-failed",
                       "measurements": None,
                       "unsupported": [],
                       "failure_log_tail": log[-2000:]})
        return result

    def value_of(measurement):
        data_path = os.path.join(
            workdir, "{}_{}.data".format(_family_of(measurement["kind"]),
                                         measurement["name"]))
        return _read_column_value(data_path, _reduction_of(measurement))

    result.update({"status": "ran",
                   "measurements": _assemble_measurements(
                       sim_scenario, value_of),
                   "unsupported": []})
    return result


def _run_with_shared_library(sim_scenario, deck, workdir, result):
    engine, load_error = _SharedNgspice.instance()
    if engine is None:
        raise SimulationError(
            "the shared-library engine disappeared between discovery "
            "and execution: {}".format(load_error))
    plan = []
    for command in _analysis_commands(sim_scenario):
        plan.append((command, sorted({
            (measurement["node"].lower(), _reduction_of(measurement))
            for measurement in _measurements_of(sim_scenario, command)})))
    previous_directory = os.getcwd()
    os.chdir(workdir)
    try:
        outcome = engine.run(_netlist_lines(deck), plan)
    finally:
        os.chdir(previous_directory)
    log = "\n".join(outcome["log"])
    result["raw_log_sha256"] = _sha256_text(log)
    result["execution"] = {
        "mode": "shared-library",
        "commands": [command for command, _vectors in plan],
        "load_status": outcome["load_status"],
        "command_status": outcome["command_status"],
    }
    failed = (outcome["load_status"] != 0
              or any(status != 0 for status in
                     outcome["command_status"].values())
              or "error" in log.lower())
    result["converged"] = not failed
    if failed:
        result.update({"status": "simulation-failed",
                       "measurements": None,
                       "unsupported": [],
                       "failure_log_tail": log[-2000:]})
        return result

    def value_of(measurement):
        return outcome["values"].get(
            (measurement["node"].lower(), _reduction_of(measurement)))

    result.update({"status": "ran",
                   "measurements": _assemble_measurements(
                       sim_scenario, value_of),
                   "unsupported": []})
    return result
