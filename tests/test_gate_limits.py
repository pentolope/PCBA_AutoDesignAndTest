"""Every limit a gate applies is a typed constraint traced to the manifest.

This used to be a gate - a check that ran against a consumer's board to prove
the toolkit's own programmers had used its API correctly, and that required
every consuming board to carry a `constraint_parity` block to enable it. Three
of the four defects it looked for are now unrepresentable: `GateResult.limit`
refuses anything but a `Constraint`, `Manifest.constraint` takes the value from
the key it names, and `Constraint` refuses to exist without units.

What is left is a property of this repository's own source, so it is checked
here, against every gate, on this repository's own fixtures.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                           # noqa: E402
from pcbqa import core                                            # noqa: E402
from pcbqa.constraints import Constraint, ConstraintError          # noqa: E402
from pcbqa.core import Context, GateResult, Manifest               # noqa: E402
from pcbqa.gates import (g_provenance, g_checks, g_geometry,       # noqa: E402,F401
                         g_contracts, g_assembly, g_export_parity,
                         g_fabrication, g_orientation, g_timing)


class TheApiRefusesAnUntypedLimit(unittest.TestCase):
    """The three defects that no longer need detecting."""

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


class TheParityGatesAreGone(unittest.TestCase):
    """A board no longer configures the policing of toolkit implementation."""

    def test_no_gate_scans_prose_or_python_for_toolkit_style(self):
        registered = {entry["id"] for entry in core.registered()}
        for gate_id in ("CFG.THRESHOLD_PARITY", "CFG.NO_RIVAL_THRESHOLDS",
                        "PROV.SOURCE_AUTHORITY"):
            self.assertNotIn(gate_id, registered)

    def test_no_manifest_this_repository_ships_configures_them(self):
        import json
        for path in (paths.REVA_MANIFEST, paths.PORTABILITY_MANIFEST,
                     paths.CLEAN_MANIFEST):
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            for key in ("source_authority", "constraint_parity"):
                self.assertNotIn(key, doc, "{} still declares {}".format(
                    os.path.basename(path), key))


if __name__ == "__main__":
    unittest.main()
