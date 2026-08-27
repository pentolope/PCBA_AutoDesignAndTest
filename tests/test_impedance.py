"""The impedance-target solver: one construction, one model, one answer.

These tests attack the containment invariant from outside: a result must
belong to the exact approved construction and analytic model it claims,
every scoped fact must stay in its scope, and everything unmappable must
refuse. The numerical section challenges the closed forms on physics -
trends, limits and symmetries - rather than mirroring their arithmetic.
"""

from __future__ import annotations

import copy
import inspect
import math
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa.fabricators import impedance, jlcpcb, model, selection  # noqa: E402
from pcbqa.fabricators import overlay_reference                     # noqa: E402
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
        """The base profile states impedance_control=false: the process
        tolerance the fabricator publishes exists as a capability, but
        it neither applies to this build nor is quoted as doing so."""
        result = impedance.solve(self.snapshot, _request())
        tolerance = result["fabrication_tolerance"]
        self.assertFalse(tolerance["impedance_control_selected"])
        self.assertTrue(tolerance["process_tolerance_published"])
        self.assertFalse(tolerance["applies_to_this_target"])
        self.assertNotIn("stated_percent", tolerance)
        self.assertNotIn("applicable", tolerance)
        self.assertIn("does NOT apply", tolerance["note"])

    def test_a_controlled_profile_separates_capability_from_applicability(
            self):
        """Three facts, three fields: the fabricator publishes a
        standard controlled-impedance tolerance (with the verbatim
        figure and source), the profile selects that process, and the
        tolerance still does NOT apply to this specific target - no
        board- or order-side specification binds a solver-request
        target, so claiming applicability would outrun the evidence."""
        requirements = _requirements(impedance_control=True)
        result = impedance.solve(self.snapshot, _request(
            requirements=requirements, stackup="JLC04161H-7628"))
        tolerance = result["fabrication_tolerance"]
        self.assertTrue(tolerance["impedance_control_selected"])
        self.assertTrue(tolerance["process_tolerance_published"])
        self.assertFalse(tolerance["applies_to_this_target"])
        self.assertEqual(tolerance["stated_percent"], 10.0)
        self.assertNotIn("applicable", tolerance)
        self.assertIn("not bound into any fabrication specification",
                      tolerance["note"])
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

    def test_design_guidance_separates_use_from_establishment(self):
        """The machine-facing contract: a feasible bare solve is a
        nominal (and also trivially provisional); a coated solve is
        PROVISIONAL ONLY - no nominal exists, the provisional domain
        is the model-only routable intersection, and resolution is
        required before fabrication; a no-solution result grants
        nothing. Policy is carried by structured fields and codes,
        never inferred from prose."""
        bare = impedance.solve(self.snapshot, _request())
        guidance = bare["design_guidance"]
        self.assertTrue(
            guidance["usable_for_autonomous_nominal_design"])
        self.assertTrue(
            guidance["usable_for_autonomous_provisional_layout"])
        self.assertEqual(guidance["confidence_class"],
                         "uncontrolled-analytic-nominal")
        self.assertEqual(guidance["nominal_width_mm"],
                         bare["numeric_solution"]["width_mm"])
        self.assertIsNone(guidance["provisional_width_domain_mm"])
        self.assertFalse(
            guidance["requires_resolution_before_fabrication"])
        actions = guidance["allowed_actions"]
        self.assertTrue(actions["draw_nominal_width"])
        self.assertTrue(actions["reserve_provisional_width_region"])
        self.assertTrue(actions["continue_board_routing"])
        self.assertFalse(actions["mark_target_fabrication_ready"])
        self.assertFalse(actions["release_design"])

        controlled = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628"))
        self.assertEqual(
            controlled["design_guidance"]["confidence_class"],
            "controlled-eligible-analytic-nominal")

        coated = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        guidance = coated["design_guidance"]
        self.assertFalse(
            guidance["usable_for_autonomous_nominal_design"])
        self.assertTrue(
            guidance["usable_for_autonomous_provisional_layout"])
        self.assertEqual(guidance["confidence_class"],
                         "model-interval-reversible-estimate")
        self.assertIsNone(guidance["nominal_width_mm"])
        self.assertEqual(
            guidance["provisional_width_domain_mm"],
            coated["enclosure"]["manufacturing"][
                "model_interval_routable_intersection_mm"])
        self.assertTrue(
            guidance["requires_resolution_before_fabrication"])
        actions = guidance["allowed_actions"]
        self.assertFalse(actions["draw_nominal_width"])
        self.assertTrue(actions["reserve_provisional_width_region"])
        self.assertTrue(actions["continue_board_routing"])
        codes = {item["code"] for item in guidance["not_established"]}
        self.assertLessEqual(
            {"FABRICATION_TARGET_BINDING", "PHYSICAL_COATED_BOUNDS",
             "COATED_POINT_MODEL",
             "FINITE_CONDUCTOR_COATED_VALIDATION"}, codes)

        unsolved = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", target_ohm=30.0,
            width_search_mm={"min": 0.15, "max": 0.5}))
        guidance = unsolved["design_guidance"]
        self.assertFalse(
            guidance["usable_for_autonomous_nominal_design"])
        self.assertFalse(
            guidance["usable_for_autonomous_provisional_layout"])
        self.assertEqual(guidance["confidence_class"], "not-usable")
        actions = guidance["allowed_actions"]
        self.assertFalse(actions["draw_nominal_width"])
        self.assertFalse(actions["reserve_provisional_width_region"])
        self.assertFalse(actions["continue_board_routing"])

    def test_nominal_usability_is_bound_to_a_nominal_width(self):
        """The finding-one invariant, structural: nominal usability
        and the presence of a nominal width are the same fact, in
        every result state."""
        results = (
            impedance.solve(self.snapshot, _request()),
            impedance.solve(self.snapshot,
                            _request(soldermask_present=True)),
            impedance.solve(self.snapshot, _request(
                requirements=_requirements(impedance_control=True),
                stackup="JLC04161H-7628", target_ohm=30.0,
                width_search_mm={"min": 0.15, "max": 0.5})),
        )
        for result in results:
            guidance = result["design_guidance"]
            self.assertEqual(
                guidance["usable_for_autonomous_nominal_design"],
                guidance["nominal_width_mm"] is not None)
            self.assertEqual(
                guidance["allowed_actions"]["draw_nominal_width"],
                guidance["nominal_width_mm"] is not None)

    def test_calibration_record_is_scoped_and_bound(self):
        """The chord characterization lives in an explicit record with
        its measured domain, reference identity and applicability
        flag; the sample-domain figures no longer leak into generic
        prose."""
        coated = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        enclosure = coated["enclosure"]
        record = enclosure["loaded_edge_calibration"]
        self.assertEqual(record["reference_id"],
                         overlay_reference.REFERENCE_ID)
        self.assertIn("not a universal", record["kind"])
        for key in ("substrate_dk", "mask_dk", "substrate_height_mm",
                    "conductor_thickness_mm", "width_mm",
                    "targets_ohm"):
            self.assertIn(key, record["domain"])
        self.assertIs(record["applies_to_this_construction"], True)
        self.assertIn("not", record["width_sensitivity"]["note"])
        out_of_domain = dict(coated["context"], epsilon_r=3.0)
        self.assertFalse(
            impedance._calibration_applicable(out_of_domain))
        self.assertNotIn("0.4", enclosure["note"])
        self.assertNotIn("percent", enclosure["note"])
        meanings = " ".join(
            item["meaning"]
            for item in coated["design_guidance"]["not_established"])
        self.assertNotIn("7 to 50", meanings)
        self.assertNotIn("percent", meanings)

    def test_guidance_never_claims_release_grade(self):
        """Every class, every state: release_grade is false, the
        FABRICATION_TARGET_BINDING code is present, fabrication-ready
        and release actions are denied, and a controlled profile
        cannot upgrade coated provisional guidance into a nominal or
        a fabrication-ready state. Policy reads codes and actions,
        so prose rewording cannot break it."""
        results = (
            impedance.solve(self.snapshot, _request()),
            impedance.solve(self.snapshot, _request(
                requirements=_requirements(impedance_control=True),
                stackup="JLC04161H-7628")),
            impedance.solve(self.snapshot,
                            _request(soldermask_present=True)),
            impedance.solve(self.snapshot, _request(
                requirements=_requirements(impedance_control=True),
                stackup="JLC04161H-7628", soldermask_present=True)),
        )
        for result in results:
            guidance = result["design_guidance"]
            self.assertIs(guidance["release_grade"], False)
            codes = {item["code"]
                     for item in guidance["not_established"]}
            self.assertIn("FABRICATION_TARGET_BINDING", codes)
            actions = guidance["allowed_actions"]
            self.assertFalse(actions["mark_target_fabrication_ready"])
            self.assertFalse(actions["release_design"])
            self.assertIn("REVERSIBLE", guidance["meaning"])
        controlled_coated = results[3]["design_guidance"]
        self.assertEqual(controlled_coated["confidence_class"],
                         "model-interval-reversible-estimate")
        self.assertFalse(
            controlled_coated["usable_for_autonomous_nominal_design"])
        self.assertFalse(controlled_coated["allowed_actions"][
            "draw_nominal_width"])
        self.assertFalse(results[3]["fabrication_control"][
            "target_bound_to_fabrication_specification"])

    def test_a_coated_solve_returns_the_model_enclosure(self):
        """Soldermask on an external layer is the coated topology: no
        point width exists in this model version, and the result
        delivers both MODEL edge solves without presenting them as
        physical bounds."""
        result = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        self.assertEqual(result["model"]["identity"],
                         "external-microstrip-coated")
        self.assertIsNone(result["numeric_solution"])
        self.assertFalse(result["geometry_feasible"])
        self.assertIn("MODEL enclosure", result["failure"])
        enclosure = result["enclosure"]
        self.assertTrue(enclosure["model_enclosure_established"])
        self.assertTrue(enclosure["ordering_verified"])
        lower = enclosure["width_mm"]["lower"]
        upper = enclosure["width_mm"]["upper"]
        self.assertLess(lower, upper)
        for edge in (enclosure["loaded_edge"], enclosure["bare_edge"]):
            self.assertTrue(edge["root_established"])
            self.assertNotIn("manufacturing", edge)
            self.assertAlmostEqual(edge["impedance_ohm"], 50.0,
                                   places=3)
        self.assertGreater(enclosure["loaded_edge"]["epsilon_effective"],
                           enclosure["bare_edge"]["epsilon_effective"])
        manufacturing = enclosure["manufacturing"]
        self.assertTrue(manufacturing["loaded_edge"]["established"])
        self.assertTrue(manufacturing["bare_edge"]["established"])
        self.assertTrue(
            manufacturing["model_interval_has_routable_widths"])
        self.assertEqual(
            manufacturing["model_interval_routable_intersection_mm"],
            {"min": lower, "max": upper})
        bare = impedance.solve(self.snapshot, _request())
        self.assertEqual(upper, bare["numeric_solution"]["width_mm"])
        self.assertIsNone(bare["enclosure"])
        control = result["fabrication_control"]
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])

    def test_model_and_physical_enclosures_cannot_be_conflated(self):
        """The implemented mathematics producing two ordered roots is
        one fact; those roots being physical bounds on the fabricated
        line is another, unestablished one. The schema carries both
        names and no bare `established` flag a reader could mistake
        for the stronger claim."""
        result = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        enclosure = result["enclosure"]
        self.assertTrue(enclosure["model_enclosure_established"])
        self.assertFalse(enclosure["physical_enclosure_established"])
        self.assertNotIn("established", enclosure)
        self.assertIn("MODEL edges", enclosure["physical_note"])
        self.assertIn("No claim is made", enclosure["physical_note"])

    def test_no_superstrate_ordering_is_claimed_anywhere(self):
        """The variational concavity argument orders a chord between
        the TRUE endpoints against the true curve; it transfers
        nothing to the implemented chord, whose bare endpoint is the
        Hammerstad approximation. Neither the model documentation nor
        the result may claim this loaded edge sits above or below the
        true infinite-superstrate response."""
        doc = " ".join(impedance.coated_microstrip_epsilon.__doc__
                       .split())
        self.assertIn("no ordering", doc.lower())
        self.assertIn("either direction", doc.lower())
        self.assertNotIn("reads LOW even against", doc)
        result = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        enclosure = result["enclosure"]
        self.assertFalse(enclosure["physical_enclosure_established"])
        self.assertNotIn("reads LOW", enclosure["physical_note"])

    def test_model_interval_fields_cannot_read_as_physical(self):
        """A mathematically routable MODEL interval must never read as
        a fabrication interval for the unknown physical coated width:
        the old generic names are gone and the note scopes itself to
        the model interval explicitly."""
        result = impedance.solve(self.snapshot,
                                 _request(soldermask_present=True))
        manufacturing = result["enclosure"]["manufacturing"]
        self.assertNotIn("usable", manufacturing)
        self.assertNotIn("routable_intersection_mm", manufacturing)
        self.assertTrue(
            manufacturing["model_interval_has_routable_widths"])
        self.assertIn("MODEL interval only", manufacturing["note"])
        self.assertIn("does NOT identify or bound",
                      manufacturing["note"])

    def test_an_edge_root_with_manufacturing_rejection_is_represented(
            self):
        """85 ohm on this construction: both model roots exist and are
        ordered, the loaded root is below the strictest published
        minimum track and reads rejected, the bare root passes, and
        the interval's routable intersection starts at the published
        minimum - numerical enclosure and manufacturability never
        conflated."""
        result = impedance.solve(self.snapshot, _request(
            target_ohm=85.0, width_search_mm={"min": 0.05, "max": 2.0},
            soldermask_present=True))
        enclosure = result["enclosure"]
        self.assertTrue(enclosure["model_enclosure_established"])
        manufacturing = enclosure["manufacturing"]
        self.assertFalse(manufacturing["loaded_edge"]["established"])
        self.assertTrue(manufacturing["bare_edge"]["established"])
        self.assertTrue(
            manufacturing["model_interval_has_routable_widths"])
        intersection = \
            manufacturing["model_interval_routable_intersection_mm"]
        self.assertEqual(
            intersection["min"],
            manufacturing["loaded_edge"]["minimum_track_mm"])
        self.assertGreater(intersection["min"],
                           enclosure["width_mm"]["lower"])
        self.assertEqual(intersection["max"],
                         enclosure["width_mm"]["upper"])
        self.assertIn("does NOT identify", manufacturing["note"])

    def test_a_reversed_edge_ordering_refuses_the_interval(self):
        """No parameterization inside the guards can reverse the
        loading order - which is why the check must not be assumed
        away: it defends against a future edge-model change.
        Synthetically reversed roots must refuse the interval instead
        of presenting lower/upper by edge name."""
        def reversed_roots(context, low, high, target):
            if context["topology"] == impedance.COATED_MICROSTRIP:
                return [(0.4, 50.0, 4.2)], "synthetic"
            return [(0.3, 50.0, 3.3)], "synthetic"
        with mock.patch.object(impedance, "_solve_width",
                               side_effect=reversed_roots):
            result = impedance.solve(self.snapshot,
                                     _request(soldermask_present=True))
        enclosure = result["enclosure"]
        self.assertTrue(enclosure["loaded_edge"]["root_established"])
        self.assertTrue(enclosure["bare_edge"]["root_established"])
        self.assertFalse(enclosure["ordering_verified"])
        self.assertFalse(enclosure["model_enclosure_established"])
        self.assertNotIn("width_mm", enclosure)
        self.assertNotIn("manufacturing", enclosure)
        self.assertIn("does not exhibit the loading relation",
                      enclosure["failure"])

    def test_a_controlled_coated_solve_is_never_called_eligible(self):
        """The enclosure has no feasible point geometry, so a
        controlled profile cannot make a coated target eligible, and
        the prose agrees."""
        result = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", soldermask_present=True))
        self.assertTrue(
            result["enclosure"]["model_enclosure_established"])
        self.assertFalse(result["geometry_feasible"])
        control = result["fabrication_control"]
        self.assertTrue(control["impedance_control_selected"])
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])
        self.assertIn("NOT eligible", control["note"])

    def test_a_missing_soldermask_material_refuses_the_coated_solve(
            self):
        """The mask permittivity is a consumed catalog fact: without an
        approved soldermask record the coated topology refuses by name,
        while the bare topology on the same snapshot still solves."""
        catalog = jlcpcb.parse(_raw_sources())
        for name in [name for name, record in
                     catalog["materials"].items()
                     if record.get("kind") == "soldermask"]:
            del catalog["materials"][name]
        snapshot = {"normalized": catalog,
                    "normalized_sha256": model.normalized_digest(catalog),
                    "parser": {"id": "x", "version": "0"},
                    "retrieved_utc": "2026-08-25T00:00:00+00:00",
                    "sources": []}
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance.solve(snapshot, _request(soldermask_present=True))
        self.assertIn("soldermask material", str(caught.exception))
        bare = impedance.solve(snapshot, _request())
        self.assertIsNotNone(bare["numeric_solution"])

    def test_soldermask_thicknesses_are_not_consumed(self):
        """The enclosure model states it does not consume the
        fabricator's mask thicknesses; deleting them from the catalog
        must not change one bit of the coated result."""
        reference = impedance.solve(self.snapshot,
                                    _request(soldermask_present=True))
        catalog = jlcpcb.parse(_raw_sources())
        for name in ("soldermask_between_traces_mil",
                     "soldermask_on_copper_mil",
                     "soldermask_on_fr4_mil"):
            self.assertIn(name, catalog["capabilities"])
            del catalog["capabilities"][name]
        snapshot = {"normalized": catalog,
                    "normalized_sha256": model.normalized_digest(catalog),
                    "parser": {"id": "x", "version": "0"},
                    "retrieved_utc": "2026-08-25T00:00:00+00:00",
                    "sources": []}
        stripped = impedance.solve(snapshot,
                                   _request(soldermask_present=True))
        self.assertEqual(reference["enclosure"],
                         stripped["enclosure"])

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
        self.assertIn("ELIGIBLE for controlled fabrication",
                      control["note"])
        self.assertIn("nothing here proves the target has been "
                      "specified to the fabricator", control["note"])
        self.assertNotIn("requested_impedance_established", control)

    def test_tolerance_prose_never_implies_a_value_exists(self):
        """The tolerance record is a pure function of profile and
        catalog - the signature proves it cannot see the numeric
        outcome - so one wording serves every result state and must
        stay true when numeric_solution is null. No note may speak of
        "the value above"."""
        self.assertEqual(
            list(inspect.signature(
                impedance._fabrication_tolerance).parameters),
            ["approved_snapshot", "context"])
        solved = impedance.solve(self.snapshot, _request())
        controlled = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628"))
        unsolved = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", target_ohm=30.0,
            width_search_mm={"min": 0.15, "max": 0.5}))
        rejected = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", target_ohm=90.0,
            width_search_mm={"min": 0.05, "max": 2.0}))
        self.assertIsNotNone(solved["numeric_solution"])
        self.assertIsNotNone(controlled["numeric_solution"])
        self.assertIsNone(unsolved["numeric_solution"])
        self.assertFalse(rejected["geometry_feasible"])
        for result in (solved, controlled, unsolved, rejected):
            note = result["fabrication_tolerance"]["note"]
            self.assertNotIn("value above", note)
            self.assertIn("any solved width", note)

    def test_a_controlled_no_solution_is_not_called_eligible(self):
        """impedance_control=true with no root in the domain: the
        boolean says not eligible, and the prose must agree - selecting
        the process is not eligibility of this target."""
        result = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", target_ohm=30.0,
            width_search_mm={"min": 0.15, "max": 0.5}))
        self.assertIsNone(result["numeric_solution"])
        control = result["fabrication_control"]
        self.assertTrue(control["impedance_control_selected"])
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])
        self.assertIn("NOT eligible", control["note"])
        self.assertNotIn("so it is ELIGIBLE", control["note"])

    def test_a_controlled_manufacturing_rejection_is_not_called_eligible(
            self):
        """The 90-ohm root on this construction is narrower than the
        strictest published minimum track; a controlled profile does
        not make an unroutable width eligible."""
        result = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628", target_ohm=90.0,
            width_search_mm={"min": 0.05, "max": 2.0}))
        self.assertIsNotNone(result["numeric_solution"])
        self.assertFalse(result["manufacturing"]["established"])
        self.assertFalse(result["geometry_feasible"])
        control = result["fabrication_control"]
        self.assertFalse(
            control["target_eligible_for_controlled_fabrication"])
        self.assertIn("NOT eligible", control["note"])

    def test_prose_always_agrees_with_the_boolean_state(self):
        """Rendered through _result directly: for every controlled
        failure shape (no root, ambiguous roots, manufacturing
        rejection) the note says NOT eligible; only a feasible
        controlled geometry reads ELIGIBLE."""
        base = impedance.solve(self.snapshot, _request(
            requirements=_requirements(impedance_control=True),
            stackup="JLC04161H-7628"))
        context = dict(base["context"])
        context["notes"] = []
        request = base["request"]
        shapes = {
            "no-root": dict(numeric=None, manufacturing=None,
                            ambiguous=None, failure="no width"),
            "ambiguous": dict(numeric=None, manufacturing=None,
                              ambiguous=[{"width_mm": 0.1,
                                          "impedance_ohm": 50.0},
                                         {"width_mm": 0.2,
                                          "impedance_ohm": 50.0}],
                              failure="two distinct widths"),
            "rejected": dict(numeric={"width_mm": 0.05,
                                      "impedance_ohm": 50.0,
                                      "epsilon_effective": 3.2},
                             manufacturing={"established": False,
                                            "issue": "too narrow"},
                             ambiguous=None, failure=None),
        }
        for name, shape in shapes.items():
            rendered = impedance._result(context, request,
                                         base["target_range"], **shape)
            control = rendered["fabrication_control"]
            self.assertFalse(
                control["target_eligible_for_controlled_fabrication"],
                name)
            self.assertIn("NOT eligible", control["note"], name)
            self.assertNotIn("so it is ELIGIBLE", control["note"], name)
        feasible = impedance._result(
            context, request, base["target_range"],
            numeric={"width_mm": 0.37, "impedance_ohm": 50.0,
                     "epsilon_effective": 3.2},
            manufacturing={"established": True,
                           "minimum_track_mm": 0.09, "evidence": "x"},
            ambiguous=None, failure=None)
        control = feasible["fabrication_control"]
        self.assertTrue(
            control["target_eligible_for_controlled_fabrication"])
        self.assertIn("ELIGIBLE for controlled fabrication",
                      control["note"])

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
        """Hand-evaluated from the IPC-2141 / Wadell printed form
        Z0 = 60/sqrt(er) * ln(4b/(0.67*pi*(0.8w+t))) at er=4.2 (MT-094
        prints the near-equivalent 1.9b/(0.8w+t) argument; 4/(0.67*pi)
        is 1.9004, so the two agree only to the rounded coefficient),
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
            self.assertNotIn("value above",
                             result["fabrication_tolerance"]["note"])

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

    @staticmethod
    def _microstrip_context():
        """A handcrafted bare-microstrip context for seam experiments."""
        return {"topology": impedance.MICROSTRIP, "height_mm": 0.2104,
                "epsilon_r": 4.4, "conductor_thickness_mm": 0.04064,
                "trapezoid_delta_mm": 0.0}

    def test_an_endpoint_seam_still_owns_its_point_stripline_low(self):
        """seam == low: the wide branch owns the whole domain including
        its first point, and the exact seam target resolves there."""
        context = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        target, _e = impedance.stripline_z0(4.2, seam, 1.0, 1e-9,
                                            _force_branch="wide")
        roots, _diag = impedance._solve_width(context, seam, 0.8, target)
        self.assertEqual(len(roots), 1)
        self.assertLess(abs(roots[0][0] - seam), 1e-9)
        self.assertAlmostEqual(roots[0][1], target, places=6)

    def test_an_endpoint_seam_still_owns_its_point_stripline_high(self):
        """seam == high: the wide branch is reduced to the single point
        it owns, and the exact seam target returns BOTH that point and
        the genuinely distinct narrow-branch root - model 4 dropped the
        endpoint seam from the partition and lost the owned root."""
        context = self._stripline_context(1e-9)
        seam = 0.35 * (1.0 - 1e-9)
        target, _e = impedance.stripline_z0(4.2, seam, 1.0, 1e-9,
                                            _force_branch="wide")
        roots, _diag = impedance._solve_width(context, 0.1, seam, target)
        self.assertEqual(len(roots), 2)
        self.assertLess(roots[0][0], seam)
        self.assertEqual(roots[1][0], seam)
        self.assertEqual(roots[1][1], target)

    def test_an_endpoint_seam_still_owns_its_point_microstrip_low(self):
        """seam == low: the narrow branch is reduced to the single point
        it owns and the exact seam target resolves there EXACTLY -
        model 4 bisected the wide branch's values instead and returned
        a 'root' 0.32 ohm off target here."""
        context = self._microstrip_context()
        (seam, owner), = impedance._seam_positions(context, 0.05, 2.0)
        self.assertEqual(owner, "left")
        target, _e = impedance._impedance_at(context, seam,
                                             _force_branch="narrow")
        roots, _diag = impedance._solve_width(context, seam, 2.0, target)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0][0], seam)
        self.assertEqual(roots[0][1], target)

    def test_a_gap_target_at_an_endpoint_seam_has_no_fabricated_root(
            self):
        """A target strictly inside the downward seam step is attained
        nowhere in [seam, high]; the honest answer is zero roots, never
        a converged width whose impedance misses the target."""
        context = self._microstrip_context()
        (seam, _owner), = impedance._seam_positions(context, 0.05, 2.0)
        narrow, _e = impedance._impedance_at(context, seam,
                                             _force_branch="narrow")
        wide, _e = impedance._impedance_at(context, seam,
                                           _force_branch="wide")
        self.assertLess(wide, narrow)
        target = (narrow + wide) / 2.0
        roots, _diag = impedance._solve_width(context, seam, 2.0, target)
        self.assertEqual(roots, [])

    def test_an_endpoint_seam_still_owns_its_point_microstrip_high(self):
        """seam == high: the closed domain ends on the narrow side and
        the production value at the endpoint is attained there."""
        context = self._microstrip_context()
        (seam, _owner), = impedance._seam_positions(context, 0.05, 2.0)
        target, _e = impedance._impedance_at(context, seam)
        roots, _diag = impedance._solve_width(context, 0.05, seam, target)
        self.assertEqual(len(roots), 1)
        self.assertLess(abs(roots[0][0] - seam), 1e-9)
        self.assertAlmostEqual(roots[0][1], target, places=6)

    def test_roots_inside_the_old_merge_threshold_both_survive(self):
        """Two genuinely distinct roots separated by LESS than the
        removed model-3 merge threshold of 1e-4 mm. Near the seam-step
        minimum (t/b ~ 0.1164) on a thin 0.15 mm span the ambiguity
        band is ~0.029 ohm wide, putting the two roots ~7e-5 mm apart -
        inside the old threshold, far above the 1e-12 mm bisection
        resolution. Multiplicity is mathematical identity: both come
        back."""
        span = 0.15
        thickness = 0.116435 * span
        context = self._stripline_context(thickness, span_mm=span)
        seam = 0.35 * (span - thickness)
        narrow, _e = impedance.stripline_z0(4.2, seam, span, thickness,
                                            _force_branch="narrow")
        wide, _e = impedance.stripline_z0(4.2, seam, span, thickness,
                                          _force_branch="wide")
        self.assertGreater(wide, narrow)
        target = (narrow + wide) / 2.0
        roots, _diag = impedance._solve_width(context, 0.02, 0.12,
                                              target)
        self.assertEqual(len(roots), 2)
        gap = roots[1][0] - roots[0][0]
        self.assertGreater(gap, 0.0)
        self.assertLess(gap, 1e-4)
        for _width, z, _eps in roots:
            self.assertAlmostEqual(z, target, places=6)

    @staticmethod
    def _tie_context():
        """h and t pinned so the u=1 seam is an exact float:
        thickness_corrected_width(TIE_SEAM, h, t) - h == 0.0 to the
        last bit. For most parameter pairs the crossing falls between
        two adjacent floats and no exact tie exists; this pair was
        found by scanning the conductor thickness."""
        return {"topology": impedance.MICROSTRIP, "height_mm": 0.2104,
                "epsilon_r": 4.4, "conductor_thickness_mm": 0.04,
                "trapezoid_delta_mm": 0.0}

    TIE_SEAM = 0.16770473581954828

    def test_seam_discovery_reports_an_interior_seam(self):
        context = self._microstrip_context()
        (seam, owner), = impedance._seam_positions(context, 0.05, 2.0)
        self.assertEqual(owner, "left")
        self.assertGreater(seam, 0.05)
        self.assertLess(seam, 2.0)
        self.assertLess(abs(propagation.thickness_corrected_width(
            seam, 0.2104, 0.04064) - 0.2104), 1e-9)

    def test_seam_discovery_includes_the_low_endpoint(self):
        """A caller whose domain starts exactly at the reported seam
        still gets the seam back, exactly, because discovery brackets
        from a fixed anchor and is clamped into the closed domain.
        Premise: this context's reported seam sits on the narrow side
        of the true crossing, so it is a domain point the partition
        must keep."""
        context = self._microstrip_context()
        (seam, _o), = impedance._seam_positions(context, 0.05, 2.0)
        self.assertLess(propagation.thickness_corrected_width(
            seam, 0.2104, 0.04064) - 0.2104, 0.0)
        self.assertEqual(impedance._seam_positions(context, seam, 2.0),
                         [(seam, "left")])

    def test_seam_discovery_includes_an_exact_tie_at_either_endpoint(
            self):
        """An exact u=1 tie at a domain endpoint IS the seam at that
        endpoint. The v5 gate tested strict inequality at its bracket
        ends and silently dropped the tie-at-high case; discovery is
        now decided at the domain endpoints themselves."""
        context = self._tie_context()
        self.assertEqual(propagation.thickness_corrected_width(
            self.TIE_SEAM, 0.2104, 0.04) - 0.2104, 0.0)
        self.assertEqual(
            impedance._seam_positions(context, 0.05, self.TIE_SEAM),
            [(self.TIE_SEAM, "left")])
        self.assertEqual(
            impedance._seam_positions(context, self.TIE_SEAM, 2.0),
            [(self.TIE_SEAM, "left")])
        (interior, _o), = impedance._seam_positions(context, 0.05, 2.0)
        self.assertLess(abs(interior - self.TIE_SEAM), 1e-9)

    def test_seam_discovery_reports_nothing_outside_the_domain(self):
        context = self._microstrip_context()
        (seam, _o), = impedance._seam_positions(context, 0.05, 2.0)
        self.assertEqual(
            impedance._seam_positions(context, seam + 0.01, 2.0), [])
        self.assertEqual(
            impedance._seam_positions(context, 0.05, seam - 0.01), [])
        strip = self._stripline_context(0.03)
        strip_seam = 0.35 * (1.0 - 0.03)
        self.assertEqual(
            impedance._seam_positions(strip, strip_seam + 0.01, 0.8),
            [])

    def test_seam_discovery_includes_stripline_domain_endpoints(self):
        """The same closed-domain rule, asserted on _seam_positions
        itself for the closed-form stripline seam - no topology is
        special-cased."""
        context = self._stripline_context(0.03)
        seam = 0.35 * (1.0 - 0.03)
        self.assertEqual(impedance._seam_positions(context, seam, 0.8),
                         [(seam, "right")])
        self.assertEqual(impedance._seam_positions(context, 0.1, seam),
                         [(seam, "right")])

    def test_an_exact_tie_seam_at_low_is_the_owned_point(self):
        """Tie at low, owner narrow: the partition reduces the narrow
        branch to the single owned point and the exact value there is
        attained exactly."""
        context = self._tie_context()
        target, _e = impedance._impedance_at(context, self.TIE_SEAM,
                                             _force_branch="narrow")
        production, _e = impedance._impedance_at(context, self.TIE_SEAM)
        self.assertEqual(target, production)
        roots, _diag = impedance._solve_width(context, self.TIE_SEAM,
                                              2.0, target)
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0][0], self.TIE_SEAM)
        self.assertEqual(roots[0][1], target)

    def test_an_exact_tie_seam_at_high_is_owned_and_attained(self):
        """Tie at high, owner narrow: the whole closed domain is the
        narrow branch and the endpoint value is attained at the
        endpoint."""
        context = self._tie_context()
        target, _e = impedance._impedance_at(context, self.TIE_SEAM)
        roots, _diag = impedance._solve_width(context, 0.05,
                                              self.TIE_SEAM, target)
        self.assertEqual(len(roots), 1)
        self.assertLess(abs(roots[0][0] - self.TIE_SEAM), 1e-9)
        self.assertAlmostEqual(roots[0][1], target, places=6)

    def test_the_loaded_chord_model_regression_vectors(self):
        """Regression vectors for the DECLARED loaded-chord model:
        hand arithmetic of this toolkit's own assumption, pinning the
        transcription against drift. These are NOT source-pinned
        coated-microstrip physics - no printed source stands behind
        the chord's interior. At er=4.3, em=3.8, vanishing conductor:
        q = (eps_bare-1)/3.3, then eps = q*4.3 + (1-q)*3.8 under each
        z_air branch; narrow branch at u=0.5 (eps_bare 2.9965 ->
        4.1025), wide branch at u=2.0 (eps_bare 3.2736 -> 4.1445)."""
        z, eps = impedance.coated_microstrip_z0(4.3, 3.8, 0.5, 1.0,
                                                1e-12)
        self.assertAlmostEqual(eps, 4.1025, places=3)
        self.assertAlmostEqual(z, 82.363, delta=0.005)
        z, eps = impedance.coated_microstrip_z0(4.3, 3.8, 2.0, 1.0,
                                                1e-12)
        self.assertAlmostEqual(eps, 4.1445, places=3)
        self.assertAlmostEqual(z, 43.874, delta=0.005)

    def test_the_coated_form_anchors_are_exact(self):
        """The two provable anchors of the two-media reading: a unit
        mask reproduces the bare model and a substrate-matched mask
        reproduces the homogeneous medium."""
        for width in (0.5, 2.0):
            bare_z, bare_eps = impedance.microstrip_z0(4.3, width, 1.0,
                                                       1e-12)
            unit_z, unit_eps = impedance.coated_microstrip_z0(
                4.3, 1.0, width, 1.0, 1e-12)
            self.assertAlmostEqual(unit_eps, bare_eps, places=12)
            self.assertAlmostEqual(unit_z, bare_z, places=12)
            _z, full_eps = impedance.coated_microstrip_z0(
                4.3, 4.3, width, 1.0, 1e-12)
            self.assertAlmostEqual(full_eps, 4.3, places=12)

    def test_the_coated_edge_reads_below_bare_everywhere(self):
        """A mask permittivity above 1 loads the line at every width on
        both branches; the coated reading is strictly below bare and
        strictly decreasing in width."""
        previous = None
        for width in (0.2, 0.5, 0.8, 1.2, 2.0, 3.0):
            bare_z, _e = impedance.microstrip_z0(4.4, width, 1.0, 0.03)
            coated_z, _e = impedance.coated_microstrip_z0(
                4.4, 3.8, width, 1.0, 0.03)
            self.assertLess(coated_z, bare_z)
            if previous is not None:
                self.assertLess(coated_z, previous)
            previous = coated_z

    def test_the_coated_mask_window_is_enforced(self):
        """Outside 1 <= mask Dk <= substrate Dk the monotone inverse is
        not established and the form refuses; a unit substrate has no
        filling fraction to decompose."""
        with self.assertRaises(propagation.Unsupported) as caught:
            impedance.coated_microstrip_z0(4.3, 4.5, 0.5, 1.0, 1e-12)
        self.assertIn("mask Dk <= substrate", str(caught.exception))
        with self.assertRaises(propagation.Unsupported):
            impedance.coated_microstrip_z0(4.3, 0.9, 0.5, 1.0, 1e-12)
        with self.assertRaises(propagation.Unsupported) as caught:
            impedance.coated_microstrip_z0(1.0, 1.0, 0.5, 1.0, 1e-12)
        self.assertIn("epsilon_r > 1", str(caught.exception))

    def test_the_coated_context_shares_the_bare_seam_exactly(self):
        """One seam serves both external models: the coated context
        reports bit-identical seam positions and owners, because the
        mask factor is continuous in width and creates no inverse
        branch of its own."""
        bare = self._microstrip_context()
        coated = dict(bare, topology=impedance.COATED_MICROSTRIP,
                      epsilon_mask=3.8)
        self.assertEqual(impedance._seam_positions(bare, 0.05, 2.0),
                         impedance._seam_positions(coated, 0.05, 2.0))

    def test_the_coated_edge_honors_the_seam_gap(self):
        """The downward u=1 step exists on the coated edge exactly as
        on the bare model; a target strictly inside the gap has no
        root, never a converged width that misses the target."""
        context = dict(self._microstrip_context(),
                       topology=impedance.COATED_MICROSTRIP,
                       epsilon_mask=3.8)
        (seam, _o), = impedance._seam_positions(context, 0.05, 2.0)
        narrow, _e = impedance._impedance_at(context, seam,
                                             _force_branch="narrow")
        wide, _e = impedance._impedance_at(context, seam,
                                           _force_branch="wide")
        self.assertLess(wide, narrow)
        roots, _diag = impedance._solve_width(context, seam, 2.0,
                                              (narrow + wide) / 2.0)
        self.assertEqual(roots, [])

    def test_unknown_topology_dispatch_refuses(self):
        """The third topology made dispatch explicit: an unenumerated
        topology refuses at every dispatch site instead of falling
        through a catch-all else into the wrong formula."""
        context = dict(self._microstrip_context(),
                       topology="bogus-topology")
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._impedance_at(context, 0.5)
        self.assertIn("no model dispatch", str(caught.exception))
        with self.assertRaises(impedance.ImpedanceError) as caught:
            impedance._seam_positions(context, 0.05, 2.0)
        self.assertIn("no seam dispatch", str(caught.exception))

    def test_unsupported_topologies_no_longer_name_coated(self):
        self.assertNotIn("coated-microstrip (soldermask present)",
                         impedance.UNSUPPORTED_TOPOLOGIES)
        self.assertIn("asymmetric-stripline",
                      impedance.UNSUPPORTED_TOPOLOGIES)

    def test_gapless_result_values_never_carry_nan(self):
        snapshot = _snapshot()
        result = impedance.solve(snapshot, _request(target_ohm=88.0))
        if result["numeric_solution"] is not None:
            for value in (result["numeric_solution"]["width_mm"],
                          result["numeric_solution"]["impedance_ohm"]):
                self.assertEqual(value, value)


class TheOverlayReferenceIsPinnedToItsPaper(unittest.TestCase):
    """Barbuto et al. (COMPEL 32(6), 2013), held to its own letter.

    Anchor values are read off the paper's own figures, with the
    plot-reading tolerance stated per assertion; the pin vectors are
    this module's exact arithmetic, guarding the transcription
    against drift. The reference is evidence only - the last test
    proves production neither imports it nor changed a digit.
    """

    def test_the_immersed_equation_reproduces_the_papers_figures(self):
        """Equation (8) against the paper's largest-thickness plotted
        readings, asymptotically consistent with its stated limiting
        behavior: Figure 4 (er=10, erc=2, w/d=1) and Figure 7 (er=4,
        erc=1.06, w/d=1), both on the Corr = 1 branch."""
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(10.0, 2.0, 1.0),
            7.14, delta=0.03)
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(4.0, 1.06, 1.0),
            2.951, delta=0.01)

    def test_the_transcription_pin_vectors(self):
        """Exact arithmetic of the transcribed constants, pinned to
        twelve digits against drift."""
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(10.0, 2.0, 1.0),
            7.153776408148, places=10)
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(2.0, 4.0, 4.0),
            2.452744317415, places=10)
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(4.0, 1.06, 1.0),
            2.954012829995, places=10)
        self.assertAlmostEqual(
            overlay_reference.immersed_epsilon(2.0, 20.0, 10.0),
            4.329681813209, places=10)

    def test_the_matched_immersion_is_exact(self):
        """eps_rc = eps_r kills the correction term outright."""
        self.assertEqual(
            overlay_reference.immersed_epsilon(4.3, 4.3, 1.7), 4.3)

    def test_the_air_limit_is_the_papers_not_hammerstads(self):
        """The paper's equation (9): k1 = 0.52 where the classic
        Hammerstad coefficient is exactly one half. The bare limits of
        the reference and of production are DIFFERENT families - a
        recorded obstacle to promoting the reference into the coated
        edge, asserted here so it cannot be forgotten."""
        for width in (0.5, 2.0):
            base = (1.0 + 12.0 / width) ** -0.5
            shape = base if width >= 1.0 else base + 0.04 * (1.0 - width)
            expected = (4.3 + 1.0) / 2.0 + 0.52 * 3.3 * shape
            value = overlay_reference.immersed_epsilon(4.3, 1.0, width)
            self.assertAlmostEqual(value, expected, places=12)
            hammerstad = propagation.hammerstad_effective_permittivity(
                4.3, width, 1.0)
            self.assertGreater(abs(value - hammerstad), 0.01)

    def test_the_cover_limits_hold_for_both_variants(self):
        """The paper's two stated constraints on equation (10): the
        cover permittivity is 1 at zero thickness and eps_rc in the
        infinite-thickness limit - true for the printed and the
        figure-consistent variant alike."""
        for cover in (overlay_reference.cover_epsilon_as_printed,
                      overlay_reference.cover_epsilon_figure_consistent):
            self.assertEqual(cover(3.8, 0.0, 1.7), 1.0)
            self.assertAlmostEqual(cover(3.8, 1e9, 1.7), 3.8, places=5)

    def test_the_printed_cover_exponent_disagrees_with_the_figures(self):
        """The printed-vs-figure inconsistency, quantified on the
        paper's own plotted curves: the printed positive w/d exponent
        misses every mid-thickness anchor by about 0.2 in effective
        permittivity while the sign-reversed CANDIDATE correction
        lands within plot-reading precision - Figures 6, 8 and 10,
        the last at the eps_rc/eps_r = 10 validity edge. Both
        variants stay pinned; neither is adopted; the w/d < 1 cover
        branch has no figure to test at all."""
        anchors = (
            (2.0, 4.0, 4.0, 0.5, 2.01),
            (2.0, 4.0, 4.0, 1.0, 2.135),
            (2.0, 4.0, 10.0, 1.0, 2.045),
            (2.0, 20.0, 10.0, 8.0, 4.05),
        )
        for er, erc, width, cover, plotted in anchors:
            consistent = \
                overlay_reference.covered_epsilon_figure_consistent(
                    er, erc, width, cover)
            printed = overlay_reference.covered_epsilon_as_printed(
                er, erc, width, cover)
            self.assertLess(abs(consistent - plotted), 0.03)
            self.assertGreater(abs(printed - plotted), 0.15)

    def test_the_validity_window_refuses(self):
        """Equation (11) enforced fail-closed, plus the physical
        domain."""
        for arguments in ((4.3, 3.8, 0.0), (4.3, 3.8, 10.5),
                          (1.0, 10.5, 1.0), (4.3, 0.9, 1.0)):
            with self.assertRaises(propagation.Unsupported):
                overlay_reference.immersed_epsilon(*arguments)
        with self.assertRaises(propagation.Unsupported):
            overlay_reference.cover_epsilon_as_printed(3.8, -0.1, 1.7)

    def test_the_covered_ratio_window_binds_the_original_materials(
            self):
        """Equation (11) speaks about the materials, not about the
        thickness-reduced equivalent: a cover with eps_rc/eps_r above
        10 refuses at ANY thickness, however thin, in both cover
        variants, while the ratio exactly 10 is inside the stated fit
        range."""
        for covered in (
                overlay_reference.covered_epsilon_as_printed,
                overlay_reference.covered_epsilon_figure_consistent):
            value = covered(2.0, 20.0, 10.0, 8.0)
            self.assertGreater(value, 1.0)
            for thickness in (0.01, 1.0, 100.0):
                with self.assertRaises(propagation.Unsupported):
                    covered(1.5, 15.1, 4.0, thickness)
                with self.assertRaises(propagation.Unsupported):
                    covered(2.0, 20.2, 4.0, thickness)

    def test_non_finite_reference_inputs_refuse(self):
        """NaN, the infinities and bools refuse at every reference
        entry point instead of propagating into the output."""
        nan, inf = float("nan"), float("inf")
        for arguments in ((inf, 2.0, 1.0), (nan, 2.0, 1.0),
                          (4.3, inf, 1.0), (4.3, nan, 1.0),
                          (4.3, 2.0, nan), (4.3, 2.0, inf),
                          (True, 2.0, 1.0), (4.3, True, 1.0),
                          (4.3, 2.0, True)):
            with self.assertRaises(propagation.Unsupported):
                overlay_reference.immersed_epsilon(*arguments)
        for cover in (overlay_reference.cover_epsilon_as_printed,
                      overlay_reference.cover_epsilon_figure_consistent):
            for arguments in ((3.8, nan, 1.7), (3.8, inf, 1.7),
                              (3.8, -inf, 1.7), (3.8, True, 1.7),
                              (nan, 1.0, 1.7), (3.8, 1.0, nan)):
                with self.assertRaises(propagation.Unsupported):
                    cover(*arguments)
        for covered in (
                overlay_reference.covered_epsilon_as_printed,
                overlay_reference.covered_epsilon_figure_consistent):
            with self.assertRaises(propagation.Unsupported):
                covered(4.3, 3.8, 1.7, nan)
            with self.assertRaises(propagation.Unsupported):
                covered(4.3, nan, 1.7, 1.0)

    def test_the_equation_eight_asymptotic_consistency(self):
        """An equation (8) ASYMPTOTIC CONSISTENCY check on the
        eps_r < eps_rc branch that never touches equation (10) - not
        a direct figure anchor, because no figure plots the infinite
        superstrate itself. The paper's constraint (2) makes equation
        (8) at the material eps_rc the stated limit of its covered
        curves, and Figure 5's largest plotted thickness (dc/d about
        9, curve and full-wave both reading about 2.40) must
        therefore sit BELOW that limit by only the remaining ArcTan
        convergence. Plot-reading evidence only; the exact arithmetic
        pin vectors live in test_the_transcription_pin_vectors."""
        asymptote = overlay_reference.immersed_epsilon(2.0, 4.0, 4.0)
        plotted_at_largest_thickness = 2.40
        self.assertGreater(asymptote, plotted_at_largest_thickness)
        self.assertLess(asymptote - plotted_at_largest_thickness, 0.08)

    def test_the_reference_has_no_conductor_thickness_parameter(self):
        """Barbuto's design chain has no finished copper thickness and
        no trapezoid: the signatures lock that in, so a thickness can
        never be threaded through quietly - the mapping from
        finite-thickness geometry to w/d stays a declared, separate
        decision and a recorded promotion blocker."""
        expected = {
            overlay_reference.immersed_epsilon:
                ["epsilon_r", "epsilon_rc", "w_over_d"],
            overlay_reference.cover_epsilon_as_printed:
                ["epsilon_rc", "dc_over_d", "w_over_d"],
            overlay_reference.cover_epsilon_figure_consistent:
                ["epsilon_rc", "dc_over_d", "w_over_d"],
            overlay_reference.covered_epsilon_as_printed:
                ["epsilon_r", "epsilon_rc", "w_over_d", "dc_over_d"],
            overlay_reference.covered_epsilon_figure_consistent:
                ["epsilon_r", "epsilon_rc", "w_over_d", "dc_over_d"],
        }
        for function, parameters in expected.items():
            self.assertEqual(
                list(inspect.signature(function).parameters),
                parameters)
            for name in parameters:
                self.assertNotIn("conductor", name)
                self.assertNotIn("thickness", name)

    def test_the_reference_is_decoupled_from_the_catalog(self):
        """The reference knows nothing of JLCPCB: no catalog, parser
        or selection import, so no convenience mapping of the
        fabricator's three mask thicknesses onto the single uniform
        dc can creep in unnoticed."""
        source = inspect.getsource(overlay_reference)
        for name in ("jlcpcb", "selection", "catalog_model",
                     "import model", "soldermask_"):
            self.assertNotIn(name, source)

    def test_the_source_artifact_is_fingerprinted(self):
        """Provenance identity: the exact supplied PDF is identified
        by hash, size and page identity, and every adjudicating
        render is fingerprinted under the recorded recipe."""
        artifact = overlay_reference.SOURCE_ARTIFACT
        self.assertEqual(len(artifact["sha256"]), 64)
        self.assertTrue(set(artifact["sha256"]) <= set(
            "0123456789abcdef"))
        self.assertEqual(artifact["bytes"], 131045)
        self.assertEqual(artifact["page_count"], 13)
        self.assertEqual(artifact["doi"], "10.1108/COMPEL-10-2012-0283")
        self.assertEqual(artifact["published_pages"], "1855-1867")
        self.assertEqual(len(overlay_reference.TRANSCRIPTION_RENDERS),
                         4)
        for render in overlay_reference.TRANSCRIPTION_RENDERS:
            self.assertEqual(len(render["sha256"]), 64)
            self.assertTrue(set(render["sha256"]) <= set(
                "0123456789abcdef"))
            self.assertIsInstance(render["page_index"], int)
            self.assertIsInstance(render["dpi"], int)
            self.assertEqual(len(render["clip"]), 4)
            for fraction in render["clip"]:
                self.assertGreaterEqual(fraction, 0.0)
                self.assertLessEqual(fraction, 1.0)
            self.assertIn("role", render)

    def test_the_render_recipe_scopes_its_own_claim(self):
        """The recipe records renderer identity, call shape, clip
        convention, colorspace, alpha, annotation handling and what
        the SHA-256 covers - and the module says these are recorded
        fingerprints of the images judged, not byte-reproducible
        artifacts, because raster output is not stable across
        renderer versions."""
        recipe = overlay_reference.RENDER_RECIPE
        for key in ("renderer", "call", "clip_convention", "colorspace",
                    "alpha", "annotations", "sha256_of"):
            self.assertIn(key, recipe)
        self.assertIn("pymupdf", recipe["renderer"])
        self.assertIn("MuPDF", recipe["renderer"])
        self.assertIs(recipe["alpha"], False)
        self.assertEqual(recipe["colorspace"], "RGB")
        self.assertIn("PNG file bytes", recipe["sha256_of"])
        self.assertIn("no rounding", recipe["clip_convention"])
        doc = " ".join(overlay_reference.__doc__.split())
        self.assertIn("RECORDED FINGERPRINTS", doc)
        self.assertIn("not claimed to be byte-reproducible", doc)

    def test_no_confirmed_erratum_is_claimed(self):
        """The evidence establishes a printed-vs-figure inconsistency
        and a candidate sign correction - not an erratum as an
        existing object. Neither module may treat one as established:
        the phrases that would ("internal erratum", "the erratum",
        "erratum remains") are banned outright, and the scoped
        statement must be present verbatim."""
        for module in (overlay_reference, impedance):
            source = inspect.getsource(module)
            for banned in ("internal erratum", "documented erratum",
                           "the erratum", "erratum remains",
                           "established erratum"):
                self.assertNotIn(banned, source)
        doc = " ".join(overlay_reference.__doc__.split())
        self.assertIn("printed-vs-figure inconsistency", doc)
        self.assertIn("candidate", doc.lower())
        impedance_doc = " ".join(
            inspect.getsource(impedance).split())
        self.assertIn("no erratum is established", impedance_doc)

    def test_reference_and_production_versions_are_separate(self):
        """Production MODEL_VERSION moves only when the production
        model or its composition changes meaning (version 12 added
        the design-guidance contract and recorded the promotion
        decision); the reference carries its own REFERENCE_VERSION.
        The identity is bound mechanically: the exact
        (revision, artifact-hash) pair is pinned, and REFERENCE_ID is
        composed from that same pair so the two can never drift
        apart."""
        self.assertEqual(impedance.MODEL_VERSION, "13")
        expected_sha = ("618bce2839878e7725f14ec7264d70a666116e5057"
                        "44290d0b4d953714e4cad4")
        self.assertEqual(
            (overlay_reference.REFERENCE_VERSION,
             overlay_reference.SOURCE_ARTIFACT["sha256"]),
            ("5", expected_sha))
        self.assertEqual(
            overlay_reference.REFERENCE_ID,
            "barbuto-2013-overlay+r5+sha256:" + expected_sha)
        self.assertEqual(
            impedance._CHORD_CALIBRATION["reference_id"],
            overlay_reference.REFERENCE_ID)
        source = inspect.getsource(impedance)
        self.assertIn("REFERENCE_VERSION", source)
        self.assertIn("PRODUCTION", source)

    def test_the_chord_is_measured_against_the_pinned_reference(self):
        """The promotion decision's first measurement: the production
        chord and Barbuto equation (8) agree in effective permittivity
        within 0.4 percent at matched zero-thickness widths across
        this fabricator's substrate range - they are the same
        functional form up to k1 and the sub-unity shape term. The
        chord is therefore characterized, not floating."""
        worst = 0.0
        for er in (3.91, 4.4, 4.6):
            for width in (0.06, 0.1, 0.15, 0.21, 0.2104, 0.25, 0.37,
                          0.6, 1.0, 1.6):
                bare = propagation.hammerstad_effective_permittivity(
                    er, width, 0.2104)
                q = (bare - 1.0) / (er - 1.0)
                chord = q * er + (1.0 - q) * 3.8
                pinned = overlay_reference.immersed_epsilon(
                    er, 3.8, width / 0.2104)
                worst = max(worst, abs(chord - pinned) / pinned)
        self.assertLess(
            worst,
            impedance._CHORD_CALIBRATION["epsilon_agreement_bound"])
        self.assertGreater(worst, 0.0005)

    def test_the_sensitivity_decomposition_isolates_each_leg(self):
        """The promotion decision's second measurement, decomposed so
        the two effects cannot be conflated. A = thickness-aware
        production chord; B = zero-thickness chord in the SAME family;
        C = zero-thickness Barbuto equation (8). Measured over the
        calibration domain: the A-to-B leg (thickness/width-convention
        sensitivity - a model/convention measurement, NOT a physical
        error bound) moves the loaded width by 5 to 60 percent; the
        B-to-C leg (model family) stays under 0.5 percent; and the
        total A-to-C hypothetical-promotion shift is dominated by the
        first leg."""
        def z_air(u):
            if u <= 1.0:
                return 60.0 * math.log(8.0 / u + u / 4.0)
            return 376.730313668 / (
                u + 1.393 + 0.667 * math.log(u + 1.444))

        def solve(z_of_w, target):
            low, high = 0.05, 2.0
            for _ in range(80):
                middle = (low + high) / 2.0
                if z_of_w(middle) > target:
                    low = middle
                else:
                    high = middle
            return (low + high) / 2.0

        height, thickness = 0.2104, 0.04064
        sensitivity = impedance._CHORD_CALIBRATION[
            "width_sensitivity"]
        for er in (3.91, 4.4):
            def chord_eps(width_for_eps):
                bare = propagation.hammerstad_effective_permittivity(
                    er, width_for_eps, height)
                q = (bare - 1.0) / (er - 1.0)
                return q * er + (1.0 - q) * 3.8

            def z_thick_chord(width):
                effective = propagation.thickness_corrected_width(
                    width, height, thickness)
                return z_air(effective / height) / math.sqrt(
                    chord_eps(effective))

            def z_zero_chord(width):
                return z_air(width / height) / math.sqrt(
                    chord_eps(width))

            def z_zero_barbuto(width):
                return z_air(width / height) / math.sqrt(
                    overlay_reference.immersed_epsilon(
                        er, 3.8, width / height))

            for target in (75.0, 50.0, 35.0):
                a = solve(z_thick_chord, target)
                b = solve(z_zero_chord, target)
                c = solve(z_zero_barbuto, target)
                leg_ab = abs(b - a) / a
                leg_bc = abs(c - b) / b
                leg_ac = abs(c - a) / a
                self.assertGreater(
                    leg_ab,
                    sensitivity["thickness_convention_shift"]["min"])
                self.assertLess(
                    leg_ab,
                    sensitivity["thickness_convention_shift"]["max"])
                self.assertLess(leg_bc,
                                sensitivity["family_shift_bound"])
                self.assertGreater(leg_ac,
                                   sensitivity["total_shift"]["min"])
                self.assertLess(leg_ac,
                                sensitivity["total_shift"]["max"])
                self.assertLess(abs(leg_ac - leg_ab), 0.01)

    def test_production_does_not_dispatch_the_reference(self):
        """The reference is evidence: the impedance module never
        imports it, and the production coated enclosure is unchanged
        to the digit."""
        source = inspect.getsource(impedance)
        self.assertNotIn("import overlay_reference", source)
        self.assertNotIn("overlay_reference import", source)
        result = impedance.solve(_snapshot(),
                                 _request(soldermask_present=True))
        self.assertEqual(result["enclosure"]["width_mm"],
                         {"lower": 0.292081, "upper": 0.370656})


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
