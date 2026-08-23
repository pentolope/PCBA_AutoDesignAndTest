"""Electrical paths, physical stackup, analytic propagation and the timing gates.

Every expectation here is arithmetic somebody can redo on paper. The fixture is
a synthetic board whose copper runs along straight lines of whole-millimetre
length, and whose physical stackup uses deliberately round, deliberately
fictional material figures - they are test values, not a claim about any
laminate - so a delay is a length times a number the test recomputes from the
published formula rather than from the implementation.

The real consumer board is not used to establish correctness. It cannot be: it
is one board, its answers are whatever they are, and a checker tuned until it
agrees with them has been fitted rather than tested.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                                      # noqa: E402

from pcbqa import (canonical, cleanroom, core, electrical_path,     # noqa: E402
                   geom, propagation, stackup_physical)
from pcbqa.core import Context, Manifest, Status                    # noqa: E402
from pcbqa.electrical_path import PathError                         # noqa: E402
from pcbqa.gates import g_timing, g_geometry                        # noqa: E402,F401
from tests import consumer, paths, synth                            # noqa: E402


# ---------------------------------------------------------------------------
# the fixture
# ---------------------------------------------------------------------------

# Round, fictional laminate figures. Chosen so every expected number below is
# calculable by hand; they describe no real material and are never used against
# a real board.
FIXTURE_EPSILON_R = 4.0
FIXTURE_HEIGHT_MM = 0.1
FIXTURE_CORE_MM = 1.0
FIXTURE_INNER_COPPER_MM = 0.0175
FIXTURE_OUTER_COPPER_MM = 0.035
TRACK_WIDTH_MM = 0.2

FIXTURE_STACKUP = [
    {"name": "F.Cu", "type": "copper", "thickness_mm": FIXTURE_OUTER_COPPER_MM},
    {"name": "dielectric 1", "type": "prepreg", "thickness_mm": FIXTURE_HEIGHT_MM,
     "material": "fixture-laminate", "epsilon_r": FIXTURE_EPSILON_R,
     "loss_tangent": 0.02},
    {"name": "In1.Cu", "type": "copper", "thickness_mm": FIXTURE_INNER_COPPER_MM},
    {"name": "dielectric 2", "type": "core", "thickness_mm": FIXTURE_CORE_MM,
     "material": "fixture-laminate", "epsilon_r": FIXTURE_EPSILON_R,
     "loss_tangent": 0.02},
    {"name": "In2.Cu", "type": "copper", "thickness_mm": FIXTURE_INNER_COPPER_MM},
    {"name": "dielectric 3", "type": "prepreg", "thickness_mm": FIXTURE_HEIGHT_MM,
     "material": "fixture-laminate", "epsilon_r": FIXTURE_EPSILON_R,
     "loss_tangent": 0.02},
    {"name": "B.Cu", "type": "copper", "thickness_mm": FIXTURE_OUTER_COPPER_MM},
]

# What the copper is, by construction. Each of these is the straight-line
# distance between two coordinates in `build_board`, and is what the gates must
# independently arrive at.
LENGTH_PRE_SERIES_MM = 14.5        # D1.1 -> R1.1
LENGTH_TO_FIRST_LOAD_MM = 9.5      # R1.2 -> L1.1
LENGTH_FIRST_TO_SECOND_MM = 6.0    # L1.1 -> L2.1
LENGTH_VIA_TOP_MM = 10.0           # D2.1 -> via
LENGTH_VIA_BOTTOM_MM = 10.0        # via  -> L3.1

PATH_TO_L1_MM = LENGTH_PRE_SERIES_MM + LENGTH_TO_FIRST_LOAD_MM          # 24.0
PATH_TO_L2_MM = PATH_TO_L1_MM + LENGTH_FIRST_TO_SECOND_MM               # 30.0
PATH_VIA_MM = LENGTH_VIA_TOP_MM + LENGTH_VIA_BOTTOM_MM                  # 20.0

# The dielectrics and inner copper a through via passes between the outer
# layers, summed straight off FIXTURE_STACKUP.
VIA_VERTICAL_MM = (FIXTURE_HEIGHT_MM + FIXTURE_INNER_COPPER_MM
                   + FIXTURE_CORE_MM + FIXTURE_INNER_COPPER_MM
                   + FIXTURE_HEIGHT_MM)


def expected_microstrip_ps_per_mm(width_mm=TRACK_WIDTH_MM,
                                  height_mm=FIXTURE_HEIGHT_MM,
                                  epsilon_r=FIXTURE_EPSILON_R):
    """Hammerstad's published formula, restated here rather than imported.

    Written out longhand on purpose: a test that calls the function under test
    to work out what the function under test should return proves only that it
    is deterministic.
    """
    ratio = width_mm / height_mm
    assert ratio >= 1.0, "the w/h < 1 branch is exercised separately"
    epsilon_eff = ((epsilon_r + 1.0) / 2.0
                   + (epsilon_r - 1.0) / 2.0 * (1.0 + 12.0 / ratio) ** -0.5)
    return math.sqrt(epsilon_eff) / 0.299792458


def build_board(directory, with_stackup=True):
    """The fixture board: a series-resistor fan-out and a via transition."""
    board = synth.new_board(layers=4, size_mm=40.0)
    gnd = synth.add_net(board, "GND")
    sig_a = synth.add_net(board, "SIG_A")
    sig_b = synth.add_net(board, "SIG_B")
    sig_v = synth.add_net(board, "SIG_V")

    # Both inner layers are poured on GND, so they are reference planes
    # because copper is actually on them and not because a file says so.
    synth.add_zone(board, gnd, (pcbnew.In1_Cu, pcbnew.In2_Cu),
                   (82.0, 82.0, 118.0, 118.0))

    # --- driver -> series resistor -> two loads, all on F.Cu ---------------
    synth.add_pad_footprint(board, "D1", 85.0, 95.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_a)
    synth.add_two_pad_footprint(board, "R1", 100.0, 95.0, 1.0, (sig_a, sig_b),
                                value="33R")
    synth.add_pad_footprint(board, "L1", 110.0, 95.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_b)
    synth.add_pad_footprint(board, "L2", 116.0, 95.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_b)
    synth.add_track(board, (85.0, 95.0), (99.5, 95.0), net=sig_a,
                    width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (100.5, 95.0), (110.0, 95.0), net=sig_b,
                    width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (110.0, 95.0), (116.0, 95.0), net=sig_b,
                    width_mm=TRACK_WIDTH_MM)

    # --- a path that changes layer ----------------------------------------
    synth.add_pad_footprint(board, "D2", 85.0, 105.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_v)
    synth.add_pad_footprint(board, "L3", 105.0, 105.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_v, flipped=True)
    synth.add_track(board, (85.0, 105.0), (95.0, 105.0), net=sig_v,
                    layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)
    synth.add_via(board, 95.0, 105.0, net=sig_v)
    synth.add_track(board, (95.0, 105.0), (105.0, 105.0), net=sig_v,
                    layer=pcbnew.B_Cu, width_mm=TRACK_WIDTH_MM)

    path = os.path.join(directory, "timing.kicad_pcb")
    synth.save(board, path)
    if with_stackup:
        synth.write_physical_stackup(path, FIXTURE_STACKUP)
    for name in ("timing.kicad_sch", "timing.kicad_pro"):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            fh.write("(fixture)\n" if name.endswith("sch") else "{}\n")
    return path


ROUTES = {
    "template": {
        "id": "series_branch_to_{load}",
        "steps": [
            {"kind": "copper", "net": "SIG_A", "from": "D1.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "copper", "net": "SIG_B", "from": "R1.2",
             "to": "^{load}\\.1$"},
        ],
    },
    "bindings": [{"load": "L1"}, {"load": "L2"}],
}

VIA_ROUTE = {
    "paths": [{
        "id": "layer_change",
        "steps": [{"kind": "copper", "net": "SIG_V", "from": "D2.1",
                   "to": "L3.1"}],
    }],
}


def manifest_document(project, **overrides):
    """A minimal but complete manifest for the fixture."""
    document = {
        "schema_version": 2,
        "board_id": "pcbqa-timing-fixture",
        "constraint_version": "1",
        "description": "Synthetic fixture for the interconnect timing gates.",
        "project_root": project,
        "sources": {"pcb": "timing.kicad_pcb",
                    "schematic": "timing.kicad_sch",
                    "project": "timing.kicad_pro"},
        "tools": {"kicad_cli": "kicad-cli"},
        "geometry_profile": {
            "version": "timing-fixture-geometry-v1",
            "tolerances": {
                "polygon_chord_error_mm": {
                    "value": 0.001, "units": "mm",
                    "why": "outward chord error when polygonising pads"},
            },
        },
        "reports": {
            "source_closure": ["*.kicad_pcb", "*.kicad_sch", "*.kicad_pro",
                               "models/*.json"],
            "implementation_closure": [],
        },
        "fixture": {"attributes_file": paths.ATTRIBUTES},
        "timing": {
            "physical_stackup": {
                "reference_nets": ["GND"],
                "require_complete": True,
            },
            "propagation": {
                "backend": "analytic",
                "model": propagation.HAMMERSTAD,
                "via_delay_model": propagation.VIA_NONE,
            },
            "interfaces": {
                "series": {
                    "description": "Driver through a series resistor to two loads.",
                    "expected_path_count": 2,
                    "required_component_crossings": 1,
                    "routes": copy.deepcopy(ROUTES),
                    "groups": {
                        "loads": {
                            "description": "Both loads on the branch.",
                            "paths": "^series_branch_to_",
                        },
                    },
                },
                "layerchange": {
                    "description": "One net that changes layer through a via.",
                    "expected_path_count": 1,
                    "routes": copy.deepcopy(VIA_ROUTE),
                },
            },
        },
    }
    for key, value in overrides.items():
        document[key] = value
    return document


def write_manifest(directory, document):
    path = os.path.join(directory, "manifest.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
    return path


class Fixture:
    """A board, a manifest and a workdir, all in one temporary directory."""

    def __init__(self, mutate=None, with_stackup=True, tag="timing"):
        self.root = tempfile.mkdtemp(prefix="pcbqa_" + tag + "_")
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.project, exist_ok=True)
        self.board_path = build_board(self.project, with_stackup=with_stackup)
        document = manifest_document(self.project)
        if mutate:
            mutate(document, self.project)
        self.manifest_path = write_manifest(self.root, document)
        self.manifest = Manifest(self.manifest_path)
        self.ctx = Context(self.manifest,
                           os.path.join(self.root, "work"),
                           kicad_cli="kicad-cli")

    def gates(self, only=None):
        results = core.run_all(self.ctx, only=only)
        return {r.gate_id: r for r in results}

    def analysis(self):
        return g_timing.analysis(self.ctx)

    def dispose(self):
        shutil.rmtree(self.root, ignore_errors=True)


_FIXTURES = []


def make(**kwargs):
    fixture = Fixture(**kwargs)
    _FIXTURES.append(fixture)
    return fixture


def tearDownModule():
    for fixture in _FIXTURES:
        fixture.dispose()
    del _FIXTURES[:]


TIMING_GATES = {"TIMING.PATH_INTEGRITY", "TIMING.INTERCONNECT_DELAY",
                "TIMING.INTERCONNECT_SKEW", "TIMING.SETUP_HOLD",
                "STACK.PHYSICAL", "PROV.TIMING_MODELS"}


# ---------------------------------------------------------------------------
# 1-4: what the path abstraction measures
# ---------------------------------------------------------------------------

class WhatAPathMeasures(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = make(tag="measure")
        cls.state = cls.fixture.analysis()
        cls.by_path = {r["resolved"].id + "->" + r["resolved"].destination.label: r
                       for _n, r in cls.state.all_paths()}

    def test_single_net_copper_path_length(self):
        """One net, one straight run: the length is the distance, exactly."""
        geom.configure(0.001)
        from pcbqa.connectivity import NetGraph
        graph = NetGraph(self.fixture.ctx.board(), "SIG_A",
                         geom.pad_copper_polygon)
        self.assertAlmostEqual(graph.path_length(["D1.1"], "R1.1"),
                               LENGTH_PRE_SERIES_MM, places=6)

    def test_path_length_and_trace_total_agree(self):
        """`trace` is what `path_length` is now built from; prove they match."""
        geom.configure(0.001)
        from pcbqa.connectivity import NetGraph
        graph = NetGraph(self.fixture.ctx.board(), "SIG_B",
                         geom.pad_copper_polygon)
        for target in ("L1.1", "L2.1"):
            length = graph.path_length(["R1.2"], target)
            traced, chain = graph.trace(["R1.2"], target)
            self.assertEqual(length, traced)
            self.assertTrue(chain, "a resolved path must name its elements")

    def test_path_with_a_via_transition(self):
        """Length is split across the two layers, and the via is recorded."""
        record = self.by_path["layer_change->L3.1"]["resolved"]
        self.assertAlmostEqual(record.copper_length_mm, PATH_VIA_MM, places=6)
        by_layer = record.length_by_layer_mm()
        self.assertAlmostEqual(by_layer["F.Cu"], LENGTH_VIA_TOP_MM, places=6)
        self.assertAlmostEqual(by_layer["B.Cu"], LENGTH_VIA_BOTTOM_MM, places=6)
        transitions = record.via_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertEqual({transitions[0]["from_layer"],
                          transitions[0]["to_layer"]}, {"F.Cu", "B.Cu"})

    def test_path_crosses_a_zero_delay_series_component(self):
        """The crossing joins two nets and contributes nothing, on the record."""
        record = self.by_path["series_branch_to_L1->L1.1"]
        resolved = record["resolved"]
        traversals = resolved.component_traversals()
        self.assertEqual([t["reference"] for t in traversals], ["R1"])
        self.assertEqual(traversals[0]["from_net"], "SIG_A")
        self.assertEqual(traversals[0]["to_net"], "SIG_B")
        self.assertIsNone(traversals[0]["declared_delay_model"])
        contributed = record["delay"]["component_traversals"][0]
        self.assertEqual(contributed["delay_ps"], 0.0)
        self.assertIn("R1", record["delay"]["unmodelled_component_delay"])

    def test_the_whole_path_spans_both_sides_of_the_series_component(self):
        """The point of the abstraction: not just the post-resistor copper."""
        resolved = self.by_path["series_branch_to_L1->L1.1"]["resolved"]
        self.assertAlmostEqual(resolved.copper_length_mm, PATH_TO_L1_MM,
                               places=6)
        self.assertEqual(resolved.source.label, "D1.1")
        self.assertEqual(resolved.destination.label, "L1.1")
        self.assertGreater(resolved.copper_length_mm, LENGTH_TO_FIRST_LOAD_MM,
                           "a path measured from the resistor's output net "
                           "would be this short")

    def test_two_paths_have_the_expected_mismatch_and_ordering(self):
        first = self.by_path["series_branch_to_L1->L1.1"]["resolved"]
        second = self.by_path["series_branch_to_L2->L2.1"]["resolved"]
        self.assertAlmostEqual(first.copper_length_mm, PATH_TO_L1_MM, places=6)
        self.assertAlmostEqual(second.copper_length_mm, PATH_TO_L2_MM, places=6)
        self.assertAlmostEqual(
            second.copper_length_mm - first.copper_length_mm,
            LENGTH_FIRST_TO_SECOND_MM, places=6)

    def test_skew_group_orders_the_endpoints(self):
        results = self.fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})
        group = results["TIMING.INTERCONNECT_SKEW"].measurements["groups"][0]
        self.assertEqual(group["members"], 2)
        self.assertEqual(group["earliest"], "L1.1")
        self.assertEqual(group["latest"], "L2.1")
        self.assertAlmostEqual(group["length_spread_mm"],
                               LENGTH_FIRST_TO_SECOND_MM, places=4)

    def test_delays_follow_the_published_microstrip_formula(self):
        per_mm = expected_microstrip_ps_per_mm()
        for key, length in (("series_branch_to_L1->L1.1", PATH_TO_L1_MM),
                            ("series_branch_to_L2->L2.1", PATH_TO_L2_MM),
                            ("layer_change->L3.1", PATH_VIA_MM)):
            delay = self.by_path[key]["delay"]
            self.assertEqual(delay["insufficient"], [],
                             "{}: {}".format(key, delay["insufficient"]))
            self.assertAlmostEqual(delay["delay_ps"], length * per_mm, places=4,
                                   msg=key)
            self.assertEqual(delay["fidelity"],
                             propagation.ANALYTIC_TRANSMISSION_LINE)

    def test_skew_in_time_is_the_mismatch_times_the_propagation_constant(self):
        results = self.fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})
        group = results["TIMING.INTERCONNECT_SKEW"].measurements["groups"][0]
        self.assertEqual(group["measured_in"], "ps")
        self.assertAlmostEqual(
            group["skew_ps"],
            LENGTH_FIRST_TO_SECOND_MM * expected_microstrip_ps_per_mm(),
            places=4)

    def test_a_result_records_how_it_was_obtained(self):
        results = self.fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})
        measurements = results["TIMING.INTERCONNECT_DELAY"].measurements
        self.assertEqual(measurements["physical_stackup_source"],
                         stackup_physical.NATIVE)
        self.assertEqual(measurements["propagation_model"],
                         propagation.HAMMERSTAD)
        self.assertEqual(measurements["via_delay_model"], propagation.VIA_NONE)
        self.assertEqual(measurements["backend"], "analytic")
        self.assertIn("interconnect", measurements["scope"])


# ---------------------------------------------------------------------------
# the propagation model itself
# ---------------------------------------------------------------------------

class TheAnalyticModel(unittest.TestCase):

    def test_microstrip_is_not_c_over_sqrt_dk(self):
        """The whole reason for a closed form rather than the naive velocity."""
        epsilon_eff = propagation.hammerstad_effective_permittivity(
            FIXTURE_EPSILON_R, TRACK_WIDTH_MM, FIXTURE_HEIGHT_MM)
        self.assertLess(epsilon_eff, FIXTURE_EPSILON_R)
        self.assertGreater(epsilon_eff, 1.0)
        naive = propagation.delay_ps_per_mm(FIXTURE_EPSILON_R)
        actual = propagation.delay_ps_per_mm(epsilon_eff)
        self.assertGreater(naive - actual, 0.5,
                           "c/sqrt(Dk) over-states an outer-layer microstrip "
                           "by more than a rounding error, which is why it is "
                           "not used")

    def test_the_narrow_trace_branch_is_the_published_one(self):
        """w/h < 1 has its own term; check it against the formula longhand."""
        width, height = 0.05, 0.1
        ratio = width / height
        expected = ((FIXTURE_EPSILON_R + 1) / 2
                    + (FIXTURE_EPSILON_R - 1) / 2
                    * ((1 + 12 / ratio) ** -0.5 + 0.04 * (1 - ratio) ** 2))
        self.assertAlmostEqual(
            propagation.hammerstad_effective_permittivity(
                FIXTURE_EPSILON_R, width, height), expected, places=9)

    def test_stripline_uses_the_homogeneous_dielectric_exactly(self):
        """An inner layer between two planes is TEM: e_eff is er, no formula."""
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        model = propagation.PropagationModel(stack, {"F.Cu", "In2.Cu"})
        record = model.conductor("In1.Cu", TRACK_WIDTH_MM)
        self.assertIn(record["mode"], (propagation.STRIPLINE,
                                       propagation.ASYMMETRIC_STRIPLINE))
        self.assertEqual(record["epsilon_effective"], FIXTURE_EPSILON_R)
        self.assertAlmostEqual(
            record["ps_per_mm"],
            math.sqrt(FIXTURE_EPSILON_R) / 0.299792458, places=6)

    def test_a_layer_with_no_reference_plane_refuses(self):
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        model = propagation.PropagationModel(stack, set())
        with self.assertRaises(propagation.Unsupported):
            model.conductor("F.Cu", TRACK_WIDTH_MM)

    def test_mixed_dielectric_stripline_refuses(self):
        """Different permittivities above and below is not one homogeneous medium."""
        layers = [dict(entry, kind=_kind(entry)) for entry in FIXTURE_STACKUP]
        layers[3] = dict(layers[3], epsilon_r=3.0)
        stack = stackup_physical.from_declaration({"layers": layers})
        model = propagation.PropagationModel(stack, {"F.Cu", "In2.Cu"})
        with self.assertRaises(propagation.Unsupported):
            model.conductor("In1.Cu", TRACK_WIDTH_MM)

    def test_an_inner_layer_with_one_reference_plane_is_not_a_microstrip(self):
        """The field is in dielectric on both sides; Hammerstad does not apply."""
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        geometry = stack.reference_geometry("In1.Cu", {"F.Cu"})
        self.assertEqual(geometry.mode, propagation.EMBEDDED_MICROSTRIP)
        model = propagation.PropagationModel(stack, {"F.Cu"})
        with self.assertRaises(propagation.Unsupported) as caught:
            model.conductor("In1.Cu", TRACK_WIDTH_MM)
        self.assertIn("understate", str(caught.exception))

    def test_an_outer_layer_with_one_reference_plane_is_a_microstrip(self):
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        self.assertEqual(
            stack.reference_geometry("F.Cu", {"In1.Cu"}).mode,
            propagation.MICROSTRIP)

    def test_an_unknown_model_name_refuses(self):
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        with self.assertRaises(propagation.PropagationError):
            propagation.PropagationModel(stack, {"In1.Cu"},
                                         model="whatever-sounds-plausible")

    def test_a_declared_propagation_constant_needs_provenance(self):
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        with self.assertRaises(propagation.PropagationError):
            propagation.PropagationModel(
                stack, {"In1.Cu"}, model=propagation.DECLARED_EFFECTIVE,
                declared_layers={"F.Cu": {"ps_per_mm": 6.0}})

    def test_a_declared_propagation_constant_is_used_and_labelled(self):
        stack = stackup_physical.from_declaration(
            {"layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        model = propagation.PropagationModel(
            stack, {"In1.Cu"}, model=propagation.DECLARED_EFFECTIVE,
            declared_layers={"F.Cu": {"ps_per_mm": 6.0,
                                      "provenance": "fixture value"}})
        record = model.conductor("F.Cu", TRACK_WIDTH_MM)
        self.assertEqual(record["ps_per_mm"], 6.0)
        self.assertEqual(record["fidelity"], propagation.DECLARED_PROPAGATION)

    def test_thickness_correction_widens_the_trace(self):
        corrected = propagation.thickness_corrected_width(
            TRACK_WIDTH_MM, FIXTURE_HEIGHT_MM, FIXTURE_OUTER_COPPER_MM)
        self.assertGreater(corrected, TRACK_WIDTH_MM)
        self.assertGreater(
            propagation.hammerstad_effective_permittivity(
                FIXTURE_EPSILON_R, corrected, FIXTURE_HEIGHT_MM),
            propagation.hammerstad_effective_permittivity(
                FIXTURE_EPSILON_R, TRACK_WIDTH_MM, FIXTURE_HEIGHT_MM))

    def test_a_missing_material_figure_is_never_substituted(self):
        layers = [dict(entry, kind=_kind(entry)) for entry in FIXTURE_STACKUP]
        layers[1] = dict(layers[1], epsilon_r=None)
        stack = stackup_physical.from_declaration({"layers": layers})
        model = propagation.PropagationModel(stack, {"In1.Cu"})
        with self.assertRaises(propagation.PropagationError):
            model.conductor("F.Cu", TRACK_WIDTH_MM)


def _kind(entry):
    if entry["type"] == "copper":
        return stackup_physical.COPPER
    if entry["type"] in ("core", "prepreg"):
        return stackup_physical.DIELECTRIC
    return stackup_physical.OTHER


# ---------------------------------------------------------------------------
# via treatment
# ---------------------------------------------------------------------------

class ViaTreatment(unittest.TestCase):

    def test_by_default_a_via_contributes_length_but_no_delay(self):
        fixture = make(tag="via_none")
        state = fixture.analysis()
        record = _find(state, "layer_change")
        self.assertEqual(len(record["delay"]["vias"]), 1)
        via = record["delay"]["vias"][0]
        self.assertEqual(via["model"], propagation.VIA_NONE)
        self.assertAlmostEqual(via["vertical_length_mm"], VIA_VERTICAL_MM,
                               places=6)
        self.assertEqual(via["delay_ps"], 0.0)

    def test_the_geometric_via_model_is_length_over_velocity_and_says_so(self):
        def mutate(document, _project):
            document["timing"]["propagation"]["via_delay_model"] = \
                propagation.VIA_GEOMETRIC
        fixture = make(mutate=mutate, tag="via_geom")
        record = _find(fixture.analysis(), "layer_change")
        via = record["delay"]["vias"][0]
        expected = (VIA_VERTICAL_MM * math.sqrt(FIXTURE_EPSILON_R)
                    / 0.299792458)
        self.assertAlmostEqual(via["delay_ps"], expected, places=4)
        self.assertIn("first-order", via["note"])
        self.assertAlmostEqual(
            record["delay"]["delay_ps"],
            PATH_VIA_MM * expected_microstrip_ps_per_mm() + expected,
            places=4)


class WhatADelayActuallyNeeds(unittest.TestCase):
    """Insufficiency has to be exact, or it blocks results it did not need to.

    "The stackup is incomplete" and "the delay cannot be derived" are different
    questions with different answers, and conflating them means a board that
    obtained everything a delay needs is still told it has nothing.
    """

    def _stack(self, drop=()):
        layers = []
        for entry in FIXTURE_STACKUP:
            entry = dict(entry, kind=_kind(entry))
            for field in drop:
                if field in entry:
                    entry[field] = None
            layers.append(entry)
        return stackup_physical.from_declaration({"layers": layers})

    def test_a_missing_loss_tangent_does_not_block_a_delay(self):
        """Loss tangent sets attenuation, not velocity."""
        stack = self._stack(drop=("loss_tangent",))
        model = propagation.PropagationModel(stack, {"In1.Cu"})
        record = model.conductor("F.Cu", TRACK_WIDTH_MM)
        self.assertAlmostEqual(record["ps_per_mm"],
                               expected_microstrip_ps_per_mm(), places=6)

    def test_a_missing_copper_thickness_does_not_block_the_zero_thickness_model(self):
        stack = self._stack()
        for layer in stack.layers:
            if layer.is_copper:
                layer.thickness_mm = None
        model = propagation.PropagationModel(stack, {"In1.Cu"})
        record = model.conductor("F.Cu", TRACK_WIDTH_MM)
        self.assertAlmostEqual(record["ps_per_mm"],
                               expected_microstrip_ps_per_mm(), places=6)

    def test_but_it_does_block_the_thickness_corrected_model(self):
        stack = self._stack()
        for layer in stack.layers:
            if layer.is_copper:
                layer.thickness_mm = None
        model = propagation.PropagationModel(stack, {"In1.Cu"},
                                             model=propagation.HAMMERSTAD_T)
        with self.assertRaises(propagation.PropagationError):
            model.conductor("F.Cu", TRACK_WIDTH_MM)

    def test_completeness_still_reports_both_and_says_what_each_is_for(self):
        """STACK.PHYSICAL's subject is the whole stackup, not just delay."""
        stack = self._stack(drop=("loss_tangent",))
        issues = stack.completeness()
        self.assertTrue(issues)
        self.assertTrue(all("needed_for" in issue for issue in issues), issues)
        self.assertTrue(any("loss" in issue["needed_for"] for issue in issues))

    def test_a_via_across_an_unknown_stackup_reports_no_length_not_a_crash(self):
        """The failure that took down the first real-board run."""
        layers = [dict(entry, kind=_kind(entry), thickness_mm=None,
                       epsilon_r=None, loss_tangent=None)
                  for entry in FIXTURE_STACKUP]
        stack = stackup_physical.from_declaration({"layers": layers})
        model = propagation.PropagationModel(stack, {"In1.Cu", "In2.Cu"})
        record = model.via({"from_layer": "F.Cu", "to_layer": "B.Cu",
                            "via_top_layer": "F.Cu",
                            "via_bottom_layer": "B.Cu"})
        self.assertIsNone(record["vertical_length_mm"])
        self.assertEqual(record["delay_ps"], 0.0)

    def test_the_same_via_refuses_when_a_delay_is_actually_asked_for(self):
        layers = [dict(entry, kind=_kind(entry), thickness_mm=None,
                       epsilon_r=None, loss_tangent=None)
                  for entry in FIXTURE_STACKUP]
        stack = stackup_physical.from_declaration({"layers": layers})
        model = propagation.PropagationModel(stack, {"In1.Cu", "In2.Cu"},
                                             via_model=propagation.VIA_GEOMETRIC)
        with self.assertRaises(propagation.Unsupported):
            model.via({"from_layer": "F.Cu", "to_layer": "B.Cu",
                       "via_top_layer": "F.Cu", "via_bottom_layer": "B.Cu"})


def _find(state, path_id):
    for _interface, record in state.all_paths():
        if record["resolved"].id == path_id:
            return record
    raise AssertionError("no resolved path {!r}".format(path_id))


# ---------------------------------------------------------------------------
# 5-7: applicability and fail-closed behaviour
# ---------------------------------------------------------------------------

class ApplicabilityAndFailClosed(unittest.TestCase):

    def test_a_board_without_timing_policy_gets_non_applicable_gates(self):
        def mutate(document, _project):
            document.pop("timing")
        fixture = make(mutate=mutate, tag="notiming")
        results = fixture.gates(only=TIMING_GATES)
        for gate_id in sorted(TIMING_GATES):
            self.assertEqual(results[gate_id].status, Status.NOT_APPLICABLE,
                             "{}: {}".format(gate_id,
                                             results[gate_id].reason))
            self.assertNotIn(results[gate_id].status, Status.BLOCKING)

    def test_required_stackup_with_no_physical_data_fails_closed(self):
        """No (stackup ...) in the board and require_complete: the gate fails."""
        fixture = make(with_stackup=False, tag="nostack")
        results = fixture.gates(only={"STACK.PHYSICAL"})
        self.assertEqual(results["STACK.PHYSICAL"].status, Status.FAIL)
        self.assertIn("incomplete", results["STACK.PHYSICAL"].reason)

    def test_a_declared_delay_limit_that_cannot_be_evaluated_blocks(self):
        """An unevaluated requirement is not a satisfied one."""
        def mutate(document, _project):
            document["timing"]["interfaces"]["series"]["limits"] = {
                "max_delay_ps": 1000.0}
        fixture = make(mutate=mutate, with_stackup=False, tag="nodelay")
        results = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})
        result = results["TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("unevaluated" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_declared_skew_limit_that_cannot_be_evaluated_blocks(self):
        def mutate(document, _project):
            group = document["timing"]["interfaces"]["series"]["groups"]["loads"]
            group["max_skew_ps"] = 500.0
        fixture = make(mutate=mutate, with_stackup=False, tag="noskew")
        result = fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})[
            "TIMING.INTERCONNECT_SKEW"]
        self.assertEqual(result.status, Status.FAIL)

    def test_a_length_limit_is_still_evaluable_without_material_data(self):
        """Geometry alone answers a geometry question, and is labelled as such."""
        def mutate(document, _project):
            group = document["timing"]["interfaces"]["series"]["groups"]["loads"]
            group["max_length_spread_mm"] = LENGTH_FIRST_TO_SECOND_MM + 0.5
            document["timing"]["physical_stackup"]["require_complete"] = False
        fixture = make(mutate=mutate, with_stackup=False, tag="lengthonly")
        results = fixture.gates(only={"TIMING.INTERCONNECT_SKEW",
                                      "STACK.PHYSICAL"})
        self.assertEqual(results["TIMING.INTERCONNECT_SKEW"].status,
                         Status.PASS)
        group = results["TIMING.INTERCONNECT_SKEW"].measurements["groups"][0]
        self.assertEqual(group["measured_in"], "mm")
        self.assertIsNone(group["skew_ps"])
        self.assertEqual(results["STACK.PHYSICAL"].status,
                         Status.NOT_APPLICABLE)

    def test_a_length_limit_that_is_exceeded_fails(self):
        def mutate(document, _project):
            group = document["timing"]["interfaces"]["series"]["groups"]["loads"]
            group["max_length_spread_mm"] = LENGTH_FIRST_TO_SECOND_MM - 0.5
        fixture = make(mutate=mutate, tag="lengthfail")
        result = fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})[
            "TIMING.INTERCONNECT_SKEW"]
        self.assertEqual(result.status, Status.FAIL)

    def test_a_missing_model_file_fails_closed(self):
        def mutate(document, project):
            document["timing"]["models"] = {
                "physical_stackup": "models/not-here.json"}
        fixture = make(mutate=mutate, tag="nomodel")
        result = fixture.gates(only={"PROV.TIMING_MODELS"})[
            "PROV.TIMING_MODELS"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("does not exist" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_missing_stackup_supplement_blocks_rather_than_being_ignored(self):
        def mutate(document, _project):
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/absent.json"
        fixture = make(mutate=mutate, tag="nosupp")
        results = fixture.gates(only={"STACK.PHYSICAL",
                                      "TIMING.PATH_INTEGRITY"})
        for gate_id, result in sorted(results.items()):
            self.assertEqual(result.status, Status.ERROR,
                             "{}: {}".format(gate_id, result.reason))

    def test_an_interface_that_declares_no_route_blocks(self):
        def mutate(document, _project):
            document["timing"]["interfaces"]["series"].pop("routes")
        fixture = make(mutate=mutate, tag="noroute")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.ERROR)

    def test_setup_hold_is_not_applicable_without_device_timing(self):
        fixture = make(tag="nodevice")
        result = fixture.gates(only={"TIMING.SETUP_HOLD"})["TIMING.SETUP_HOLD"]
        self.assertEqual(result.status, Status.NOT_APPLICABLE)

    def test_setup_hold_with_an_incomplete_device_model_blocks(self):
        def mutate(document, _project):
            document["timing"]["device_timing"] = {
                "receivers": {"L1": {"source_tco_ps": 1000.0}}}
        fixture = make(mutate=mutate, tag="halfdevice")
        result = fixture.gates(only={"TIMING.SETUP_HOLD"})["TIMING.SETUP_HOLD"]
        self.assertEqual(result.status, Status.ERROR)
        self.assertTrue(result.findings)


# ---------------------------------------------------------------------------
# declaration errors refuse rather than approximate
# ---------------------------------------------------------------------------

class DeclarationsThatCannotBeSatisfied(unittest.TestCase):

    def test_an_intermediate_fan_out_refuses(self):
        def mutate(document, _project):
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            steps[0]["to"] = "^R1\\.\\d+$"
            steps[0]["net"] = "SIG_B"
        fixture = make(mutate=mutate, tag="fanout")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)

    def test_a_traversal_that_does_not_change_net_refuses(self):
        def mutate(document, _project):
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            # Cross R1 from pad 1 back to pad 1, and follow it with copper on
            # the net that pad is really on, so the net-agreement check is
            # satisfied and the traversal itself is what has to be caught.
            steps[1]["to_pad"] = "1"
            steps[2]["net"] = "SIG_A"
            steps[2]["from"] = "R1.1"
            steps[2]["to"] = "D1.1"
        fixture = make(mutate=mutate, tag="samenet")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("joins nothing" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_path_that_skips_its_series_component_is_caught(self):
        """Exactly the defect the abstraction exists to prevent."""
        def mutate(document, _project):
            interface = document["timing"]["interfaces"]["series"]
            interface["routes"] = {"template": {
                "id": "post_series_only_{load}",
                "steps": [{"kind": "copper", "net": "SIG_B", "from": "R1.2",
                           "to": "^{load}\\.1$"}]},
                "bindings": [{"load": "L1"}, {"load": "L2"}]}
        fixture = make(mutate=mutate, tag="skipseries")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("wrong side" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_an_unimplemented_step_kind_refuses(self):
        with self.assertRaises(PathError):
            electrical_path.step_from_spec({"kind": "waveguide"}, 0)

    def test_a_step_declaring_no_net_refuses(self):
        with self.assertRaises(PathError):
            electrical_path.step_from_spec({"kind": "copper", "from": "A.1",
                                            "to": "B.1"}, 0)

    def test_duplicate_path_ids_refuse(self):
        with self.assertRaises(PathError):
            electrical_path.paths_from_spec({"paths": [
                {"id": "same", "steps": [{"kind": "copper", "net": "N",
                                          "from": "A.1", "to": "B.1"}]},
                {"id": "same", "steps": [{"kind": "copper", "net": "N",
                                          "from": "A.1", "to": "C.1"}]}]})

    def test_a_template_token_with_no_binding_refuses(self):
        with self.assertRaises(PathError):
            electrical_path.paths_from_spec({
                "template": {"id": "x_{missing}", "steps": [
                    {"kind": "copper", "net": "N", "from": "A.1",
                     "to": "B.1"}]},
                "bindings": [{"present": "1"}]})

    def test_a_path_that_starts_on_a_component_refuses(self):
        with self.assertRaises(PathError):
            electrical_path.paths_from_spec({"paths": [{
                "id": "starts_wrong",
                "steps": [{"kind": "component", "reference": "R1",
                           "from_pad": "1", "to_pad": "2"},
                          {"kind": "copper", "net": "N", "from": "R1.2",
                           "to": "B.1"}]}]})


# ---------------------------------------------------------------------------
# 8: provenance
# ---------------------------------------------------------------------------

class TimingInputsAreTracked(unittest.TestCase):

    @staticmethod
    def _with_model(document, project):
        directory = os.path.join(project, "models")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "physical_stackup.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"layers": [dict(entry, kind=_kind(entry))
                                  for entry in FIXTURE_STACKUP],
                       "total_thickness_mm": 1.305}, handle, indent=2)
        document["timing"]["models"] = {
            "physical_stackup": "models/physical_stackup.json"}
        return path

    def test_a_declared_model_file_is_a_closure_member(self):
        fixture = make(mutate=self._with_model, tag="model")
        result = fixture.gates(only={"PROV.TIMING_MODELS"})[
            "PROV.TIMING_MODELS"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertEqual(result.measurements["models"]["physical_stackup"],
                         "models/physical_stackup.json")
        self.assertTrue(result.evidence)

    def test_changing_a_model_file_changes_the_source_closure(self):
        fixture = make(mutate=self._with_model, tag="modelhash")
        policy = canonical.AttributePolicy.load(paths.ATTRIBUTES)
        before = cleanroom.closure_digest(
            cleanroom.source_closure(fixture.manifest, policy))
        target = os.path.join(fixture.project, "models",
                              "physical_stackup.json")
        document = json.load(open(target, encoding="utf-8"))
        document["layers"][1]["epsilon_r"] = 4.2
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
        after = cleanroom.closure_digest(
            cleanroom.source_closure(Manifest(fixture.manifest_path), policy))
        self.assertNotEqual(before, after,
                            "a material figure changed and the closure did "
                            "not, so every committed result would still look "
                            "fresh")

    def test_timing_policy_is_part_of_the_configuration_identity(self):
        fixture = make(tag="cfgid")
        before = cleanroom.configuration_identity(fixture.manifest)
        document = json.load(open(fixture.manifest_path, encoding="utf-8"))
        document["timing"]["interfaces"]["series"]["groups"]["loads"][
            "max_length_spread_mm"] = 99.0
        other = write_manifest(
            tempfile.mkdtemp(prefix="pcbqa_cfgid_"), document)
        after = cleanroom.configuration_identity(Manifest(other))
        self.assertNotEqual(before, after,
                            "a timing limit changed without changing the "
                            "configuration identity")
        shutil.rmtree(os.path.dirname(other), ignore_errors=True)

    def test_a_model_file_outside_the_closure_globs_is_reported(self):
        def mutate(document, project):
            path = self._with_model(document, project)
            document["reports"]["source_closure"] = ["*.kicad_pcb"]
            return path
        fixture = make(mutate=mutate, tag="modeloutside")
        result = fixture.gates(only={"PROV.TIMING_MODELS"})[
            "PROV.TIMING_MODELS"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("source closure" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)


# ---------------------------------------------------------------------------
# stackup sourcing
# ---------------------------------------------------------------------------

class WhereAStackupComesFrom(unittest.TestCase):

    def test_a_board_with_no_stackup_block_says_so_rather_than_assuming_fr4(self):
        fixture = make(with_stackup=False, tag="source_none")
        stack = fixture.analysis().stackup
        self.assertTrue(stack.empty)
        self.assertEqual(stack.source, stackup_physical.NATIVE)
        self.assertTrue(any("no (setup (stackup" in note
                            for note in stack.notes), stack.notes)
        self.assertIsNotNone(stack.declared_total_thickness_mm,
                             "overall board thickness is known even when the "
                             "stackup is not")

    def test_native_data_are_read_from_the_board(self):
        fixture = make(tag="source_native")
        stack = fixture.analysis().stackup
        self.assertEqual(stack.source, stackup_physical.NATIVE)
        self.assertEqual(stack.copper_layer_names,
                         ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(stack.completeness(), [])

    def test_a_supplement_fills_gaps_where_the_board_is_silent(self):
        def mutate(document, project):
            directory = os.path.join(project, "models")
            os.makedirs(directory, exist_ok=True)
            with open(os.path.join(directory, "supplement.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"layers": [dict(entry, kind=_kind(entry))
                                      for entry in FIXTURE_STACKUP]}, handle)
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/supplement.json"
        fixture = make(mutate=mutate, with_stackup=False, tag="supp")
        stack = fixture.analysis().stackup
        self.assertEqual(stack.completeness(), [])
        self.assertIn(stackup_physical.DECLARED, stack.source)

    def test_a_supplement_may_not_contradict_the_board(self):
        def mutate(document, project):
            directory = os.path.join(project, "models")
            os.makedirs(directory, exist_ok=True)
            layers = [dict(entry, kind=_kind(entry))
                      for entry in FIXTURE_STACKUP]
            layers[1] = dict(layers[1], epsilon_r=9.9)
            with open(os.path.join(directory, "supplement.json"), "w",
                      encoding="utf-8") as handle:
                json.dump({"layers": layers}, handle)
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/supplement.json"
        fixture = make(mutate=mutate, with_stackup=True, tag="conflict")
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("contradicts", result.reason)

    def test_reference_planes_come_from_poured_copper(self):
        fixture = make(tag="planes")
        self.assertEqual(sorted(fixture.analysis().reference_layers),
                         ["In1.Cu", "In2.Cu"])


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class BackendsStayOptional(unittest.TestCase):

    def test_no_solver_is_imported_by_loading_the_gates(self):
        """Importing a gate module must not drag in an EM solver."""
        loaded = [name for name in sys.modules
                  if "openems" in name.lower() or "openEMS" in name]
        self.assertEqual(
            [n for n in loaded if not n.startswith("pcbqa.backends")], [],
            "an EM solver was imported by ordinary validation: {}".format(
                loaded))

    def test_the_analytic_backend_is_always_available(self):
        from pcbqa import backends
        ok, detail = backends.available(backends.ANALYTIC)
        self.assertTrue(ok, detail)

    def test_an_unknown_backend_is_refused_not_approximated(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendError):
            backends.require("magic-solver")

    def test_a_board_requiring_an_unavailable_solver_blocks(self):
        from pcbqa.backends import openems
        if openems.executable():                       # pragma: no cover
            self.skipTest("openEMS is installed here, so unavailability "
                          "cannot be exercised")

        def mutate(document, _project):
            document["timing"]["propagation"]["backend"] = "openems"
        fixture = make(mutate=mutate, tag="openems")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("openems", result.reason.lower())

    def test_the_solver_stub_refuses_rather_than_estimating(self):
        from pcbqa.backends import openems
        with self.assertRaises(NotImplementedError):
            openems.extract(None, None)


# ---------------------------------------------------------------------------
# 9: nothing that already worked has changed
# ---------------------------------------------------------------------------

class ExistingBehaviourIsUnchanged(unittest.TestCase):

    def test_the_structural_stackup_gate_still_answers_its_own_question(self):
        """STACK.NATIVE_VS_MANIFEST is about layer order and planes, still."""
        def mutate(document, _project):
            document["stackup"] = {"expected": [
                {"role": "signal"},
                {"role": "plane", "plane_net": "GND"},
                {"role": "plane", "plane_net": "GND"},
                {"role": "signal"}]}
        fixture = make(mutate=mutate, tag="structural")
        result = fixture.gates(only={"STACK.NATIVE_VS_MANIFEST"})[
            "STACK.NATIVE_VS_MANIFEST"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertEqual(result.measurements["copper_layers"], 4)
        # It reads no thickness, material or permittivity: that is the other
        # gate's subject, and this one's semantics are unchanged.
        self.assertNotIn("physical_stackup", result.measurements)

    def test_the_structural_gate_still_fails_on_a_plane_disagreement(self):
        def mutate(document, _project):
            document["stackup"] = {"expected": [
                {"role": "signal"},
                {"role": "plane", "plane_net": "SOMETHING_ELSE"},
                {"role": "plane", "plane_net": "GND"},
                {"role": "signal"}]}
        fixture = make(mutate=mutate, tag="structural_bad")
        result = fixture.gates(only={"STACK.NATIVE_VS_MANIFEST"})[
            "STACK.NATIVE_VS_MANIFEST"]
        self.assertEqual(result.status, Status.FAIL)

    def test_every_timing_limit_is_a_typed_manifest_constraint(self):
        """What CFG.THRESHOLD_PARITY demands of every gate, checked here too."""
        def mutate(document, _project):
            interface = document["timing"]["interfaces"]["series"]
            interface["limits"] = {"max_delay_ps": 100000.0}
            interface["groups"]["loads"]["max_skew_ps"] = 100000.0
            interface["groups"]["loads"]["max_length_spread_mm"] = 100.0
        fixture = make(mutate=mutate, tag="parity")
        results = fixture.gates(only=TIMING_GATES)
        applied = {}
        for result in results.values():
            applied.update(result.limits)
        self.assertTrue(applied, "the timing gates applied no limits at all")
        for name, record in sorted(applied.items()):
            self.assertIsNotNone(record.get("units"), name)
            self.assertTrue(record.get("provenance"), name)
            key = record["manifest_key"]
            self.assertTrue(fixture.manifest.has(key),
                            "{} cites {} which the manifest does not "
                            "have".format(name, key))
            self.assertEqual(record["value"], fixture.manifest.get(key), name)

    def test_the_timing_gates_are_registered_and_ordered_after_geometry(self):
        registered = {entry["id"]: entry for entry in core.registered()}
        for gate_id in TIMING_GATES:
            self.assertIn(gate_id, registered)
        self.assertLess(registered["STACK.PHYSICAL"]["order"],
                        registered["TIMING.INTERCONNECT_DELAY"]["order"])

    def test_generic_source_names_no_board(self):
        """A second pass over just the new modules, cheap and specific."""
        forbidden = ("PDM", "MSM261", "microphone", "SN74LVC")
        for name in ("electrical_path.py", "propagation.py",
                     "stackup_physical.py", "backends/__init__.py",
                     "backends/openems.py", "gates/g_timing.py"):
            path = os.path.join(paths.PACKAGE, *name.split("/"))
            text = open(path, encoding="utf-8").read()
            for token in forbidden:
                self.assertNotIn(token.lower(), text.lower(),
                                 "{} names {}".format(name, token))


# ---------------------------------------------------------------------------
# a registered consumer board, if one declares a timing policy
# ---------------------------------------------------------------------------

@consumer.needed
class ARegisteredConsumersTimingPolicy(unittest.TestCase):
    """The integration half: a real board, checked against its own declaration.

    Nothing here knows anything about the board. Every expectation is read out
    of whatever that board's manifest declares, so this passes for any consumer
    with a timing policy and skips for any consumer without one. What it proves
    is the property a fixture cannot: that the declaration mechanism survives
    contact with a real netlist, real copper and a real stackup.
    """

    @classmethod
    def setUpClass(cls):
        cls.document = consumer.document()
        if not cls.document.get("timing", {}).get("interfaces"):
            raise unittest.SkipTest(
                "the registered consumer board declares no timing.interfaces")
        cls.manifest = Manifest(consumer.require())
        cls.workdir = tempfile.mkdtemp(prefix="pcbqa_consumer_timing_")
        cls.ctx = Context(cls.manifest, cls.workdir)
        cls.results = {r.gate_id: r
                       for r in core.run_all(cls.ctx, only=TIMING_GATES)}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def _rows(self):
        rows = []
        interfaces = self.results["TIMING.PATH_INTEGRITY"].measurements.get(
            "interfaces", {})
        for name, interface in sorted(interfaces.items()):
            for row in interface["paths"]:
                rows.append((name, row))
        return rows

    def test_no_timing_gate_errors_on_a_real_board(self):
        """An ERROR here means the gate could not be evaluated at all."""
        errored = {gate_id: result.reason
                   for gate_id, result in self.results.items()
                   if result.status == Status.ERROR}
        self.assertEqual(errored, {})

    def test_every_declared_route_resolves(self):
        result = self.results["TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS,
                         "{}\n{}".format(result.reason, result.findings[:4]))

    def test_the_resolved_count_matches_what_the_board_declared(self):
        interfaces = self.document["timing"]["interfaces"]
        measured = self.results["TIMING.PATH_INTEGRITY"].measurements[
            "interfaces"]
        checked = 0
        for name, declared in sorted(interfaces.items()):
            expected = declared.get("expected_path_count")
            if expected is None:
                continue
            checked += 1
            self.assertEqual(measured[name]["resolved_paths"], expected, name)
        if not checked:
            self.skipTest("the consumer declares no expected_path_count")

    def test_every_path_crosses_the_components_its_route_declares(self):
        """The defect this layer exists to prevent, checked on real copper."""
        declared_crossings = {}
        for _name, interface in sorted(
                self.document["timing"]["interfaces"].items()):
            for route in _declared_routes(interface):
                declared_crossings[route["id"]] = [
                    step["reference"] for step in route["steps"]
                    if step.get("kind") == "component"]
        self.assertTrue(declared_crossings,
                        "the consumer declares no routes at all")
        for _interface, row in self._rows():
            expected = declared_crossings.get(row["path"])
            self.assertIsNotNone(expected, row["path"])
            self.assertEqual(row["crosses"], expected, row["path"])

    def test_a_path_that_crosses_a_component_is_longer_than_its_last_step(self):
        """A crossing means copper before it, and that copper is in the total."""
        checked = 0
        for _interface, row in self._rows():
            if not row["crosses"]:
                continue
            checked += 1
            copper = [s for s in row["steps"] if s["kind"] == "copper"]
            self.assertGreater(len(copper), 1, row["path"])
            self.assertGreater(row["copper_length_mm"],
                               copper[-1]["length_mm"], row["path"])
        if not checked:
            self.skipTest("no consumer path crosses a component")

    def test_results_identify_the_stackup_source_and_the_model(self):
        measurements = self.results["TIMING.INTERCONNECT_DELAY"].measurements
        self.assertIn(measurements["physical_stackup_source"],
                      (stackup_physical.NATIVE, stackup_physical.DECLARED,
                       stackup_physical.MERGED))
        self.assertIn(measurements["propagation_model"], propagation.MODELS)
        self.assertIn(measurements["via_delay_model"], propagation.VIA_MODELS)
        self.assertTrue(measurements["backend"])

    def test_no_delay_is_reported_where_the_stackup_cannot_support_one(self):
        """The whole point: absent material data produce absence, not numbers."""
        for row in self.results["TIMING.INTERCONNECT_DELAY"].measurements[
                "paths"]:
            if row["insufficient"]:
                self.assertIsNone(row["delay_ps"], row["path"])
                self.assertEqual(row["fidelity"], propagation.GEOMETRY_ONLY,
                                 row["path"])
            else:
                self.assertIsNotNone(row["delay_ps"], row["path"])

    def test_a_declared_model_file_is_covered_by_provenance(self):
        if not self.document.get("timing", {}).get("models"):
            self.skipTest("the consumer declares no timing model files")
        result = self.results["PROV.TIMING_MODELS"]
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_the_skew_report_says_it_is_interconnect_only(self):
        scope = self.results["TIMING.INTERCONNECT_SKEW"].measurements["scope"]
        self.assertIn("NOT total clock arrival skew", scope)


def _declared_routes(interface):
    """The routes an interface declares, template expanded, as plain dicts."""
    spec = interface.get("routes") or {}
    routes = list(spec.get("paths") or [])
    if spec.get("template") is not None:
        routes.extend(electrical_path.expand_template(spec["template"],
                                                      spec["bindings"]))
    return routes


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
