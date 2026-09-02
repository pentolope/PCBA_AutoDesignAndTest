"""Simulation measurement reductions, and the gates that judge a run.

Two things are proven here. First, that a transient's excursion is read
from the whole waveform and not from its endpoint: a droop that recovers
and an overshoot that decays are invisible to `tran_final_voltage`, which
is exactly why they had to become their own measurement kinds. Second,
that the gates refuse the ways a declared simulation stops being evidence
- a scenario that did not run, a verdict the evidence cannot decide, a
required stage nothing covers, and a board-derived model extracted from a
different board.

No ngspice engine is required: the reductions are tested on data, and the
gates on run records shaped exactly as `run_scenario` returns them.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from pcbqa import claim
from pcbqa.core import GateResult
from pcbqa.gates import g_simulation
from pcbqa.sim import ngspice, scenario


class ReductionsReadTheWholeWaveform(unittest.TestCase):
    """An excursion that recovers is invisible at the endpoint."""

    WAVE = [4.75, 4.70, 4.62, 4.71, 4.75]

    def _written(self, values):
        handle, path = tempfile.mkstemp(suffix=".data")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for index, value in enumerate(values):
                out.write("{} {}\n".format(index * 1e-6, value))
        self.addCleanup(os.unlink, path)
        return path

    def test_final_value_misses_the_droop(self):
        path = self._written(self.WAVE)
        self.assertAlmostEqual(
            ngspice._read_column_value(path, "last"), 4.75)

    def test_minimum_finds_the_droop(self):
        path = self._written(self.WAVE)
        self.assertAlmostEqual(
            ngspice._read_column_value(path, "min"), 4.62)

    def test_maximum_finds_the_overshoot(self):
        path = self._written([5.0, 5.4, 5.1, 5.0])
        self.assertAlmostEqual(
            ngspice._read_column_value(path, "max"), 5.4)

    def test_an_empty_result_refuses_rather_than_defaulting(self):
        path = self._written([])
        with self.assertRaises(ngspice.SimulationError):
            ngspice._read_column_value(path, "min")

    def test_an_unknown_reduction_refuses(self):
        with self.assertRaises(ngspice.SimulationError):
            ngspice._reduce([1.0], "average")

    def test_every_measurement_kind_declares_a_reduction(self):
        for kind in scenario._MEASUREMENT_KINDS:
            self.assertIn(kind, scenario.MEASUREMENT_REDUCTIONS)

    def test_the_excursion_kinds_run_on_the_transient(self):
        for kind in ("tran_min_voltage", "tran_max_voltage"):
            self.assertEqual(ngspice._family_of(kind), "tran")


def _run(name, status="ran", results=("PASS",)):
    measurements = {}
    for index, result in enumerate(results):
        measurements["m%d" % index] = {
            "claim": None,
            "verdict": (None if result is None else
                        {"result": result, "basis": "exact",
                         "exact": result == claim.PASS,
                         "knowledge_basis": None}),
        }
    return {"scenario": name, "status": status,
            "backend": {"name": "ngspice", "available": status != "x"},
            "measurements": measurements if status == "ran" else None}


class _Manifest:
    def __init__(self, values):
        self._values = values

    def get(self, key, *default):
        if key in self._values:
            return self._values[key]
        if default:
            return default[0]
        raise KeyError(key)

    def constraint(self, key, units=None, cid=None):
        from pcbqa.constraints import Constraint
        return Constraint(cid or key, key, self._values[key],
                          units or "unit", "manifest.json", "0" * 64)


class _Ctx:
    def __init__(self, runs, values=None):
        self.manifest = _Manifest(values or {})
        self._runs = runs

    def cache(self, key, factory):
        if key == "simulation_runs":
            return self._runs
        return factory()


class ScenarioGateRefusesUnprovenRuns(unittest.TestCase):
    def _result(self, runs, values=None):
        res = GateResult("SIM.SCENARIOS", "t")
        g_simulation.sim_scenarios(_Ctx(runs, values), res)
        return res

    def test_all_assertions_met_passes(self):
        res = self._result({"pre": [("a.json", _run("a"))]})
        self.assertEqual(res.status, "PASS")

    def test_a_scenario_that_never_ran_is_not_evidence(self):
        res = self._result(
            {"pre": [("a.json", _run("a", status="backend-unavailable"))]})
        self.assertEqual(res.status, "FAIL")
        self.assertIn("unexecuted simulation", res.findings[0]["issue"])

    def test_a_scenario_that_did_not_converge_is_not_evidence(self):
        res = self._result(
            {"pre": [("a.json", _run("a", status="simulation-failed"))]})
        self.assertEqual(res.status, "FAIL")

    def test_a_failed_assertion_blocks(self):
        res = self._result({"pre": [("a.json", _run("a", results=("FAIL",)))]})
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.findings[0]["result"], "FAIL")

    def test_an_undecidable_assertion_blocks(self):
        """UNKNOWN is where a number exists but cannot answer the question."""
        res = self._result(
            {"pre": [("a.json", _run("a", results=("UNKNOWN",)))]})
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.findings[0]["result"], "UNKNOWN")

    def test_descriptive_measurements_are_not_judged(self):
        res = self._result(
            {"pre": [("a.json", _run("a", results=("PASS", None)))]})
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.measurements["assertions_evaluated"], 1)


class StageCoverageRefusesUnprovenStages(unittest.TestCase):
    def _result(self, runs, required):
        res = GateResult("SIM.STAGE_COVERAGE", "t")
        g_simulation.sim_stage_coverage(
            _Ctx(runs, {"simulation.required_stages": required}), res)
        return res

    def test_a_covered_stage_passes(self):
        res = self._result({"pre": [("a.json", _run("a"))]}, ["pre"])
        self.assertEqual(res.status, "PASS")

    def test_a_required_stage_with_no_scenario_blocks(self):
        res = self._result({"pre": [("a.json", _run("a"))]}, ["pre", "post"])
        self.assertEqual(res.status, "FAIL")
        self.assertEqual(res.findings[0]["stage"], "post")

    def test_a_stage_whose_scenarios_all_failed_to_run_blocks(self):
        res = self._result(
            {"pre": [("a.json", _run("a", status="backend-unavailable"))]},
            ["pre"])
        self.assertEqual(res.status, "FAIL")
        self.assertIn("declared but unproven", res.findings[0]["issue"])

    def test_a_stage_that_asserts_nothing_proves_nothing(self):
        res = self._result(
            {"pre": [("a.json", _run("a", results=(None,)))]}, ["pre"])
        self.assertEqual(res.status, "FAIL")
        self.assertIn("descriptive", res.findings[0]["issue"])


class ModelProvenanceBindsToTheBoard(unittest.TestCase):
    def _result(self, models, board_bytes=b"board"):
        handle, path = tempfile.mkstemp(suffix=".kicad_pcb")
        with os.fdopen(handle, "wb") as out:
            out.write(board_bytes)
        self.addCleanup(os.unlink, path)

        class Registry:
            def identities(self):
                return sorted(models)

            def get(self, identity):
                return models[identity]

        class Ctx:
            manifest = _Manifest({})

            def cache(self, key, factory):
                return Registry()

            def board_path(self):
                return path

        res = GateResult("SIM.MODEL_PROVENANCE", "t")
        g_simulation.sim_model_provenance(Ctx(), res)
        return res

    def _digest(self, data=b"board"):
        import hashlib
        return hashlib.sha256(data).hexdigest()

    def test_a_model_extracted_from_this_board_passes(self):
        res = self._result({"p": {"derivation": {
            "board_file_sha256": self._digest()}}})
        self.assertEqual(res.status, "PASS")

    def test_a_model_extracted_from_another_board_blocks(self):
        res = self._result({"p": {"derivation": {
            "board_file_sha256": self._digest(b"other")}}})
        self.assertEqual(res.status, "FAIL")
        self.assertIn("stale extraction", res.findings[0]["issue"])

    def test_a_model_that_is_not_board_derived_is_not_bound(self):
        res = self._result({"p": {"notes": ["a datasheet model"]}})
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.measurements["board_derived_models"], 0)


class AliasingKeepsTheMeasuredIdentity(unittest.TestCase):
    """A stable handle must not become an anonymous one."""

    RECORD = {"identity": "path:N:A->B@abc+phys:def", "kind": "k",
              "evidence": [],
              "spice": ".subckt path:N:A->B@abc+phys:def a b\n"
                       "R1 a b 0.011\n.ends"}

    def _aliased(self, alias="handle"):
        from pcbqa import extract
        return extract.aliased(self.RECORD, alias)

    def test_the_alias_becomes_the_identity(self):
        self.assertEqual(self._aliased()["identity"], "handle")

    def test_the_subcircuit_is_renamed_with_it(self):
        spice = self._aliased()["spice"]
        self.assertIn(".subckt handle a b", spice)
        self.assertNotIn(self.RECORD["identity"], spice)

    def test_the_measured_identity_survives_in_the_derivation(self):
        self.assertEqual(
            self._aliased()["derivation"]["extracted_identity"],
            self.RECORD["identity"])

    def test_the_original_record_is_not_mutated(self):
        self._aliased()
        self.assertEqual(self.RECORD["identity"], "path:N:A->B@abc+phys:def")
        self.assertNotIn("derivation", self.RECORD)

    def test_an_empty_alias_refuses(self):
        from pcbqa import extract
        for alias in ("", "   ", None, 3):
            with self.assertRaises(extract.ExtractionError):
                extract.aliased(self.RECORD, alias)

    def test_a_record_with_no_spice_cannot_be_aliased(self):
        from pcbqa import extract
        with self.assertRaises(extract.ExtractionError):
            extract.aliased({"identity": "x"}, "handle")


if __name__ == "__main__":
    unittest.main()
