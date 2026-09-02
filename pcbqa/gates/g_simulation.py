"""Circuit-simulation gates: the declared scenarios, actually executed.

`pcbqa.sim` could already state a model's evidence, refuse a scenario whose
contributors are not covered at an accepted class, run ngspice and turn each
measurement into a shared `pcbqa.claim` record with a conservative verdict.
Nothing called it during validation, so a board could ship having declared
simulation intent that no release ever ran. These gates close that: the
manifest names a model registry and, per design stage, the scenarios that
stage requires, and the release re-runs them.

Two gates, because they fail for two different reasons.

``SIM.SCENARIOS``       every declared scenario runs and every assertion it
                        makes is met. A scenario that cannot be executed -
                        no engine, no convergence - is a failure, never a
                        pass by omission: an unrun simulation is not evidence.
``SIM.MODEL_PROVENANCE``a model derived from the board must have been derived
                        from THIS board. An extracted model is a frozen
                        measurement of copper; nothing about the file says
                        which copper, so the digest it records is checked
                        against the board under validation.
``SIM.STAGE_COVERAGE``  each stage the board declares it requires is actually
                        covered by at least one scenario that ran and asserted
                        something. A stage listed with no assertion is a stage
                        nothing was proven about.

An UNKNOWN verdict blocks. The claim model produces UNKNOWN when the declared
knowledge cannot decide the assertion - an approximate value, an unbounded
model, a bound pointing the wrong way. That is exactly the case where a gate
must not pass: the number exists, but it does not answer the question.

Nothing here names a board, a net, a node, a stage or a threshold. The stage
names are whatever the board declares; this module only requires that what it
declares as required is what it covered.
"""

from __future__ import annotations

import json
import os

from ..core import gate, sha256_file
from ..claim import PASS as CLAIM_PASS
from ..sim import model_registry, ngspice


def _load_json(path, label):
    with open(path, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, list):
        raise ValueError(
            "{} must be a JSON array of records, not {}".format(
                label, type(document).__name__))
    return document


def _registry(ctx):
    """The models a scenario may reference: frozen ones, and live ones.

    A model measured from the board is extracted HERE, during validation,
    rather than read from a file a board generated earlier. That removes
    the whole staleness question for those models: there is no earlier
    file to have gone out of date. Each is registered under the stable
    alias the manifest gives it, because the measured identity embeds the
    board digest and could not otherwise be named by a stored scenario.
    """
    def build():
        records = []
        if ctx.manifest.has("simulation.models"):
            records.extend(_load_json(
                ctx.manifest.resolve(ctx.manifest.get("simulation.models")),
                "simulation.models"))
        registry = model_registry.ModelRegistry(records)
        for record in _extracted(ctx):
            registry.add(record)
        return registry
    return ctx.cache("simulation_registry", build)


def _extracted(ctx):
    """Models measured from the board under validation, or nothing.

    The copper traversal is live - it reads the board being validated, so
    no earlier file can describe copper that is no longer there. The
    PHYSICAL inputs it needs are not a property of the board at all: they
    are the fabricator's finished copper and board thickness, and they are
    read here as committed parameter records, each carrying its own source
    type and evidence digest. Resolving them from the fabricator catalog is
    deliberately not done here - validation must not be able to reach the
    network even by accident - so a board freezes them once, outside a
    gate, and this refuses anything that is not a valid parameter record.
    """
    def build():
        if not ctx.manifest.has("simulation.extracted_models"):
            return []
        import pcbnew

        from .. import extract, geom

        spec = ctx.manifest.get("simulation.extracted_models")
        paths = spec.get("paths") or {}
        if not paths:
            raise ValueError(
                "simulation.extracted_models declares no paths; an empty "
                "declaration measures nothing")
        with open(ctx.manifest.resolve(spec["physical_inputs"]),
                  encoding="utf-8") as handle:
            physical = json.load(handle)
        if set(physical) != {"copper_thickness_mm", "board_thickness_mm"}:
            raise ValueError(
                "physical inputs carry exactly copper_thickness_mm and "
                "board_thickness_mm")
        copper = {layer: extract.validate_parameter(
                      record, "copper thickness on {}".format(layer))
                  for layer, record in physical["copper_thickness_mm"].items()}
        extract.validate_parameter(physical["board_thickness_mm"],
                                   "board thickness")
        geom.configure(ctx.manifest.geometry_profile()
                       .tolerance("polygon_chord_error_mm").value)
        board_path = ctx.board_path()
        board = pcbnew.LoadBoard(board_path)
        board_sha256 = sha256_file(board_path)
        records = []
        for alias in sorted(paths):
            declared = paths[alias]
            traced = extract.path_resistance(
                board, declared["net"], declared["from_pad"],
                declared["to_pad"], copper)
            records.append(extract.aliased(
                extract.interconnect_model_from_path(
                    traced, board_sha256, physical), alias))
        return records
    return ctx.cache("simulation_extracted", build)


def _stages(ctx):
    """{stage: [(scenario path, run result)]}, every scenario executed once."""
    def build():
        registry = _registry(ctx)
        declared = ctx.manifest.get("simulation.stages")
        if not isinstance(declared, dict) or not declared:
            raise ValueError(
                "simulation.stages maps a stage name to the scenarios that "
                "stage requires; an empty map declares no simulation")
        runs = {}
        for stage in sorted(declared):
            entries = []
            for index, relative in enumerate(declared[stage]):
                path = ctx.manifest.resolve(relative)
                with open(path, "r", encoding="utf-8") as handle:
                    scenario = json.load(handle)
                workdir = os.path.join(ctx.workdir, "simulation", stage,
                                       "%02d" % index)
                entries.append((relative,
                                ngspice.run_scenario(registry, scenario,
                                                     workdir)))
            runs[stage] = entries
        return runs
    return ctx.cache("simulation_runs", build)


def _asserted(run):
    """{measurement: verdict} for every measurement that asserts something."""
    return {name: entry["verdict"]
            for name, entry in sorted((run["measurements"] or {}).items())
            if entry["verdict"] is not None}


@gate("SIM.SCENARIOS", "Declared circuit simulations run and their assertions hold",
      requires=("simulation.stages",))
def sim_scenarios(ctx, res):
    runs = _stages(ctx)
    executed, asserted, decided = 0, 0, 0
    for stage in sorted(runs):
        for relative, run in runs[stage]:
            executed += 1
            if run["status"] != "ran":
                res.finding(stage=stage, scenario=relative,
                            simulation=run["scenario"],
                            status=run["status"],
                            issue="the scenario did not run, so it is not "
                                  "evidence; an unexecuted simulation never "
                                  "passes by omission",
                            backend=run["backend"])
                continue
            verdicts = _asserted(run)
            asserted += len(verdicts)
            for name, verdict in verdicts.items():
                if verdict["result"] == CLAIM_PASS:
                    decided += 1
                    continue
                res.finding(stage=stage, scenario=relative,
                            simulation=run["scenario"], measurement=name,
                            result=verdict["result"], basis=verdict["basis"],
                            issue="the measurement does not meet its declared "
                                  "assertion under the evidence the scenario "
                                  "records")
    res.measurements.update(scenarios_executed=executed,
                            assertions_evaluated=asserted,
                            assertions_met=decided)
    if res.findings:
        unrun = len([f for f in res.findings if "measurement" not in f])
        return res.failed(
            "{} scenario(s) did not run and {} of {} declared assertion(s) "
            "are unmet or undecided".format(unrun, asserted - decided,
                                            asserted))
    return res.passed(
        "{} scenario(s) ran and all {} declared assertion(s) hold".format(
            executed, asserted))


@gate("SIM.STAGE_COVERAGE",
      "Every design stage the board requires simulation for is covered",
      requires=("simulation.stages", "simulation.required_stages"))
def sim_stage_coverage(ctx, res):
    runs = _stages(ctx)
    required = res.limit(ctx.manifest.constraint(
        "simulation.required_stages", units="stage name",
        cid="simulation.required_stages")).value
    for stage in required:
        entries = runs.get(stage)
        if not entries:
            res.finding(stage=stage,
                        issue="the board requires this stage to be simulated "
                              "but declares no scenario for it")
            continue
        ran = [entry for entry in entries if entry[1]["status"] == "ran"]
        if not ran:
            res.finding(stage=stage, scenarios=len(entries),
                        issue="no scenario for this stage executed, so the "
                              "stage is declared but unproven")
            continue
        if not any(_asserted(run) for _relative, run in ran):
            res.finding(stage=stage, scenarios_ran=len(ran),
                        issue="every scenario for this stage is descriptive: "
                              "no measurement asserts anything, so nothing "
                              "about the stage was proven")
    res.measurements.update(
        required_stages=len(required),
        declared_stages=sorted(runs),
        scenarios_per_stage={stage: len(entries)
                             for stage, entries in sorted(runs.items())})
    if res.findings:
        return res.failed("{} required simulation stage(s) are not "
                          "covered".format(len(res.findings)))
    return res.passed(
        "all {} required stage(s) are covered by a scenario that ran and "
        "asserted".format(len(required)))


@gate("SIM.MODEL_PROVENANCE",
      "Board-derived simulation models describe the board being validated",
      requires=("simulation.models", "sources.pcb"))
def sim_model_provenance(ctx, res):
    registry = _registry(ctx)
    board_sha256 = sha256_file(ctx.board_path())
    checked = 0
    for identity in registry.identities():
        derivation = registry.get(identity).get("derivation") or {}
        recorded = derivation.get("board_file_sha256")
        if recorded is None:
            continue
        checked += 1
        if recorded != board_sha256:
            res.finding(model=identity, recorded_sha256=recorded,
                        board_sha256=board_sha256,
                        issue="the model was extracted from a different "
                              "board file than the one under validation; a "
                              "stale extraction is a measurement of copper "
                              "that is no longer there")
    res.measurements.update(models=len(registry.identities()),
                            board_derived_models=checked,
                            board_sha256=board_sha256)
    if res.findings:
        return res.failed(
            "{} board-derived model(s) describe a different board".format(
                len(res.findings)))
    return res.passed(
        "all {} board-derived model(s) were extracted from this exact "
        "board".format(checked))
