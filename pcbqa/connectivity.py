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
    """One piece of copper: a track, an arc, a pad or a via."""

    __slots__ = ("kind", "ref", "shape", "layers", "length_mm", "obj")

    def __init__(self, kind, ref, shape, layers, length_mm, obj=None):
        self.kind = kind
        self.ref = ref
        self.shape = shape
        self.layers = frozenset(layers)
        self.length_mm = length_mm
        self.obj = obj

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


class NetGraph:
    """Connectivity graph for one net, built from copper intersection."""

    def __init__(self, board, net_name, pad_polygon):
        self.net = net_name
        self.elements = build_elements(board, net_name, pad_polygon)
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
        return sum(e.length_mm for e in self.elements if e.kind == "track")

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
