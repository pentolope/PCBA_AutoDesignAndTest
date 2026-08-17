"""Put the test points back, but only where they can actually go.

The original 24 pads sat on rings at R = 26 and 32 mm, which is the annulus the
clock branches fan out through; one of them shorted a module socket pin. This
places them differently: a probe pad goes *on top of* a piece of its own net
that is already routed, so nothing has to be routed to reach it. A pad
overlapping a track or a via of the same net is connected, which means the
placement problem is only "is there room", never "can it be wired".

A candidate has to clear every other net by that pair's net class clearance,
stay out of every courtyard, sit outside the Tang Nano's outline so a probe can
reach it, and keep its distance from the other test points. Nets with nowhere
that satisfies all of that are reported and left out - an unplaceable test
point is not worth a short.
"""

import collections
import math
import os
import sys

import pcbnew

import design as d
import patch_board as pb

BOARD = "microphone_array_v2.kicad_pcb"
FOOTPRINT_LIB = r"C:\Program Files\KiCad\10.0\share\kicad\footprints\TestPoint.pretty"
# Two pad sizes. The 1.5 mm pad is the comfortable one; where the host block is
# too dense for it a 1.0 mm pad still takes a fine probe tip.
PAD_SIZES = (("TestPoint_Pad_D1.5mm", 0.75, 3.5),
             ("TestPoint_Pad_D1.0mm", 0.50, 2.5))
LOCAL_LIB_NICK = "MicArrayV2"
SILK_FREE_SUFFIX = "_NoSilk"


def local_variant(root, name):
    """A silk-free copy of a stock test point footprint, in the local library.

    The pads land in whatever gaps the routed board left, and the stock
    silkscreen circle collides with the legend of the parts they tuck beside.
    Editing the footprint on the board would work but leaves KiCad reporting
    that it no longer matches its library, so the edited version is saved as a
    footprint in its own right.
    """
    variant = name + SILK_FREE_SUFFIX
    path = os.path.join(root, LOCAL_LIB_NICK + ".pretty")
    footprint = pcbnew.FootprintLoad(FOOTPRINT_LIB, name)
    for item in list(footprint.GraphicalItems()):
        if item.GetLayer() in (pcbnew.F_SilkS, pcbnew.B_SilkS):
            footprint.Remove(item)
    footprint.SetFPID(pcbnew.LIB_ID(LOCAL_LIB_NICK, variant))
    pcbnew.FootprintSave(path, footprint)
    return variant


def courtyard_radius(name):
    """Half-diagonal of a test point footprint's own courtyard.

    Clearing the pad is not enough - DRC compares courtyard against courtyard,
    and the test point's is a good deal bigger than its pad.
    """
    footprint = pcbnew.FootprintLoad(FOOTPRINT_LIB, name)
    box = footprint.GetCourtyard(pcbnew.F_CrtYd).BBox()
    return max(box.GetWidth(), box.GetHeight()) / 2.0

COURTYARD_MARGIN = 0.15
SAMPLE_STEP = 0.4
GRID_STEP = 1.0            # for nets that need a via rather than existing copper
EDGE_LIMIT = d.BOARD_RADIUS - 2.0

# The module hides everything under its own outline: 70 x 26 mm, centred on the
# socket rows. A pad there could be soldered but never probed.
MODULE_X = (d.TANG_CX - 35.0, d.TANG_CX + 35.0)
MODULE_Y = (-13.0, 13.0)

PROBE_NETS = [net for _ref, net, _r, _a, _label in d.TEST_POINT_TABLE]


def under_module(x, y):
    return MODULE_X[0] <= x <= MODULE_X[1] and MODULE_Y[0] <= y <= MODULE_Y[1]


def courtyards(board):
    shapes = []
    for footprint in board.Footprints():
        for layer in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
            poly = footprint.GetCourtyard(layer)
            if poly.OutlineCount():
                shapes.append(poly)
    return shapes


def in_courtyard(shapes, point, pad_radius):
    limit = pcbnew.FromMM(pad_radius + COURTYARD_MARGIN)
    for poly in shapes:
        if poly.Collide(point, limit):
            return True
    return False


def grid_points():
    """Anywhere on the board, for a net whose copper is a plane."""
    points = []
    steps = int(2 * EDGE_LIMIT / GRID_STEP)
    for i in range(steps + 1):
        for j in range(steps + 1):
            x = -EDGE_LIMIT + i * GRID_STEP
            y = -EDGE_LIMIT + j * GRID_STEP
            if math.hypot(x, y) <= EDGE_LIMIT:
                points.append(pcbnew.VECTOR2I(pcbnew.FromMM(d.PAGE_CX + x),
                                              pcbnew.FromMM(d.PAGE_CY - y)))
    return points


def candidates(board, net):
    """Points on this net's own top-layer copper that a pad could sit on."""
    if net == "GND":
        # The ground planes run under the whole board, so a probe pad anywhere
        # clear reaches ground through a via of its own.
        return grid_points()
    points = []
    for item in board.Tracks():
        if item.GetNetname() != net:
            continue
        if isinstance(item, pcbnew.PCB_VIA):
            points.append(item.GetPosition())
            continue
        if item.GetLayer() != pcbnew.F_Cu:
            continue
        start, end = item.GetStart(), item.GetEnd()
        length = math.hypot(end.x - start.x, end.y - start.y)
        steps = max(1, int(length / pcbnew.FromMM(SAMPLE_STEP)))
        for i in range(steps + 1):
            t = i / steps
            points.append(pcbnew.VECTOR2I(int(start.x + (end.x - start.x) * t),
                                          int(start.y + (end.y - start.y) * t)))
    return points


def headroom(model, rules, net, point, pad_radius, drill=None):
    """How much room a pad here has beyond the minimum, or None if it fails."""
    radius = pcbnew.FromMM(pad_radius)
    worst = None
    for pad, pad_net, _label in model.pads:
        if pad_net == net or not pad.IsOnLayer(pcbnew.F_Cu):
            continue
        gap = (pb.pad_distance(pad, (point.x, point.y), (point.x, point.y))
               - radius - rules.between(net, pad_net))
        if gap < 0:
            return None
        if drill is not None and pad.GetDrillSizeX() > 0:
            hole = (math.hypot(pad.GetPosition().x - point.x,
                               pad.GetPosition().y - point.y)
                    - drill / 2.0 - pad.GetDrillSizeX() / 2.0
                    - pcbnew.FromMM(pb.HOLE_TO_HOLE_MM))
            if hole < 0:
                return None
        worst = gap if worst is None else min(worst, gap)
    for track in model.tracks:
        other = track.GetNetname()
        if other == net or track.GetLayer() != pcbnew.F_Cu:
            continue
        start, end = track.GetStart(), track.GetEnd()
        gap = (pb.point_segment(point.x, point.y, start.x, start.y, end.x, end.y)
               - track.GetWidth() / 2.0 - radius - rules.between(net, other))
        if gap < 0:
            return None
        worst = gap if worst is None else min(worst, gap)
    for via in model.vias:
        other = via.GetNetname()
        if other == net:
            continue
        position = via.GetPosition()
        gap = (math.hypot(position.x - point.x, position.y - point.y)
               - via.GetWidth(pcbnew.F_Cu) / 2.0 - radius
               - rules.between(net, other))
        if gap < 0:
            return None
        worst = gap if worst is None else min(worst, gap)
    if drill is not None:
        for via in model.vias:
            position = via.GetPosition()
            hole = (math.hypot(position.x - point.x, position.y - point.y)
                    - drill / 2.0 - via.GetDrill() / 2.0
                    - pcbnew.FromMM(pb.HOLE_TO_HOLE_MM))
            if hole < 0 and via.GetNetname() != net:
                return None
    return worst if worst is not None else pcbnew.FromMM(10.0)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board = pcbnew.LoadBoard(os.path.join(root, BOARD))
    rules = pb.Rules(root)
    model = pb.Model(board, rules)
    shapes = courtyards(board)
    drill = pcbnew.FromMM(0.3)

    placed, missed, taken = [], [], []
    for net in PROBE_NETS:
        # GND is reached through a via of the pad's own, so its hole has to
        # clear every other drill on the board as well.
        needs_via = net == "GND"
        chosen = None
        for name, pad_radius, spacing_mm in PAD_SIZES:
            spacing = pcbnew.FromMM(spacing_mm)
            keepout = pcbnew.ToMM(courtyard_radius(name))
            best, best_room = None, -1
            for point in candidates(board, net):
                x, y = pb.board_xy(point)
                if math.hypot(x, y) > EDGE_LIMIT or under_module(x, y):
                    continue
                if any(math.hypot(point.x - t.x, point.y - t.y) < spacing
                       for t in taken):
                    continue
                if in_courtyard(shapes, point, keepout):
                    continue
                room = headroom(model, rules, net, point, pad_radius,
                                drill if needs_via else None)
                if room is not None and room > best_room:
                    best, best_room = point, room
            if best is not None:
                chosen = (name, pad_radius, best, best_room)
                break
        if chosen is None:
            missed.append(net)
            continue
        name, pad_radius, point, room = chosen
        taken.append(point)
        placed.append((f"TP{len(placed) + 1}", net, name, point, room,
                       needs_via))

    footprints = []
    for ref, net, name, point, room, needs_via in placed:
        variant = local_variant(root, name)
        footprint = pcbnew.FootprintLoad(
            os.path.join(root, LOCAL_LIB_NICK + ".pretty"), variant)
        board.Add(footprint)
        footprint.SetFPID(pcbnew.LIB_ID(LOCAL_LIB_NICK, variant))
        footprint.SetReference(ref)
        footprint.SetValue(net)
        footprint.SetPosition(point)
        footprint.SetExcludedFromBOM(True)
        footprint.SetLibDescription(f"test point on {net}")
        footprint.SetField("Description", f"test point on {net}")
        for field in footprint.GetFields():
            if field.GetName() != "Reference":
                field.SetVisible(False)
            field.SetLayer(pcbnew.F_Fab)
        net_item = board.FindNet(net)
        for pad in footprint.Pads():
            pad.SetNet(net_item)
        if needs_via:
            via = pcbnew.PCB_VIA(board)
            via.SetPosition(point)
            via.SetWidth(pcbnew.FromMM(0.45))
            via.SetDrill(drill)
            via.SetNet(net_item)
            via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
            board.Add(via)
        footprints.append((ref, net, name, pb.board_xy(point),
                           pcbnew.ToMM(room)))

    print(f"placed {len(placed)} of {len(PROBE_NETS)} test points")
    for ref, net, name, point, room in footprints:
        size = "1.5 mm" if "1.5" in name else "1.0 mm"
        print(f"   {ref:5s} {net:14s} {size} at {point}  "
              f"clear by {room:.2f} mm")
    if missed:
        print(f"no room for {len(missed)}: {', '.join(sorted(set(missed)))}")

    board.Save(os.path.join(root, BOARD))
    print("saved")

    # The positions come from the routed copper, not from a formula, so they
    # have to be recorded for tools/netlist.py to reproduce them.
    table = os.path.join(root, "generated", "test_points.py")
    with open(table, "w", encoding="utf-8") as handle:
        print("# Written by tools/place_testpoints.py. Board positions of the",
              file=handle)
        print("# probe pads, chosen against the routed copper, so that",
              file=handle)
        print("# tools/netlist.py can reproduce them.", file=handle)
        print("PLACED_TEST_POINTS = (", file=handle)
        for ref, net, name, (x, y), _room in footprints:
            print('    ("%s", "%s", "%s", %.3f, %.3f),'
                  % (ref, net, LOCAL_LIB_NICK + ":" + name + SILK_FREE_SUFFIX,
                     x, y), file=handle)
        print(")", file=handle)
    print(f"wrote {table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
