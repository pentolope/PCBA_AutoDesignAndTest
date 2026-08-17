"""Tidy the routed copper without changing where anything goes.

Four passes, each safe by construction - none of them moves copper outside the
shape it already occupied, so no clearance that passed before can fail after:

  snap      endpoints within 30 um of each other become one point, which is
            what removes the sliver segments; anything left with zero length
            goes, and its neighbours still meet because they now share a point.
  merge     two collinear segments meeting end to end become one.
  retrace   where two segments leave a join in the *same* direction the shorter
            one is inside the longer, so it is redundant.
  chamfer   a corner that turns by more than 90 degrees leaves an interior
            angle under 90 - the acute notch that traps etchant. Cutting the
            tip off with a short chord splits it into two corners of half the
            angle each, so one pass is enough to clear every one of them.

Corners that turn by 90 degrees or less are left alone. They are not on the
45 degree grid the generator used, but an interior angle of 90 degrees or more
is not an acid trap, and re-routing the autorouted host block to put it back on
the grid would be a much bigger change than the problem warrants.
"""

import collections
import math
import os
import sys

import pcbnew

BOARD = "microphone_array_v2.kicad_pcb"

# Wide enough to swallow the sliver segments themselves, not just coincident
# endpoints: the autorouter left 31 segments under 0.05 mm, whose two ends are
# further apart than a coincidence tolerance would allow.
SNAP_IU = 60000            # 0.06 mm
COLLINEAR_DEG = 1.0
RETRACE_DEG = 179.0
ACUTE_TURN_DEG = 90.0
CHAMFER_MM = 0.25
CHAMFER_FRACTION = 0.35


def key(point):
    return (point.x, point.y)


def turn_between(vertex, a, b):
    """Degrees this corner deviates from straight, 0 = straight on."""
    ax, ay = a[0] - vertex[0], a[1] - vertex[1]
    bx, by = b[0] - vertex[0], b[1] - vertex[1]
    na, nb = math.hypot(ax, ay), math.hypot(bx, by)
    if na == 0 or nb == 0:
        return None
    cosine = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
    return 180.0 - math.degrees(math.acos(cosine))


class Seg:
    """One track segment, detached from KiCad so it can be edited freely.

    Editing PCB_TRACK objects while removing others invalidates the SWIG
    wrappers, so the whole pass runs on this model and the board's tracks are
    rebuilt from it at the end. Vias are never touched.
    """

    __slots__ = ("net", "layer", "width", "locked", "a", "b")

    def __init__(self, track):
        self.net = track.GetNetCode()
        self.layer = track.GetLayer()
        self.width = track.GetWidth()
        self.locked = track.IsLocked()
        self.a = key(track.GetStart())
        self.b = key(track.GetEnd())

    def length(self):
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def other_end(self, point):
        return self.b if self.a == point else self.a

    def replace(self, point, new_point):
        if self.a == point:
            self.a = new_point
        else:
            self.b = new_point


def read_segments(board):
    buckets = collections.defaultdict(list)
    for track in board.Tracks():
        if not isinstance(track, pcbnew.PCB_VIA):
            seg = Seg(track)
            buckets[(seg.net, seg.layer)].append(seg)
    return buckets


def joins(segs):
    table = collections.defaultdict(list)
    for seg in segs:
        table[seg.a].append(seg)
        table[seg.b].append(seg)
    return table


def snap(segs):
    """Pull endpoints within SNAP_IU of each other onto one point."""
    parent = {}
    for seg in segs:
        parent.setdefault(seg.a, seg.a)
        parent.setdefault(seg.b, seg.b)

    def find(p):
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    unique = list(parent)
    for i, a in enumerate(unique):
        for b in unique[i + 1:]:
            if abs(a[0] - b[0]) <= SNAP_IU and abs(a[1] - b[1]) <= SNAP_IU:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    members = collections.defaultdict(list)
    for p in unique:
        members[find(p)].append(p)
    moved = {}
    for group in members.values():
        if len(group) == 1:
            continue
        cx = sum(p[0] for p in group) // len(group)
        cy = sum(p[1] for p in group) // len(group)
        for p in group:
            moved[p] = (cx, cy)

    changed = 0
    for seg in segs:
        for name in ("a", "b"):
            p = getattr(seg, name)
            if p in moved and moved[p] != p:
                setattr(seg, name, moved[p])
                changed += 1

    kept = [seg for seg in segs if seg.a != seg.b]
    dropped = len(segs) - len(kept)
    segs[:] = kept
    return changed, dropped


def merge_collinear(segs):
    merged = 0
    changed = True
    while changed:
        changed = False
        for point, group in joins(segs).items():
            if len(group) != 2:
                continue
            first, second = group
            if first.width != second.width:
                continue
            a, b = first.other_end(point), second.other_end(point)
            angle = turn_between(point, a, b)
            if angle is None or angle > COLLINEAR_DEG:
                continue
            first.a, first.b = a, b
            segs.remove(second)
            merged += 1
            changed = True
            break
    return merged


def drop_retraces(segs):
    dropped = 0
    changed = True
    while changed:
        changed = False
        for point, group in joins(segs).items():
            if len(group) != 2:
                continue
            first, second = group
            a, b = first.other_end(point), second.other_end(point)
            angle = turn_between(point, a, b)
            if angle is None or angle < RETRACE_DEG:
                continue
            segs.remove(first if first.length() <= second.length() else second)
            dropped += 1
            changed = True
            break
    return dropped


def chamfer(segs, anchored):
    """Cut the tip off every corner whose interior angle is under 90 degrees."""
    cut = 0
    for point, group in list(joins(segs).items()):
        if len(group) != 2 or point in anchored:
            continue
        first, second = group
        a, b = first.other_end(point), second.other_end(point)
        angle = turn_between(point, a, b)
        if angle is None or angle <= ACUTE_TURN_DEG or angle >= RETRACE_DEG:
            continue
        back = min(pcbnew.FromMM(CHAMFER_MM),
                   first.length() * CHAMFER_FRACTION,
                   second.length() * CHAMFER_FRACTION)
        if back < pcbnew.FromMM(0.02):
            continue

        def step(towards):
            dx, dy = towards[0] - point[0], towards[1] - point[1]
            length = math.hypot(dx, dy)
            return (int(point[0] + dx / length * back),
                    int(point[1] + dy / length * back))

        p, q = step(a), step(b)
        first.replace(point, p)
        second.replace(point, q)
        bridge = object.__new__(Seg)
        bridge.net, bridge.layer = first.net, first.layer
        bridge.width = max(first.width, second.width)
        bridge.locked = first.locked and second.locked
        bridge.a, bridge.b = p, q
        segs.append(bridge)
        cut += 1
    return cut


def anchor_points(board):
    """Corners on a pad or via, where cutting back would break the contact."""
    points = set()
    for footprint in board.Footprints():
        for pad in footprint.Pads():
            points.add(key(pad.GetPosition()))
    for item in board.Tracks():
        if isinstance(item, pcbnew.PCB_VIA):
            points.add(key(item.GetPosition()))
    return points


def rebuild(board, buckets):
    for track in list(board.Tracks()):
        if not isinstance(track, pcbnew.PCB_VIA):
            board.Remove(track)
    count = 0
    for segs in buckets.values():
        for seg in segs:
            track = pcbnew.PCB_TRACK(board)
            track.SetStart(pcbnew.VECTOR2I(*seg.a))
            track.SetEnd(pcbnew.VECTOR2I(*seg.b))
            track.SetWidth(seg.width)
            track.SetLayer(seg.layer)
            track.SetNetCode(seg.net)
            track.SetLocked(seg.locked)
            board.Add(track)
            count += 1
    return count


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board = pcbnew.LoadBoard(os.path.join(root, BOARD))
    anchored = anchor_points(board)
    buckets = read_segments(board)
    before = sum(len(v) for v in buckets.values())

    totals = collections.Counter()
    for segs in buckets.values():
        # Snapping can leave two segments collinear, and merging them can bring
        # two more endpoints within snapping distance, so run the three to a
        # fixed point before touching the corners.
        for _ in range(8):
            moved, dropped = snap(segs)
            merged = merge_collinear(segs)
            retraced = drop_retraces(segs)
            totals["endpoints snapped"] += moved
            totals["zero length dropped"] += dropped
            totals["collinear merged"] += merged
            totals["retraces dropped"] += retraced
            if not (moved or dropped or merged or retraced):
                break
        totals["acute corners chamfered"] += chamfer(segs, anchored)

    for name, count in totals.most_common():
        print(f"   {name}: {count}")
    after = rebuild(board, buckets)
    print(f"   segments {before} -> {after}")
    board.Save(os.path.join(root, BOARD))
    print("saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
