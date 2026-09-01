"""Geometry extraction on synthetic boards: every number hand-derivable.

The fixtures are built in memory, so every expected length, count and
resistance below is analytic. The provenance contract is tested
alongside the arithmetic: bare numbers cannot masquerade as physical
evidence, approved-evidence resolution mirrors the impedance solver's
finished-copper semantics exactly, absent nets refuse, and the
derived interconnect model covers only the phenomenon the extraction
actually supplies.
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                      # noqa: E402
from pcbqa import claim, extract                   # noqa: E402
from pcbqa.extract import ExtractionError          # noqa: E402
from pcbqa.sim import model_registry               # noqa: E402
from tests import synth                            # noqa: E402
from tests.test_fabricators import _raw_sources    # noqa: E402
from pcbqa.fabricators import jlcpcb, model        # noqa: E402

COPPER = extract.caller_declared_copper({"F.Cu": 0.035,
                                         "B.Cu": 0.035})
THICKNESS = extract.physical_parameter(
    1.6, "mm", "caller-declared", "test fixture board thickness")


def _snapshot():
    catalog = jlcpcb.parse(_raw_sources())
    return {"normalized": catalog,
            "normalized_sha256": model.normalized_digest(catalog)}


def _board_with_net():
    board = synth.new_board()
    net = synth.add_net(board, "SIG")
    synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                    layer=pcbnew.F_Cu, width_mm=0.2)
    synth.add_track(board, (10.0, 0.0), (10.0, 5.0), net=net,
                    layer=pcbnew.B_Cu, width_mm=0.4)
    synth.add_via(board, 10.0, 0.0, net=net)
    return board


class PhysicalInputsCarryProvenance(unittest.TestCase):

    def test_bare_numbers_cannot_masquerade_as_evidence(self):
        board = _board_with_net()
        with self.assertRaises(ExtractionError):
            extract.extract_net(board, "SIG",
                                {"F.Cu": 0.035, "B.Cu": 0.035},
                                THICKNESS)
        with self.assertRaises(ExtractionError):
            extract.extract_net(board, "SIG", COPPER, 1.6)

    def test_caller_declared_is_marked_forever(self):
        for record in COPPER.values():
            self.assertEqual(record["source_type"], "caller-declared")
            self.assertIsNone(record["digest"])

    def test_approved_copper_mirrors_the_impedance_resolution(self):
        """Finished copper resolved from approved evidence: external
        1 oz is 1.6 mil = 0.04064 mm and internal 0.5 oz is 0.6 mil =
        0.01524 mm - the finished thicknesses, NOT the 35 um nominal
        foil - each carrying the record identity and the catalog
        digest."""
        snapshot = _snapshot()
        parameters = extract.approved_finished_copper(snapshot, {
            "F.Cu": ("external", 1.0),
            "In1.Cu": ("internal", 0.5),
        })
        outer = parameters["F.Cu"]
        self.assertEqual(outer["value"], 0.04064)
        self.assertEqual(outer["source_type"], "approved-evidence")
        self.assertEqual(outer["source"],
                         "finished_copper_external_1oz_mil")
        self.assertEqual(outer["digest"],
                         snapshot["normalized_sha256"])
        self.assertEqual(parameters["In1.Cu"]["value"], 0.01524)
        with self.assertRaises(ExtractionError):
            extract.approved_finished_copper(snapshot, {
                "F.Cu": ("external", 9.0)})

    def test_parameter_validation_is_strict(self):
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ExtractionError):
                extract.physical_parameter(value, "mm",
                                           "caller-declared", "x")
        with self.assertRaises(ExtractionError):
            extract.physical_parameter(1.0, "mm", "guessed", "x")
        with self.assertRaises(ExtractionError):
            extract.physical_parameter(1.0, "mm", "derived", "")


class ExtractionIsAnalytic(unittest.TestCase):

    def test_segments_totals_and_resistance(self):
        record = extract.extract_net(_board_with_net(), "SIG",
                                     COPPER, THICKNESS)
        totals = record["totals"]
        self.assertAlmostEqual(totals["copper_length_mm"], 15.0,
                               places=6)
        self.assertEqual(totals["via_count"], 1)
        self.assertAlmostEqual(totals["via_barrel_estimate_mm"], 1.6,
                               places=6)
        rho = extract.IACS_RESISTIVITY_OHM_M
        expected = rho * 0.010 / (0.0002 * 0.000035) \
            + rho * 0.005 / (0.0004 * 0.000035)
        self.assertAlmostEqual(
            record["dc"]["segment_resistance_sum_ohm"], expected,
            places=9)
        self.assertIn("no two-terminal claim",
                      record["dc"]["meaning"])
        self.assertIn("IEC 60028", record["dc"]["resistivity_source"])

    def test_an_absent_net_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.extract_net(_board_with_net(), "GHOST", COPPER,
                                THICKNESS)

    def test_unstated_copper_thickness_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.extract_net(
                _board_with_net(), "SIG",
                extract.caller_declared_copper({"F.Cu": 0.035}),
                THICKNESS)

    def test_the_baseline_report_carries_physical_inputs(self):
        board = _board_with_net()
        directory = synth.tempdir("extract-baseline")
        board_file = os.path.join(directory, "fixture.kicad_pcb")
        synth.save(board, board_file)
        saved = pcbnew.LoadBoard(board_file)
        report = extract.baseline_report(
            board_file, saved, ["SIG"], COPPER, THICKNESS)
        self.assertEqual(report["kind"], "board-geometry-baseline")
        self.assertEqual(len(report["board_file_sha256"]), 64)
        inputs = report["physical_inputs"]
        self.assertEqual(
            inputs["copper_thickness_mm"]["F.Cu"]["source_type"],
            "caller-declared")
        self.assertEqual(inputs["board_thickness_mm"]["value"], 1.6)

    def test_paths_lift_requires_the_timing_gates(self):
        with self.assertRaises(ExtractionError):
            extract.paths_from_validation({"gates": []})


class ExtractedInterconnectsEnterSimulationHonestly(unittest.TestCase):

    def _net_record(self):
        return extract.extract_net(_board_with_net(), "SIG", COPPER,
                                   THICKNESS)

    def _physical_inputs(self):
        return {"copper_thickness_mm": COPPER,
                "board_thickness_mm": THICKNESS}

    def test_the_model_covers_only_dc(self):
        """The derived model registers cleanly, covers EXACTLY
        interconnect_dc at geometry-derived, and cannot satisfy an
        interconnect_si requirement - geometry-derived DC data never
        becomes a transmission-line model by renaming."""
        record = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._physical_inputs())
        registry = model_registry.ModelRegistry([record])
        evidence = {fact["phenomenon"]: fact
                    for fact in record["evidence"]}
        self.assertEqual(evidence["interconnect_dc"]["evidence_class"],
                         "geometry-derived")
        self.assertEqual(
            evidence["interconnect_si"]["applicability"]["status"],
            claim.UNSUPPORTED)
        self.assertEqual(
            evidence["device_electrical"]["applicability"]["status"],
            claim.NOT_APPLICABLE)
        report = registry.coverage_report(
            [record["identity"]],
            {"interconnect_si": ["full-wave-extracted",
                                 "quasi-static-extracted"]})
        self.assertFalse(report["satisfied"])
        report = registry.coverage_report(
            [record["identity"]],
            {"interconnect_dc": ["geometry-derived"]})
        self.assertTrue(report["satisfied"])

    def test_spice_needs_the_two_terminal_assertion(self):
        without = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._physical_inputs())
        self.assertNotIn("spice", without)
        with_assertion = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._physical_inputs(),
            two_terminal_asserted_by="test author")
        self.assertIn(".subckt", with_assertion["spice"])
        self.assertEqual(
            with_assertion["derivation"]["resistance_claim"]["evidence"]
            ["provenance"]["two_terminal_asserted_by"], "test author")


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()


class TheEvidenceChainSurvivesIntoSimulation(unittest.TestCase):

    def _net_record(self, copper=COPPER):
        return extract.extract_net(_board_with_net(), "SIG", copper,
                                   THICKNESS)

    def _inputs(self, copper=COPPER):
        return {"copper_thickness_mm": copper,
                "board_thickness_mm": THICKNESS}

    def test_the_model_carries_its_derivation(self):
        record = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._inputs())
        derivation = record["derivation"]
        self.assertEqual(derivation["chain"][0],
                         "physical-parameters[caller-declared]")
        self.assertEqual(
            derivation["roots"]["copper_thickness_mm.F.Cu"],
            "caller-declared")
        self.assertEqual(derivation["chain"][-1],
                         "simulation-model")
        self.assertEqual(derivation["extract_version"],
                         extract.EXTRACT_VERSION)
        self.assertEqual(
            sorted(derivation["copper_thickness_mm"]),
            ["B.Cu", "F.Cu"])
        self.assertEqual(len(derivation["physical_inputs_sha256"]),
                         64)
        self.assertIn("IEC 60028",
                      derivation["resistivity"]["source"])

    def test_different_physical_assumptions_are_different_models(
            self):
        """Two extractions of the same board under different copper
        thicknesses must never be indistinguishable downstream: the
        physical-input digest is part of the model identity."""
        thick = extract.caller_declared_copper({"F.Cu": 0.04064,
                                                "B.Cu": 0.04064})
        one = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._inputs())
        other = extract.interconnect_model_from_net(
            self._net_record(thick), "a" * 64, self._inputs(thick))
        self.assertNotEqual(one["identity"], other["identity"])
        self.assertIn("+phys:", one["identity"])

    def test_a_missing_layer_refuses(self):
        partial = extract.caller_declared_copper({"F.Cu": 0.035,
                                                  "B.Cu": 0.035})
        del partial["B.Cu"]
        with self.assertRaises(ExtractionError):
            extract.interconnect_model_from_net(
                self._net_record(), "a" * 64,
                {"copper_thickness_mm": partial,
                 "board_thickness_mm": THICKNESS})

    def test_the_iacs_reference_temperature_is_declared(self):
        record = extract.interconnect_model_from_net(
            self._net_record(), "a" * 64, self._inputs())
        declared = record["conditions"]["temperature_c"]
        self.assertEqual(declared["kind"], "fixed-reference")
        self.assertEqual(declared["value"], 20.0)

    def test_caller_resistivity_declares_no_temperature(self):
        """With a caller-supplied resistivity no reference
        temperature is known; the model declares nothing, and
        condition coverage downstream fails closed instead of
        assuming 20 C."""
        board = _board_with_net()
        net_record = extract.extract_net(
            board, "SIG", COPPER, THICKNESS,
            resistivity_ohm_m=1.68e-8)
        record = extract.interconnect_model_from_net(
            net_record, "a" * 64, self._inputs())
        self.assertNotIn("conditions", record)


class ApprovedEvidenceCannotBeForged(unittest.TestCase):

    def test_the_public_constructor_refuses_the_label(self):
        with self.assertRaises(ExtractionError):
            extract.physical_parameter(
                0.035, "mm", "approved-evidence",
                "totally-real-catalog-record")

    def test_a_hand_built_record_without_the_digest_refuses(self):
        forged = {"value": 0.035, "units": "mm",
                  "source_type": "approved-evidence",
                  "source": "made-up", "digest": "short",
                  "applicability": "none"}
        with self.assertRaises(ExtractionError):
            extract.validate_parameter(forged, "forged copper")

    def test_the_real_resolver_still_mints(self):
        parameters = extract.approved_finished_copper(
            _snapshot(), {"F.Cu": ("external", 1.0)})
        record = parameters["F.Cu"]
        self.assertEqual(record["source_type"], "approved-evidence")
        self.assertEqual(len(record["digest"]), 64)
        self.assertEqual(record["value"], 0.04064)


class RequirementsDeriveThePhysicalInputs(unittest.TestCase):

    _REQUIREMENTS = {"copper_layers": 4, "board_thickness_mm": 1.6,
                     "outer_copper_oz": 1.0, "inner_copper_oz": 0.5}

    def test_assignments_follow_the_stack(self):
        assignments = extract.copper_assignments_from_requirements(
            self._REQUIREMENTS, ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(assignments["F.Cu"], ("external", 1.0))
        self.assertEqual(assignments["B.Cu"], ("external", 1.0))
        self.assertEqual(assignments["In1.Cu"], ("internal", 0.5))

    def test_a_layer_count_contradiction_refuses(self):
        with self.assertRaises(ExtractionError):
            extract.copper_assignments_from_requirements(
                self._REQUIREMENTS, ["F.Cu", "B.Cu"])

    def test_thickness_is_derived_and_digest_bound(self):
        parameter = extract.requirements_board_thickness(
            self._REQUIREMENTS, "b" * 64)
        self.assertEqual(parameter["source_type"], "derived")
        self.assertEqual(parameter["value"], 1.6)
        self.assertEqual(parameter["digest"], "b" * 64)


class ConnectivityIsRealNotTrackCount(unittest.TestCase):
    """"Has copper" is not "routed": classification answers whether
    every pad of a net sits in one connected copper component."""

    def setUp(self):
        from pcbqa import geom
        geom.configure(0.001)

    def _three_pad_board(self):
        """One net, three SMD pads in a row at x = 0, 10, 20."""
        board = synth.new_board()
        net = synth.add_net(board, "TREE")
        for index, x in enumerate((0.0, 10.0, 20.0)):
            synth.add_pad_footprint(
                board, "P{}".format(index + 1), x, 0.0,
                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0), net=net)
        return board, net

    def test_partial_copper_is_not_complete(self):
        """Many tracks, one pad still an island: partial-copper,
        never complete - the seed02-05 PDM_CLK_IN failure mode."""
        from pcbqa import connectivity, geom
        board, net = self._three_pad_board()
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net)
        synth.add_track(board, (2.0, 0.0), (2.0, 3.0), net=net)
        synth.add_track(board, (4.0, 0.0), (4.0, 3.0), net=net)
        record = connectivity.classify_net(
            board, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "partial-copper")
        self.assertEqual(record["pad_count"], 3)
        self.assertEqual(record["pad_components"],
                         [["P1.1", "P2.1"], ["P3.1"]])

    def test_fully_connected_multipoint_is_complete(self):
        from pcbqa import connectivity, geom
        board, net = self._three_pad_board()
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net)
        synth.add_track(board, (10.0, 0.0), (20.0, 0.0), net=net)
        record = connectivity.classify_net(
            board, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "connectivity-complete")
        self.assertEqual(record["pad_components"],
                         [["P1.1", "P2.1", "P3.1"]])

    def test_no_copper_is_named_no_copper(self):
        from pcbqa import connectivity, geom
        board, _net = self._three_pad_board()
        record = connectivity.classify_net(
            board, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "no-copper")

    def test_zone_fill_serves_connectivity(self):
        """A plane-served net is connected by its filled zone; the
        same zone unfilled contributes nothing."""
        from pcbqa import connectivity, geom
        board, net = self._three_pad_board()
        synth.add_zone(board, net, [pcbnew.F_Cu],
                       (-2.0, -2.0, 24.0, 2.0), fill=True)
        record = connectivity.classify_net(
            board, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "connectivity-complete")
        self.assertEqual(record["copper"]["zone_fills"], 1)
        bare, net2 = self._three_pad_board()
        synth.add_zone(bare, net2, [pcbnew.F_Cu],
                       (-2.0, -2.0, 24.0, 2.0), fill=False)
        record = connectivity.classify_net(
            bare, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "no-copper")

    def test_connected_but_wrong_topology_stays_topology_invalid(
            self):
        """Connectivity completeness never implies topology
        validity: a net whose pads all connect can still fail its
        declared driver-to-load topology rule."""
        from pcbqa import connectivity, geom
        from pcbqa.rules import NetTopologyRule
        board, net = self._three_pad_board()
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net)
        synth.add_track(board, (10.0, 0.0), (20.0, 0.0), net=net)
        record = connectivity.classify_net(
            board, "TREE", geom.pad_copper_polygon)
        self.assertEqual(record["class"], "connectivity-complete")
        rule = NetTopologyRule({
            "id": "tree-topology", "net_regex": "TREE",
            "source_pad_regex": r"P1\.1",
            "load_pad_regex": r"P[23]\.1",
            "max_vias_per_net": 0,
            "permitted_layers": ["B.Cu"],
        })
        measured, problems = rule.evaluate(
            board, geom.pad_copper_polygon)
        limit_problems = rule.check_limits(measured)
        self.assertTrue(limit_problems)
        self.assertIn("layer", limit_problems[0]["issue"])

    def test_a_net_without_pads_is_explicitly_degenerate(self):
        """No pads means no pad-joining question: the state says so
        and can never read as complete."""
        from pcbqa import connectivity, geom
        board = synth.new_board()
        net = synth.add_net(board, "ORPHAN")
        synth.add_track(board, (0.0, 0.0), (5.0, 0.0), net=net)
        record = connectivity.classify_net(board, "ORPHAN",
                                           geom.pad_copper_polygon)
        self.assertEqual(record["class"], "no-pads")
        self.assertNotEqual(record["class"],
                            "connectivity-complete")

    def test_extract_net_embeds_the_classification(self):
        from pcbqa import geom  # noqa: F401 - import guard
        board, net = self._three_pad_board()
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net)
        record = extract.extract_net(
            board, "TREE",
            extract.caller_declared_copper({"F.Cu": 0.035}),
            THICKNESS)
        self.assertEqual(record["connectivity"]["class"],
                         "partial-copper")


class TrustIsVerifiedNotShaped(unittest.TestCase):

    def test_a_plausible_forged_record_refuses_trust(self):
        """A hand-built record whose shape validates must still fail
        TRUST verification: its digest is not the approved
        snapshot's, and its capability does not exist."""
        snapshot = _snapshot()
        forged = {"value": 0.04064, "units": "mm",
                  "source_type": "approved-evidence",
                  "source": "made-up", "digest": "a" * 64,
                  "applicability": "external 1 oz finished copper"}
        extract.validate_parameter(forged, "forged")  # shape passes
        with self.assertRaises(ExtractionError):
            extract.verify_approved_parameter(forged, snapshot)

    def test_a_wrong_value_under_the_right_digest_refuses(self):
        snapshot = _snapshot()
        genuine = extract.approved_finished_copper(
            snapshot, {"F.Cu": ("external", 1.0)})["F.Cu"]
        extract.verify_approved_parameter(genuine, snapshot)
        tampered = dict(genuine)
        tampered["value"] = 0.035
        with self.assertRaises(ExtractionError):
            extract.verify_approved_parameter(tampered, snapshot)

    def test_mixed_roots_stay_mixed(self):
        """Caller-declared physics never claims the approved chain
        head; the roots name each input's own provenance."""
        board = _board_with_net()
        record = extract.extract_net(board, "SIG", COPPER,
                                     THICKNESS)
        model = extract.interconnect_model_from_net(
            record, "a" * 64,
            {"copper_thickness_mm": COPPER,
             "board_thickness_mm": THICKNESS})
        self.assertNotIn("approved",
                         model["derivation"]["chain"][0])
        self.assertEqual(
            model["derivation"]["roots"]["board_thickness_mm"],
            "caller-declared")


class ConstructionsBindTheComparison(unittest.TestCase):

    def test_different_constructions_have_different_digests(self):
        thin = extract.caller_declared_copper({"F.Cu": 0.035})
        thick = extract.caller_declared_copper({"F.Cu": 0.04064})
        one = extract.construction_digest(thin, THICKNESS)
        other = extract.construction_digest(thick, THICKNESS)
        self.assertNotEqual(one, other)
        self.assertEqual(one,
                         extract.construction_digest(thin,
                                                     THICKNESS))


class PathScopedExtractionIsHonest(unittest.TestCase):
    """DC resistance over the actual traversal: stubs excluded,
    parallel copper refused, plane copper refused."""

    def setUp(self):
        from pcbqa import geom
        geom.configure(0.001)

    def _stubbed_board(self):
        """P1 -- 10 mm run -- P2, with a 6 mm stub hanging off the
        middle: the stub inflates the net inventory but never the
        traversal."""
        board = synth.new_board()
        net = synth.add_net(board, "LINK")
        synth.add_pad_footprint(board, "P1", 0.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "P2", 10.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (5.0, 0.0), (5.0, 6.0), net=net,
                        width_mm=0.2)
        return board

    def test_stubs_are_excluded_from_the_traversal(self):
        board = self._stubbed_board()
        record = extract.path_resistance(
            board, "LINK", "P1.1", "P2.1", COPPER)
        inventory = extract.extract_net(board, "LINK", COPPER,
                                        THICKNESS)
        resistance = record["resistance_claim"]["quantity"]["value"]
        self.assertLess(
            resistance,
            inventory["dc"]["segment_resistance_sum_ohm"])
        self.assertAlmostEqual(record["path_length_mm"], 10.0,
                               delta=1.2)
        expected = extract.IACS_RESISTIVITY_OHM_M \
            * (record["path_length_mm"] / 1000.0) \
            / ((0.2 / 1000.0) * (0.035 / 1000.0))
        self.assertAlmostEqual(resistance, expected,
                               places=9)

    def test_parallel_copper_refuses(self):
        board = self._stubbed_board()
        net = board.GetNetsByName()["LINK"]
        synth.add_track(board, (0.0, 0.0), (0.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (0.0, 3.0), (10.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (10.0, 3.0), (10.0, 0.0), net=net,
                        width_mm=0.2)
        with self.assertRaises(ExtractionError):
            extract.path_resistance(board, "LINK", "P1.1", "P2.1",
                                    COPPER)

    def test_plane_copper_refuses(self):
        board = self._stubbed_board()
        net = board.GetNetsByName()["LINK"]
        synth.add_zone(board, net, [pcbnew.F_Cu],
                       (-2.0, -2.0, 12.0, 8.0), fill=True)
        with self.assertRaises(ExtractionError):
            extract.path_resistance(board, "LINK", "P1.1", "P2.1",
                                    COPPER)

    def test_disconnected_endpoints_refuse(self):
        board = self._stubbed_board()
        net = synth.add_net(board, "LONELY")
        synth.add_pad_footprint(board, "P3", 20.0, 20.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_track(board, (20.0, 20.0), (22.0, 20.0), net=net)
        with self.assertRaises(ExtractionError):
            extract.path_resistance(board, "LONELY", "P3.1", "P9.9",
                                    COPPER)

    def test_the_path_model_is_two_terminal_by_construction(self):
        board = self._stubbed_board()
        record = extract.path_resistance(
            board, "LINK", "P1.1", "P2.1", COPPER)
        model = extract.interconnect_model_from_path(
            record, "a" * 64,
            {"copper_thickness_mm": COPPER,
             "board_thickness_mm": THICKNESS})
        registry = model_registry.ModelRegistry([model])
        self.assertIn("path:LINK:P1.1->P2.1@", model["identity"])
        self.assertIn("established by construction",
                      model["derivation"]["two_terminal_assertion"])
        self.assertIn(".subckt", model["spice"])
        self.assertEqual(
            model["conditions"]["temperature_c"]["value"], 20.0)
        self.assertEqual(registry.identities(),
                         [model["identity"]])


class UniquenessIsBridgeRigorous(unittest.TestCase):
    """Every resistive traversal element must be a bridge in the
    electrical node graph: rejoining detours refuse wherever they
    rejoin, junction pivots never false-positive."""

    def setUp(self):
        from pcbqa import geom
        geom.configure(0.001)

    def _linked_board(self):
        board = synth.new_board()
        net = synth.add_net(board, "LINK")
        synth.add_pad_footprint(board, "P1", 0.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "P2", 10.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                        width_mm=0.2)
        return board, net

    def test_a_detour_rejoining_the_next_junction_refuses(self):
        """The adversarial case interior-node removal missed:
        source--A==B--dest with a detour from A's junction to B's.
        The detour hangs off removed nodes, so node removal saw
        nothing - yet it carries current."""
        board, net = self._linked_board()
        synth.add_track(board, (3.0, 0.0), (3.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (3.0, 3.0), (7.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (7.0, 3.0), (7.0, 0.0), net=net,
                        width_mm=0.2)
        with self.assertRaisesRegex(ExtractionError,
                                    "not a bridge"):
            extract.path_resistance(board, "LINK", "P1.1", "P2.1",
                                    COPPER)

    def test_a_second_track_between_the_same_nodes_refuses(self):
        board, net = self._linked_board()
        synth.add_track(board, (3.0, 0.0), (5.0, 1.5), net=net,
                        width_mm=0.2)
        synth.add_track(board, (5.0, 1.5), (7.0, 0.0), net=net,
                        width_mm=0.2)
        with self.assertRaises(ExtractionError):
            extract.path_resistance(board, "LINK", "P1.1", "P2.1",
                                    COPPER)

    def test_junction_pivots_are_not_parallel_copper(self):
        """Stubs meeting the traversal at a junction - even several
        at one junction - pivot on a single electrical node and
        must not read as dividers."""
        board, net = self._linked_board()
        synth.add_track(board, (5.0, 0.0), (5.0, 6.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (5.0, 0.0), (5.0, -4.0), net=net,
                        width_mm=0.2)
        record = extract.path_resistance(board, "LINK",
                                         "P1.1", "P2.1", COPPER)
        self.assertAlmostEqual(record["path_length_mm"], 10.0,
                               delta=1.2)


class TheOldUniquenessCheckMissedThis(unittest.TestCase):
    """The committed record of WHY interior-node removal was
    replaced: run verbatim against the rejoining-detour fixture, it
    finds no alternate path, while the bridge analysis refuses."""

    def setUp(self):
        from pcbqa import geom
        geom.configure(0.001)

    def test_interior_removal_reports_no_alternate_here(self):
        from pcbqa.connectivity import NetGraph
        from pcbqa import geom
        board = synth.new_board()
        net = synth.add_net(board, "LINK")
        synth.add_pad_footprint(board, "P1", 0.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "P2", 10.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_track(board, (0.0, 0.0), (10.0, 0.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (3.0, 0.0), (3.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (3.0, 3.0), (7.0, 3.0), net=net,
                        width_mm=0.2)
        synth.add_track(board, (7.0, 3.0), (7.0, 0.0), net=net,
                        width_mm=0.2)
        graph = NetGraph(board, "LINK", geom.pad_copper_polygon,
                         split_at_junctions=True)
        _length, chain = graph.trace(["P1.1"], "P2.1")
        # The superseded algorithm, verbatim: remove every interior
        # chain node at once, then ask whether the endpoints still
        # connect.
        interior = set(chain) - {chain[0], chain[-1]}
        frontier = [chain[0]]
        seen = {chain[0]}
        found_alternate = False
        while frontier:
            node = frontier.pop()
            if node == chain[-1]:
                found_alternate = True
                break
            for neighbour, _weight in graph.adj[node]:
                if neighbour in seen or neighbour in interior:
                    continue
                seen.add(neighbour)
                frontier.append(neighbour)
        self.assertIs(found_alternate, False)
        # The detour carries current all the same: the bridge
        # analysis refuses what the old check waved through.
        with self.assertRaisesRegex(ExtractionError,
                                    "not a bridge"):
            extract.path_resistance(board, "LINK", "P1.1", "P2.1",
                                    COPPER)


class OmittedPhysicsIsABoundNotATruth(unittest.TestCase):
    """Via barrels contribute zero by declared omission, so a path
    through vias reports a LOWER bound on resistance - and says so
    machine-readably."""

    def setUp(self):
        from pcbqa import geom
        geom.configure(0.001)

    def test_a_via_free_path_is_exact(self):
        board = synth.new_board()
        net = synth.add_net(board, "FLAT")
        synth.add_pad_footprint(board, "P1", 0.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "P2", 8.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_track(board, (0.0, 0.0), (8.0, 0.0), net=net,
                        width_mm=0.2)
        record = extract.path_resistance(board, "FLAT",
                                         "P1.1", "P2.1", COPPER)
        self.assertEqual(record["resistance_claim"]["knowledge"],
                         claim.EXACT)
        self.assertEqual(record["via_count_in_path"], 0)

    def test_a_via_path_reports_a_lower_bound(self):
        board = synth.new_board()
        net = synth.add_net(board, "DEEP")
        synth.add_pad_footprint(board, "P1", 0.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net)
        synth.add_pad_footprint(board, "P2", 8.0, 0.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=net, flipped=True)
        synth.add_track(board, (0.0, 0.0), (4.0, 0.0), net=net,
                        width_mm=0.2)
        synth.add_via(board, 4.0, 0.0, net=net)
        synth.add_track(board, (4.0, 0.0), (8.0, 0.0), net=net,
                        width_mm=0.2, layer=pcbnew.B_Cu)
        record = extract.path_resistance(board, "DEEP",
                                         "P1.1", "P2.1", COPPER)
        self.assertEqual(record["via_count_in_path"], 1)
        resistance_claim = record["resistance_claim"]
        self.assertEqual(resistance_claim["knowledge"], claim.LOWER_BOUND)
        self.assertTrue(any(
            "via barrel" in omission["detail"] for omission in
            resistance_claim["evidence"]["omitted_contributions"]))
        model = extract.interconnect_model_from_path(
            record, "b" * 64,
            {"copper_thickness_mm": COPPER,
             "board_thickness_mm": THICKNESS})
        self.assertEqual(
            model["derivation"]["resistance_claim"]["knowledge"],
            claim.LOWER_BOUND)


class MetricNamesCannotMasquerade(unittest.TestCase):

    def test_leaf_inventory_and_complete_path_are_distinct(self):
        definitions = extract.METRIC_DEFINITIONS
        self.assertNotEqual(
            definitions["clock_leaf_net_length_spread_mm"],
            definitions["complete_path_spread_mm"])
        self.assertIn("NET-length-inventory",
                      definitions["clock_leaf_net_length_spread_mm"])
        self.assertIn("complete-path",
                      definitions["complete_path_spread_mm"])
