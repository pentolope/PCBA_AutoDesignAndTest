"""Repair the routed board in place, without disturbing the routing.

The board is no longer regenerated from tools/gen_pcb.py - it has been through
an external autorouter - so fixes have to be surgical. This script edits only
the specific items that DRC objects to and leaves every other track alone.

What it fixes:

  * Ground stitching vias that sit on another net. These come from
    place_ground_stitching()'s special case for the regulator row, which pushes
    the via 1.45 mm straight down with no obstacle test at all. Two of them are
    hard shorts - one onto the LDO's enable pin, one onto the 5 V input lane.
    Each is moved to the nearest position where both it and its stub are clear.
  * Tracks that clear a pad by less than their net class demands, narrowed to
    the minimum width that satisfies the rule.
  * Dangling stubs left behind by earlier edits.
  * Via pairs whose drills are closer than the hole-to-hole minimum.

Clearance is evaluated the way KiCad does it: the larger of the two net
classes' clearances, which is why a 0.15 mm assumption in the generator let
several of these through against POWER (0.25 mm) and Default (0.20 mm) nets.
"""

import fnmatch
import json
import math
import os
import sys

import pcbnew

import design as d

BOARD = "microphone_array_v2.kicad_pcb"
PROJECT = "microphone_array_v2.kicad_pro"

STUB_MAX_MM = 2.5          # a stitching stub is never longer than this
HOLE_TO_HOLE_MM = 0.25
SEARCH_RADII = [0.9 + 0.1 * i for i in range(32)]
SEARCH_STEPS = 72


def load_clearances(root):
    """Net class clearance per pattern, in KiCad internal units."""
    with open(os.path.join(root, PROJECT), encoding="utf-8") as handle:
        project = json.load(handle)
    settings = project["net_settings"]
    by_name = {c["name"]: c["clearance"] for c in settings["classes"]}
    patterns = [(p["pattern"], by_name[p["netclass"]])
                for p in settings.get("netclass_patterns", [])]
    default = by_name.get("Default", 0.2)
    return patterns, default


class Rules:
    def __init__(self, root):
        self.patterns, self.default = load_clearances(root)
        self._cache = {}

    def clearance(self, net):
        if net not in self._cache:
            value = self.default
            for pattern, amount in self.patterns:
                if fnmatch.fnmatchcase(net, pattern):
                    value = amount
                    break
            self._cache[net] = pcbnew.FromMM(value)
        return self._cache[net]

    def between(self, net_a, net_b):
        return max(self.clearance(net_a), self.clearance(net_b))


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------

def point_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def side(ax, ay, bx, by, cx, cy):
    value = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return 0 if abs(value) < 1.0 else (1 if value > 0 else -1)


def segments_cross(a1, a2, b1, b2):
    return (side(*b1, *b2, *a1) * side(*b1, *b2, *a2) < 0
            and side(*a1, *a2, *b1) * side(*a1, *a2, *b2) < 0)


def segment_distance(a1, a2, b1, b2):
    if segments_cross(a1, a2, b1, b2):
        return 0.0
    return min(point_segment(*a1, *b1, *b2), point_segment(*a2, *b1, *b2),
               point_segment(*b1, *a1, *a2), point_segment(*b2, *a1, *a2))


def pad_distance(pad, a, b):
    """Distance from segment a-b to a pad's outline."""
    position = pad.GetPosition()
    size = pad.GetSize()
    if pad.GetShape() == pcbnew.PAD_SHAPE_CIRCLE:
        return max(0.0, point_segment(position.x, position.y, *a, *b)
                   - size.x / 2.0)
    angle = math.radians(pad.GetOrientation().AsDegrees())
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    hx, hy = size.x / 2.0, size.y / 2.0
    local = []
    for point in (a, b):
        dx, dy = point[0] - position.x, point[1] - position.y
        local.append((dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a))
    for x, y in local:
        if -hx <= x <= hx and -hy <= y <= hy:
            return 0.0
    corners = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    return min(segment_distance(local[0], local[1], corners[i],
                                corners[(i + 1) % 4]) for i in range(4))


# --------------------------------------------------------------------------
# board model
# --------------------------------------------------------------------------

class Model:
    """Everything on the board that a new piece of copper has to clear."""

    def __init__(self, board, rules):
        self.board = board
        self.rules = rules
        self.refresh()

    def refresh(self, ignore=()):
        # SWIG wrappers are not hashable, so identify items by KIID string.
        self.ignore = {item.m_Uuid.AsString() for item in ignore}
        self.pads = []
        for footprint in self.board.Footprints():
            for pad in footprint.Pads():
                self.pads.append((pad, pad.GetNetname(),
                                  f"{footprint.GetReference()}.{pad.GetNumber()}"))
        self.tracks, self.vias = [], []
        for item in self.board.Tracks():
            if item.m_Uuid.AsString() in self.ignore:
                continue
            if isinstance(item, pcbnew.PCB_VIA):
                self.vias.append(item)
            else:
                self.tracks.append(item)

    def conflict(self, net, layers, a, b, half_width, drill=None):
        """What this piece of copper is too close to, or None."""
        for pad, pad_net, label in self.pads:
            if pad_net == net:
                continue
            if not any(pad.IsOnLayer(layer) for layer in layers):
                continue
            need = self.rules.between(net, pad_net) + half_width
            if pad_distance(pad, a, b) < need:
                return f"pad {label} ({pad_net or 'no net'})"
            if drill is not None and pad.GetDrillSizeX() > 0:
                gap = point_segment(pad.GetPosition().x, pad.GetPosition().y,
                                    *a, *b)
                if gap < drill / 2.0 + pad.GetDrillSizeX() / 2.0 \
                        + pcbnew.FromMM(HOLE_TO_HOLE_MM):
                    return f"hole of {label}"

        for track in self.tracks:
            other = track.GetNetname()
            if other == net or track.GetLayer() not in layers:
                continue
            need = (self.rules.between(net, other) + half_width
                    + track.GetWidth() / 2.0)
            start, end = track.GetStart(), track.GetEnd()
            if segment_distance(a, b, (start.x, start.y), (end.x, end.y)) < need:
                return f"track {other}"

        for via in self.vias:
            other = via.GetNetname()
            if other == net:
                continue
            position = via.GetPosition()
            need = (self.rules.between(net, other) + half_width
                    + via.GetWidth(pcbnew.F_Cu) / 2.0)
            if point_segment(position.x, position.y, *a, *b) < need:
                return f"via {other}"
            if drill is not None:
                gap = point_segment(position.x, position.y, *a, *b)
                if gap < drill / 2.0 + via.GetDrill() / 2.0 \
                        + pcbnew.FromMM(HOLE_TO_HOLE_MM):
                    return f"drill of via {other}"
        return None


def board_xy(point):
    return (round(point.x / 1e6 - d.PAGE_CX, 3),
            round(-(point.y / 1e6 - d.PAGE_CY), 3))


# --------------------------------------------------------------------------
# repairs
# --------------------------------------------------------------------------

def find_stitching_groups(board):
    """(via, stub, source pad) for every short ground stitch on the board."""
    stubs = [t for t in board.Tracks()
             if not isinstance(t, pcbnew.PCB_VIA)
             and t.GetNetname() == "GND"
             and t.GetLength() <= pcbnew.FromMM(STUB_MAX_MM)]
    vias = {}
    for via in board.Tracks():
        if isinstance(via, pcbnew.PCB_VIA) and via.GetNetname() == "GND":
            vias[(via.GetPosition().x, via.GetPosition().y)] = via

    groups = []
    for stub in stubs:
        for end, other in ((stub.GetStart(), stub.GetEnd()),
                           (stub.GetEnd(), stub.GetStart())):
            via = vias.get((end.x, end.y))
            if via is None:
                continue
            pad = None
            for footprint in board.Footprints():
                for candidate in footprint.Pads():
                    if candidate.GetNetname() == "GND" \
                            and candidate.HitTest(other):
                        pad = candidate
            groups.append((via, stub, pad, other))
            break
    return groups


def relocate(board, model, rules, via, stub, anchor):
    """Move a stitching via and its stub to the nearest clear position."""
    net = "GND"
    half_via = via.GetWidth(pcbnew.F_Cu) / 2.0
    half_stub = stub.GetWidth() / 2.0
    layers = [stub.GetLayer()]
    via_layers = [pcbnew.F_Cu, pcbnew.B_Cu]
    origin = (anchor.x, anchor.y)

    for radius in SEARCH_RADII:
        for step in range(SEARCH_STEPS):
            angle = 2.0 * math.pi * step / SEARCH_STEPS
            target = (origin[0] + pcbnew.FromMM(radius) * math.cos(angle),
                      origin[1] + pcbnew.FromMM(radius) * math.sin(angle))
            if model.conflict(net, via_layers, target, target, half_via,
                              drill=via.GetDrill()):
                continue
            if model.conflict(net, layers, origin, target, half_stub):
                continue
            return target
    return None


def mmv(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(d.PAGE_CX + x), pcbnew.FromMM(d.PAGE_CY - y))


def find_track(board, net, start, end, tol=0.05):
    """The track on `net` running between two board-frame points."""
    for track in board.Tracks():
        if isinstance(track, pcbnew.PCB_VIA) or track.GetNetname() != net:
            continue
        a, b = board_xy(track.GetStart()), board_xy(track.GetEnd())
        for first, second in ((a, b), (b, a)):
            if (abs(first[0] - start[0]) < tol and abs(first[1] - start[1]) < tol
                    and abs(second[0] - end[0]) < tol
                    and abs(second[1] - end[1]) < tol):
                return track
    return None


def find_via(board, net, at, tol=0.05):
    for track in board.Tracks():
        if not isinstance(track, pcbnew.PCB_VIA) or track.GetNetname() != net:
            continue
        p = board_xy(track.GetPosition())
        if abs(p[0] - at[0]) < tol and abs(p[1] - at[1]) < tol:
            return track
    return None


def targeted_repairs(board):
    """The four remaining DRC objections, each fixed at its root."""
    done = []

    # 1. The +5V bus still runs 1.6 mm past the point where the Schottky taps
    #    into it - left over from turning D1 round so its anode faces the fuse.
    #    Trim the tail rather than delete the segment: the rest of it carries
    #    the diode's output along to the bulk capacitor.
    bus = find_track(board, "+5V", (-15.5, -14.6), (-7.45, -14.6))
    if bus is not None:
        bus.SetStart(mmv(-13.9, -14.6))
        done.append("trimmed the dangling 1.6 mm tail off the +5V bus")

    # 2. The supply feed lands on its own via 0.5 mm from the ring's existing
    #    one, which puts two 0.3 mm drills 0.204 mm apart. They are the same
    #    net, so drop the feed's via and run the radial leg into the ring's.
    ring_via = find_via(board, "+3V3A", (33.776, 27.093))
    feed_via = find_via(board, "+3V3A", (34.089, 26.699))
    rise = find_track(board, "+3V3A", (17.62, 13.8), (34.089, 26.699))
    joiner = find_track(board, "+3V3A", (34.089, 26.699), (33.902, 26.935))
    if None not in (ring_via, feed_via, rise, joiner):
        rise.SetEnd(ring_via.GetPosition())
        board.Remove(joiner)
        board.Remove(feed_via)
        done.append("merged the supply feed's landing via into the ring's")

    # 3. The feed threads the module socket at 0.4 mm wide, which needs 1.30 mm
    #    of the 1.27 mm between two pins. +3V3A is a POWER net, so it wants
    #    0.25 mm clearance, not the 0.15 mm the generator assumed. At 0.25 mm
    #    it fits with margin and still carries thirty times the ring's load.
    climb = find_track(board, "+3V3A", (17.62, -13.0), (17.62, 13.8))
    if climb is not None:
        climb.SetWidth(pcbnew.FromMM(0.25))
        done.append("narrowed the socket crossing of the +3V3A feed to 0.25 mm")

    # 4. PDM_D6 runs 1.11 mm from the centre of an unused Pi header pin and
    #    needs 1.15 mm. Step the lower half of the run 0.2 mm further out;
    #    the top of it has to stay on 16.35 mm because that is its own pin.
    spoke = find_track(board, "PDM_D6", (16.35, -37.872), (16.35, -15.23))
    if spoke is not None:
        layer, width, net = spoke.GetLayer(), spoke.GetWidth(), spoke.GetNet()
        board.Remove(spoke)
        for a, b in (((16.35, -37.872), (16.55, -37.672)),
                     ((16.55, -37.672), (16.55, -25.0)),
                     ((16.55, -25.0), (16.35, -24.8)),
                     ((16.35, -24.8), (16.35, -15.23))):
            piece = pcbnew.PCB_TRACK(board)
            piece.SetStart(mmv(*a))
            piece.SetEnd(mmv(*b))
            piece.SetWidth(width)
            piece.SetLayer(layer)
            piece.SetNet(net)
            board.Add(piece)
        done.append("stepped PDM_D6 clear of the unused Pi header pin")
    return done


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board = pcbnew.LoadBoard(os.path.join(root, BOARD))
    rules = Rules(root)
    model = Model(board, rules)

    fixed, failed = [], []
    for via, stub, pad, anchor in find_stitching_groups(board):
        model.refresh(ignore=(via, stub))
        position = via.GetPosition()
        bad = model.conflict("GND", [pcbnew.F_Cu, pcbnew.B_Cu],
                             (position.x, position.y), (position.x, position.y),
                             via.GetWidth(pcbnew.F_Cu) / 2.0, drill=via.GetDrill())
        start, end = stub.GetStart(), stub.GetEnd()
        bad = bad or model.conflict("GND", [stub.GetLayer()],
                                    (start.x, start.y), (end.x, end.y),
                                    stub.GetWidth() / 2.0)
        if bad is None:
            continue
        target = relocate(board, model, rules, via, stub, anchor)
        where = board_xy(position)
        if target is None:
            failed.append((where, bad))
            continue
        moved = pcbnew.VECTOR2I(int(target[0]), int(target[1]))
        via.SetPosition(moved)
        stub.SetStart(anchor)
        stub.SetEnd(moved)
        fixed.append((where, board_xy(moved), bad))

    print(f"ground stitching vias moved: {len(fixed)}")
    for old, new, why in fixed:
        print(f"   {old} -> {new}   (was too close to {why})")
    if failed:
        print(f"could not place: {len(failed)}")
        for where, why in failed:
            print(f"   {where} still conflicts with {why}")

    for note in targeted_repairs(board):
        print(f"   {note}")

    board.Save(os.path.join(root, BOARD))
    print("saved")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
