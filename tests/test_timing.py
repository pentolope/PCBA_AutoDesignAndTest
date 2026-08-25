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

from pcbqa import (canonical, cleanroom, component_models, core,    # noqa: E402
                   electrical_path, geom, propagation, stackup_physical)
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

# A T-junction: a stub lands in the middle of a run rather than at its end.
LENGTH_TEE_RUN_MM = 20.0           # D3.1 -> L5.1, straight through
LENGTH_TEE_TO_JUNCTION_MM = 10.0   # D3.1 -> where the stub lands
LENGTH_TEE_STUB_MM = 7.0           # the stub itself
# What the walk to the stub's load costs, split and unsplit. The unsplit graph
# charges the whole run whichever end you leave from; the split graph charges
# only the half actually travelled. Both are exactly calculable, which is the
# point of the fixture.
TEE_SPLIT_MM = LENGTH_TEE_TO_JUNCTION_MM + LENGTH_TEE_STUB_MM        # 17.0
TEE_UNSPLIT_MM = LENGTH_TEE_RUN_MM + LENGTH_TEE_STUB_MM              # 27.0

# A path that changes layer twice and changes width with it.
LENGTH_M_TOP_A_MM = 5.0
LENGTH_M_BOTTOM_MM = 10.0
LENGTH_M_TOP_B_MM = 7.0
WIDE_TRACK_MM = 0.4
PATH_MULTI_VIA_MM = (LENGTH_M_TOP_A_MM + LENGTH_M_BOTTOM_MM
                     + LENGTH_M_TOP_B_MM)                            # 22.0

# A series part the board says is not fitted.
LENGTH_DNP_IN_MM = 9.5
LENGTH_DNP_OUT_MM = 9.5

# A layer change made inside a footprint pad rather than through a via.
LENGTH_PTH_TOP_MM = 10.0
LENGTH_PTH_BOTTOM_MM = 8.0
PATH_PTH_MM = LENGTH_PTH_TOP_MM + LENGTH_PTH_BOTTOM_MM                # 18.0

# The pour stops here, so copper above it has no reference conductor.
ZONE_TOP_MM = 120.0
UNREFERENCED_Y_MM = 125.0
LENGTH_UNREFERENCED_MM = 15.0

# Two wide tracks meeting at forty-five degrees, so the shared region is long
# and no single cut point through it is the right one.
OBLIQUE_WIDTH_MM = 1.0

# One inner plane is interrupted across this band and the other is not, so a
# route here is referenced to a broken plane or a continuous one depending
# only on which layer it is on.
SPLIT_BAND_LOW_MM = 91.5
SPLIT_BAND_HIGH_MM = 94.5
SPLIT_ROUTE_Y_MM = 93.0
LENGTH_SPLIT_ROUTE_MM = 15.0

# Two stubs landing part-way along one run, so a walk to the far one passes
# through two ambiguous junctions rather than one.
MULTI_JUNCTION_Y_MM = 70.0
MULTI_JUNCTION_STUB_Y_MM = 66.0
LENGTH_TO_FIRST_STUB_MM = 8.0
LENGTH_TO_SECOND_STUB_MM = 16.0
LENGTH_MULTI_STUB_MM = 4.0

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


def build_board(directory, with_stackup=True, fill_zones=True):
    """The fixture board: a series-resistor fan-out and a via transition."""
    board = synth.new_board(layers=4, size_mm=60.0)
    gnd = synth.add_net(board, "GND")
    sig_a = synth.add_net(board, "SIG_A")
    sig_b = synth.add_net(board, "SIG_B")
    sig_v = synth.add_net(board, "SIG_V")
    sig_t = synth.add_net(board, "SIG_T")
    sig_m = synth.add_net(board, "SIG_M")
    sig_d = synth.add_net(board, "SIG_D")
    sig_e = synth.add_net(board, "SIG_E")
    sig_p = synth.add_net(board, "SIG_P")
    sig_q = synth.add_net(board, "SIG_Q")
    sig_r = synth.add_net(board, "SIG_R")
    sig_w = synth.add_net(board, "SIG_W")
    sig_u = synth.add_net(board, "SIG_U")
    sig_x = synth.add_net(board, "SIG_X")
    sig_s = synth.add_net(board, "SIG_S")
    sig_sb = synth.add_net(board, "SIG_SB")
    sig_mj = synth.add_net(board, "SIG_MJ")
    sig_sparse = synth.add_net(board, "SIG_SPARSE")

    # Both inner layers are poured on GND, so they are reference planes
    # because copper is actually on them and not because a file says so.
    # In2.Cu is poured in one piece. In1.Cu is poured either side of a band,
    # so copper in that band has a continuous plane two layers down and a
    # broken one directly beneath it - which is the whole point: whether a
    # formula applies depends on the plane it is actually referenced to, not
    # on there being reference copper somewhere in the stack.
    synth.add_zone(board, gnd, (pcbnew.In2_Cu,),
                   (72.0, 72.0, 128.0, ZONE_TOP_MM), fill=fill_zones)
    synth.add_zone(board, gnd, (pcbnew.In1_Cu,),
                   (72.0, 72.0, 128.0, SPLIT_BAND_LOW_MM), fill=fill_zones)
    synth.add_zone(board, gnd, (pcbnew.In1_Cu,),
                   (72.0, SPLIT_BAND_HIGH_MM, 128.0, ZONE_TOP_MM),
                   fill=fill_zones)

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

    # --- a T-junction: the stub lands mid-run, not at an end --------------
    synth.add_pad_footprint(board, "D3", 85.0, 85.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_t)
    synth.add_pad_footprint(board, "L4", 95.0, 78.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_t)
    synth.add_pad_footprint(board, "L5", 105.0, 85.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_t)
    synth.add_track(board, (85.0, 85.0), (105.0, 85.0), net=sig_t,
                    width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (95.0, 85.0), (95.0, 78.0), net=sig_t,
                    width_mm=TRACK_WIDTH_MM)

    # --- two layer changes and a width change ----------------------------
    synth.add_pad_footprint(board, "D5", 85.0, 75.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_m)
    synth.add_pad_footprint(board, "L7", 107.0, 75.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_m)
    synth.add_track(board, (85.0, 75.0), (90.0, 75.0), net=sig_m,
                    layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)
    synth.add_via(board, 90.0, 75.0, net=sig_m)
    synth.add_track(board, (90.0, 75.0), (100.0, 75.0), net=sig_m,
                    layer=pcbnew.B_Cu, width_mm=WIDE_TRACK_MM)
    synth.add_via(board, 100.0, 75.0, net=sig_m)
    synth.add_track(board, (100.0, 75.0), (107.0, 75.0), net=sig_m,
                    layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)

    # --- a series part the board says is not fitted -----------------------
    synth.add_pad_footprint(board, "D4", 85.0, 115.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_d)
    unfitted, _pads = synth.add_two_pad_footprint(
        board, "RD1", 95.0, 115.0, 1.0, (sig_d, sig_e), value="0R")
    unfitted.SetDNP(True)
    synth.add_pad_footprint(board, "L6", 105.0, 115.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_e)
    synth.add_track(board, (85.0, 115.0), (94.5, 115.0), net=sig_d,
                    width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (95.5, 115.0), (105.0, 115.0), net=sig_e,
                    width_mm=TRACK_WIDTH_MM)

    # --- a layer change made through a plated through-hole pad ------------
    synth.add_pad_footprint(board, "D6", 85.0, 90.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_p)
    synth.add_through_hole_footprint(board, "TH1", 95.0, 90.0, net=sig_p)
    synth.add_pad_footprint(board, "L8", 103.0, 90.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_p, flipped=True)
    synth.add_track(board, (85.0, 90.0), (95.0, 90.0), net=sig_p,
                    layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (95.0, 90.0), (103.0, 90.0), net=sig_p,
                    layer=pcbnew.B_Cu, width_mm=TRACK_WIDTH_MM)

    # --- two physical pads sharing one pad number, on one net -------------
    synth.add_pad_footprint(board, "D7", 85.0, 100.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_q)
    synth.add_through_hole_footprint(board, "DUP1", 95.0, 100.0, net=sig_q,
                                     numbers=("1", "1"))
    synth.add_track(board, (85.0, 100.0), (95.0, 100.0), net=sig_q,
                    width_mm=TRACK_WIDTH_MM)

    # --- and two sharing one number on different nets ---------------------
    synth.add_pad_footprint(board, "D8", 85.0, 110.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_r)
    synth.add_through_hole_footprint(board, "DUP2", 95.0, 110.0,
                                     numbers=("1", "1"),
                                     nets=(sig_r, sig_u))
    synth.add_track(board, (85.0, 110.0), (95.0, 110.0), net=sig_r,
                    width_mm=TRACK_WIDTH_MM)

    # --- two wide tracks meeting obliquely --------------------------------
    synth.add_pad_footprint(board, "D9", 80.0, 88.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_w)
    synth.add_pad_footprint(board, "L9", 100.0, 88.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_w)
    synth.add_pad_footprint(board, "L10", 96.0, 82.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_w)
    synth.add_track(board, (80.0, 88.0), (100.0, 88.0), net=sig_w,
                    width_mm=OBLIQUE_WIDTH_MM)
    synth.add_track(board, (90.0, 88.0), (96.0, 82.0), net=sig_w,
                    width_mm=OBLIQUE_WIDTH_MM)

    # --- a route that runs off the end of the pour ------------------------
    synth.add_pad_footprint(board, "D10", 85.0, UNREFERENCED_Y_MM,
                            pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=sig_x)
    synth.add_pad_footprint(board, "L11", 85.0 + LENGTH_UNREFERENCED_MM,
                            UNREFERENCED_Y_MM, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_x)
    synth.add_track(board, (85.0, UNREFERENCED_Y_MM),
                    (85.0 + LENGTH_UNREFERENCED_MM, UNREFERENCED_Y_MM),
                    net=sig_x, width_mm=TRACK_WIDTH_MM)

    # --- a through hole whose copper is only on the outer layers ----------
    synth.add_pad_footprint(board, "D13", 85.0, 84.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_sparse)
    synth.add_through_hole_footprint(
        board, "TH2", 95.0, 84.0, net=sig_sparse,
        copper_layers=(pcbnew.F_Cu, pcbnew.B_Cu))
    synth.add_pad_footprint(board, "L15", 103.0, 84.0, pcbnew.PAD_SHAPE_RECT,
                            (0.5, 0.5), net=sig_sparse, flipped=True)
    synth.add_track(board, (85.0, 84.0), (95.0, 84.0), net=sig_sparse,
                    layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)
    synth.add_track(board, (95.0, 84.0), (103.0, 84.0), net=sig_sparse,
                    layer=pcbnew.B_Cu, width_mm=TRACK_WIDTH_MM)

    # --- same footprint on two layers, over the interrupted band ----------
    for name, layer, net in (("S", pcbnew.F_Cu, sig_s),
                             ("SB", pcbnew.B_Cu, sig_sb)):
        flipped = layer == pcbnew.B_Cu
        synth.add_pad_footprint(board, "D" + name, 85.0, SPLIT_ROUTE_Y_MM,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=net,
                                flipped=flipped)
        synth.add_pad_footprint(board, "L" + name,
                                85.0 + LENGTH_SPLIT_ROUTE_MM,
                                SPLIT_ROUTE_Y_MM, pcbnew.PAD_SHAPE_RECT,
                                (0.5, 0.5), net=net, flipped=flipped)
        synth.add_track(board, (85.0, SPLIT_ROUTE_Y_MM),
                        (85.0 + LENGTH_SPLIT_ROUTE_MM, SPLIT_ROUTE_Y_MM),
                        net=net, layer=layer, width_mm=TRACK_WIDTH_MM)

    # --- one run, two stubs landing part-way along it ---------------------
    synth.add_pad_footprint(board, "D12", 80.0, MULTI_JUNCTION_Y_MM,
                            pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=sig_mj)
    synth.add_track(board, (80.0, MULTI_JUNCTION_Y_MM),
                    (104.0, MULTI_JUNCTION_Y_MM), net=sig_mj,
                    width_mm=TRACK_WIDTH_MM)
    for index, (reference, along) in enumerate(
            (("L13", LENGTH_TO_FIRST_STUB_MM),
             ("L14", LENGTH_TO_SECOND_STUB_MM))):
        x = 80.0 + along
        synth.add_track(board, (x, MULTI_JUNCTION_Y_MM),
                        (x, MULTI_JUNCTION_STUB_Y_MM), net=sig_mj,
                        width_mm=TRACK_WIDTH_MM)
        synth.add_pad_footprint(board, reference, x, MULTI_JUNCTION_STUB_Y_MM,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=sig_mj)

    path = os.path.join(directory, "timing.kicad_pcb")
    synth.save(board, path)
    if with_stackup:
        synth.write_physical_stackup(path, FIXTURE_STACKUP)
    for name in ("timing.kicad_sch", "timing.kicad_pro"):
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            fh.write("(fixture)\
" if name.endswith("sch") else "{}\
")
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
                "via_delay_model": {
                    "model": propagation.VIA_NONE,
                    "justification": "fixture: the vertical transit is "
                                     "deliberately excluded so the arithmetic "
                                     "under test is the copper formula alone"},
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

    def __init__(self, mutate=None, with_stackup=True, tag="timing",
                 fill_zones=True):
        self.root = tempfile.mkdtemp(prefix="pcbqa_" + tag + "_")
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.project, exist_ok=True)
        self.board_path = build_board(self.project, with_stackup=with_stackup,
                                      fill_zones=fill_zones)
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

    def geometry(self):
        return g_timing.geometry(self.ctx)

    def stackup(self):
        return g_timing.stackup(self.ctx)

    def geometry_resolver(self):
        return self.geometry().resolver

    def propagation(self):
        return g_timing.propagation_analysis(self.ctx)

    def delayed(self):
        """`{(path id, destination): delay record}` for every resolved path."""
        paths, state = self.geometry(), self.propagation()
        return {key: state.delays[key]
                for key in (paths.key(r) for _n, r in paths.all_paths())}

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
        cls.state = cls.fixture.geometry()
        cls.delays = cls.fixture.delayed()
        cls.by_path = {r["resolved"].id + "->" + r["resolved"].destination.label:
                       dict(r, delay=cls.delays[cls.state.key(r)])
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
        self.assertEqual(contributed["model_status"], "unmodelled")
        self.assertIn("R1", record["delay"]["unmodelled_component_delay"])
        # An unmodelled traversal is an acknowledged omission, so the total is
        # a lower bound rather than a value.
        self.assertTrue(record["delay"]["delay_is_lower_bound"])

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
            # Any omission - a component or a via the board chose not to
            # model - keeps the path from claiming analytic fidelity for the
            # whole of itself, however well the copper itself is modelled.
            omitted = bool(delay["component_traversals"]) or bool(delay["vias"])
            expected = (propagation.UNKNOWN_CONTRIBUTION if omitted
                        else propagation.ANALYTIC_TRANSMISSION_LINE)
            self.assertEqual(delay["fidelity"], expected, key)

    def test_a_path_with_nothing_omitted_claims_analytic_fidelity(self):
        """One copper step, no part to cross and no hole to go down."""
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"plain": {
                "description": "x",
                "routes": {"paths": [{"id": "plain", "steps": [
                    {"kind": "copper", "net": "SIG_A", "from": "D1.1",
                     "to": "R1.1"}]}]}}}
        fixture = make(mutate=mutate, tag="plainfid")
        record = _find(fixture, "plain")["delay"]
        self.assertFalse(record["delay_is_lower_bound"])
        self.assertEqual(record["fidelity"],
                         propagation.ANALYTIC_TRANSMISSION_LINE)
        self.assertAlmostEqual(record["delay_upper_ps"], record["delay_ps"],
                               places=9)

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
        self.assertEqual(measurements["backend_requested"], "analytic")
        self.assertEqual(measurements["backend_used"], "analytic")
        self.assertFalse(measurements["backend_fell_back"])
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
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
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
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        model = propagation.PropagationModel(stack, set())
        with self.assertRaises(propagation.Unsupported):
            model.conductor("F.Cu", TRACK_WIDTH_MM)

    def test_mixed_dielectric_stripline_refuses(self):
        """Different permittivities above and below is not one homogeneous medium."""
        layers = [dict(entry, kind=_kind(entry)) for entry in FIXTURE_STACKUP]
        layers[3] = dict(layers[3], epsilon_r=3.0)
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})
        model = propagation.PropagationModel(stack, {"F.Cu", "In2.Cu"})
        with self.assertRaises(propagation.Unsupported):
            model.conductor("In1.Cu", TRACK_WIDTH_MM)

    def test_an_inner_layer_with_one_reference_plane_is_not_a_microstrip(self):
        """The field is in dielectric on both sides; Hammerstad does not apply."""
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        geometry = stack.reference_geometry("In1.Cu", {"F.Cu"})
        self.assertEqual(geometry.mode, propagation.EMBEDDED_MICROSTRIP)
        model = propagation.PropagationModel(stack, {"F.Cu"})
        with self.assertRaises(propagation.Unsupported) as caught:
            model.conductor("In1.Cu", TRACK_WIDTH_MM)
        self.assertIn("understate", str(caught.exception))

    def test_an_outer_layer_with_one_reference_plane_is_a_microstrip(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        self.assertEqual(
            stack.reference_geometry("F.Cu", {"In1.Cu"}).mode,
            propagation.MICROSTRIP)

    def test_an_unknown_model_name_refuses(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        with self.assertRaises(propagation.PropagationError):
            propagation.PropagationModel(stack, {"In1.Cu"},
                                         model="whatever-sounds-plausible")

    def test_a_declared_propagation_constant_needs_provenance(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
                        for entry in FIXTURE_STACKUP]})
        with self.assertRaises(propagation.PropagationError):
            propagation.PropagationModel(
                stack, {"In1.Cu"}, model=propagation.DECLARED_EFFECTIVE,
                declared_layers={"F.Cu": {"ps_per_mm": 6.0}})

    def test_a_declared_propagation_constant_is_used_and_labelled(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": [dict(entry, kind=_kind(entry))
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
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})
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
        record = _find(fixture, "layer_change")
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
        record = _find(fixture, "layer_change")
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
        return stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})

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
        self.assertTrue(
            any(issue.get("field") == stackup_physical.NEEDS_LOSS_TANGENT
                for issue in issues), issues)

    def test_a_via_across_an_unknown_stackup_reports_no_length_not_a_crash(self):
        """The failure that took down the first real-board run."""
        layers = [dict(entry, kind=_kind(entry), thickness_mm=None,
                       epsilon_r=None, loss_tangent=None)
                  for entry in FIXTURE_STACKUP]
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})
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
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})
        model = propagation.PropagationModel(stack, {"In1.Cu", "In2.Cu"},
                                             via_model=propagation.VIA_GEOMETRIC)
        with self.assertRaises(propagation.Unsupported):
            model.via({"from_layer": "F.Cu", "to_layer": "B.Cu",
                       "via_top_layer": "F.Cu", "via_bottom_layer": "B.Cu"})


def _find(fixture, path_id):
    """One resolved path plus its delay record, by route id."""
    paths, delays = fixture.geometry(), fixture.delayed()
    for _interface, record in paths.all_paths():
        if record["resolved"].id == path_id:
            return dict(record, delay=delays[paths.key(record)])
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
        self.assertIn("does not state", results["STACK.PHYSICAL"].reason)

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

    def test_a_missing_stackup_supplement_blocks_the_gates_that_need_it(self):
        """And only those. Connectivity never needed the supplement."""
        def mutate(document, _project):
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/absent.json"
        fixture = make(mutate=mutate, tag="nosupp")
        results = fixture.gates(only={"STACK.PHYSICAL", "TIMING.PATH_INTEGRITY",
                                      "TIMING.INTERCONNECT_DELAY"})
        self.assertEqual(results["STACK.PHYSICAL"].status, Status.ERROR)
        self.assertEqual(results["TIMING.INTERCONNECT_DELAY"].status,
                         Status.ERROR)
        self.assertEqual(results["TIMING.PATH_INTEGRITY"].status, Status.PASS,
                         results["TIMING.PATH_INTEGRITY"].reason)

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
        """Caught at declaration now, which is earlier and board-independent.

        Two guards make this unreachable at resolution time: a traversal from
        a pad to itself is refused when the declaration is read, and a path
        whose copper steps name the same net twice is a loop. Both were added
        because "the part bridges nothing" is a statement about the
        declaration, and a declaration that cannot be a path on any board
        should never reach one.
        """
        def mutate(document, _project):
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            steps[1]["to_pad"] = "1"
            steps[2]["net"] = "SIG_A"
            steps[2]["from"] = "R1.1"
            steps[2]["to"] = "D1.1"
        fixture = make(mutate=mutate, tag="samenet")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("traverses nothing" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_the_resolver_still_guards_a_same_net_traversal(self):
        """Defence in depth: PathResolver is usable without the declaration
        checks in front of it, so it keeps its own guard."""
        source = open(os.path.join(paths.PACKAGE, "electrical_path.py"),
                      encoding="utf-8").read()
        self.assertIn("a traversal that does not change net joins", source)

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
        stack = fixture.stackup().stackup
        self.assertTrue(stack.empty)
        self.assertEqual(stack.source, stackup_physical.NATIVE)
        self.assertTrue(any("no (setup (stackup" in note
                            for note in stack.notes), stack.notes)
        self.assertIsNotNone(stack.declared_total_thickness_mm,
                             "overall board thickness is known even when the "
                             "stackup is not")

    def test_native_data_are_read_from_the_board(self):
        fixture = make(tag="source_native")
        stack = fixture.stackup().stackup
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
                json.dump({"provenance": "synthetic fixture values",
                           "layers": [dict(entry, kind=_kind(entry))
                                      for entry in FIXTURE_STACKUP]}, handle)
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/supplement.json"
        fixture = make(mutate=mutate, with_stackup=False, tag="supp")
        stack = fixture.stackup().stackup
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
                json.dump({"layers": layers,
                           "provenance": "synthetic fixture values"}, handle)
            document["timing"]["physical_stackup"]["supplement"] = \
                "models/supplement.json"
        fixture = make(mutate=mutate, with_stackup=True, tag="conflict")
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.ERROR)
        self.assertIn("contradicts", result.reason)

    def test_reference_planes_come_from_poured_copper(self):
        fixture = make(tag="planes")
        self.assertEqual(sorted(fixture.stackup().reference_layers),
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
        results = fixture.gates(only={"TIMING.PATH_INTEGRITY",
                                      "TIMING.INTERCONNECT_DELAY",
                                      "TIMING.INTERCONNECT_SKEW"})
        for gate_id in ("TIMING.INTERCONNECT_DELAY",
                        "TIMING.INTERCONNECT_SKEW"):
            self.assertEqual(results[gate_id].status, Status.ERROR, gate_id)
            self.assertIn("openems", results[gate_id].reason.lower())
        # The geometry question never needed the solver.
        self.assertEqual(results["TIMING.PATH_INTEGRITY"].status, Status.PASS,
                         results["TIMING.PATH_INTEGRITY"].reason)

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
                         "{}\
{}".format(result.reason, result.findings[:4]))

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
        self.assertTrue(measurements["backend_requested"])
        self.assertTrue(measurements["backend_used"])
        self.assertIn("backend_fell_back", measurements)

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


# ---------------------------------------------------------------------------
# provenance, checked against something other than the declaration
# ---------------------------------------------------------------------------

def _pinning(*extra):
    """A mutation that pins the modules the applicable gates say they need."""
    def mutate(document, _project):
        needed = sorted(core.derivation_modules())
        document["reports"]["implementation_closure"] = [
            name for name in needed if name not in extra]
    return mutate


class ProvenanceIsCheckedIndependently(unittest.TestCase):
    """Removing a dependency must fail even though nothing contradicts itself.

    The trap this replaces compared the closure's executed modules against the
    manifest key the closure was built from. That comparison holds however
    wrong the manifest is: delete an entry and both sides shrink together. The
    requirement has to come from somewhere the board cannot edit, which is the
    gate registry - each gate states which modules compute what it reports.
    """

    def test_a_complete_declaration_passes(self):
        fixture = make(mutate=_pinning(), tag="prov_ok")
        result = fixture.gates(only={"PROV.DERIVATION_CLOSURE"})[
            "PROV.DERIVATION_CLOSURE"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertTrue(result.measurements["derivation_modules_required"])

    def test_dropping_a_timing_dependency_is_caught(self):
        """The whole point. Manifest and closure agree; the toolkit does not."""
        fixture = make(mutate=_pinning("pcbqa.propagation"), tag="prov_gap")
        result = fixture.gates(only={"PROV.DERIVATION_CLOSURE"})[
            "PROV.DERIVATION_CLOSURE"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertEqual([f["module"] for f in result.findings],
                         ["pcbqa.propagation"])
        self.assertIn("TIMING.INTERCONNECT_DELAY",
                      result.findings[0]["required_by"])

    def test_the_declaration_and_the_closure_still_agree_with_each_other(self):
        """So a test comparing those two would have passed on the broken board."""
        fixture = make(mutate=_pinning("pcbqa.propagation"), tag="prov_agree")
        policy = canonical.AttributePolicy.load(paths.ATTRIBUTES)
        closure = cleanroom.source_closure(fixture.manifest, policy)
        executed = sorted(k[len("<executed>"):] for k in closure
                          if k.startswith("<executed>"))
        declared = sorted(fixture.manifest.get(
            "reports.implementation_closure"))
        self.assertEqual(executed, declared,
                         "the two sides of the old check must still match, or "
                         "this test is not demonstrating anything")

    def test_a_gate_that_is_not_applicable_demands_nothing(self):
        """A board with no timing policy keeps its previous obligations."""
        def mutate(document, project):
            _pinning()(document, project)
            document.pop("timing")
            document["reports"]["implementation_closure"] = []
        fixture = make(mutate=mutate, tag="prov_none")
        result = fixture.gates(only={"PROV.DERIVATION_CLOSURE"})[
            "PROV.DERIVATION_CLOSURE"]
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_every_declared_derivation_module_is_importable(self):
        """A registry naming a module that does not exist would fail closed
        for the wrong reason, and only on a board unlucky enough to run it."""
        import importlib
        for module in sorted(core.derivation_modules()):
            self.assertTrue(importlib.import_module(module))


# ---------------------------------------------------------------------------
# component models
# ---------------------------------------------------------------------------

def _series_model(model):
    def mutate(document, _project):
        steps = document["timing"]["interfaces"]["series"]["routes"][
            "template"]["steps"]
        steps[1]["delay_model"] = model
    return mutate


class ComponentModelsFailSafely(unittest.TestCase):

    def test_no_model_is_a_lower_bound_not_a_zero(self):
        fixture = make(tag="cm_none")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        traversal = record["component_traversals"][0]
        self.assertEqual(traversal["model_status"], component_models.UNMODELLED)
        self.assertEqual(traversal["delay_ps"], 0.0)
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertEqual(record["fidelity"], propagation.UNKNOWN_CONTRIBUTION)

    def test_a_declared_but_unimplemented_model_is_never_a_silent_zero(self):
        """The defect this contract exists to prevent."""
        fixture = make(mutate=_series_model({"model": "ibis"}), tag="cm_ibis")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        traversal = record["component_traversals"][0]
        self.assertEqual(traversal["model_status"],
                         component_models.UNSUPPORTED)
        self.assertIsNone(traversal["delay_ps"])
        self.assertIsNone(record["delay_ps"])
        self.assertTrue(any(i["portion"] == "component"
                            for i in record["insufficient"]), record)

    def test_an_unimplemented_model_blocks_a_declared_limit(self):
        def mutate(document, project):
            _series_model({"model": "touchstone"})(document, project)
            document["timing"]["interfaces"]["series"]["limits"] = {
                "max_delay_ps": 1e6}
        fixture = make(mutate=mutate, tag="cm_block")
        result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)

    def test_an_explicit_zero_needs_a_justification(self):
        with self.assertRaises(component_models.ComponentModelError):
            component_models.evaluate({"model": "none"}, "R1.1")

    def test_a_justified_zero_records_its_reason_and_stays_a_bound(self):
        fixture = make(mutate=_series_model(
            {"model": "none",
             "justification": "series termination; transit below resolution"}),
            tag="cm_zero")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        traversal = record["component_traversals"][0]
        self.assertEqual(traversal["model_status"],
                         component_models.UNMODELLED)
        self.assertEqual(traversal["delay_ps"], 0.0)
        # Still an omission: the reason is recorded, the part is not measured.
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertIsNone(record["delay_upper_ps"])
        self.assertIn("records a decision", traversal["reason"])
        self.assertAlmostEqual(record["delay_ps"],
                               PATH_TO_L1_MM * expected_microstrip_ps_per_mm(),
                               places=4)

    def test_bounding_the_component_omission_makes_a_maximum_decidable(self):
        fixture = make(mutate=_series_model(
            {"model": "none", "max_delay_ps": 3.0,
             "provenance": "fixture arithmetic",
             "justification": "fixture bound"}), tag="cm_bounded")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertAlmostEqual(record["omitted_bound_ps"], 3.0, places=6)
        self.assertAlmostEqual(record["delay_upper_ps"],
                               record["delay_ps"] + 3.0, places=6)

    def test_an_unbounded_omission_cannot_pass_a_maximum(self):
        def mutate(document, project):
            _series_model({"model": "none",
                           "justification": "no bound"})(document, project)
            document["timing"]["interfaces"]["series"]["limits"] = {
                "max_delay_ps": 1e9}
        fixture = make(mutate=mutate, tag="cm_unbounded_limit")
        result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any("has no upper bound" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_bounded_omission_can(self):
        def mutate(document, project):
            _series_model({"model": "none", "max_delay_ps": 3.0,
                           "provenance": "fixture arithmetic",
                           "justification": "fixture bound"})(document, project)
            document["timing"]["interfaces"]["series"]["limits"] = {
                "max_delay_ps": 1e9}
        fixture = make(mutate=mutate, tag="cm_bounded_limit")
        result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_a_limit_between_the_two_bounds_is_not_met(self):
        """Straddling what is known is not the same as meeting it."""
        def mutate(document, project):
            _series_model({"model": "none", "max_delay_ps": 50.0,
                           "provenance": "fixture arithmetic",
                           "justification": "fixture bound"})(document, project)
            copper = PATH_TO_L1_MM * expected_microstrip_ps_per_mm()
            document["timing"]["interfaces"]["series"]["limits"] = {
                "max_delay_ps": copper + 10.0}
        fixture = make(mutate=mutate, tag="cm_straddle")
        result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any("falls inside the uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_fixed_delay_is_added_and_needs_provenance(self):
        with self.assertRaises(component_models.ComponentModelError):
            component_models.evaluate({"model": "fixed_delay",
                                       "delay_ps": 12.0}, "R1.1")
        fixture = make(mutate=_series_model(
            {"model": "fixed_delay", "delay_ps": 12.0,
             "provenance": "fixture value, not a real part"}), tag="cm_fixed")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        self.assertFalse(record["delay_is_lower_bound"])
        self.assertAlmostEqual(
            record["delay_ps"],
            PATH_TO_L1_MM * expected_microstrip_ps_per_mm() + 12.0, places=4)

    def test_a_negative_fixed_delay_refuses(self):
        with self.assertRaises(component_models.ComponentModelError):
            component_models.evaluate({"model": "fixed_delay",
                                       "delay_ps": -1.0,
                                       "provenance": "x"}, "R1.1")

    def test_an_unknown_model_name_refuses_at_declaration_time(self):
        with self.assertRaises(PathError):
            electrical_path.step_from_spec(
                {"kind": "component", "reference": "R1", "from_pad": "1",
                 "to_pad": "2", "delay_model": {"model": "vibes"}}, 1)

    def test_a_bare_string_model_refuses(self):
        with self.assertRaises(component_models.ComponentModelError):
            component_models.evaluate("none", "R1.1")

    def test_every_reserved_name_is_refused_rather_than_unknown(self):
        for name in component_models.RESERVED_MODELS:
            contribution = component_models.evaluate({"model": name}, "R1.1")
            self.assertEqual(contribution.kind, component_models.UNSUPPORTED,
                             name)
            self.assertFalse(contribution.evaluable, name)


# ---------------------------------------------------------------------------
# population state
# ---------------------------------------------------------------------------

DNP_ROUTE = {
    "paths": [{
        "id": "through_unfitted",
        "steps": [
            {"kind": "copper", "net": "SIG_D", "from": "D4.1", "to": "RD1.1"},
            {"kind": "component", "reference": "RD1", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "copper", "net": "SIG_E", "from": "RD1.2", "to": "L6.1"},
        ],
    }],
}


def _dnp_interface(extra_step=None):
    def mutate(document, _project):
        route = copy.deepcopy(DNP_ROUTE)
        if extra_step:
            route["paths"][0]["steps"][1].update(extra_step)
        document["timing"]["interfaces"] = {
            "unfitted": {"description": "crosses a part marked DNP",
                         "routes": route}}
    return mutate


class UnpopulatedPartsDoNotConduct(unittest.TestCase):
    """Two nets on two pads is a footprint, not a connection."""

    def test_a_path_through_a_dnp_part_refuses(self):
        fixture = make(mutate=_dnp_interface(), tag="dnp")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any("do-not-populate" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_build_variant_may_say_it_is_fitted(self):
        fixture = make(mutate=_dnp_interface(
            {"assume_populated": {"justification": "fitted in the RF build"}}),
            tag="dnp_ok")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        row = result.measurements["interfaces"]["unfitted"]["paths"][0]
        self.assertAlmostEqual(row["copper_length_mm"],
                               LENGTH_DNP_IN_MM + LENGTH_DNP_OUT_MM, places=4)

    def test_that_override_has_to_be_justified(self):
        with self.assertRaises(PathError):
            electrical_path.step_from_spec(
                {"kind": "component", "reference": "R1", "from_pad": "1",
                 "to_pad": "2", "assume_populated": True}, 1)

    def test_the_population_state_is_recorded_either_way(self):
        fixture = make(tag="dnp_record")
        traversal = _find(fixture, "series_branch_to_L1")[
            "resolved"].component_traversals()[0]
        self.assertIn("dnp", traversal)
        self.assertFalse(traversal["dnp"])


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class BackendFallbackSemantics(unittest.TestCase):

    def setUp(self):
        from pcbqa.backends import openems
        if openems.executable():                        # pragma: no cover
            self.skipTest("openEMS is installed here")

    def test_a_required_backend_that_is_missing_blocks(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendUnavailable):
            backends.select("openems", {"required": True})

    def test_silence_means_required(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendUnavailable):
            backends.select("openems", {})

    def test_an_optional_backend_falls_back_and_says_so(self):
        from pcbqa import backends
        selection = backends.select("openems", {"required": False})
        self.assertEqual(selection.used, backends.ANALYTIC)
        self.assertEqual(selection.requested, "openems")
        self.assertTrue(selection.fell_back)
        self.assertIn("fallback", selection.detail.lower() + " fallback")

    def test_a_fallback_to_something_unimplemented_refuses(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendError):
            backends.select("openems", {"required": False,
                                        "fallback": "handwaving"})

    def test_a_backend_cannot_fall_back_to_itself(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendError):
            backends.select("openems", {"required": False,
                                        "fallback": "openems"})

    def test_the_gates_report_the_backend_that_actually_ran(self):
        def mutate(document, _project):
            document["timing"]["propagation"].update(
                {"backend": "openems", "required": False})
        fixture = make(mutate=mutate, tag="backend_fb")
        result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertNotEqual(result.status, Status.ERROR, result.reason)
        self.assertEqual(result.measurements["backend_requested"], "openems")
        self.assertEqual(result.measurements["backend_used"], "analytic")
        self.assertTrue(result.measurements["backend_fell_back"])

    def test_a_required_backend_blocks_delay_but_not_geometry(self):
        def mutate(document, _project):
            document["timing"]["propagation"]["backend"] = "openems"
        fixture = make(mutate=mutate, tag="backend_req")
        results = fixture.gates(only={"TIMING.PATH_INTEGRITY",
                                      "TIMING.INTERCONNECT_DELAY"})
        self.assertEqual(results["TIMING.INTERCONNECT_DELAY"].status,
                         Status.ERROR)
        self.assertEqual(results["TIMING.PATH_INTEGRITY"].status, Status.PASS)


# ---------------------------------------------------------------------------
# stackup applicability and contradictions
# ---------------------------------------------------------------------------

def _supplement(layers, **top):
    def mutate(document, project):
        directory = os.path.join(project, "models")
        os.makedirs(directory, exist_ok=True)
        top.setdefault("provenance", "synthetic fixture values")
        with open(os.path.join(directory, "supplement.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"layers": layers, **top}, handle)
        document["timing"]["physical_stackup"]["supplement"] = \
            "models/supplement.json"
    return mutate


def _fixture_layers(**overrides):
    out = []
    for entry in FIXTURE_STACKUP:
        entry = dict(entry, kind=_kind(entry))
        for field, value in overrides.items():
            if field in entry:
                entry[field] = value
        out.append(entry)
    return out


class StackupApplicabilityFollowsTheAnalysis(unittest.TestCase):
    """Complete for what, exactly."""

    def test_a_stackup_missing_only_unread_fields_is_sufficient(self):
        """Loss tangent and copper weight are not inputs to this model."""
        layers = _fixture_layers(loss_tangent=None)
        for entry in layers:
            if entry["kind"] == "copper":
                entry["thickness_mm"] = None

        def mutate(document, project):
            _supplement(layers)(document, project)
            document["timing"]["physical_stackup"]["require_complete"] = True
        fixture = make(mutate=mutate, with_stackup=False, tag="stack_enough")
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertEqual(result.measurements["insufficient_fields"], [])
        # And the full inventory still records what is absent.
        self.assertTrue(result.measurements["full_inventory_gaps"])

    def test_the_same_stackup_is_insufficient_for_a_model_that_reads_more(self):
        layers = _fixture_layers()
        for entry in layers:
            if entry["kind"] == "copper":
                entry["thickness_mm"] = None

        def mutate(document, project):
            _supplement(layers)(document, project)
            document["timing"]["physical_stackup"]["require_complete"] = True
            document["timing"]["propagation"]["model"] = propagation.HAMMERSTAD_T
        fixture = make(mutate=mutate, with_stackup=False, tag="stack_tcorr")
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any(f.get("field") == stackup_physical.
                            NEEDS_COPPER_THICKNESS
                            for f in result.findings), result.findings)

    def test_a_layer_nothing_routes_on_cannot_block_an_analysis(self):
        """Only the layers the declared paths use are consulted."""
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        stack.layers[3].epsilon_r = None            # the inner core
        missing = stack.completeness(
            required={stackup_physical.NEEDS_EPSILON_R}, layers=["F.Cu"])
        self.assertEqual(missing, [])
        self.assertTrue(stack.completeness(
            required={stackup_physical.NEEDS_EPSILON_R}))

    def test_a_declared_constant_removes_the_geometry_requirement(self):
        required = propagation.required_stackup_fields(
            propagation.HAMMERSTAD, propagation.VIA_NONE,
            {"F.Cu": True}, ["F.Cu"])
        self.assertEqual(required, set())


class StackupContradictionsAlwaysBlock(unittest.TestCase):

    def _contradiction(self, layers, tag, **top):
        fixture = make(mutate=_supplement(layers, **top), with_stackup=False,
                       tag=tag)
        return fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]

    def test_layers_thicker_than_the_board_are_impossible(self):
        layers = _fixture_layers()
        layers[3]["thickness_mm"] = 40.0
        result = self._contradiction(layers, "sx_thick",
                                     total_thickness_mm=1.6)
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertIn("overall thickness", result.reason + str(result.findings))

    def test_a_permittivity_below_one_is_impossible(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers(epsilon_r=0.5)})
        self.assertTrue(any("below 1" in p["issue"]
                            for p in stack.contradictions()))

    def test_a_negative_thickness_is_impossible(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers(thickness_mm=-0.1)})
        self.assertTrue(any("positive length" in p["issue"]
                            for p in stack.contradictions()))

    def test_two_adjacent_copper_layers_are_impossible(self):
        layers = [entry for entry in _fixture_layers()
                  if entry["name"] != "dielectric 1"]
        stack = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers})
        self.assertTrue(any("adjacent" in p["issue"]
                            for p in stack.contradictions()))

    def test_a_stackup_describing_other_copper_layers_is_caught(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        problems = stack.contradictions(["F.Cu", "In1.Cu", "B.Cu"])
        self.assertTrue(any("copper layers" in p["issue"] for p in problems),
                        problems)

    def test_a_contradiction_blocks_whatever_the_completeness_policy_says(self):
        layers = _fixture_layers()
        layers[3]["thickness_mm"] = 40.0

        def mutate(document, project):
            _supplement(layers, total_thickness_mm=1.6)(document, project)
            document["timing"]["physical_stackup"]["require_complete"] = False
        fixture = make(mutate=mutate, with_stackup=False, tag="sx_policy")
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.FAIL, result.reason)

    def test_a_supplement_may_not_add_a_copper_layer(self):
        native = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()}, source=stackup_physical.NATIVE)
        extra = _fixture_layers() + [
            {"name": "In9.Cu", "kind": "copper", "type": "copper",
             "thickness_mm": 0.0175}]
        with self.assertRaises(stackup_physical.StackupError):
            stackup_physical.merge(
                native, stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": extra}))


# ---------------------------------------------------------------------------
# transmission-line classification
# ---------------------------------------------------------------------------

class ClassificationIsConservative(unittest.TestCase):

    def _stack(self, **kwargs):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        for key, value in kwargs.items():
            setattr(stack, key, value)
        return stack

    def test_outer_is_decided_by_the_board_not_by_the_declaration(self):
        """A supplement missing the bottom layer must not promote an inner one."""
        partial = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()[:-1]})
        partial.board_copper_layers = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        self.assertEqual(
            partial.reference_geometry("In2.Cu", {"In1.Cu"}).mode,
            propagation.EMBEDDED_MICROSTRIP)
        # Without the board's own list it would have looked like the last
        # copper layer there is, and therefore outer.
        partial.board_copper_layers = None
        self.assertEqual(
            partial.reference_geometry("In2.Cu", {"In1.Cu"}).mode,
            propagation.MICROSTRIP)

    def test_a_signal_layer_between_a_trace_and_a_plane_hides_the_plane(self):
        stack = self._stack()
        geometry = stack.reference_geometry("F.Cu", {"In2.Cu"})
        self.assertIsNone(geometry.mode)

    def test_no_model_is_chosen_for_an_unclassifiable_layer(self):
        stack = self._stack()
        model = propagation.PropagationModel(stack, {"In2.Cu"})
        with self.assertRaises(propagation.Unsupported):
            model.conductor("F.Cu", TRACK_WIDTH_MM)

    def test_the_implemented_geometries_are_exactly_these(self):
        """A guard against quietly widening what the analytic model claims."""
        stack = self._stack()
        model = propagation.PropagationModel(stack, {"F.Cu", "In2.Cu"})
        supported = set()
        for layer in stack.copper_layer_names:
            try:
                supported.add(model.conductor(layer, TRACK_WIDTH_MM)["mode"])
            except propagation.PropagationError:
                continue
        self.assertTrue(supported <= {propagation.MICROSTRIP,
                                      propagation.STRIPLINE,
                                      propagation.ASYMMETRIC_STRIPLINE},
                        supported)


# ---------------------------------------------------------------------------
# path declarations, as an external interface
# ---------------------------------------------------------------------------

def _route(steps, path_id="p"):
    return {"paths": [{"id": path_id, "steps": steps}]}


class PathDeclarationRobustness(unittest.TestCase):

    def _refuses(self, steps):
        with self.assertRaises(PathError):
            electrical_path.paths_from_spec(_route(steps))

    def test_two_copper_steps_in_a_row_refuse(self):
        self._refuses([
            {"kind": "copper", "net": "A", "from": "X.1", "to": "Y.1"},
            {"kind": "copper", "net": "B", "from": "Y.1", "to": "Z.1"}])

    def test_two_component_steps_in_a_row_refuse(self):
        self._refuses([
            {"kind": "copper", "net": "A", "from": "X.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "component", "reference": "R2", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "copper", "net": "B", "from": "R2.2", "to": "Z.1"}])

    def test_a_repeated_net_is_a_loop(self):
        self._refuses([
            {"kind": "copper", "net": "A", "from": "X.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "copper", "net": "A", "from": "R1.2", "to": "Z.1"}])

    def test_crossing_one_part_twice_refuses(self):
        self._refuses([
            {"kind": "copper", "net": "A", "from": "X.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "2"},
            {"kind": "copper", "net": "B", "from": "R1.2", "to": "R1.3"},
            {"kind": "component", "reference": "R1", "from_pad": "3",
             "to_pad": "4"},
            {"kind": "copper", "net": "C", "from": "R1.4", "to": "Z.1"}])

    def test_a_component_crossed_pad_to_the_same_pad_refuses(self):
        self._refuses([
            {"kind": "copper", "net": "A", "from": "X.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "1"},
            {"kind": "copper", "net": "B", "from": "R1.1", "to": "Z.1"}])

    def test_a_copper_step_from_a_pad_to_itself_refuses(self):
        self._refuses([{"kind": "copper", "net": "A", "from": "X.1",
                        "to": "X.1"}])

    def test_an_ambiguous_source_refuses_by_default(self):
        def mutate(document, _project):
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            steps[2]["from"] = "^(R1\\.2|L1\\.1)$"
        fixture = make(mutate=mutate, tag="ambiguous")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any("stable declaration" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_an_ambiguous_source_is_allowed_when_declared(self):
        def mutate(document, _project):
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            steps[2]["from"] = "^(R1\\.2|L1\\.1)$"
            steps[2]["source_selection"] = "shortest"
        fixture = make(mutate=mutate, tag="ambiguous_ok")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_one_malformed_route_does_not_blind_the_others(self):
        def mutate(document, _project):
            interface = document["timing"]["interfaces"]["series"]
            interface["routes"] = {
                "paths": [
                    {"id": "good", "steps": [
                        {"kind": "copper", "net": "SIG_B", "from": "R1.2",
                         "to": "L1.1"}]},
                    {"id": "broken", "steps": [
                        {"kind": "copper", "net": "SIG_B", "from": "R1.2",
                         "to": "R1.2"}]},
                ]}
            interface.pop("expected_path_count")
            interface.pop("required_component_crossings")
        fixture = make(mutate=mutate, tag="partial")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)
        rows = result.measurements["interfaces"]["series"]["paths"]
        self.assertEqual([r["path"] for r in rows], ["good"])
        self.assertEqual([f["path"] for f in result.findings], ["broken"])

    def test_duplicate_path_ids_are_reported_not_silently_merged(self):
        paths_built, problems = electrical_path.build_paths({"paths": [
            {"id": "same", "steps": [{"kind": "copper", "net": "A",
                                      "from": "X.1", "to": "Y.1"}]},
            {"id": "same", "steps": [{"kind": "copper", "net": "A",
                                      "from": "X.1", "to": "Z.1"}]}]})
        self.assertEqual(len(paths_built), 1)
        self.assertEqual(len(problems), 1)


# ---------------------------------------------------------------------------
# connectivity accuracy
# ---------------------------------------------------------------------------

class ConnectivityAccuracy(unittest.TestCase):
    """A stub landing mid-track must not charge the whole track."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = make(tag="accuracy")
        geom.configure(0.001)
        cls.board = cls.fixture.ctx.board()

    def _graph(self, split):
        from pcbqa.connectivity import NetGraph
        return NetGraph(self.board, "SIG_T", geom.pad_copper_polygon,
                        split_at_junctions=split)

    def test_the_unsplit_graph_charges_the_whole_run(self):
        """The pre-existing behaviour, unchanged and still the default."""
        self.assertAlmostEqual(
            self._graph(False).path_length(["D3.1"], "L4.1"),
            TEE_UNSPLIT_MM, places=4)

    def test_the_split_graph_charges_only_what_is_travelled(self):
        self.assertAlmostEqual(
            self._graph(True).path_length(["D3.1"], "L4.1"),
            TEE_SPLIT_MM, places=4)

    def test_a_walk_that_uses_the_whole_run_is_unchanged_by_splitting(self):
        for split in (False, True):
            self.assertAlmostEqual(
                self._graph(split).path_length(["D3.1"], "L5.1"),
                LENGTH_TEE_RUN_MM, places=4)

    def test_splitting_does_not_change_the_copper_total(self):
        self.assertAlmostEqual(self._graph(False).total_track_mm(),
                               self._graph(True).total_track_mm(), places=4)

    def test_the_default_is_unsplit_so_existing_measurements_are_untouched(self):
        from pcbqa.connectivity import NetGraph
        graph = NetGraph(self.board, "SIG_T", geom.pad_copper_polygon)
        self.assertFalse(graph.split_at_junctions)

    def test_electrical_paths_use_the_accurate_graph(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"tee": {
                "description": "a stub landing mid-run",
                "routes": _route([{"kind": "copper", "net": "SIG_T",
                                   "from": "D3.1", "to": "L4.1"}], "tee")}}
        fixture = make(mutate=mutate, tag="accuracy_path")
        row = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"].measurements[
                "interfaces"]["tee"]["paths"][0]
        self.assertAlmostEqual(row["copper_length_mm"], TEE_SPLIT_MM, places=3)

    def test_a_pad_at_a_track_end_does_not_shorten_the_measurement(self):
        """The convention that was there before, deliberately preserved."""
        from pcbqa.connectivity import NetGraph
        graph = NetGraph(self.board, "SIG_A", geom.pad_copper_polygon,
                         split_at_junctions=True)
        self.assertAlmostEqual(graph.path_length(["D1.1"], "R1.1"),
                               LENGTH_PRE_SERIES_MM, places=6)


class MultipleViasAndMixedWidths(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"multi": {
                "description": "two layer changes and a width change",
                "expected_path_count": 1,
                "routes": _route([{"kind": "copper", "net": "SIG_M",
                                   "from": "D5.1", "to": "L7.1"}], "multi")}}
        cls.fixture = make(mutate=mutate, tag="multivia")
        cls.record = _find(cls.fixture, "multi")

    def test_both_via_transitions_are_recorded(self):
        transitions = self.record["resolved"].via_transitions()
        self.assertEqual(len(transitions), 2)
        self.assertEqual([t["from_layer"] for t in transitions],
                         ["F.Cu", "B.Cu"])
        self.assertEqual([t["to_layer"] for t in transitions],
                         ["B.Cu", "F.Cu"])

    def test_length_is_split_across_layers(self):
        by_layer = self.record["resolved"].length_by_layer_mm()
        self.assertAlmostEqual(by_layer["F.Cu"],
                               LENGTH_M_TOP_A_MM + LENGTH_M_TOP_B_MM, places=4)
        self.assertAlmostEqual(by_layer["B.Cu"], LENGTH_M_BOTTOM_MM, places=4)

    def test_each_width_is_modelled_separately(self):
        conductors = {(c["layer"], c["width_mm"]): c["length_mm"]
                      for c in self.record["resolved"].conductors()}
        self.assertAlmostEqual(conductors[("F.Cu", TRACK_WIDTH_MM)],
                               LENGTH_M_TOP_A_MM + LENGTH_M_TOP_B_MM, places=4)
        self.assertAlmostEqual(conductors[("B.Cu", WIDE_TRACK_MM)],
                               LENGTH_M_BOTTOM_MM, places=4)

    def test_the_delay_sums_the_two_widths_at_their_own_velocities(self):
        narrow = expected_microstrip_ps_per_mm(TRACK_WIDTH_MM)
        wide = expected_microstrip_ps_per_mm(WIDE_TRACK_MM)
        self.assertNotAlmostEqual(narrow, wide, places=3)
        self.assertAlmostEqual(
            self.record["delay"]["delay_ps"],
            (LENGTH_M_TOP_A_MM + LENGTH_M_TOP_B_MM) * narrow
            + LENGTH_M_BOTTOM_MM * wide, places=4)


# ---------------------------------------------------------------------------
# fidelity
# ---------------------------------------------------------------------------

class FidelityCannotOverstate(unittest.TestCase):

    def test_the_weakest_portion_decides(self):
        self.assertEqual(
            propagation.weakest({propagation.ANALYTIC_TRANSMISSION_LINE,
                                 propagation.UNKNOWN_CONTRIBUTION}),
            propagation.UNKNOWN_CONTRIBUTION)
        self.assertEqual(
            propagation.weakest({propagation.DECLARED_PROPAGATION,
                                 propagation.ANALYTIC_TRANSMISSION_LINE}),
            propagation.ANALYTIC_TRANSMISSION_LINE)

    def test_an_unrecognised_fidelity_never_ranks_high(self):
        self.assertEqual(
            propagation.weakest({propagation.DEVICE_AWARE, "from-the-future"}),
            "from-the-future")
        self.assertEqual(propagation.fidelity_rank("from-the-future"), -1)

    def test_nothing_measured_is_geometry_only(self):
        self.assertEqual(propagation.weakest(set()),
                         propagation.GEOMETRY_ONLY)

    def test_the_ladder_keeps_its_distinctions(self):
        rank = propagation.fidelity_rank
        self.assertLess(rank(propagation.GEOMETRY_ONLY),
                        rank(propagation.UNKNOWN_CONTRIBUTION))
        self.assertLess(rank(propagation.UNKNOWN_CONTRIBUTION),
                        rank(propagation.ANALYTIC_TRANSMISSION_LINE))
        self.assertLess(rank(propagation.ANALYTIC_TRANSMISSION_LINE),
                        rank(propagation.DECLARED_PROPAGATION))
        self.assertLess(rank(propagation.DECLARED_PROPAGATION),
                        rank(propagation.QUASI_STATIC_EXTRACTED))
        self.assertLess(rank(propagation.QUASI_STATIC_EXTRACTED),
                        rank(propagation.FULL_WAVE_EXTRACTED))
        self.assertLess(rank(propagation.FULL_WAVE_EXTRACTED),
                        rank(propagation.DEVICE_AWARE))

    def test_a_path_with_no_modelled_copper_reports_nothing(self):
        """A total of zero would be a number where nothing was measured."""
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        model = propagation.PropagationModel(stack, {"In1.Cu"})

        class _Empty:
            id = "empty"
            source = electrical_path.Endpoint("A.1", "N", "source")
            destination = electrical_path.Endpoint("B.1", "N", "destination")
            copper_length_mm = 0.0

            @staticmethod
            def length_by_layer_mm():
                return {}

            @staticmethod
            def conductors():
                return []

            @staticmethod
            def via_transitions():
                return []

            @staticmethod
            def component_traversals():
                return []

        record = model.evaluate(_Empty())
        self.assertIsNone(record["delay_ps"])
        self.assertEqual(record["fidelity"], propagation.GEOMETRY_ONLY)


class TheBackendResultContract(unittest.TestCase):
    """What any backend has to return, so the gates need not know which ran."""

    @classmethod
    def setUpClass(cls):
        cls.fixture = make(tag="contract")
        cls.record = _find(cls.fixture, "layer_change")["delay"]

    def test_a_path_result_carries_every_contract_field(self):
        for field in propagation.PATH_RESULT_FIELDS:
            self.assertIn(field, self.record, field)

    def test_a_conductor_result_carries_every_contract_field(self):
        for conductor in self.record["conductors"]:
            for field in propagation.CONDUCTOR_RESULT_FIELDS:
                self.assertIn(field, conductor, field)

    def test_a_via_result_carries_every_contract_field(self):
        for via in self.record["vias"]:
            for field in propagation.VIA_RESULT_FIELDS:
                self.assertIn(field, via, field)

    def test_the_backend_that_ran_is_named_on_the_result(self):
        self.assertEqual(self.record["backend"], "analytic")

    def test_the_gates_read_no_backend_specific_field(self):
        """A gate reaching into a backend's internals could not survive a
        second backend, so it must not."""
        source = open(os.path.join(paths.PACKAGE, "gates", "g_timing.py"),
                      encoding="utf-8").read()
        for token in ("openems", "openEMS", "s2p", "touchstone"):
            self.assertNotIn(token, source, token)


class AnUndeclaredViaTreatmentIsAnOmission(unittest.TestCase):
    """`none` chosen is a decision; `none` inherited is nobody having asked."""

    def _record(self, tag, propagation_spec):
        def mutate(document, _project):
            document["timing"]["propagation"] = propagation_spec
        fixture = make(mutate=mutate, tag=tag)
        return _find(fixture, "layer_change")["delay"]

    def test_naming_none_without_justifying_it_is_still_a_bound(self):
        """Naming a treatment is not the same as establishing an amount.

        `"via_delay_model": "none"` says the board chose to omit the transit.
        It does not say the transit is negligible, and a barrel through a
        1.6 mm board at six picoseconds per millimetre is not. Treating the
        naming as an assertion turned "not modelled yet" into an exact zero,
        which is the whole defect.
        """
        record = self._record("via_named", {
            "backend": "analytic", "model": propagation.HAMMERSTAD,
            "via_delay_model": propagation.VIA_NONE})
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertEqual(record["vias"][0]["fidelity"],
                         propagation.UNKNOWN_CONTRIBUTION)
        self.assertTrue(record["vias"][0]["policy"]["declared"])
        self.assertFalse(record["vias"][0]["policy"]["justified"])

    def test_justifying_none_records_the_reason_and_stays_a_bound(self):
        """Prose explains a decision. It does not measure a barrel.

        A justified omission is better than an unexplained one - the reason is
        on the record - and it is still an omission, so the total is still a
        lower bound. Letting a sentence promote it to an exact zero would
        create more physical certainty than the sentence contains.
        """
        record = self._record("via_justified", {
            "backend": "analytic", "model": propagation.HAMMERSTAD,
            "via_delay_model": {"model": propagation.VIA_NONE,
                                "justification": "fixture value"}})
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertEqual(record["vias"][0]["fidelity"],
                         propagation.UNKNOWN_CONTRIBUTION)
        self.assertTrue(record["vias"][0]["policy"]["justified"])
        self.assertFalse(record["vias"][0]["policy"]["bounded"])
        self.assertIsNone(record["delay_upper_ps"])

    def test_bounding_the_omission_is_what_makes_it_decidable(self):
        """A number can be reasoned about; a sentence cannot."""
        record = self._record("via_bounded", {
            "backend": "analytic", "model": propagation.HAMMERSTAD,
            "via_delay_model": {"model": propagation.VIA_NONE,
                                "max_delay_ps": 12.0,
                                "provenance": "fixture arithmetic",
                                "justification": "fixture bound"}})
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertTrue(record["vias"][0]["policy"]["bounded"])
        self.assertAlmostEqual(record["omitted_bound_ps"], 12.0, places=6)
        self.assertAlmostEqual(record["delay_upper_ps"],
                               record["delay_ps"] + 12.0, places=6)

    def test_a_bound_without_a_reason_is_refused(self):
        with self.assertRaises(propagation.PropagationError):
            propagation.via_policy({"model": propagation.VIA_NONE,
                                    "max_delay_ps": 5.0})

    def test_a_bound_without_provenance_is_refused(self):
        """A number that arithmetic will lean on has to say where it is from."""
        with self.assertRaises(propagation.PropagationError) as caught:
            propagation.via_policy({"model": propagation.VIA_NONE,
                                    "max_delay_ps": 5.0,
                                    "justification": "reason but no source"})
        self.assertIn("provenance", str(caught.exception))

    def test_bounding_a_model_that_computes_the_transit_is_refused(self):
        with self.assertRaises(propagation.PropagationError):
            propagation.via_policy({"model": propagation.VIA_GEOMETRIC,
                                    "max_delay_ps": 5.0,
                                    "justification": "x",
                                    "provenance": "fixture"})

    def test_only_the_geometric_model_is_exact(self):
        for declaration in (None, propagation.VIA_NONE,
                            {"model": propagation.VIA_NONE,
                             "justification": "x"},
                            {"model": propagation.VIA_NONE,
                             "max_delay_ps": 1.0, "justification": "x",
                             "provenance": "fixture"}):
            self.assertFalse(propagation.via_policy(declaration).exact,
                             declaration)
        self.assertTrue(
            propagation.via_policy(propagation.VIA_GEOMETRIC).exact)

    def test_the_declared_and_absent_states_are_told_apart(self):
        named = self._record("via_named2", {
            "backend": "analytic", "model": propagation.HAMMERSTAD,
            "via_delay_model": propagation.VIA_NONE})
        absent = self._record("via_absent2", {
            "backend": "analytic", "model": propagation.HAMMERSTAD})
        # Same arithmetic, different record of who decided what.
        self.assertEqual(named["delay_is_lower_bound"],
                         absent["delay_is_lower_bound"])
        self.assertTrue(named["vias"][0]["policy"]["declared"])
        self.assertFalse(absent["vias"][0]["policy"]["declared"])

    def test_a_reserved_via_model_is_refused_by_name(self):
        for name in propagation.VIA_RESERVED:
            with self.assertRaises(propagation.PropagationError):
                propagation.via_policy({"model": name})

    def test_an_unknown_via_model_refuses(self):
        with self.assertRaises(propagation.PropagationError):
            propagation.via_policy({"model": "guesswork"})

    def test_declaring_nothing_makes_the_total_a_lower_bound(self):
        record = self._record("via_silent", {
            "backend": "analytic", "model": propagation.HAMMERSTAD})
        self.assertTrue(record["delay_is_lower_bound"])
        self.assertEqual(record["vias"][0]["fidelity"],
                         propagation.UNKNOWN_CONTRIBUTION)
        self.assertEqual(record["fidelity"], propagation.UNKNOWN_CONTRIBUTION)

    def test_a_path_with_no_vias_is_unaffected_either_way(self):
        def mutate(document, _project):
            document["timing"]["propagation"] = {
                "backend": "analytic", "model": propagation.HAMMERSTAD}
            steps = document["timing"]["interfaces"]["series"]["routes"][
                "template"]["steps"]
            steps[1]["delay_model"] = {"model": "none",
                                       "justification": "fixture"}
        fixture = make(mutate=mutate, tag="via_none_path")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        # No vias, so the via policy contributes nothing either way; the
        # component's justified omission is what keeps it a lower bound.
        self.assertEqual(record["vias"], [])
        self.assertTrue(record["delay_is_lower_bound"])


# ---------------------------------------------------------------------------
# what a via's vertical span makes the stackup responsible for
# ---------------------------------------------------------------------------

def _multi_via_interface(document, _project):
    document["timing"]["interfaces"] = {"multi": {
        "description": "two layer changes",
        "routes": _route([{"kind": "copper", "net": "SIG_M",
                           "from": "D5.1", "to": "L7.1"}], "multi")}}


class AViaSpanIsPartOfTheStackupQuestion(unittest.TestCase):
    """The dielectric a barrel passes through is data the model reads.

    Scoping the stackup question to the layers carrying horizontal copper asks
    for less than the calculation consumes: a via joining the two outer layers
    of a four-layer board passes both inner planes and all three dielectrics,
    and a stackup silent about the middle one was being called complete.
    """

    def _fixture(self, tag, via_model, layers=None):
        def mutate(document, project):
            _multi_via_interface(document, project)
            document["timing"]["propagation"]["via_delay_model"] = via_model
            document["timing"]["physical_stackup"]["require_complete"] = True
            if layers is not None:
                _supplement(layers)(document, project)
        return make(mutate=mutate, with_stackup=layers is None, tag=tag)

    def test_the_span_layers_are_identified(self):
        fixture = self._fixture("span_ids", propagation.VIA_GEOMETRIC)
        paths, stack = fixture.geometry(), fixture.stackup().stackup
        self.assertEqual(sorted(paths.layers_used()), ["B.Cu", "F.Cu"])
        self.assertEqual(sorted(paths.via_span_layers(stack)),
                         ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"])

    def test_missing_data_inside_the_span_blocks_a_geometric_via_model(self):
        """The defect: dielectric 2 is read by the model and by nothing else."""
        layers = _fixture_layers()
        for entry in layers:
            if entry["name"] == "dielectric 2":
                entry["epsilon_r"] = None
                entry["thickness_mm"] = None
        fixture = self._fixture("span_gap", propagation.VIA_GEOMETRIC, layers)
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any(f.get("layer") == "dielectric 2"
                            for f in result.findings), result.findings)

    def test_the_same_gap_is_irrelevant_when_no_via_model_reads_it(self):
        """And it must not be demanded for its own sake."""
        layers = _fixture_layers()
        for entry in layers:
            if entry["name"] == "dielectric 2":
                entry["epsilon_r"] = None
                entry["thickness_mm"] = None
        fixture = self._fixture(
            "span_nogap",
            {"model": propagation.VIA_NONE, "justification": "fixture"},
            layers)
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_the_delay_itself_also_refuses(self):
        layers = _fixture_layers()
        for entry in layers:
            if entry["name"] == "dielectric 2":
                entry["epsilon_r"] = None
        fixture = self._fixture("span_delay", propagation.VIA_GEOMETRIC, layers)
        record = _find(fixture, "multi")["delay"]
        self.assertIsNone(record["delay_ps"])
        self.assertTrue(any(i["portion"] == "via"
                            for i in record["insufficient"]), record)


# ---------------------------------------------------------------------------
# a plated hole inside a footprint is still a plated hole
# ---------------------------------------------------------------------------

class PadsCanBeLayerTransitions(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"pth": {
                "description": "a layer change through a through-hole pad",
                "expected_path_count": 1,
                "routes": _route([{"kind": "copper", "net": "SIG_P",
                                   "from": "D6.1", "to": "L8.1"}], "pth")}}
        cls.fixture = make(mutate=mutate, tag="pthpad")
        cls.record = _find(cls.fixture, "pth")

    def test_the_transit_is_not_silently_dropped(self):
        """It changes layer inside a pad, and that is still going down a hole."""
        transitions = self.record["resolved"].via_transitions()
        self.assertEqual(len(transitions), 1, transitions)
        self.assertEqual(transitions[0]["through"], "pad")
        self.assertEqual(transitions[0]["pad"], "TH1.1")
        self.assertTrue(transitions[0]["plated"])

    def test_it_spans_the_layers_the_pad_is_on(self):
        transition = self.record["resolved"].via_transitions()[0]
        self.assertEqual(transition["from_layer"], "F.Cu")
        self.assertEqual(transition["to_layer"], "B.Cu")
        self.assertEqual(transition["via_top_layer"], "F.Cu")
        self.assertEqual(transition["via_bottom_layer"], "B.Cu")

    def test_the_copper_either_side_is_measured(self):
        by_layer = self.record["resolved"].length_by_layer_mm()
        self.assertAlmostEqual(by_layer["F.Cu"], LENGTH_PTH_TOP_MM, places=4)
        self.assertAlmostEqual(by_layer["B.Cu"], LENGTH_PTH_BOTTOM_MM, places=4)
        self.assertAlmostEqual(self.record["resolved"].copper_length_mm,
                               PATH_PTH_MM, places=4)

    def test_a_via_model_treats_it_like_any_other_barrel(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"pth": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_P",
                                   "from": "D6.1", "to": "L8.1"}], "pth")}}
            document["timing"]["propagation"]["via_delay_model"] = \
                propagation.VIA_GEOMETRIC
        fixture = make(mutate=mutate, tag="pthgeom")
        via = _find(fixture, "pth")["delay"]["vias"][0]
        self.assertEqual(via["through"], "pad")
        self.assertAlmostEqual(via["vertical_length_mm"], VIA_VERTICAL_MM,
                               places=6)
        self.assertGreater(via["delay_ps"], 0.0)

    def test_the_pad_span_counts_towards_the_stackup_question(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"pth": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_P",
                                   "from": "D6.1", "to": "L8.1"}], "pth")}}
            document["timing"]["propagation"]["via_delay_model"] = \
                propagation.VIA_GEOMETRIC
        fixture = make(mutate=mutate, tag="pthspan")
        paths, stack = fixture.geometry(), fixture.stackup().stackup
        self.assertEqual(sorted(paths.via_span_layers(stack)),
                         ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"])

    def test_a_pad_entered_and_left_on_one_layer_is_no_transition(self):
        record = _find(self.fixture, "pth")
        for other in ("series_branch_to_L1",):
            pass
        # The series path runs entirely on F.Cu through two SMD pads.
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"flat": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_B",
                                   "from": "R1.2", "to": "L2.1"}], "flat")}}
        flat = make(mutate=mutate, tag="pthflat")
        self.assertEqual(_find(flat, "flat")["resolved"].via_transitions(), [])


# ---------------------------------------------------------------------------
# backends: what the machine has is not what the release implements
# ---------------------------------------------------------------------------

class BackendAvailabilityIsAboutThisRelease(unittest.TestCase):
    """Every case here holds whether or not a solver is installed.

    That is the property under test. The old probe asked the filesystem, so a
    board declaring `required: false` fell back cleanly on a machine without
    openEMS and hard-errored on a machine with it - a validator answering
    differently depending on whose laptop it ran on.
    """

    def test_a_recognised_but_unimplemented_backend_is_not_available(self):
        from pcbqa import backends
        ok, detail = backends.available("openems")
        self.assertFalse(ok)
        self.assertIn("implements no evaluation", detail)

    def test_that_holds_with_the_executable_present(self):
        from pcbqa import backends
        from pcbqa.backends import openems
        ok, detail = backends.available(
            "openems", {"executable": sys.executable})
        self.assertEqual(openems.executable({"executable": sys.executable}),
                         sys.executable, "the probe should have found it")
        self.assertFalse(ok, "an installed binary this release cannot drive is "
                             "not availability")
        self.assertIn("does not help", detail)

    def test_required_blocks_with_the_executable_present(self):
        from pcbqa import backends
        with self.assertRaises(backends.BackendUnavailable):
            backends.select("openems", {"required": True,
                                        "executable": sys.executable})

    def test_optional_falls_back_with_the_executable_present(self):
        from pcbqa import backends
        selection = backends.select("openems", {"required": False,
                                                "executable": sys.executable})
        self.assertEqual(selection.used, backends.ANALYTIC)
        self.assertTrue(selection.fell_back)

    def test_optional_falls_back_with_the_executable_absent(self):
        from pcbqa import backends
        selection = backends.select("openems", {"required": False,
                                                "executable": "/nowhere/at/all"})
        self.assertEqual(selection.used, backends.ANALYTIC)
        self.assertTrue(selection.fell_back)

    def test_the_two_states_give_the_same_answer(self):
        from pcbqa import backends
        present = backends.select("openems", {"required": False,
                                              "executable": sys.executable})
        absent = backends.select("openems", {"required": False,
                                             "executable": "/nowhere"})
        self.assertEqual((present.used, present.fell_back),
                         (absent.used, absent.fell_back))

    def test_describe_still_reports_the_binary_for_a_human(self):
        from pcbqa import backends
        rows = {row["backend"]: row for row in backends.describe()}
        self.assertFalse(rows["openems"]["evaluation_implemented"])
        self.assertTrue(rows["analytic"]["evaluation_implemented"])
        self.assertIn("executable_found", rows["openems"])

    def test_the_gates_are_deterministic_either_way(self):
        for tag, executable in (("be_present", sys.executable),
                                ("be_absent", "/nowhere/at/all")):
            def mutate(document, _project, executable=executable):
                document["timing"]["propagation"].update(
                    {"backend": "openems", "required": False,
                     "executable": executable})
            fixture = make(mutate=mutate, tag=tag)
            result = fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
                "TIMING.INTERCONNECT_DELAY"]
            self.assertNotEqual(result.status, Status.ERROR, tag)
            self.assertEqual(result.measurements["backend_used"], "analytic")
            self.assertTrue(result.measurements["backend_fell_back"], tag)


# ---------------------------------------------------------------------------
# the supplement is a gap-filler, not a second authority
# ---------------------------------------------------------------------------

class TheSupplementMayNotOutrankTheBoard(unittest.TestCase):

    def _merge(self, layers, **top):
        native = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers(), "total_thickness_mm": 1.305},
            source=stackup_physical.NATIVE)
        declared = stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers, **top})
        return stackup_physical.merge(native, declared)

    def test_a_different_overall_thickness_is_refused(self):
        with self.assertRaises(stackup_physical.StackupError) as caught:
            self._merge(_fixture_layers(), total_thickness_mm=1.6)
        self.assertIn("overall thickness", str(caught.exception))

    def test_the_same_overall_thickness_is_fine(self):
        self.assertTrue(self._merge(_fixture_layers(),
                                    total_thickness_mm=1.305))

    def test_reordering_the_board_layers_is_refused(self):
        layers = _fixture_layers()
        layers[0], layers[2] = layers[2], layers[0]
        with self.assertRaises(stackup_physical.StackupError) as caught:
            self._merge(layers)
        self.assertIn("different order", str(caught.exception))

    def test_reclassifying_a_layer_is_refused(self):
        layers = _fixture_layers()
        for entry in layers:
            if entry["name"] == "In1.Cu":
                entry["kind"] = stackup_physical.DIELECTRIC
        with self.assertRaises(stackup_physical.StackupError) as caught:
            self._merge(layers)
        self.assertIn("calls layer", str(caught.exception))

    def test_a_field_the_board_states_may_not_be_restated_differently(self):
        layers = _fixture_layers()
        for entry in layers:
            if entry["name"] == "dielectric 1":
                entry["epsilon_r"] = 9.9
        with self.assertRaises(stackup_physical.StackupError):
            self._merge(layers)

    def test_filling_a_gap_the_board_left_is_the_point(self):
        native_layers = _fixture_layers()
        for entry in native_layers:
            if entry["name"] == "dielectric 1":
                entry["epsilon_r"] = None
        native = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": native_layers}, source=stackup_physical.NATIVE)
        merged = stackup_physical.merge(
            native,
            stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": _fixture_layers()}))
        self.assertEqual(merged.layer("dielectric 1").epsilon_r,
                         FIXTURE_EPSILON_R)

    def test_the_board_file_wins_on_the_real_thing(self):
        """Native data present, supplement silent: nothing moves."""
        fixture = make(mutate=_supplement(
            [{"name": "dielectric 1", "kind": stackup_physical.DIELECTRIC}]),
            tag="supp_silent")
        stack = fixture.stackup().stackup
        self.assertEqual(stack.layer("dielectric 1").epsilon_r,
                         FIXTURE_EPSILON_R)


# ---------------------------------------------------------------------------
# a reference plane has to be under the trace
# ---------------------------------------------------------------------------

class ReferenceContinuity(unittest.TestCase):
    """A layer carrying a pour somewhere is not a reference conductor here."""

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"ref": {
                "description": "one route over the pour and one past its edge",
                "routes": {"paths": [
                    {"id": "over_copper", "steps": [
                        {"kind": "copper", "net": "SIG_A", "from": "D1.1",
                         "to": "R1.1"}]},
                    {"id": "past_the_edge", "steps": [
                        {"kind": "copper", "net": "SIG_X", "from": "D10.1",
                         "to": "L11.1"}]}]}}}
        cls.fixture = make(mutate=mutate, tag="refcont")

    def test_the_pour_is_read_as_filled_copper_not_as_an_outline(self):
        paths = self.fixture.geometry()
        self.assertEqual(sorted(paths.reference_copper), ["In1.Cu", "In2.Cu"])
        self.assertEqual(paths.unfilled_layers, [])

    def test_a_route_over_the_pour_is_fully_referenced(self):
        conductors = _find(self.fixture, "over_copper")[
            "resolved"].conductors()
        self.assertTrue(conductors)
        for conductor in conductors:
            self.assertTrue(conductor["reference_checked"])
            per_layer = conductor["unreferenced_by_layer_mm"]
            # Each candidate plane reported on its own, the pair intersection
            # for a two-plane geometry, and the union for the design measure.
            self.assertEqual(sorted(per_layer),
                             ["<any>", "In1.Cu", "In1.Cu&In2.Cu", "In2.Cu"])
            for missing in per_layer.values():
                self.assertAlmostEqual(missing, 0.0, places=4)

    def test_a_route_past_the_edge_is_not(self):
        conductors = _find(self.fixture, "past_the_edge")[
            "resolved"].conductors()
        per_layer = conductors[0]["unreferenced_by_layer_mm"]
        self.assertEqual(sorted(per_layer),
                         ["<any>", "In1.Cu", "In1.Cu&In2.Cu", "In2.Cu"])
        for missing in per_layer.values():
            self.assertAlmostEqual(missing, LENGTH_UNREFERENCED_MM, places=3)

    def test_and_the_model_refuses_rather_than_guessing(self):
        record = _find(self.fixture, "past_the_edge")["delay"]
        self.assertIsNone(record["delay_ps"])
        self.assertTrue(any("geometry is incomplete" in i["issue"]
                            for i in record["insufficient"]), record)

    def test_the_referenced_route_still_computes(self):
        record = _find(self.fixture, "over_copper")["delay"]
        self.assertIsNotNone(record["delay_ps"])
        self.assertAlmostEqual(
            record["delay_ps"],
            LENGTH_PRE_SERIES_MM * expected_microstrip_ps_per_mm(), places=4)

    def test_assuming_continuity_takes_a_treatment_a_bound_and_a_reason(self):
        """And it is a statement about the formula, not about the design."""
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"ref": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_X",
                                   "from": "D10.1", "to": "L11.1"}],
                                 "past_the_edge")}}
            document["timing"]["propagation"][
                "reference_discontinuity"] = {
                    "treatment": "assume_continuous",
                    "up_to_mm": LENGTH_UNREFERENCED_MM + 1.0,
                    "reference_layers": ["In1.Cu", "In2.Cu"],
                    "justification": "fixture: assumed for the test"}
        fixture = make(mutate=mutate, tag="reftol")
        record = _find(fixture, "past_the_edge")["delay"]
        self.assertIsNotNone(record["delay_ps"])
        # The assumption is exercised and recorded, not silently absorbed -
        # per plane, naming the assumption that covered each gap.
        self.assertTrue(record["assumptions"])
        gaps = record["assumptions"][0]["covered_gaps"]
        # A microstrip is referenced to one plane, so exactly one gap needed
        # covering - the assumption on the other plane was never consulted.
        self.assertEqual([g["plane"] for g in gaps], ["In1.Cu"])
        for gap in gaps:
            self.assertAlmostEqual(gap["unreferenced_mm"],
                                   LENGTH_UNREFERENCED_MM, places=3)
        # And the value it produced is marked as standing on an assumption.
        self.assertEqual(record["fidelity"],
                         propagation.ASSUMED_TRANSMISSION_LINE)
        # The path-level accumulation is surfaced as one number: a bound
        # written per run must not hide how much the whole path leaned on it.
        self.assertAlmostEqual(record["assumed_unreferenced_total_mm"],
                               LENGTH_UNREFERENCED_MM, places=3)

    def test_a_bound_smaller_than_the_gap_still_refuses(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"ref": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_X",
                                   "from": "D10.1", "to": "L11.1"}],
                                 "past_the_edge")}}
            document["timing"]["propagation"][
                "reference_discontinuity"] = {
                    "treatment": "assume_continuous", "up_to_mm": 1.0,
                    "reference_layers": ["In1.Cu", "In2.Cu"],
                    "justification": "fixture"}
        fixture = make(mutate=mutate, tag="reftol_small")
        self.assertIsNone(
            _find(fixture, "past_the_edge")["delay"]["delay_ps"])

    def test_assuming_continuity_without_its_parts_is_refused(self):
        """A treatment, a size, a reason, and the plane it applies to."""
        for declaration in ({"treatment": "assume_continuous"},
                            {"treatment": "assume_continuous",
                             "up_to_mm": 1.0},
                            {"treatment": "assume_continuous",
                             "up_to_mm": 1.0, "justification": "x"},
                            {"treatment": "wishful"}):
            with self.assertRaises(propagation.PropagationError):
                propagation.ReferenceDiscontinuity(declaration)

    def test_a_design_tolerance_does_not_make_the_formula_valid(self):
        """The two questions are answered by two different declarations."""
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"ref": {
                "description": "x",
                "max_unreferenced_mm": LENGTH_UNREFERENCED_MM + 1.0,
                "routes": _route([{"kind": "copper", "net": "SIG_X",
                                   "from": "D10.1", "to": "L11.1"}],
                                 "past_the_edge")}}
        fixture = make(mutate=mutate, tag="refdesign")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        # The board accepts the discontinuity as a design matter...
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertAlmostEqual(
            result.measurements["worst_path_unreferenced_mm"],
            LENGTH_UNREFERENCED_MM, places=3)
        # ...and the microstrip formula still does not describe that copper.
        self.assertIsNone(
            _find(fixture, "past_the_edge")["delay"]["delay_ps"])

    def test_the_design_limit_can_also_fail(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"ref": {
                "description": "x",
                "max_unreferenced_mm": 1.0,
                "routes": _route([{"kind": "copper", "net": "SIG_X",
                                   "from": "D10.1", "to": "L11.1"}],
                                 "past_the_edge")}}
        fixture = make(mutate=mutate, tag="refdesign_fail")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL, result.reason)

    def test_an_unfilled_pour_is_reported_rather_than_believed(self):
        """A zone nobody filled answers no question about what is underneath."""
        fixture = make(tag="refunfilled", fill_zones=False)
        paths = fixture.geometry()
        self.assertEqual(paths.reference_copper, {})
        self.assertEqual(sorted(paths.unfilled_layers), ["In1.Cu", "In2.Cu"])
        result = fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertTrue(any("no filled polygons" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)


# ---------------------------------------------------------------------------
# duplicate pad numbers
# ---------------------------------------------------------------------------

class DuplicatePadNumbers(unittest.TestCase):
    """KiCad allows them, so a selector has to have an answer for them."""

    def _integrity(self, tag, net, target):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"dup": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": net,
                                   "from": "D{}.1".format(
                                       "7" if net == "SIG_Q" else "8"),
                                   "to": target}], "dup")}}
        fixture = make(mutate=mutate, tag=tag)
        return fixture, fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]

    def test_both_physical_pads_are_kept(self):
        fixture = make(tag="dup_index")
        pads, _footprints = fixture.geometry_resolver()._index()
        self.assertEqual(len(pads["DUP1.1"]), 2)
        self.assertEqual(len(pads["DUP2.1"]), 2)

    def test_pads_sharing_a_number_on_one_net_are_one_place(self):
        _fixture, result = self._integrity("dup_one", "SIG_Q", "DUP1.1")
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_pads_sharing_a_number_on_two_nets_are_ambiguous(self):
        _fixture, result = self._integrity("dup_two", "SIG_R", "DUP2.1")
        self.assertEqual(result.status, Status.FAIL, result.reason)
        self.assertTrue(any("one electrical place" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_the_ambiguity_does_not_depend_on_iteration_order(self):
        """Whichever pad is seen first, the answer is the same refusal."""
        for tag in ("dup_order_a", "dup_order_b"):
            _fixture, result = self._integrity(tag, "SIG_R", "DUP2.1")
            self.assertEqual(result.status, Status.FAIL)

    def test_a_component_traversal_onto_a_split_number_refuses(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"dup": {
                "description": "x",
                "routes": {"paths": [{"id": "cross", "steps": [
                    {"kind": "copper", "net": "SIG_R", "from": "D8.1",
                     "to": "DUP2.1"},
                    {"kind": "component", "reference": "DUP2",
                     "from_pad": "1", "to_pad": "1"},
                    {"kind": "copper", "net": "SIG_U", "from": "DUP2.1",
                     "to": "DUP2.1"}]}]}}}
        fixture = make(mutate=mutate, tag="dup_cross")
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.FAIL)


# ---------------------------------------------------------------------------
# junction geometry that is not a clean tee
# ---------------------------------------------------------------------------

class NontrivialJunctionGeometry(unittest.TestCase):

    def _ambiguity(self, tag, net, source, target, pid):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"j": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": net,
                                   "from": source, "to": target}], pid)}}
        fixture = make(mutate=mutate, tag=tag)
        return _find(fixture, pid)

    def test_a_clean_perpendicular_tee_is_barely_ambiguous(self):
        """One junction, so the bound is half the crossing track's width."""
        record = self._ambiguity("j_tee", "SIG_T", "D3.1", "L4.1", "tee")
        self.assertAlmostEqual(record["resolved"].copper_length_mm,
                               TEE_SPLIT_MM, places=4)
        self.assertEqual(record["delay"]["ambiguous_junctions"], 1)
        self.assertAlmostEqual(record["delay"]["length_uncertainty_mm"],
                               TRACK_WIDTH_MM / 2.0, places=3)

    def test_an_oblique_wide_junction_says_how_far_out_it_could_be(self):
        """Two 1 mm tracks at forty-five degrees share a long region.

        The shared region runs 1 + 1/sqrt(2) mm along the straight track: the
        crossing track's own width plus the extra its diagonal edges reach.
        No single cut point through that is the right one, so the width of it
        is reported rather than hidden.
        """
        record = self._ambiguity("j_obl", "SIG_W", "D9.1", "L10.1", "obl")
        span = OBLIQUE_WIDTH_MM * (1.0 + 1.0 / math.sqrt(2.0))
        self.assertEqual(record["delay"]["ambiguous_junctions"], 1)
        self.assertAlmostEqual(record["delay"]["length_uncertainty_mm"],
                               span / 2.0, places=3)

    def test_the_ambiguity_is_much_larger_than_a_clean_tee(self):
        tee = self._ambiguity("j_tee2", "SIG_T", "D3.1", "L4.1", "tee")
        obl = self._ambiguity("j_obl2", "SIG_W", "D9.1", "L10.1", "obl")
        self.assertGreater(obl["delay"]["length_uncertainty_mm"],
                           5 * tee["delay"]["length_uncertainty_mm"])

    def test_a_straight_run_with_nothing_on_it_is_unambiguous(self):
        record = self._ambiguity("j_straight", "SIG_A", "D1.1", "R1.1",
                                 "straight")
        self.assertEqual(record["delay"]["length_uncertainty_mm"], 0.0)
        self.assertEqual(record["delay"]["ambiguous_junctions"], 0)

    def test_the_midpoint_convention_matches_centre_line_lengths(self):
        """A tee measured centre line to centre line, as any EDA tool reports.

        Cutting at the overlap's ends instead was tried: it lets the walk enter
        at whichever end is nearer and shortens this by half a track width.
        """
        record = self._ambiguity("j_conv", "SIG_T", "D3.1", "L4.1", "tee")
        self.assertAlmostEqual(
            record["resolved"].copper_length_mm,
            LENGTH_TEE_TO_JUNCTION_MM + LENGTH_TEE_STUB_MM, places=6)


class AnUnfilledReferenceZoneBlocksPropagation(unittest.TestCase):
    """Filled, unfilled and absent are three states, and only one is safe.

    An unfilled zone used to leave the coverage map empty, and an empty map
    read as "nothing is uncovered" - so a board that had simply never been
    refilled produced delays as though every plane were continuous. The
    stackup was otherwise complete, so nothing else objected either.
    """

    @classmethod
    def setUpClass(cls):
        cls.fixture = make(tag="unfilled", fill_zones=False)

    def test_geometry_reports_the_zones_as_unfilled(self):
        paths = self.fixture.geometry()
        self.assertEqual(paths.reference_copper, {})
        self.assertEqual(sorted(paths.unfilled_layers), ["In1.Cu", "In2.Cu"])

    def test_no_delay_is_produced_from_an_unfilled_plane(self):
        record = _find(self.fixture, "series_branch_to_L1")["delay"]
        self.assertIsNone(record["delay_ps"])
        self.assertTrue(any("no filled polygons" in i["issue"]
                            for i in record["insufficient"]), record)

    def test_the_stackup_gate_treats_it_as_missing_data(self):
        result = self.fixture.gates(only={"STACK.PHYSICAL"})["STACK.PHYSICAL"]
        self.assertNotEqual(result.status, Status.PASS, result.reason)
        self.assertTrue(any(f.get("field") == "reference_fill"
                            for f in result.findings), result.findings)

    def test_the_same_board_filled_does_produce_one(self):
        """Proving the refusal is about the fill and nothing else."""
        record = _find(make(tag="unfilled_control"),
                       "series_branch_to_L1")["delay"]
        self.assertIsNotNone(record["delay_ps"])

    def test_path_integrity_is_unaffected(self):
        result = self.fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS, result.reason)


class CoverageFollowsThePlaneTheFormulaUses(unittest.TestCase):
    """Reference copper somewhere in the stack is not reference copper here.

    Both routes below sit at the same coordinates over the same band. One is on
    the top layer, referenced to the interrupted inner plane; the other is on
    the bottom layer, referenced to the continuous one. Unioning coverage
    across reference-net layers made them indistinguishable and passed both.
    """

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"split": {
                "description": "one route per outer layer over a split plane",
                "routes": {"paths": [
                    {"id": "over_the_gap", "steps": [
                        {"kind": "copper", "net": "SIG_S", "from": "DS.1",
                         "to": "LS.1"}]},
                    {"id": "over_the_whole_plane", "steps": [
                        {"kind": "copper", "net": "SIG_SB", "from": "DSB.1",
                         "to": "LSB.1"}]}]}}}
        cls.fixture = make(mutate=mutate, tag="splitplane")

    def test_the_two_planes_are_measured_separately(self):
        conductor = _find(self.fixture, "over_the_gap")[
            "resolved"].conductors()[0]
        per_layer = conductor["unreferenced_by_layer_mm"]
        self.assertAlmostEqual(per_layer["In1.Cu"], LENGTH_SPLIT_ROUTE_MM,
                               places=3)
        self.assertAlmostEqual(per_layer["In2.Cu"], 0.0, places=3)

    def test_the_route_referenced_to_the_broken_plane_refuses(self):
        record = _find(self.fixture, "over_the_gap")["delay"]
        self.assertIsNone(record["delay_ps"])
        problem = next(i for i in record["insufficient"]
                       if i["portion"] == "conductor")
        self.assertEqual(problem["reference_layers_used"], ["In1.Cu"])

    def test_the_route_referenced_to_the_continuous_plane_does_not(self):
        record = _find(self.fixture, "over_the_whole_plane")["delay"]
        self.assertIsNotNone(record["delay_ps"])
        self.assertAlmostEqual(
            record["delay_ps"],
            LENGTH_SPLIT_ROUTE_MM * expected_microstrip_ps_per_mm(), places=4)

    def test_the_two_routes_occupy_the_same_footprint(self):
        """So nothing but the reference plane can explain the difference."""
        over = _find(self.fixture, "over_the_gap")["resolved"]
        whole = _find(self.fixture, "over_the_whole_plane")["resolved"]
        self.assertAlmostEqual(over.copper_length_mm, whole.copper_length_mm,
                               places=6)
        self.assertEqual(over.conductors()[0]["unreferenced_by_layer_mm"],
                         whole.conductors()[0]["unreferenced_by_layer_mm"])


class APlatedHoleGoesThroughTheBoard(unittest.TestCase):
    """The barrel is the hole. Which layers carry pads is a padstack choice."""

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"sparse": {
                "description": "a through hole with copper only outside",
                "routes": _route([{"kind": "copper", "net": "SIG_SPARSE",
                                   "from": "D13.1", "to": "L15.1"}],
                                 "sparse")}}
        cls.fixture = make(mutate=mutate, tag="sparsepad")
        cls.transition = _find(cls.fixture, "sparse")[
            "resolved"].via_transitions()[0]

    def test_the_span_is_the_whole_board_not_the_pads(self):
        self.assertEqual(self.transition["via_top_layer"], "F.Cu")
        self.assertEqual(self.transition["via_bottom_layer"], "B.Cu")

    def test_the_padstack_is_recorded_separately(self):
        """Both facts are kept, because they are different facts.

        On a saved and reloaded board KiCad restores a through-hole pad's
        copper to every layer, so the two coincide here. They are still read
        from different places, which is what the in-memory test below shows.
        """
        self.assertIn("layers_with_copper", self.transition)
        self.assertIn("F.Cu", self.transition["layers_with_copper"])
        self.assertIn("B.Cu", self.transition["layers_with_copper"])

    def test_a_sparse_padstack_does_not_shorten_the_barrel(self):
        """The case finding 5 is about, built in memory.

        A through-hole pad carrying copper on only the outer layers still has
        a hole through the whole board. Deriving the span from copper
        membership would report it as spanning nothing at all - the two
        layers it does have copper on are the two ends - and on a padstack
        with copper removed from the *outer* layers it would be shorter still.
        KiCad restores full membership when such a board is saved and
        reloaded, and the enum that would prevent that is not exposed to
        Python, so this stays in memory where the distinction survives.
        """
        board = synth.new_board(layers=4, size_mm=40.0)
        net = synth.add_net(board, "SPARSE")
        synth.add_pad_footprint(board, "A1", 92.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=net)
        _fp, pads = synth.add_through_hole_footprint(
            board, "H1", 100.0, 100.0, net=net,
            copper_layers=(pcbnew.F_Cu, pcbnew.B_Cu))
        synth.add_pad_footprint(board, "A2", 108.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=net,
                                flipped=True)
        synth.add_track(board, (92.0, 100.0), (100.0, 100.0), net=net,
                        layer=pcbnew.F_Cu, width_mm=TRACK_WIDTH_MM)
        synth.add_track(board, (100.0, 100.0), (108.0, 100.0), net=net,
                        layer=pcbnew.B_Cu, width_mm=TRACK_WIDTH_MM)
        stack = [pcbnew.LayerName(layer) for layer
                 in board.GetEnabledLayers().CuStack()]
        self.assertEqual(
            [name for name in stack if pads[0].IsOnLayer(
                board.GetEnabledLayers().CuStack()[stack.index(name)])],
            ["F.Cu", "B.Cu"],
            "the fixture should carry copper on the outer layers only")

        geom.configure(0.001)
        resolver = electrical_path.PathResolver(board,
                                                geom.pad_copper_polygon)
        path = electrical_path.paths_from_spec({"paths": [{
            "id": "sparse", "steps": [{"kind": "copper", "net": "SPARSE",
                                       "from": "A1.1", "to": "A2.1"}]}]})[0]
        resolved = resolver.resolve(path)[0]
        transition = resolved.via_transitions()[0]
        # The hole goes through the board whatever the padstack says.
        self.assertEqual(transition["via_top_layer"], stack[0])
        self.assertEqual(transition["via_bottom_layer"], stack[-1])
        self.assertEqual(transition["layers_with_copper"], ["F.Cu", "B.Cu"])

    def test_a_surface_mount_pad_cannot_be_a_layer_change(self):
        source = open(os.path.join(paths.PACKAGE, "electrical_path.py"),
                      encoding="utf-8").read()
        self.assertIn("Only a plated hole", source)

    def test_the_barrel_reaches_the_layers_a_geometric_model_integrates(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"sparse": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_SPARSE",
                                   "from": "D13.1", "to": "L15.1"}],
                                 "sparse")}}
            document["timing"]["propagation"]["via_delay_model"] = \
                propagation.VIA_GEOMETRIC
        fixture = make(mutate=mutate, tag="sparsegeom")
        via = _find(fixture, "sparse")["delay"]["vias"][0]
        self.assertAlmostEqual(via["vertical_length_mm"], VIA_VERTICAL_MM,
                               places=6)


class WhatTheGeometricViaModelClaims(unittest.TestCase):
    """The traversed span, and explicitly not the stub either side of it."""

    def _model(self, via_model=None):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        return propagation.PropagationModel(
            stack, {"In1.Cu", "In2.Cu"},
            via_model=via_model or propagation.VIA_GEOMETRIC)

    def test_an_inner_to_inner_transit_integrates_only_that_span(self):
        """A full through via used between the two inner layers."""
        record = self._model().via({
            "from_layer": "In1.Cu", "to_layer": "In2.Cu",
            "via_top_layer": "F.Cu", "via_bottom_layer": "B.Cu"})
        self.assertAlmostEqual(record["vertical_length_mm"], FIXTURE_CORE_MM,
                               places=6)
        self.assertAlmostEqual(
            record["delay_ps"],
            FIXTURE_CORE_MM * math.sqrt(FIXTURE_EPSILON_R) / 0.299792458,
            places=4)

    def test_the_unused_barrel_is_named_as_unmodelled(self):
        record = self._model().via({
            "from_layer": "In1.Cu", "to_layer": "In2.Cu",
            "via_top_layer": "F.Cu", "via_bottom_layer": "B.Cu"})
        stub = record["unmodelled_stub"]
        self.assertEqual(stub["above"], "F.Cu")
        self.assertEqual(stub["below"], "B.Cu")
        self.assertIn("not modelled", stub["note"])

    def test_an_outer_to_outer_transit_has_no_stub(self):
        record = self._model().via({
            "from_layer": "F.Cu", "to_layer": "B.Cu",
            "via_top_layer": "F.Cu", "via_bottom_layer": "B.Cu"})
        self.assertIsNone(record["unmodelled_stub"])

    def test_completeness_asks_only_about_the_traversed_span(self):
        """Data the calculation never reads is not data it is missing."""
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"multi": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_M",
                                   "from": "D5.1", "to": "L7.1"}], "multi")}}
            document["timing"]["propagation"]["via_delay_model"] = \
                propagation.VIA_GEOMETRIC
        fixture = make(mutate=mutate, tag="spanscope")
        paths, stack = fixture.geometry(), fixture.stackup().stackup
        # The vias here join the outer layers, so the traversed span is the
        # whole stack and every dielectric is read.
        self.assertEqual(sorted(paths.via_span_layers(stack)),
                         ["B.Cu", "F.Cu", "In1.Cu", "In2.Cu"])


class MoreThanOneAmbiguousJunction(unittest.TestCase):
    """Two stubs on one run, so the bound has to accumulate over both."""

    @classmethod
    def setUpClass(cls):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"mj": {
                "description": "x",
                "routes": {"paths": [
                    {"id": "past_one", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L13.1"}]},
                    {"id": "past_two", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L14.1"}]}]}}}
        cls.fixture = make(mutate=mutate, tag="multijunction")

    def test_the_near_stub_crosses_one_junction(self):
        record = _find(self.fixture, "past_one")
        self.assertAlmostEqual(record["resolved"].copper_length_mm,
                               LENGTH_TO_FIRST_STUB_MM + LENGTH_MULTI_STUB_MM,
                               places=4)
        self.assertEqual(record["delay"]["ambiguous_junctions"], 1)
        self.assertAlmostEqual(record["delay"]["length_uncertainty_mm"],
                               TRACK_WIDTH_MM / 2.0, places=4)

    def test_the_far_stub_crosses_two_and_the_bound_adds_up(self):
        record = _find(self.fixture, "past_two")
        self.assertAlmostEqual(record["resolved"].copper_length_mm,
                               LENGTH_TO_SECOND_STUB_MM + LENGTH_MULTI_STUB_MM,
                               places=4)
        self.assertEqual(record["delay"]["ambiguous_junctions"], 2)
        self.assertAlmostEqual(record["delay"]["length_uncertainty_mm"],
                               2 * (TRACK_WIDTH_MM / 2.0), places=4)

    def test_a_junction_is_counted_once_however_many_pieces_meet_at_it(self):
        """Two pieces bound each cut; the cut is still one cut."""
        near = _find(self.fixture, "past_one")["delay"]
        far = _find(self.fixture, "past_two")["delay"]
        self.assertEqual(far["ambiguous_junctions"],
                         near["ambiguous_junctions"] + 1)

    def test_it_is_a_bound_and_is_not_converted_into_a_delay(self):
        record = _find(self.fixture, "past_two")["delay"]
        self.assertNotIn("delay_uncertainty_ps", record)
        self.assertGreater(record["length_uncertainty_mm"], 0.0)


class ASupplementMayNotAddStructureOfAnyKind(unittest.TestCase):

    def _merge(self, layers):
        native = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()}, source=stackup_physical.NATIVE)
        return stackup_physical.merge(
            native, stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": layers}))

    def test_an_extra_dielectric_is_refused(self):
        layers = _fixture_layers() + [
            {"name": "dielectric 9", "kind": stackup_physical.DIELECTRIC,
             "type": "prepreg", "thickness_mm": 0.1, "epsilon_r": 4.0}]
        with self.assertRaises(stackup_physical.StackupError) as caught:
            self._merge(layers)
        self.assertIn("dielectric 9", str(caught.exception))

    def test_an_extra_copper_layer_is_refused(self):
        layers = _fixture_layers() + [
            {"name": "In9.Cu", "kind": stackup_physical.COPPER,
             "thickness_mm": 0.0175}]
        with self.assertRaises(stackup_physical.StackupError):
            self._merge(layers)

    def test_an_extra_mask_entry_is_refused_too(self):
        """A mask between a trace and its plane would move them apart."""
        layers = _fixture_layers() + [
            {"name": "F.Mask", "kind": stackup_physical.OTHER,
             "type": "Top Solder Mask", "thickness_mm": 0.01}]
        with self.assertRaises(stackup_physical.StackupError):
            self._merge(layers)

    def test_filling_a_property_on_a_layer_that_exists_is_still_fine(self):
        native_layers = _fixture_layers()
        for entry in native_layers:
            if entry["name"] == "dielectric 1":
                entry["epsilon_r"] = None
        native = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": native_layers}, source=stackup_physical.NATIVE)
        merged = stackup_physical.merge(
            native,
            stackup_physical.from_declaration({"provenance": "synthetic fixture values", "layers": _fixture_layers()}))
        self.assertEqual(merged.layer("dielectric 1").epsilon_r,
                         FIXTURE_EPSILON_R)

    def test_a_board_with_no_native_structure_may_declare_all_of_it(self):
        """The documented second source: nothing to contradict."""
        fixture = make(mutate=_supplement(_fixture_layers()),
                       with_stackup=False, tag="nostructure")
        stack = fixture.stackup().stackup
        self.assertEqual(stack.copper_layer_names,
                         ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])


# ---------------------------------------------------------------------------
# skew over intervals: the nominal spread proves nothing by itself
# ---------------------------------------------------------------------------

def _two_bounded_paths(bound_l1, bound_l2, limit=None):
    """Two paths through R1 with independently bounded omissions.

    Delays are exactly PATH_TO_L1_MM and PATH_TO_L2_MM times the fixture's
    microstrip constant, so every interval endpoint below is hand-calculable.
    A bound of None leaves that path's omission unbounded.
    """
    def model(bound):
        if bound is None:
            return {"model": "none", "justification": "fixture: unbounded"}
        return {"model": "none", "max_delay_ps": bound,
                "provenance": "fixture arithmetic",
                "justification": "fixture bound"}

    def route(load, bound):
        return {"id": "to_{}".format(load), "steps": [
            {"kind": "copper", "net": "SIG_A", "from": "D1.1", "to": "R1.1"},
            {"kind": "component", "reference": "R1", "from_pad": "1",
             "to_pad": "2", "delay_model": model(bound)},
            {"kind": "copper", "net": "SIG_B", "from": "R1.2",
             "to": "{}.1".format(load)}]}

    def mutate(document, _project):
        interface = {
            "description": "two arrivals with different omission bounds",
            "routes": {"paths": [route("L1", bound_l1), route("L2", bound_l2)]},
            "groups": {"pair": {"description": "both arrivals",
                                "paths": "^to_"}}}
        if limit is not None:
            interface["groups"]["pair"]["max_skew_ps"] = limit
        document["timing"]["interfaces"] = {"series": interface}
    return mutate


class SkewIntervalArithmetic(unittest.TestCase):
    """True skew lives in an interval, and decisions must live there too.

    Arrivals are dA < dB with omission bounds bA, bB, so arrival i is in
    [d_i, d_i + b_i] and the true skew is bracketed by

        lower = max(0, max_i(lo_i) - min_i(hi_i))
        upper = max_i(hi_i) - min_i(lo_i)

    The nominal spread dB - dA sits between the two and proves nothing on its
    own: a large bA can close the gap entirely, a large bB can widen it. The
    old arithmetic treated nominal > limit as a proven violation, which case
    three below shows to be false.
    """

    D_A = PATH_TO_L1_MM
    D_B = PATH_TO_L2_MM

    def _group(self, bound_l1, bound_l2, limit=None, tag="skewint"):
        fixture = make(mutate=_two_bounded_paths(bound_l1, bound_l2, limit),
                       tag=tag)
        result = fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})[
            "TIMING.INTERCONNECT_SKEW"]
        return result, result.measurements["groups"][0]

    def test_the_interval_endpoints_are_the_hand_calculated_ones(self):
        per_mm = expected_microstrip_ps_per_mm()
        nominal = (self.D_B - self.D_A) * per_mm
        _result, group = self._group(50.0, 1.0, tag="skew_ends")
        # lower: B must arrive at or after d_B; A can arrive as late as
        # d_A + 50, but B's own upper end d_B + 1 is the smaller ceiling.
        self.assertAlmostEqual(group["skew_lower_ps"], 0.0, places=4)
        self.assertAlmostEqual(group["skew_ps"], nominal, places=3)
        # upper: the realisable maximum pairs one path's latest against a
        # DIFFERENT path's earliest. A alone spans 50 ps, but A cannot arrive
        # at both of its own endpoints at once, so the widest realisable pair
        # is B at d_B + 1 against A at d_A.
        self.assertAlmostEqual(group["skew_upper_ps"], nominal + 1.0,
                               places=3)

    def test_a_single_member_group_can_have_no_skew(self):
        """One arrival, however uncertain, is zero skew - the loose formula
        reported its own interval width here."""
        def mutate(document, _project):
            _two_bounded_paths(50.0, 1.0)(document, _project)
            document["timing"]["interfaces"]["series"]["groups"]["pair"][
                "paths"] = "^to_L1$"
        fixture = make(mutate=mutate, tag="skew_single")
        result = fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})[
            "TIMING.INTERCONNECT_SKEW"]
        group = result.measurements["groups"][0]
        self.assertEqual(group["members"], 1)
        self.assertEqual(group["skew_lower_ps"], 0.0)
        self.assertEqual(group["skew_upper_ps"], 0.0)

    def test_omissions_can_erase_a_nonzero_nominal_spread(self):
        """bA covers the gap, so zero true skew is possible: no proven FAIL."""
        result, group = self._group(50.0, 1.0, limit=10.0, tag="skew_zero")
        self.assertEqual(group["skew_lower_ps"], 0.0)
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("falls inside the uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_small_bounds_leave_the_violation_proven(self):
        per_mm = expected_microstrip_ps_per_mm()
        result, group = self._group(2.0, 2.0, limit=10.0, tag="skew_fail")
        expected_lower = (self.D_B - self.D_A) * per_mm - 2.0
        self.assertAlmostEqual(group["skew_lower_ps"], expected_lower,
                               places=3)
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("whole of its uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_generous_limit_is_proven_met_through_the_bounds(self):
        result, _group = self._group(2.0, 2.0, limit=100.0, tag="skew_pass")
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_an_unbounded_member_forbids_pass_but_not_proven_fail(self):
        """No upper end anywhere near, yet the violation is still provable:
        an unbounded late arrival cannot make anyone arrive earlier."""
        per_mm = expected_microstrip_ps_per_mm()
        result, group = self._group(2.0, None, limit=10.0, tag="skew_unb")
        self.assertIsNone(group["skew_upper_ps"])
        expected_lower = (self.D_B - self.D_A) * per_mm - 2.0
        self.assertAlmostEqual(group["skew_lower_ps"], expected_lower,
                               places=3)
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("whole of its uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_an_unbounded_member_blocks_an_otherwise_generous_limit(self):
        result, _group = self._group(2.0, None, limit=1e6, tag="skew_unb2")
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("has no upper bound" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)


# ---------------------------------------------------------------------------
# geometry uncertainty against hard limits
# ---------------------------------------------------------------------------

class GeometryUncertaintyAndHardLimits(unittest.TestCase):
    """The toolkit's own length uncertainty must be able to veto a PASS."""

    def _length_group(self, limit, tag):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"mj": {
                "description": "two stub arrivals with junction uncertainty",
                "routes": {"paths": [
                    {"id": "past_one", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L13.1"}]},
                    {"id": "past_two", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L14.1"}]}]},
                "groups": {"pair": {"description": "both stubs",
                                    "max_length_spread_mm": limit}}}}
        fixture = make(mutate=mutate, tag=tag)
        result = fixture.gates(only={"TIMING.INTERCONNECT_SKEW"})[
            "TIMING.INTERCONNECT_SKEW"]
        return result, result.measurements["groups"][0]

    # nominal spread 8 mm; uncertainties 0.1 and 0.2 mm, so the spread is
    # bracketed by [7.7, 8.3].
    def test_the_length_spread_interval_is_the_hand_calculated_one(self):
        _result, group = self._length_group(9.0, "len_ends")
        self.assertAlmostEqual(group["length_spread_mm"], 8.0, places=4)
        self.assertAlmostEqual(group["length_spread_lower_mm"], 7.7, places=4)
        self.assertAlmostEqual(group["length_spread_upper_mm"], 8.3, places=4)

    def test_nominal_passes_but_the_uncertainty_crosses_the_limit(self):
        result, _group = self._length_group(8.1, "len_straddle")
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("falls inside the uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_nominal_fails_regardless_of_the_uncertainty(self):
        result, _group = self._length_group(7.5, "len_fail")
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("whole of its uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_limit_clear_of_the_interval_passes(self):
        result, _group = self._length_group(9.0, "len_pass")
        self.assertEqual(result.status, Status.PASS, result.reason)

    def _tee_delay_limit(self, limit_ps, tag):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"tee": {
                "description": "one ambiguous junction on the way",
                "routes": _route([{"kind": "copper", "net": "SIG_T",
                                   "from": "D3.1", "to": "L4.1"}], "tee"),
                "limits": {"max_delay_ps": limit_ps}}}
        fixture = make(mutate=mutate, tag=tag)
        return fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]

    def test_delay_uncertainty_uses_the_paths_own_velocity(self):
        fixture = make(mutate=lambda d, _p: d["timing"].update(
            {"interfaces": {"tee": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_T",
                                   "from": "D3.1", "to": "L4.1"}], "tee")}}}),
            tag="tee_u")
        record = _find(fixture, "tee")["delay"]
        per_mm = expected_microstrip_ps_per_mm()
        self.assertAlmostEqual(record["geometric_uncertainty_ps"],
                               (TRACK_WIDTH_MM / 2.0) * per_mm, places=4)
        self.assertAlmostEqual(record["delay_upper_ps"] - record["delay_ps"],
                               record["geometric_uncertainty_ps"], places=6)
        self.assertAlmostEqual(record["delay_ps"] - record["delay_lower_ps"],
                               record["geometric_uncertainty_ps"], places=6)

    def test_a_delay_limit_inside_the_geometric_interval_is_undecided(self):
        per_mm = expected_microstrip_ps_per_mm()
        nominal = TEE_SPLIT_MM * per_mm
        result = self._tee_delay_limit(nominal + 0.1, "tee_straddle")
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("falls inside the uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_delay_limit_below_the_interval_is_a_proven_fail(self):
        per_mm = expected_microstrip_ps_per_mm()
        result = self._tee_delay_limit(TEE_SPLIT_MM * per_mm - 1.0,
                                       "tee_fail")
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("whole of its uncertainty interval"
                            in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_a_delay_limit_clear_of_the_interval_passes(self):
        per_mm = expected_microstrip_ps_per_mm()
        result = self._tee_delay_limit(TEE_SPLIT_MM * per_mm + 1.0,
                                       "tee_pass")
        self.assertEqual(result.status, Status.PASS, result.reason)

    def test_an_unambiguous_path_has_a_degenerate_interval(self):
        fixture = make(tag="noamb")
        record = _find(fixture, "series_branch_to_L1")["delay"]
        self.assertEqual(record["geometric_uncertainty_ps"], 0.0)
        self.assertAlmostEqual(record["delay_lower_ps"], record["delay_ps"],
                               places=9)


# ---------------------------------------------------------------------------
# two reference planes with gaps in different places
# ---------------------------------------------------------------------------

class GapsOnTwoPlanesCombineAsAUnion(unittest.TestCase):
    """A stripline is missing wherever either plane is, and positions matter.

    Reduced to one scalar per plane, two disjoint 3 mm and 4 mm gaps look no
    worse than overlapping ones. The resolver therefore measures the pair
    intersection and the any-plane union while the shapes still exist, and
    each in-memory board below has gap positions chosen so the three answers
    differ and are checkable by hand.
    """

    def _board(self, gap_in1, gap_in2):
        """A 20 mm F.Cu trace with engineered gaps in each inner pour."""
        board = synth.new_board(layers=4, size_mm=60.0)
        gnd = synth.add_net(board, "GND")
        net = synth.add_net(board, "SIG")
        # Pour each plane as two rectangles leaving the declared gap.
        for layer, (gap_low, gap_high) in ((pcbnew.In1_Cu, gap_in1),
                                           (pcbnew.In2_Cu, gap_in2)):
            synth.add_zone(board, gnd, (layer,),
                           (80.0, 90.0, 80.0 + gap_low, 110.0))
            synth.add_zone(board, gnd, (layer,),
                           (80.0 + gap_high, 90.0, 120.0, 110.0))
        synth.add_pad_footprint(board, "A1", 85.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=net)
        synth.add_pad_footprint(board, "A2", 105.0, 100.0,
                                pcbnew.PAD_SHAPE_RECT, (0.5, 0.5), net=net)
        synth.add_track(board, (85.0, 100.0), (105.0, 100.0), net=net,
                        width_mm=TRACK_WIDTH_MM)
        geom.configure(0.001)
        from pcbqa.stackup_physical import reference_copper
        poured, unfilled = reference_copper(board, ["GND"])
        assert not unfilled
        resolver = electrical_path.PathResolver(
            board, geom.pad_copper_polygon, reference_copper=poured)
        path = electrical_path.paths_from_spec({"paths": [{
            "id": "run", "steps": [{"kind": "copper", "net": "SIG",
                                    "from": "A1.1", "to": "A2.1"}]}]})[0]
        return resolver.resolve(path)[0].conductors()[0][
            "unreferenced_by_layer_mm"]

    def test_disjoint_gaps_sum_in_the_pair_and_vanish_in_the_union(self):
        # In1 missing over trace x 10..13 (3 mm), In2 over 15..19 (4 mm).
        per = self._board((10.0, 13.0), (15.0, 19.0))
        self.assertAlmostEqual(per["In1.Cu"], 3.0, delta=0.1)
        self.assertAlmostEqual(per["In2.Cu"], 4.0, delta=0.1)
        self.assertAlmostEqual(per["In1.Cu&In2.Cu"], 7.0, delta=0.1)
        # Wherever one plane is missing the other is present, so the design
        # measure - no reference anywhere - is zero.
        self.assertAlmostEqual(per["<any>"], 0.0, delta=0.1)

    def test_coincident_gaps_do_not_sum(self):
        per = self._board((10.0, 13.0), (10.0, 13.0))
        self.assertAlmostEqual(per["In1.Cu&In2.Cu"], 3.0, delta=0.1)
        self.assertAlmostEqual(per["<any>"], 3.0, delta=0.1)

    def test_partial_overlap_lands_in_between(self):
        # In1 missing 10..13, In2 missing 12..16: either-missing 10..16 = 6,
        # both-missing 12..13 = 1.
        per = self._board((10.0, 13.0), (12.0, 16.0))
        self.assertAlmostEqual(per["In1.Cu&In2.Cu"], 6.0, delta=0.1)
        self.assertAlmostEqual(per["<any>"], 1.0, delta=0.1)

    def test_the_model_prefers_the_pair_key_and_falls_back_conservatively(self):
        stack = stackup_physical.from_declaration(
            {"provenance": "synthetic fixture values", "layers": _fixture_layers()})
        model = propagation.PropagationModel(stack, {"In1.Cu", "In2.Cu"})
        synthetic_model = {"reference_layers_used": ["In1.Cu", "In2.Cu"]}
        with_pair = {"layer": "F.Cu", "width_mm": 0.2, "length_mm": 20.0,
                     "reference_checked": True,
                     "unreferenced_by_layer_mm": {
                         "In1.Cu": 3.0, "In2.Cu": 4.0,
                         "In1.Cu&In2.Cu": 6.0}}
        _pp, combined, _u, _k = model._missing_on_used_planes(with_pair,
                                                              synthetic_model)
        self.assertEqual(combined, 6.0)
        without = dict(with_pair, unreferenced_by_layer_mm={
            "In1.Cu": 3.0, "In2.Cu": 4.0})
        _pp, combined, _u, _k = model._missing_on_used_planes(without,
                                                              synthetic_model)
        # No positions survive, so the sum is the only safe answer.
        self.assertEqual(combined, 7.0)


# ---------------------------------------------------------------------------
# assumptions are scoped to the condition they describe
# ---------------------------------------------------------------------------

def _split_route(document, _project):
    document["timing"]["interfaces"] = {"split": {
        "description": "a route over the interrupted plane",
        "routes": _route([{"kind": "copper", "net": "SIG_S", "from": "DS.1",
                           "to": "LS.1"}], "over_the_gap")}}


class AssumptionsAreScoped(unittest.TestCase):
    """An assumption describes one physical condition, not the whole board."""

    def _with_assumption(self, tag, **fields):
        def mutate(document, project):
            _split_route(document, project)
            document["timing"]["propagation"]["reference_discontinuity"] = {
                "treatment": "assume_continuous",
                "up_to_mm": LENGTH_SPLIT_ROUTE_MM + 1.0,
                "justification": "fixture", **fields}
        fixture = make(mutate=mutate, tag=tag)
        return _find(fixture, "over_the_gap")["delay"]

    def test_naming_the_gapped_plane_covers_it(self):
        record = self._with_assumption("scope_ok",
                                       reference_layers=["In1.Cu"])
        self.assertIsNotNone(record["delay_ps"])

    def test_naming_a_different_plane_does_not(self):
        """The gap is in In1.Cu; an assumption about In2.Cu says nothing
        about it, and a global reading would have waved it through."""
        record = self._with_assumption("scope_wrongplane",
                                       reference_layers=["In2.Cu"])
        self.assertIsNone(record["delay_ps"])

    def test_a_signal_layer_scope_must_match(self):
        record = self._with_assumption("scope_wrongsig",
                                       reference_layers=["In1.Cu"],
                                       signal_layers=["B.Cu"])
        self.assertIsNone(record["delay_ps"])

    def test_a_path_scope_must_match(self):
        record = self._with_assumption("scope_wrongpath",
                                       reference_layers=["In1.Cu"],
                                       paths="^some_other_interface")
        self.assertIsNone(record["delay_ps"])
        record = self._with_assumption("scope_rightpath",
                                       reference_layers=["In1.Cu"],
                                       paths="^over_")
        self.assertIsNotNone(record["delay_ps"])

    def test_several_assumptions_may_coexist(self):
        def mutate(document, project):
            _split_route(document, project)
            document["timing"]["propagation"]["reference_discontinuity"] = [
                {"treatment": "assume_continuous", "up_to_mm": 0.5,
                 "reference_layers": ["In2.Cu"],
                 "justification": "fixture: irrelevant entry"},
                {"treatment": "assume_continuous",
                 "up_to_mm": LENGTH_SPLIT_ROUTE_MM + 1.0,
                 "reference_layers": ["In1.Cu"],
                 "justification": "fixture: the relevant entry"}]
        fixture = make(mutate=mutate, tag="scope_list")
        record = _find(fixture, "over_the_gap")["delay"]
        self.assertIsNotNone(record["delay_ps"])
        covered = record["assumptions"][0]["covered_gaps"][0]
        self.assertEqual(covered["plane"], "In1.Cu")
        self.assertIn("relevant entry", covered["justification"])


class AssumedResultsAreMarked(unittest.TestCase):
    """A value standing on an assumption must never read as established."""

    @classmethod
    def setUpClass(cls):
        def mutate(document, project):
            _split_route(document, project)
            document["timing"]["interfaces"]["split"]["limits"] = {
                "max_delay_ps": 1e6}
            document["timing"]["propagation"]["reference_discontinuity"] = {
                "treatment": "assume_continuous",
                "up_to_mm": LENGTH_SPLIT_ROUTE_MM + 1.0,
                "reference_layers": ["In1.Cu"],
                "justification": "fixture"}
        cls.fixture = make(mutate=mutate, tag="assumedfid")

    def test_the_fidelity_is_the_assumed_rung(self):
        record = _find(self.fixture, "over_the_gap")["delay"]
        self.assertEqual(record["fidelity"],
                         propagation.ASSUMED_TRANSMISSION_LINE)

    def test_the_assumed_rung_sits_between_unknown_and_analytic(self):
        rank = propagation.fidelity_rank
        self.assertLess(rank(propagation.UNKNOWN_CONTRIBUTION),
                        rank(propagation.ASSUMED_TRANSMISSION_LINE))
        self.assertLess(rank(propagation.ASSUMED_TRANSMISSION_LINE),
                        rank(propagation.ANALYTIC_TRANSMISSION_LINE))

    def test_the_gate_may_pass_but_carries_the_assumption_visibly(self):
        result = self.fixture.gates(only={"TIMING.INTERCONNECT_DELAY"})[
            "TIMING.INTERCONNECT_DELAY"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertIn(propagation.ASSUMED_TRANSMISSION_LINE,
                      result.measurements["fidelity"])
        row = result.measurements["paths"][0]
        self.assertTrue(row["assumptions"])

    def test_the_same_route_without_the_assumption_still_refuses(self):
        fixture = make(mutate=_split_route, tag="assumedfid_ctl")
        self.assertIsNone(_find(fixture, "over_the_gap")["delay"]["delay_ps"])


# ---------------------------------------------------------------------------
# declared constants do not bypass the reference structure
# ---------------------------------------------------------------------------

class DeclaredConstantsRespectTheReference(unittest.TestCase):
    """A per-layer number characterises an intact structure, not a broken one."""

    def _fixture(self, tag, declared):
        def mutate(document, project):
            _split_route(document, project)
            document["timing"]["propagation"] = {
                "backend": "analytic",
                "model": "declared-effective",
                "via_delay_model": {
                    "model": propagation.VIA_NONE,
                    "justification": "fixture"},
                "declared_layers": declared}
        return make(mutate=mutate, tag=tag)

    def test_a_declared_constant_still_meets_the_gap(self):
        fixture = self._fixture("decl_gap", {
            "F.Cu": {"ps_per_mm": 6.0, "provenance": "fixture value"}})
        record = _find(fixture, "over_the_gap")["delay"]
        self.assertIsNone(record["delay_ps"])
        self.assertTrue(any("geometry is incomplete" in i["issue"]
                            for i in record["insufficient"]), record)

    def test_its_declared_scope_is_honoured(self):
        """The constant assumes In2.Cu, which is continuous here - so the
        In1.Cu gap is not this declaration's problem, on the record."""
        fixture = self._fixture("decl_scope", {
            "F.Cu": {"ps_per_mm": 6.0, "provenance": "fixture value",
                     "reference_layers": ["In2.Cu"]}})
        record = _find(fixture, "over_the_gap")["delay"]
        self.assertAlmostEqual(record["delay_ps"],
                               LENGTH_SPLIT_ROUTE_MM * 6.0, places=3)
        conductor = record["conductors"][0]
        self.assertEqual(conductor["reference_layers_used"], ["In2.Cu"])
        self.assertEqual(conductor["reference_scope"], "declared")

    def test_an_unscoped_declaration_checks_every_candidate_plane(self):
        fixture = self._fixture("decl_unscoped", {
            "F.Cu": {"ps_per_mm": 6.0, "provenance": "fixture value"}})
        record = _find(fixture, "over_the_gap")["delay"]
        problem = next(i for i in record["insufficient"]
                       if i["portion"] == "conductor")
        self.assertEqual(problem["reference_layers_used"],
                         ["In1.Cu", "In2.Cu"])

    def test_a_malformed_scope_is_refused(self):
        with self.assertRaises(propagation.PropagationError):
            propagation.DeclaredLayerModel("F.Cu", {
                "ps_per_mm": 6.0, "provenance": "x",
                "reference_layers": "In2.Cu"})


# ---------------------------------------------------------------------------
# the design measure: per path, and never zero by accident
# ---------------------------------------------------------------------------

class TheDesignMeasureIsPerPath(unittest.TestCase):
    """max_unreferenced_mm limits the worst single endpoint path."""

    def _fixture(self, limit, tag, fill_zones=True):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"mj": {
                "description": "two paths sharing their first run",
                "max_unreferenced_mm": limit,
                "routes": {"paths": [
                    {"id": "past_one", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L13.1"}]},
                    {"id": "past_two", "steps": [
                        {"kind": "copper", "net": "SIG_MJ", "from": "D12.1",
                         "to": "L14.1"}]}]}}}
        fixture = make(mutate=mutate, tag=tag, fill_zones=fill_zones)
        return fixture, fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]

    def test_shared_copper_is_not_accumulated_across_paths(self):
        """Both paths lie wholly off the pour: 12 mm and 20 mm, sharing
        their first 8 mm. The old total said 32; the worst path says 20."""
        _fixture, result = self._fixture(25.0, "design_shared")
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertAlmostEqual(
            result.measurements["worst_path_unreferenced_mm"], 20.0,
            places=2)

    def test_the_limit_names_only_the_offending_path(self):
        _fixture, result = self._fixture(15.0, "design_limit")
        self.assertEqual(result.status, Status.FAIL)
        offenders = [f for f in result.findings if f.get("path")]
        self.assertEqual(len(offenders), 1, result.findings)
        self.assertIn("past_two", offenders[0]["path"])

    def test_unknown_coverage_cannot_satisfy_the_limit(self):
        """Unfilled zones make the exposure unknown, and unknown is not zero."""
        _fixture, result = self._fixture(1000.0, "design_unknown",
                                         fill_zones=False)
        self.assertEqual(result.status, Status.FAIL)
        self.assertTrue(any("unevaluated" in str(f.get("issue", ""))
                            for f in result.findings), result.findings)

    def test_without_a_limit_the_unknown_is_reported_not_failed(self):
        def mutate(document, _project):
            document["timing"]["interfaces"] = {"mj": {
                "description": "x",
                "routes": _route([{"kind": "copper", "net": "SIG_MJ",
                                   "from": "D12.1", "to": "L13.1"}],
                                 "past_one")}}
        fixture = make(mutate=mutate, tag="design_noreq", fill_zones=False)
        result = fixture.gates(only={"TIMING.PATH_INTEGRITY"})[
            "TIMING.PATH_INTEGRITY"]
        self.assertEqual(result.status, Status.PASS, result.reason)
        self.assertIsNone(result.measurements["worst_path_unreferenced_mm"])
        self.assertIn("unreferenced_unknown", result.measurements)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()
