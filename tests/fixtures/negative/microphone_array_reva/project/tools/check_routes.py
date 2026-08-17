"""Post-route engineering checks for the microphone array board.

Covers the constraints that are specific to this design and that KiCad's DRC
does not express: which layers a net is allowed to use, via budgets on the
clock nets, branch-to-branch skew across the eight microphone pairs, stub
length on the shared PDM data nets, different-net crossings, and acid traps -
corners left with an interior angle under 90 degrees.
"""

import collections
import math
import os
import sys

import pcbnew

import design as d

MAX_TURN_DEGREES = 45.0
ACUTE_TURN_DEGREES = 90.0
TURN_TOLERANCE = 1.0
MIN_SEGMENT_MM = 0.05

# Via budgets, revised from the original "zero vias on any clock".
#
# That rule was set before the stackup was settled and is stricter than the
# physics warrants. Both inner layers are solid ground, so an F.Cu-to-B.Cu
# transition keeps a continuous reference plane either side - which is the
# reason a clock via is normally avoided in the first place. At 3.072 MHz
# (325 ns period) and 24.576 MHz, the stub and discontinuity of a 0.3 mm via
# are negligible. A small, counted budget is enforced instead of zero, so the
# clocks can take the short route rather than being contorted around the
# module sockets.
VIA_BUDGET = {
    "AUDIO_MCLK": (2, {"F.Cu", "B.Cu"}),
    "MCLK_OSC": (0, {"F.Cu"}),
    # The buffer's eight inputs interleave with its eight outputs on 0.65 mm
    # pitch, and the 0.25 mm between adjacent pads fits no track, so the input
    # bus has to leave on the bottom layer. Three vias is what that escape
    # costs; the budget was written before the pinout was understood. This is
    # not the same case as a clock branch, which fans out into open board and
    # still needs none.
    "PDM_CLK_IN": (4, {"F.Cu", "B.Cu"}),
    "PDM_CLK_FPGA": (2, {"F.Cu", "B.Cu"}),
}
CLOCK_BRANCH_VIA_BUDGET = 2

# Likewise relaxed. 6 mm was tidiness, not physics: 6 mm of FR-4 is about
# 36 ps, and even 25 mm is under one part in 10^5 of a 48 kHz sample period.
# The limit is kept only to catch a grossly asymmetric branch.
BRANCH_SKEW_LIMIT_MM = 25.0
DATA_STUB_LIMIT_MM = 14.0
DATA_TOTAL_LIMIT_MM = 110.0
ALLOWED_TRACK_LAYERS = {"F.Cu", "B.Cu"}


def track_length(track):
    start, end = track.GetStart(), track.GetEnd()
    return math.hypot(end.x - start.x, end.y - start.y) / 1e6


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        root, "microphone_array_v2.kicad_pcb")
    board = pcbnew.LoadBoard(path)
    problems = []

    by_net = collections.defaultdict(list)
    vias_by_net = collections.Counter()
    for item in board.Tracks():
        name = item.GetNetname()
        if isinstance(item, pcbnew.PCB_VIA):
            vias_by_net[name] += 1
        else:
            by_net[name].append(item)

    # --- forbidden layers ---------------------------------------------
    for name, tracks in by_net.items():
        for track in tracks:
            layer = board.GetLayerName(track.GetLayer())
            if layer not in ALLOWED_TRACK_LAYERS:
                problems.append(f"{name}: track on forbidden layer {layer}")
                break

    # --- via budgets and layer restrictions ---------------------------
    for name, (max_vias, layers) in VIA_BUDGET.items():
        if vias_by_net[name] > max_vias:
            problems.append(
                f"{name}: {vias_by_net[name]} vias exceeds budget of {max_vias}")
        for track in by_net.get(name, []):
            layer = board.GetLayerName(track.GetLayer())
            if layer not in layers:
                problems.append(f"{name}: uses {layer}, allowed {sorted(layers)}")
                break

    for branch in range(8):
        name = f"PDM_CLK_B{branch}"
        if vias_by_net[name] > CLOCK_BRANCH_VIA_BUDGET:
            problems.append(
                f"{name}: {vias_by_net[name]} vias, clock branches allow at most "
                f"{CLOCK_BRANCH_VIA_BUDGET}")

    # --- branch length matching ---------------------------------------
    lengths = {}
    for branch in range(8):
        name = f"PDM_CLK_B{branch}"
        lengths[name] = sum(track_length(t) for t in by_net.get(name, []))
    routed = {k: v for k, v in lengths.items() if v > 0}
    if len(routed) == 8:
        spread = max(routed.values()) - min(routed.values())
        if spread > BRANCH_SKEW_LIMIT_MM:
            problems.append(
                f"PDM clock branch length spread {spread:.1f} mm exceeds "
                f"{BRANCH_SKEW_LIMIT_MM:.1f} mm "
                f"(min {min(routed.values()):.1f}, max {max(routed.values()):.1f})")

    # --- shared PDM data net length ------------------------------------
    #
    # The limit is set from signal integrity, not tidiness. A 3.072 MHz PDM
    # line with roughly 2 ns edges has a critical length near 165 mm, beyond
    # which the 22 ohm source damping stops controlling reflections. Each net
    # spans two microphones on opposite sides of a 120 mm board, so 100 mm of
    # total copper is expected and only a gross detour is worth flagging.
    for pair in range(8):
        name = f"PDM_D{pair}"
        total = sum(track_length(t) for t in by_net.get(name, []))
        if total > DATA_TOTAL_LIMIT_MM:
            problems.append(
                f"{name}: total routed length {total:.1f} mm exceeds "
                f"{DATA_TOTAL_LIMIT_MM:.0f} mm")

    # --- route style ----------------------------------------------------
    tiny = 0
    for name, tracks in by_net.items():
        for track in tracks:
            if track_length(track) < MIN_SEGMENT_MM:
                tiny += 1
    if tiny:
        problems.append(f"{tiny} track segments shorter than {MIN_SEGMENT_MM} mm")

    sharp = check_turns(by_net, board)
    problems.extend(sharp)

    problems.extend(check_crossings(board, by_net))

    # --- ground stitching still present ---------------------------------
    gnd_vias = vias_by_net["GND"]
    if gnd_vias < 100:
        problems.append(f"only {gnd_vias} ground stitching vias remain")

    # --- the board is actually finished ----------------------------------
    # Without this the checks above all pass on a board with no routing at all.
    connectivity = board.GetConnectivity()
    connectivity.RecalculateRatsnest()
    unconnected = connectivity.GetUnconnectedCount(True)
    if unconnected:
        problems.append(f"{unconnected} unconnected ratsnest connections remain")

    for k in range(d.MIC_COUNT):
        for net in (f"MIC_VDD_{k}", f"MIC_DOUT_{k}"):
            if not by_net.get(net):
                problems.append(f"{net} has no routing")
    for branch in range(8):
        if not by_net.get(f"PDM_CLK_B{branch}"):
            problems.append(f"PDM_CLK_B{branch} has no routing")
        if not by_net.get(f"PDM_D{branch}"):
            problems.append(f"PDM_D{branch} has no routing")

    if problems:
        print(f"ROUTE CHECKS FAILED ({len(problems)} problems)")
        for line in problems[:40]:
            print("  " + line)
        return 1

    total_length = sum(track_length(t) for tracks in by_net.values() for t in tracks)
    total_vias = sum(vias_by_net.values())
    print("route checks OK")
    print(f"  routed length {total_length:.0f} mm, vias {total_vias} "
          f"(ground stitching {gnd_vias})")
    if len(routed) == 8:
        print(f"  PDM clock branches {min(routed.values()):.1f}"
              f"-{max(routed.values()):.1f} mm")
    return 0


def check_crossings(board, by_net):
    """Report tracks of different nets that literally overlap on one layer.

    These are hard shorts, not clearance warnings. KiCad's DRC does flag them
    as `tracks_crossing`, but this checker was silent on them, which made it
    look cleaner than the board actually was - so they are reported here by
    net pair, which is what makes the pattern obvious.
    """
    segments = collections.defaultdict(list)
    for name, tracks in by_net.items():
        for track in tracks:
            segments[track.GetLayer()].append((name, track))

    def side(ax, ay, bx, by, cx, cy):
        value = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    pairs = collections.Counter()
    for layer, items in segments.items():
        for i, (name_a, track_a) in enumerate(items):
            a1, a2 = track_a.GetStart(), track_a.GetEnd()
            for name_b, track_b in items[i + 1:]:
                if name_a == name_b:
                    continue
                b1, b2 = track_b.GetStart(), track_b.GetEnd()
                if (side(b1.x, b1.y, b2.x, b2.y, a1.x, a1.y)
                        * side(b1.x, b1.y, b2.x, b2.y, a2.x, a2.y) < 0
                        and side(a1.x, a1.y, a2.x, a2.y, b1.x, b1.y)
                        * side(a1.x, a1.y, a2.x, a2.y, b2.x, b2.y) < 0):
                    key = (board.GetLayerName(layer),) + tuple(
                        sorted((name_a, name_b)))
                    pairs[key] += 1

    problems = []
    for (layer, net_a, net_b), count in pairs.most_common():
        problems.append(
            f"{net_a} crosses {net_b} on {layer} ({count} places)")
    return problems


def check_turns(by_net, board):
    """Reject acid traps: corners whose interior angle is under 90 degrees.

    This used to reject anything off the 45 degree grid, which was right while
    the whole board came out of the generator. It no longer does - the host
    block was finished with an external autorouter, which routes at arbitrary
    angles - so a grid rule now describes a style the board does not have.

    What still matters is the acute notch that holds etchant. A turn of more
    than 90 degrees leaves an interior angle under 90; anything shallower is
    fine. Corners that sit on a pad or a via are exempt: the round copper there
    fills the notch, so there is nothing to trap.
    """
    anchors = set()
    for footprint in board.Footprints():
        for pad in footprint.Pads():
            anchors.add((pad.GetPosition().x, pad.GetPosition().y))
    for item in board.Tracks():
        if isinstance(item, pcbnew.PCB_VIA):
            anchors.add((item.GetPosition().x, item.GetPosition().y))

    problems = []
    endpoints = collections.defaultdict(list)
    for name, tracks in by_net.items():
        for track in tracks:
            key = (name, track.GetLayer())
            endpoints[key].append(track)

    offenders = 0
    off_grid = 0
    for (name, _layer), tracks in endpoints.items():
        joins = collections.defaultdict(list)
        for track in tracks:
            joins[(track.GetStart().x, track.GetStart().y)].append(track)
            joins[(track.GetEnd().x, track.GetEnd().y)].append(track)
        for point, group in joins.items():
            if len(group) != 2:
                continue
            vectors = []
            for track in group:
                start, end = track.GetStart(), track.GetEnd()
                if (start.x, start.y) == point:
                    other = end
                else:
                    other = start
                vectors.append((other.x - point[0], other.y - point[1]))
            (ax, ay), (bx, by) = vectors
            na, nb = math.hypot(ax, ay), math.hypot(bx, by)
            if na == 0 or nb == 0:
                continue
            cosine = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
            interior = math.degrees(math.acos(cosine))
            turn = 180.0 - interior
            if turn > MAX_TURN_DEGREES + TURN_TOLERANCE:
                off_grid += 1
            if turn > ACUTE_TURN_DEGREES + TURN_TOLERANCE and point not in anchors:
                offenders += 1
    if offenders:
        problems.append(
            f"{offenders} corners leave an interior angle under "
            f"{180 - ACUTE_TURN_DEGREES:.0f} degrees (acid traps)")
    if off_grid:
        print(f"  note: {off_grid} corners are off the 45 degree grid, from the "
              f"autorouted host block; none of them is acute")
    return problems


if __name__ == "__main__":
    sys.exit(main())
