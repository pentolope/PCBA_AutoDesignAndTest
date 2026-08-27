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
                        and value == value for value in position)):
            raise PlacementError(
                "fixed position_mm must be [x, y] in millimetres")
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
    """A whole constraint set: each member valid, fixed refs unique."""
    if not isinstance(constraints, list):
        raise PlacementError("a constraint set must be a list")
    fixed = set()
    for constraint in constraints:
        validate_constraint(constraint)
        if constraint["kind"] == "fixed":
            reference = constraint["reference"]
            if reference in fixed:
                raise PlacementError(
                    "component {!r} is fixed twice; two pins for one "
                    "part is a contradiction, not an "
                    "average".format(reference))
            fixed.add(reference)
    return constraints
