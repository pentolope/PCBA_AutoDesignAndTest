"""Build the portability fixture: a structurally different KiCad board.

Deliberately unlike Rev A in every structural respect - 2 copper layers instead
of 4, rectangular instead of circular, 30 x 20 mm instead of 120 mm diameter,
three components instead of 124, different reference designators, different net
names, and a different directory. The same validator executable must run on it
with no source change, configured only by its own manifest.
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import pcbnew                       # noqa: E402
from tests import synth             # noqa: E402

from tests import paths                                                    # noqa: E402

TARGET = os.path.join(paths.PORTABILITY_FIXTURE, "widget_b.kicad_pcb")


def build(path=TARGET):
    board = synth.new_board(layers=2, size_mm=0.0)
    # rectangular 30 x 20 outline, origin away from Rev A's 150,150
    pts = [(60.0, 40.0), (90.0, 40.0), (90.0, 60.0), (60.0, 60.0), (60.0, 40.0)]
    for a, b in zip(pts, pts[1:]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(synth.MM(a[0]), synth.MM(a[1])))
        seg.SetEnd(pcbnew.VECTOR2I(synth.MM(b[0]), synth.MM(b[1])))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(synth.MM(0.1))
        board.Add(seg)

    alpha = synth.add_net(board, "ALPHA_BUS")
    bravo = synth.add_net(board, "BRAVO_RET")

    # three parts, references unlike anything in Rev A
    synth.add_pad_footprint(board, "X9", 65.0, 45.0, pcbnew.PAD_SHAPE_RECT,
                            (1.2, 0.8), net=alpha)
    synth.add_pad_footprint(board, "Y3", 75.0, 55.0, pcbnew.PAD_SHAPE_ROUNDRECT,
                            (1.0, 1.0), rotation_deg=22.5, net=alpha)
    synth.add_pad_footprint(board, "Z11", 85.0, 45.0, pcbnew.PAD_SHAPE_CIRCLE,
                            (1.4, 1.4), net=bravo)

    # 45-degree-only routing between X9 and Y3: two collinear diagonal
    # segments, so every junction turns by 0 degrees
    synth.add_track(board, (65.0, 45.0), (70.0, 50.0), net=alpha)
    synth.add_track(board, (70.0, 50.0), (75.0, 55.0), net=alpha)
    # a via well clear of every mask opening
    synth.add_via(board, 80.0, 50.0, net=bravo)
    synth.add_track(board, (85.0, 45.0), (80.0, 50.0), net=bravo)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    board.Save(path)
    return path


if __name__ == "__main__":
    print(build())
