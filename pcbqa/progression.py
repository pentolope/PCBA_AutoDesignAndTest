"""Candidate progression: ordered correctness classes, fail-closed.

A candidate advances through CLASSES in order, and no later quantity
may override an earlier one: a board that is not fully connected is
not "better" for having less copper, and a weighted scalar never
outranks a correctness class. Search consumers rank by
``rank_key`` - a lexicographic tuple built strictly from these
classes - and may apply their own tie-breaks only among candidates
whose rank keys are equal.

Distinctions this module keeps separate, deliberately:

  * NETLIST PARITY leads the order: whether the candidate
    implements the authoritative product intent - every required
    footprint, pad and net assignment, no unexpected nets - is
    judged before anything else, because a candidate that differs
    from the intent is a different product, and no downstream
    completion may launder that;
  * ``accept_for_comparison`` (worth measuring experimentally)
    versus ``search_winner_eligible`` (permitted to be presented
    as best): a candidate whose critical paths or topology FAIL -
    or are unresolved - may still be measured, but a search winner
    must never outrank an unresolved correctness class;

  * benchmark-set completion (the A/B measurement inventory) versus
    BOARD-REQUIRED completion (every required net on the board): a
    candidate must never read as fully connected while required
    board nets remain incomplete, so ``fully_connected`` is defined
    over the board-required set alone;
  * critical NETS connected (a connectivity fact) versus critical
    PATHS resolved and critical TOPOLOGY valid (policy-owned truths
    from the electrical-path and topology gates): net connectivity
    is necessary but never sufficient, and an unknown gate truth is
    unknown - not credited;
  * ``candidate_ready_for_next_stage`` (every correctness class up
    to the quality gates passes) versus ``accept_for_comparison``
    (worth measuring experimentally): a candidate may be worth
    comparing while still being invalid as a board.
"""

from __future__ import annotations


class ProgressionError(Exception):
    """The assessment input cannot be accepted as declared."""


#: The ordered correctness classes. ``optimization`` is recorded but
#: never judged and never enters the rank key.
CLASSES = (
    "netlist-parity",
    "placement-policy",
    "critical-structures",
    "board-connectivity",
    "fabrication-geometry",
    "blocking-gates",
    "quality-gates",
    "electrical-evidence",
    "optimization",
)

_REQUIRED_KEYS = {
    "netlist_parity", "placement_policy_ok", "critical",
    "board_required_connectivity", "benchmark_connectivity",
    "fabrication_geometry", "blocking_gates", "quality_gates",
    "electrical_evidence", "optimization",
}

_CRITICAL_KEYS = {"nets_connected", "paths_resolved",
                  "topology_valid"}


def _tristate(label, value):
    if value is True or value is False or value == "unknown":
        return value
    raise ProgressionError(
        "{} must be True, False or 'unknown', not {!r}".format(
            label, value))


def _counts(label, value):
    if not isinstance(value, dict) or \
            set(value) != {"complete", "total"}:
        raise ProgressionError(
            "{} must carry exactly complete and total".format(label))
    for key in ("complete", "total"):
        if isinstance(value[key], bool) or \
                not isinstance(value[key], int) or value[key] < 0:
            raise ProgressionError(
                "{}.{} must be a non-negative integer".format(
                    label, key))
    if value["complete"] > value["total"] or value["total"] == 0:
        raise ProgressionError(
            "{}: complete {} of total {} is not a valid "
            "count".format(label, value["complete"],
                            value["total"]))
    return value


def _gates(label, value):
    if not isinstance(value, dict) or \
            set(value) != {"evaluated", "failing"}:
        raise ProgressionError(
            "{} must carry exactly evaluated and failing".format(
                label))
    if not isinstance(value["evaluated"], bool):
        raise ProgressionError(
            "{}.evaluated must be a bool".format(label))
    if not isinstance(value["failing"], list) or \
            not all(isinstance(item, str) and item
                    for item in value["failing"]):
        raise ProgressionError(
            "{}.failing must be a list of gate names".format(label))
    return value


def assess(record):
    """Judge one candidate's progression. Strict input, fail-closed.

    Unknown truths stop progression at their class: a gate that was
    never evaluated cannot pass, and net connectivity never stands
    in for an unevaluated path or topology truth.
    """
    if not isinstance(record, dict):
        raise ProgressionError("the assessment input must be a dict")
    unknown_keys = sorted(set(record) - _REQUIRED_KEYS)
    missing = sorted(_REQUIRED_KEYS - set(record))
    if unknown_keys or missing:
        raise ProgressionError(
            "assessment input must carry exactly {} (unknown {}, "
            "missing {})".format(sorted(_REQUIRED_KEYS),
                                 unknown_keys, missing))
    parity = record["netlist_parity"]
    if not isinstance(parity, dict) or (
            set(parity) != {"ok", "detail"}):
        raise ProgressionError(
            "netlist_parity must carry exactly ok and detail")
    parity_ok = _tristate("netlist_parity.ok", parity["ok"])
    if not isinstance(record["placement_policy_ok"], bool):
        raise ProgressionError("placement_policy_ok must be a bool")
    critical = record["critical"]
    if not isinstance(critical, dict) or \
            set(critical) != _CRITICAL_KEYS:
        raise ProgressionError(
            "critical must carry exactly {}".format(
                sorted(_CRITICAL_KEYS)))
    if not isinstance(critical["nets_connected"], bool):
        raise ProgressionError(
            "critical.nets_connected must be a bool; it is a "
            "connectivity fact, never unknown once classified")
    paths = _tristate("critical.paths_resolved",
                      critical["paths_resolved"])
    topology = _tristate("critical.topology_valid",
                         critical["topology_valid"])
    board = _counts("board_required_connectivity",
                    record["board_required_connectivity"])
    benchmark = _counts("benchmark_connectivity",
                        record["benchmark_connectivity"])
    fabrication = record["fabrication_geometry"]
    if not isinstance(fabrication, dict) or \
            set(fabrication) != {"ok", "detail"}:
        raise ProgressionError(
            "fabrication_geometry must carry exactly ok and detail")
    fabrication_ok = _tristate("fabrication_geometry.ok",
                               fabrication["ok"])
    blocking = _gates("blocking_gates", record["blocking_gates"])
    quality = _gates("quality_gates", record["quality_gates"])
    evidence = record["electrical_evidence"]
    if not isinstance(evidence, dict) or \
            set(evidence) != {"usable_results"}:
        raise ProgressionError(
            "electrical_evidence must carry exactly usable_results")
    usable = evidence["usable_results"]
    if isinstance(usable, bool) or not isinstance(usable, int) \
            or usable < 0:
        raise ProgressionError(
            "usable_results must be a non-negative integer")
    if not isinstance(record["optimization"], dict):
        raise ProgressionError(
            "optimization must be a dict of recorded metrics")

    classes = {}
    classes["netlist-parity"] = {
        "status": {True: "pass", False: "fail",
                   "unknown": "unknown"}[parity_ok],
        "detail": parity["detail"],
    }
    classes["placement-policy"] = {
        "status": "pass" if record["placement_policy_ok"]
        else "fail",
        "detail": "toolkit placement-policy evaluation on actual "
                  "positions",
    }
    critical_states = {
        "nets_connected": critical["nets_connected"],
        "paths_resolved": paths,
        "topology_valid": topology,
    }
    if critical["nets_connected"] and paths is True \
            and topology is True:
        critical_status = "pass"
    elif critical["nets_connected"] is False or paths is False \
            or topology is False:
        critical_status = "fail"
    else:
        critical_status = "unknown"
    classes["critical-structures"] = {
        "status": critical_status,
        "states": critical_states,
        "detail": "net connectivity is necessary but never "
                  "sufficient: paths and topology are policy-owned "
                  "gate truths, and unknown is not credited",
    }
    fully_connected = board["complete"] == board["total"]
    classes["board-connectivity"] = {
        "status": "pass" if fully_connected else "fail",
        "board_required": dict(board),
        "benchmark": dict(benchmark),
        "detail": "judged over the board-required net set; the "
                  "benchmark set is reported beside it and never "
                  "stands in for it",
    }
    classes["fabrication-geometry"] = {
        "status": {True: "pass", False: "fail",
                   "unknown": "unknown"}[fabrication_ok],
        "detail": fabrication["detail"],
    }
    for name, gates in (("blocking-gates", blocking),
                        ("quality-gates", quality)):
        if not gates["evaluated"]:
            status = "unknown"
        elif gates["failing"]:
            status = "fail"
        else:
            status = "pass"
        classes[name] = {"status": status,
                         "failing": sorted(gates["failing"])}
    classes["electrical-evidence"] = {
        "status": "pass" if usable > 0 else "fail",
        "usable_results": usable,
        "detail": "results usable for a design decision under the "
                  "simulation result policy",
    }
    classes["optimization"] = {
        "status": "recorded",
        "metrics": dict(record["optimization"]),
        "detail": "never judged here and never in the rank key",
    }

    progress_class = "optimization"
    for name in CLASSES[:-1]:
        if classes[name]["status"] != "pass":
            progress_class = name
            break

    judged = CLASSES[:-2]  # everything up to and incl. quality-gates
    ready = all(classes[name]["status"] == "pass"
                for name in judged)
    rank_key = (
        parity_ok is True,
        record["placement_policy_ok"],
        critical["nets_connected"],
        paths is True,
        topology is True,
        board["complete"] / float(board["total"]),
        fabrication_ok is True,
        (-len(blocking["failing"]) if blocking["evaluated"]
         else float("-inf")),
        (-len(quality["failing"]) if quality["evaluated"]
         else float("-inf")),
        usable,
    )
    comparison = (parity_ok is True
                  and record["placement_policy_ok"]
                  and critical["nets_connected"])
    return {
        "classes": classes,
        "progress_class": progress_class,
        "fully_connected": fully_connected,
        "candidate_ready_for_next_stage": ready,
        "accept_for_comparison": comparison,
        "search_winner_eligible": comparison
        and critical_status == "pass",
        "rank_key": rank_key,
        "meaning": "rank_key is lexicographic over the correctness "
                   "classes in order; optimization metrics are "
                   "recorded but never ranked, and any consumer "
                   "tie-break applies only between equal rank keys; "
                   "a measured candidate whose critical truths are "
                   "failed or unresolved is never winner-eligible",
    }
