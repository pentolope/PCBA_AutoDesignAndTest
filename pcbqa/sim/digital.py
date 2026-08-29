"""Board-level digital contracts: separate stimulus, DUT and checker.

Repository ownership, stated once and bindingly:

  * The PCBA repository owns the board's interface contract: the
    CHECKER module (SystemVerilog assertions stating what the board
    expects) and the behavioral reference DUT.
  * A firmware repository owns production RTL and may consume the
    PCBA repository as a submodule; it substitutes its RTL as the DUT
    while the checker - the contract - stays exactly the PCBA's. One
    fingerprinted contract, never two independently edited copies.
  * MCUs/SoCs are represented by boundary/protocol behavioral models,
    never transistor-level simulation.

The architecture keeps three interchangeable-by-role pieces:

    stimulus/harness  - generated here from the contract's DECLARED
                        clocking (no hidden port-name assumptions);
    DUT               - behavioral board model OR production RTL,
                        selected per run;
    checker           - PCBA-owned assertions, identical for every DUT.

A generated SystemVerilog wrapper instantiates DUT and checker side by
side on the same declared ports; a generated C++ main (built with
Verilator's --cc --exe --build flow, which expects exactly one
caller-supplied main) drives the declared clock and reset. The
eventual invariant this serves: production RTL satisfies the
behavioral contract the board was designed against, under the same
assertions.

Statuses mirror the SPICE backend: an absent Verilator is
``backend-unavailable`` (neither a pass nor a fabricated failure), a
present one that cannot build is ``build-failed``, a fired assertion
is ``assertions-failed``, and only a clean run is ``ran``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

from .fidelity import SimulationError

VERILATOR_BINARY = "verilator"


def _forward(path):
    """Forward-slash form of a path: every toolchain in this flow
    (Verilator, make, g++) accepts it on every platform, while
    backslashes break generated makefiles under an MSYS2-hosted
    toolchain."""
    return path.replace(os.sep, "/")

_REQUIRED_CONTRACT_KEYS = {"name", "checker_module", "checker_sources",
                           "ports", "clocking", "assertion_summary"}
_KNOWN_CONTRACT_KEYS = _REQUIRED_CONTRACT_KEYS | {"description"}

_REQUIRED_CLOCKING_KEYS = {"clock_port", "reset_port",
                           "reset_active_high", "reset_cycles"}


def backend_identity():
    """Discover Verilator: availability, path and version string.

    Discovery order: the ``VERILATOR`` environment variable (an
    explicit path to the executable or to a launcher shim - how a
    toolchain hosted outside the native PATH, such as an MSYS2
    install, is bound in), then ``verilator`` on PATH. An override
    that points at nothing is reported as unavailable with the
    reason, never silently ignored.
    """
    import shutil
    override = os.environ.get("VERILATOR")
    if override:
        if not os.path.isfile(override):
            return {"name": "verilator", "available": False,
                    "path": override, "version": None,
                    "detail": "the VERILATOR environment override "
                              "points to no file"}
        path = override
    else:
        path = shutil.which(VERILATOR_BINARY)
    if path is None:
        return {"name": "verilator", "available": False, "path": None,
                "version": None,
                "detail": "verilator binary not found on PATH and no "
                          "VERILATOR override is set"}
    try:
        probe = subprocess.run(
            [path, "--version"], capture_output=True, text=True,
            timeout=30)
        version = (probe.stdout or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"name": "verilator", "available": False, "path": path,
                "version": None,
                "detail": "verilator exists but did not answer "
                          "--version: {}".format(exc)}
    return {"name": "verilator", "available": True, "path": path,
            "version": version, "detail": "discovered on PATH"}


def _fingerprint_sources(sources, base_directory, role):
    if not isinstance(sources, list) or not sources:
        raise SimulationError(
            "{} needs a nonempty list of SystemVerilog "
            "sources".format(role))
    fingerprints = []
    for source in sources:
        path = os.path.join(base_directory, source)
        if not os.path.isfile(path):
            raise SimulationError(
                "{} source {!r} does not exist; a contract whose "
                "model is absent proves nothing".format(role, source))
        with open(path, "rb") as handle:
            digest = hashlib.sha256(handle.read()).hexdigest()
        fingerprints.append({"source": source, "sha256": digest})
    return fingerprints


def validate_contract(contract, base_directory):
    """The PCBA-owned contract: checker, shared ports, clocking.

    The clocking block DECLARES the stimulus interface - clock port,
    reset port, reset polarity, reset cycles - so nothing about
    stimulus is assumed from port names. Ports are the shared
    interface every DUT and the checker present; each entry is
    {name, dir(in|out), width}.
    """
    if not isinstance(contract, dict):
        raise SimulationError("a digital contract must be a dict")
    unknown = sorted(set(contract) - _KNOWN_CONTRACT_KEYS)
    if unknown:
        raise SimulationError(
            "digital contract carries unknown key(s) {}".format(unknown))
    missing = sorted(_REQUIRED_CONTRACT_KEYS - set(contract))
    if missing:
        raise SimulationError(
            "digital contract is missing key(s) {}".format(missing))
    clocking = contract["clocking"]
    if not isinstance(clocking, dict) or \
            set(clocking) != _REQUIRED_CLOCKING_KEYS:
        raise SimulationError(
            "clocking must declare exactly {}".format(
                sorted(_REQUIRED_CLOCKING_KEYS)))
    if not isinstance(clocking["reset_active_high"], bool):
        raise SimulationError("reset_active_high must be a bool")
    if isinstance(clocking["reset_cycles"], bool) or \
            not isinstance(clocking["reset_cycles"], int) or \
            clocking["reset_cycles"] < 1:
        raise SimulationError("reset_cycles must be a positive int")
    ports = contract["ports"]
    if not isinstance(ports, list) or not ports:
        raise SimulationError("ports must be a nonempty list")
    for port in ports:
        if not isinstance(port, dict) or \
                set(port) != {"name", "dir", "width"}:
            raise SimulationError(
                "each port declares exactly name, dir and width")
        if port["dir"] not in ("in", "out"):
            raise SimulationError(
                "port {!r} dir must be in or out".format(port["name"]))
        if isinstance(port["width"], bool) or \
                not isinstance(port["width"], int) or port["width"] < 1:
            raise SimulationError(
                "port {!r} width must be a positive int".format(
                    port["name"]))
    return {"name": contract["name"],
            "checker_module": contract["checker_module"],
            "checker_sources": _fingerprint_sources(
                contract["checker_sources"], base_directory,
                "checker"),
            "ports": contract["ports"],
            "clocking": dict(clocking),
            "assertion_summary": contract["assertion_summary"]}


def generate_wrapper(contract, dut_module):
    """Deterministic SV wrapper: DUT and checker on the same ports."""
    clocking = contract["clocking"]
    lines = [
        "// deterministic wrapper generated by pcbqa.sim.digital",
        "// contract {} with DUT {}".format(contract["name"],
                                            dut_module),
        "module contract_top (",
        "    input  logic {},".format(clocking["clock_port"]),
        "    input  logic {}".format(clocking["reset_port"]),
        ");",
    ]
    for port in contract["ports"]:
        lines.append("    logic [{}:0] {};".format(
            port["width"] - 1, port["name"]))
    connections = ["        .{0}({0})".format(clocking["clock_port"]),
                   "        .{0}({0})".format(clocking["reset_port"])]
    connections += ["        .{0}({0})".format(port["name"])
                    for port in contract["ports"]]
    joined = ",\n".join(connections)
    lines.append("    {} dut (\n{}\n    );".format(dut_module, joined))
    lines.append("    {} checker_instance (\n{}\n    );".format(
        contract["checker_module"], joined))
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def generate_main(contract, cycles):
    """Deterministic C++ main for the --cc --exe --build flow.

    Drives exactly the DECLARED clock and reset ports at the declared
    polarity for the declared reset duration; nothing about the
    interface is assumed.
    """
    clocking = contract["clocking"]
    asserted = "1" if clocking["reset_active_high"] else "0"
    released = "0" if clocking["reset_active_high"] else "1"
    lines = [
        "// deterministic Verilator main generated by pcbqa.sim.digital",
        "#include \"Vcontract_top.h\"",
        "#include \"verilated.h\"",
        "// legacy time hook: models that reference simulation time",
        "// outside a VerilatedContext resolve it here",
        "double sc_time_stamp() { return 0; }",
        "int main(int argc, char** argv) {",
        "    VerilatedContext context;",
        "    context.commandArgs(argc, argv);",
        "    Vcontract_top top{&context};",
        "    top.{} = {};".format(clocking["reset_port"], asserted),
        "    top.{} = 0;".format(clocking["clock_port"]),
        "    top.eval();",
        "    for (int cycle = 0; cycle < {}; ++cycle) {{".format(
            int(cycles)),
        "        if (cycle == {}) top.{} = {};".format(
            int(clocking["reset_cycles"]), clocking["reset_port"],
            released),
        "        top.{} = 1; context.timeInc(1); top.eval();".format(
            clocking["clock_port"]),
        "        top.{} = 0; context.timeInc(1); top.eval();".format(
            clocking["clock_port"]),
        "        if (context.gotFinish() || context.gotError())",
        "            break;",
        "    }",
        "    top.final();",
        "    if (context.gotError()) return 1;",
        "    return 0;",
        "}",
    ]
    return "\n".join(lines) + "\n"


def run_contract(contract_record, dut, base_directory, workdir,
                 cycles=64):
    """Run one DUT against the PCBA-owned checker under Verilator.

    `dut` declares the substitutable half: {"module": name,
    "sources": [paths]} - the behavioral board model or production
    RTL. The checker travels with the contract and is identical for
    every DUT, which is the whole point.
    """
    if not isinstance(dut, dict) or set(dut) != {"module", "sources"}:
        raise SimulationError(
            "dut must declare exactly module and sources")
    dut_sources = _fingerprint_sources(dut["sources"], base_directory,
                                       "dut")
    backend = backend_identity()
    wrapper = generate_wrapper(contract_record, dut["module"])
    main_text = generate_main(contract_record, cycles)
    result = {
        "contract": contract_record["name"],
        "checker_sources": contract_record["checker_sources"],
        "dut_module": dut["module"],
        "dut_sources": dut_sources,
        "backend": backend,
        "wrapper_sha256": hashlib.sha256(
            wrapper.encode("utf-8")).hexdigest(),
        "main_sha256": hashlib.sha256(
            main_text.encode("utf-8")).hexdigest(),
        "significance": {
            "release_grade": False,
            "meaning": "a behavioral-contract run under the stated "
                       "fingerprinted checker and DUT; it never "
                       "establishes electrical behavior, and only a "
                       "production-RTL DUT run makes any claim about "
                       "production RTL",
        },
    }
    if not backend["available"]:
        result.update({"status": "backend-unavailable",
                       "assertions_passed": None})
        return result
    os.makedirs(workdir, exist_ok=True)
    wrapper_path = _forward(os.path.join(workdir, "contract_top.sv"))
    with open(wrapper_path, "w", encoding="utf-8",
              newline="\n") as handle:
        handle.write(wrapper)
    main_path = _forward(os.path.join(workdir, "contract_main.cpp"))
    with open(main_path, "w", encoding="utf-8",
              newline="\n") as handle:
        handle.write(main_text)
    sources = [_forward(os.path.join(base_directory,
                                     item["source"]))
               for item in contract_record["checker_sources"]]
    sources += [_forward(os.path.join(base_directory,
                                      item["source"]))
                for item in dut_sources]
    build = subprocess.run(
        [backend["path"], "--cc", "--exe", "--build", "--assert",
         "-Wall", "--top-module", "contract_top",
         "-o", "contract_model", wrapper_path, main_path] + sources,
        capture_output=True, text=True, timeout=600, cwd=workdir)
    if build.returncode != 0:
        result.update({"status": "build-failed",
                       "assertions_passed": None,
                       "build_log_tail":
                           (build.stdout + build.stderr)[-2000:]})
        return result
    binary = os.path.join(workdir, "obj_dir", "contract_model")
    if not os.path.isfile(binary):
        result.update({"status": "build-failed",
                       "assertions_passed": None,
                       "build_log_tail":
                           "the build reported success but produced "
                           "no contract_model binary"})
        return result
    run = subprocess.run([binary], capture_output=True, text=True,
                         timeout=600, cwd=workdir)
    result.update({
        "status": "ran" if run.returncode == 0 else "assertions-failed",
        "assertions_passed": run.returncode == 0,
        "run_log_tail": (run.stdout + run.stderr)[-2000:],
    })
    return result
