"""Native-board geometry: pad outlines, solder-mask openings, via annuli.

Everything is reconstructed from KiCad's own effective shapes rather than from
size/position arithmetic, so rotation, side mirroring, custom pad primitives,
chamfers and per-pad mask overrides are handled by construction instead of by
special cases. Shapes are shapely polygons in millimetres.

Fail-closed: an unsupported shape raises rather than being skipped.
"""

from __future__ import annotations

import math

try:
    from shapely.geometry import Polygon, MultiPolygon, Point
    from shapely.ops import unary_union
except ImportError as exc:                    # pragma: no cover - environment
    raise RuntimeError(
        "shapely is required for geometric verification; refusing to run "
        "with a reduced-accuracy fallback") from exc

import pcbnew

IU_PER_MM = 1e6


def to_mm(value):
    return value / IU_PER_MM


class UnsupportedGeometry(Exception):
    pass


# ---------------------------------------------------------------------------
# shape extraction
# ---------------------------------------------------------------------------

def _polyset_to_polygons(polyset):
    """Convert a KiCad SHAPE_POLY_SET into shapely polygons (mm)."""
    out = []
    for i in range(polyset.OutlineCount()):
        outline = polyset.Outline(i)
        pts = [(to_mm(outline.CPoint(j).x), to_mm(outline.CPoint(j).y))
               for j in range(outline.PointCount())]
        if len(pts) < 3:
            continue
        holes = []
        for h in range(polyset.HoleCount(i)):
            hole = polyset.Hole(i, h)
            hp = [(to_mm(hole.CPoint(j).x), to_mm(hole.CPoint(j).y))
                  for j in range(hole.PointCount())]
            if len(hp) >= 3:
                holes.append(hp)
        poly = Polygon(pts, holes)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if not poly.is_empty:
            out.append(poly)
    if not out:
        raise UnsupportedGeometry("shape produced no usable outline")
    return unary_union(out)


# How finely a curve is approximated, and in which direction. Approximating
# OUTWARD is deliberate: an inscribed polygon under-states a pad and would
# therefore over-state the clearance to it, which is the unsafe direction for a
# fabrication check. The chord error itself is a board-level tolerance and
# comes from the geometry profile, never from a literal in this module.
_POLYGON_ERROR_IU = None


def configure(polygon_chord_error_mm):
    """Install the chord error from a board's geometry profile."""
    global _POLYGON_ERROR_IU
    if not polygon_chord_error_mm or polygon_chord_error_mm <= 0:
        raise UnsupportedGeometry(
            "polygon chord error must be a positive length in mm")
    _POLYGON_ERROR_IU = int(round(polygon_chord_error_mm * 1e6))
    return _POLYGON_ERROR_IU


def polygon_error_iu():
    if _POLYGON_ERROR_IU is None:
        raise UnsupportedGeometry(
            "no polygon chord error has been configured; call "
            "geom.configure() with the manifest's "
            "`geometry_profile.tolerances.polygon_chord_error_mm.value` "
            "before any shape is approximated - the gates do this via "
            "manifest.geometry_profile().tolerance('polygon_chord_error_mm')")
    return _POLYGON_ERROR_IU


def item_id(item):
    """The KIID string of a board item - the only stable identity.

    `str(item.m_Uuid)` is the SWIG proxy's repr: an address, reused as objects
    are freed, so two live items can print the same string. Every identity
    comparison goes through `AsString()`.
    """
    return item.m_Uuid.AsString()


def _effective_polygon(item, layer, error_iu=None):
    error_iu = polygon_error_iu() if error_iu is None else error_iu
    """Ask KiCad for the item's effective shape on a layer, as a polygon."""
    polyset = pcbnew.SHAPE_POLY_SET()
    try:
        item.TransformShapeToPolygon(polyset, layer, 0, error_iu, pcbnew.ERROR_OUTSIDE)
    except (AttributeError, TypeError) as exc:
        raise UnsupportedGeometry(
            f"cannot polygonise {type(item).__name__} on layer {layer}: {exc}") from exc
    return _polyset_to_polygons(polyset)


def pad_copper_polygon(pad, layer):
    """The pad's copper outline on `layer`, exactly as KiCad renders it."""
    return _effective_polygon(pad, layer)


def pad_mask_opening(pad, mask_layer, board):
    """The solder-mask aperture for a pad, honouring per-pad and board expansion.

    KiCad grows the copper shape by the resolved mask margin; a negative margin
    shrinks it. The margin is resolved in KiCad's own precedence order: pad
    override, then footprint override, then board setting.
    """
    if not pad.IsOnLayer(mask_layer):
        return None
    margin_mm = to_mm(_resolved_mask_margin(pad, board))
    copper_layer = pcbnew.F_Cu if mask_layer == pcbnew.F_Mask else pcbnew.B_Cu
    if not pad.IsOnLayer(copper_layer):
        # A mask aperture with no copper on that side (rare, e.g. mask-only pad).
        copper_layer = pcbnew.F_Cu if pad.IsOnLayer(pcbnew.F_Cu) else pcbnew.B_Cu
    shape = pad_copper_polygon(pad, copper_layer)
    if abs(margin_mm) < 1e-9:
        return shape
    grown = shape.buffer(margin_mm, join_style=2, quad_segs=32)
    if grown.is_empty:
        return None
    return grown


def _resolved_mask_margin(pad, board):
    local = pad.GetLocalSolderMaskMargin()
    if local:
        return local
    parent = pad.GetParentFootprint()
    if parent is not None:
        fp_margin = parent.GetLocalSolderMaskMargin()
        if fp_margin:
            return fp_margin
    return board.GetDesignSettings().m_SolderMaskExpansion


# ---------------------------------------------------------------------------
# board model
# ---------------------------------------------------------------------------

class ViaGeometry:
    __slots__ = ("via", "x", "y", "pad_radius", "drill_radius", "tented",
                 "net", "layers")

    def __init__(self, via, board):
        pos = via.GetPosition()
        self.via = via
        self.x = to_mm(pos.x)
        self.y = to_mm(pos.y)
        self.pad_radius = to_mm(via.GetWidth(pcbnew.F_Cu)) / 2.0
        self.drill_radius = to_mm(via.GetDrill()) / 2.0
        self.net = via.GetNetname()
        self.layers = (via.TopLayer(), via.BottomLayer())
        self.tented = {}
        for side, getter in (("front", via.GetFrontTentingMode),
                             ("back", via.GetBackTentingMode)):
            mode = getter()
            if mode == pcbnew.TENTING_MODE_TENTED:
                self.tented[side] = True
            elif mode == pcbnew.TENTING_MODE_NOT_TENTED:
                self.tented[side] = False
            else:
                self.tented[side] = _board_tenting(board, side)

    def annulus(self):
        return Point(self.x, self.y).buffer(self.pad_radius, quad_segs=64)

    def drill(self):
        return Point(self.x, self.y).buffer(self.drill_radius, quad_segs=64)

    @property
    def annular_width(self):
        return self.pad_radius - self.drill_radius


def _board_tenting(board, side):
    """Board-level default tenting, read from the saved project setup."""
    try:
        import re
        text = board.GetFileName()
        with open(text, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(200000)
        m = re.search(r"\(tenting\s*\(front\s+(\w+)\)\s*\(back\s+(\w+)\)", head)
        if m:
            return (m.group(1) if side == "front" else m.group(2)) == "yes"
    except OSError:
        pass
    raise UnsupportedGeometry(
        "board default via tenting could not be determined; refusing to guess")


class BoardGeometry:
    """Pads, mask openings and vias for one board, built once."""

    def __init__(self, board, contact_tolerance_mm=0.0):
        self.board = board
        # Two independent polygonisations of the same tangency can differ by a
        # sub-nanometre gap, so "touching" is a tolerance decision, not an
        # exact predicate. The value comes from the geometry profile.
        self.contact_tolerance_mm = contact_tolerance_mm
        self.pads = []          # dicts
        self.vias = []
        self._build()

    def _build(self):
        for fp in self.board.Footprints():
            ref = fp.GetReference()
            for pad in fp.Pads():
                entry = {
                    "ref": ref,
                    "pad": pad.GetNumber(),
                    "label": f"{ref}.{pad.GetNumber()}",
                    "net": pad.GetNetname(),
                    "attribute": pad.GetAttribute(),
                    "is_smd": pad.GetAttribute() == pcbnew.PAD_ATTRIB_SMD,
                    "is_pth": pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
                    "on_board_bottom": fp.IsFlipped(),
                    "drill_mm": to_mm(pad.GetDrillSizeX()),
                    "pad_obj": pad,
                    "copper": {},
                    "mask": {},
                }
                for cu, mk, side in ((pcbnew.F_Cu, pcbnew.F_Mask, "front"),
                                     (pcbnew.B_Cu, pcbnew.B_Mask, "back")):
                    if pad.IsOnLayer(cu):
                        entry["copper"][side] = pad_copper_polygon(pad, cu)
                    opening = pad_mask_opening(pad, mk, self.board)
                    if opening is not None:
                        entry["mask"][side] = opening
                self.pads.append(entry)
        for item in self.board.Tracks():
            if isinstance(item, pcbnew.PCB_VIA):
                self.vias.append(ViaGeometry(item, self.board))

    # -- queries -----------------------------------------------------------
    def mask_openings(self, side):
        for entry in self.pads:
            if side in entry["mask"]:
                yield entry, entry["mask"][side]

    def via_mask_report(self, via, side):
        """All distances between one via and every mask opening on one side.

        Returns the nearest opening plus the four distances the process rules
        actually care about, kept separate rather than merged into one number:

          drill_to_opening    hole edge   -> mask aperture edge
          annulus_to_opening  via pad edge-> mask aperture edge
          centre_to_opening   via centre  -> mask aperture edge (negative inside)
          annulus_to_copper   via pad edge-> pad copper edge
        """
        centre = Point(via.x, via.y)
        annulus = via.annulus()
        drill = via.drill()
        best = None
        for entry, opening in self.mask_openings(side):
            d_centre = centre.distance(opening)
            inside = opening.contains(centre)
            if inside:
                d_centre = -centre.distance(opening.exterior)
            d_ann = 0.0 if annulus.intersects(opening) else annulus.distance(opening)
            d_drill = 0.0 if drill.intersects(opening) else drill.distance(opening)
            copper = entry["copper"].get(side)
            if copper is None:
                d_cu = None
            else:
                d_cu = 0.0 if annulus.intersects(copper) else annulus.distance(copper)
            rec = {
                "pad": entry["label"],
                "pad_net": entry["net"],
                "is_smd": entry["is_smd"],
                "is_pth": entry["is_pth"],
                "centre_to_opening_mm": round(d_centre, 4),
                "centre_inside_opening": bool(inside),
                "annulus_to_opening_mm": round(d_ann, 4),
                "drill_to_opening_mm": round(d_drill, 4),
                "annulus_to_pad_copper_mm": None if d_cu is None else round(d_cu, 4),
                # Contact (distance == 0) and strict overlap (positive shared
                # area) are reported separately: a tangential via still has no
                # ink dam, but the two counts must not be conflated.
                "annulus_contacts_opening": bool(d_ann <= self.contact_tolerance_mm),
                "annulus_overlaps_opening": bool(
                    annulus.intersection(opening).area > 0.0),
            }
            if best is None or rec["annulus_to_opening_mm"] < best["annulus_to_opening_mm"]:
                best = rec
        return best
