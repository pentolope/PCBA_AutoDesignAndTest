"""The routing-run record: what a candidate-based routing run must state.

Routing is search. A run tries candidates, may transform one after the
router produced it, and promotes exactly one into the authoritative
board. Each of those steps is an opportunity for the tree to end up
holding copper that nothing ever judged:

  * the record describes the candidate the router wrote, while the
    board on disk is a later, unrecorded derivative of it;
  * one attempt is routed on top of the previous attempt's output, so
    the run silently compounds copper instead of exploring independent
    candidates;
  * no candidate was accepted, yet the last, failing one is what stayed
    in the tree;
  * something ran over the router's copper afterwards and left no
    statement of what it changed.

None of those is detectable from the board alone: the copper looks like
copper. They are detectable from a record that states the whole chain
and is required to agree with the board it claims to describe.

This module owns only the shape and the invariants. It knows no board,
no router and no transform: which transforms are legitimate is the
consumer's engineering question, and this module asks only that each one
be named and measured. Absent a record a board simply does not opt in -
the gate reports NOT_APPLICABLE - but a record that exists must be true.
"""

from __future__ import annotations

import re

KIND = "routing-run"

_HEX = re.compile(r"^[0-9a-f]{64}$")

_RECORD_KEYS = {"kind", "source_sha256", "attempts", "accepted_attempt",
                "adopted_sha256"}
_ATTEMPT_KEYS = {"attempt", "source_sha256", "accepted", "stages"}
_STAGE_REQUIRED = {"stage", "produced_by", "sha256"}

# Consumers carry their own detail - router identity, invocation options,
# acceptance metrics - beside the contract rather than instead of it. One
# reserved key holds it, so extension never softens what must be true.
CONTEXT = "context"

ROUTER = "router"
TRANSFORM = "transform"
PRODUCERS = (ROUTER, TRANSFORM)


class RoutingRecordError(Exception):
    """The record cannot be accepted as declared. Always blocks."""


def _keys(node, required, label):
    if not isinstance(node, dict):
        raise RoutingRecordError("{} must be an object".format(label))
    missing = sorted(required - set(node))
    if missing:
        raise RoutingRecordError(
            "{} is missing {}".format(label, missing))
    unknown = sorted(set(node) - required - {CONTEXT})
    if unknown:
        raise RoutingRecordError(
            "{} carries unknown key(s) {}; consumer detail belongs under "
            "{!r}".format(label, unknown, CONTEXT))
    return node


def _digest(value, label):
    if not isinstance(value, str) or not _HEX.match(value):
        raise RoutingRecordError(
            "{} must be a lowercase sha256 digest".format(label))
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RoutingRecordError("{} must be a non-empty string".format(label))
    return value


def validate(record):
    """Validate the record's shape and internal consistency."""
    _keys(record, _RECORD_KEYS, "a routing run")
    if record["kind"] != KIND:
        raise RoutingRecordError("kind must be {!r}".format(KIND))
    _digest(record["source_sha256"], "source_sha256")

    attempts = record["attempts"]
    if not isinstance(attempts, list) or not attempts:
        raise RoutingRecordError("a routing run records at least one attempt")

    numbers = []
    for attempt in attempts:
        _validate_attempt(attempt, record["source_sha256"])
        numbers.append(attempt["attempt"])
    if len(set(numbers)) != len(numbers):
        raise RoutingRecordError("attempt numbers must be unique")

    accepted = [a for a in attempts if a["accepted"]]
    if len(accepted) > 1:
        raise RoutingRecordError("at most one attempt may be accepted")

    chosen = record["accepted_attempt"]
    adopted = record["adopted_sha256"]
    if not accepted:
        if chosen is not None:
            raise RoutingRecordError(
                "accepted_attempt names an attempt no entry marks accepted")
        if adopted is not None:
            raise RoutingRecordError(
                "no attempt was accepted, so nothing may be recorded as "
                "adopted")
        return record

    if chosen != accepted[0]["attempt"]:
        raise RoutingRecordError(
            "accepted_attempt must name the attempt marked accepted")
    _digest(adopted, "adopted_sha256")
    final = accepted[0]["stages"][-1]["sha256"]
    if adopted != final:
        raise RoutingRecordError(
            "adopted_sha256 must be the last recorded stage of the accepted "
            "attempt; a promotion that is not the end of the chain is an "
            "unrecorded transform")
    return record


def _validate_attempt(attempt, run_source):
    _keys(attempt, _ATTEMPT_KEYS, "an attempt")
    if not isinstance(attempt["attempt"], int) or attempt["attempt"] < 1:
        raise RoutingRecordError("attempt must be a positive integer")
    if not isinstance(attempt["accepted"], bool):
        raise RoutingRecordError("accepted must be a boolean")
    _digest(attempt["source_sha256"], "attempt.source_sha256")
    if attempt["source_sha256"] != run_source:
        raise RoutingRecordError(
            "attempt {} started from a different board than the run's "
            "declared source: candidates must be independent, not routed on "
            "top of one another".format(attempt["attempt"]))

    stages = attempt["stages"]
    if not isinstance(stages, list) or not stages:
        raise RoutingRecordError("an attempt records at least one stage")
    producers = [_validate_stage(stage) for stage in stages]
    if producers.count(ROUTER) != 1:
        raise RoutingRecordError(
            "an attempt records exactly one router stage")
    if producers[0] != ROUTER:
        raise RoutingRecordError(
            "the router stage comes first; everything after it is a "
            "transform that must declare itself")


def _validate_stage(stage):
    if not isinstance(stage, dict) or not _STAGE_REQUIRED <= set(stage):
        raise RoutingRecordError(
            "a stage carries at least {}".format(sorted(_STAGE_REQUIRED)))
    _text(stage["stage"], "stage.stage")
    _digest(stage["sha256"], "stage.sha256")
    produced_by = stage["produced_by"]
    if produced_by not in PRODUCERS:
        raise RoutingRecordError(
            "stage.produced_by is one of {}".format(list(PRODUCERS)))
    if produced_by == TRANSFORM:
        _text(stage.get("transform"), "stage.transform")
        effects = stage.get("effects")
        if not isinstance(effects, dict) or not effects:
            raise RoutingRecordError(
                "a transform states what it measurably changed; an empty "
                "effects block is an unmeasured edit")
        for key, value in effects.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise RoutingRecordError(
                    "effects.{} must be a number".format(key))
    return produced_by


def adopted_digest(record):
    """The digest the record claims is in the tree, or None."""
    return validate(record)["adopted_sha256"]


def compare_to_board(record, board_sha256):
    """Differences between the record's claim and the board on disk."""
    validate(record)
    adopted = record["adopted_sha256"]
    problems = []
    if adopted is None:
        problems.append({
            "issue": "no candidate was accepted, so the board in the tree is "
                     "not a recorded routing result",
            "board_sha256": board_sha256})
    elif adopted != board_sha256:
        problems.append({
            "issue": "the adopted board is not the candidate the record "
                     "describes",
            "recorded_sha256": adopted,
            "board_sha256": board_sha256})
    return problems


def transforms(record):
    """Every post-router transform the accepted attempt declares."""
    validate(record)
    if record["accepted_attempt"] is None:
        return []
    accepted = [a for a in record["attempts"] if a["accepted"]][0]
    return [stage for stage in accepted["stages"]
            if stage["produced_by"] == TRANSFORM]
