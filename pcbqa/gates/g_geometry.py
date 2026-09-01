"""Stackup parity, via/solder-mask geometry and routing-style gates.

All limits, layer expectations and permitted styles come from the manifest.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict

from ..core import Status, gate, sha256_file
from .. import geom, gerber


# ---------------------------------------------------------------------------
# stackup
# ---------------------------------------------------------------------------

def _native_stackup(ctx):
    """Copper layers in physical order with their zone net(s), from the board."""
    import pcbnew
    board = ctx.board()
    ids = [l for l in board.GetEnabledLayers().CuStack()]
    zones = defaultdict(set)
    for zone in board.Zones():
        # A rule area is a keep-out, not copper: it pours nothing and carries
        # no net. Counting one made every layer carrying a via keep-out look
        # like a layer carrying an unnamed plane.
        if zone.GetIsRuleArea():
            continue
        for layer in zone.GetLayerSet().CuStack():
            zones[layer].add(zone.GetNetname())
    stack = []
    for layer in ids:
        stack.append({
            "index": len(stack) + 1,
            "canonical": pcbnew.LayerName(layer),
            "user_name": board.GetLayerName(layer),
            "zone_nets": sorted(zones.get(layer, [])),
        })
    return stack


@gate("STACK.NATIVE_VS_MANIFEST", "Board stackup matches the frozen constraints",
      requires=("stackup.expected",))
def stack_native(ctx, res):
    expected = res.limit(ctx.manifest.constraint(
        "stackup.expected", units="layer roles", cid="stackup.expected")).value
    stack = _native_stackup(ctx)
    res.measurements["native_stackup"] = stack
    res.measurements["copper_layers"] = len(stack)
    if len(stack) != len(expected):
        return res.failed(f"board has {len(stack)} copper layers, constraints "
                          f"describe {len(expected)}")
    bad = []
    for got, want in zip(stack, expected):
        role = want.get("role")
        net = want.get("plane_net")
        if role == "plane":
            if net not in got["zone_nets"]:
                bad.append({"layer_index": got["index"],
                            "layer": got["user_name"],
                            "expected_plane_net": net,
                            "actual_zone_nets": got["zone_nets"] or ["<none>"],
                            "issue": "plane net disagreement"})
        elif role == "signal":
            if got["zone_nets"]:
                bad.append({"layer_index": got["index"], "layer": got["user_name"],
                            "expected": "signal", "actual_zone_nets": got["zone_nets"],
                            "issue": "signal layer carries a plane"})
    for b in bad:
        res.finding(**b)
    if bad:
        return res.failed(
            "the frozen constraint manifest and the native board describe "
            "different plane assignments; the board is authoritative, so the "
            "manifest is the representation that disagrees")
    return res.passed("native stackup agrees with the frozen constraints")


# ---------------------------------------------------------------------------
# via / mask geometry
# ---------------------------------------------------------------------------

def _via_survey(ctx):
    """Per-via nearest mask opening on each side, from the native board."""
    def build():
        profile = ctx.manifest.geometry_profile()
        # The chord error is a board tolerance, so it is installed from the
        # profile before anything is polygonised. Gates that apply it also
        # record it as a limit, which is what proves it came from the manifest.
        geom.configure(profile.tolerance("polygon_chord_error_mm").value)
        tol = profile.tolerance("contact_mm").value
        g = geom.BoardGeometry(ctx.board(), contact_tolerance_mm=tol)
        rows = []
        for via in g.vias:
            row = {"net": via.net, "x_mm": round(via.x, 4), "y_mm": round(via.y, 4),
                   "annular_width_mm": round(via.annular_width, 4),
                   "drill_mm": round(via.drill_radius * 2, 4),
                   "pad_mm": round(via.pad_radius * 2, 4),
                   "tented": dict(via.tented), "sides": {}}
            for side in ("front", "back"):
                rep = g.via_mask_report(via, side)
                if rep:
                    row["sides"][side] = rep
            rows.append(row)
        return g, rows
    return ctx.cache("via_survey", build)


def _worst(row, field):
    vals = [s[field] for s in row["sides"].values() if s.get(field) is not None]
    return min(vals) if vals else None


def _clearance_gate(ctx, res, limit_key, label):
    constraint = res.limit(ctx.manifest.constraint(limit_key, units="mm", cid=label))
    # The clearances below were measured from polygons approximated at this
    # chord error, so it is part of the result and is declared as such.
    res.limit(ctx.manifest.geometry_profile()
              .tolerance("polygon_chord_error_mm"))
    metric = res.limit(ctx.manifest.constraint(
        "via_mask.metric", units="field name", cid="via_mask.metric")).value
    limit = constraint.value
    _, rows = _via_survey(ctx)
    offenders = []
    for row in rows:
        worst = _worst(row, metric)
        if worst is not None and constraint.violated_minimum(worst):
            side = min(row["sides"].items(),
                       key=lambda kv: kv[1].get(metric, 9e9))
            offenders.append({
                "net": row["net"], "x_mm": row["x_mm"], "y_mm": row["y_mm"],
                "side": side[0], "nearest_pad": side[1]["pad"],
                metric: side[1][metric],
                "drill_to_opening_mm": side[1]["drill_to_opening_mm"],
                "annulus_to_opening_mm": side[1]["annulus_to_opening_mm"],
                "centre_to_opening_mm": side[1]["centre_to_opening_mm"],
            })
    res.measurements["vias_total"] = len(rows)
    res.measurements["vias_below_limit"] = len(offenders)
    for o in sorted(offenders, key=lambda d: d[metric])[:120]:
        res.finding(**o)
    if offenders:
        return res.failed(f"{len(offenders)} of {len(rows)} vias are closer than "
                          f"{limit} mm ({metric}) to a solder-mask opening")
    return res.passed(f"all {len(rows)} vias clear {limit} mm ({metric})")


@gate("VIA.MASK_CLEARANCE_TARGET", "Via to mask opening meets the project target",
      requires=("via_mask.design_target_mm", "via_mask.metric"))
def via_target(ctx, res):
    return _clearance_gate(ctx, res, "via_mask.design_target_mm", "design_target_mm")


@gate("VIA.MASK_CLEARANCE_PROCESS", "Via to mask opening meets the fab process limit",
      requires=("via_mask.process.limit_mm", "via_mask.metric"))
def via_process(ctx, res):
    proc = ctx.manifest.get("via_mask.process")
    res.measurements["process"] = {
        "name": proc.get("name"), "rule": proc.get("rule"),
        "url": proc.get("url"), "retrieved": proc.get("retrieved"),
        "interpretation": proc.get("interpretation"),
    }
    return _clearance_gate(ctx, res, "via_mask.process.limit_mm", "process_limit_mm")


@gate("VIA.ANNULUS_MASK_OVERLAP", "No via annulus intersects a mask opening",
      requires=("via_mask",))
def via_overlap(ctx, res):
    res.limit(ctx.manifest.geometry_profile().tolerance("contact_mm"))
    res.limit(ctx.manifest.geometry_profile()
              .tolerance("polygon_chord_error_mm"))
    _, rows = _via_survey(ctx)
    hits, strict = [], 0
    for row in rows:
        for side, rep in row["sides"].items():
            if not rep["annulus_contacts_opening"]:
                continue
            kind = "overlap" if rep["annulus_overlaps_opening"] else "tangency"
            strict += 1 if rep["annulus_overlaps_opening"] else 0
            hits.append({"net": row["net"], "x_mm": row["x_mm"], "y_mm": row["y_mm"],
                         "side": side, "pad": rep["pad"], "pad_net": rep["pad_net"],
                         "contact": kind,
                         "centre_inside": rep["centre_inside_opening"],
                         "annulus_to_opening_mm": rep["annulus_to_opening_mm"]})
    res.measurements["vias_total"] = len(rows)
    res.measurements["annulus_contacts"] = len(hits)
    res.measurements["annulus_strict_overlaps"] = strict
    res.measurements["annulus_tangencies"] = len(hits) - strict
    for h in hits[:80]:
        res.finding(**h)
    if hits:
        return res.failed(f"{len(hits)} via annulus/mask-opening contacts "
                          f"({strict} strict overlaps + {len(hits) - strict} exact "
                          f"tangencies): these vias cannot be tented or plugged")
    return res.passed("no via annulus reaches a mask opening")


@gate("VIA.IN_PAD_CONTACT", "No via contacts a pad that receives solder",
      requires=("via_mask.pad_contact",))
def via_in_pad(ctx, res):
    spec = ctx.manifest.get("via_mask.pad_contact")
    res.limit(ctx.manifest.constraint(
        "via_mask.pad_contact.populated_pad_attributes", units="pad attribute",
        cid="via_mask.populated_pad_attributes"))
    res.limit(ctx.manifest.constraint(
        "via_mask.mask_dam_rule", units="policy", cid="via_mask.mask_dam_rule"))
    g, rows = _via_survey(ctx)
    paste = _paste_pads(ctx)
    populated = set(spec["populated_pad_attributes"])
    centre_in, partial = [], []
    for row in rows:
        for side, rep in row["sides"].items():
            if not rep["annulus_contacts_opening"]:
                continue
            solderable = (rep["pad"] in paste) if spec.get("require_paste", True) \
                else rep["is_smd"]
            kind = "populated" if solderable else "unpopulated"
            entry = {"net": row["net"], "x_mm": row["x_mm"], "y_mm": row["y_mm"],
                     "side": side, "pad": rep["pad"], "pad_net": rep["pad_net"],
                     "pad_receives_paste": bool(solderable), "class": kind,
                     "contact": ("overlap" if rep["annulus_overlaps_opening"]
                                 else "tangency"),
                     "centre_to_opening_mm": rep["centre_to_opening_mm"]}
            if rep["centre_inside_opening"]:
                centre_in.append(entry)
            else:
                partial.append(entry)
    pop_centre = [e for e in centre_in if e["pad_receives_paste"]]
    pop_partial = [e for e in partial if e["pad_receives_paste"]]
    res.measurements.update({
        "partial_overlap_populated_strict": sum(
            1 for e in partial if e["pad_receives_paste"] and e["contact"] == "overlap"),
        "partial_tangency_populated": sum(
            1 for e in partial if e["pad_receives_paste"] and e["contact"] == "tangency"),
        "centres_inside_openings": len(centre_in),
        "centres_inside_populated": len(pop_centre),
        "centres_inside_unpopulated": len(centre_in) - len(pop_centre),
        "partial_overlap_populated": len(pop_partial),
        "populated_pad_contacts_total": len(pop_centre) + len(pop_partial),
    })
    for e in centre_in + partial:
        res.finding(**e)
    if pop_centre or pop_partial:
        return res.failed(
            f"{len(pop_centre) + len(pop_partial)} vias contact a pad that receives "
            f"solder paste ({len(pop_centre)} centre-inside, {len(pop_partial)} partial); "
            f"solder will wick into the barrel unless a filled/capped process is ordered")
    if centre_in:
        return res.failed(f"{len(centre_in)} vias sit inside a mask opening")
    return res.passed("no via contacts a solderable pad")


def _paste_pads(ctx):
    """Labels of pads that actually receive paste, from the native board."""
    def build():
        import pcbnew
        out = set()
        for fp in ctx.board().Footprints():
            for pad in fp.Pads():
                if pad.IsOnLayer(pcbnew.F_Paste) or pad.IsOnLayer(pcbnew.B_Paste):
                    out.add(f"{fp.GetReference()}.{pad.GetNumber()}")
        return out
    return ctx.cache("paste_pads", build)


# ---------------------------------------------------------------------------
# routing style / topology
# ---------------------------------------------------------------------------

def _track_graph(ctx):
    def build():
        import pcbnew
        board = ctx.board()
        segs, vias = [], []
        for t in board.Tracks():
            if isinstance(t, pcbnew.PCB_VIA):
                vias.append(t)
            else:
                segs.append(t)
        return board, segs, vias
    return ctx.cache("track_graph", build)


def _joins(segs):
    table = defaultdict(list)
    for t in segs:
        k = (t.GetNetCode(), t.GetLayer())
        table[(k, (t.GetStart().x, t.GetStart().y))].append(t)
        table[(k, (t.GetEnd().x, t.GetEnd().y))].append(t)
    return table


@gate("ROUTE.ANGLE_STYLE", "Routing obeys the permitted angle style",
      requires=("routing.permitted_turn_degrees",))
def route_angles(ctx, res):
    permitted_constraint = res.limit(ctx.manifest.constraint(
        "routing.permitted_turn_degrees", units="deg",
        cid="routing.permitted_turn_degrees"))
    permitted = permitted_constraint.value
    tolerance = res.limit(ctx.manifest.constraint(
        "routing.angle_tolerance_deg", units="deg",
        cid="routing.angle_tolerance_deg"))
    board, segs, _ = _track_graph(ctx)
    off = []
    for (k, pt), grp in _joins(segs).items():
        if len(grp) != 2:
            continue
        vs = []
        for t in grp:
            s, e = t.GetStart(), t.GetEnd()
            o = e if (s.x, s.y) == pt else s
            vs.append((o.x - pt[0], o.y - pt[1]))
        (ax, ay), (bx, by) = vs
        na, nb = math.hypot(ax, ay), math.hypot(bx, by)
        if na == 0 or nb == 0:
            continue
        cos = max(-1.0, min(1.0, (ax * bx + ay * by) / (na * nb)))
        turn = 180.0 - math.degrees(math.acos(cos))
        if not any(tolerance.within(turn, p) for p in permitted):
            off.append({"net": grp[0].GetNetname(),
                        "layer": board.GetLayerName(grp[0].GetLayer()),
                        "x_mm": round(pt[0] / 1e6, 3), "y_mm": round(-pt[1] / 1e6, 3),
                        "turn_deg": round(turn, 2)})
    res.measurements["corners_examined"] = sum(
        1 for _k, g in _joins(segs).items() if len(g) == 2)
    res.measurements["off_style_corners"] = len(off)
    widest = max(permitted)
    res.measurements["off_style_sharper_than_permitted_max"] = sum(
        1 for o in off
        if tolerance.violated_maximum(o["turn_deg"] - widest))
    res.measurements["off_style_between_permitted_values"] = sum(
        1 for o in off
        if not tolerance.violated_maximum(o["turn_deg"] - widest))
    for o in sorted(off, key=lambda d: -d["turn_deg"])[:60]:
        res.finding(**o)
    if off:
        return res.failed(f"{len(off)} corners are not on the permitted "
                          f"{permitted} degree geometry")
    return res.passed("every corner is on the permitted geometry")


@gate("ROUTE.TINY_SEGMENTS", "No unjustified sub-minimum track fragments",
      requires=("routing.min_segment_mm",))
def route_tiny(ctx, res):
    constraint = res.limit(ctx.manifest.constraint(
        "routing.min_segment_mm", units="mm", cid="routing.min_segment_mm"))
    limit = constraint.value
    justify = ctx.manifest.get("routing.short_segment_justification", {})
    board, segs, vias = _track_graph(ctx)
    via_pts = {(v.GetPosition().x, v.GetPosition().y) for v in vias}
    pads = []
    for fp in board.Footprints():
        for pad in fp.Pads():
            pads.append(pad)
    unjustified, justified = [], 0
    for t in segs:
        length = t.GetLength() / 1e6
        if not constraint.violated_minimum(length):
            continue
        # A short fragment is legitimate when it is an entry stub into a pad or
        # via: one of its ends lands on one. Anything else is an artifact.
        ends = [(t.GetStart().x, t.GetStart().y), (t.GetEnd().x, t.GetEnd().y)]
        import pcbnew
        touches = any(p in via_pts for p in ends)
        if not touches:
            for pad in pads:
                if pad.GetNetCode() != t.GetNetCode():
                    continue
                if any(pad.HitTest(pcbnew.VECTOR2I(*p)) for p in ends):
                    touches = True
                    break
        if touches and justify.get("allow_pad_or_via_entry", True):
            justified += 1
            continue
        unjustified.append({"net": t.GetNetname(),
                            "layer": board.GetLayerName(t.GetLayer()),
                            "x_mm": round(t.GetStart().x / 1e6, 3),
                            "y_mm": round(-t.GetStart().y / 1e6, 3),
                            "length_mm": round(length, 4)})
    res.measurements["segments_total"] = len(segs)
    res.measurements["below_limit_total"] = justified + len(unjustified)
    res.measurements["below_limit_justified_pad_or_via_entry"] = justified
    res.measurements["below_limit_unjustified"] = len(unjustified)
    for u in sorted(unjustified, key=lambda d: d["length_mm"])[:60]:
        res.finding(**u)
    if unjustified:
        return res.failed(f"{len(unjustified)} track fragments below {limit} mm are "
                          f"not pad or via entries")
    return res.passed(f"{justified} short fragments are all pad/via entry geometry")


@gate("ROUTE.GEOMETRY_HYGIENE", "No duplicate, dangling or crossing copper",
      requires=("routing.hygiene",))
def route_hygiene(ctx, res):
    spec = ctx.manifest.get("routing.hygiene")
    board, segs, vias = _track_graph(ctx)
    problems = []

    # duplicate / collinear-overlapping fragments
    if spec.get("forbid_duplicate_geometry", True):
        bykey = defaultdict(list)
        for t in segs:
            bykey[(t.GetNetCode(), t.GetLayer())].append(t)
        for _k, ts in bykey.items():
            for i, a in enumerate(ts):
                a1 = (a.GetStart().x, a.GetStart().y); a2 = (a.GetEnd().x, a.GetEnd().y)
                for c in ts[i + 1:]:
                    c1 = (c.GetStart().x, c.GetStart().y); c2 = (c.GetEnd().x, c.GetEnd().y)
                    if {a1, a2} == {c1, c2}:
                        problems.append({"issue": "duplicate segment",
                                         "net": a.GetNetname(),
                                         "x_mm": round(a1[0] / 1e6, 3),
                                         "y_mm": round(-a1[1] / 1e6, 3)})

    # different-net crossings
    if spec.get("forbid_net_crossings", True):
        bylayer = defaultdict(list)
        for t in segs:
            bylayer[t.GetLayer()].append(t)

        def side(ax, ay, bx, by, cx, cy):
            v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            return 0 if abs(v) < 1 else (1 if v > 0 else -1)
        for layer, ts in bylayer.items():
            for i, a in enumerate(ts):
                a1 = (a.GetStart().x, a.GetStart().y); a2 = (a.GetEnd().x, a.GetEnd().y)
                for c in ts[i + 1:]:
                    if a.GetNetCode() == c.GetNetCode():
                        continue
                    c1 = (c.GetStart().x, c.GetStart().y); c2 = (c.GetEnd().x, c.GetEnd().y)
                    if (side(*c1, *c2, *a1) * side(*c1, *c2, *a2) < 0 and
                            side(*a1, *a2, *c1) * side(*a1, *a2, *c2) < 0):
                        problems.append({"issue": "different-net crossing",
                                         "nets": f"{a.GetNetname()}/{c.GetNetname()}",
                                         "layer": board.GetLayerName(layer),
                                         "x_mm": round(a1[0] / 1e6, 3),
                                         "y_mm": round(-a1[1] / 1e6, 3)})

    # dangling ends
    if spec.get("forbid_dangling", True):
        import pcbnew
        via_pts = [(v.GetPosition().x, v.GetPosition().y,
                    v.GetWidth(pcbnew.F_Cu) / 2.0, v.GetNetCode()) for v in vias]
        pads = [(pad, pad.GetNetCode()) for fp in board.Footprints() for pad in fp.Pads()]
        for (k, pt), grp in _joins(segs).items():
            if len(grp) != 1:
                continue
            net, layer = k
            v = pcbnew.VECTOR2I(*pt)
            if any(n == net and pad.HitTest(v) for pad, n in pads):
                continue
            if any(n == net and math.hypot(vx - pt[0], vy - pt[1]) <= r
                   for vx, vy, r, n in via_pts):
                continue
            other = grp[0]
            hit = False
            for o in segs:
                if o is other or o.GetNetCode() != net or o.GetLayer() != layer:
                    continue
                if _point_seg(pt, (o.GetStart().x, o.GetStart().y),
                              (o.GetEnd().x, o.GetEnd().y)) <= o.GetWidth() / 2.0:
                    hit = True
                    break
            if not hit:
                problems.append({"issue": "dangling track end",
                                 "net": other.GetNetname(),
                                 "layer": board.GetLayerName(layer),
                                 "x_mm": round(pt[0] / 1e6, 3),
                                 "y_mm": round(-pt[1] / 1e6, 3)})

    counts = Counter(p["issue"] for p in problems)
    res.measurements["issues"] = dict(counts)
    for p in problems[:60]:
        res.finding(**p)
    if problems:
        return res.failed("; ".join(f"{v} {k}" for k, v in counts.items()))
    return res.passed("no duplicate, dangling or crossing copper")


def _point_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dx == 0 and dy == 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))
