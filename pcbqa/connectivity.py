"""Copper connectivity by geometric intersection.

The previous implementation joined tracks only where their *endpoints* were
equal and attached pads only where a track endpoint fell inside a pad. Real
boards are not built that way: a track may end anywhere inside a via annulus,
land part-way along another track, or reach a pad with its body rather than its
end. That produced false "load not reachable" findings.

Here every piece of copper is turned into its actual shape on its actual layer
and two pieces are connected when those shapes intersect. Distance along a path
is the sum of track centre-line lengths; a via transition contributes zero
length, because board thickness is not part of the trace-length budget.

Definition used by every length measurement in this package:

    electrical path length = sum of the centre-line lengths of the track
    segments on the shortest connected copper path from the driver pad to the
    load pad, vias contributing zero.

This is deliberately not "total copper on the net", which counts every branch
including ones the signal never traverses.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

import pcbnew

IU = 1e6


class Element:
    """One piece of copper: a track, an arc, a pad, a via, or part of a track.

    `part_of` is set when an element is one piece of a track that was split at
    the points other copper lands on it. The whole track is still what `obj`
    refers to, so layer, width and every other property are read from it
    exactly as before; only `length_mm` and `shape` describe the piece.
    """

    __slots__ = ("kind", "ref", "shape", "layers", "length_mm", "obj",
                 "part_of", "junctions")

    def __init__(self, kind, ref, shape, layers, length_mm, obj=None,
                 part_of=None, junctions=()):
        self.kind = kind
        self.ref = ref
        self.shape = shape
        self.layers = frozenset(layers)
        self.length_mm = length_mm
        self.obj = obj
        self.part_of = part_of
        #: The ambiguous junctions bounding this piece, as
        #: ``(identity, span_mm)``. `identity` names the cut itself - the track
        #: it is on and where along it - so a walk crossing one junction counts
        #: it once however many pieces meet there. `span_mm` is the length of
        #: the shared region the cut sits in the middle of: the cut could have
        #: been placed anywhere in it, so the piece's own length could differ
        #: by up to half of it at this end.
        self.junctions = tuple(junctions)

    def touches(self, other):
        if self.layers.isdisjoint(other.layers):
            return False
        return self.shape.intersects(other.shape)


def _track_shape(track):
    start, end = track.GetStart(), track.GetEnd()
    half = max(track.GetWidth() / 2.0, 1.0) / IU
    if isinstance(track, pcbnew.PCB_ARC):
        pts = _arc_points(track)
        line = LineString(pts)
        length = line.length
    else:
        line = LineString([(start.x / IU, start.y / IU), (end.x / IU, end.y / IU)])
        length = line.length
    if line.length <= 0:
        return Point(start.x / IU, start.y / IU).buffer(half, quad_segs=16), 0.0
    return line.buffer(half, cap_style=1, quad_segs=16), length


def _arc_points(arc, steps=24):
    centre = arc.GetCenter()
    start = arc.GetStart()
    radius = math.hypot(start.x - centre.x, start.y - centre.y) / IU
    a0 = math.atan2(start.y - centre.y, start.x - centre.x)
    sweep = math.radians(arc.GetAngle().AsDegrees())
    return [((centre.x / IU) + radius * math.cos(a0 + sweep * i / steps),
             (centre.y / IU) + radius * math.sin(a0 + sweep * i / steps))
            for i in range(steps + 1)]


def _centre_line(track):
    """The centre line of a track or arc, as a LineString (mm), or None."""
    if isinstance(track, pcbnew.PCB_ARC):
        line = LineString(_arc_points(track))
    else:
        start, end = track.GetStart(), track.GetEnd()
        line = LineString([(start.x / IU, start.y / IU),
                           (end.x / IU, end.y / IU)])
    return line if line.length > 0 else None


def _substring(line, start, end):
    """The piece of `line` between two distances along it."""
    if end - start <= 1e-12:
        return None
    head = line.interpolate(start)
    coords = [(head.x, head.y)]
    walked = 0.0
    points = list(line.coords)
    for a, b in zip(points, points[1:]):
        walked += math.hypot(b[0] - a[0], b[1] - a[1])
        if start < walked < end:
            coords.append((b[0], b[1]))
    tail = line.interpolate(end)
    coords.append((tail.x, tail.y))
    cleaned = []
    for point in coords:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)
    if len(cleaned) < 2:
        return None
    return LineString(cleaned)


def _projected_span(line, meeting):
    """Where a shared region starts and ends along a centre line.

    `project` clamps to the line, so copper lying alongside rather than across
    it still yields points on the centre line instead of raising.
    """
    from shapely.geometry import Point as _Point
    coords = []
    for geometry in getattr(meeting, "geoms", [meeting]):
        if geometry.is_empty:
            continue
        boundary = getattr(geometry, "exterior", None)
        source = boundary.coords if boundary is not None else geometry.coords
        coords.extend(source)
    if not coords:
        return None, None
    projected = [line.project(_Point(x, y)) for x, y in coords]
    return min(projected), max(projected)


def _junctions_at(track_id, spans, start, end, tolerance_mm):
    """The ambiguous junctions bounding one piece, each named once.

    A junction is identified by the track it cuts and where along that track it
    falls, so two pieces meeting at it report the *same* junction rather than
    two of them. That is what lets a walk add up its uncertainty without
    counting a cut twice for having passed through it.
    """
    found = []
    for low, high, width in spans:
        middle = (low + high) / 2.0
        for boundary in (start, end):
            if abs(middle - boundary) <= tolerance_mm:
                found.append(((track_id, round(middle, 6)), width))
                break
    return tuple(found)


def split_track_elements(elements, tolerance_mm=1e-6):
    """Replace each track with the pieces between the things that touch it.

    Why this exists
    ---------------
    The cost of entering an element is that element's whole length. That is
    exact when copper only ever meets copper end to end, and it is not how
    boards are built: a stub landing part-way along a 30 mm track makes the
    walk from that stub to the nearer end cost 30 mm instead of 4 mm. For a
    spread comparison between similar branches the error largely cancels and
    the model was good enough. For a propagation delay on an arbitrary board
    it is simply wrong, and wrong in a direction nothing else would catch.

    So each track is cut where another element lands *along* it, and the
    pieces become the graph's elements. Entering a piece then costs the piece,
    and a walk can only leave a piece at one of its ends - which is what makes
    the whole-element cost model exact again rather than approximately right.

    Copper meeting a track at one of its own ends is not a cut. That is how
    tracks are ordinarily joined, and cutting there would move where a
    measurement begins rather than correct what it counts: a pad overlapping
    the last fraction of a millimetre of its own track would quietly shorten
    every path through it. The existing convention - a walk from a pad is
    charged the track it enters - is preserved exactly.

    Opt-in. `NetTopologyRule` and every measurement that predates this keeps
    the unsplit graph, because changing what those numbers mean is a separate
    decision from making a new measurement accurate.
    """
    if not any(e.kind == "track" for e in elements):
        return elements
    tree = STRtree([e.shape for e in elements])
    out = [e for e in elements if e.kind != "track"]
    for index, element in enumerate(elements):
        if element.kind != "track":
            continue
        line = _centre_line(element.obj)
        if line is None:
            out.append(element)
            continue
        ends = (Point(line.coords[0]), Point(line.coords[-1]))
        cuts = {0.0, line.length}
        spans = []
        for other_index in tree.query(element.shape):
            other_index = int(other_index)
            if other_index == index:
                continue
            other = elements[other_index]
            if not element.touches(other):
                continue
            meeting = element.shape.intersection(other.shape)
            if meeting.is_empty:
                continue
            # Only copper that lands *along* this track is a cut. Copper
            # meeting it at an end is how tracks are normally joined, and
            # cutting there would move where a measurement starts - a pad
            # overlapping the last fraction of a millimetre of its own track
            # would silently shorten every path through it, which is a change
            # to what the existing definition means rather than a correction
            # of it. What is being fixed here is the other case: a stub or a
            # pad landing in the middle, where charging the whole track is
            # simply wrong.
            if any(meeting.distance(end) <= tolerance_mm for end in ends):
                continue
            # One cut, at the middle of the shared region, and the width of
            # that region recorded as the error bar on it.
            #
            # Cutting at both ends instead was tried and is wrong: it lets the
            # walk enter at whichever end is nearer, which shortcuts a plain
            # perpendicular tee by half a track width and disagrees with the
            # centre-line-to-centre-line length every EDA tool reports. The
            # midpoint reproduces that convention exactly where the overlap is
            # small and symmetric, which is the overwhelmingly common case.
            #
            # Where the overlap is long - two wide tracks meeting obliquely, or
            # copper running alongside for a while - no single cut point is
            # right, and the honest answer is to keep the best one and say how
            # far out it could be rather than to pick a different guess.
            low, high = _projected_span(line, meeting)
            if low is None:
                continue
            cuts.add((low + high) / 2.0)
            if high - low > tolerance_mm:
                spans.append((low, high, high - low))
        ordered = sorted(cuts)
        merged = [ordered[0]]
        for value in ordered[1:]:
            if value - merged[-1] > tolerance_mm:
                merged.append(value)
        if len(merged) < 3:                     # nothing lands mid-track
            out.append(element)
            continue
        half = max(element.obj.GetWidth() / 2.0, 1.0) / IU
        for start, end in zip(merged, merged[1:]):
            piece = _substring(line, start, end)
            if piece is None:
                continue
            out.append(Element(
                "track", element.ref,
                piece.buffer(half, cap_style=1, quad_segs=16),
                element.layers, piece.length, element.obj,
                part_of=element,
                junctions=_junctions_at(id(element.obj), spans, start, end,
                                        tolerance_mm)))
    return out


def build_elements(board, net_name, pad_polygon):
    """Every copper element on one net, as shapes on layers."""
    elements = []
    for track in board.Tracks():
        if track.GetNetname() != net_name:
            continue
        if isinstance(track, pcbnew.PCB_VIA):
            pos = track.GetPosition()
            radius = track.GetWidth(pcbnew.F_Cu) / 2.0 / IU
            layers = [l for l in board.GetEnabledLayers().CuStack()
                      if track.IsOnLayer(l)]
            elements.append(Element(
                "via", f"via@{pos.x / IU:.3f},{pos.y / IU:.3f}",
                Point(pos.x / IU, pos.y / IU).buffer(radius, quad_segs=32),
                layers, 0.0, track))
        else:
            shape, length = _track_shape(track)
            elements.append(Element("track", "", shape, [track.GetLayer()],
                                    length, track))
    for fp in board.Footprints():
        for pad in fp.Pads():
            if pad.GetNetname() != net_name:
                continue
            layers = [l for l in board.GetEnabledLayers().CuStack()
                      if pad.IsOnLayer(l)]
            if not layers:
                continue
            shape = pad_polygon(pad, layers[0])
            for extra in layers[1:]:
                shape = shape.union(pad_polygon(pad, extra))
            elements.append(Element("pad", f"{fp.GetReference()}.{pad.GetNumber()}",
                                    shape, layers, 0.0, pad))
    return elements


def zone_fill_elements(board, net_name):
    """Filled zone copper on one net, as polygon elements per layer.

    Rule areas are keepouts, not copper, and are skipped. An unfilled
    zone contributes nothing: copper that does not exist on the board
    is never assumed. Zone elements carry zero length - they join
    copper, they are not part of any trace-length budget.
    """
    from shapely.geometry import Polygon

    elements = []
    for zone in board.Zones():
        if zone.GetIsRuleArea():
            continue
        if zone.GetNetname() != net_name:
            continue
        if not zone.IsFilled():
            continue
        for layer in board.GetEnabledLayers().CuStack():
            if not zone.IsOnLayer(layer):
                continue
            filled = zone.GetFilledPolysList(layer)
            for outline_index in range(filled.OutlineCount()):
                chain = filled.Outline(outline_index)
                shell = [(chain.CPoint(k).x / IU,
                          chain.CPoint(k).y / IU)
                         for k in range(chain.PointCount())]
                if len(shell) < 3:
                    continue
                holes = []
                for hole_index in range(
                        filled.HoleCount(outline_index)):
                    hole = filled.Hole(outline_index, hole_index)
                    ring = [(hole.CPoint(k).x / IU,
                             hole.CPoint(k).y / IU)
                            for k in range(hole.PointCount())]
                    if len(ring) >= 3:
                        holes.append(ring)
                polygon = Polygon(shell, holes)
                if polygon.is_empty:
                    continue
                elements.append(Element(
                    "zone", "zone@{}#{}/{}".format(
                        net_name, outline_index,
                        board.GetLayerName(layer)),
                    polygon, [layer], 0.0, zone))
    return elements


class NetGraph:
    """Connectivity graph for one net, built from copper intersection."""

    def __init__(self, board, net_name, pad_polygon,
                 split_at_junctions=False, include_zone_fills=False):
        self.net = net_name
        self.split_at_junctions = split_at_junctions
        elements = build_elements(board, net_name, pad_polygon)
        if split_at_junctions:
            elements = split_track_elements(elements)
        if include_zone_fills:
            elements = elements + zone_fill_elements(board, net_name)
        self.elements = elements
        self.adj = defaultdict(list)
        self._link()

    def _link(self):
        if not self.elements:
            return
        shapes = [e.shape for e in self.elements]
        tree = STRtree(shapes)
        for i, element in enumerate(self.elements):
            for j in tree.query(element.shape):
                j = int(j)
                if j <= i:
                    continue
                other = self.elements[j]
                if element.touches(other):
                    # Cost of entering an element is that element's own length.
                    self.adj[i].append((j, other.length_mm))
                    self.adj[j].append((i, element.length_mm))

    # -- queries -----------------------------------------------------------
    def index_of(self, ref):
        return [i for i, e in enumerate(self.elements) if e.ref == ref]

    def vias(self):
        return sum(1 for e in self.elements if e.kind == "via")

    def layers_used(self, board):
        used = set()
        for e in self.elements:
            if e.kind == "track":
                used.add(board.GetLayerName(e.obj.GetLayer()))
        return sorted(used)

    def total_track_mm(self):
        """Total routed copper on the net.

        The pieces of a split track sum to the track, so this is the same
        number whether or not the graph was split.
        """
        return sum(e.length_mm for e in self.elements if e.kind == "track")

    def track_objects(self):
        """The distinct KiCad tracks behind the elements, split or not."""
        seen, out = set(), []
        for element in self.elements:
            if element.kind != "track":
                continue
            if id(element.obj) in seen:
                continue
            seen.add(id(element.obj))
            out.append(element.obj)
        return out

    def path_length(self, source_refs, target_ref):
        """Shortest electrical path length, or None if not connected."""
        distance, _elements = self.trace(source_refs, target_ref)
        return distance

    def trace(self, source_refs, target_ref):
        """The shortest path itself: (length_mm, [element index, ...]).

        The same search `path_length` has always performed - identical start
        set, identical relaxation, identical early exit the moment a target is
        popped - with the predecessor of each settled element remembered, so
        the elements the signal actually traverses can be reported rather than
        only how long they add up to. `path_length` is this function's total,
        so the two can never disagree about a length.

        Returned in traversal order, source element first. Consistent with the
        cost model above, the source element's own length is not part of the
        total: the walk starts at it, it is not entered.

        (None, []) when there is no connected path, exactly as before.
        """
        starts = [i for ref in source_refs for i in self.index_of(ref)]
        targets = set(self.index_of(target_ref))
        if not starts or not targets:
            return None, []
        dist = {}
        prev = {}
        pq = []
        for s in starts:
            dist[s] = 0.0
            prev[s] = None
            heapq.heappush(pq, (0.0, s))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, math.inf):
                continue
            if u in targets:
                return d, self._walk_back(prev, u)
            for v, w in self.adj[u]:
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return None, []

    @staticmethod
    def _walk_back(prev, node):
        chain = []
        while node is not None:
            chain.append(node)
            node = prev[node]
        chain.reverse()
        return chain

    def branch_points(self):
        return sum(1 for i, e in enumerate(self.elements)
                   if e.kind == "track" and len(self.adj[i]) > 2)


#: The connectivity states this module distinguishes. Deliberately
#: NOT the same question as topology validity: a net may connect all
#: of its pads and still violate a required tree/path topology -
#: that judgment belongs to NetTopologyRule and the electrical-path
#: gates, and nothing here claims it.
CONNECTIVITY_CLASSES = ("no-pads", "no-copper", "partial-copper",
                        "connectivity-complete")


def classify_net(board, net_name, pad_polygon,
                 include_zone_fills=True):
    """Real connectivity state of one net, from board geometry.

    ``connectivity-complete`` means every pad on the net belongs to
    ONE connected copper component - the only sense in which a
    multipoint net is routed. A net with copper that fails this is
    ``partial-copper`` however many tracks it carries; a net whose
    pads are islands with no copper at all is ``no-copper``. Filled
    zones participate (a plane-served net is connected by its plane);
    unfilled zones do not exist. Router logs play no part: the board
    file is the arbiter.

    A net with no pads gets the explicit degenerate state
    ``no-pads``: there is no pad-joining question to answer, and the
    state says so instead of pretending an answer exists. Such a net
    can never be connectivity-complete.
    """
    graph = NetGraph(board, net_name, pad_polygon,
                     include_zone_fills=include_zone_fills)
    pad_indices = [index for index, element
                   in enumerate(graph.elements)
                   if element.kind == "pad"]
    copper = {"tracks": 0, "vias": 0, "zone_fills": 0}
    for element in graph.elements:
        if element.kind == "track":
            copper["tracks"] += 1
        elif element.kind == "via":
            copper["vias"] += 1
        elif element.kind == "zone":
            copper["zone_fills"] += 1
    seen = set()
    components = []
    for start in range(len(graph.elements)):
        if start in seen:
            continue
        stack = [start]
        component = []
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            component.append(node)
            for neighbour, _weight in graph.adj[node]:
                if neighbour not in seen:
                    stack.append(neighbour)
        components.append(component)
    pad_components = []
    for component in components:
        pads = sorted(graph.elements[index].ref
                      for index in component
                      if graph.elements[index].kind == "pad")
        if pads:
            pad_components.append(pads)
    pad_components.sort()
    total_pads = len(pad_indices)
    if total_pads == 0:
        connectivity_class = "no-pads"
    elif len(pad_components) == 1 and             len(pad_components[0]) == total_pads:
        connectivity_class = "connectivity-complete"
    elif any(copper.values()) or             any(len(group) > 1 for group in pad_components):
        connectivity_class = "partial-copper"
    else:
        connectivity_class = "no-copper"
    return {
        "net": net_name,
        "class": connectivity_class,
        "pad_count": total_pads,
        "pads": sorted(graph.elements[index].ref
                       for index in pad_indices),
        "copper": copper,
        "pad_components": pad_components,
        "meaning": "connectivity-complete means every pad on the "
                   "net is in one connected copper component; it "
                   "never implies the topology the design requires "
                   "- that is a separate, policy-owned judgment",
    }
