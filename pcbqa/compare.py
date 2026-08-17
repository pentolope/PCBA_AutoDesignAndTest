"""Compare two boards by geometry, not by bytes.

Two clean builds of the same inputs produce files that differ in every UUID and
timestamp and are identical as copper. This reads both boards through pcbnew
and compares what a fabricator would see: where the footprints sit, what the
outline is, and every track and via rounded to a micron.

    "C:/Program Files/KiCad/10.0/bin/python.exe" tools/compare_boards.py A B
"""

from __future__ import annotations

import collections
import math
import sys

import pcbnew

LAYERS = {pcbnew.F_Cu: "F.Cu", pcbnew.B_Cu: "B.Cu"}


def survey(path):
    board = pcbnew.LoadBoard(path)
    tracks, vias = [], []
    length = collections.Counter()
    for item in board.Tracks():
        net = item.GetNetname()
        if isinstance(item, pcbnew.PCB_VIA):
            pos = item.GetPosition()
            vias.append((net, round(pos.x / 1e6, 3), round(pos.y / 1e6, 3),
                         round(item.GetWidth(pcbnew.F_Cu) / 1e6, 3),
                         round(item.GetDrill() / 1e6, 3)))
            continue
        start, end = item.GetStart(), item.GetEnd()
        ends = tuple(sorted(((round(start.x / 1e6, 3), round(start.y / 1e6, 3)),
                             (round(end.x / 1e6, 3), round(end.y / 1e6, 3)))))
        tracks.append((net, LAYERS.get(item.GetLayer(), str(item.GetLayer())),
                       round(item.GetWidth() / 1e6, 3)) + ends)
        length[net] += math.hypot(end.x - start.x, end.y - start.y) / 1e6

    footprints = []
    for fp in board.Footprints():
        pos = fp.GetPosition()
        footprints.append((fp.GetReference(), round(pos.x / 1e6, 4),
                           round(pos.y / 1e6, 4),
                           round(fp.GetOrientationDegrees(), 3),
                           fp.IsFlipped()))
    outline = []
    for shape in board.GetDrawings():
        if shape.GetLayer() == pcbnew.Edge_Cuts:
            outline.append((shape.GetShape(),
                            round(shape.GetStart().x / 1e6, 4),
                            round(shape.GetStart().y / 1e6, 4),
                            round(shape.GetEnd().x / 1e6, 4),
                            round(shape.GetEnd().y / 1e6, 4)))
    return {
        "footprints": sorted(footprints),
        "outline": sorted(outline),
        "tracks": sorted(tracks),
        "vias": sorted(vias),
        "copper_layers": board.GetCopperLayerCount(),
        "nets": sorted(str(name) for name
                       in board.GetNetInfo().NetsByName().keys()),
        "length_mm": {net: round(value, 3) for net, value in length.items()},
    }


def main(argv):
    if len(argv) != 3:
        print(__doc__)
        return 2
    one, two = survey(argv[1]), survey(argv[2])
    differences = []
    for key in one:
        if one[key] == two[key]:
            print("  same  {:<14} {}".format(key, _size(one[key])))
            continue
        differences.append(key)
        print("  DIFF  {:<14} {} vs {}".format(key, _size(one[key]),
                                               _size(two[key])))
        if isinstance(one[key], list):
            only_a = [row for row in one[key] if row not in two[key]]
            only_b = [row for row in two[key] if row not in one[key]]
            for row in only_a[:5]:
                print("          only in A: {}".format(row))
            for row in only_b[:5]:
                print("          only in B: {}".format(row))
    print("identical" if not differences
          else "differs in: " + ", ".join(differences))
    return 0 if not differences else 1


def _size(value):
    if isinstance(value, list):
        return "{} items".format(len(value))
    if isinstance(value, dict):
        return "{} nets".format(len(value))
    return str(value)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
