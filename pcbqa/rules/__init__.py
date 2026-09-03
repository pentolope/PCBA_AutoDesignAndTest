"""Reusable, configurable rule types for electrical and mechanical contracts.

These carry no knowledge of any particular board. Each takes a declarative spec
from the project manifest and produces findings against the native KiCad data.
"""

from __future__ import annotations

import heapq
import math
import re
from collections import defaultdict

SNAP = 30000          # 0.03 mm, in KiCad internal units


def pad_label(fp, pad):
    return f"{fp.GetReference()}.{pad.GetNumber()}"


def iter_pads(board):
    for fp in board.Footprints():
        for pad in fp.Pads():
            yield fp, pad


class CopperGraph:
    """Net-local graph of tracks and vias for electrical path measurement."""

    def __init__(self, board, net_name):
        import pcbnew
        self.board = board
        self.net = net_name
        self.adj = defaultdict(list)
        self.vias = 0
        self.layers = set()
        self.segments = []
        for t in board.Tracks():
            if t.GetNetname() != net_name:
                continue
            if isinstance(t, pcbnew.PCB_VIA):
                self.vias += 1
                p = t.GetPosition()
                a = self._key(p.x, p.y, t.TopLayer())
                b = self._key(p.x, p.y, t.BottomLayer())
                self.adj[a].append((b, 0.0))
                self.adj[b].append((a, 0.0))
            else:
                self.layers.add(board.GetLayerName(t.GetLayer()))
                self.segments.append(t)
                s, e = t.GetStart(), t.GetEnd()
                length = t.GetLength() / 1e6
                a = self._key(s.x, s.y, t.GetLayer())
                b = self._key(e.x, e.y, t.GetLayer())
                self.adj[a].append((b, length))
                self.adj[b].append((a, length))

    @staticmethod
    def _key(x, y, layer):
        return (round(x / SNAP), round(y / SNAP), layer)

    def attach(self, pad):
        """Every graph node the pad physically covers."""
        nodes = set()
        for t in self.segments:
            for point in (t.GetStart(), t.GetEnd()):
                if pad.IsOnLayer(t.GetLayer()) and pad.HitTest(point):
                    nodes.add(self._key(point.x, point.y, t.GetLayer()))
        return nodes

    def shortest(self, starts, targets):
        best = None
        for start in starts:
            dist = {start: 0.0}
            pq = [(0.0, start)]
            while pq:
                d, u = heapq.heappop(pq)
                if d > dist.get(u, 1e18):
                    continue
                for v, w in self.adj[u]:
                    nd = d + w
                    if nd < dist.get(v, 1e18):
                        dist[v] = nd
                        heapq.heappush(pq, (nd, v))
            for t in targets:
                if t in dist and (best is None or dist[t] < best):
                    best = dist[t]
        return best


class NetTopologyRule:
    """Driver-to-load electrical path length across a family of nets.

    Connectivity comes from actual copper intersection (see pcbqa.connectivity),
    not from endpoint equality, so a track that lands inside a via annulus or
    part-way along another track is correctly seen as connected.
    """

    def __init__(self, spec):
        self.spec = spec
        self.id = spec["id"]

    def evaluate(self, board, pad_polygon):
        from ..connectivity import NetGraph
        net_re = re.compile(self.spec["net_regex"])
        src_re = re.compile(self.spec["source_pad_regex"])
        load_re = re.compile(self.spec["load_pad_regex"])
        nets = sorted({t.GetNetname() for t in board.Tracks()
                       if net_re.match(t.GetNetname() or "")})
        measured, problems = [], []
        for net in nets:
            graph = NetGraph(board, net, pad_polygon)
            sources, loads = [], []
            for fp, pad in iter_pads(board):
                if pad.GetNetname() != net:
                    continue
                label = pad_label(fp, pad)
                if src_re.match(label):
                    sources.append(label)
                elif load_re.match(label):
                    loads.append(label)
            if not sources or not loads:
                problems.append({"net": net, "issue": "driver or load pad not found",
                                 "sources": sources, "loads": loads})
                continue
            paths = {l: graph.path_length(sources, l) for l in loads}
            unreachable = sorted(l for l, d in paths.items() if d is None)
            good = [d for d in paths.values() if d is not None]
            measured.append({
                "net": net,
                "driver_pads": sorted(sources),
                "load_pads": sorted(loads),
                "path_mm": {l: (None if d is None else round(d, 3))
                            for l, d in sorted(paths.items())},
                "vias": graph.vias(),
                "layers": graph.layers_used(board),
                "total_track_copper_mm": round(graph.total_track_mm(), 3),
                "max_path_mm": round(max(good), 3) if good else None,
                "min_path_mm": round(min(good), 3) if good else None,
                "branch_points": graph.branch_points(),
            })
            if unreachable:
                problems.append({"net": net,
                                 "issue": "load not reachable along copper",
                                 "loads": unreachable})
        return measured, problems

    def check_limits(self, measured):
        problems = []
        maxima = [m["max_path_mm"] for m in measured if m["max_path_mm"] is not None]
        if "max_spread_mm" in self.spec and maxima:
            spread = max(maxima) - min(maxima)
            if spread > self.spec["max_spread_mm"]:
                problems.append({
                    "issue": "branch length spread exceeds the requirement",
                    "measured_spread_mm": round(spread, 3),
                    "limit_mm": self.spec["max_spread_mm"],
                    "min_mm": round(min(maxima), 3), "max_mm": round(max(maxima), 3),
                    "definition": "longest driver-to-load path per net; vias contribute zero"})
        if "max_vias_per_net" in self.spec:
            for m in measured:
                if m["vias"] > self.spec["max_vias_per_net"]:
                    problems.append({"issue": "via budget exceeded", "net": m["net"],
                                     "vias": m["vias"],
                                     "limit": self.spec["max_vias_per_net"]})
        if "permitted_layers" in self.spec:
            allowed = set(self.spec["permitted_layers"])
            for m in measured:
                extra = sorted(set(m["layers"]) - allowed)
                if extra:
                    problems.append({"issue": "net uses a layer it is not allowed on",
                                     "net": m["net"], "layers": extra,
                                     "permitted": sorted(allowed)})
        return problems


class ConnectorContractRule:
    """Board-to-board / cable connector contract, checked against the native PCB."""

    def __init__(self, spec, tokens):
        self.spec = spec
        self.tokens = tokens
        self.id = spec["id"]

    def evaluate(self, board, doc_texts):
        ref = self.spec["reference"]
        fp = board.FindFootprintByReference(ref)
        if fp is None:
            return [{"issue": "connector not present on the board",
                     "reference": ref}], {}
        # A pad with no number is mechanical - a mounting hole, a locating
        # peg - not a position on the connector. Counting it made
        # required_positions mean something no datasheet states, and let a
        # locating hole set the measured row count and pitch. They are
        # reported, never counted.
        numbered = [p for p in fp.Pads() if p.GetNumber()]
        pads = {p.GetNumber(): p for p in numbered}
        facts = {
            "reference": ref,
            "footprint_id": fp.GetFPIDAsString(),
            "value": fp.GetValue(),
            "description": fp.GetLibDescription() or "",
            "side": "back" if fp.IsFlipped() else "front",
            "positions": len(pads),
            "mechanical_pads": len(list(fp.Pads())) - len(numbered),
            "dnp": bool(fp.IsDNP()),
            "excluded_from_bom": bool(fp.IsExcludedFromBOM()),
            "models": [m.m_Filename for m in fp.Models()],
        }
        facts.update(self._grid(pads))
        problems = []

        if "required_positions" in self.spec and facts["positions"] != self.spec["required_positions"]:
            problems.append({"issue": "position count mismatch", "reference": ref,
                             "expected": self.spec["required_positions"],
                             "measured": facts["positions"],
                             "counted": "distinct pad numbers on the footprint; "
                                        "lands sharing a number count once and "
                                        "unnumbered mechanical pads are not "
                                        "counted"})
        if "required_rows" in self.spec and facts.get("rows") != self.spec["required_rows"]:
            problems.append({"issue": "row count mismatch", "reference": ref,
                             "expected": self.spec["required_rows"],
                             "measured": facts.get("rows"),
                             "counted": "min(distinct pad-centre x, distinct "
                                        "pad-centre y) over one pad per number"})
        if "required_pitch_mm" in self.spec and facts.get("pitch_mm") is not None:
            if abs(facts["pitch_mm"] - self.spec["required_pitch_mm"]) > 0.02:
                problems.append({"issue": "pitch mismatch", "reference": ref,
                                 "expected_mm": self.spec["required_pitch_mm"],
                                 "measured_mm": facts["pitch_mm"]})
        if "required_side" in self.spec and facts["side"] != self.spec["required_side"]:
            problems.append({"issue": "mounting side mismatch", "reference": ref,
                             "expected": self.spec["required_side"],
                             "measured": facts["side"]})
        pop = self.spec.get("population", {})
        for key, attr in (("dnp", "dnp"), ("exclude_from_bom", "excluded_from_bom")):
            if key in pop and facts[attr] != pop[key]:
                problems.append({"issue": f"{key} state mismatch", "reference": ref,
                                 "expected": pop[key], "measured": facts[attr]})

        want_gender = self.spec.get("required_gender")
        if want_gender:
            evidence = {
                "footprint_id": self._gender_of(facts["footprint_id"]),
                "model_3d": self._gender_of(" ".join(facts["models"])),
                "description": self._gender_of(facts["description"]),
                "value": self._gender_of(facts["value"]),
            }
            facts["gender_evidence"] = evidence
            if not any(evidence.values()):
                problems.append({"issue": "no artifact states the connector gender",
                                 "reference": ref, "required": want_gender})
            for source, got in evidence.items():
                if got and got != want_gender:
                    problems.append({"issue": "artifact contradicts the required gender",
                                     "artifact": source, "states": got,
                                     "required": want_gender, "reference": ref})

        prefix = self.spec.get("unconnected_net_prefix", "unconnected-")
        for pin, expected in (self.spec.get("pin_map") or {}).items():
            pad = pads.get(pin)
            if pad is None:
                problems.append({"issue": "contract names a pin the footprint lacks",
                                 "pin": pin, "reference": ref})
                continue
            actual = pad.GetNetname()
            if expected is None:
                if not actual.startswith(prefix):
                    problems.append({"issue": "pin should be unconnected", "pin": pin,
                                     "actual_net": actual, "reference": ref})
            elif actual != expected:
                problems.append({"issue": "pin net mismatch", "pin": pin,
                                 "expected_net": expected, "actual_net": actual,
                                 "reference": ref})

        docs = self.spec.get("documentation", {})
        for banned in docs.get("must_not_claim", []):
            pattern = re.compile(banned["pattern"], re.I)
            for rel, text in sorted(doc_texts.items()):
                for m in pattern.finditer(text):
                    problems.append({
                        "issue": "documentation asserts a superseded interconnect",
                        "label": banned["label"], "file": rel,
                        "line": text[:m.start()].count("\n") + 1,
                        "text": m.group(0)[:100], "reference": ref})
        for need in docs.get("must_claim", []):
            pattern = re.compile(need["pattern"], re.I)
            if not any(pattern.search(t) for t in doc_texts.values()):
                problems.append({"issue": "no document states a required property "
                                          "of the interconnect",
                                 "label": need["label"], "reference": ref})
        return problems, facts

    @staticmethod
    def _grid(pads):
        pts = [(p.GetPosition().x / 1e6, p.GetPosition().y / 1e6) for p in pads.values()]
        if len(pts) < 2:
            return {"rows": None, "pitch_mm": None}
        xs = sorted({round(v[0], 3) for v in pts})
        ys = sorted({round(v[1], 3) for v in pts})
        spacing = []
        for axis in (xs, ys):
            spacing += [round(b - a, 3) for a, b in zip(axis, axis[1:]) if b - a > 1e-6]
        return {"rows": min(len(xs), len(ys)),
                "pitch_mm": min(spacing) if spacing else None}

    def _gender_of(self, text):
        low = (text or "").lower()
        for gender, words in self.tokens.items():
            for w in words:
                if w.lower() in low:
                    return gender
        return None


class PlacementRule:
    """Polar placement and orientation contract for a family of footprints."""

    def __init__(self, spec):
        self.spec = spec
        self.id = spec["id"]

    def evaluate(self, board, origin_mm):
        pattern = re.compile(self.spec["reference_regex"])
        members = [fp for fp in board.Footprints() if pattern.match(fp.GetReference())]
        problems, measured = [], []
        if "count" in self.spec and len(members) != self.spec["count"]:
            problems.append({"issue": "population count mismatch",
                             "expected": self.spec["count"], "measured": len(members)})
        offset = self.spec.get("reference_offset_local_mm", [0.0, 0.0])
        for fp in sorted(members, key=lambda f: f.GetReference()):
            px, py = self._local_to_board(fp, offset)
            x = px - origin_mm[0]
            y = -(py - origin_mm[1])
            radius = math.hypot(x, y)
            angle = math.degrees(math.atan2(y, x)) % 360.0
            rot = fp.GetOrientationDegrees() % 360.0
            entry = {"reference": fp.GetReference(), "radius_mm": round(radius, 4),
                     "angle_deg": round(angle, 4), "rotation_deg": round(rot, 3)}
            measured.append(entry)
            polar = self.spec.get("polar")
            if polar:
                if abs(radius - polar["radius_mm"]) > polar.get("tolerance_mm", 0.05):
                    problems.append({**entry, "issue": "radius out of tolerance",
                                     "expected_mm": polar["radius_mm"]})
                pitch = polar.get("angular_pitch_deg")
                if pitch:
                    nearest = round(angle / pitch) * pitch
                    err = abs(((angle - nearest + 180) % 360) - 180)
                    if err > polar.get("angle_tolerance_deg", 0.05):
                        problems.append({**entry, "issue": "azimuth off the array grid",
                                         "nearest_grid_deg": round(nearest, 3),
                                         "error_deg": round(err, 4)})
            orient = self.spec.get("rotation")
            if orient and orient.get("mode") == "radial":
                want = (angle + orient.get("offset_deg", 0.0)) % 360.0
                err = abs(((rot - want + 180) % 360) - 180)
                if err > orient.get("tolerance_deg", 0.1):
                    problems.append({**entry, "issue": "rotation is not radial",
                                     "expected_deg": round(want, 3),
                                     "error_deg": round(err, 4)})
        return measured, problems

    @staticmethod
    def _local_to_board(fp, offset):
        ox, oy = offset
        if fp.IsFlipped():
            ox = -ox
        angle = math.radians(fp.GetOrientationDegrees())
        c, s = math.cos(angle), math.sin(angle)
        pos = fp.GetPosition()
        return (pos.x / 1e6 + ox * c + oy * s,
                pos.y / 1e6 - ox * s + oy * c)
