"""Typed gate policy, checked as toolkit development behavior.

Runtime types ensure an applied limit has units and manifest provenance. A
focused source audit checks that gate policy comparisons use the constraint API
instead of bypassing it with raw numeric literals.
"""

from __future__ import annotations

import ast
import glob
import operator
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                           # noqa: E402
from pcbqa import core                                            # noqa: E402
from pcbqa.constraints import (Constraint, ConstraintError,        # noqa: E402
                               implementation_constant)
from pcbqa.core import Context, GateResult, Manifest               # noqa: E402
from pcbqa.gates import (g_provenance, g_checks, g_geometry,       # noqa: E402,F401
                         g_contracts, g_assembly, g_export_parity,
                         g_fabrication, g_orientation, g_timing)


class TheConstraintApiIsTyped(unittest.TestCase):
    """Applied limits and policy comparisons retain type and provenance."""

    def test_a_raw_value_is_not_a_limit(self):
        res = GateResult("T.EST", "t")
        for raw in (0.5, "0.5", None, {"value": 0.5}):
            with self.assertRaises(TypeError):
                res.limit(raw)

    def test_a_constraint_cannot_exist_without_units(self):
        for units in (None, ""):
            with self.assertRaises(ConstraintError):
                Constraint("t", "a.b", 1.0, units, "m.json", "0" * 64)

    def test_a_constraint_cannot_cite_a_key_it_does_not_come_from(self):
        """`constraint()` reads the value from the key, so they cannot differ."""
        manifest = Manifest(paths.REVA_MANIFEST)
        constraint = manifest.constraint("routing.min_segment_mm", units="mm")
        self.assertEqual(constraint.value,
                         manifest.get("routing.min_segment_mm"))
        with self.assertRaises(core.ManifestError):
            manifest.constraint("routing.no_such_key", units="mm")

    def test_policy_comparisons_live_on_the_constraint(self):
        constraint = Constraint(
            "clearance", "routing.clearance_mm", 0.15, "mm",
            "board.json", "0" * 64)
        self.assertTrue(constraint.violated_minimum(0.14))
        self.assertFalse(constraint.violated_minimum(0.15))
        self.assertTrue(constraint.violated_maximum(0.16))
        self.assertTrue(constraint.within(1.14, 1.0))
        self.assertTrue(constraint.differs_by_more_than(1.16, 1.0))

    def test_range_constraints_keep_their_boundary_semantics(self):
        constraint = Constraint(
            "angle", "orientation.range", [0.0, 360.0], "degrees",
            "board.json", "0" * 64)
        self.assertTrue(constraint.contains(0.0))
        self.assertTrue(constraint.contains(359.999))
        self.assertFalse(constraint.contains(360.0))

    def test_implementation_constants_need_numeric_value_and_rationale(self):
        self.assertEqual(implementation_constant(2, "two endpoints"), 2)
        for value, rationale in ((True, "not numeric"), (2, "")):
            with self.assertRaises(ConstraintError):
                implementation_constant(value, rationale)


class EveryLimitEveryGateAppliesIsTraceable(unittest.TestCase):
    """Run the real gates and audit what they recorded."""

    def _limits(self, manifest_path):
        manifest = Manifest(manifest_path)
        workdir = tempfile.mkdtemp(prefix="pcbqa_limits_")
        ctx = Context(manifest, workdir)
        applied = {}
        for result in core.run_all(ctx):
            for name, record in result.limits.items():
                applied["{}.{}".format(result.gate_id, name)] = record
        return manifest, applied

    def _audit(self, manifest_path):
        manifest, applied = self._limits(manifest_path)
        self.assertTrue(applied, "no gate applied any limit at all")
        problems = []
        for name, record in sorted(applied.items()):
            key = record.get("manifest_key")
            if not key or not record.get("provenance"):
                problems.append((name, "carries no provenance"))
                continue
            if record.get("units") is None:
                problems.append((name, "declares no units"))
            if not manifest.has(key):
                problems.append((name, "cites a key that does not exist: "
                                       + key))
                continue
            if _leaf(record["value"]) != _leaf(manifest.get(key)):
                problems.append((name, "applied a value that is not the "
                                       "manifest value at " + key))
        self.assertEqual(problems, [], problems)
        return len(applied)

    def test_the_negative_fixture(self):
        self.assertGreater(self._audit(paths.REVA_MANIFEST), 20)

    def test_a_structurally_different_board(self):
        self.assertGreater(self._audit(paths.PORTABILITY_MANIFEST), 0)


def _leaf(value):
    return round(value, 9) if isinstance(value, float) else value


def _target_names(target):
    if isinstance(target, ast.Name):
        return {target.id}
    names = set()
    for child in ast.iter_child_nodes(target):
        names.update(_target_names(child))
    return names


def _number(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
            and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _number(node.operand)
        return -value if value is not None else None
    if isinstance(node, ast.BinOp):
        operations = {
            ast.Add: operator.add, ast.Sub: operator.sub,
            ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
            ast.Pow: operator.pow,
        }
        operation = operations.get(type(node.op))
        left, right = _number(node.left), _number(node.right)
        if operation is not None and left is not None and right is not None:
            try:
                value = operation(left, right)
            except (ArithmeticError, OverflowError):
                return None
            return value if isinstance(value, (int, float)) else None
    return None


def _call_name(node):
    if not isinstance(node, ast.Call):
        return None
    function = node.func
    return function.id if isinstance(function, ast.Name) else \
        function.attr if isinstance(function, ast.Attribute) else None


class _PolicyComparisonAudit(ast.NodeVisitor):
    """Ordered comparisons in gate code, deliberately not a Python linter."""

    def __init__(self, source):
        self.source = source
        self.numeric_names = {}
        self.implementation_names = set()
        self.policy_names = set()
        self.issues = []

    def collect(self, tree):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) \
                else [node.target]
            names = set().union(*(_target_names(target) for target in targets))
            number = _number(value)
            if number is not None:
                for name in names:
                    self.numeric_names[name] = number
            if _call_name(value) == "implementation_constant":
                self.implementation_names.update(names)
            if any(isinstance(part, ast.Attribute) and part.attr == "value"
                   for part in ast.walk(value)):
                self.policy_names.update(names)
            for call in (part for part in ast.walk(value)
                         if isinstance(part, ast.Call)):
                if isinstance(call.func, ast.Attribute) and \
                        call.func.attr == "get" and any(
                            isinstance(part, ast.Attribute) and
                            part.attr == "manifest"
                            for part in ast.walk(call.func.value)):
                    self.policy_names.update(names)

    @staticmethod
    def _ordered(node):
        return any(isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                   for op in node.ops)

    def visit_Compare(self, node):
        if not self._ordered(node):
            return self.generic_visit(node)
        operands = [node.left] + list(node.comparators)
        for operand in operands:
            number = _number(operand)
            if number not in (None, -1, 0, 1):
                self.issues.append(
                    (node.lineno, "literal numeric policy candidate {}".format(
                        number)))
            for part in ast.walk(operand):
                if isinstance(part, ast.Attribute) and part.attr == "value":
                    self.issues.append(
                        (node.lineno, "ordered comparison extracts .value"))
                if not isinstance(part, ast.Name):
                    continue
                name = part.id
                if name in self.policy_names:
                    self.issues.append((
                        node.lineno,
                        "ordered comparison uses extracted policy {!r}".format(
                            name)))
                numeric = self.numeric_names.get(name)
                if numeric not in (None, -1, 0, 1) and \
                        name not in self.implementation_names:
                    self.issues.append((
                        node.lineno,
                        "numeric constant {!r} lacks implementation rationale"
                        .format(name)))
        self.generic_visit(node)


def _policy_comparison_issues(source):
    tree = ast.parse(source)
    audit = _PolicyComparisonAudit(source)
    audit.collect(tree)
    audit.visit(tree)
    return sorted(set(audit.issues))


class GatePolicyComparisonsAreStructural(unittest.TestCase):

    def test_literal_policy_bypass_is_detected(self):
        source = "def gate(measured):\n    return measured < 0.15\n"
        self.assertTrue(_policy_comparison_issues(source))

    def test_named_literal_and_extracted_value_bypasses_are_detected(self):
        literal = "LIMIT = 0.15\ndef gate(x):\n    return x < LIMIT\n"
        expression = ("LIMIT = 3 / 20\n"
                      "def gate(x):\n    return x < LIMIT\n")
        extracted = ("def gate(res, constraint, x):\n"
                     "    limit = res.limit(constraint).value\n"
                     "    return x < limit\n")
        self.assertTrue(_policy_comparison_issues(literal))
        self.assertTrue(_policy_comparison_issues(expression))
        self.assertTrue(_policy_comparison_issues(extracted))

    def test_explained_implementation_constant_is_not_policy(self):
        source = ("EPS = implementation_constant(1e-9, 'rounding')\n"
                  "def gate(x):\n    return x < EPS\n")
        self.assertEqual(_policy_comparison_issues(source), [])

    def test_real_gate_code_has_no_untyped_ordered_policy_comparisons(self):
        problems = []
        pattern = os.path.join(HERE, "pcbqa", "gates", "g_*.py")
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as handle:
                issues = _policy_comparison_issues(handle.read())
            problems.extend((os.path.basename(path), line, issue)
                            for line, issue in issues)
        self.assertEqual(problems, [], problems)


if __name__ == "__main__":
    unittest.main()
