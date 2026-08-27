"""Generic placement-constraint representation for optimizer-driven layout.

The intended workflow separates concerns the way the toolkit always
has: an AI states SEMANTIC intent (this block decouples that device;
these parts form one oscillator; this region is noisy), a numerical
optimizer turns intent into coordinates, trial routing scores the
result, and the loop perturbs and retries. This module owns only the
middle contract: a strict, machine-readable constraint vocabulary that
an optimizer can consume and a reviewer can audit. It performs no
placement itself and knows no specific board.

Constraint kinds, deliberately few and each with a stated shape:

  * fixed            - a component is mechanically pinned (position,
                       optional rotation) and may not move;
  * board_edge       - a component must sit within a stated distance
                       of the outline (connectors, mounting hardware);
  * functional_block - a named group that should place compactly, with
                       an optional maximum spread;
  * proximity        - one component must sit within a stated distance
                       of another (decoupling next to its pin, series
                       termination next to its driver);
  * ordering         - components must appear in a stated order along
                       a path (ESD before filter before device);
  * separation       - two named groups must keep a stated minimum
                       distance (sensitive vs noisy regions);
  * orientation      - a component's allowed rotations;
  * swap_group       - interchangeable equivalents an optimizer may
                       permute (equivalent gates, resistor positions).

Trial-route quality is the intended primary routability signal; net
counts and ratline lengths are inputs, not verdicts. Score plumbing
lives with the optimizer integration, not here.

The validator's boundary, stated explicitly: it rejects OBVIOUS
contradictions deterministically - a fixed rotation outside the same
part's allowed orientations, self-proximity and self-separation,
overlapping swap groups, duplicate fixes, non-finite numbers. It does
NOT attempt constraint satisfiability: geometric infeasibility that
needs the outline, courtyards or an ordering axis is the OPTIMIZER'S
to discover and report separately. A set this validator accepts may
still be infeasible; a set it rejects is wrong on its face.
"""

from __future__ import annotations


class PlacementError(Exception):
    """The constraint set cannot be accepted as declared. Always blocks."""


_KINDS = ("fixed", "board_edge", "functional_block", "proximity",
          "ordering", "separation", "orientation", "swap_group")

_REQUIRED = {
    "fixed": {"reference", "position_mm"},
    "board_edge": {"reference", "max_distance_mm"},
    "functional_block": {"name", "members"},
    "proximity": {"reference", "anchor", "max_distance_mm"},
    "ordering": {"path", "references"},
    "separation": {"group_a", "group_b", "min_distance_mm"},
    "orientation": {"reference", "allowed_rotations_deg"},
    "swap_group": {"name", "references"},
}
_OPTIONAL = {
    "fixed": {"rotation_deg"},
    "board_edge": set(),
    "functional_block": {"max_spread_mm"},
    "proximity": {"pin"},
    "ordering": set(),
    "separation": set(),
    "orientation": set(),
    "swap_group": set(),
}


def _finite_positive(label, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or value != value or value <= 0 \
            or value in (float("inf"), float("-inf")):
        raise PlacementError(
            "{} is {!r}, not a usable positive number".format(
                label, value))


def _reference_list(label, value):
    if not isinstance(value, list) or len(value) < 2 or \
            not all(isinstance(item, str) and item for item in value):
        raise PlacementError(
            "{} must list at least two component references".format(
                label))
    if len(set(value)) != len(value):
        raise PlacementError(
            "{} repeats a reference; each member appears once".format(
                label))


def validate_constraint(constraint):
    """One constraint, strictly: known kind, exact keys, sane values."""
    if not isinstance(constraint, dict):
        raise PlacementError("a constraint must be a dict")
    kind = constraint.get("kind")
    if kind not in _KINDS:
        raise PlacementError(
            "constraint kind {!r} is not one of {}".format(
                kind, list(_KINDS)))
    keys = set(constraint) - {"kind", "reason"}
    required = _REQUIRED[kind]
    unknown = sorted(keys - required - _OPTIONAL[kind])
    if unknown:
        raise PlacementError(
            "{} constraint carries unknown key(s) {}".format(
                kind, unknown))
    missing = sorted(required - keys)
    if missing:
        raise PlacementError(
            "{} constraint is missing key(s) {}".format(kind, missing))
    if kind == "fixed":
        position = constraint["position_mm"]
        if not (isinstance(position, list) and len(position) == 2
                and all(isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and value == value
                        and value not in (float("inf"),
                                          float("-inf"))
                        for value in position)):
            raise PlacementError(
                "fixed position_mm must be two finite millimetre "
                "coordinates")
        rotation = constraint.get("rotation_deg")
        if rotation is not None and (
                isinstance(rotation, bool)
                or not isinstance(rotation, (int, float))
                or rotation != rotation
                or rotation in (float("inf"), float("-inf"))):
            raise PlacementError(
                "fixed rotation_deg must be a finite number")
    if kind == "proximity" and \
            constraint["reference"] == constraint["anchor"]:
        raise PlacementError(
            "component {!r} cannot be proximity-constrained to "
            "itself".format(constraint["reference"]))
    if kind in ("board_edge", "proximity"):
        _finite_positive("max_distance_mm",
                         constraint["max_distance_mm"])
    if kind == "separation":
        _finite_positive("min_distance_mm",
                         constraint["min_distance_mm"])
    if kind == "functional_block":
        _reference_list("functional_block members",
                        constraint["members"])
        if "max_spread_mm" in constraint:
            _finite_positive("max_spread_mm",
                             constraint["max_spread_mm"])
    if kind in ("ordering", "swap_group"):
        _reference_list("{} references".format(kind),
                        constraint["references"])
    if kind == "separation":
        _reference_list("group_a", constraint["group_a"])
        _reference_list("group_b", constraint["group_b"])
        shared = sorted(set(constraint["group_a"])
                        & set(constraint["group_b"]))
        if shared:
            raise PlacementError(
                "component(s) {} appear on both sides of a "
                "separation; a part cannot be kept away from "
                "itself".format(shared))
    if kind == "orientation":
        rotations = constraint["allowed_rotations_deg"]
        if not (isinstance(rotations, list) and rotations
                and all(isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        for value in rotations)):
            raise PlacementError(
                "allowed_rotations_deg must be a nonempty numeric "
                "list")
    return constraint


def validate_constraint_set(constraints):
    """A whole constraint set: obvious cross-contradictions refuse.

    Deterministic pairwise checks only - see the module docstring for
    the boundary between this validator and the optimizer's
    infeasibility reporting.
    """
    if not isinstance(constraints, list):
        raise PlacementError("a constraint set must be a list")
    fixed_rotation = {}
    fixed = set()
    orientations = {}
    swap_membership = {}
    for constraint in constraints:
        validate_constraint(constraint)
        kind = constraint["kind"]
        if kind == "fixed":
            reference = constraint["reference"]
            if reference in fixed:
                raise PlacementError(
                    "component {!r} is fixed twice; two pins for one "
                    "part is a contradiction, not an "
                    "average".format(reference))
            fixed.add(reference)
            if "rotation_deg" in constraint:
                fixed_rotation[reference] = \
                    constraint["rotation_deg"]
        if kind == "orientation":
            orientations[constraint["reference"]] = \
                constraint["allowed_rotations_deg"]
        if kind == "swap_group":
            for reference in constraint["references"]:
                if reference in swap_membership:
                    raise PlacementError(
                        "component {!r} belongs to swap groups {!r} "
                        "and {!r}; overlapping swap groups make the "
                        "permutation ill-defined".format(
                            reference, swap_membership[reference],
                            constraint["name"]))
                swap_membership[reference] = constraint["name"]
    for reference, rotation in sorted(fixed_rotation.items()):
        allowed = orientations.get(reference)
        if allowed is not None and \
                float(rotation) % 360.0 not in \
                {float(value) % 360.0 for value in allowed}:
            raise PlacementError(
                "component {!r} is fixed at {} degrees but its "
                "orientation constraint allows only {}".format(
                    reference, rotation, allowed))
    return constraints


_POSITION_KEYS = {"x_mm", "y_mm", "rotation_deg"}


def _position(positions, reference, context):
    record = positions.get(reference)
    if record is None:
        raise PlacementError(
            "{} names component {!r}, which the position map does "
            "not carry; evaluation refuses rather than skipping a "
            "constrained part".format(context, reference))
    if not isinstance(record, dict) or \
            set(record) != _POSITION_KEYS:
        raise PlacementError(
            "position of {!r} must carry exactly {}".format(
                reference, sorted(_POSITION_KEYS)))
    for key in _POSITION_KEYS:
        value = record[key]
        if isinstance(value, bool) or \
                not isinstance(value, (int, float)) or \
                value != value or \
                value in (float("inf"), float("-inf")):
            raise PlacementError(
                "position of {!r} has non-finite {}".format(
                    reference, key))
    return record


def _distance(one, other):
    return ((one["x_mm"] - other["x_mm"]) ** 2
            + (one["y_mm"] - other["y_mm"]) ** 2) ** 0.5


def evaluate_placement(positions, constraints, outline=None,
                       fixed_tolerance_mm=0.001):
    """Judge actual component positions against a constraint set.

    This is the toolkit's half of the placement loop: an optimizer
    may have produced the coordinates however it liked, but whether
    the SEMANTIC constraints hold is decided here, deterministically,
    from the candidate's own positions - never from the optimizer's
    claims. ``positions`` maps reference -> {x_mm, y_mm,
    rotation_deg}; a constrained reference missing from the map
    refuses the whole evaluation.

    Statuses per constraint:
      * ``satisfied`` / ``violated`` - the constraint has a
        threshold and the measurement answers it;
      * ``unthresholded`` - measured (functional_block without
        max_spread_mm reports its spread) but nothing to pass or
        fail;
      * ``not_applicable`` - nothing to evaluate on final positions
        (swap_group: permutation freedom, not a geometric predicate);
      * ``not_evaluable`` - the needed context is absent (board_edge
        without an outline; a degenerate ordering axis). Fail-closed
        callers treat these as blocking, and the summary's ``ok`` is
        true only with zero violated AND zero not_evaluable.

    ``outline`` currently supports {"kind": "circle", "center_mm":
    [x, y], "radius_mm": r}; an unknown outline kind refuses.
    """
    validate_constraint_set(constraints)
    if outline is not None:
        if not isinstance(outline, dict) or \
                outline.get("kind") != "circle":
            raise PlacementError(
                "outline kind {!r} is not supported; supported "
                "outlines: circle".format(
                    outline.get("kind") if isinstance(outline, dict)
                    else outline))
        center = {"x_mm": outline["center_mm"][0],
                  "y_mm": outline["center_mm"][1],
                  "rotation_deg": 0.0}
        radius = outline["radius_mm"]
    results = []
    for index, constraint in enumerate(constraints):
        kind = constraint["kind"]
        entry = {"index": index, "kind": kind,
                 "constraint": constraint}
        context = "{} constraint #{}".format(kind, index)
        if kind == "fixed":
            record = _position(positions, constraint["reference"],
                               context)
            offset = _distance(record, {
                "x_mm": constraint["position_mm"][0],
                "y_mm": constraint["position_mm"][1],
                "rotation_deg": 0.0})
            rotation_ok = True
            if "rotation_deg" in constraint:
                rotation_ok = (record["rotation_deg"] % 360.0
                               == constraint["rotation_deg"] % 360.0)
            entry["measured"] = {"offset_mm": round(offset, 6),
                                 "rotation_matches": rotation_ok}
            entry["status"] = "satisfied" \
                if offset <= fixed_tolerance_mm and rotation_ok \
                else "violated"
        elif kind == "proximity":
            reference = _position(positions,
                                  constraint["reference"], context)
            anchor = _position(positions, constraint["anchor"],
                               context)
            distance = _distance(reference, anchor)
            entry["measured"] = {"distance_mm": round(distance, 6)}
            entry["status"] = "satisfied" \
                if distance <= constraint["max_distance_mm"] \
                else "violated"
        elif kind == "functional_block":
            members = [_position(positions, member, context)
                       for member in constraint["members"]]
            spread = max(
                _distance(one, other)
                for i, one in enumerate(members)
                for other in members[i + 1:])
            entry["measured"] = {"max_pairwise_mm": round(spread, 6)}
            if "max_spread_mm" in constraint:
                entry["status"] = "satisfied" \
                    if spread <= constraint["max_spread_mm"] \
                    else "violated"
            else:
                entry["status"] = "unthresholded"
        elif kind == "separation":
            group_a = [_position(positions, member, context)
                       for member in constraint["group_a"]]
            group_b = [_position(positions, member, context)
                       for member in constraint["group_b"]]
            closest = min(_distance(one, other)
                          for one in group_a for other in group_b)
            entry["measured"] = {"min_distance_mm": round(closest, 6)}
            entry["status"] = "satisfied" \
                if closest >= constraint["min_distance_mm"] \
                else "violated"
        elif kind == "orientation":
            record = _position(positions, constraint["reference"],
                               context)
            allowed = {float(value) % 360.0 for value in
                       constraint["allowed_rotations_deg"]}
            entry["measured"] = {
                "rotation_deg": record["rotation_deg"] % 360.0}
            entry["status"] = "satisfied" \
                if record["rotation_deg"] % 360.0 in allowed \
                else "violated"
        elif kind == "board_edge":
            record = _position(positions, constraint["reference"],
                               context)
            if outline is None:
                entry["status"] = "not_evaluable"
                entry["measured"] = {
                    "reason": "no board outline was supplied; edge "
                              "distance cannot be measured"}
            else:
                edge = radius - _distance(record, center)
                entry["measured"] = {
                    "edge_distance_mm": round(edge, 6)}
                entry["status"] = "satisfied" \
                    if 0.0 <= edge <= constraint["max_distance_mm"] \
                    else "violated"
        elif kind == "ordering":
            records = [_position(positions, reference, context)
                       for reference in constraint["references"]]
            first, last = records[0], records[-1]
            axis = _distance(first, last)
            if axis == 0.0:
                entry["status"] = "not_evaluable"
                entry["measured"] = {
                    "reason": "the first and last components "
                              "coincide; the ordering axis is "
                              "degenerate"}
            else:
                unit = ((last["x_mm"] - first["x_mm"]) / axis,
                        (last["y_mm"] - first["y_mm"]) / axis)
                projections = [
                    round((record["x_mm"] - first["x_mm"]) * unit[0]
                          + (record["y_mm"] - first["y_mm"])
                          * unit[1], 6)
                    for record in records]
                monotonic = all(
                    earlier < later for earlier, later in
                    zip(projections, projections[1:]))
                entry["measured"] = {
                    "projections_mm": projections}
                entry["status"] = "satisfied" if monotonic \
                    else "violated"
        else:  # swap_group
            entry["status"] = "not_applicable"
            entry["measured"] = {
                "reason": "swap groups grant permutation freedom; "
                          "they impose no geometric predicate on "
                          "final positions"}
        results.append(entry)
    violated = [entry["index"] for entry in results
                if entry["status"] == "violated"]
    not_evaluable = [entry["index"] for entry in results
                     if entry["status"] == "not_evaluable"]
    summary = {
        "satisfied": sum(1 for entry in results
                         if entry["status"] == "satisfied"),
        "violated": violated,
        "unthresholded": [entry["index"] for entry in results
                          if entry["status"] == "unthresholded"],
        "not_applicable": sum(1 for entry in results
                              if entry["status"] == "not_applicable"),
        "not_evaluable": not_evaluable,
        "ok": not violated and not not_evaluable,
    }
    return {"results": results, "summary": summary}


def overlapping_pairs(boxes):
    """Strictly overlapping axis-aligned boxes among {ref: [minx,
    miny, maxx, maxy]} (millimetres). Touching edges do not overlap.
    The caller decides which geometry to box (courtyards where they
    exist); this function only answers the collision question - a
    scatter is called non-overlapping ONLY when this says so."""
    for reference, box in boxes.items():
        if not (isinstance(box, (list, tuple)) and len(box) == 4
                and all(isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and value == value
                        and value not in (float("inf"),
                                          float("-inf"))
                        for value in box)
                and box[0] < box[2] and box[1] < box[3]):
            raise PlacementError(
                "box of {!r} must be finite [minx, miny, maxx, "
                "maxy] with positive extent".format(reference))
    pairs = []
    names = sorted(boxes)
    for i, one in enumerate(names):
        a = boxes[one]
        for other in names[i + 1:]:
            b = boxes[other]
            if a[0] < b[2] and b[0] < a[2] \
                    and a[1] < b[3] and b[1] < a[3]:
                pairs.append((one, other))
    return pairs
