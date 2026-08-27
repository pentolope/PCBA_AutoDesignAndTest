"""Geometry-derived board parasitics: defensible quantities only.

The first extraction layer deliberately stops where its evidence
stops. Everything here is derived from board geometry plus explicitly
supplied physical facts (per-layer finished copper thickness, board
thickness, a named copper resistivity). It produces:

  * per-net segment inventories - layer, width, length;
  * per-net totals - copper length, length by layer, via count, a
    through-via barrel-length estimate;
  * a DC segment-resistance SUM per net.

The resistance figure is named ``segment_resistance_sum_ohm`` because
that is what it is: the series sum of every segment's resistance. For
an unbranched two-terminal net that equals the end-to-end DC
resistance; for a branched net it is a copper inventory metric and NOT
a two-terminal resistance, and the record says so. No inductance or
capacitance is emitted here at all: no source-supported model for them
is wired in yet, and unknown positive contributions stay unknown
rather than becoming invented constants.

Resistivity defaults to the International Annealed Copper Standard
(IEC 60028): 1.7241e-8 ohm metre at 20 degrees C. It is a named,
citable physical standard - the one constant this module carries - and
callers may override it explicitly.

Path-level delay and skew are NOT recomputed here: the timing gates
already measure declared interface paths with their own recorded
uncertainty, and ``paths_from_validation`` lifts those recorded
measurements into a baseline report instead of duplicating the
machinery.
"""

from __future__ import annotations

import hashlib
import json

#: IEC 60028 International Annealed Copper Standard, 20 C.
IACS_RESISTIVITY_OHM_M = 1.7241e-8


class ExtractionError(Exception):
    """Extraction cannot proceed as asked. Always blocks."""


def _require_positive(label, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value != value or value <= 0 \
            or value in (float("inf"), float("-inf")):
        raise ExtractionError(
            "{} is {!r}, which is not a usable positive "
            "number".format(label, value))


def extract_net(board, net_name, copper_thickness_mm,
                board_thickness_mm,
                resistivity_ohm_m=IACS_RESISTIVITY_OHM_M):
    """Segment inventory and derived DC quantities for one net.

    `copper_thickness_mm` maps layer names to finished copper
    thickness; a segment on a layer without a stated thickness refuses
    rather than guessing. `board_thickness_mm` prices through-via
    barrels as an ESTIMATE (every via counted at full board
    thickness); the field name carries the word estimate for exactly
    that reason.
    """
    import pcbnew
    _require_positive("board_thickness_mm", board_thickness_mm)
    _require_positive("resistivity_ohm_m", resistivity_ohm_m)
    segments = []
    via_count = 0
    seen = False
    for track in board.GetTracks():
        if track.GetNetname() != net_name:
            continue
        seen = True
        if track.GetClass() in ("PCB_VIA", "VIA"):
            via_count += 1
            continue
        layer = track.GetLayerName()
        segments.append({
            "layer": layer,
            "width_mm": round(pcbnew.ToMM(track.GetWidth()), 6),
            "length_mm": round(pcbnew.ToMM(track.GetLength()), 6),
        })
    if not seen:
        raise ExtractionError(
            "net {!r} has no copper on this board; an absent net is "
            "reported as absent, not as zeros".format(net_name))
    segments.sort(key=lambda s: (s["layer"], -s["length_mm"],
                                 s["width_mm"]))
    by_layer = {}
    resistance = 0.0
    for segment in segments:
        by_layer[segment["layer"]] = round(
            by_layer.get(segment["layer"], 0.0)
            + segment["length_mm"], 6)
        thickness = copper_thickness_mm.get(segment["layer"])
        if thickness is None:
            raise ExtractionError(
                "no finished copper thickness was supplied for layer "
                "{!r}; resistance is not computed from a guessed "
                "cross-section".format(segment["layer"]))
        _require_positive(
            "copper thickness on {}".format(segment["layer"]),
            thickness)
        area_m2 = (segment["width_mm"] / 1000.0) * (thickness / 1000.0)
        resistance += resistivity_ohm_m \
            * (segment["length_mm"] / 1000.0) / area_m2
    return {
        "net": net_name,
        "segments": segments,
        "totals": {
            "copper_length_mm": round(
                sum(s["length_mm"] for s in segments), 6),
            "length_by_layer_mm": by_layer,
            "via_count": via_count,
            "via_barrel_estimate_mm": round(
                via_count * board_thickness_mm, 6),
        },
        "dc": {
            "segment_resistance_sum_ohm": round(resistance, 9),
            "resistivity_ohm_m": resistivity_ohm_m,
            "resistivity_source": "IEC 60028 international annealed "
                                  "copper standard, 20 C"
                                  if resistivity_ohm_m
                                  == IACS_RESISTIVITY_OHM_M
                                  else "caller-supplied",
            "meaning": "the series sum of per-segment DC resistances: "
                       "equal to the two-terminal DC resistance only "
                       "for an unbranched net; for a branched net it "
                       "is a copper inventory metric, and no "
                       "two-terminal claim is made",
        },
    }


def paths_from_validation(validation):
    """Lift the recorded interface-path measurements from a validation.

    Takes a parsed validation.json document and returns the
    TIMING.INTERCONNECT_DELAY and TIMING.INTERCONNECT_SKEW measurement
    blocks verbatim - the gates' own recorded numbers, with their own
    stated scopes and uncertainties, never recomputed here.
    """
    lifted = {}
    for gate in validation.get("gates", []):
        if gate.get("gate") in ("TIMING.INTERCONNECT_DELAY",
                                "TIMING.INTERCONNECT_SKEW"):
            lifted[gate["gate"]] = {
                "status": gate.get("status"),
                "measurements": gate.get("measurements"),
            }
    if not lifted:
        raise ExtractionError(
            "the validation document carries no interconnect timing "
            "gates; a baseline without them would silently lack the "
            "path measurements it promises")
    return lifted


def baseline_report(board_file, board, net_names, copper_thickness_mm,
                    board_thickness_mm, validation=None):
    """A machine-readable geometry baseline for one board revision.

    Identified by the board file's SHA-256 so an A/B comparison can
    prove which copper it measured. Net extraction is per `net_names`
    (all required to exist - fail closed); recorded interface-path
    measurements are embedded when a validation document is given.
    """
    with open(board_file, "rb") as handle:
        board_sha = hashlib.sha256(handle.read()).hexdigest()
    nets = {}
    for name in net_names:
        nets[name] = extract_net(board, name, copper_thickness_mm,
                                 board_thickness_mm)
    report = {
        "kind": "board-geometry-baseline",
        "board_file_sha256": board_sha,
        "board_thickness_mm": board_thickness_mm,
        "copper_thickness_mm": dict(sorted(
            copper_thickness_mm.items())),
        "nets": nets,
        "interface_paths": paths_from_validation(validation)
        if validation is not None else None,
        "notes": [
            "geometry-derived quantities only; no inductance or "
            "capacitance is claimed anywhere in this report",
            "interface_paths, when present, are the timing gates' own "
            "recorded measurements with their stated scopes and "
            "uncertainties",
        ],
    }
    return report


def write_report(report, out_path):
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
        handle.write("\n")
