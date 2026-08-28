"""Critical-topology planning: deterministic local copper the general
router cannot be trusted to produce.

Scope, deliberately small: SHORT local structures around a known
anchor - a pad's escape out of a tight land pattern, a stitching via
from guard copper to an internal plane - generated at the DECLARED
fabrication values, never at a relaxed floor. The generator produces
only geometry it has itself verified against the exact obstacle
polygons (generate-then-verify, choose only passing candidates); the
board's gates remain the authority afterwards, and nothing here
weakens them.

Board-specific intent (which pads, which plane, which direction)
belongs to the consumer; this module owns the generic machinery:

  * an obstacle field built from the board's actual copper, mask
    openings and via-keepout rule areas around one anchor;
  * grid path search for a short escape whose emitted segments are
    then EXACTLY re-verified against the un-quantized obstacles;
  * via-site search honoring copper clearance on both outer layers,
    hole-to-hole distance, the solder-mask annulus-to-opening
    process target against EVERY mask opening, do-not-allow-vias
    rule areas, and actual plane fill presence at the site;
  * application of a verified proposal to a candidate board.

Every refusal is explicit. A proposal that cannot be verified is not
returned.
"""

from __future__ import annotations

import heapq
import math


class TopologyPlanError(Exception):
    """The planner cannot produce a verified structure as asked."""


_RULE_KEYS = {"layer", "track_width_mm", "clearance_mm",
              "via_diameter_mm", "via_drill_mm",
              "hole_to_hole_mm", "hole_clearance_mm",
              "mask_annulus_target_mm",
              "grid_step_mm", "search_radius_mm"}


def validate_rules(rules):
    """Declared local-planning values; every one required, positive."""
    if not isinstance(rules, dict) or set(rules) != _RULE_KEYS:
        raise TopologyPlanError(
            "rules must carry exactly {}".format(sorted(_RULE_KEYS)))
    for key in _RULE_KEYS - {"layer"}:
        value = rules[key]
        if isinstance(value, bool) or \
                not isinstance(value, (int, float)) or \
                value != value or value <= 0 or \
                value in (float("inf"), float("-inf")):
            raise TopologyPlanError(
                "rules.{} must be a positive finite number".format(
                    key))
    if not isinstance(rules["layer"], str) or not rules["layer"]:
        raise TopologyPlanError("rules.layer must be a layer name")
    return rules


def _layer_id(board, name):
    for layer in board.GetEnabledLayers().CuStack():
        if board.GetLayerName(layer) == name:
            return layer
    raise TopologyPlanError(
        "layer {!r} is not an enabled copper layer".format(name))


class LocalField:
    """The exact obstacle geometry around one anchor, for one net."""

    def __init__(self, board, net_name, center_mm, rules,
                 pad_polygon, outline=None):
        import pcbnew
        from shapely.geometry import LineString, Point, box
        from shapely.ops import unary_union

        validate_rules(rules)
        if outline is not None:
            if not isinstance(outline, dict) or set(outline) != {
                    "center_mm", "radius_mm", "clearance_mm"}:
                raise TopologyPlanError(
                    "outline must carry exactly center_mm, "
                    "radius_mm and clearance_mm (circular outlines "
                    "only)")
        self.outline = outline
        self.board = board
        self.net = net_name
        self.rules = rules
        self.center = center_mm
        self.layer = _layer_id(board, rules["layer"])
        radius = rules["search_radius_mm"]
        self.window = box(center_mm[0] - radius,
                          center_mm[1] - radius,
                          center_mm[0] + radius,
                          center_mm[1] + radius)
        outer_layers = [board.GetEnabledLayers().CuStack()[0],
                        board.GetEnabledLayers().CuStack()[-1]]

        def _in_window(shape):
            return shape is not None and not shape.is_empty and \
                shape.intersects(self.window)

        route_obstacles = []
        via_obstacles = []
        self.own_copper = []
        self.existing_holes = []
        # Holes repel FOREIGN copper by the declared hole clearance,
        # whatever net the hole belongs to - a track may sit ON its
        # own via, but never within hole clearance of anything
        # else's drill. Modeled as an extra obstacle disk of drill/2
        # + hole_clearance around every foreign hole.
        hole_margin = rules["hole_clearance_mm"]
        for track in board.GetTracks():
            same_net = track.GetNetname() == net_name
            if track.GetClass() in ("PCB_VIA", "VIA"):
                position = track.GetPosition()
                point = Point(position.x / 1e6, position.y / 1e6)
                diameter = track.GetWidth(outer_layers[0]) / 1e6
                circle = point.buffer(diameter / 2.0, quad_segs=16)
                if not _in_window(circle):
                    continue
                drill = track.GetDrillValue() / 1e6
                self.existing_holes.append((point, drill))
                if same_net:
                    self.own_copper.append(circle)
                else:
                    hole_disk = point.buffer(
                        drill / 2.0 + hole_margin, quad_segs=16)
                    route_obstacles.append(circle.union(hole_disk))
                    via_obstacles.append(circle.union(hole_disk))
                continue
            shape = LineString([
                (track.GetStart().x / 1e6, track.GetStart().y / 1e6),
                (track.GetEnd().x / 1e6, track.GetEnd().y / 1e6),
            ]).buffer(track.GetWidth() / 2e6, quad_segs=8)
            if not _in_window(shape):
                continue
            on_layer = track.GetLayer() == self.layer
            if same_net:
                if on_layer:
                    self.own_copper.append(shape)
            else:
                if on_layer:
                    route_obstacles.append(shape)
                if track.GetLayer() in outer_layers:
                    via_obstacles.append(shape)
        self.mask_openings = []
        for footprint in board.GetFootprints():
            for pad in footprint.Pads():
                shapes = {}
                for layer in outer_layers:
                    if pad.IsOnLayer(layer):
                        shapes[layer] = pad_polygon(pad, layer)
                if not shapes:
                    continue
                relevant = [s for s in shapes.values()
                            if _in_window(s)]
                if not relevant:
                    continue
                same_net = pad.GetNetname() == net_name
                drill_size = pad.GetDrillSize()
                pad_drill = max(drill_size.x, drill_size.y) / 1e6
                if pad_drill > 0:
                    position = pad.GetPosition()
                    hole_point = Point(position.x / 1e6,
                                       position.y / 1e6)
                    self.existing_holes.append((hole_point,
                                                pad_drill))
                    if not same_net:
                        hole_disk = hole_point.buffer(
                            pad_drill / 2.0 + hole_margin,
                            quad_segs=16)
                        if _in_window(hole_disk):
                            route_obstacles.append(hole_disk)
                            via_obstacles.append(hole_disk)
                for layer, shape in shapes.items():
                    if not _in_window(shape):
                        continue
                    if same_net:
                        if layer == self.layer:
                            self.own_copper.append(shape)
                    else:
                        if layer == self.layer:
                            route_obstacles.append(shape)
                        via_obstacles.append(shape)
                # Every mask OPENING constrains via placement,
                # whatever its net: the ink dam is a process rule,
                # not an electrical one. The true aperture (copper
                # grown by the resolved mask margin) is used, never
                # the copper outline - the copper would understate
                # the opening in exactly the unsafe direction.
                from . import geom as geom_module
                openings = []
                for mask in (pcbnew.F_Mask, pcbnew.B_Mask):
                    opening = geom_module.pad_mask_opening(
                        pad, mask, board)
                    if opening is not None and _in_window(opening):
                        openings.append(opening)
                if openings:
                    self.mask_openings.append(
                        unary_union(openings))
        self.keepout_vias = []
        self.plane_fills = {}
        from .connectivity import zone_fill_elements
        for zone in board.Zones():
            if zone.GetIsRuleArea():
                if not zone.GetDoNotAllowVias():
                    continue
                bb = zone.GetBoundingBox()
                area = box(bb.GetLeft() / 1e6, bb.GetTop() / 1e6,
                           bb.GetRight() / 1e6,
                           bb.GetBottom() / 1e6)
                if _in_window(area):
                    self.keepout_vias.append(area)
        self.route_union = unary_union(route_obstacles) \
            if route_obstacles else None
        self.via_union = unary_union(via_obstacles) \
            if via_obstacles else None
        self.own_union = unary_union(self.own_copper) \
            if self.own_copper else None
        self._zone_fill_elements = zone_fill_elements

    # -- via sites -----------------------------------------------------
    def plane_fill_at(self, plane_net):
        from shapely.ops import unary_union
        elements = [element.shape for element in
                    self._zone_fill_elements(self.board, plane_net)
                    if element.shape.intersects(self.window)]
        if not elements:
            raise TopologyPlanError(
                "net {!r} has no filled zone copper in this window; "
                "a stitch to an unfilled plane connects "
                "nothing".format(plane_net))
        return unary_union(elements)

    def _edge_distance_ok(self, x, y, extent_mm):
        """Copper extent stays clear of the board edge, when the
        consumer supplied the outline."""
        if self.outline is None:
            return True
        reach = math.hypot(x - self.outline["center_mm"][0],
                           y - self.outline["center_mm"][1])
        return reach + extent_mm <= self.outline["radius_mm"] \
            - self.outline["clearance_mm"]

    def via_site_ok(self, x, y, plane_fill):
        from shapely.geometry import Point
        rules = self.rules
        if not self._edge_distance_ok(
                x, y, rules["via_diameter_mm"] / 2.0):
            return False, "board edge clearance"
        point = Point(x, y)
        annulus = point.buffer(rules["via_diameter_mm"] / 2.0,
                               quad_segs=16)
        if self.via_union is not None and annulus.buffer(
                rules["clearance_mm"]).intersects(self.via_union):
            return False, "copper clearance"
        # The new via's own HOLE keeps the declared hole clearance
        # from foreign copper too.
        if self.via_union is not None and point.buffer(
                rules["via_drill_mm"] / 2.0
                + rules["hole_clearance_mm"]).intersects(
                    self.via_union):
            return False, "hole clearance"
        for opening in self.mask_openings:
            if annulus.distance(opening) < \
                    rules["mask_annulus_target_mm"]:
                return False, "mask annulus target"
        for area in self.keepout_vias:
            if area.contains(point) or area.intersects(annulus):
                return False, "do-not-allow-vias rule area"
        for hole_point, drill in self.existing_holes:
            if point.distance(hole_point) < \
                    rules["hole_to_hole_mm"] \
                    + (drill + rules["via_drill_mm"]) / 2.0:
                return False, "hole-to-hole distance"
        if not annulus.intersects(plane_fill):
            return False, "no plane copper at the site"
        return True, "ok"

    def find_via_site(self, near_xy, plane_fill,
                      prefer_angle=0.0):
        golden = math.pi * (3.0 - math.sqrt(5.0))
        step = 0
        radius = self.rules["via_diameter_mm"]
        while radius <= self.rules["search_radius_mm"]:
            angle = prefer_angle + step * golden
            x = near_xy[0] + radius * math.cos(angle)
            y = near_xy[1] + radius * math.sin(angle)
            ok, _why = self.via_site_ok(x, y, plane_fill)
            if ok:
                return (round(x, 4), round(y, 4))
            step += 1
            if step % 14 == 0:
                radius += self.rules["grid_step_mm"] * 4
        return None

    # -- escape search -------------------------------------------------
    def escape(self, start_xy, end_xy):
        """A short verified path start -> end on the field's layer.

        Grid A* over the window, obstacles inflated by clearance
        plus half the track width; every emitted segment is then
        re-verified against the UN-quantized obstacle union, so grid
        quantization can never smuggle a violation through. Own-net
        copper is never an obstacle.
        """
        from shapely.geometry import LineString, Point
        from shapely.prepared import prep

        rules = self.rules
        inflation = rules["clearance_mm"] \
            + rules["track_width_mm"] / 2.0
        blocked_shape = None
        if self.route_union is not None:
            blocked_shape = self.route_union.buffer(inflation)
        prepared = prep(blocked_shape) if blocked_shape is not None \
            else None
        step = rules["grid_step_mm"]

        def blocked(x, y):
            if not self._edge_distance_ok(
                    x, y, rules["track_width_mm"] / 2.0):
                return True
            if prepared is None:
                return False
            return prepared.intersects(Point(x, y))

        def snap(xy):
            return (round(round(xy[0] / step) * step, 6),
                    round(round(xy[1] / step) * step, 6))

        start = snap(start_xy)
        goal = snap(end_xy)
        limit = rules["search_radius_mm"]

        def inside(node):
            return abs(node[0] - self.center[0]) <= limit and \
                abs(node[1] - self.center[1]) <= limit

        if not inside(start) or not inside(goal):
            raise TopologyPlanError(
                "escape endpoints fall outside the search window")
        open_heap = [(0.0, start)]
        best = {start: 0.0}
        previous = {start: None}
        found = False
        while open_heap:
            _priority, node = heapq.heappop(open_heap)
            if node == goal:
                found = True
                break
            for dx in (-step, 0.0, step):
                for dy in (-step, 0.0, step):
                    if dx == 0.0 and dy == 0.0:
                        continue
                    neighbour = (round(node[0] + dx, 6),
                                 round(node[1] + dy, 6))
                    if neighbour in best or not inside(neighbour):
                        continue
                    if neighbour != goal and \
                            blocked(neighbour[0], neighbour[1]):
                        continue
                    cost = best[node] + math.hypot(dx, dy)
                    best[neighbour] = cost
                    previous[neighbour] = node
                    heapq.heappush(open_heap, (
                        cost + math.hypot(goal[0] - neighbour[0],
                                          goal[1] - neighbour[1]),
                        neighbour))
        if not found:
            raise TopologyPlanError(
                "no escape path exists at the declared values "
                "within the search window")
        chain = []
        node = goal
        while node is not None:
            chain.append(node)
            node = previous[node]
        chain.reverse()
        corners = [chain[0]]
        for index in range(1, len(chain) - 1):
            ax, ay = chain[index - 1]
            bx, by = chain[index]
            cx, cy = chain[index + 1]
            if (bx - ax, by - ay) != (cx - bx, cy - by):
                corners.append(chain[index])
        corners.append(chain[-1])
        segments = []
        for start_point, end_point in zip(corners, corners[1:]):
            line = LineString([start_point, end_point])
            if self.route_union is not None and line.buffer(
                    rules["track_width_mm"] / 2.0
                    + rules["clearance_mm"] * 0.999).intersects(
                        self.route_union):
                raise TopologyPlanError(
                    "a generated segment failed exact "
                    "re-verification against the obstacle "
                    "geometry; refusing rather than emitting it")
            segments.append({
                "start_mm": [start_point[0], start_point[1]],
                "end_mm": [end_point[0], end_point[1]],
                "width_mm": rules["track_width_mm"],
                "layer": rules["layer"],
            })
        return segments


def stitch_to_plane(board, net_name, anchor_xy, plane_net, rules,
                    pad_polygon, prefer_angle=0.0, outline=None):
    """A verified pad/copper-to-plane stitch: short escape plus via.

    ``anchor_xy`` is a point on the net's own copper (a pad centre,
    a guard-bar end). The via site honors every process constraint
    this module knows and must land on actual plane fill; the track
    from anchor to via is search-generated and exactly re-verified.
    """
    field = LocalField(board, net_name, anchor_xy, rules,
                       pad_polygon, outline=outline)
    plane_fill = field.plane_fill_at(plane_net)
    site = field.find_via_site(anchor_xy, plane_fill, prefer_angle)
    if site is None:
        raise TopologyPlanError(
            "no via site within the window satisfies copper "
            "clearance, the mask annulus target, hole-to-hole and "
            "keepout rules while landing on plane fill")
    segments = field.escape(anchor_xy, site)
    return {
        "kind": "critical-topology-proposal",
        "net": net_name,
        "tracks": segments,
        "vias": [{"x_mm": site[0], "y_mm": site[1],
                  "diameter_mm": rules["via_diameter_mm"],
                  "drill_mm": rules["via_drill_mm"]}],
        "verified": "generated at declared values and re-verified "
                    "against exact obstacle geometry; board gates "
                    "remain the authority",
    }


def local_connect(board, net_name, from_xy, to_xy, rules,
                  pad_polygon, outline=None):
    """A verified short connection between two points of one net."""
    field = LocalField(board, net_name, from_xy, rules, pad_polygon,
                       outline=outline)
    segments = field.escape(from_xy, to_xy)
    return {
        "kind": "critical-topology-proposal",
        "net": net_name,
        "tracks": segments,
        "vias": [],
        "verified": "generated at declared values and re-verified "
                    "against exact obstacle geometry; board gates "
                    "remain the authority",
    }


def apply_proposal(board, proposal):
    """Add a verified proposal's copper to a candidate board."""
    import pcbnew
    if proposal.get("kind") != "critical-topology-proposal":
        raise TopologyPlanError("not a critical-topology proposal")
    nets = board.GetNetsByName()
    net = nets[proposal["net"]]
    added = 0
    for segment in proposal["tracks"]:
        track = pcbnew.PCB_TRACK(board)
        track.SetStart(pcbnew.VECTOR2I(
            pcbnew.FromMM(segment["start_mm"][0]),
            pcbnew.FromMM(segment["start_mm"][1])))
        track.SetEnd(pcbnew.VECTOR2I(
            pcbnew.FromMM(segment["end_mm"][0]),
            pcbnew.FromMM(segment["end_mm"][1])))
        track.SetWidth(pcbnew.FromMM(segment["width_mm"]))
        track.SetLayer(_layer_id(board, segment["layer"]))
        track.SetNet(net)
        board.Add(track)
        added += 1
    for via in proposal["vias"]:
        item = pcbnew.PCB_VIA(board)
        item.SetPosition(pcbnew.VECTOR2I(
            pcbnew.FromMM(via["x_mm"]),
            pcbnew.FromMM(via["y_mm"])))
        item.SetWidth(pcbnew.FromMM(via["diameter_mm"]))
        item.SetDrill(pcbnew.FromMM(via["drill_mm"]))
        item.SetLayerPair(board.GetEnabledLayers().CuStack()[0],
                          board.GetEnabledLayers().CuStack()[-1])
        item.SetNet(net)
        board.Add(item)
        added += 1
    return added
