"""Synthetic KiCad fixtures, built in memory, independent of any real board.

These exist so the geometry and gate logic can be tested against shapes whose
correct answer is known analytically, rather than against one board that the
checkers might have been tuned to.
"""

from __future__ import annotations

import os
import tempfile

import pcbnew

MM = pcbnew.FromMM


def new_board(layers=2, size_mm=20.0):
    board = pcbnew.BOARD()
    board.SetCopperLayerCount(layers)
    ds = board.GetDesignSettings()
    ds.SetCopperLayerCount(layers)
    half = size_mm / 2.0
    pts = [(-half, -half), (half, -half), (half, half), (-half, half), (-half, -half)]
    for a, b in zip(pts, pts[1:]):
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetStart(pcbnew.VECTOR2I(MM(100 + a[0]), MM(100 + a[1])))
        seg.SetEnd(pcbnew.VECTOR2I(MM(100 + b[0]), MM(100 + b[1])))
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetWidth(MM(0.1))
        board.Add(seg)
    return board


def add_net(board, name):
    net = pcbnew.NETINFO_ITEM(board, name)
    board.Add(net)
    return net


def add_pad_footprint(board, ref, x_mm, y_mm, pad_shape, size_mm,
                      rotation_deg=0.0, net=None, mask_margin_mm=None,
                      flipped=False, roundrect_ratio=0.25):
    """A one-pad SMD footprint at a known place, rotation and mask margin."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(ref)
    board.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pad_shape)
    pad.SetSize(pcbnew.VECTOR2I(MM(size_mm[0]), MM(size_mm[1])))
    if pad_shape == pcbnew.PAD_SHAPE_ROUNDRECT:
        pad.SetRoundRectRadiusRatio(roundrect_ratio)
    pad.SetLayerSet(pad.SMDMask())
    if mask_margin_mm is not None:
        pad.SetLocalSolderMaskMargin(MM(mask_margin_mm))
    if net is not None:
        pad.SetNet(net)
    fp.Add(pad)
    fp.SetOrientationDegrees(rotation_deg)
    if flipped:
        fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    return fp, pad


def add_via(board, x_mm, y_mm, net=None, diameter_mm=0.45, drill_mm=0.30,
            tented=True):
    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    via.SetWidth(MM(diameter_mm))
    via.SetDrill(MM(drill_mm))
    via.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
    mode = pcbnew.TENTING_MODE_TENTED if tented else pcbnew.TENTING_MODE_NOT_TENTED
    via.SetFrontTentingMode(mode)
    via.SetBackTentingMode(mode)
    if net is not None:
        via.SetNet(net)
    board.Add(via)
    return via


def add_track(board, a_mm, b_mm, net=None, layer=None, width_mm=0.2):
    t = pcbnew.PCB_TRACK(board)
    t.SetStart(pcbnew.VECTOR2I(MM(a_mm[0]), MM(a_mm[1])))
    t.SetEnd(pcbnew.VECTOR2I(MM(b_mm[0]), MM(b_mm[1])))
    t.SetWidth(MM(width_mm))
    t.SetLayer(pcbnew.F_Cu if layer is None else layer)
    if net is not None:
        t.SetNet(net)
    board.Add(t)
    return t


def save(board, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    board.Save(path)
    return path


def tempdir(name):
    d = os.path.join(tempfile.gettempdir(), "pcbqa_synth", name)
    os.makedirs(d, exist_ok=True)
    return d
