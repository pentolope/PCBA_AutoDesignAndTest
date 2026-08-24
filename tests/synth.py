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


def add_two_pad_footprint(board, ref, x_mm, y_mm, pitch_mm, nets,
                          size_mm=(0.5, 0.5), value=""):
    """A two-pad SMD part straddling two nets, for a series-component crossing.

    `nets` is (net for pad 1, net for pad 2). Pads sit at x_mm -/+ pitch/2, so
    the part's own footprint occupies a known width and the copper either side
    of it is measurable independently.
    """
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(value or ref)
    board.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    pads = []
    for index, (number, net) in enumerate(zip(("1", "2"), nets)):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetShape(pcbnew.PAD_SHAPE_RECT)
        pad.SetSize(pcbnew.VECTOR2I(MM(size_mm[0]), MM(size_mm[1])))
        pad.SetLayerSet(pad.SMDMask())
        offset = (-pitch_mm / 2.0) if index == 0 else (pitch_mm / 2.0)
        pad.SetPosition(pcbnew.VECTOR2I(MM(x_mm + offset), MM(y_mm)))
        if net is not None:
            pad.SetNet(net)
        fp.Add(pad)
        pads.append(pad)
    return fp, pads


def add_through_hole_footprint(board, ref, x_mm, y_mm, net=None,
                               pad_mm=1.2, drill_mm=0.6, numbers=("1",),
                               nets=None, copper_layers=None):
    """A plated through-hole part: pads on every copper layer, with a barrel.

    `numbers` may repeat a pad number, which KiCad permits and which the path
    layer has to cope with; `nets` gives one net per pad when they differ.
    """
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(ref)
    board.Add(fp)
    fp.SetPosition(pcbnew.VECTOR2I(MM(x_mm), MM(y_mm)))
    pads = []
    for index, number in enumerate(numbers):
        pad = pcbnew.PAD(fp)
        pad.SetNumber(number)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetSize(pcbnew.VECTOR2I(MM(pad_mm), MM(pad_mm)))
        pad.SetDrillSize(pcbnew.VECTOR2I(MM(drill_mm), MM(drill_mm)))
        if copper_layers is None:
            pad.SetLayerSet(pad.PTHMask())
        else:
            # A padstack that carries copper on only some layers. The hole
            # still goes through the board - which is the distinction the
            # toolkit has to get right.
            layer_set = pcbnew.LSET()
            for layer in copper_layers:
                layer_set.addLayer(layer)
            pad.SetLayerSet(layer_set)
        pad.SetPosition(pcbnew.VECTOR2I(MM(x_mm + index * 2.0), MM(y_mm)))
        chosen = (nets[index] if nets else net)
        if chosen is not None:
            pad.SetNet(chosen)
        fp.Add(pad)
        pads.append(pad)
    return fp, pads


def add_zone(board, net, layers, rect_mm, fill=True):
    """A filled-copper zone on one or more layers, so a plane is a real pour."""
    zone = pcbnew.ZONE(board)
    layer_set = pcbnew.LSET()
    for layer in layers:
        layer_set.addLayer(layer)
    zone.SetLayerSet(layer_set)
    if net is not None:
        zone.SetNet(net)
    outline = pcbnew.SHAPE_POLY_SET()
    outline.NewOutline()
    x0, y0, x1, y1 = rect_mm
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        outline.Append(MM(x), MM(y))
    # SetOutline takes ownership of the polygon. Without disowning it here,
    # Python frees it when this function returns and the zone is left holding
    # a dangling pointer - which segfaults the interpreter a few boards later,
    # nowhere near the code that caused it.
    outline.thisown = 0
    zone.SetOutline(outline)
    board.Add(zone)

    # Fill it. A zone that has never been filled carries no filled polygons,
    # and a coverage question answered from an unfilled zone is a question
    # answered about nothing - which is exactly the state the toolkit reports
    # rather than guesses at. These layers carry no other copper, so the fill
    # of a rectangle is that rectangle, and setting it directly keeps the
    # fixture deterministic instead of depending on the filler's clearance
    # arithmetic.
    if not fill:
        return zone
    for layer in layers:
        filled = pcbnew.SHAPE_POLY_SET()
        filled.NewOutline()
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            filled.Append(MM(x), MM(y))
        zone.SetFilledPolysList(layer, filled)
    zone.SetIsFilled(True)
    return zone


def write_physical_stackup(path, layers, copper_finish="None"):
    """Insert a `(stackup ...)` block into a saved board's `(setup ...)`.

    KiCad 10's Python bindings expose the stackup descriptor as an opaque SWIG
    pointer with no accessors, so a fixture cannot build one through pcbnew.
    It is written into the board file directly instead, in exactly the form
    KiCad itself writes, which is also the form `pcbqa.stackup_physical` reads.

    `layers` is a list of dicts: name, type, and optionally thickness_mm,
    material, epsilon_r, loss_tangent.
    """
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    marker = text.find("(setup")
    if marker < 0:
        raise ValueError("{}: no (setup ...) block to insert a stackup "
                         "into".format(path))
    insert = text.index("\n", marker) + 1
    lines = ["\t\t(stackup"]
    for entry in layers:
        lines.append('\t\t\t(layer "{}"'.format(entry["name"]))
        lines.append('\t\t\t\t(type "{}")'.format(entry["type"]))
        if entry.get("thickness_mm") is not None:
            lines.append("\t\t\t\t(thickness {})".format(entry["thickness_mm"]))
        if entry.get("material") is not None:
            lines.append('\t\t\t\t(material "{}")'.format(entry["material"]))
        if entry.get("epsilon_r") is not None:
            lines.append("\t\t\t\t(epsilon_r {})".format(entry["epsilon_r"]))
        if entry.get("loss_tangent") is not None:
            lines.append("\t\t\t\t(loss_tangent {})".format(
                entry["loss_tangent"]))
        lines.append("\t\t\t)")
    lines.append('\t\t\t(copper_finish "{}")'.format(copper_finish))
    lines.append("\t\t\t(dielectric_constraints no)")
    lines.append("\t\t)")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text[:insert] + "\n".join(lines) + "\n" + text[insert:])
    return path


def save(board, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    board.Save(path)
    return path


def tempdir(name):
    d = os.path.join(tempfile.gettempdir(), "pcbqa_synth", name)
    os.makedirs(d, exist_ok=True)
    return d
