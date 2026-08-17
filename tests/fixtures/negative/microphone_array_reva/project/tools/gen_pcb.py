"""Build microphone_array_v2.kicad_pcb from the shared netlist and placement.

Stackup rationale: both inner layers are solid ground rather than the more
usual signal/GND/power/signal. The whole board draws about 200 mA, so the
supplies are comfortable on 0.6 mm tracks, and giving every F.Cu and B.Cu trace
an unbroken adjacent reference plane matters far more here than power-plane
impedance would. It also removes any chance of a PDM clock or data line
crossing a plane split.

Ground is a plane net that the autorouter never sees, so this script places an
explicit stitching via plus a short connecting track at every ground pad.
"""

import collections
import math
import os
import sys

import pcbnew

import design as d
import netlist as nl

FOOTPRINT_LIBS = {
    "Resistor_SMD": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Resistor_SMD.pretty",
    "Capacitor_SMD": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Capacitor_SMD.pretty",
    "Inductor_SMD": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Inductor_SMD.pretty",
    "Package_TO_SOT_SMD": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Package_TO_SOT_SMD.pretty",
    "Package_SO": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Package_SO.pretty",
    "Diode_SMD": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Diode_SMD.pretty",
    "Fuse": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Fuse.pretty",
    "Connector_PinHeader_2.54mm": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Connector_PinHeader_2.54mm.pretty",
    "Connector_IDC": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\Connector_IDC.pretty",
    "MountingHole": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\MountingHole.pretty",
    "TestPoint": r"C:\Program Files\KiCad\10.0\share\kicad\footprints\TestPoint.pretty",
}

VIA_DIAMETER = 0.45
VIA_DRILL = 0.30
STITCH_CLEARANCE = 0.55
EDGE_INSET = 0.30
PLANE_INSET = 0.50

GROUND_PLANE_LAYERS = (pcbnew.In1_Cu, pcbnew.In2_Cu)


def mm(value):
    return pcbnew.FromMM(float(value))


def vec(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def load_footprint(lib_id, project_root):
    nickname, name = lib_id.split(":", 1)
    path = FOOTPRINT_LIBS.get(nickname)
    if path is None:
        path = os.path.join(project_root, f"{nickname}.pretty")
    footprint = pcbnew.FootprintLoad(path, name)
    if footprint is None:
        raise SystemExit(f"footprint not found: {lib_id} (searched {path})")
    return footprint


# Footprints whose library silkscreen is dropped. Populated from the DRC
# result rather than by guesswork; everything else keeps the library legend so
# the board still matches its footprint library.
SILKSCREEN_STRIP = frozenset()


def strip_silkscreen(footprint):
    """Remove silkscreen graphics from a footprint, keeping copper and fab."""
    doomed = [item for item in footprint.GraphicalItems()
              if item.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS)]
    for item in doomed:
        footprint.Remove(item)


def open_pin_name(ref, pad):
    """Symbol pin name for a deliberately open pin, matching gen_symbols.py."""
    if ref == "U1":
        return "NC"
    if ref == "J1":
        return f"P{pad}_NC"
    labels = d.TANG_J5 if ref == "J2" else d.TANG_J6
    return f"P{pad}_FPGA{labels[int(pad) - 1]}"


def circle_points(radius, count=240, cx=d.PAGE_CX, cy=d.PAGE_CY):
    points = []
    for index in range(count):
        angle = 2.0 * math.pi * index / count
        points.append((cx + radius * math.cos(angle),
                       cy + radius * math.sin(angle)))
    return points


def build(project_root, with_escapes=True):
    """Build the board. `with_escapes=False` omits the pre-routed microphone
    escapes, which is the form handed to an autorouter whose polyline
    normaliser cannot accept them as existing wiring."""
    components, nets = nl.build()
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(4)
    board.SetLayerName(pcbnew.In1_Cu, "GND1")
    board.SetLayerName(pcbnew.In2_Cu, "GND2")

    settings = board.GetDesignSettings()
    settings.SetCopperLayerCount(4)

    # ------------------------------------------------------------------
    # Nets
    # ------------------------------------------------------------------
    net_items = {}
    for name in sorted(nets):
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        net_items[name] = item

    pin_net = {}
    for name, pins in nets.items():
        for ref, pad in pins:
            pin_net[(ref, pad)] = name

    # KiCad gives every deliberately open pin an auto-generated net name in the
    # schematic. Reproducing those names here keeps schematic parity clean
    # instead of leaving 39 net_conflict reports to be waived by hand.
    for ref, pad in nl.expected_unconnected():
        pin_name = open_pin_name(ref, pad)
        name = f"unconnected-({ref}-{pin_name}-Pad{pad})"
        item = pcbnew.NETINFO_ITEM(board, name)
        board.Add(item)
        net_items[name] = item
        pin_net[(ref, pad)] = name

    # ------------------------------------------------------------------
    # Footprints
    # ------------------------------------------------------------------
    placed = {}
    silk_reference = {"J1", "J2", "J3"}
    for component in components:
        ref = component["ref"]
        footprint = load_footprint(component["footprint"], project_root)
        board.Add(footprint)
        # Keep the library nickname so schematic parity can match the symbol's
        # footprint field.
        nickname, name = component["footprint"].split(":", 1)
        footprint.SetFPID(pcbnew.LIB_ID(nickname, name))
        footprint.SetReference(ref)
        footprint.SetValue(component["value"])
        footprint.SetLibDescription(component["description"])
        footprint.SetField("Description", component["description"])
        kx, ky = d.to_kicad(component["x"], component["y"])
        footprint.SetPosition(vec(kx, ky))
        if component["side"] == "bottom":
            footprint.Flip(vec(kx, ky), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
        footprint.SetOrientationDegrees(component["rot"])
        if component["dnp"]:
            footprint.SetDNP(True)
        if not component["in_bom"]:
            footprint.SetExcludedFromBOM(True)

        for field, value in (("LCSC", component["lcsc"]),
                             ("MPN", component["mpn"]),
                             ("Manufacturer", component["manufacturer"])):
            if value:
                footprint.SetField(field, value)

        # Newly created fields default to being visible on the silkscreen, all
        # stacked on the footprint origin. Hide them and move them to the
        # fabrication layer - they are BOM data, not legend.
        for field in footprint.GetFields():
            if field.GetName() not in ("Reference", "Value"):
                field.SetVisible(False)
                field.SetLayer(pcbnew.B_Fab if footprint.IsFlipped()
                               else pcbnew.F_Fab)

        # The board is dense, so silkscreen designators are kept only for the
        # hand-soldered connectors and the test points. Everything else keeps
        # its designator on the fabrication layer, where JLCPCB's assembly data
        # and the CPL still carry it.
        # Two-terminal passives keep no silkscreen outline. At this density
        # their tiny side bars only collide with neighbouring pads, and they
        # carry no orientation information that assembly needs - JLCPCB places
        # from the CPL. Silkscreen is retained where it means something: the
        # microphone pin-1 mark, the diode polarity bar, the IC and oscillator
        # pin-1 marks, and the connectors.
        if ref in SILKSCREEN_STRIP:
            strip_silkscreen(footprint)

        back = footprint.IsFlipped()
        if ref not in silk_reference:
            footprint.Reference().SetLayer(pcbnew.B_Fab if back else pcbnew.F_Fab)
        if ref.startswith("TP"):
            # The full net name stays on the fabrication layer; a short legend
            # is drawn separately so neighbouring labels do not collide.
            footprint.Value().SetLayer(pcbnew.F_Fab)

        for pad in footprint.Pads():
            name = pin_net.get((ref, pad.GetNumber()))
            if name:
                pad.SetNet(net_items[name])
        placed[ref] = footprint

    # ------------------------------------------------------------------
    # Board outline and ground planes
    # ------------------------------------------------------------------
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_CIRCLE)
    outline.SetStart(vec(d.PAGE_CX, d.PAGE_CY))
    outline.SetEnd(vec(d.PAGE_CX + d.BOARD_RADIUS, d.PAGE_CY))
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetWidth(mm(0.1))
    board.Add(outline)

    for layer in GROUND_PLANE_LAYERS:
        zone = pcbnew.ZONE(board)
        zone.SetLayer(layer)
        zone.SetNet(net_items["GND"])
        zone.SetIsFilled(False)
        zone.SetLocalClearance(mm(0.25))
        zone.SetMinThickness(mm(0.2))
        zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
        polygon = zone.Outline()
        polygon.NewOutline()
        for x, y in circle_points(d.BOARD_RADIUS - PLANE_INSET):
            polygon.Append(mm(x), mm(y))
        board.Add(zone)

    reset_short_guard(board)
    strap_count = link_microphone_straps(board, placed, pin_net, net_items)
    if with_escapes:
        strap_count += route_microphone_escapes(board, placed, pin_net, net_items)
        strap_count += route_channel_local(board, placed, pin_net, net_items)
        strap_count += route_supply_ring(board, placed, pin_net, net_items)
        # Power before the clocks. The regulator's feed out to the supply ring
        # has one workable route in each direction and the clock branches have
        # hundreds, so whichever is laid down first keeps its route - and it
        # should be the one with nowhere else to go.
        strap_count += route_supply_ring_feed(board, placed, pin_net, net_items)
        strap_count += route_power_block(board, placed, pin_net, net_items)
        strap_count += route_clock_branches(board, placed, pin_net, net_items)
        strap_count += route_data_spokes(board, placed, pin_net, net_items)
        # route_host_block() is written but not yet called. The stack order is
        # now right - connector, ESD, resistors, socket, all flowing inward -
        # but routing *through* a specific ESD pin still needs a proper escape:
        # each array has two signal pins per side in one column, so a track
        # cannot simply pass both. See docs/status.md.
    else:
        strap_count += reserve_microphone_corridors(board, placed, pin_net,
                                                    net_items)
    stitch_count = place_ground_stitching(board, placed, pin_net, net_items)
    add_silkscreen(board)

    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())

    path = os.path.join(project_root, "microphone_array_v2.kicad_pcb")
    board.Save(path)
    apply_project_settings(project_root)
    return path, len(components), len(nets), stitch_count, strap_count


# The MSM261DHP006 land pattern encloses its four signal pads in a ground ring
# with only a 0.40 mm straight gap, so nothing escapes in a straight line. The
# open diagonal corner between the side bar and the end bar is 0.566 mm wide,
# which fits a 0.15 mm track with 0.15 mm clearance and nothing wider. The
# three classes that reach a microphone pad are sized for that escape; they
# carry a few milliamps at most, so the narrower track costs nothing.
NET_CLASSES = [
    # name, track width, clearance, via diameter, via drill
    ("Default", 0.20, 0.20, 0.45, 0.30),
    # 0.15 mm clearance because the microphone escape has to pass a GND pad on
    # both sides of a 0.566 mm diagonal channel; the ground zones keep their
    # own 0.25 mm local clearance for the fill.
    ("PLANE", 0.60, 0.15, 0.60, 0.35),
    ("POWER", 0.60, 0.25, 0.60, 0.35),
    ("MIC_SUPPLY", 0.15, 0.15, 0.45, 0.30),
    # MIC_DOUT is split out of PDM_DATA so the finished per-channel routing can
    # be handed to the autorouter as an ignored class while PDM_D stays live.
    ("MIC_DATA", 0.15, 0.15, 0.45, 0.30),
    ("MANUAL_CRITICAL", 0.15, 0.15, 0.45, 0.30),
    ("PDM_DATA", 0.15, 0.15, 0.45, 0.30),
    ("HOST", 0.20, 0.20, 0.45, 0.30),
]

NET_CLASS_PATTERNS = [
    ("PLANE", "GND"),
    ("POWER", "+5V"), ("POWER", "+3V3A"), ("POWER", "+3V3_CLK"),
    ("POWER", "PI_5V"), ("POWER", "5V_FUSED"), ("POWER", "TANG_3V3"),
    ("MIC_SUPPLY", "MIC_VDD_*"),
    ("MANUAL_CRITICAL", "AUDIO_MCLK"), ("MANUAL_CRITICAL", "MCLK_OSC"),
    ("MANUAL_CRITICAL", "PDM_CLK_*"),
    ("PDM_DATA", "PDM_D?"), ("MIC_DATA", "MIC_DOUT_*"),
    # Listed explicitly rather than as "PI_*", which would also capture PI_5V
    # and put that net in two classes at once.
    ("HOST", "SPI_*"), ("HOST", "HOST_*"),
    ("HOST", "PI_SCLK"), ("HOST", "PI_MOSI"), ("HOST", "PI_MISO"),
    ("HOST", "PI_CS_N"), ("HOST", "PI_IRQ"), ("HOST", "PI_SYNC"),
    ("HOST", "PI_RESET_N"), ("HOST", "PI_STATUS"),
]

# Conservative JLCPCB standard four-layer profile.
BOARD_RULES = {
    "max_error": 0.005,
    "min_clearance": 0.127,
    "min_connection": 0.127,
    "min_copper_edge_clearance": 0.30,
    "min_hole_clearance": 0.25,
    "min_hole_to_hole": 0.25,
    "min_microvia_diameter": 0.20,
    "min_microvia_drill": 0.10,
    "min_resolved_spokes": 2,
    "min_silk_clearance": 0.15,
    "min_text_height": 1.0,
    "min_text_thickness": 0.15,
    "min_through_hole_diameter": 0.30,
    "min_track_width": 0.127,
    "min_via_annular_width": 0.075,
    "min_via_diameter": 0.45,
    "solder_mask_to_copper_clearance": 0.0,
    "use_height_for_length_calcs": True,
}


def apply_project_settings(project_root):
    """Re-apply design rules and net classes after pcbnew rewrites the project.

    Saving a board from the Python API resets the sibling .kicad_pro to
    defaults, so the curated rules are written back here rather than being
    maintained by hand.
    """
    import json

    path = os.path.join(project_root, "microphone_array_v2.kicad_pro")
    with open(path, "r", encoding="utf-8") as handle:
        project = json.load(handle)

    design = project.setdefault("board", {}).setdefault("design_settings", {})
    design.setdefault("rules", {}).update(BOARD_RULES)
    design["track_widths"] = [0.0, 0.127, 0.20, 0.25, 0.30, 0.60, 1.00]
    design["via_dimensions"] = [
        {"diameter": 0.0, "drill": 0.0},
        {"diameter": 0.45, "drill": 0.30},
        {"diameter": 0.60, "drill": 0.35},
    ]

    classes = []
    for name, track, clearance, via_d, via_drill in NET_CLASSES:
        classes.append({
            "bus_width": 12, "clearance": clearance,
            "diff_pair_gap": 0.25, "diff_pair_width": 0.2,
            "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
            "name": name,
            "pcb_color": "rgba(0, 0, 0, 0.000)",
            "schematic_color": "rgba(0, 0, 0, 0.000)",
            "track_width": track, "via_diameter": via_d, "via_drill": via_drill,
            "wire_width": 6,
        })

    project["net_settings"] = {
        "classes": classes,
        "meta": {"version": 4},
        "net_colors": None,
        "netclass_assignments": None,
        "netclass_patterns": [{"netclass": cls, "pattern": pattern}
                              for cls, pattern in NET_CLASS_PATTERNS],
    }

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(project, handle, indent=2)
        handle.write("\n")


def choose_stitch_position(pad, footprint, position, obstacles, vias_placed):
    """Pick a clear spot just outside a ground pad for its stitching via.

    A simple "straight out from the footprint centre" rule fails on fine-pitch
    packages, where the diagonal runs straight across a neighbouring pad. So
    candidate directions are scored and the roomiest clear one wins.
    """
    size = pad.GetSize()
    centre = footprint.GetPosition()
    via_radius = mm(VIA_DIAMETER / 2.0)
    needed = mm(STITCH_CLEARANCE) + via_radius

    # Leave along one of the pad's own axes, never on a diagonal. A diagonal
    # runs across the neighbouring pin on a fine-pitch package, and on the
    # microphone it lands in the corner channel that the signal pads escape
    # through. Among the axes pointing away from the footprint centre, take
    # the one that clears the rest of this footprint by the widest margin.
    base_x = position.x - centre.x
    base_y = position.y - centre.y
    if base_x == 0 and base_y == 0:
        base_x, base_y = 0, 1
    norm = math.hypot(base_x, base_y)
    outward = (base_x / norm, base_y / norm)

    siblings = [p for p in footprint.Pads() if p.GetPosition() != position]
    pad_angle = math.radians(pad.GetOrientationDegrees())
    candidates = []
    for quarter in range(4):
        angle = pad_angle + quarter * math.pi / 2.0
        ux, uy = math.cos(angle), math.sin(angle)
        if ux * outward[0] + uy * outward[1] <= 0.1:
            continue
        reach = abs(ux) * size.x / 2.0 + abs(uy) * size.y / 2.0
        probe_x = position.x + ux * (reach + needed)
        probe_y = position.y + uy * (reach + needed)
        room = min((math.hypot(probe_x - p.GetPosition().x,
                               probe_y - p.GetPosition().y)
                    - math.hypot(p.GetSize().x, p.GetSize().y) / 2.0
                    for p in siblings), default=math.inf)
        candidates.append((-room, ux, uy))
    if not candidates:
        candidates = [(0.0, outward[0], outward[1])]
    candidates.sort()

    for _rank, ux, uy in candidates:
        reach = abs(ux) * size.x / 2.0 + abs(uy) * size.y / 2.0
        for extra in (0.0, mm(0.25), mm(0.5)):
            distance = reach + needed + extra
            candidate = pcbnew.VECTOR2I(int(position.x + ux * distance),
                                        int(position.y + uy * distance))
            clearance = math.inf
            for other_position, other_radius, other_net in obstacles:
                if other_position == position:
                    continue
                gap = (math.hypot(candidate.x - other_position.x,
                                  candidate.y - other_position.y)
                       - other_radius - via_radius)
                if other_net == "GND":
                    gap += mm(0.5)  # same-net crowding matters less
                clearance = min(clearance, gap)
            for other in vias_placed:
                gap = (math.hypot(candidate.x - other.x, candidate.y - other.y)
                       - 2 * via_radius - mm(0.25))
                clearance = min(clearance, gap)
            if clearance >= mm(0.2):
                return candidate
    return None


def local_to_board(footprint, lx, ly):
    """Map footprint-local millimetres to a board position."""
    angle = math.radians(footprint.GetOrientationDegrees())
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    dx = lx * cos_a + ly * sin_a
    dy = -lx * sin_a + ly * cos_a
    origin = footprint.GetPosition()
    return pcbnew.VECTOR2I(int(origin.x + mm(dx)), int(origin.y + mm(dy)))


def board_to_local(footprint, position):
    """Inverse of local_to_board, in millimetres."""
    angle = math.radians(footprint.GetOrientationDegrees())
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    origin = footprint.GetPosition()
    dx = (position.x - origin.x) / 1e6
    dy = (position.y - origin.y) / 1e6
    return (dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a)


# Escape paths in microphone-footprint-local millimetres. Local +Y points
# radially inward, so every path ends inward where the channel's own resistors
# and capacitor sit. The diagonal waypoints at (+/-0.85, +/-1.35) are the exact
# midpoints of the 0.566 mm corner gaps in the ground ring, which is the only
# way out of the ring; the clock path additionally runs up the outside of the
# left-hand ground bar because its pad is on the outward half of the package.
MIC_ESCAPES = {
    # supply and data pads are already on the inward half, so each leaves
    # straight through its own diagonal corner
    "1": [(0.45, 0.675), (0.45, 0.95), (0.85, 1.35), (1.20, 1.70), (1.20, 3.40)],
    "4": [(-0.45, 0.675), (-0.45, 0.95), (-0.85, 1.35), (-1.20, 1.70),
          (-1.20, 3.40)],
    # the clock pad is on the outward half, so it leaves through the outward
    # corner and then runs up the outside of the left ground bar
    # stops short of the others: the channel's decoupling capacitor sits at
    # local y ~ 4.0 on this side, so the handover via has to stay inboard of it
    "3": [(-0.45, -0.675), (-0.45, -0.95), (-0.85, -1.35), (-1.20, -1.70),
          (-2.15, -1.70), (-2.45, -1.40), (-2.45, 2.60)],
}

# Corridor reserved for the escapes above, in footprint-local millimetres.
# A keepout of this shape is placed for the autorouter and removed again once
# the escapes are drawn.
MIC_CORRIDOR = (-2.95, -2.75, 1.75, 3.15)  # left, top, right, bottom

# Ground stitching vias for the microphone are placed explicitly rather than by
# search, so that the short track from each ground bar to its via never crosses
# one of the escape corridors above.
MIC_GND_VIAS = {
    "5": (0.0, 2.85),
    "6": (1.95, 0.0),
    "7": (0.0, -2.85),
    "8": (-1.95, 0.0),
}


def path_45(start, end):
    """Two-segment path from start to end using only 45 degree geometry.

    Travels diagonally for the shorter axis, then straight along the longer
    one, so the single corner is always a 45 degree turn.
    """
    dx = end.x - start.x
    dy = end.y - start.y
    if dx == 0 or dy == 0 or abs(dx) == abs(dy):
        return [start, end]
    step = min(abs(dx), abs(dy))
    knee = pcbnew.VECTOR2I(
        int(start.x + step * (1 if dx > 0 else -1)),
        int(start.y + step * (1 if dy > 0 else -1)))
    return [start, knee, end]


# --------------------------------------------------------------------------
# Short guard
#
# Every piece of copper is checked against what is already on the board before
# it is committed. A path that would cross another net on its own layer, or run
# over another net's pad, is discarded whole and the connection is left
# unrouted. An open connection is honest and shows up in the ratsnest; a short
# is a defect that has to be found later. Routing is emitted in priority order,
# so the constrained nets claim their space first.
# --------------------------------------------------------------------------

TRACK_CLEARANCE = 0.15   # the board's minimum copper-to-copper clearance

_PLACED = []      # committed track segments
_PLACED_VIAS = []  # committed via positions
_PADS = []        # (pad, net name) for every pad on the board
REFUSED = []      # (net, layer, reason) for everything turned away
_TRACE_DETAIL = collections.Counter()


def reset_short_guard(board):
    del _PLACED[:], _PLACED_VIAS[:], _PADS[:], REFUSED[:]
    for footprint in board.Footprints():
        for pad in footprint.Pads():
            position = pad.GetPosition()
            size = pad.GetSize()
            reach = math.hypot(size.x, size.y) / 2.0
            _PADS.append((pad, pad.GetNetname(),
                          position.x, position.y, reach))


def _side(ax, ay, bx, by, cx, cy):
    value = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return 0 if abs(value) < 1.0 else (1 if value > 0 else -1)


def _segments_cross(a1, a2, b1, b2):
    return (_side(*b1, *b2, *a1) * _side(*b1, *b2, *a2) < 0
            and _side(*a1, *a2, *b1) * _side(*a1, *a2, *b2) < 0)


def _segment_distance(a1, a2, b1, b2):
    """Closest approach between two segments, zero if they cross."""
    if _segments_cross(a1, a2, b1, b2):
        return 0.0
    return min(_point_near_segment(a1[0], a1[1], b1, b2),
               _point_near_segment(a2[0], a2[1], b1, b2),
               _point_near_segment(b1[0], b1[1], a1, a2),
               _point_near_segment(b2[0], b2[1], a1, a2))


def _segment_hits_pad(pad, start, end, half_width):
    """True if a track centre-line runs closer to a pad than the rules allow.

    The distance is measured to the pad's real outline. Inflating the pad's
    bounding box by the track width instead over-states the diagonal corners by
    a factor of root two, which turned away several perfectly legal 45-degree
    runs past the module socket pins.
    """
    limit = half_width + mm(TRACK_CLEARANCE)
    position = pad.GetPosition()
    size = pad.GetSize()

    if pad.GetShape() == pcbnew.PAD_SHAPE_CIRCLE:
        return (_point_near_segment(position.x, position.y, start, end)
                < size.x / 2.0 + limit)

    angle = math.radians(pad.GetOrientation().AsDegrees())
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    hx, hy = size.x / 2.0, size.y / 2.0
    local = []
    for point in (start, end):
        dx = point[0] - position.x
        dy = point[1] - position.y
        local.append((dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a))
    (x1, y1), (x2, y2) = local

    for x, y in local:                       # endpoint inside the pad
        if -hx <= x <= hx and -hy <= y <= hy:
            return True
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    return any(_segment_distance((x1, y1), (x2, y2),
                                 corners[i], corners[(i + 1) % 4]) < limit
               for i in range(4))


def path_conflict(net_name, layer, width, points):
    """Why this path may not be placed, or None if it is clear."""
    half = width / 2.0
    # Two tracks of the same width, so the centre lines must stay a full width
    # plus the clearance apart. Checking clearance here and not just crossings
    # means the generator settles a conflict by taking another route rather
    # than by handing a violation to DRC.
    clearance = mm(TRACK_CLEARANCE)
    for a, b in zip(points, points[1:]):
        start, end = (a.x, a.y), (b.x, b.y)
        if start == end:
            continue
        # Cheap bounding box for the segment, so the great majority of the
        # board's copper is dismissed with four comparisons instead of a
        # segment-to-segment distance.
        lo_x, hi_x = min(start[0], end[0]), max(start[0], end[0])
        lo_y, hi_y = min(start[1], end[1]), max(start[1], end[1])
        where = f" near {(start[0] / 1e6 - d.PAGE_CX):.1f},"\
                f"{-(start[1] / 1e6 - d.PAGE_CY):.1f}"
        for other in _PLACED:
            if other["net"] == net_name or other["layer"] != layer:
                continue
            gap = half + other["half"] + clearance
            if (other["lo_x"] - gap > hi_x or other["hi_x"] + gap < lo_x
                    or other["lo_y"] - gap > hi_y or other["hi_y"] + gap < lo_y):
                continue
            if _segment_distance(start, end, other["a"], other["b"]) < gap:
                return f"crosses {other['net']}{where}"
        for pad, pad_net, px, py, reach in _PADS:
            if pad_net == net_name or not pad.IsOnLayer(layer):
                continue
            span = reach + half + clearance
            if (px - span > hi_x or px + span < lo_x
                    or py - span > hi_y or py + span < lo_y):
                continue
            if _segment_hits_pad(pad, start, end, half):
                ref = pad.GetParentFootprint().GetReference()
                return (f"runs over {ref}.{pad.GetNumber()}"
                        f" ({pad_net or 'no net'}){where}")
        for via_net, vx, vy, radius in _PLACED_VIAS:
            if via_net == net_name:
                continue
            if _point_near_segment(vx, vy, start, end) < radius + half + clearance:
                return f"runs over a {via_net} via{where}"
    return None


def _point_near_segment(px, py, start, end):
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def add_via(board, net, position, offset_mm=0.0):
    """Place a tented through via on `net`, optionally pushed radially inward."""
    target = position
    if offset_mm:
        centre_x, centre_y = mm(d.PAGE_CX), mm(d.PAGE_CY)
        dx, dy = centre_x - position.x, centre_y - position.y
        length = math.hypot(dx, dy) or 1.0
        step = mm(offset_mm) / length
        target = pcbnew.VECTOR2I(int(position.x + dx * step),
                                 int(position.y + dy * step))
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(target)
    via.SetWidth(mm(VIA_DIAMETER))
    via.SetDrill(mm(VIA_DRILL))
    via.SetNet(net)
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    via.SetFrontTentingMode(pcbnew.TENTING_MODE_TENTED)
    via.SetBackTentingMode(pcbnew.TENTING_MODE_TENTED)
    board.Add(via)
    _PLACED_VIAS.append((net.GetNetname(), target.x, target.y,
                         mm(VIA_DIAMETER / 2.0)))
    return target


def board_xy(point):
    """Board coordinates, centre origin and y upwards, from a KiCad point."""
    return point.x / 1e6 - d.PAGE_CX, -(point.y / 1e6 - d.PAGE_CY)


def dedupe(points):
    return [p for i, p in enumerate(points)
            if i == 0 or (p.x, p.y) != (points[i - 1].x, points[i - 1].y)]


def polyline_45(waypoints):
    """Expand a waypoint list into one 45-degree polyline."""
    points = [waypoints[0]]
    for a, b in zip(waypoints, waypoints[1:]):
        points.extend(path_45(a, b)[1:])
    return dedupe(points)


def thread_path(waypoints, bias=0):
    """Like polyline_45, but stepping through a clear gap wherever the run has
    to cross one of the through-hole pin rows.

    The gaps have to be found on the 45-degree path, not on the straight line
    between the waypoints. Those two cross the pin row in different places -
    often several millimetres apart - so threading the straight line and then
    bending the result to 45 degrees walks the track straight into the pin it
    was supposed to avoid.
    """
    points = [waypoints[0]]
    for _a, b in zip(waypoints, waypoints[1:]):
        points.extend(_thread_leg(points[-1], b, bias)[1:])
    return dedupe(points)


def _thread_leg(start, end, bias, depth=0):
    """One 45-degree leg, stepped through any pin row it has to cross.

    The gap is chosen per straight segment of the expanded path rather than
    once for the leg as a whole, because a single segment *is* the straight
    line thread_socket_rows assumes, so the crossing point it computes is the
    real one. The two halves either side of the gap are then threaded in turn -
    neither can meet the same row again, since both of their ends sit on the
    same side of it.
    """
    points = path_45(start, end)
    if depth >= 4:
        return points
    for a, b in zip(points, points[1:]):
        detour = thread_socket_rows(a, b, bias)
        if not detour:
            continue
        # Rebuild the whole leg through the gap rather than splicing the
        # detour into the middle of it. Keeping the earlier segments means
        # keeping the one that already carried the track into the row's
        # clearance band, and the track then doubles back to the gap right
        # across the pin it is dodging.
        rebuilt = [start]
        for pair in range(0, len(detour) - 1, 2):
            near, far = detour[pair], detour[pair + 1]
            rebuilt.extend(_thread_leg(rebuilt[-1], near, bias, depth + 1)[1:])
            rebuilt.append(far)          # straight across the row, in the gap
        rebuilt.extend(_thread_leg(rebuilt[-1], end, bias, depth + 1)[1:])
        return dedupe(rebuilt)
    return points


def try_paths(board, net, layer, width_mm, alternatives, locked=True):
    """Commit the first alternative whose every polyline is clear.

    Routing one net is a search, not a formula: when the preferred path is
    blocked the router should take its second choice rather than either
    shorting or giving up. An alternative is accepted or rejected whole - a
    clock branch is trunk plus two matched arms, and committing the trunk then
    refusing an arm would leave a stub and destroy the length matching that is
    the point of the tree.
    """
    blockers = collections.Counter()
    _TRACE_DETAIL.clear()
    first = None
    for paths in alternatives:
        cleaned = [p for p in (dedupe(list(path)) for path in paths)
                   if len(p) >= 2]
        if not cleaned:
            continue
        blocked = None
        for points in cleaned:
            blocked = path_conflict(net.GetNetname(), layer, mm(width_mm), points)
            if blocked is not None:
                break
        if blocked is not None:
            blockers[blocked.split(" near ")[0]] += 1
            if os.environ.get("TRACE_NET") == net.GetNetname():
                _TRACE_DETAIL[blocked] += 1
            if first is None:
                first = blocked
                if os.environ.get("TRACE_NET") == net.GetNetname():
                    for points in cleaned:
                        print("   trace", " ".join(
                            f"{p.x / 1e6 - d.PAGE_CX:.2f},"
                            f"{-(p.y / 1e6 - d.PAGE_CY):.2f}" for p in points))
                    print("   blocked:", blocked)
            continue
        return sum(add_track(board, net, layer, width_mm, points, locked,
                             guard=False)
                   for points in cleaned)
    if os.environ.get("TRACE_NET") == net.GetNetname():
        for reason, hits in _TRACE_DETAIL.most_common(40):
            print(f"   {hits:4d} x {reason}")
    if blockers:
        # The obstacle that turned away the most alternatives is the one worth
        # reporting: the first alternative's blocker is often incidental, and
        # the last one tried is the most desperate route of all.
        REFUSED.append((net.GetNetname(), board.GetLayerName(layer),
                        f"{blockers.most_common(1)[0][0]}"
                        f"; first choice {first}"))
    return 0


def add_track(board, net, layer, width_mm, points, locked=True, guard=True):
    """Lay a polyline of tracks, unless doing so would short another net.

    The whole path is checked before any of it is committed: half a path is a
    dangling stub that connects nothing and hides the problem.
    """
    points = dedupe(points)
    if len(points) < 2:
        return 0

    name = net.GetNetname()
    if guard:
        reason = path_conflict(name, layer, mm(width_mm), points)
        if reason is not None:
            REFUSED.append((name, board.GetLayerName(layer), reason))
            return 0

    count = 0
    for a, b in zip(points, points[1:]):
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(a)
        track.SetEnd(b)
        track.SetWidth(mm(width_mm))
        track.SetLayer(layer)
        track.SetNet(net)
        track.SetLocked(locked)
        board.Add(track)
        _PLACED.append({"net": name, "layer": layer, "half": mm(width_mm) / 2.0,
                        "a": (a.x, a.y), "b": (b.x, b.y),
                        "lo_x": min(a.x, b.x), "hi_x": max(a.x, b.x),
                        "lo_y": min(a.y, b.y), "hi_y": max(a.y, b.y)})
        count += 1
    return count


def route_microphone_escapes(board, placed, pin_net, net_items):
    """Pre-route each microphone's three signal pads out of its ground ring.

    An autorouter that approximates pad outlines with bounding octagons closes
    the narrow diagonal corners and leaves these nets unrouted. The geometry is
    identical for all sixteen channels, so it is laid down deterministically
    here and locked, and the autorouter only has to pick the paths up outside
    the package.
    """
    count = 0
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        footprint = placed[ref]
        pads = {pad.GetNumber(): pad for pad in footprint.Pads()}

        for number, path in MIC_ESCAPES.items():
            net_name = pin_net[(ref, number)]
            # Confirm the local frame really lands on the pad before drawing.
            start = local_to_board(footprint, *path[0])
            actual = pads[number].GetPosition()
            if abs(start.x - actual.x) > 2000 or abs(start.y - actual.y) > 2000:
                raise SystemExit(
                    f"{ref} pad {number}: local frame mismatch, "
                    f"computed {start.x},{start.y} actual {actual.x},{actual.y}")

            points = [actual] + [local_to_board(footprint, x, y)
                                 for x, y in path[1:]]
            count += add_track(board, net_items[net_name], pcbnew.F_Cu, 0.15,
                               points)
    return count


# Per-channel routing, in microphone-local millimetres. Local +Y is inward.
# Every path is 45 degree geometry and stays on the tangential side its escape
# leaves from, so the supply, data and clock nets of a channel never cross.
CHANNEL_ROUTES = [
    # MIC_VDD: supply escape -> decoupling capacitor -> isolation resistor
    ("1", [(1.20, 3.40), (1.79, 3.99), (2.21, 3.99)]),
    ("1", [(2.21, 3.99), (2.42, 4.20), (5.27, 4.20)]),
    # MIC_DOUT: data escape -> damping resistor, entering on its outward pad
    ("4", [(-1.20, 3.40), (0.00, 4.60), (0.00, 7.71)]),
]


def route_channel_local(board, placed, pin_net, net_items):
    """Route each channel's supply and data nets within its own cluster."""
    count = 0
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        footprint = placed[ref]
        for number, path in CHANNEL_ROUTES:
            net = net_items[pin_net[(ref, number)]]
            points = [local_to_board(footprint, x, y) for x, y in path]
            count += add_track(board, net, pcbnew.F_Cu, 0.15, points)
    return count


# The ring sits in the one clear annulus: outside the module socket pins and
# the host block, inside the channels' own data resistors.
SUPPLY_RING_RADIUS = 43.3
SUPPLY_RING_WIDTH = 0.6
SUPPLY_STUB_WIDTH = 0.5
SUPPLY_RING_MAX_STEP_DEG = 11.25  # keeps chord sag off the host block


def route_supply_ring(board, placed, pin_net, net_items):
    """Distribute +3V3A to the ring as a polygon on B.Cu.

    A ring necessarily crosses every radial spoke, so it goes on the bottom
    layer where the spokes are not: the clock and data runs stay on F.Cu, and
    the two only meet at vias. Vertices sit exactly at the connection angles,
    so every turn is well under 45 degrees and each consumer is a short radial
    stub up to the top layer.
    """
    net = net_items["+3V3A"]
    centre_x, centre_y = mm(d.PAGE_CX), mm(d.PAGE_CY)

    consumers = []
    for k in range(d.MIC_COUNT):
        consumers.append((f"RV{k + 1}", "1"))
    for i in range(4):
        consumers.append((f"CB{i + 1}", "1"))

    nodes = []
    for ref, pad_number in consumers:
        footprint = placed[ref]
        pad = next(p for p in footprint.Pads() if p.GetNumber() == pad_number)
        position = pad.GetPosition()
        angle = math.atan2(position.y - centre_y, position.x - centre_x)
        nodes.append((angle % (2 * math.pi), position))
    nodes.sort()

    count = 0
    ring_points = []
    for angle, pad_position in nodes:
        vertex = pcbnew.VECTOR2I(
            int(centre_x + mm(SUPPLY_RING_RADIUS) * math.cos(angle)),
            int(centre_y + mm(SUPPLY_RING_RADIUS) * math.sin(angle)))
        ring_points.append(vertex)
        add_via(board, net, vertex)
        count += add_track(board, net, pcbnew.F_Cu, SUPPLY_STUB_WIDTH,
                           [vertex, pad_position])

    # Subdivide long spans so the chords do not sag inward onto the host block.
    angles = [angle for angle, _position in nodes]
    step = math.radians(SUPPLY_RING_MAX_STEP_DEG)
    path = []
    for i, angle in enumerate(angles):
        nxt = angles[(i + 1) % len(angles)]
        span = (nxt - angle) % (2 * math.pi)
        divisions = max(1, int(math.ceil(span / step)))
        for j in range(divisions):
            a = angle + span * j / divisions
            path.append(pcbnew.VECTOR2I(
                int(centre_x + mm(SUPPLY_RING_RADIUS) * math.cos(a)),
                int(centre_y + mm(SUPPLY_RING_RADIUS) * math.sin(a))))
    for a, b in zip(path, path[1:] + path[:1]):
        count += add_track(board, net, pcbnew.B_Cu, SUPPLY_RING_WIDTH, [a, b])
    return count


SPOKE_HANDOVER_RADIUS = 41.5
CLOCK_WIDTH = 0.25

# Radius of the tangential ring the clock branches fan out along, tried in
# order. Every value is inside SPOKE_HANDOVER_RADIUS on purpose: the data
# spokes are on F.Cu only *outside* that radius, so a clock ring underneath it
# cannot meet a data spoke on the top layer at all. Earlier the two shared
# 41.5 mm and the tangential clock run sliced straight through the radial data
# legs. The alternatives let a branch step inward when its first choice is
# already taken, and they are kept close together so the eight branches stay
# length-matched.
#
# All of them also stay clear of R = 38 mm, where the four M3 mounting holes
# sit: two of those holes are at 56.25 and 326.25 degrees, which are branch
# bisectors, so a ring at 37-39.5 mm put branch 1 and branch 7 straight through
# a screw hole. And they stay outside the R = 32 mm test point ring.
CLOCK_RING_RADII = (34.5, 35.5, 30.0, 27.5)


def polar_point(radius, degrees):
    """Board position at a polar coordinate, in KiCad units."""
    x, y = d.polar(radius, degrees)
    kx, ky = d.to_kicad(x, y)
    return vec(kx, ky)


def board_angle(position):
    """Azimuth of a board position in design degrees, counter-clockwise."""
    dx = position.x / 1e6 - d.PAGE_CX
    dy = -(position.y / 1e6 - d.PAGE_CY)
    return math.degrees(math.atan2(dy, dx)) % 360.0


def board_radius(position):
    return math.hypot(position.x / 1e6 - d.PAGE_CX,
                      position.y / 1e6 - d.PAGE_CY)


SOCKET_ROW_CLEARANCE = 2.2


def pin_rows():
    """Every through-hole pin row that a track may have to cross.

    Both module sockets and both rows of the Pi header. Each entry is
    (row y, list of pin x positions) in design millimetres.
    """
    socket_x = [d.tang_socket_x(n) for n in range(1, d.TANG_PINS + 1)]
    header_x = [d.PI_HEADER_POS[0] + 15.24 - 2.54 * k for k in range(13)]
    half = d.TANG_ROW_SPACING / 2.0
    header_low = d.PI_HEADER_POS[1] + 1.27
    # (low y, high y, pin x positions). The Pi header is one band, not two
    # rows: its rows are 2.54 mm apart, closer than the clearance a crossing
    # needs on either side, so a track that stepped over them one at a time
    # zigzagged in and out of the gap between them.
    return [
        (half, half, socket_x),
        (-half, -half, socket_x),
        (header_low, header_low + d.TANG_PITCH, header_x),
    ]


def thread_socket_rows(start, end, bias=0):
    """Waypoints for a run that has to cross a row of through-hole pins.

    Those rows are solid walls of pads. A straight run into one lands on a pin,
    so each crossing is snapped to the midpoint between two adjacent pins,
    where 2.54 mm pitch on 1.0 mm drills leaves 1.54 mm of clear space - ample
    for a 0.25 mm track at 0.25 mm hole clearance.

    `bias` picks the n-th nearest gap instead of the nearest, so a caller can
    offer the router alternative crossings when the obvious one is taken.
    """
    ax, ay = start.x / 1e6 - d.PAGE_CX, -(start.y / 1e6 - d.PAGE_CY)
    bx, by = end.x / 1e6 - d.PAGE_CX, -(end.y / 1e6 - d.PAGE_CY)
    if by == ay:
        return []

    points = []
    for low, high, pins in pin_rows():
        middle = (low + high) / 2.0
        if (ay - middle) * (by - middle) >= 0:
            continue
        t = (middle - ay) / (by - ay)
        cross_x = ax + t * (bx - ax)
        if not (min(pins) - 1.0 <= cross_x <= max(pins) + 1.0):
            continue  # already passes beyond the end of the row
        gaps = sorted((x - d.TANG_PITCH / 2.0 for x in pins),
                      key=lambda g: abs(g - cross_x))
        gap = gaps[min(bias, len(gaps) - 1)]
        above = high + SOCKET_ROW_CLEARANCE
        below = low - SOCKET_ROW_CLEARANCE
        near, far = (above, below) if ay > middle else (below, above)
        points.append((t, (gap, near)))
        points.append((t, (gap, far)))

    points.sort(key=lambda item: item[0])
    return [vec(*d.to_kicad(x, y)) for _t, (x, y) in points]


# Lateral corridor used to get past the Pi header, which together with the ESD
# parts and the series resistors blocks the whole lower centre of the board.
HOST_DETOUR_X = 26.0
HOST_DETOUR_Y = -43.0
HOST_BLOCK_HALF_WIDTH = 22.0
HOST_BLOCK_TOP_Y = -25.0


def host_block_detour(start, end):
    """Waypoints taking a bottom-layer run around the Pi header block."""
    ex = end.x / 1e6 - d.PAGE_CX
    ey = -(end.y / 1e6 - d.PAGE_CY)
    if ey > HOST_BLOCK_TOP_Y or abs(ex) > HOST_BLOCK_HALF_WIDTH:
        return []
    side = HOST_DETOUR_X if ex >= 0 else -HOST_DETOUR_X
    return [vec(*d.to_kicad(side, -8.0)),
            vec(*d.to_kicad(side, HOST_DETOUR_Y)),
            vec(*d.to_kicad(ex, HOST_DETOUR_Y))]


def order_waypoints(start, end, points):
    """Keep inserted waypoints in travel order and drop duplicates."""
    ordered, seen = [], set()
    for point in points:
        key = (point.x, point.y)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(point)
    return ordered


def pad_axis_via(board, net, footprint, from_pad, to_pad, distance_mm=1.6):
    """Place a via just beyond `to_pad`, along the part's own pad axis.

    Offsetting towards the board centre instead drops the via onto the
    component's other pad on half the placements.
    """
    pads = {p.GetNumber(): p.GetPosition() for p in footprint.Pads()}
    a, b = pads[from_pad], pads[to_pad]
    dx, dy = b.x - a.x, b.y - a.y
    length = math.hypot(dx, dy) or 1.0
    target = pcbnew.VECTOR2I(int(b.x + dx / length * mm(distance_mm)),
                             int(b.y + dy / length * mm(distance_mm)))
    return add_via(board, net, target)


def route_clock_branches(board, placed, pin_net, net_items):
    """Route each PDM clock branch as a symmetric tree to its two channels.

    The branch leaves its series resistor, drops to B.Cu for the run out
    through the module-socket area, and comes back up at a split point placed
    on the bisector of its two landing angles. The two arms from there are
    mirror images, so the pair is length-matched by construction rather than by
    tuning - which is the thing an autorouter could not give us.
    """
    count = 0
    for branch in range(8):
        net = net_items[f"PDM_CLK_B{branch}"]
        resistor = placed[f"RC{branch + 1}"]
        source = next(p.GetPosition() for p in resistor.Pads()
                      if p.GetNumber() == "2")

        landings = []
        for index in (2 * branch, 2 * branch + 1):
            microphone = placed[f"MK{index + 1}"]
            landings.append(local_to_board(microphone, *MIC_ESCAPES["3"][-1]))
        split_angle = sum(board_angle(p) for p in landings) / 2.0

        alternatives = []
        for radius in CLOCK_RING_RADII:
            split = polar_point(radius, split_angle)
            for trunk in clock_trunk_options(source, split, radius):
                for bias in range(4):
                    # Resistor to split point, staying on F.Cu. The data spokes
                    # own B.Cu inside the handover radius and F.Cu only outside
                    # it, so a clock ring under that radius separates the two
                    # families by layer instead of letting them fight over the
                    # same annulus. It also leaves every branch with no vias.
                    paths = [thread_path(trunk, bias)]
                    # split point -> each microphone, entering down the same
                    # clear tangential lane the escape leaves by, so the arm
                    # never crosses the channel's own resistors or capacitor
                    entry_radius = arm_entry_radius(
                        radius, [board_angle(p) for p in landings])
                    for landing in landings:
                        entry = polar_point(entry_radius, board_angle(landing))
                        paths.append(thread_path([split, entry], bias))
                        # The last leg is a single straight radial line, not a
                        # 45-degree dogleg. Everything in the outer annulus -
                        # the damping resistors, the decoupling caps, the
                        # supply ring stubs - is arranged radially per channel,
                        # so a dogleg sideways here lands on a neighbour.
                        paths.append([entry, landing])
                    alternatives.append(paths)

        count += try_paths(board, net, pcbnew.F_Cu, CLOCK_WIDTH, alternatives)
    return count


# Vertical corridors down the outside of the central block. The lower middle of
# the board is solid on F.Cu - power row, series resistors, ESD arrays and the
# Pi header fill it from y = -14 to y = -33 - so a branch heading to the bottom
# of the array cannot go straight there. It runs out to one of these corridors
# first, drops to the clock ring radius, and comes back in along a chord, which
# keeps the whole detour inside the ring and clear of the data spokes.
CLOCK_CORRIDORS = (25.5, -24.5, 29.0, -27.5, 19.0)
CLOCK_BLOCK_TOP_Y = -13.0
CLOCK_LATERAL_Y = (-13.2, -14.6, -15.4)


def arm_entry_radius(ring_radius, angles):
    """Radius at which the two arms leave the ring for their microphones.

    The Tang socket rows run out to x = 39 mm, so on the right of the board a
    ring at 34 mm passes within a millimetre of the pin rows and the arms have
    nowhere to turn. Where that happens the arms leave from further out, up to
    just inside the radius where the data spokes take over the top layer. Both
    arms of a pair always use the same value, so the branch stays symmetric and
    the two microphones stay length-matched.
    """
    radius = ring_radius
    while radius < SPOKE_HANDOVER_RADIUS - 1.0 and any(
            _near_pin_row(radius, angle) for angle in angles):
        radius += 1.0
    return radius


def _near_pin_row(radius, angle):
    """Would a point at this radius and angle land on a through-hole row?

    Both the module socket, whose rows reach out to x = 39 mm, and the Pi
    header, which fills the bottom of the board, get in the way: a ring at
    34 mm passes within half a millimetre of a header pin at 292 degrees.
    """
    x = radius * math.cos(math.radians(angle))
    y = radius * math.sin(math.radians(angle))
    for low, high, pins in pin_rows():
        if not (min(pins) - 2.0 <= x <= max(pins) + 2.0):
            continue
        if low - 3.0 <= y <= high + 3.0:
            return True
    return False


def clock_trunk_options(source, split, radius):
    """Waypoint sets from a branch's series resistor out to its split point."""
    sy = -(source.y / 1e6 - d.PAGE_CY)
    px = split.x / 1e6 - d.PAGE_CX
    py = -(split.y / 1e6 - d.PAGE_CY)
    options = [[source, split]]
    if py >= CLOCK_BLOCK_TOP_Y:
        return options
    # Nearest corridor on the split's own side first: the shorter the lateral
    # detour, the less of the board it takes up and the less skew it adds.
    for corridor in sorted(CLOCK_CORRIDORS, key=lambda c: abs(c - px)):
        if abs(corridor) >= radius - 1.0:
            continue
        ring_y = -math.sqrt(radius ** 2 - corridor ** 2)
        # The lateral run out to the corridor can go across at the resistor's
        # own height, or in the clear band just under the module socket. The
        # low ones matter: branch 4 runs almost straight out to the left at the
        # resistors' height, so a branch heading for the bottom left has to
        # pass underneath it rather than through it.
        for lateral_y in (sy,) + CLOCK_LATERAL_Y:
            turn = vec(*d.to_kicad(corridor, lateral_y))
            if lateral_y < CLOCK_BLOCK_TOP_Y:
                # Drop through the pin row beside the resistor before setting
                # off sideways, so the crossing lands in the gap next to the
                # branch's own column instead of somewhere out to the right,
                # where the supply feed runs.
                near = vec(*d.to_kicad(board_xy(source)[0], lateral_y))
                options.append([source, near, turn,
                                vec(*d.to_kicad(corridor, ring_y)), split])
                if math.hypot(corridor, py) < SPOKE_HANDOVER_RADIUS - 1.0:
                    options.append([source, near, turn,
                                    vec(*d.to_kicad(corridor, py)), split])
            # Down the corridor to the ring, then in along a chord.
            options.append([source, turn,
                            vec(*d.to_kicad(corridor, ring_y)), split])
            # Or straight past the block and in level with the split. That runs
            # wider than the ring, so it is only offered while it stays inside
            # the radius where the data spokes take over the top layer.
            if math.hypot(corridor, py) < SPOKE_HANDOVER_RADIUS - 1.0:
                options.append([source, turn,
                                vec(*d.to_kicad(corridor, py)), split])
    return options


DATA_WIDTH = 0.2
# Lane offsets from the pin row, one per data net on that row, ordered so the
# net that reaches furthest along the row travels closest to it.
DATA_LANE_OFFSETS = (5.4, 4.6, 3.8, 3.0)
# Extra lanes further out from the row, tried only when all four are taken.
# A spoke whose handover via sits under another net's lane cannot reach the row
# from inside it; it has to go round the outside of that lane and come up at
# its own pin, which is beyond where the blocking lane ends.
DATA_LANE_FALLBACKS = (6.2, 7.0, 7.8, 8.6)


def socket_pin_for(net_name):
    """Which module socket pin a net is assigned to."""
    for (ref, position), name in d.TANG_NET_MAP.items():
        if name == net_name:
            return ref, position
    raise SystemExit(f"{net_name} is not assigned to a module pin")


def route_data_spokes(board, placed, pin_net, net_items):
    """Bring each pair's PDM data line in to its FPGA socket pin.

    The outer leg is a radial run on F.Cu, in the same lane the channel's own
    damping resistor sits on, then the line drops to B.Cu at the handover
    radius and crosses the module sockets between pins. The clock branches use
    F.Cu outside that radius and B.Cu inside, so the two families share the
    board without ever meeting on a layer.
    """
    count = 0
    for k in range(d.MIC_COUNT):
        pair = k // 2
        net = net_items[f"PDM_D{pair}"]
        microphone = placed[f"MK{k + 1}"]
        resistor = placed[f"RD{k + 1}"]
        source = next(p.GetPosition() for p in resistor.Pads()
                      if p.GetNumber() == "2")

        entry = polar_point(SPOKE_HANDOVER_RADIUS, board_angle(source))
        if not add_track(board, net, pcbnew.F_Cu, DATA_WIDTH, [source, entry]):
            continue
        count += 1

        socket_ref, position = socket_pin_for(f"PDM_D{pair}")
        socket = placed[socket_ref]
        target = next(p.GetPosition() for p in socket.Pads()
                      if p.GetNumber() == str(position))
        # Each net gets its own lane parallel to the pin row, and only runs
        # along it as far as its own pin. Cutting diagonally to the pin instead
        # makes neighbouring data lines cross each other.
        row_y = d.TANG_ROW_SPACING / 2.0 * (1 if socket_ref == "J2" else -1)
        # Approach from whichever side the handover via is already on. Channels
        # at 0 and 180 degrees hand over at y = 0, between the two rows, so
        # forcing every J3 line to come from below made those legs dive past
        # the row and double back - and that doubled-back leg is what swept
        # across the neighbouring data net.
        entry_y = -(entry.y / 1e6 - d.PAGE_CY)
        sign = 1.0 if entry_y > row_y else -1.0
        pin_x = d.tang_socket_x(position)
        approach = vec(*d.to_kicad(pin_x, row_y + sign * 2.0))

        # The preferred lane first, then the others: sixteen spokes converge on
        # two pin rows, so which lane is free depends on what has already been
        # laid down rather than on the channel's own geometry.
        preferred = DATA_LANE_OFFSETS[pair % 4]
        offsets = ([preferred]
                   + [o for o in DATA_LANE_OFFSETS if o != preferred]
                   + list(DATA_LANE_FALLBACKS))
        alternatives = []
        entry_x = board_xy(entry)[0]
        for offset in offsets:
            lane_y = row_y + sign * offset
            lane_turn = vec(*d.to_kicad(pin_x, lane_y))
            # Two shapes: cut straight across to the lane, or come in to the
            # lane's height first and only then run along it. The second matters
            # for the pins at the ends of the rows - a diagonal out to pin 1
            # bulges past the supply ring before it turns.
            for head in ([entry], [entry, vec(*d.to_kicad(entry_x, lane_y))]):
                for bias in range(3):
                    alternatives.append([thread_path(
                        head + [lane_turn, approach, target], bias)])

        placed_len = try_paths(board, net, pcbnew.B_Cu, DATA_WIDTH, alternatives)
        if placed_len:
            add_via(board, net, entry)
            count += placed_len
    return count


POWER_WIDTH = 0.6
SIGNAL_WIDTH = 0.2
INPUT_WIDTH = 0.4
PI_FEED_LANES = (-21.00, -21.15, -20.85, -21.30)
# Parts in the power row, whose ground vias must clear the supply bus.
POWER_ROW_REFS = frozenset(["C1", "C2", "C3", "C4", "C5", "C9", "U1"])


def pad_at(placed, ref, number):
    footprint = placed[ref]
    return next(p.GetPosition() for p in footprint.Pads()
                if p.GetNumber() == str(number))


def chain(board, net_items, name, layer, width, nodes, placed):
    """Route a net through an ordered list of pads and explicit waypoints."""
    net = net_items[name]
    points = []
    for node in nodes:
        if isinstance(node, tuple) and len(node) == 2 and isinstance(node[0], str):
            points.append(pad_at(placed, node[0], node[1]))
        else:
            points.append(vec(*d.to_kicad(node[0], node[1])))
    count = 0
    for a, b in zip(points, points[1:]):
        for x, y in zip(path_45(a, b), path_45(a, b)[1:]):
            count += add_track(board, net, layer, width, [x, y])
    return count


def route_power_block(board, placed, pin_net, net_items):
    """Route the 5 V input chain, the LDO and the clock rail filter."""
    count = 0
    # The 5 V feed stays on F.Cu the whole way. On B.Cu it would have to run
    # across the lower half of the board, and every data spoke from the bottom
    # eight microphones crosses that band on its way in to the module socket.
    # On top the same trip is clear: up the right of the ESD arrays, along the
    # gap between the regulator row and the host series resistors, then up the
    # outside of the fuse. That gap is only about 0.9 mm wide, hence the
    # narrower track - still over twice the 500 mA the fuse will pass.
    net = net_items["PI_5V"]
    count += add_track(board, net, pcbnew.F_Cu, POWER_WIDTH,
                       [pad_at(placed, "J1", 2), pad_at(placed, "J1", 4)])
    fuse_x = -23.14
    count += try_paths(board, net, pcbnew.F_Cu, INPUT_WIDTH,
                       [[thread_path([pad_at(placed, "J1", 4),
                                      vec(*d.to_kicad(12.70, lane)),
                                      vec(*d.to_kicad(fuse_x, lane)),
                                      pad_at(placed, "F1", 1)])]
                        for lane in PI_FEED_LANES])
    count += chain(board, net_items, "5V_FUSED", pcbnew.F_Cu, POWER_WIDTH, [
        ("F1", 2), ("D1", 2)], placed)
    # Both rails run as a bus just above the component row. Routing along the
    # row itself collides with each part's own ground pad and stitching via.
    bus = -14.60
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, POWER_WIDTH, [
        ("D1", 1), (-15.50, bus), (-7.45, bus), ("C4", 1)], placed)
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, POWER_WIDTH, [
        (-7.45, bus), (-2.28, bus), ("C1", 1)], placed)
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, POWER_WIDTH, [
        (-2.28, bus), (1.86, bus), ("U1", 1)], placed)
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, SIGNAL_WIDTH, [
        ("C4", 1), (-7.45, -19.60), ("C5", 1)], placed)
    # EN is strapped to IN down the free side of the package.
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, SIGNAL_WIDTH, [
        ("U1", 1), (0.30, -16.05), (0.30, -17.95), ("U1", 3)], placed)
    count += chain(board, net_items, "+5V", pcbnew.F_Cu, POWER_WIDTH, [
        (-2.28, bus), (-3.97, -13.30), ("J3", 18)], placed)
    # LDO output joins a matching bus on the far side.
    out_bus = -14.60
    count += chain(board, net_items, "+3V3A", pcbnew.F_Cu, POWER_WIDTH, [
        ("U1", 5), (4.14, out_bus), (6.72, out_bus), ("C2", 1)], placed)
    count += chain(board, net_items, "+3V3A", pcbnew.F_Cu, POWER_WIDTH, [
        (6.72, out_bus), (11.44, out_bus), ("FB1", 1)], placed)
    count += chain(board, net_items, "+3V3A", pcbnew.F_Cu, POWER_WIDTH, [
        ("C2", 1), (6.55, -18.60), ("C3", 1)], placed)
    count += chain(board, net_items, "+3V3_CLK", pcbnew.F_Cu, POWER_WIDTH, [
        ("FB1", 2), ("C9", 1)], placed)
    return count


# Radial escape lanes for the supply feed. Every angle sits in a gap between
# two microphone pairs, where no clock arm and no data spoke runs, and clear of
# both the test point groups and the mounting holes.
# Radial escape lanes for the supply feed, all in the gap between microphone
# pairs 0 and 1, where no clock arm and no data spoke runs, and clear of both
# the test point groups and the mounting hole at 33.75 degrees.
SUPPLY_FEED_ANGLES = (30.0, 27.0, 33.0, 36.0, 24.0, 39.0)
# Narrower than the rest of the supply routing because the feed has to pass
# between two module socket pins: 2.54 mm pitch on 1.7 mm pads leaves 0.84 mm,
# and a 0.6 mm track needs 0.9 mm of it. 0.4 mm carries over 1 A, against the
# 20-odd mA the sixteen microphones actually draw.
SUPPLY_FEED_WIDTH = 0.4
# The lane out of the regulator row, in the clear band immediately under the
# module socket. It is above everything else in the lower half: the clock
# branches heading for the bottom of the array drop through the pin row beside
# their own resistors, well to the left, and then run out underneath it.
SUPPLY_FEED_LANES = (-13.0, -12.7, -13.3)
# Where the feed crosses the module socket. Each is the midpoint between two
# socket pins, and each puts the top via between 34 and 43 degrees - in the gap
# between microphone pairs 0 and 1, so the radial leg above the socket starts
# in clear space instead of on pair 0's own clock arm. The near ones come
# first: the shorter the lane under the socket, the more of the band is left
# for the clock branches that have to drop through it.
SUPPLY_FEED_HOPS = (17.62, 20.16, 15.08, 22.70, 25.24)
SUPPLY_FEED_TOP_Y = 13.8


def route_supply_ring_feed(board, placed, pin_net, net_items):
    """Carry +3V3A from the regulator out to the distribution ring.

    This is the one net on the board that has to get from the centre to the rim
    against the grain of everything else, and it cannot do it on one layer. The
    eight clock branches radiate from the middle in every direction, so any
    top-layer path from the regulator to the edge crosses some of them; the
    eight bottom data spokes fan in across the lower half, so any bottom-layer
    path crosses those. So the feed changes layer twice: it runs out along the
    top in the clear band under the module socket, drops through the socket on
    the bottom layer in the gap between two pins, comes back up above the
    socket, and then goes straight out along a radial line in the gap between
    two microphone pairs. Three vias on a supply rail cost nothing, and both
    inner layers are ground, so the reference plane is continuous throughout.
    """
    net = net_items["+3V3A"]
    name = net.GetNetname()
    width = mm(SUPPLY_FEED_WIDTH)
    # Leave the ferrite's input pad downwards. Every part in the output row has
    # its other pad directly to one side, so a run that sets off sideways from
    # a pad hits its own package first.
    start = pad_at(placed, "FB1", 1)
    start_x = board_xy(start)[0]

    for lane in SUPPLY_FEED_LANES:
        for hop in SUPPLY_FEED_HOPS:
            drop = vec(*d.to_kicad(hop, lane))
            stub = [start, vec(*d.to_kicad(start_x, lane)), drop]
            if path_conflict(name, pcbnew.F_Cu, width, stub):
                continue
            top = vec(*d.to_kicad(hop, SUPPLY_FEED_TOP_Y))
            climb = [drop, top]
            if path_conflict(name, pcbnew.B_Cu, width, climb):
                continue
            # Straight out from the via along its own radius. A 45-degree
            # dogleg here would swing sideways into the neighbouring pair's
            # clock arm; the radial line stays in the gap between pairs.
            landing = polar_point(SUPPLY_RING_RADIUS,
                                  math.degrees(math.atan2(*reversed(
                                      board_xy(top)))))
            rise = [top, landing]
            if not path_conflict(name, pcbnew.F_Cu, width, rise):
                count = add_track(board, net, pcbnew.F_Cu, SUPPLY_FEED_WIDTH,
                                  stub, guard=False)
                add_via(board, net, drop)
                count += add_track(board, net, pcbnew.B_Cu, SUPPLY_FEED_WIDTH,
                                   climb, guard=False)
                add_via(board, net, top)
                count += add_track(board, net, pcbnew.F_Cu, SUPPLY_FEED_WIDTH,
                                   rise, guard=False)
                add_via(board, net, landing)
                return count

    REFUSED.append((name, "F.Cu",
                    "no clear route from the regulator out to the supply ring"))
    return 0


def route_host_block(board, placed, pin_net, net_items):
    """Route the Pi header through its ESD arrays and series resistors.

    Each signal runs connector -> ESD array -> series resistor on F.Cu, then
    back up to its socket pin on B.Cu. The return leg has to cross both rows of
    the header, so it is threaded between pins.
    """
    links = [
        # (Pi-side net, board-side net, ESD ref, ESD pin, resistor ref)
        ("PI_SCLK", "SPI_SCLK", "U3", "1", "RH1"),
        ("PI_MOSI", "SPI_MOSI", "U3", "3", "RH2"),
        ("PI_MISO", "SPI_MISO", "U3", "4", "RH3"),
        ("PI_CS_N", "SPI_CS_N", "U3", "6", "RH4"),
        ("PI_IRQ", "HOST_IRQ", "U4", "1", "RH5"),
        ("PI_SYNC", "HOST_SYNC", "U4", "3", "RH6"),
        ("PI_RESET_N", "HOST_RESET_N", "U4", "4", "RH7"),
        ("PI_STATUS", "HOST_STATUS", "U4", "6", "RH8"),
    ]
    header_pin = {}
    for pin, name in d.PI_HEADER.items():
        if name.startswith("PI_") and name != "PI_5V":
            header_pin[name] = pin

    count = 0
    for pi_net, board_net, esd, esd_pin, resistor in links:
        net = net_items[pi_net]

        # connector -> ESD array
        start = pad_at(placed, "J1", header_pin[pi_net])
        middle = pad_at(placed, esd, esd_pin)
        run = [start] + thread_socket_rows(start, middle) + [middle]
        for a, b in zip(run, run[1:]):
            count += add_track(board, net, pcbnew.F_Cu, SIGNAL_WIDTH,
                               path_45(a, b))

        # ESD array -> series resistor
        end = pad_at(placed, resistor, "1")
        count += add_track(board, net, pcbnew.F_Cu, SIGNAL_WIDTH,
                           path_45(middle, end))

        # series resistor -> socket pin, on the bottom layer
        net = net_items[board_net]
        source = pad_at(placed, resistor, "2")
        socket_ref, position = socket_pin_for(board_net)
        target = pad_at(placed, socket_ref, position)
        drop = add_via(board, net, pcbnew.VECTOR2I(
            source.x, source.y - mm(1.4)))
        count += add_track(board, net, pcbnew.F_Cu, SIGNAL_WIDTH,
                           [source, drop])
        run = [drop] + thread_socket_rows(drop, target) + [target]
        for a, b in zip(run, run[1:]):
            count += add_track(board, net, pcbnew.B_Cu, SIGNAL_WIDTH,
                               path_45(a, b))
    return count


def reserve_microphone_corridors(board, placed, pin_net, net_items):
    """Prepare the board the autorouter sees.

    Each escape ends on a via placed just outside the microphone, and the
    corridor leading to it is covered by a keepout. The autorouter can reach a
    bare via perfectly well, so it routes the rest of each net up to that
    handover point while staying out of the corridor. The keepouts are deleted
    again once the escapes are drawn.
    """
    vias = 0
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        footprint = placed[ref]

        for number, path in MIC_ESCAPES.items():
            net = net_items[pin_net[(ref, number)]]
            add_via(board, net, local_to_board(footprint, *path[-1]))
            vias += 1

        left, top, right, bottom = MIC_CORRIDOR
        keepout = pcbnew.ZONE(board)
        keepout.SetIsRuleArea(True)
        keepout.SetDoNotAllowTracks(True)
        keepout.SetDoNotAllowVias(True)
        keepout.SetDoNotAllowZoneFills(False)
        keepout.SetLayer(pcbnew.F_Cu)
        keepout.SetZoneName(f"MIC_ESCAPE_{ref}")
        outline = keepout.Outline()
        outline.NewOutline()
        for lx, ly in ((left, top), (right, top), (right, bottom),
                       (left, bottom)):
            point = local_to_board(footprint, lx, ly)
            outline.Append(point.x, point.y)
        board.Add(keepout)
    return vias


def link_microphone_straps(board, placed, pin_net, net_items):
    """Pre-route each microphone's L/R strap inside the ground ring.

    Pad 2 sits inside the ring of ground pads with only a 0.40 mm gap around
    it, so it cannot reach a via of its own and it must not consume one of the
    four diagonal corners that pads 1, 3 and 4 need to escape through. It is
    always on the same net as a neighbour it can simply be joined to: ground
    (pad 6) on even channels, or the supply pad 1 on odd channels.
    """
    count = 0
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        footprint = placed[ref]
        pads = {pad.GetNumber(): pad for pad in footprint.Pads()}
        strap = pads["2"]
        net_name = pin_net[(ref, "2")]
        target = pads["6"] if net_name == "GND" else pads["1"]

        track = pcbnew.PCB_TRACK(board)
        track.SetStart(strap.GetPosition())
        track.SetEnd(target.GetPosition())
        track.SetWidth(mm(0.15))
        track.SetLayer(pcbnew.F_Cu)
        track.SetNet(net_items[net_name])
        board.Add(track)
        count += 1
    return count


def place_ground_stitching(board, placed, pin_net, net_items):
    """Give every ground pad its own via into the inner planes.

    The via is pushed straight out from the footprint centre past the pad edge,
    and a short track ties the pad to it on the pad's own copper layer.
    """
    gnd = net_items["GND"]
    count = 0

    # Every pad on the board, so a stitching via never lands on a neighbour.
    obstacles = []
    for other_ref, other in placed.items():
        for pad in other.Pads():
            size = pad.GetSize()
            obstacles.append((pad.GetPosition(),
                              math.hypot(size.x, size.y) / 2.0,
                              pin_net.get((other_ref, pad.GetNumber()))))
    vias_placed = []

    for ref, footprint in sorted(placed.items()):
        on_bottom = footprint.IsFlipped()
        for pad in footprint.Pads():
            if pin_net.get((ref, pad.GetNumber())) != "GND":
                continue
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH:
                continue  # plated holes already reach both planes
            if ref.startswith("MK") and pad.GetNumber() == "2":
                continue  # L/R strap: linked to the ground ring instead

            position = pad.GetPosition()
            override = (MIC_GND_VIAS.get(pad.GetNumber())
                        if ref.startswith("MK") else None)
            if override is not None:
                target = local_to_board(footprint, *override)
            elif ref in POWER_ROW_REFS:
                # Push the via straight down, away from the supply bus that
                # runs above the row.
                target = pcbnew.VECTOR2I(position.x, position.y + mm(1.45))
            elif ref.startswith("CM"):
                # Per-channel decoupling capacitor: push the via straight in
                # towards the board centre. The generic axis search can pick a
                # tangential direction here, which lays the via and its track
                # across the microphone's data escape running past it.
                centre_x, centre_y = mm(d.PAGE_CX), mm(d.PAGE_CY)
                dx = centre_x - position.x
                dy = centre_y - position.y
                length = math.hypot(dx, dy)
                step = mm(1.05) / length
                target = pcbnew.VECTOR2I(int(position.x + dx * step),
                                         int(position.y + dy * step))
            else:
                target = choose_stitch_position(pad, footprint, position,
                                                obstacles, vias_placed)
            if target is None:
                continue
            vias_placed.append(target)

            via = pcbnew.PCB_VIA(board)
            via.SetPosition(target)
            via.SetWidth(mm(VIA_DIAMETER))
            via.SetDrill(mm(VIA_DRILL))
            via.SetNet(gnd)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            # Tented both sides: these are routing vias for the plugged-via
            # process, and leaving the mask open would also clip silkscreen.
            via.SetFrontTentingMode(pcbnew.TENTING_MODE_TENTED)
            via.SetBackTentingMode(pcbnew.TENTING_MODE_TENTED)
            board.Add(via)

            track = pcbnew.PCB_TRACK(board)
            track.SetStart(position)
            track.SetEnd(target)
            track.SetWidth(mm(0.3))
            track.SetLayer(pcbnew.B_Cu if on_bottom else pcbnew.F_Cu)
            track.SetNet(gnd)
            board.Add(track)
            count += 1
    return count


def add_silkscreen(board):
    """Channel numbers, cardinal marks and board identity."""
    def text(value, x, y, size=1.2, layer=pcbnew.F_SilkS, angle=0.0,
             thickness=0.16, mirrored=False):
        item = pcbnew.PCB_TEXT(board)
        item.SetText(value)
        item.SetPosition(vec(x, y))
        item.SetLayer(layer)
        item.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
        item.SetTextThickness(mm(thickness))
        item.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        item.SetTextAngleDegrees(angle)
        item.SetMirrored(mirrored)
        board.Add(item)

    # Channel numbers sit just inside the rim. They are deliberately left
    # unrotated: KiCad bounds text with an axis-aligned box, so tangential
    # labels would claim far more room than their glyphs occupy and collide
    # with the microphone pads.
    for k in range(d.MIC_COUNT):
        lx, ly = d.polar(57.8, d.mic_angle(k))
        kx, ky = d.to_kicad(lx, ly)
        text(f"CH{k}", kx, ky, size=1.1, thickness=0.15)

    # Axis marks live in the clear band between the two socket rows.
    text("+X", *d.to_kicad(30.0, 0.0), size=1.4)
    text("-X", *d.to_kicad(-16.0, 0.0), size=1.4)

    # Short legend for each test point, pushed radially outward from its pad.
    for _ref, _net, radius, angle, label in d.TEST_POINTS:
        lx, ly = d.polar(radius + 2.5, angle)
        text(label, *d.to_kicad(lx, ly), size=1.0, thickness=0.15)

    text("16-CH PDM MIC ARRAY  rev A", *d.to_kicad(0.0, 18.0), size=1.4)
    text("PORTS FACE UP - DO NOT WASH", *d.to_kicad(0.0, 15.5), size=1.2)
    text("CH0..CH15 CCW FROM +X", *d.to_kicad(0.0, 20.5), size=1.2)

    # The module and host connector are on the reverse, so label them there.
    text("TANG NANO 9K - USB-C THIS END", d.PAGE_CX + 24.0, d.PAGE_CY,
         size=1.4, layer=pcbnew.B_SilkS, mirrored=True)
    text("RPi P1 26-WAY - PIN 1 MARKED", d.PAGE_CX, d.PAGE_CY + 17.5,
         size=1.4, layer=pcbnew.B_SilkS, mirrored=True)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    path, comps, nets, stitches, straps = build(
        root, with_escapes="--no-escapes" not in sys.argv)
    print(f"wrote {path}")
    print(f"components {comps}  nets {nets}  ground stitching vias {stitches}  "
          f"routed segments {straps}")
    if REFUSED:
        summary = collections.Counter((net, reason) for net, _l, reason in REFUSED)
        print(f"refused {len(REFUSED)} paths that would have shorted:")
        for (net, reason), count in summary.most_common():
            print(f"   {net}: {reason} ({count})")
