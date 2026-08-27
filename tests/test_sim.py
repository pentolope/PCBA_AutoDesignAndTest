"""The simulation foundation: registry, scenarios, backends, honesty.

These tests run without any simulator installed: what they pin is the
contract - strict validation, deterministic deck/harness generation,
fail-closed model lookup, and the explicit backend-unavailable shape
that is neither a pass nor a fabricated failure. When a backend IS
present the same tests exercise a real run, so installing ngspice or
Verilator strengthens the suite without changing it.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa.sim import fidelity, scenario, ngspice, digital  # noqa: E402
from pcbqa.sim.fidelity import SimulationError               # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "digital")


def _rc_scenario():
    return {
        "name": "rc-divider",
        "description": "two-resistor divider from a 1 V source",
        "elements": [
            {"kind": "vsource_dc", "name": "src",
             "nodes": ["in", "0"], "value": 1.0},
            {"kind": "resistor", "name": "top",
             "nodes": ["in", "mid"], "value": 1000.0},
            {"kind": "resistor", "name": "bottom",
             "nodes": ["mid", "0"], "value": 1000.0},
        ],
        "analyses": [{"kind": "op"}],
        "measurements": [
            {"name": "mid", "kind": "op_voltage", "node": "mid",
             "assertion": {"op": "within", "value": 0.5,
                           "tolerance": 0.001}},
        ],
    }


class TheModelRegistryFailsClosed(unittest.TestCase):

    def _record(self, **overrides):
        record = {"identity": "part", "kind": "resistor-network",
                  "fidelity": "datasheet-behavioral",
                  "provenance": {"source": "datasheet X rev 2"}}
        record.update(overrides)
        return record

    def test_the_fidelity_order_is_the_contract(self):
        self.assertEqual(fidelity.FIDELITY_CLASSES[0], "measured")
        self.assertEqual(fidelity.FIDELITY_CLASSES[-1], "unsupported")
        self.assertTrue(fidelity.meets("vendor-spice",
                                       "assumed-behavioral"))
        self.assertFalse(fidelity.meets("assumed-behavioral",
                                        "vendor-spice"))
        self.assertEqual(
            fidelity.weakest_of(["vendor-spice",
                                 "assumed-behavioral"]),
            "assumed-behavioral")
        with self.assertRaises(SimulationError):
            fidelity.rank("excellent")
        with self.assertRaises(SimulationError):
            fidelity.weakest_of([])

    def test_records_validate_strictly(self):
        registry = fidelity.ModelRegistry([self._record()])
        self.assertEqual(registry.identities(), ["part"])
        with self.assertRaises(SimulationError):
            registry.add(self._record())  # duplicate identity
        with self.assertRaises(SimulationError):
            fidelity.validate_model(self._record(extra="x"))
        with self.assertRaises(SimulationError):
            fidelity.validate_model(self._record(provenance={}))
        with self.assertRaises(SimulationError):
            fidelity.validate_model(self._record(fidelity="great"))
        with self.assertRaises(SimulationError):
            registry.get("absent")

    def test_coverage_summarizes_by_weakest(self):
        registry = fidelity.ModelRegistry([
            self._record(),
            self._record(identity="other", fidelity="vendor-spice"),
        ])
        coverage = registry.coverage(["part", "other"])
        self.assertEqual(coverage["weakest_fidelity"],
                         "datasheet-behavioral")
        self.assertEqual(sorted(coverage["models"]),
                         ["other", "part"])


class ScenariosValidateStrictly(unittest.TestCase):

    def test_the_fixture_scenario_is_valid(self):
        scenario.validate_scenario(_rc_scenario())

    def test_unknown_and_missing_keys_refuse(self):
        bad = _rc_scenario()
        bad["surprise"] = 1
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)
        for key in ("name", "elements", "analyses", "measurements"):
            bad = _rc_scenario()
            del bad[key]
            with self.assertRaises(SimulationError):
                scenario.validate_scenario(bad)

    def test_measurements_must_match_a_declared_analysis(self):
        bad = _rc_scenario()
        bad["measurements"][0]["kind"] = "tran_final_voltage"
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)

    def test_non_finite_element_values_refuse(self):
        for value in (float("nan"), float("inf"), True):
            bad = _rc_scenario()
            bad["elements"][1]["value"] = value
            with self.assertRaises(SimulationError):
                scenario.validate_scenario(bad)


class TheNgspiceBackendIsHonest(unittest.TestCase):

    def test_deck_generation_is_deterministic(self):
        registry = fidelity.ModelRegistry()
        deck_one = ngspice.generate_deck(registry, _rc_scenario())
        deck_two = ngspice.generate_deck(registry, _rc_scenario())
        self.assertEqual(deck_one, deck_two)
        self.assertIn("Rtop in mid 1000.0", deck_one)
        self.assertIn("wrdata op_mid.data v(mid)", deck_one)

    def test_an_unregistered_model_refuses_before_any_run(self):
        registry = fidelity.ModelRegistry()
        with_model = _rc_scenario()
        with_model["elements"].append(
            {"kind": "model_instance", "name": "u1",
             "nodes": ["in", "0"], "model": "missing-part"})
        with self.assertRaises(SimulationError):
            ngspice.generate_deck(registry, with_model)

    def test_insufficient_fidelity_refuses_before_any_run(self):
        registry = fidelity.ModelRegistry([{
            "identity": "weak", "kind": "load",
            "fidelity": "assumed-behavioral",
            "provenance": {"source": "assumption, recorded"},
            "spice": ".subckt weak a b\nR1 a b 1e6\n.ends",
        }])
        demanding = _rc_scenario()
        demanding["elements"].append(
            {"kind": "model_instance", "name": "u1",
             "nodes": ["in", "0"], "model": "weak"})
        demanding["required_fidelity"] = "vendor-spice"
        with self.assertRaises(SimulationError):
            ngspice.run_scenario(registry, demanding,
                                 tempfile.mkdtemp())

    def test_the_result_contract_separates_its_verdicts(self):
        """With ngspice absent the status is backend-unavailable and
        measurements are None - never fabricated; with ngspice
        present the divider must converge and meet its assertion.
        Either way model coverage and significance are stated, and
        release_grade is false unconditionally."""
        registry = fidelity.ModelRegistry()
        result = ngspice.run_scenario(registry, _rc_scenario(),
                                      tempfile.mkdtemp())
        self.assertIs(
            result["significance"]["release_grade"], False)
        self.assertEqual(len(result["deck_sha256"]), 64)
        self.assertIn("model_coverage", result)
        if not result["backend"]["available"]:
            self.assertEqual(result["status"], "backend-unavailable")
            self.assertIsNone(result["converged"])
            self.assertIsNone(result["measurements"])
        else:
            self.assertEqual(result["status"], "ran")
            self.assertTrue(result["converged"])
            measurement = result["measurements"]["mid"]
            self.assertTrue(measurement["passed"])
            self.assertAlmostEqual(measurement["value"], 0.5,
                                   places=3)


class TheDigitalContractFoundationIsHonest(unittest.TestCase):

    def _contract(self):
        return {"name": "clock-divider-contract",
                "top_module": "clock_divider_contract",
                "sources": ["clock_divider_contract.sv"],
                "assertion_summary": "the divided output toggles at "
                                     "exactly half the input rate "
                                     "after reset deasserts"}

    def test_contract_sources_are_fingerprinted(self):
        record = digital.validate_contract(self._contract(), FIXTURES)
        self.assertEqual(len(record["sources"]), 1)
        self.assertEqual(len(record["sources"][0]["sha256"]), 64)

    def test_a_missing_source_refuses(self):
        contract = self._contract()
        contract["sources"] = ["absent.sv"]
        with self.assertRaises(SimulationError):
            digital.validate_contract(contract, FIXTURES)

    def test_harness_generation_is_deterministic(self):
        record = digital.validate_contract(self._contract(), FIXTURES)
        one = digital.generate_harness(record, cycles=64)
        two = digital.generate_harness(record, cycles=64)
        self.assertEqual(one, two)
        self.assertIn("Vclock_divider_contract", one)

    def test_the_run_contract_never_fabricates(self):
        record = digital.validate_contract(self._contract(), FIXTURES)
        result = digital.run_contract(record, FIXTURES,
                                      tempfile.mkdtemp())
        self.assertIs(
            result["significance"]["release_grade"], False)
        if not result["backend"]["available"]:
            self.assertEqual(result["status"], "backend-unavailable")
            self.assertIsNone(result["assertions_passed"])
        else:
            self.assertIn(result["status"],
                          ("ran", "build-failed",
                           "assertions-failed"))


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
