"""Structured downstream-to-placement feedback.

A refusal or a geometry violation discovered after placement is
only useful to an autonomous loop if it names, machine-readably,
WHAT failed, WHERE, by HOW MUCH, and WHICH design variables may
move in response. This module owns the generic record shape and
the producers that derive such records from board geometry; the
consumer owns which references are actually movable and what its
design intent permits.

A feedback record is a claim about one failure, not a repair plan:
the suggested movables are candidates the producer can justify
from the geometry, and the movement domain describes the direction
that addresses the failure - the consumer's placement machinery
decides whether and how far anything actually moves, under its own
constraints, and the next evaluation judges the result.
"""

from __future__ import annotations


class FeedbackError(Exception):
    """The feedback record cannot be accepted as declared."""


FEEDBACK_KINDS = (
    "board-edge-clearance",
    "escape-refused",
    "via-site-refused",
    "corridor-blocked",
)

_REQUIRED_KEYS = {
    "kind", "references", "pads", "nets", "location_mm",
    "required_margin_mm", "observed_margin_mm", "no_route_reason",
    "suggested_movable_references", "movement_domain",
    "source_artifact",
}


def validate_feedback(record):
    """Strict shape check; fail-closed.

    Exactly one of ``observed_margin_mm`` (a number: the geometry
    was measurable) and ``no_route_reason`` (a string: the failure
    is a refusal with no margin to report) must be non-None -
    a record with neither says nothing, and one with both is
    contradictory.
    """
    if not isinstance(record, dict) or \
            set(record) != _REQUIRED_KEYS:
        raise FeedbackError(
            "a feedback record carries exactly {}".format(
                sorted(_REQUIRED_KEYS)))
    if record["kind"] not in FEEDBACK_KINDS:
        raise FeedbackError(
            "kind {!r} is not one of {}".format(
                record["kind"], list(FEEDBACK_KINDS)))
    for key in ("references", "pads", "nets",
                "suggested_movable_references"):
        value = record[key]
        if not isinstance(value, list) or \
                not all(isinstance(item, str) and item
                        for item in value):
            raise FeedbackError(
                "{} must be a list of nonempty strings".format(key))
    location = record["location_mm"]
    if not (isinstance(location, (list, tuple))
            and len(location) == 2
            and all(isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    for v in location)):
        raise FeedbackError("location_mm must be [x_mm, y_mm]")
    required = record["required_margin_mm"]
    if isinstance(required, bool) or \
            not isinstance(required, (int, float)) or required < 0:
        raise FeedbackError(
            "required_margin_mm must be a non-negative number")
    observed = record["observed_margin_mm"]
    reason = record["no_route_reason"]
    if (observed is None) == (reason is None):
        raise FeedbackError(
            "exactly one of observed_margin_mm and "
            "no_route_reason must be present: a record with "
            "neither says nothing, one with both is contradictory")
    if observed is not None and (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))):
        raise FeedbackError("observed_margin_mm must be a number")
    if reason is not None and not (
            isinstance(reason, str) and reason):
        raise FeedbackError(
            "no_route_reason must be a nonempty string")
    domain = record["movement_domain"]
    if not isinstance(domain, dict) or \
            set(domain) != {"kind", "detail"}:
        raise FeedbackError(
            "movement_domain carries exactly kind and detail")
    if not (isinstance(domain["kind"], str) and domain["kind"]):
        raise FeedbackError("movement_domain.kind must be a "
                            "nonempty string")
    source = record["source_artifact"]
    if not isinstance(source, dict) or \
            set(source) != {"kind", "identity"}:
        raise FeedbackError(
            "source_artifact carries exactly kind and identity")
    return record


def edge_clearance_findings(board, outline, clearance_mm,
                            pad_polygon):
    """Pad-accurate board-edge findings on a circular outline.

    The placement policy's board_edge constraint judges component
    ORIGINS; a testpoint's pad can still reach the rim while its
    origin passes. This measures the actual pad copper: for every
    pad on any copper layer, the worst radial reach against the
    outline. A pad whose copper comes within ``clearance_mm`` of
    the edge produces one record per pad, naming its footprint as
    the movement candidate with a radial-inward domain - the
    consumer decides whether that reference is actually movable.
    """
    import math
    if not isinstance(outline, dict) or \
            outline.get("kind") != "circle":
        raise FeedbackError(
            "edge findings support circular outlines only; got "
            "{!r}".format(outline))
    center_x, center_y = outline["center_mm"]
    radius = outline["radius_mm"]
    findings = []
    copper = board.GetEnabledLayers().CuStack()
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            worst = None
            for layer in copper:
                if not pad.IsOnLayer(layer):
                    continue
                polygon = pad_polygon(pad, layer)
                if polygon is None or polygon.is_empty:
                    continue
                reach = max(
                    math.hypot(x - center_x, y - center_y)
                    for x, y in polygon.exterior.coords)
                if worst is None or reach > worst:
                    worst = reach
            if worst is None:
                continue
            margin = radius - worst
            if margin >= clearance_mm:
                continue
            position = pad.GetPosition()
            reference = footprint.GetReference()
            findings.append(validate_feedback({
                "kind": "board-edge-clearance",
                "references": [reference],
                "pads": ["{}.{}".format(reference,
                                        pad.GetNumber())],
                "nets": [pad.GetNetname()] if pad.GetNetname()
                else [],
                "location_mm": [position.x / 1e6,
                                position.y / 1e6],
                "required_margin_mm": clearance_mm,
                "observed_margin_mm": round(margin, 4),
                "no_route_reason": None,
                "suggested_movable_references": [reference],
                "movement_domain": {
                    "kind": "radial-inward",
                    "detail": "move at least {} mm toward the "
                              "outline center; the consumer's "
                              "constraints decide the exact "
                              "position".format(
                                  round(clearance_mm - margin,
                                        4)),
                },
                "source_artifact": {
                    "kind": "edge-clearance-analysis",
                    "identity": "pcbqa.feedback."
                                "edge_clearance_findings",
                },
            }))
    return findings


def escape_refusal_record(pad_label, net_name, location_mm,
                          reason, movable_references,
                          source_artifact,
                          required_margin_mm):
    """One escape-planner refusal as structured feedback.

    The producer knows the pad, the net, where it is, and why the
    planner refused; the CALLER supplies which references its
    design intent permits to move - the toolkit never guesses a
    consumer's movables.
    """
    return validate_feedback({
        "kind": "escape-refused",
        "references": [pad_label.split(".")[0]],
        "pads": [pad_label],
        "nets": [net_name] if net_name else [],
        "location_mm": list(location_mm),
        "required_margin_mm": required_margin_mm,
        "observed_margin_mm": None,
        "no_route_reason": reason,
        "suggested_movable_references": list(movable_references),
        "movement_domain": {
            "kind": "local-declutter",
            "detail": "open a corridor at the declared clearance "
                      "near the refused pad; movement is bounded "
                      "by the consumer's own placement "
                      "constraints",
        },
        "source_artifact": dict(source_artifact),
    })
