"""Geometry-only coupling inventory: how much of two nets' routed
copper runs close and parallel, measured - never a crosstalk
voltage.

A spacing heuristic is not a coupling model. What routed geometry
DOES establish exactly is proximity: for a pair of nets on one
layer, the length of one net's centerline whose copper edge runs
within a declared separation of the other's, and the closest
approach observed. Those are exact geometric quantities with
honest units (millimetres), suitable for risk RANKING and for
matched-fidelity A/B comparison - and for nothing electrical
until a source-supported coupling model consumes them.

Every record produced here says so: phenomenon ``coupling``,
model fidelity ``geometry-only``, decision significance limited to
risk ranking.
"""

from __future__ import annotations

from .parasitics import ParasiticsError, validate_metric


def parallelism_inventory(board, net_names, threshold_mm,
                          layers=None):
    """Exact coupled-run inventory over pairs of the named nets.

    For each unordered pair and each copper layer where both nets
    have tracks: the length of the second net's centerline whose
    copper edge comes within ``threshold_mm`` of the first net's
    copper edge, plus the minimum edge separation observed. Pairs
    with zero coupled length produce nothing. Track copper only -
    pads and zone fills are outside this inventory and are named
    in the record's omissions.
    """
    import pcbnew
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    if not isinstance(threshold_mm, (int, float)) \
            or isinstance(threshold_mm, bool) or threshold_mm <= 0:
        raise ParasiticsError(
            "threshold_mm must be a positive number")
    names = list(net_names)
    if len(set(names)) != len(names) or len(names) < 2:
        raise ParasiticsError(
            "parallelism needs at least two distinct net names")

    if layers is None:
        layer_ids = list(board.GetEnabledLayers().CuStack())
    else:
        layer_ids = []
        for name in layers:
            identifier = board.GetLayerID(name)
            if identifier < 0:
                raise ParasiticsError(
                    "unknown layer {!r}".format(name))
            layer_ids.append(identifier)

    per_layer = {}
    for track in board.GetTracks():
        if track.GetClass() in ("PCB_VIA", "VIA"):
            continue
        net = track.GetNetname()
        if net not in names:
            continue
        layer = track.GetLayer()
        if layer not in layer_ids:
            continue
        start, end = track.GetStart(), track.GetEnd()
        line = LineString([(start.x / 1e6, start.y / 1e6),
                           (end.x / 1e6, end.y / 1e6)])
        if line.length == 0:
            continue
        width = track.GetWidth() / 1e6
        per_layer.setdefault(layer, {}).setdefault(
            net, []).append((line, width))

    records = []
    for layer, nets_on_layer in sorted(per_layer.items()):
        layer_name = board.GetLayerName(layer)
        present = [n for n in names if n in nets_on_layer]
        for i, net_a in enumerate(present):
            for net_b in present[i + 1:]:
                coupled = 0.0
                minimum = None
                reach_b = unary_union([
                    line.buffer(width / 2.0)
                    for line, width in nets_on_layer[net_b]])
                for line_a, width_a in nets_on_layer[net_a]:
                    copper_a = line_a.buffer(width_a / 2.0)
                    separation = copper_a.distance(reach_b)
                    if minimum is None or separation < minimum:
                        minimum = separation
                    # Centerline of A within (edge threshold) of
                    # B's copper: buffer B by the threshold and
                    # take A's centerline length inside, minus A's
                    # own half-width already spent reaching its
                    # edge.
                    zone = reach_b.buffer(threshold_mm
                                          + width_a / 2.0)
                    coupled += line_a.intersection(zone).length
                if coupled <= 0:
                    continue
                pair = "{}||{}".format(*sorted((net_a, net_b)))
                records.append(validate_metric({
                    "kind": "parasitic-metric",
                    "phenomenon": "coupling",
                    "scope": {
                        "level": "pair",
                        "identity": "{}@{}<= {} mm".format(
                            pair, layer_name, threshold_mm)},
                    "quantity": {"semantics": "exact",
                                 "value": round(coupled, 4),
                                 "bound": None, "interval": None,
                                 "units": "mm"},
                    "model": {"name": "parallelism-inventory",
                              "fidelity": "geometry-only"},
                    "provenance": {
                        "source": "pcbqa.coupling_geometry over "
                                  "the board's own track "
                                  "centerlines",
                        "layer": layer_name,
                        "threshold_mm": threshold_mm,
                        "minimum_edge_separation_mm":
                            round(minimum, 4)},
                    "assumptions": [],
                    "omitted_contributions": [],
                    "applicability": {
                        "applicable": True,
                        "detail": "straight track segments on one "
                                  "copper layer; pads and zone "
                                  "fills are outside this "
                                  "inventory"},
                    "requirement_linkage": None,
                    "decision_significance":
                        "geometric proximity for risk RANKING "
                        "only; this is coupled run length in mm, "
                        "not a crosstalk voltage, and no electrical "
                        "claim follows until a source-supported "
                        "coupling model consumes it",
                }))
    return records
