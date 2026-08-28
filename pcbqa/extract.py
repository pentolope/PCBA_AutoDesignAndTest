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

#: The extraction contract version: recorded in every baseline and
#: in every derived simulation model, so a consumer can tell which
#: semantics produced a number.
EXTRACT_VERSION = "3"

#: IEC 60028 International Annealed Copper Standard, 20 C.
IACS_RESISTIVITY_OHM_M = 1.7241e-8

#: The reference temperature of the IEC 60028 resistivity value.
IACS_REFERENCE_TEMPERATURE_C = 20.0

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
    """One provenance-bearing physical input.

    ``approved-evidence`` records cannot be minted here: only a
    resolver that actually reads the approved catalog (such as
    ``approved_finished_copper``) may produce them, so a caller can
    never accidentally dress an arbitrary number up as fabricator
    evidence. Everything else states what it is and stays
    distinguishable forever.
    """
    if source_type == "approved-evidence":
        raise ExtractionError(
            "approved-evidence records are minted only by resolvers "
            "that read the approved catalog (approved_finished_copper "
            "and peers); a caller-assembled number can never carry "
            "that label")
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


def _approved_parameter(value, units, source, digest,
                        applicability):
    """Resolver-only minting of an approved-evidence record."""
    _finite_positive("parameter value", value)
    record = {"value": value, "units": units,
              "source_type": "approved-evidence", "source": source,
              "digest": digest, "applicability": applicability}
    return validate_parameter(record, "approved parameter")


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
    if record["source_type"] == "approved-evidence":
        digest = record["digest"]
        if not (isinstance(digest, str) and len(digest) == 64
                and set(digest) <= set("0123456789abcdef")):
            raise ExtractionError(
                "{}: an approved-evidence record must carry the "
                "approved catalog's 64-hex-character normalized "
                "SHA-256 digest, not {!r}".format(label, digest))
        for key in ("source", "applicability"):
            if not isinstance(record[key], str) or not record[key]:
                raise ExtractionError(
                    "{}: an approved-evidence record needs a "
                    "nonempty {} naming the canonical capability "
                    "record it resolved".format(label, key))
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
        parameters[layer] = _approved_parameter(
            round(record["value"] * MIL_TO_MM, 6), "mm",
            identity, digest,
            "{} {:g} oz finished copper".format(position, weight_oz))
    return parameters


def copper_assignments_from_requirements(requirements,
                                         copper_layer_stack):
    """(position, weight_oz) per layer, from declared requirements.

    ``requirements`` is the board's own fabrication-requirements
    document (copper_layers, outer_copper_oz, inner_copper_oz);
    ``copper_layer_stack`` is the board's copper layer names in
    front-to-back stack order. The first and last layers are
    external, everything between is internal - and a stack whose
    length contradicts the declared layer count refuses rather than
    guessing which document is wrong.
    """
    for key in ("copper_layers", "outer_copper_oz",
                "inner_copper_oz"):
        if key not in requirements:
            raise ExtractionError(
                "the fabrication requirements declare no {}; copper "
                "assignments are not invented".format(key))
    if not isinstance(copper_layer_stack, (list, tuple))             or len(copper_layer_stack) < 2:
        raise ExtractionError(
            "copper_layer_stack must list at least the two external "
            "layers in stack order")
    if len(copper_layer_stack) != requirements["copper_layers"]:
        raise ExtractionError(
            "the board exposes {} copper layers but the fabrication "
            "requirements declare {}; the contradiction blocks "
            "rather than being resolved by guesswork".format(
                len(copper_layer_stack),
                requirements["copper_layers"]))
    assignments = {}
    last = len(copper_layer_stack) - 1
    for index, layer in enumerate(copper_layer_stack):
        if index in (0, last):
            assignments[layer] = ("external",
                                  requirements["outer_copper_oz"])
        else:
            assignments[layer] = ("internal",
                                  requirements["inner_copper_oz"])
    return assignments


def requirements_board_thickness(requirements, requirements_digest):
    """Board thickness as a parameter derived from the declared
    fabrication requirements, digest-bound to that document."""
    if "board_thickness_mm" not in requirements:
        raise ExtractionError(
            "the fabrication requirements declare no "
            "board_thickness_mm; thickness is not invented")
    return physical_parameter(
        requirements["board_thickness_mm"], "mm", "derived",
        "board fabrication requirements (board_thickness_mm)",
        digest=requirements_digest,
        applicability="the board's own declared fabrication "
                      "requirement; through-via barrel estimates "
                      "only")


def verify_approved_parameter(record, approved_snapshot):
    """TRUST verification of an approved-evidence claim.

    Structural validation says a record is well-formed; this says it
    is TRUE: the claimed digest must be the approved snapshot's
    normalized SHA-256, the claimed source must name a capability
    record that snapshot actually contains, and the value must equal
    that capability's value (converted to the record's units). A
    hand-built record with a plausible-looking digest refuses here,
    whatever its shape.
    """
    validate_parameter(record, "approved-evidence claim")
    if record["source_type"] != "approved-evidence":
        raise ExtractionError(
            "trust verification applies to approved-evidence "
            "records; this one claims {!r}".format(
                record["source_type"]))
    digest = approved_snapshot["normalized_sha256"]
    if record["digest"] != digest:
        raise ExtractionError(
            "the record claims catalog digest {}... but the "
            "approved snapshot is {}...; the claim is not "
            "trusted".format(record["digest"][:12], digest[:12]))
    capabilities = approved_snapshot["normalized"]["capabilities"]
    capability = capabilities.get(record["source"])
    if capability is None:
        raise ExtractionError(
            "the record claims capability {!r}, which the approved "
            "snapshot does not contain; the claim is not "
            "trusted".format(record["source"]))
    if capability.get("units") == "mil" and record["units"] == "mm":
        expected = round(capability["value"] * MIL_TO_MM, 6)
    elif capability.get("units") == record["units"]:
        expected = capability["value"]
    else:
        raise ExtractionError(
            "the record's units {!r} cannot be checked against the "
            "capability's {!r}".format(record["units"],
                                       capability.get("units")))
    if record["value"] != expected:
        raise ExtractionError(
            "the record's value {} does not equal the approved "
            "capability's {} {}; the claim is not trusted".format(
                record["value"], expected, record["units"]))
    return record


def construction_digest(copper_parameters, board_thickness_parameter,
                        resistivity_ohm_m=IACS_RESISTIVITY_OHM_M):
    """Canonical identity of one resolved physical construction.

    The SHA-256 of the exact physical parameter records a
    measurement consumed - every layer's copper record, the board
    thickness record, and the resistivity. Two boards measured under
    the same fabricator catalog but different resolved constructions
    get different digests, so an A/B binding over this digest asks
    the right question: same construction, not merely same catalog.
    """
    for layer, record in sorted(copper_parameters.items()):
        validate_parameter(record,
                           "copper thickness on {}".format(layer))
    validate_parameter(board_thickness_parameter, "board thickness")
    _finite_positive("resistivity_ohm_m", resistivity_ohm_m)
    return hashlib.sha256(json.dumps(
        {"copper_thickness_mm": copper_parameters,
         "board_thickness_mm": board_thickness_parameter,
         "resistivity_ohm_m": resistivity_ohm_m},
        sort_keys=True, separators=(",", ":")).encode(
            "utf-8")).hexdigest()


#: Stable semantic identities for the metrics this module produces.
#: The extract version is part of the identity: a metric produced
#: under different extraction semantics is a different metric, and
#: the A/B comparator refuses to pair them.
METRIC_DEFINITIONS = {
    "copper_length_mm":
        "pcbqa.extract/net-copper-length@" + EXTRACT_VERSION,
    "via_count": "pcbqa.extract/net-via-count@" + EXTRACT_VERSION,
    "segment_resistance_sum_ohm":
        "pcbqa.extract/net-segment-resistance-sum@" + EXTRACT_VERSION,
    "clock_leaf_length_spread_mm":
        "pcbqa.extract/clock-leaf-length-spread@" + EXTRACT_VERSION,
    "partial_copper_length_mm":
        "pcbqa.extract/partial-net-copper-inventory@"
        + EXTRACT_VERSION,
}


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
    from . import geom
    from .connectivity import classify_net
    connectivity = classify_net(board, net_name,
                                geom.pad_copper_polygon)
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
        "connectivity": connectivity,
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
        "extract_version": EXTRACT_VERSION,
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
                                physical_inputs,
                                two_terminal_asserted_by=None):
    """A simulation model record from one extracted net - DC only.

    The returned record conforms to the simulation registry's schema
    and covers EXACTLY the phenomenon the extraction supplies:
    interconnect_dc at evidence class geometry-derived. It cannot
    satisfy an interconnect_si or power_integrity requirement, by
    construction of the coverage contract.

    The record carries its complete DERIVATION: approved fabrication
    evidence -> physical parameters -> board geometry -> extracted
    electrical quantity -> simulation model. ``physical_inputs`` is
    the baseline's own block ({"copper_thickness_mm": {layer:
    record}, "board_thickness_mm": record}); the copper records for
    every layer the net actually uses are embedded, and a canonical
    digest of the physical inputs and resistivity is part of the
    model IDENTITY - two extractions of one board under different
    physical assumptions are different models by name, everywhere
    downstream.

    The IEC 60028 resistivity is a 20 C value, so the model declares
    ``temperature_c`` as fixed-reference 20 C; with a caller-supplied
    resistivity no reference temperature is known, nothing is
    declared, and condition coverage fails closed for any requested
    temperature. A SPICE resistor subcircuit is attached ONLY when
    the caller asserts the net is an unbranched two-terminal run -
    that assertion is recorded as the caller's, because the segment
    sum equals a two-terminal resistance only then.
    """
    if not isinstance(physical_inputs, dict) or \
            set(physical_inputs) != {"copper_thickness_mm",
                                     "board_thickness_mm"}:
        raise ExtractionError(
            "physical_inputs must be the baseline's own block with "
            "exactly copper_thickness_mm and board_thickness_mm; a "
            "model without its physical inputs would lose the "
            "evidence chain")
    validate_parameter(physical_inputs["board_thickness_mm"],
                       "board thickness")
    used_layers = sorted({segment["layer"]
                          for segment in net_record["segments"]})
    copper = {}
    for layer in used_layers:
        record = physical_inputs["copper_thickness_mm"].get(layer)
        if record is None:
            raise ExtractionError(
                "the net uses layer {!r} but the physical inputs "
                "carry no copper record for it; the derivation "
                "chain refuses to drop a layer".format(layer))
        copper[layer] = validate_parameter(
            record, "copper thickness on {}".format(layer))
    resistivity = {
        "value_ohm_m": net_record["dc"]["resistivity_ohm_m"],
        "source": net_record["dc"]["resistivity_source"],
    }
    physical_digest = construction_digest(
        copper, physical_inputs["board_thickness_mm"],
        resistivity["value_ohm_m"])
    identity = "net:{}@{}+phys:{}".format(
        net_record["net"], board_sha256[:12], physical_digest[:12])
    # The chain's head states what the inputs actually were: mixed
    # provenance stays mixed, and a caller-declared number is never
    # upgraded to fabrication evidence by association.
    root_types = sorted(
        {record["source_type"] for record in copper.values()}
        | {physical_inputs["board_thickness_mm"]["source_type"]})
    roots = {"copper_thickness_mm.{}".format(layer):
             record["source_type"]
             for layer, record in sorted(copper.items())}
    roots["board_thickness_mm"] = \
        physical_inputs["board_thickness_mm"]["source_type"]
    roots["resistivity"] = (
        "physical-constant (IEC 60028)"
        if resistivity["value_ohm_m"] == IACS_RESISTIVITY_OHM_M
        else "caller-supplied")
    derivation = {
        "chain": ["physical-parameters[{}]".format(
                      "+".join(root_types)),
                  "board-geometry",
                  "extracted-electrical-quantity",
                  "simulation-model"],
        "roots": roots,
        "board_file_sha256": board_sha256,
        "extract_version": EXTRACT_VERSION,
        "physical_inputs_sha256": physical_digest,
        "copper_thickness_mm": copper,
        "board_thickness_mm": physical_inputs["board_thickness_mm"],
        "resistivity": resistivity,
        "two_terminal_assertion": two_terminal_asserted_by,
        "assumptions": [net_record["dc"]["meaning"]],
    }
    record = {
        "identity": identity,
        "kind": "board-interconnect",
        # Every phenomenon is explicitly accounted for: the DC
        # inventory is covered, SI and PI are applicable to an
        # interconnect but unsupported by this model, and the device
        # phenomena do not arise for passive copper.
        "coverage": {"interconnect_dc": "geometry-derived",
                     "interconnect_si": "unsupported",
                     "power_integrity": "unsupported",
                     "functional_behavior": "not-applicable",
                     "device_electrical": "not-applicable",
                     "digital_io": "not-applicable"},
        "provenance": {
            "source": "pcbqa.extract segment inventory",
            "board_file_sha256": board_sha256,
            "resistivity_source":
                net_record["dc"]["resistivity_source"],
        },
        "derivation": derivation,
        "omissions": [
            "inductance", "capacitance", "distributed effects",
            "frequency dependence", "temperature dependence beyond "
            "the stated resistivity reference",
        ],
        "notes": [net_record["dc"]["meaning"]],
    }
    if resistivity["value_ohm_m"] == IACS_RESISTIVITY_OHM_M:
        record["conditions"] = {
            "temperature_c": {
                "kind": "fixed-reference",
                "value": IACS_REFERENCE_TEMPERATURE_C,
                "units": "C",
                "source": "IEC 60028 resistivity reference "
                          "temperature",
            }
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
