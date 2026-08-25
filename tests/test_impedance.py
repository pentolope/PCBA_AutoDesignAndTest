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
        result = impedance.solve_with_provenance(self.snapshot, _request())
        solved = result["solved"]
        self.assertTrue(solved["manufacturing"]["established"])
        self.assertAlmostEqual(solved["impedance_ohm"], 50.0, places=2)
        # ~0.37 mm for 50 ohm over 0.2104 mm of er-4.4 prepreg with 1 oz
        # finished copper: the physically expected neighbourhood.
        self.assertTrue(0.30 < solved["width_mm"] < 0.45, solved)
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
        self.assertEqual(result["fabrication_tolerance"]["stated_percent"],
                         10.0)
        self.assertIn("nominal only",
                      result["fabrication_tolerance"]["note"])

    def test_repeated_solves_are_deterministic(self):
        first = impedance.solve(self.snapshot, _request())
        second = impedance.solve(copy.deepcopy(self.snapshot), _request())
        self.assertEqual(first["solved"], second["solved"])

    def test_targets_outside_the_published_ranges_refuse(self):
        self._refuses("outside the fabricator's stated single-ended range",
                      target_ohm=150.0)
        self._refuses("outside the fabricator's stated differential range",
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
        solved = result["solved"]
        self.assertFalse(solved["manufacturing"]["established"])
        self.assertIn("below the strictest published minimum",
                      solved["manufacturing"]["issue"])

    def test_an_unreachable_target_is_a_clear_no_solution(self):
        result = impedance.solve(
            self.snapshot,
            _request(target_ohm=30.0,
                     width_search_mm={"min": 0.15, "max": 0.5}))
        self.assertIsNone(result["solved"])
        self.assertIn("NOT returned", result["failure"])

    def test_provenance_reaches_the_sources(self):
        result = impedance.solve_with_provenance(self.snapshot, _request())
        ids = {s["id"] for s in result["provenance"]["sources"]}
        self.assertIn("impedance-calculator", ids)
        self.assertEqual(result["provenance"]["model_version"],
                         impedance.MODEL_VERSION)

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
            snapshot, _requirements(), "SYN-SYM", 2, [1, 3], True)
        lower = impedance.resolve_context(
            snapshot, _requirements(), "SYN-SYM", 3, [2, 4], True)
        for key in ("topology", "span_mm", "epsilon_r",
                    "dielectric_record", "conductor_record",
                    "conductor_thickness_mm"):
            self.assertEqual(upper[key], lower[key], key)
        z_upper = impedance._impedance_at(upper, 0.2)
        z_lower = impedance._impedance_at(lower, 0.2)
        self.assertEqual(z_upper, z_lower)
        self.assertEqual(upper["topology"], "symmetric-stripline")

    def test_gapless_result_values_never_carry_nan(self):
        snapshot = _snapshot()
        result = impedance.solve(snapshot, _request(target_ohm=88.0))
        if result["solved"] is not None:
            for value in (result["solved"]["width_mm"],
                          result["solved"]["impedance_ohm"]):
                self.assertEqual(value, value)


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
