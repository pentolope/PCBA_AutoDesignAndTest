"""The impedance-target solver: one construction, one model, one answer.

These tests attack the containment invariant from outside: a result must
belong to the exact approved construction and analytic model it claims,
every scoped fact must stay in its scope, and everything unmappable must
refuse. The numerical section challenges the closed forms on physics -
trends, limits and symmetries - rather than mirroring their arithmetic.
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa.fabricators import impedance, jlcpcb, model, selection  # noqa: E402
from pcbqa import propagation                                       # noqa: E402
from tests.test_fabricators import _raw_sources                     # noqa: E402


def _snapshot():
    catalog = jlcpcb.parse(_raw_sources())
    return {"normalized": catalog,
            "normalized_sha256": model.normalized_digest(catalog),
            "parser": {"id": "pcbqa.fabricators.jlcpcb",
                       "version": jlcpcb.PARSER_VERSION},
            "retrieved_utc": "2026-08-25T00:00:00+00:00",
            "sources": [{"id": spec["id"], "url": spec["url"],
                         "sha256_raw": "f" * 64}
                        for spec in jlcpcb.SOURCES]}


def _requirements(**overrides):
    base = {"copper_layers": 4, "board_thickness_mm": 1.6,
            "min_track_mm": 0.15, "min_space_mm": 0.15,
            "min_drill_mm": 0.3, "min_via_diameter_mm": 0.45,
            "outer_copper_oz": 1.0, "inner_copper_oz": 0.5,
            "impedance_control": False}
    base.update(overrides)
    return base


def _request(**overrides):
    base = {"requirements": _requirements(),
            "stackup": "JLC-4L-no-requirement",
            "copper_layer": 1,
            "reference_copper_layers": [2],
            "mode": "single-ended",
            "target_ohm": 50.0,
            "width_search_mm": {"min": 0.1, "max": 2.0},
            "soldermask_present": False}
    base.update(overrides)
    return base


class TheSolveBelongsToItsConstruction(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.snapshot = _snapshot()

    def _refuses(self, message, **overrides):
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance.solve(self.snapshot, _request(**overrides))
        self.assertIn(message, str(caught.exception))
        return caught.exception

    def test_a_representative_solve_carries_its_whole_context(self):
        result = impedance.solve(self.snapshot, _request())
        numeric = result["numeric_solution"]
        self.assertTrue(result["geometry_feasible"])
        self.assertTrue(result["manufacturing"]["established"])
        self.assertAlmostEqual(numeric["impedance_ohm"], 50.0, places=2)
        # ~0.37 mm for 50 ohm over 0.2104 mm of er-4.4 prepreg with 1 oz
        # finished copper: the physically expected neighbourhood.
        self.assertTrue(0.30 < numeric["width_mm"] < 0.45, numeric)
        context = result["context"]
        self.assertEqual(context["dielectric_record"],
                         "prepreg 7628 (NP-155F, impedance-calculator)")
        self.assertEqual(context["conductor_record"],
                         "finished_copper_external_1oz_mil")
        self.assertEqual(result["model"]["identity"],
                         "external-microstrip-bare")
        self.assertEqual(result["provenance"]["approved_normalized_sha256"],
                         self.snapshot["normalized_sha256"])
        self.assertEqual(len(result["provenance"]["sources"]),
                         len(jlcpcb.SOURCES))

    def test_an_uncontrolled_profile_claims_no_fabrication_tolerance(self):
        """The base profile states impedance_control=false: the nominal
        estimate is available, but the fabricator's controlled-impedance
        tolerance is explicitly NOT applicable and not quoted as such."""
        result = impedance.solve(self.snapshot, _request())
        tolerance = result["fabrication_tolerance"]
        self.assertFalse(tolerance["impedance_control_selected"])
        self.assertFalse(tolerance["applicable"])
        self.assertNotIn("stated_percent", tolerance)
        self.assertIn("does NOT apply", tolerance["note"])

    def test_a_controlled_profile_exposes_the_applicable_tolerance(self):
        requirements = _requirements(impedance_control=True)
        result = impedance.solve(self.snapshot, _request(
            requirements=requirements, stackup="JLC04161H-7628"))
        tolerance = result["fabrication_tolerance"]
        self.assertTrue(tolerance["impedance_control_selected"])
        self.assertTrue(tolerance["applicable"])
        self.assertEqual(tolerance["stated_percent"], 10.0)
        self.assertIn("nominal analytic estimate", tolerance["note"])

    def test_there_is_exactly_one_public_solve(self):
        """No provenance-free variant exists: every result an AI caller
        can receive carries the evidence chain, on success and on
        no-solution alike."""
        self.assertFalse(hasattr(impedance, "solve_with_provenance"))
        result = impedance.solve(
            self.snapshot, _request(target_ohm=30.0,
                                    width_search_mm={"min": 0.15,
                                                     "max": 0.5}))
        self.assertIsNone(result["numeric_solution"])
        self.assertIn("provenance", result)
        self.assertIn("fabrication_tolerance", result)
        self.assertEqual(result["provenance"]["model_version"],
                         impedance.MODEL_VERSION)

    def test_soldermask_on_an_internal_layer_is_contradictory(self):
        self._refuses("contradicts an internal", copper_layer=2,
                      reference_copper_layers=[1, 3],
                      soldermask_present=True)

    def test_repeated_solves_are_deterministic(self):
        first = impedance.solve(self.snapshot, _request())
        second = impedance.solve(copy.deepcopy(self.snapshot), _request())
        self.assertEqual(first["numeric_solution"],
                         second["numeric_solution"])
        self.assertEqual(first["geometry_feasible"], second["geometry_feasible"])

    def test_the_controlled_range_binds_only_controlled_profiles(self):
        """A controlled profile is held to the fabricator's stated
        target range; an uncontrolled nominal analysis is not - the
        range describes a process nobody ordered, so it rides along as
        information while the model's own validity governs."""
        self._refuses("outside the fabricator's stated single-ended "
                      "controlled-impedance range",
                      requirements=_requirements(impedance_control=True),
                      stackup="JLC04161H-7628", target_ohm=150.0)
        result = impedance.solve(self.snapshot,
                                 _request(target_ohm=150.0))
        self.assertFalse(result["target_range"]["enforced"])
        self.assertIn("does not select", result["target_range"]["note"])
        # 150 ohm is not reachable on this stack in this domain - the
        # honest outcome is no-solution, never a range refusal.
        self.assertIsNone(result["numeric_solution"])
        self.assertIn("no width", result["failure"])

    def test_an_uncontrolled_nominal_can_exceed_the_controlled_range(self):
        """91 ohm sits above the stated 20-90 controlled range.
        Uncontrolled analysis may still compute it - the range is
        information, not a constraint - and the result then says
        exactly what the number is: an analytic root whose width falls
        below the published minimum track, so the geometry is not
        feasible and nothing claims fabrication control."""
        result = impedance.solve(
            self.snapshot, _request(target_ohm=91.0,
                                    width_search_mm={"min": 0.06,
                                                     "max": 2.0}))
        self.assertFalse(result["target_range"]["enforced"])
        self.assertIsNotNone(result["numeric_solution"])
        self.assertLess(result["numeric_solution"]["width_mm"], 0.09)
        self.assertFalse(result["geometry_feasible"])
        self.assertFalse(result["manufacturing"]["established"])
        control = result["fabrication_control"]
        self.assertFalse(control["impedance_control_selected"])
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])
        self.assertIn("nothing here establishes", control["note"])

    def test_differential_refuses_as_unsupported_at_any_target(self):
        self._refuses("differential solving is not implemented",
                      mode="differential", target_ohm=250.0)

    def test_differential_inside_range_is_a_named_unsupported(self):
        self._refuses("differential solving is not implemented",
                      mode="differential", target_ohm=100.0)

    def test_soldermask_makes_the_topology_coated_and_unsupported(self):
        self._refuses("coated-microstrip", soldermask_present=True)

    def test_soldermask_presence_must_be_explicit(self):
        self._refuses("explicitly true or false", soldermask_present=None)

    def test_malformed_targets_refuse(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -50, 0,
                    "50", True):
            with self.assertRaises(impedance.ImpedanceError):
                impedance.solve(self.snapshot,
                                _request(target_ohm=bad))

    def test_malformed_search_bounds_refuse(self):
        for bounds in ({"min": 0.5, "max": 0.1},
                       {"min": float("nan"), "max": 1.0},
                       {"min": 0.1}, "0.1-2.0", None):
            with self.assertRaises(impedance.ImpedanceError):
                impedance.solve(self.snapshot,
                                _request(width_search_mm=bounds))

    def test_an_unknown_request_key_refuses(self):
        self._refuses("does not implement", widthh=0.2)

    def test_missing_reference_planes_refuse(self):
        exc = self._refuses("no reference plane was declared",
                            reference_copper_layers=[])
        self.assertIn("caller must name", str(exc))

    def test_a_non_adjacent_reference_refuses(self):
        self._refuses("not the adjacent copper layer",
                      reference_copper_layers=[3])

    def test_the_inner_layers_of_this_construction_refuse_by_name(self):
        exc = self._refuses("asymmetric-stripline", copper_layer=2,
                            reference_copper_layers=[1, 3])
        self.assertIn("not", str(exc))

    def test_a_single_sided_internal_reference_refuses(self):
        self._refuses("BOTH adjacent copper layers", copper_layer=2,
                      reference_copper_layers=[1])

    def test_an_infeasible_profile_cannot_get_an_impedance_number(self):
        self._refuses("unfabricable",
                      requirements=_requirements(copper_layers=3))

    def test_a_stackup_outside_the_profile_refuses(self):
        self._refuses("exact approved construction",
                      requirements=_requirements(board_thickness_mm=0.8))

    def test_the_family_is_resolved_by_layer_count(self):
        context = impedance.resolve_context(
            self.snapshot, _requirements(), "JLC-4L-no-requirement",
            1, [2], False)
        self.assertEqual(context["material_family"], "NP-155F")

    def test_ten_layer_boards_resolve_the_other_family(self):
        family = impedance._material_family(
            self.snapshot["normalized"]["capabilities"], 10)
        self.assertEqual(family["value"], "S1000-2M")
        family = impedance._material_family(
            self.snapshot["normalized"]["capabilities"], 8)
        self.assertEqual(family["value"], "NP-155F")

    def test_contradictory_family_evidence_refuses(self):
        capabilities = copy.deepcopy(
            self.snapshot["normalized"]["capabilities"])
        extra = dict(capabilities["impedance_core_material 4-8L"])
        extra["value"] = "SOMETHING-ELSE"
        capabilities["impedance_core_material 4-8L (duplicate)"] = extra
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._material_family(capabilities, 4)
        self.assertIn("exactly one must", str(caught.exception))

    def test_generic_dk_cannot_stand_in_for_the_calculator_model(self):
        """Delete the calculator-model prepreg record: the generic
        stackup-page 'prepreg 7628' (same dk, different scope) must NOT
        be picked up - the solve refuses instead."""
        snapshot = copy.deepcopy(self.snapshot)
        del snapshot["normalized"]["materials"][
            "prepreg 7628 (NP-155F, impedance-calculator)"]
        self.assertIn("prepreg 7628", snapshot["normalized"]["materials"])
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance.solve(snapshot, _request())
        self.assertIn("never borrowed", str(caught.exception))

    def test_calculator_dk_does_not_leak_into_the_stackup_export(self):
        document = selection.export_physical_stackup(
            self.snapshot, _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(document["layers"][1]["epsilon_r"], 4.4)
        self.assertEqual(document["layers"][3]["epsilon_r"], 4.6)

    def test_conductor_thickness_follows_position_and_weight(self):
        capabilities = self.snapshot["normalized"]["capabilities"]
        identity, _record, thickness = impedance._conductor_record(
            capabilities, "external", 1.0)
        self.assertEqual(identity, "finished_copper_external_1oz_mil")
        self.assertAlmostEqual(thickness, 1.6 * 0.0254, places=6)
        identity, _record, thickness = impedance._conductor_record(
            capabilities, "internal", 0.5)
        self.assertEqual(identity, "finished_copper_internal_0.5oz_mil")
        self.assertAlmostEqual(thickness, 0.6 * 0.0254, places=6)

    def test_a_weight_without_a_finished_thickness_refuses(self):
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._conductor_record(
                self.snapshot["normalized"]["capabilities"],
                "external", 2.0)
        self.assertIn("0 finished-conductor-thickness",
                      str(caught.exception))

    def test_an_unmapped_core_thickness_refuses(self):
        materials = self.snapshot["normalized"]["materials"]
        layer = {"role": "dielectric", "form": "core",
                 "thickness_mm": 0.09}
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._dielectric_record(materials, "NP-155F", layer, 4)
        self.assertIn("not interpolated", str(caught.exception))

    def test_a_thick_core_maps_to_the_open_ended_record(self):
        materials = self.snapshot["normalized"]["materials"]
        layer = {"role": "dielectric", "form": "core",
                 "thickness_mm": 1.065}
        identity, record = impedance._dielectric_record(
            materials, "NP-155F", layer, 4)
        self.assertIn(">0.7mm", identity)
        self.assertEqual(record["dk"], 4.43)

    def test_contradictory_core_evidence_refuses(self):
        materials = copy.deepcopy(self.snapshot["normalized"]["materials"])
        duplicate = dict(
            materials["core NP-155F 0.13mm (impedance-calculator)"])
        duplicate["dk"] = 9.9
        materials["core NP-155F 0.13mm (duplicate)"] = duplicate
        layer = {"role": "dielectric", "form": "core",
                 "thickness_mm": 0.13}
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._dielectric_record(materials, "NP-155F", layer, 4)
        self.assertIn("contradictory", str(caught.exception))

    def test_manufacturing_limits_prune_numerical_solutions(self):
        """95 ohm is inside no published range... use a target near the
        top of the stated range whose width lands under a synthetic,
        stricter minimum track: the result reports infeasible with the
        limit cited, not a silently returned width."""
        snapshot = copy.deepcopy(self.snapshot)
        for record in snapshot["normalized"]["capabilities"].values():
            if record.get("category") == "trace":
                record["value"] = {"track": 0.4, "space": 0.4}
        requirements = _requirements(min_track_mm=0.4, min_space_mm=0.4)
        result = impedance.solve(
            snapshot, _request(requirements=requirements, target_ohm=60.0))
        # The numeric root is preserved as diagnostics; the DESIGN answer
        # is infeasible, and that is the field a caller must key on.
        self.assertIsNotNone(result["numeric_solution"])
        self.assertFalse(result["geometry_feasible"])
        self.assertFalse(result["manufacturing"]["established"])
        self.assertIn("below the strictest published minimum",
                      result["manufacturing"]["issue"])

    def test_an_unreachable_target_is_a_clear_no_solution(self):
        result = impedance.solve(
            self.snapshot,
            _request(target_ohm=30.0,
                     width_search_mm={"min": 0.15, "max": 0.5}))
        self.assertIsNone(result["numeric_solution"])
        self.assertFalse(result["geometry_feasible"])
        self.assertIn("NOT returned", result["failure"])

    def test_provenance_reaches_the_sources(self):
        result = impedance.solve(self.snapshot, _request())
        ids = {s["id"] for s in result["provenance"]["sources"]}
        self.assertIn("impedance-calculator", ids)
        self.assertEqual(result["provenance"]["model_version"],
                         impedance.MODEL_VERSION)

    def test_cli_exit_status_tracks_feasibility(self):
        """Exit 0 means geometry_feasible - an unambiguous analytic
        root at a manufacturable width; anything else, here an
        unreachable target, exits nonzero. Control state never rides
        in the exit code."""
        import json as json_module
        import subprocess
        import tempfile
        requirements_path = os.path.join(
            tempfile.mkdtemp(prefix="pcbqa_impcli_"), "req.json")
        with open(requirements_path, "w", encoding="utf-8") as handle:
            json_module.dump(_requirements(), handle)
        base = [sys.executable, os.path.join(HERE, "run.py"),
                "fab", "impedance", requirements_path,
                "--stackup", "JLC-4L-no-requirement",
                "--layer", "1", "--references", "2",
                "--width-min", "0.1", "--width-max", "2.0",
                "--soldermask", "absent"]
        feasible = subprocess.run(base + ["--target", "50"],
                                  capture_output=True, text=True, cwd=HERE)
        self.assertEqual(feasible.returncode, 0, feasible.stdout[-500:])
        document = json_module.loads(
            feasible.stdout[feasible.stdout.index("{"):])
        self.assertTrue(document["geometry_feasible"])
        no_solution = subprocess.run(
            [arg if arg not in ("0.1", "2.0") else
             {"0.1": "0.15", "2.0": "0.5"}[arg] for arg in base]
            + ["--target", "30"], capture_output=True, text=True,
            cwd=HERE)
        self.assertNotEqual(no_solution.returncode, 0)
        document = json_module.loads(
            no_solution.stdout[no_solution.stdout.index("{"):])
        self.assertFalse(document["geometry_feasible"])
        self.assertIsNone(document["numeric_solution"])

    def test_a_controlled_solution_is_eligible_but_never_bound(self):
        """Selecting the controlled process makes a feasible target
        ELIGIBLE for controlled fabrication. It does not bind the
        target to any board- or order-side specification - the target
        came from this solver request, and no such binding exists in
        the toolkit - so that fact reads false, never inferred."""
        result = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628"))
        self.assertTrue(result["geometry_feasible"])
        control = result["fabrication_control"]
        self.assertTrue(control["impedance_control_selected"])
        self.assertTrue(
            control["target_eligible_for_controlled_fabrication"])
        self.assertFalse(
            control["target_bound_to_fabrication_specification"])
        self.assertIn("nothing here proves the target has been "
                      "specified to the fabricator", control["note"])
        self.assertNotIn("requested_impedance_established", control)

    def test_an_uncontrolled_root_never_reads_as_established(self):
        result = impedance.solve(self.snapshot, _request())
        self.assertTrue(result["geometry_feasible"])
        control = result["fabrication_control"]
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])
        self.assertFalse(
            control["target_bound_to_fabrication_specification"])
        self.assertNotIn("feasible", result)
        self.assertNotIn("requested_impedance_established", control)

    def test_the_solver_module_performs_no_network_access(self):
        with open(os.path.join(HERE, "pcbqa", "fabricators",
                               "impedance.py"), encoding="utf-8") as handle:
            self.assertNotIn("urllib", handle.read())


class TheClosedFormsBehaveLikePhysics(unittest.TestCase):
    """Trends, limits and symmetries - not mirrored arithmetic."""

    def test_wider_microstrip_means_lower_impedance(self):
        widths = [0.15, 0.25, 0.4, 0.7, 1.2]
        values = [impedance.microstrip_z0(4.4, w, 0.21, 0.04)[0]
                  for w in widths]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_higher_permittivity_means_lower_impedance(self):
        low, _e = impedance.microstrip_z0(3.0, 0.35, 0.21, 0.04)
        high, _e = impedance.microstrip_z0(4.8, 0.35, 0.21, 0.04)
        self.assertGreater(low, high)

    def test_taller_dielectric_means_higher_impedance(self):
        thin, _e = impedance.microstrip_z0(4.4, 0.35, 0.15, 0.04)
        tall, _e = impedance.microstrip_z0(4.4, 0.35, 0.30, 0.04)
        self.assertGreater(tall, thin)

    def test_effective_permittivity_sits_between_air_and_substrate(self):
        _z, eps = impedance.microstrip_z0(4.4, 0.35, 0.21, 0.04)
        self.assertTrue(1.0 < eps < 4.4)

    def test_stripline_effective_permittivity_is_the_substrate(self):
        _z, eps = impedance.stripline_z0(4.4, 0.2, 1.0, 0.03)
        self.assertEqual(eps, 4.4)

    def test_stripline_is_lower_impedance_than_the_same_microstrip(self):
        """Fully embedded in dielectric versus half in air: the stripline
        must come out lower for comparable geometry."""
        micro, _e = impedance.microstrip_z0(4.4, 0.3, 0.21, 0.03)
        strip, _e = impedance.stripline_z0(4.4, 0.3, 2 * 0.21 + 0.03, 0.03)
        self.assertGreater(micro, strip)

    def test_wider_stripline_means_lower_impedance(self):
        widths = [0.1, 0.15, 0.25, 0.4, 0.6]
        values = [impedance.stripline_z0(4.2, w, 1.0, 0.03)[0]
                  for w in widths]
        self.assertEqual(values, sorted(values, reverse=True))

    def test_the_stripline_branches_meet_without_a_cliff(self):
        """The narrow and wide characterisations hand over at
        w/(b-t) = 0.35; the two must agree there within a few percent or
        the split would manufacture a discontinuity."""
        b, t = 1.0, 0.03
        w = 0.35 * (b - t)
        narrow, _e = impedance.stripline_z0(4.2, w * 0.999, b, t)
        wide, _e = impedance.stripline_z0(4.2, w * 1.001, b, t)
        self.assertLess(abs(narrow - wide) / wide, 0.05)

    def test_air_microstrip_matches_the_published_form_anchors(self):
        """Both published Hammerstad branches, anchored in air where the
        expressions are exact and independent of any fitted data: the
        narrow branch at u=0.5 is 60*ln(16.125) and the wide branch at
        u=2 is eta0/(2+1.393+0.667*ln(3.444)). The vanishing conductor
        thickness perturbs u only at the 1e-11 level."""
        import math
        t = 1e-12
        z, eps = impedance.microstrip_z0(1.0 + 1e-12, 0.5, 1.0, t)
        self.assertAlmostEqual(eps, 1.0, places=5)
        self.assertAlmostEqual(z, 60.0 * math.log(16.125), delta=0.01)
        z, _eps = impedance.microstrip_z0(1.0 + 1e-12, 2.0, 1.0, t)
        expected = 376.730313668 / (2.0 + 1.393
                                    + 0.667 * math.log(3.444))
        self.assertAlmostEqual(z, expected, delta=0.01)

    def test_the_hammerstad_branch_seam_is_small_and_monotone(self):
        """The classic Hammerstad pair meets at u=1 with a known ~0.5%
        step. The step must stay small, and it must step DOWNWARD with
        increasing width, so monotonicity - which the solver checks at
        runtime - survives the seam instead of being broken by it."""
        t = 1e-12
        narrow, _e = impedance.microstrip_z0(1.0 + 1e-12, 0.9999999, 1.0, t)
        wide, _e = impedance.microstrip_z0(1.0 + 1e-12, 1.0000001, 1.0, t)
        self.assertGreater(narrow, wide)
        self.assertLess((narrow - wide) / narrow, 0.01)

    def test_symmetric_construction_layers_agree(self):
        """In a synthetic symmetric 4-layer construction the two internal
        layers are geometrically indistinguishable; the resolver must
        produce identical model contexts for them, and the model must
        therefore price them identically."""
        catalog = jlcpcb.parse(_raw_sources())
        synthetic = copy.deepcopy(
            catalog["stackups"]["JLC-4L-no-requirement"])
        for layer in synthetic["layers"]:
            if layer["role"] == "dielectric":
                layer.pop("material", None)
                layer.pop("sheet_count", None)
                layer["form"] = "core"
                layer["thickness_mm"] = 0.2104
        core = dict(catalog["materials"][
            "core NP-155F 0.2mm (impedance-calculator)"])
        core["properties"] = dict(core["properties"],
                                  core_thickness_mm=0.2104)
        catalog["materials"][
            "core NP-155F 0.2104mm (impedance-calculator)"] = core
        catalog["stackups"]["SYN-SYM"] = synthetic
        snapshot = {"normalized": catalog,
                    "normalized_sha256": model.normalized_digest(catalog),
                    "parser": {"id": "x", "version": "0"},
                    "retrieved_utc": "2026-08-25T00:00:00+00:00",
                    "sources": []}
        upper = impedance.resolve_context(
            snapshot, _requirements(), "SYN-SYM", 2, [1, 3], False)
        lower = impedance.resolve_context(
            snapshot, _requirements(), "SYN-SYM", 3, [2, 4], False)
        for key in ("topology", "span_mm", "epsilon_r",
                    "dielectric_record", "conductor_record",
                    "conductor_thickness_mm"):
            self.assertEqual(upper[key], lower[key], key)
        z_upper = impedance._impedance_at(upper, 0.2)
        z_lower = impedance._impedance_at(lower, 0.2)
        self.assertEqual(z_upper, z_lower)
        self.assertEqual(upper["topology"], "symmetric-stripline")

    def test_the_pinned_narrow_stripline_reference_vector(self):
        """Hand-evaluated from the IPC-2141 / Wadell / MT-094 printed
        form Z0 = 60/sqrt(er) * ln(4b/(0.67*pi*(0.8w+t))) at er=4.2,
        w=0.2, b=1.0, t=0.03. The model-1 transcription (an equivalent-
        diameter expansion with an unpinnable 0.51*pi term) produced
        66.91 ohm here and fails this vector."""
        z, _eps = impedance.stripline_z0(4.2, 0.2, 1.0, 0.03)
        self.assertAlmostEqual(z, 67.4183, delta=0.005)

    def test_the_wide_stripline_reference_vector(self):
        """Hand-evaluated from Cohn's wide-strip fringing form at er=4.2,
        w=0.6, b=1.0, t=0.03."""
        z, _eps = impedance.stripline_z0(4.2, 0.6, 1.0, 0.03)
        self.assertAlmostEqual(z, 41.3588, delta=0.005)

    def test_the_microstrip_branch_reference_vectors(self):
        """Hand-evaluated from the published Hammerstad expressions with
        a vanishing conductor: narrow branch at u=0.5, er=4.3 and wide
        branch at u=2, er=4.3."""
        z, eps = impedance.microstrip_z0(4.3, 0.5, 1.0, 1e-12)
        self.assertAlmostEqual(eps, 2.9965, places=3)
        self.assertAlmostEqual(z, 96.371, delta=0.02)
        z, eps = impedance.microstrip_z0(4.3, 2.0, 1.0, 1e-12)
        self.assertAlmostEqual(eps, 3.2736, places=3)
        self.assertAlmostEqual(z, 49.365, delta=0.02)

    @staticmethod
    def _exact_cohn_z0(epsilon_r, width, plate_gap):
        """Cohn's EXACT zero-thickness symmetric stripline, from the
        elliptic-integral solution printed in the Polar Instruments IPC
        paper: Z0 = (eta0/4)/sqrt(er) * K(k)/K(k'), k = sech(pi*w/2b),
        k' = tanh(pi*w/2b). K evaluated by the AGM, implemented HERE so
        the reference is independent of production code."""
        import math

        def complete_k(k):
            a, b = 1.0, math.sqrt(1.0 - k * k)
            for _ in range(60):
                a, b = (a + b) / 2.0, math.sqrt(a * b)
                if abs(a - b) < 1e-15:
                    break
            return math.pi / (2.0 * a)

        argument = math.pi * width / (2.0 * plate_gap)
        k = 1.0 / math.cosh(argument)
        k_prime = math.tanh(argument)
        return (376.730313668 / 4.0) / math.sqrt(epsilon_r) \
            * complete_k(k) / complete_k(k_prime)

    def test_both_stripline_branches_track_the_exact_solution(self):
        """At t -> 0 the implemented branches are compared with Cohn's
        exact elliptic solution. Measured characterisation, pinned here
        as bounds rather than tuned away: the IPC-2141 narrow closed
        form reads 2-4.5% LOW across its window, worst near the
        w/(b-t)=0.35 branch edge; Cohn's wide fringing form tracks
        within 0.6%. (At realistic conductor thicknesses the two
        branches nearly meet at the seam; the seam-and-monotonicity
        behaviour is guarded at solve time.)"""
        t = 1e-9
        for w_over_b in (0.15, 0.25, 0.34):
            approx, _e = impedance.stripline_z0(4.2, w_over_b, 1.0, t)
            exact = self._exact_cohn_z0(4.2, w_over_b, 1.0)
            error = (approx - exact) / exact
            self.assertTrue(-0.05 < error < -0.015,
                            (w_over_b, approx, exact, error))
        for w_over_b in (0.45, 0.6, 0.8):
            approx, _e = impedance.stripline_z0(4.2, w_over_b, 1.0, t)
            exact = self._exact_cohn_z0(4.2, w_over_b, 1.0)
            self.assertLess(abs(approx - exact) / exact, 0.01,
                            (w_over_b, approx, exact))

    def test_stripline_validity_limits_refuse(self):
        with self.assertRaises(propagation.Unsupported):
            impedance.stripline_z0(4.2, 0.2, 1.0, 0.3)      # t/b > 0.25
        with self.assertRaises(propagation.Unsupported):
            impedance.stripline_z0(4.2, 1.1, 1.0, 0.03)     # w >= b
        with self.assertRaises(propagation.Unsupported):
            impedance.microstrip_z0(4.3, 25.0, 1.0, 1e-12)  # u > 20
        with self.assertRaises(propagation.Unsupported):
            impedance.microstrip_z0(4.3, 0.05, 1.0, 1e-12)  # u < 0.1

    @staticmethod
    def _stripline_context(conductor_mm, span_mm=1.0, epsilon_r=4.2):
        """A handcrafted stripline model context for seam experiments."""
        return {"topology": impedance.STRIPLINE,
                "span_mm": span_mm, "epsilon_r": epsilon_r,
                "conductor_thickness_mm": conductor_mm,
                "trapezoid_delta_mm": 0.0}

    def test_direct_evaluation_on_both_sides_of_every_seam(self):
        """The seam locations are analytic facts; the values immediately
        on each side are part of the model contract. At near-zero
        thickness the stripline seam steps UP with width (the known
        narrow-form bias); at t/b = 0.03 the step nearly closes. The
        microstrip u=1 seam steps DOWN, preserving monotonicity."""
        thin = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        below, _e = impedance._impedance_at(thin, seam - 1e-9)
        above, _e = impedance._impedance_at(thin, seam + 1e-9)
        self.assertGreater(above, below)
        self.assertLess((above - below) / below, 0.05)
        thick = self._stripline_context(0.03)
        seam = 0.35 * (1.0 - 0.03)
        below, _e = impedance._impedance_at(thick, seam - 1e-9)
        above, _e = impedance._impedance_at(thick, seam + 1e-9)
        # measured: the step closes from ~3.5% at t->0 to ~1.2% here
        self.assertLess(abs(above - below) / below, 0.02)
        narrow, _e = impedance.microstrip_z0(1.0 + 1e-12, 0.9999999,
                                             1.0, 1e-12)
        wide, _e = impedance.microstrip_z0(1.0 + 1e-12, 1.0000001,
                                           1.0, 1e-12)
        self.assertGreater(narrow, wide)

    def test_a_target_inside_the_seam_overlap_returns_all_roots(self):
        """At tiny thickness the upward seam step means one target is
        reached by two widths - one on each branch. Model v2 bisected
        across the seam and silently returned one of them; model v3
        reports both and refuses to choose."""
        context = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        below, _e = impedance._impedance_at(context, seam - 1e-9)
        above, _e = impedance._impedance_at(context, seam + 1e-9)
        target = (below + above) / 2.0
        roots, _diag = impedance._solve_width(context, 0.1, 0.8, target)
        self.assertEqual(len(roots), 2)
        first, second = roots[0][0], roots[1][0]
        self.assertLess(first, seam)
        self.assertGreater(second, seam)
        for _width, z, _eps in roots:
            self.assertAlmostEqual(z, target, places=6)

    def test_a_target_inside_a_downward_seam_gap_has_no_root(self):
        """Where the seam steps down, the band between the two edge
        values is unreachable: the solver reports zero roots rather
        than converging to the seam and calling it a solution."""
        context = self._stripline_context(0.05)
        seam = 0.35 * (1.0 - 0.05)
        below, _e = impedance._impedance_at(context, seam - 1e-9)
        above, _e = impedance._impedance_at(context, seam + 1e-9)
        if above < below:
            target = (below + above) / 2.0
            roots, _diag = impedance._solve_width(context, 0.1, 0.8,
                                                  target)
            self.assertEqual(len(roots), 0)

    def test_the_full_solve_reports_ambiguity_without_choosing(self):
        """Through the public solve: a synthetic symmetric construction
        with near-zero inner copper puts the seam step in play, and a
        target inside the overlap must come back as ambiguous_roots
        with no numeric_solution."""
        catalog = jlcpcb.parse(_raw_sources())
        synthetic = copy.deepcopy(
            catalog["stackups"]["JLC-4L-no-requirement"])
        for layer in synthetic["layers"]:
            if layer["role"] == "dielectric":
                layer.pop("material", None)
                layer.pop("sheet_count", None)
                layer["form"] = "core"
                layer["thickness_mm"] = 0.2104
        core = dict(catalog["materials"][
            "core NP-155F 0.2mm (impedance-calculator)"])
        core["properties"] = dict(core["properties"],
                                  core_thickness_mm=0.2104)
        catalog["materials"][
            "core NP-155F 0.2104mm (impedance-calculator)"] = core
        catalog["stackups"]["SYN-SYM"] = synthetic
        snapshot = {"normalized": catalog,
                    "normalized_sha256": model.normalized_digest(catalog),
                    "parser": {"id": "x", "version": "0"},
                    "retrieved_utc": "2026-08-25T00:00:00+00:00",
                    "sources": []}
        context = impedance.resolve_context(
            snapshot, _requirements(), "SYN-SYM", 2, [1, 3], False)
        seam = 0.35 * (context["span_mm"]
                       - context["conductor_thickness_mm"])
        below, _e = impedance._impedance_at(context, seam - 1e-9)
        above, _e = impedance._impedance_at(context, seam + 1e-9)
        if above > below:
            target = round((below + above) / 2.0, 4)
            result = impedance.solve(snapshot, _request(
                stackup="SYN-SYM", copper_layer=2,
                reference_copper_layers=[1, 3],
                soldermask_present=False, target_ohm=target,
                width_search_mm={"min": 0.05, "max": 0.4}))
            self.assertIsNone(result["numeric_solution"])
            self.assertEqual(len(result["ambiguous_roots"]), 2)
            self.assertFalse(result["geometry_feasible"])
            self.assertIn("no root is silently chosen",
                          result["failure"])

    def test_a_domain_reaching_invalid_geometry_refuses_cleanly(self):
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance.solve(_snapshot(), _request(
                width_search_mm={"min": 0.005, "max": 2.0}))
        self.assertIn("narrow the domain", str(caught.exception))

    def test_the_exact_stripline_seam_target_is_not_lost(self):
        """The seam point belongs to the wide branch, per the production
        inequality; a target equal to Z at the exact seam resolves to a
        root at the seam instead of falling into an inter-interval
        crack."""
        context = self._stripline_context(0.03)
        seam = 0.35 * (1.0 - 0.03)
        target, _e = impedance._impedance_at(context, seam)
        wide_value, _e = impedance.stripline_z0(
            4.2, seam, 1.0, 0.03, _force_branch="wide")
        self.assertEqual(target, wide_value)
        roots, _diag = impedance._solve_width(context, 0.1, 0.8, target)
        self.assertGreaterEqual(len(roots), 1)
        self.assertTrue(any(abs(width - seam) < 1e-6
                            for width, _z, _eps in roots))

    def test_the_exact_microstrip_seam_target_is_not_lost(self):
        """The Hammerstad u=1 point belongs to the narrow branch
        (u <= 1 -> narrow); a target equal to Z there resolves."""
        context = {"topology": impedance.MICROSTRIP,
                   "height_mm": 0.2104, "epsilon_r": 4.4,
                   "conductor_thickness_mm": 0.04064,
                   "trapezoid_delta_mm": 0.0}
        seams = impedance._seam_positions(context, 0.05, 2.0)
        self.assertEqual(len(seams), 1)
        seam, owner = seams[0]
        self.assertEqual(owner, "left")
        target, _e = impedance._impedance_at(context, seam)
        roots, _diag = impedance._solve_width(context, 0.05, 2.0, target)
        self.assertGreaterEqual(len(roots), 1)
        self.assertTrue(any(abs(width - seam) < 1e-6
                            for width, _z, _eps in roots))

    def test_a_seam_target_with_a_second_root_reports_both(self):
        """At near-zero thickness the upward seam step means the exact
        wide-branch seam value is ALSO reached strictly inside the
        narrow branch; both roots must come back, including the one at
        the seam itself."""
        context = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        target, _e = impedance._impedance_at(context, seam)
        roots, _diag = impedance._solve_width(context, 0.1, 0.8, target)
        self.assertEqual(len(roots), 2)
        self.assertLess(roots[0][0], seam)
        self.assertAlmostEqual(roots[1][0], seam, places=6)

    def test_nearby_but_distinct_roots_are_never_merged(self):
        """Multiplicity is mathematical identity, not width proximity:
        no merging logic exists any more, so two distinct roots survive
        at ANY separation. Here the narrow-branch root sits 2e-5 mm
        inside its branch and the wide-branch root ~0.03 mm past the
        seam; both come back, ordered, distinct."""
        context = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        narrow_value, _e = impedance.stripline_z0(
            4.2, seam - 2e-5, 1.0, 1e-9, _force_branch="narrow")
        roots, _diag = impedance._solve_width(context, 0.1, 0.8,
                                              narrow_value)
        self.assertEqual(len(roots), 2)
        gap = roots[1][0] - roots[0][0]
        self.assertGreater(gap, 0)
        self.assertLess(gap, 0.1)
        self.assertAlmostEqual(roots[0][0], seam - 2e-5, places=6)

    def test_no_point_of_the_domain_is_deleted(self):
        """Ownership splits the domain exactly: a target just below the
        narrow branch's one-sided limit clears the ambiguity band, so
        exactly one root exists - on the wide branch, just past the
        seam - and it is found, not lost in an epsilon crack between
        intervals."""
        context = self._stripline_context(0.03)
        seam = 0.35 * (1.0 - 0.03)
        narrow_limit, _e = impedance.stripline_z0(
            4.2, seam, 1.0, 0.03, _force_branch="narrow")
        roots, _diag = impedance._solve_width(context, 0.1, 0.8,
                                              narrow_limit - 1e-3)
        self.assertEqual(len(roots), 1)
        self.assertGreater(roots[0][0], seam)
        self.assertLess(roots[0][0] - seam, 0.05)

    def test_gapless_result_values_never_carry_nan(self):
        snapshot = _snapshot()
        result = impedance.solve(snapshot, _request(target_ohm=88.0))
        if result["numeric_solution"] is not None:
            for value in (result["numeric_solution"]["width_mm"],
                          result["numeric_solution"]["impedance_ohm"]):
                self.assertEqual(value, value)


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
