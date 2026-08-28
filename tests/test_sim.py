"""The simulation foundation: coverage, scenarios, backends, honesty.

These tests run without any simulator installed: what they pin is the
contract - phenomenon-aware coverage with no cross-domain
substitution, strict scenario validation where accepted means applied,
deterministic generation, and the explicit backend-unavailable shape
that is neither a pass nor a fabricated failure. When a backend IS
present the same tests become hard requirements: the known-good
fixtures MUST build, run and pass, and the known-bad fixtures MUST be
caught.
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


def _rtl_model():
    return {"identity": "controller-rtl", "kind": "digital-controller",
            "coverage": {"functional_behavior": "rtl"},
            "provenance": {"source": "firmware repo, fingerprinted"}}


def _ibis_model():
    return {"identity": "controller-io", "kind": "digital-io-buffer",
            "coverage": {"digital_io": "vendor-ibis"},
            "provenance": {"source": "vendor IBIS file"}}


class CoverageIsPhenomenonAware(unittest.TestCase):

    def test_vocabularies_are_closed(self):
        with self.assertRaises(SimulationError):
            fidelity.validate_coverage({"telepathy": "measured"})
        with self.assertRaises(SimulationError):
            fidelity.validate_coverage({"digital_io": "excellent"})
        with self.assertRaises(SimulationError):
            fidelity.validate_requirement({"digital_io": []})

    def test_wrong_phenomenon_never_satisfies(self):
        """The invariant: RTL, however strong for function, cannot
        satisfy a digital_io requirement - a model never satisfies a
        phenomenon it does not cover."""
        registry = fidelity.ModelRegistry([_rtl_model()])
        report = registry.coverage_report(
            ["controller-rtl"],
            {"digital_io": ["vendor-ibis", "measured"]})
        self.assertFalse(report["satisfied"])
        self.assertEqual(
            report["per_phenomenon"]["digital_io"]["satisfied_by"], [])

    def test_mixed_domain_coverage_reports_per_phenomenon(self):
        registry = fidelity.ModelRegistry([_rtl_model(),
                                           _ibis_model()])
        report = registry.coverage_report(
            ["controller-rtl", "controller-io"],
            {"functional_behavior": ["rtl"],
             "digital_io": ["vendor-ibis"]})
        self.assertTrue(report["satisfied"])
        per = report["per_phenomenon"]
        self.assertEqual(per["functional_behavior"]["satisfied_by"],
                         ["controller-rtl"])
        self.assertEqual(per["digital_io"]["satisfied_by"],
                         ["controller-io"])

    def test_unaccepted_class_is_reported_not_promoted(self):
        registry = fidelity.ModelRegistry([_ibis_model()])
        report = registry.coverage_report(
            ["controller-io"], {"digital_io": ["measured"]})
        self.assertFalse(report["satisfied"])
        self.assertEqual(
            report["per_phenomenon"]["digital_io"][
                "covered_at_unaccepted_class"], ["controller-io"])

    def test_records_validate_strictly(self):
        registry = fidelity.ModelRegistry([_rtl_model()])
        with self.assertRaises(SimulationError):
            registry.add(_rtl_model())
        with self.assertRaises(SimulationError):
            fidelity.validate_model(dict(_rtl_model(), surprise=1))
        with self.assertRaises(SimulationError):
            fidelity.validate_model(dict(_rtl_model(), coverage={}))
        with self.assertRaises(SimulationError):
            fidelity.validate_model(dict(_rtl_model(),
                                         provenance={}))
        with self.assertRaises(SimulationError):
            registry.get("absent")


class ScenariosAcceptOnlyWhatExecutes(unittest.TestCase):

    def test_the_fixture_scenario_is_valid(self):
        scenario.validate_scenario(_rc_scenario())

    def test_unimplemented_features_refuse(self):
        for key, value in (("substitutions", {"a": "b"}),
                           ("surprise", 1)):
            bad = _rc_scenario()
            bad[key] = value
            with self.assertRaises(SimulationError):
                scenario.validate_scenario(bad)

    def test_operating_conditions_validate_exactly(self):
        good = _rc_scenario()
        good["operating_conditions"] = {"temperature_c": 85.0}
        scenario.validate_scenario(good)
        for conditions in ({"temperature_c": -300.0},
                           {"temperature_c": float("nan")},
                           {"humidity": 0.5},
                           {"temperature_c": 25.0, "extra": 1}):
            bad = _rc_scenario()
            bad["operating_conditions"] = conditions
            with self.assertRaises(SimulationError):
                scenario.validate_scenario(bad)

    def test_nested_unknown_keys_refuse(self):
        bad = _rc_scenario()
        bad["elements"][1]["tolerance"] = 0.01
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)
        bad = _rc_scenario()
        bad["analyses"][0]["surprise"] = 1
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)
        bad = _rc_scenario()
        bad["measurements"][0]["surprise"] = 1
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)
        bad = _rc_scenario()
        bad["measurements"][0]["assertion"]["surprise"] = 1
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(bad)

    def test_invalid_numerics_refuse(self):
        for value in (float("nan"), float("inf"), True, 0.0, -5.0):
            bad = _rc_scenario()
            bad["elements"][1]["value"] = value
            with self.assertRaises(SimulationError):
                scenario.validate_scenario(bad)

    def test_transient_parameters_must_be_sane(self):
        def tran_scenario(step, stop, pulse_overrides=None):
            pulse = {"v1": 0.0, "v2": 1.0, "delay_s": 0.0,
                     "rise_s": 1e-9, "fall_s": 1e-9,
                     "width_s": 5e-7, "period_s": 1e-6}
            pulse.update(pulse_overrides or {})
            return {
                "name": "tran-fixture",
                "elements": [
                    {"kind": "vsource_pulse", "name": "src",
                     "nodes": ["in", "0"], "pulse": pulse},
                    {"kind": "resistor", "name": "load",
                     "nodes": ["in", "0"], "value": 50.0},
                ],
                "analyses": [{"kind": "tran", "step_s": step,
                              "stop_s": stop}],
                "measurements": [
                    {"name": "final", "kind": "tran_final_voltage",
                     "node": "in"},
                ],
            }
        scenario.validate_scenario(tran_scenario(1e-9, 1e-6))
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(tran_scenario(1e-6, 1e-9))
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(tran_scenario(0.0, 1e-6))
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(
                tran_scenario(1e-9, 1e-6,
                              {"rise_s": float("inf")}))
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(
                tran_scenario(1e-9, 1e-6,
                              {"width_s": 1e-6, "period_s": 1e-7}))


class TheNgspiceBackendIsHonest(unittest.TestCase):

    def test_deck_generation_is_deterministic_and_applies_conditions(
            self):
        registry = fidelity.ModelRegistry()
        with_conditions = _rc_scenario()
        with_conditions["operating_conditions"] = {
            "temperature_c": 85.0}
        deck_one = ngspice.generate_deck(registry, with_conditions)
        deck_two = ngspice.generate_deck(registry, with_conditions)
        self.assertEqual(deck_one, deck_two)
        self.assertIn(".options temp=85.0", deck_one)
        self.assertIn("wrdata op_mid.data v(mid)", deck_one)

    def test_an_unregistered_model_refuses_before_any_run(self):
        registry = fidelity.ModelRegistry()
        with_model = _rc_scenario()
        with_model["elements"].append(
            {"kind": "model_instance", "name": "u1",
             "nodes": ["in", "0"], "model": "missing-part"})
        with self.assertRaises(SimulationError):
            ngspice.generate_deck(registry, with_model)

    def test_unmet_coverage_refuses_before_any_run(self):
        registry = fidelity.ModelRegistry([{
            "identity": "weak-load", "kind": "load",
            "coverage": {"device_electrical": "assumed-behavioral"},
            "provenance": {"source": "assumption, recorded"},
            "spice": ".subckt weak-load a b\nR1 a b 1e6\n.ends",
        }])
        demanding = _rc_scenario()
        demanding["elements"].append(
            {"kind": "model_instance", "name": "u1",
             "nodes": ["in", "0"], "model": "weak-load"})
        demanding["required_coverage"] = {
            "device_electrical": ["vendor-spice", "measured"]}
        with self.assertRaises(SimulationError):
            ngspice.run_scenario(registry, demanding,
                                 tempfile.mkdtemp())

    def test_the_result_contract_separates_its_verdicts(self):
        registry = fidelity.ModelRegistry()
        with_conditions = _rc_scenario()
        with_conditions["operating_conditions"] = {
            "temperature_c": 60.0}
        result = ngspice.run_scenario(registry, with_conditions,
                                      tempfile.mkdtemp())
        self.assertIs(
            result["significance"]["release_grade"], False)
        self.assertEqual(len(result["deck_sha256"]), 64)
        self.assertEqual(
            result["operating_conditions_applied"],
            {"temperature_c": 60.0})
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


class TheDigitalContractSeparatesDutFromChecker(unittest.TestCase):

    def _contract(self):
        return {"name": "clock-divider-contract",
                "checker_module": "clock_divider_checker",
                "checker_sources": ["clock_divider_checker.sv"],
                "ports": [{"name": "divided", "dir": "out",
                           "width": 1}],
                "clocking": {"clock_port": "clk",
                             "reset_port": "rst",
                             "reset_active_high": True,
                             "reset_cycles": 2},
                "assertion_summary": "the divided output toggles at "
                                     "exactly half the input rate "
                                     "after reset deasserts"}

    def _record(self):
        return digital.validate_contract(self._contract(), FIXTURES)

    def test_the_contract_declares_its_stimulus(self):
        record = self._record()
        self.assertEqual(record["clocking"]["clock_port"], "clk")
        self.assertEqual(len(record["checker_sources"][0]["sha256"]),
                         64)
        bad = self._contract()
        bad["clocking"] = {"clock_port": "clk"}
        with self.assertRaises(SimulationError):
            digital.validate_contract(bad, FIXTURES)

    def test_no_hidden_port_names(self):
        """The generated stimulus uses only DECLARED names: renaming
        the clock in the contract renames it everywhere, so nothing
        generic assumes clk/rst."""
        renamed = self._contract()
        renamed["clocking"] = {"clock_port": "sysclk",
                               "reset_port": "nreset",
                               "reset_active_high": False,
                               "reset_cycles": 3}
        record = digital.validate_contract(renamed, FIXTURES)
        main_text = digital.generate_main(record, 16)
        self.assertIn("top.sysclk", main_text)
        self.assertIn("top.nreset = 0;", main_text)
        self.assertIn("if (cycle == 3) top.nreset = 1;", main_text)
        self.assertNotIn("top.clk", main_text)
        wrapper = digital.generate_wrapper(record, "some_dut")
        self.assertIn("input  logic sysclk,", wrapper)

    def test_generation_is_deterministic(self):
        record = self._record()
        self.assertEqual(
            digital.generate_wrapper(record, "clock_divider_dut"),
            digital.generate_wrapper(record, "clock_divider_dut"))
        self.assertEqual(digital.generate_main(record, 64),
                         digital.generate_main(record, 64))

    def test_the_checker_is_identical_across_duts(self):
        """DUT substitution - the production-RTL path - never touches
        the checker: both runs carry the same fingerprinted checker
        sources while the DUT fingerprints differ."""
        record = self._record()
        results = []
        for module, source in (
                ("clock_divider_dut", "clock_divider_dut.sv"),
                ("alternate_divider_dut", "alternate_divider_dut.sv")):
            results.append(digital.run_contract(
                record, {"module": module, "sources": [source]},
                FIXTURES, tempfile.mkdtemp(), cycles=32))
        self.assertEqual(results[0]["checker_sources"],
                         results[1]["checker_sources"])
        self.assertNotEqual(results[0]["dut_sources"],
                            results[1]["dut_sources"])

    def test_positive_fixture_must_pass_when_backend_exists(self):
        """The hard requirement: with Verilator installed the
        known-good fixture MUST build, execute and pass - build
        failure or assertion failure here is a test failure, not an
        accepted outcome. Without Verilator the only accepted shape
        is backend-unavailable."""
        record = self._record()
        for module, source in (
                ("clock_divider_dut", "clock_divider_dut.sv"),
                ("alternate_divider_dut", "alternate_divider_dut.sv")):
            result = digital.run_contract(
                record, {"module": module, "sources": [source]},
                FIXTURES, tempfile.mkdtemp(), cycles=32)
            if not result["backend"]["available"]:
                self.assertEqual(result["status"],
                                 "backend-unavailable")
                self.assertIsNone(result["assertions_passed"])
            else:
                self.assertEqual(result["status"], "ran")
                self.assertIs(result["assertions_passed"], True)

    def test_negative_fixtures_are_detected_when_backend_exists(self):
        record = self._record()
        backend = digital.backend_identity()
        if not backend["available"]:
            self.skipTest("verilator is not installed; the negative "
                          "fixtures need a real build")
        broken_behavior = digital.run_contract(
            record, {"module": "broken_divider_dut",
                     "sources": ["broken_divider_dut.sv"]},
            FIXTURES, tempfile.mkdtemp(), cycles=32)
        self.assertEqual(broken_behavior["status"],
                         "assertions-failed")
        broken_build = digital.run_contract(
            record, {"module": "broken_syntax",
                     "sources": ["broken_syntax.sv"]},
            FIXTURES, tempfile.mkdtemp(), cycles=32)
        self.assertEqual(broken_build["status"], "build-failed")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()


def _interconnect(identity, evidence="geometry-derived",
                  conditions=None):
    record = {
        "identity": identity, "kind": "board-interconnect",
        "coverage": {"interconnect_dc": evidence},
        "provenance": {"source": "test fixture"},
        "spice": ".subckt {} a b\nR1 a b 0.01\n.ends".format(
            identity),
    }
    if conditions is not None:
        record["conditions"] = conditions
    return record


class CoverageIsContributorScoped(unittest.TestCase):
    """The completeness invariant: every model contributing to a
    measurement must individually satisfy the required policy."""

    _REQUIRE_DC = {"interconnect_dc": ["geometry-derived"]}

    def _two_link_scenario(self):
        """Source -> strong link -> weak link -> load, measured at
        the far end: both links contribute to the measurement."""
        return {
            "name": "two-links",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "strong_link",
                 "nodes": ["in", "mid"], "model": "strong"},
                {"kind": "model_instance", "name": "weak_link",
                 "nodes": ["mid", "out"], "model": "weak"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out"}],
            "required_coverage": dict(self._REQUIRE_DC),
        }

    def test_one_strong_model_cannot_mask_a_weak_contributor(self):
        registry = fidelity.ModelRegistry([
            _interconnect("strong"),
            _interconnect("weak", evidence="assumed-behavioral"),
        ])
        report = scenario.contributor_coverage_report(
            registry, self._two_link_scenario())
        self.assertFalse(report["satisfied"])
        phenomenon = report["per_measurement"]["vout"][
            "per_phenomenon"]["interconnect_dc"]
        self.assertEqual(phenomenon["violating"], ["weak_link"])
        with self.assertRaises(SimulationError):
            ngspice.run_scenario(fidelity.ModelRegistry([
                _interconnect("strong"),
                _interconnect("weak",
                              evidence="assumed-behavioral"),
            ]), self._two_link_scenario(), tempfile.mkdtemp())

    def _two_island_scenario(self, measure_node):
        """Two subcircuits sharing only the reference node: a strong
        link island and a weak link island."""
        return {
            "name": "two-islands",
            "elements": [
                {"kind": "vsource_dc", "name": "src_a",
                 "nodes": ["a_in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "strong_link",
                 "nodes": ["a_in", "a_out"], "model": "strong"},
                {"kind": "resistor", "name": "load_a",
                 "nodes": ["a_out", "0"], "value": 50.0},
                {"kind": "vsource_dc", "name": "src_b",
                 "nodes": ["b_in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "weak_link",
                 "nodes": ["b_in", "b_out"], "model": "weak"},
                {"kind": "resistor", "name": "load_b",
                 "nodes": ["b_out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "v", "kind": "op_voltage",
                 "node": measure_node}],
            "required_coverage": dict(self._REQUIRE_DC),
        }

    def test_an_unrelated_strong_model_satisfies_nothing(self):
        """Measuring the weak island: the strong model elsewhere in
        the scenario is not a contributor and cannot help."""
        registry = fidelity.ModelRegistry([
            _interconnect("strong"),
            _interconnect("weak", evidence="assumed-behavioral"),
        ])
        report = scenario.contributor_coverage_report(
            registry, self._two_island_scenario("b_out"))
        self.assertFalse(report["satisfied"])
        entry = report["per_measurement"]["v"]
        self.assertNotIn("strong_link",
                         entry["contributing_elements"])

    def test_a_scoped_measurement_needs_only_its_own_path(self):
        """Measuring the strong island: the weak model in the OTHER
        island is not a contributor, so it is not required to be
        strong - scoping never over-demands."""
        registry = fidelity.ModelRegistry([
            _interconnect("strong"),
            _interconnect("weak", evidence="assumed-behavioral"),
        ])
        report = scenario.contributor_coverage_report(
            registry, self._two_island_scenario("a_out"))
        self.assertTrue(report["satisfied"])

    def test_mixed_phenomena_stay_independent(self):
        """A strong device_electrical contributor in the path never
        answers an interconnect_dc requirement: with no DC provider
        among the contributors the measurement is unmet."""
        registry = fidelity.ModelRegistry([{
            "identity": "vendor-part", "kind": "device",
            "coverage": {"device_electrical": "vendor-spice"},
            "provenance": {"source": "vendor model"},
            "spice": ".subckt vendor-part a b\nR1 a b 10.0\n.ends",
        }])
        mixed = {
            "name": "mixed",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "u1",
                 "nodes": ["in", "out"], "model": "vendor-part"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out"}],
            "required_coverage": {
                "interconnect_dc": ["geometry-derived"]},
        }
        report = scenario.contributor_coverage_report(
            fidelity.ModelRegistry([{
                "identity": "vendor-part", "kind": "device",
                "coverage": {"device_electrical": "vendor-spice"},
                "provenance": {"source": "vendor model"},
            }]), mixed)
        self.assertFalse(report["satisfied"])
        phenomenon = report["per_measurement"]["vout"][
            "per_phenomenon"]["interconnect_dc"]
        self.assertEqual(phenomenon["providers"], {})
        self.assertEqual(phenomenon["unaccounted"], ["u1"])
        self.assertIn("silence is not irrelevance",
                      phenomenon["why"])

    def test_measuring_the_reference_node_refuses(self):
        broken = _rc_scenario()
        broken["measurements"][0]["node"] = "0"
        with self.assertRaises(SimulationError):
            scenario.validate_scenario(broken)


class ConditionCoverageIsHonest(unittest.TestCase):

    _FIXED_20C = {"temperature_c": {
        "kind": "fixed-reference", "value": 20.0, "units": "C",
        "source": "IEC 60028 reference temperature"}}

    def _link_scenario(self, temperature, conditions):
        return {
            "name": "condition-check",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "link",
                 "nodes": ["in", "out"], "model": "link-model"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out"}],
            "operating_conditions": {"temperature_c": temperature},
        }, fidelity.ModelRegistry([
            _interconnect("link-model", conditions=conditions)])

    def test_simulator_condition_never_covers_undeclared_models(
            self):
        sim_scenario, registry = self._link_scenario(85.0, None)
        coverage = scenario.condition_coverage(registry,
                                               sim_scenario)
        self.assertFalse(coverage["fully_covered"])
        entry = coverage["conditions"]["temperature_c"]["models"][
            "link-model"]
        self.assertEqual(entry["kind"], "undeclared")

    def test_a_fixed_reference_mismatch_is_flagged(self):
        """An 85 C scenario with a 20 C-fixed interconnect model is
        explicitly NOT fully covered, and the flag names the
        mismatch."""
        sim_scenario, registry = self._link_scenario(
            85.0, self._FIXED_20C)
        coverage = scenario.condition_coverage(registry,
                                               sim_scenario)
        self.assertFalse(coverage["fully_covered"])
        entry = coverage["conditions"]["temperature_c"]["models"][
            "link-model"]
        self.assertIs(entry["matches_requested"], False)
        self.assertIn("does NOT represent", entry["detail"])

    def test_a_matching_reference_is_covered(self):
        sim_scenario, registry = self._link_scenario(
            20.0, self._FIXED_20C)
        coverage = scenario.condition_coverage(registry,
                                               sim_scenario)
        self.assertTrue(coverage["fully_covered"])

    def test_a_parameterized_model_covers_its_range_only(self):
        parameterized = {"temperature_c": {
            "kind": "parameterized", "range": [-40.0, 125.0],
            "units": "C", "source": "vendor model card"}}
        sim_scenario, registry = self._link_scenario(85.0,
                                                     parameterized)
        coverage = scenario.condition_coverage(registry,
                                               sim_scenario)
        self.assertTrue(coverage["fully_covered"])
        sim_scenario, registry = self._link_scenario(150.0,
                                                     parameterized)
        coverage = scenario.condition_coverage(registry,
                                               sim_scenario)
        self.assertFalse(coverage["fully_covered"])

    def test_the_result_carries_condition_coverage(self):
        sim_scenario, registry = self._link_scenario(
            85.0, self._FIXED_20C)
        sim_scenario["required_coverage"] = {
            "interconnect_dc": ["geometry-derived"]}
        result = ngspice.run_scenario(registry, sim_scenario,
                                      tempfile.mkdtemp())
        self.assertFalse(
            result["condition_coverage"]["fully_covered"])


class TheRealEngineRunsTransients(unittest.TestCase):

    def test_rc_charge_reaches_the_supply(self):
        """With a real engine present: a 1 kohm / 1 uF lowpass driven
        by 1 V DC sits at ~1 V after 10 time constants, and the
        transient vector is read from the tran plot, not the op
        plot."""
        backend = ngspice.backend_identity()
        if not backend["available"]:
            self.skipTest("no ngspice engine on this machine")
        lowpass = {
            "name": "rc-lowpass-charge",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 1.0},
                {"kind": "resistor", "name": "series",
                 "nodes": ["in", "out"], "value": 1000.0},
                {"kind": "capacitor", "name": "shunt",
                 "nodes": ["out", "0"], "value": 1e-6},
            ],
            "analyses": [{"kind": "op"},
                         {"kind": "tran", "step_s": 1e-5,
                          "stop_s": 1e-2}],
            "measurements": [
                {"name": "settled", "kind": "op_voltage",
                 "node": "out",
                 "assertion": {"op": "within", "value": 1.0,
                               "tolerance": 0.001}},
                {"name": "charged", "kind": "tran_final_voltage",
                 "node": "out",
                 "assertion": {"op": ">=", "value": 0.99}},
            ],
        }
        result = ngspice.run_scenario(
            fidelity.ModelRegistry(), lowpass, tempfile.mkdtemp())
        self.assertEqual(result["status"], "ran")
        self.assertTrue(result["measurements"]["settled"]["passed"])
        self.assertTrue(result["measurements"]["charged"]["passed"])
        self.assertGreaterEqual(
            result["measurements"]["charged"]["value"], 0.99)


class OmissionIsNeverIrrelevance(unittest.TestCase):
    """Every contributor must account for each required phenomenon:
    covered, explicitly not-applicable, or unsupported - silence
    refuses."""

    _REQUIRE_DC = {"interconnect_dc": ["geometry-derived"]}

    def _scenario_with(self, second_model_coverage):
        registry = fidelity.ModelRegistry([
            _interconnect("provider"),
            {"identity": "companion", "kind": "device",
             "coverage": second_model_coverage,
             "provenance": {"source": "test fixture"},
             "spice": ".subckt companion a b\nR1 a b 10.0\n.ends"},
        ])
        sim_scenario = {
            "name": "omission-check",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "link",
                 "nodes": ["in", "mid"], "model": "provider"},
                {"kind": "model_instance", "name": "part",
                 "nodes": ["mid", "out"], "model": "companion"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out"}],
            "required_coverage": dict(self._REQUIRE_DC),
        }
        return registry, sim_scenario

    def test_an_omitting_contributor_blocks_despite_a_provider(
            self):
        registry, sim_scenario = self._scenario_with(
            {"device_electrical": "vendor-spice"})
        report = scenario.contributor_coverage_report(registry,
                                                      sim_scenario)
        self.assertFalse(report["satisfied"])
        phenomenon = report["per_measurement"]["vout"][
            "per_phenomenon"]["interconnect_dc"]
        self.assertEqual(phenomenon["unaccounted"], ["part"])

    def test_explicit_not_applicable_passes(self):
        registry, sim_scenario = self._scenario_with(
            {"device_electrical": "vendor-spice",
             "interconnect_dc": "not-applicable"})
        report = scenario.contributor_coverage_report(registry,
                                                      sim_scenario)
        self.assertTrue(report["satisfied"])
        phenomenon = report["per_measurement"]["vout"][
            "per_phenomenon"]["interconnect_dc"]
        self.assertEqual(phenomenon["not_applicable"], ["part"])

    def test_explicit_unsupported_blocks(self):
        registry, sim_scenario = self._scenario_with(
            {"device_electrical": "vendor-spice",
             "interconnect_dc": "unsupported"})
        report = scenario.contributor_coverage_report(registry,
                                                      sim_scenario)
        self.assertFalse(report["satisfied"])
        phenomenon = report["per_measurement"]["vout"][
            "per_phenomenon"]["interconnect_dc"]
        self.assertIn("part", phenomenon["violating"])

    def test_a_disconnected_model_still_owes_nothing(self):
        """The disposition demand scopes with contribution: a model
        in an unrelated island accounts for nothing here."""
        registry = fidelity.ModelRegistry([
            _interconnect("provider"),
            {"identity": "companion", "kind": "device",
             "coverage": {"device_electrical": "vendor-spice"},
             "provenance": {"source": "test fixture"}},
        ])
        sim_scenario = {
            "name": "island-check",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "link",
                 "nodes": ["in", "out"], "model": "provider"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
                {"kind": "vsource_dc", "name": "src_b",
                 "nodes": ["b_in", "0"], "value": 1.0},
                {"kind": "model_instance", "name": "part",
                 "nodes": ["b_in", "0"], "model": "companion"},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out"}],
            "required_coverage": dict(self._REQUIRE_DC),
        }
        report = scenario.contributor_coverage_report(registry,
                                                      sim_scenario)
        self.assertTrue(report["satisfied"])


class ResultsCarryAUsabilityPolicy(unittest.TestCase):

    _FIXED_20C = {"temperature_c": {
        "kind": "fixed-reference", "value": 20.0, "units": "C",
        "source": "IEC 60028 reference temperature"}}

    def _run(self, temperature):
        registry = fidelity.ModelRegistry([
            _interconnect("link-model",
                          conditions=self._FIXED_20C)])
        sim_scenario = {
            "name": "usability-check",
            "elements": [
                {"kind": "vsource_dc", "name": "src",
                 "nodes": ["in", "0"], "value": 5.0},
                {"kind": "model_instance", "name": "link",
                 "nodes": ["in", "out"], "model": "link-model"},
                {"kind": "resistor", "name": "load",
                 "nodes": ["out", "0"], "value": 50.0},
            ],
            "analyses": [{"kind": "op"}],
            "measurements": [
                {"name": "vout", "kind": "op_voltage",
                 "node": "out",
                 "assertion": {"op": ">=", "value": 4.9}}],
            "operating_conditions": {"temperature_c": temperature},
            "required_coverage": {
                "interconnect_dc": ["geometry-derived"]},
        }
        return ngspice.run_scenario(registry, sim_scenario,
                                    tempfile.mkdtemp())

    def test_condition_incomplete_is_never_design_usable(self):
        """A numerically passing measurement at an unrepresented
        temperature is explicitly unusable for the requested
        condition - no field-weighing left to the agent."""
        result = self._run(85.0)
        policy = result["result_policy"]
        if result["status"] == "ran":
            self.assertTrue(
                policy["numerical_assertions_passed"])
        self.assertIs(
            policy["result_applicable_to_requested_conditions"],
            False)
        self.assertIs(policy["usable_for_design_decision"], False)
        self.assertIs(policy["usable_for_release"], False)

    def test_matching_condition_is_design_usable_when_ran(self):
        result = self._run(20.0)
        policy = result["result_policy"]
        if result["status"] == "ran":
            self.assertIs(policy["usable_for_design_decision"],
                          True)
        self.assertIs(policy["usable_for_release"], False)
