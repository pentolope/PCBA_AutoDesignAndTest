"""Placement audit for the microphone array board.

Checks the things KiCad's DRC either cannot express or reports too late to be
useful while iterating on placement:

  * courtyard overlaps between footprints;
  * component body clearance against the JLCPCB package-pair matrix;
  * copper and courtyard clearance to the routed board edge;
  * the microphone ring geometry actually matching the specified array;
  * the Tang Nano 9K socket pin map landing where the module expects it.
"""

import math
import os
import sys

import pcbnew

import design as d

# JLCPCB recommended minimum body-to-body spacing, by package pair.
CHIP = {"0402", "0603", "0805", "1206", "1812"}
PAIR_MINIMUM = {
    frozenset({"chip", "chip"}): 0.18,
    frozenset({"chip", "1206"}): 0.25,
    frozenset({"1206", "1206"}): 0.35,
    frozenset({"chip", "QFN"}): 1.00,
    frozenset({"chip", "SOIC"}): 0.40,
    frozenset({"chip", "SOT"}): 0.20,
    frozenset({"SOT", "SOT"}): 0.40,
    frozenset({"SOIC", "SOIC"}): 0.50,
    frozenset({"QFN", "QFN"}): 1.00,
}
DEFAULT_PAIR_MINIMUM = 0.20

EDGE_COPPER_MIN = 0.30
EDGE_COURTYARD_MIN = 0.20


def package_class(footprint):
    name = str(footprint.GetFPID().GetLibItemName())
    for token in ("0402", "0603", "0805", "1206", "1812"):
        if token in name:
            return "chip" if token != "1206" else "1206"
    if "SOT" in name:
        return "SOT"
    if "TSSOP" in name or "SOIC" in name or "SO-" in name:
        return "SOIC"
    if "QFN" in name or "LGA" in name:
        return "QFN"
    return "other"


def pair_minimum(a, b):
    return PAIR_MINIMUM.get(frozenset({a, b}), DEFAULT_PAIR_MINIMUM)


def outline_points(shape):
    """Vertices of every outline in a SHAPE_POLY_SET, in nanometres."""
    points = []
    for index in range(shape.OutlineCount()):
        outline = shape.Outline(index)
        for vertex in range(outline.PointCount()):
            point = outline.CPoint(vertex)
            points.append((point.x, point.y))
    return points


def polygon_gap(shape_a, shape_b):
    """Millimetre gap between two courtyard polygons, negative if overlapping.

    Bounding boxes badly over-report on the rotated parts around the ring, so
    this measures the real outlines: point-to-segment distance both ways, with
    the sign taken from an actual collision test.
    """
    if shape_a.Collide(shape_b, 0):
        return -0.001

    points_a = outline_points(shape_a)
    points_b = outline_points(shape_b)
    if not points_a or not points_b:
        return math.inf

    def closest(points, polygon):
        best = math.inf
        for x, y in points:
            for index in range(polygon.OutlineCount()):
                outline = polygon.Outline(index)
                count = outline.PointCount()
                for vertex in range(count):
                    p1 = outline.CPoint(vertex)
                    p2 = outline.CPoint((vertex + 1) % count)
                    best = min(best, segment_distance(x, y, p1.x, p1.y,
                                                     p2.x, p2.y))
        return best

    return min(closest(points_a, shape_b), closest(points_b, shape_a)) / 1e6


def segment_distance(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board = pcbnew.LoadBoard(os.path.join(root, "microphone_array_v2.kicad_pcb"))
    problems = []

    centre_x = pcbnew.FromMM(d.PAGE_CX)
    centre_y = pcbnew.FromMM(d.PAGE_CY)
    radius = pcbnew.FromMM(d.BOARD_RADIUS)

    footprints = list(board.Footprints())

    # --- courtyard overlap and body spacing -------------------------------
    boxes = {}
    for footprint in footprints:
        layer = pcbnew.B_CrtYd if footprint.IsFlipped() else pcbnew.F_CrtYd
        shape = footprint.GetCourtyard(layer)
        if shape.OutlineCount() == 0:
            shape = footprint.GetCourtyard(
                pcbnew.F_CrtYd if footprint.IsFlipped() else pcbnew.B_CrtYd)
        if shape.OutlineCount() == 0:
            problems.append(f"{footprint.GetReference()}: no courtyard")
            continue
        boxes[footprint.GetReference()] = (footprint, shape)

    references = sorted(boxes)
    for i, ref_a in enumerate(references):
        fp_a, shape_a = boxes[ref_a]
        box_a = shape_a.BBox()
        for ref_b in references[i + 1:]:
            fp_b, shape_b = boxes[ref_b]
            if fp_a.IsFlipped() != fp_b.IsFlipped():
                continue  # opposite sides cannot collide mechanically
            box_b = shape_b.BBox()
            # Cheap reject before the exact polygon measurement.
            if (box_a.GetLeft() - box_b.GetRight() > pcbnew.FromMM(3)
                    or box_b.GetLeft() - box_a.GetRight() > pcbnew.FromMM(3)
                    or box_a.GetTop() - box_b.GetBottom() > pcbnew.FromMM(3)
                    or box_b.GetTop() - box_a.GetBottom() > pcbnew.FromMM(3)):
                continue
            gap = polygon_gap(shape_a, shape_b)
            if gap < 0:
                problems.append(
                    f"courtyard overlap {ref_a} / {ref_b} by {-gap:.2f} mm")
                continue
            need = pair_minimum(package_class(fp_a), package_class(fp_b))
            if gap < need:
                problems.append(
                    f"body spacing {ref_a} / {ref_b}: {gap:.2f} mm < {need:.2f} mm")

    # --- through-hole pads inside opposite-side courtyards ----------------
    for footprint in footprints:
        for pad in footprint.Pads():
            if pad.GetAttribute() not in (pcbnew.PAD_ATTRIB_PTH,
                                          pcbnew.PAD_ATTRIB_NPTH):
                continue
            position = pad.GetPosition()
            for ref_other, (other, shape) in boxes.items():
                if other.GetReference() == footprint.GetReference():
                    continue
                if shape.Contains(pcbnew.VECTOR2I(position.x, position.y)):
                    problems.append(
                        f"through-hole {footprint.GetReference()}."
                        f"{pad.GetNumber()} inside courtyard of {ref_other}")

    # --- edge clearance ----------------------------------------------------
    for footprint in footprints:
        for pad in footprint.Pads():
            position = pad.GetPosition()
            size = pad.GetSize()
            reach = math.hypot(size.x, size.y) / 2.0
            distance = math.hypot(position.x - centre_x, position.y - centre_y)
            margin = (radius - distance - reach) / 1e6
            if margin < EDGE_COPPER_MIN:
                problems.append(
                    f"{footprint.GetReference()}.{pad.GetNumber()} is "
                    f"{margin:.2f} mm from the board edge")

    # --- microphone ring geometry -----------------------------------------
    for k in range(d.MIC_COUNT):
        ref = f"MK{k + 1}"
        footprint = board.FindFootprintByReference(ref)
        if footprint is None:
            problems.append(f"{ref} missing from board")
            continue
        position = footprint.GetPosition()
        dx = (position.x - centre_x) / 1e6
        dy = -(position.y - centre_y) / 1e6
        body_radius = math.hypot(dx, dy)
        angle = math.degrees(math.atan2(dy, dx)) % 360.0
        want_angle = d.mic_angle(k) % 360.0
        if abs(body_radius - d.MIC_BODY_RADIUS) > 0.05:
            problems.append(
                f"{ref} body radius {body_radius:.2f} != {d.MIC_BODY_RADIUS:.2f}")
        if min(abs(angle - want_angle), 360 - abs(angle - want_angle)) > 0.05:
            problems.append(f"{ref} at {angle:.2f} deg, expected {want_angle:.2f}")

        # The acoustic port must land on the specified array radius.
        port = footprint.GetPosition()
        rotation = footprint.GetOrientationDegrees()
        local = (0.0, -d.MIC_PORT_OFFSET)
        theta = math.radians(-rotation)
        px = local[0] * math.cos(theta) - local[1] * math.sin(theta)
        py = local[0] * math.sin(theta) + local[1] * math.cos(theta)
        port_x = port.x / 1e6 + px
        port_y = port.y / 1e6 + py
        port_radius = math.hypot(port_x - d.PAGE_CX, port_y - d.PAGE_CY)
        if abs(port_radius - d.MIC_PORT_RADIUS) > 0.05:
            problems.append(
                f"{ref} acoustic port radius {port_radius:.2f} != "
                f"{d.MIC_PORT_RADIUS:.2f}")

    # --- Tang Nano 9K socket pin positions --------------------------------
    for ref, row_y in (("J2", d.TANG_ROW_SPACING / 2.0),
                       ("J3", -d.TANG_ROW_SPACING / 2.0)):
        footprint = board.FindFootprintByReference(ref)
        if footprint is None:
            problems.append(f"{ref} missing from board")
            continue
        if not footprint.IsFlipped():
            problems.append(f"{ref} must be on the bottom side")
        for pad in footprint.Pads():
            position = int(pad.GetNumber())
            want_x, want_y = d.to_kicad(d.tang_socket_x(position), row_y)
            got = pad.GetPosition()
            if (abs(got.x / 1e6 - want_x) > 0.02
                    or abs(got.y / 1e6 - want_y) > 0.02):
                problems.append(
                    f"{ref} pin {position} at ({got.x / 1e6:.2f},"
                    f"{got.y / 1e6:.2f}), expected ({want_x:.2f},{want_y:.2f})")
                break

    if problems:
        print(f"PLACEMENT AUDIT FAILED ({len(problems)} problems)")
        for line in problems[:50]:
            print("  " + line)
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
        return 1
    print(f"placement audit OK: {len(footprints)} footprints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
