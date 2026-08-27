"""Geometry-derived board parasitics with provenance-bearing inputs.

The first extraction layer deliberately stops where its evidence
stops, and - since the provenance hardening - every physical input
used to derive an electrical quantity is a PARAMETER RECORD, not a
bare number:

    {"value", "units", "source_type", "source", "digest",
     "applicability"}

with ``source_type`` one of ``approved-evidence`` (resolved from the
approved fabricator catalog, digest = the catalog's normalized
SHA-256), ``derived`` (computed from such evidence, derivation
stated), or ``caller-declared`` (supplied by the caller and forever
distinguishable from evidence). A bare float where a parameter record
is required refuses: an arbitrary CLI number can never masquerade as
approved physical evidence.

``approved_finished_copper`` resolves finished copper thickness from
the approved catalog with EXACTLY the semantics the impedance solver
uses (category copper-finished, position and weight scoping, mil
units, exactly one match or refusal), so extraction and impedance can
never disagree about what the fabricator's finished copper is - and
so the nominal foil weight (35 um for 1 oz) can never silently stand
in for the finished thickness (1.6 mil = 40.64 um external).

What is emitted per net: segment inventories (layer, width, length),
totals (copper length, per-layer lengths, via count, an
estimate-labeled through-via barrel length) and a DC
``segment_resistance_sum_ohm`` - the series sum of segment
resistances, equal to a two-terminal resistance only for an unbranched
net, as its ``meaning`` states. No inductance and no capacitance are
emitted anywhere: no source-supported model for them is wired in, and
unknown positive contributions stay unknown.

Resistivity defaults to the International Annealed Copper Standard
(IEC 60028): 1.7241e-8 ohm metre at 20 degrees C - the one physical
constant this module carries.
"""

from __future__ import annotations

import hashlib
import json

#: IEC 60028 International Annealed Copper Standard, 20 C.
IACS_RESISTIVITY_OHM_M = 1.7241e-8

MIL_TO_MM = 0.0254

_SOURCE_TYPES = ("approved-evidence", "derived", "caller-declared")
_PARAMETER_KEYS = {"value", "units", "source_type", "source",
                   "digest", "applicability"}


class ExtractionError(Exception):
    """Extraction cannot proceed as asked. Always blocks."""


def _finite_positive(label, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value != value or value <= 0 \
            or value in (float("inf"), float("-inf")):
        raise ExtractionError(
            "{} is {!r}, which is not a usable positive "
            "number".format(label, value))


def physical_parameter(value, units, source_type, source,
                       digest=None, applicability=None):
    """One provenance-bearing physical input."""
    _finite_positive("parameter value", value)
    if source_type not in _SOURCE_TYPES:
        raise ExtractionError(
            "source_type {!r} is not one of {}".format(
                source_type, list(_SOURCE_TYPES)))
    if not isinstance(units, str) or not units:
        raise ExtractionError("a parameter needs units")
    if not isinstance(source, str) or not source:
        raise ExtractionError(
            "a parameter needs a source identity; a number without an "
            "origin is not physical evidence")
    return {"value": value, "units": units,
            "source_type": source_type, "source": source,
            "digest": digest, "applicability": applicability}


def validate_parameter(record, label):
    if not isinstance(record, dict) or \
            set(record) != _PARAMETER_KEYS:
        raise ExtractionError(
            "{} must be a physical parameter record with exactly "
            "keys {} - a bare number cannot masquerade as physical "
            "evidence".format(label, sorted(_PARAMETER_KEYS)))
    _finite_positive("{} value".format(label), record["value"])
    if record["source_type"] not in _SOURCE_TYPES:
        raise ExtractionError(
            "{} source_type {!r} is not one of {}".format(
                label, record["source_type"], list(_SOURCE_TYPES)))
    return record


def approved_finished_copper(approved_snapshot, assignments):
    """Finished copper thickness per layer, from approved evidence.

    `assignments` maps layer name -> (position, weight_oz) with
    position "external" or "internal". Resolution mirrors the
    impedance solver's conductor lookup exactly: category
    copper-finished, applies.position and applies.copper_weights_oz
    scoping, units mil, exactly one matching record or refusal - so
    extraction and impedance can never disagree, and nominal foil
    weight can never stand in for finished thickness.
    """
    capabilities = approved_snapshot["normalized"]["capabilities"]
    digest = approved_snapshot["normalized_sha256"]
    parameters = {}
    for layer, (position, weight_oz) in sorted(assignments.items()):
        if position not in ("external", "internal"):
            raise ExtractionError(
                "layer {!r} position {!r} must be external or "
                "internal".format(layer, position))
        matches = []
        for identity in sorted(capabilities):
            record = capabilities[identity]
            if record.get("category") != "copper-finished":
                continue
            applies = record.get("applies") or {}
            if applies.get("position") != position:
                continue
            if weight_oz not in (applies.get("copper_weights_oz")
                                 or []):
                continue
            if record.get("units") != "mil":
                continue
            matches.append((identity, record))
        if len(matches) != 1:
            raise ExtractionError(
                "{} finished-conductor record(s) cover {} {:g} oz "
                "copper; exactly one must, so extraction refuses "
                "rather than guessing".format(
                    len(matches), position, weight_oz))
        identity, record = matches[0]
        parameters[layer] = physical_parameter(
            round(record["value"] * MIL_TO_MM, 6), "mm",
            "approved-evidence", identity, digest=digest,
            applicability="{} {:g} oz finished copper".format(
                position, weight_oz))
    return parameters


def caller_declared_copper(values_mm):
    """Caller-supplied thicknesses, marked as exactly that forever."""
    return {
        layer: physical_parameter(
            value, "mm", "caller-declared",
            "caller-declared thickness for {}".format(layer),
            applicability="caller's own responsibility; not "
                          "fabricator evidence")
        for layer, value in sorted(values_mm.items())
    }


def extract_net(board, net_name, copper_parameters,
                board_thickness_parameter,
                resistivity_ohm_m=IACS_RESISTIVITY_OHM_M):
    """Segment inventory and derived DC quantities for one net.

    `copper_parameters` maps layer names to physical parameter
    records; `board_thickness_parameter` is one such record. A
    segment on a layer without a stated parameter refuses.
    """
    import pcbnew
    for layer, record in copper_parameters.items():
        validate_parameter(record, "copper thickness on "
                                   "{}".format(layer))
    validate_parameter(board_thickness_parameter, "board thickness")
    _finite_positive("resistivity_ohm_m", resistivity_ohm_m)
    board_thickness_mm = board_thickness_parameter["value"]
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
        parameter = copper_parameters.get(segment["layer"])
        if parameter is None:
            raise ExtractionError(
                "no finished copper thickness was supplied for layer "
                "{!r}; resistance is not computed from a guessed "
                "cross-section".format(segment["layer"]))
        thickness = parameter["value"]
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

    The timing gates' own recorded numbers with their own stated
    scopes and uncertainties, never recomputed here.
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


def baseline_report(board_file, board, net_names, copper_parameters,
                    board_thickness_parameter, validation=None):
    """A machine-readable geometry baseline for one board revision.

    Bound to the exact board file by SHA-256, with every physical
    input carried as a provenance record in `physical_inputs`.
    """
    with open(board_file, "rb") as handle:
        board_sha = hashlib.sha256(handle.read()).hexdigest()
    nets = {}
    for name in net_names:
        nets[name] = extract_net(board, name, copper_parameters,
                                 board_thickness_parameter)
    return {
        "kind": "board-geometry-baseline",
        "board_file_sha256": board_sha,
        "physical_inputs": {
            "copper_thickness_mm": dict(sorted(
                copper_parameters.items())),
            "board_thickness_mm": board_thickness_parameter,
        },
        "nets": nets,
        "interface_paths": paths_from_validation(validation)
        if validation is not None else None,
        "notes": [
            "geometry-derived quantities only; no inductance or "
            "capacitance is claimed anywhere in this report",
            "every physical input above carries its provenance; "
            "caller-declared inputs are permanently distinguishable "
            "from approved evidence",
            "interface_paths, when present, are the timing gates' own "
            "recorded measurements with their stated scopes and "
            "uncertainties",
        ],
    }


def interconnect_model_from_net(net_record, board_sha256,
                                two_terminal_asserted_by=None):
    """A simulation model record from one extracted net - DC only.

    The returned record conforms to the simulation registry's schema
    and covers EXACTLY the phenomenon the extraction supplies:
    interconnect_dc at evidence class geometry-derived. It cannot
    satisfy an interconnect_si or power_integrity requirement, by
    construction of the coverage contract. A SPICE resistor
    subcircuit is attached ONLY when the caller asserts the net is an
    unbranched two-terminal run - that assertion is recorded as the
    caller's, because the segment sum equals a two-terminal
    resistance only then.
    """
    identity = "net:{}@{}".format(net_record["net"], board_sha256[:12])
    record = {
        "identity": identity,
        "kind": "board-interconnect",
        "coverage": {"interconnect_dc": "geometry-derived"},
        "provenance": {
            "source": "pcbqa.extract segment inventory",
            "board_file_sha256": board_sha256,
            "resistivity_source":
                net_record["dc"]["resistivity_source"],
        },
        "omissions": [
            "inductance", "capacitance", "distributed effects",
            "frequency dependence", "temperature dependence beyond "
            "the 20 C resistivity reference",
        ],
        "notes": [net_record["dc"]["meaning"]],
    }
    if two_terminal_asserted_by:
        record["provenance"]["two_terminal_asserted_by"] = \
            two_terminal_asserted_by
        record["spice"] = (
            ".subckt {identity} a b\n"
            "R1 a b {value}\n"
            ".ends".format(
                identity=identity,
                value=net_record["dc"][
                    "segment_resistance_sum_ohm"]))
    return record


def write_report(report, out_path):
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)
        handle.write("\n")
