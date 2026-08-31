"""The reviewed orientation registry, the generator that uses it, and the gate.

Two corrections reach a placement angle, and confusing them is expensive.
Normalisation moves an angle into the range the assembly house reads and turns
nothing. A library-zero offset does turn the part, and only where the house's
library and the layout disagree about the part's zero.

The failure that started this was neither: a part whose orientation nobody had
checked shipped at whatever the layout said, because the lookup defaulted to
zero. So the case these tests care about most is the *absent* entry, and it is
driven through the real generation code rather than by editing an output.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import core                                          # noqa: E402
from pcbqa.core import Context, Manifest, Status                # noqa: E402
from pcbqa.gates import g_orientation                           # noqa: E402,F401
from pcbqa.orientation import (OrientationError, Registry,      # noqa: E402
                               apply_to_rows, normalise)

from tests import consumer                                                 # noqa: E402

GATE = "CPL.ORIENTATION"


def _live():
    """The registered consumer board, or skip. See tests/consumer.py.

    This module needs a board that ships frozen part-orientation evidence and
    an installed CPL, which no fixture here provides: the evidence is a record
    of a manufacturer's own library for the specific parts a board uses, so it
    belongs to the board, not to the toolkit.
    """
    return consumer.require()


def _project():
    return consumer.project_root()


def _cpl():
    return os.path.join(consumer.project_root(), "generated", "release",
                        "cpl.csv")

# The angles the four corrected parts must ship at. Recorded here as plain
# numbers so a change to the registry has to be argued for twice.
EXPECTED_U = {"U1": 180.0, "U2": 270.0, "U3": 270.0, "U4": 270.0}
# A sample of the 48 whose JLC-revised values were never supplied. Their
# fractional angles must survive, normalised and otherwise untouched.
EXPECTED_FRACTIONAL = {
    "CB1": 33.75, "CB3": 213.75, "CM2": 202.5, "CM10": 22.5,
    "MK1": 270.0, "MK2": 292.5, "RD8": 337.5, "RV16": 337.5,
}


# The validator, its output tree and the routing scratch are not the project
# being released. A clean run copies the project itself, so a test copy that
# dragged verification/out along would be copying gigabytes of past attempts.
_NOT_THE_PROJECT = shutil.ignore_patterns(".git", "verification", "build",
                                          "candidates", "__pycache__", "*.pyc")


# Scratch directories, cleaned up when the module finishes. tempfile puts them
# wherever TMPDIR points, which on some machines is inside the checkout, so
# leaving them behind litters the repository the tests are checking.
_SCRATCH = []


def _scratch(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    _SCRATCH.append(path)
    return path


def tearDownModule():
    for path in _SCRATCH:
        shutil.rmtree(path, ignore_errors=True)
    del _SCRATCH[:]


def _copy_project_safely(destination):
    """Copy the project without the copy chasing its own tail.

    tempfile puts its directories wherever TMPDIR says, and on a machine where
    that is inside the repository a plain copytree descends into the copy it is
    writing until the path length gives out. The shared helper blocks the
    destination and every ancestor of it, so the copy terminates regardless of
    where the temporary directory landed.
    """
    from pcbqa.core import copy_project
    copy_project(_project(), destination)
    for unwanted in ("verification", "build", "candidates"):
        path = os.path.join(destination, unwanted)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _manifest_doc():
    with open(_live(), encoding="utf-8") as fh:
        return json.load(fh)


def _spec():
    return _manifest_doc()["release_generation"]["cpl_orientation"]


def _cpl_rows(path=None):
    path = path or _cpl()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _board_numbers_and_angles():
    import pcbnew
    board = pcbnew.LoadBoard(os.path.join(
        _project(), consumer.document()["sources"]["pcb"]))
    out = {}
    for fp in board.Footprints():
        number = ""
        for field in fp.GetFields():
            if field.GetName() == "LCSC" and field.GetText().strip():
                number = field.GetText().strip()
        out[fp.GetReference()] = (number, fp.GetOrientationDegrees())
    return out


def _sha256_file(path):
    from pcbqa.core import sha256_file
    return sha256_file(path)


def _run_gate(manifest_path, gate_id=GATE):
    from pcbqa.gates import g_provenance                       # noqa: F401
    ctx = Context(Manifest(manifest_path),
                  _scratch("pcbqa_orient_work_"))
    results = core.run_all(ctx, only={gate_id})
    return {r.gate_id: r.to_dict() for r in results}[gate_id]


# ---------------------------------------------------------------------------
# the registry as a document
# ---------------------------------------------------------------------------

class RegistryIsReviewedAndComplete(unittest.TestCase):

    def setUp(self):
        self.spec = _spec()
        self.registry = Registry(self.spec)

    def test_it_is_internally_well_formed(self):
        self.assertEqual(self.registry.defects(), [])

    def test_every_populated_part_has_an_entry(self):
        """Coverage judged from the board, not from the registry's own list."""
        placed = {row["Designator"] for row in _cpl_rows()}
        board = _board_numbers_and_angles()
        missing = sorted(ref for ref in placed
                         if not self.registry.covers(board[ref][0]))
        self.assertEqual(missing, [], "populated parts with no reviewed entry")

    def test_parts_needing_no_turn_are_registered_rather_than_defaulted(self):
        """An explicit zero is a review; a missing entry is not."""
        zeros = [lcsc for lcsc, row in self.registry.entries.items()
                 if float(row["offset_deg"]) == 0.0]
        self.assertGreater(len(zeros), 1,
                           "this board has parts that need no turn; each must "
                           "still be reviewed explicitly")
        for lcsc in zeros:
            row = self.registry.entries[lcsc]
            self.assertEqual(row["review_status"], "reviewed")
            self.assertTrue(str(row["evidence_file"]).strip())

    def test_every_entry_cites_evidence_that_exists_and_matches(self):
        for lcsc, row in sorted(self.registry.entries.items()):
            path = os.path.join(_project(), row["evidence_file"])
            self.assertTrue(os.path.isfile(path),
                            "{} cites missing evidence".format(lcsc))
            with open(path, encoding="utf-8") as fh:
                evidence = json.load(fh)
            self.assertEqual(evidence["raw_sha256"],
                             row["evidence_sha256"],
                             "{}: recorded digest is not the evidence's"
                             .format(lcsc))
            self.assertTrue(evidence["source_url"])
            self.assertTrue(evidence["retrieved_utc"])
            self.assertTrue(evidence["pads"])

    def test_the_offsets_are_what_the_evidence_derives(self):
        """The registry is checked against JLC's geometry, not against itself."""
        sys.path.insert(0, os.path.join(_project(), "tools"))
        import jlc_orientation
        derived = jlc_orientation.derive("LCSC")
        for lcsc, row in sorted(self.registry.entries.items()):
            self.assertIn(lcsc, derived)
            self.assertTrue(derived[lcsc]["decisive"],
                            "{}: evidence does not decide".format(lcsc))
            self.assertAlmostEqual(
                float(row["offset_deg"]),
                float(derived[lcsc]["best_offset_deg"]), places=3,
                msg="{}: registry and evidence disagree".format(lcsc))

    def test_the_undocumented_edits_are_recorded(self):
        block = self.spec["undocumented_production_edits"]
        self.assertEqual(len(block["references"]), 48)
        self.assertIn("unavailable", block["verification_limit"])


# ---------------------------------------------------------------------------
# the arithmetic
# ---------------------------------------------------------------------------

class NormalisationAndLookup(unittest.TestCase):

    def test_normalisation_covers_the_turn(self):
        for angle, want in ((-90, 270), (-157.5, 202.5), (0, 0), (359.9, 359.9),
                            (360, 0), (-0.0001, 359.9999)):
            self.assertAlmostEqual(normalise(angle), want, places=4)

    def test_an_unknown_part_raises_rather_than_defaulting(self):
        registry = Registry(_spec())
        with self.assertRaises(OrientationError):
            registry.offset("C-NOT-A-PART")
        with self.assertRaises(OrientationError):
            registry.offset("")

    def test_a_registered_zero_is_not_the_same_as_no_entry(self):
        registry = Registry(_spec())
        zero = next(lcsc for lcsc, row in registry.entries.items()
                    if float(row["offset_deg"]) == 0.0)
        self.assertEqual(registry.offset(zero), 0.0)
        with self.assertRaises(OrientationError):
            registry.offset(zero + "X")


# ---------------------------------------------------------------------------
# generation
# ---------------------------------------------------------------------------

class GenerationRefusesAnUnreviewedPart(unittest.TestCase):
    """The regression test for the defect that started this.

    Not an edited output: the registry loses U2's part number and the real
    generation code is asked to produce the placement angles from it.
    """

    def _generate(self, spec):
        """What the release does: the populated placements, angles from the
        board, run through the same code the generator calls.

        Only populated parts, because that is what the position export emits -
        the hand-fitted connectors and the test pads are excluded from it and
        never reach a machine."""
        board = _board_numbers_and_angles()
        populated = sorted(row["Designator"] for row in _cpl_rows())
        rows = [{"Designator": ref, "Rotation": "{:.6f}".format(board[ref][1])}
                for ref in populated]
        numbers = {ref: board[ref][0] for ref in populated}
        return apply_to_rows(rows, Registry(spec), numbers,
                             "Designator", "Rotation")

    def test_the_intact_registry_generates_every_angle(self):
        applied, problems = self._generate(_spec())
        self.assertEqual(problems, [])
        self.assertIn("U2", applied)

    def test_removing_u2s_part_blocks_generation_naming_that_part(self):
        spec = copy.deepcopy(_spec())
        before = len(spec["registry"])
        spec["registry"] = [row for row in spec["registry"]
                            if row["lcsc"] != "C7668"]
        self.assertEqual(len(spec["registry"]), before - 1,
                         "C7668 must have been in the registry to remove")

        applied, problems = self._generate(spec)

        self.assertNotIn("U2", applied,
                         "U2 must not receive an angle without a reviewed "
                         "entry")
        blamed = [p for p in problems if p.get("reference") == "U2"]
        self.assertTrue(blamed, "generation must fail for U2 specifically: {}"
                        .format(problems))
        self.assertEqual(blamed[0].get("lcsc"), "C7668")
        self.assertIn("no reviewed orientation entry", blamed[0]["issue"])
        # and nothing else is collateral damage
        self.assertEqual([p["reference"] for p in problems], ["U2"])

    def test_the_gate_also_refuses_it(self):
        """Belt and braces: if a file were produced anyway, the gate objects."""
        spec_doc = _manifest_doc()
        spec_doc["release_generation"]["cpl_orientation"]["registry"] = [
            row for row
            in spec_doc["release_generation"]["cpl_orientation"]["registry"]
            if row["lcsc"] != "C7668"]
        work = _scratch("pcbqa_orient_missing_")
        spec_doc["project_root"] = _project()
        spec_doc["fixture"] = {
            "attributes_file": os.path.join(_project(), ".gitattributes")}
        path = os.path.join(work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(spec_doc, fh, indent=2)
        gate = _run_gate(path)
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(
            any(f.get("lcsc") == "C7668" or f.get("reference") == "U2"
                for f in gate["findings"]),
            gate["findings"])


# ---------------------------------------------------------------------------
# the shipped file
# ---------------------------------------------------------------------------

@consumer.needed
class ShippedPlacementsAreCorrect(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rows = _cpl_rows()
        cls.angle = {row["Designator"]: float(row["Rotation"])
                     for row in cls.rows}

    def test_the_gate_passes(self):
        gate = _run_gate(_live())
        self.assertEqual(gate["status"], Status.PASS, gate.get("findings"))

    def test_every_angle_is_normalised(self):
        for ref, angle in self.angle.items():
            self.assertGreaterEqual(angle, 0.0, ref)
            self.assertLess(angle, 360.0, ref)

    def test_the_four_corrected_parts_ship_their_reviewed_angles(self):
        for ref, want in EXPECTED_U.items():
            self.assertAlmostEqual(self.angle[ref], want, places=3,
                                   msg="{} shipped {}".format(ref,
                                                              self.angle[ref]))

    def test_parts_sharing_a_number_share_a_correction(self):
        board = _board_numbers_and_angles()
        turns = {}
        for ref, shipped in self.angle.items():
            number, rotation = board[ref]
            turns.setdefault(number, set()).add(round((shipped - rotation)
                                                      % 360.0, 4))
        for number, seen in sorted(turns.items()):
            self.assertEqual(len(seen), 1,
                             "{} was corrected {} different ways".format(
                                 number, len(seen)))
        u3, u4 = board["U3"][0], board["U4"][0]
        self.assertEqual(u3, u4)
        self.assertAlmostEqual(self.angle["U3"], self.angle["U4"], places=4)

    def test_the_fractional_angles_survive(self):
        """The 48 JLC edited keep their submitted values, only normalised."""
        for ref, want in EXPECTED_FRACTIONAL.items():
            self.assertAlmostEqual(self.angle[ref], want, places=4,
                                   msg="{} shipped {}".format(ref,
                                                              self.angle[ref]))
        # However many placements sit off the 45-degree grid on the board,
        # exactly that many must sit off it in the shipped file: a rounding
        # policy applied anywhere would show up as a smaller count.
        board = _board_numbers_and_angles()
        on_board = {ref for ref, (_n, angle) in board.items()
                    if ref in self.angle and angle % 45.0 not in (0.0,)}
        shipped = {ref for ref, angle in self.angle.items()
                   if angle % 45.0 > 1e-6}
        self.assertEqual(shipped, on_board,
                         "the set of off-grid angles changed between the "
                         "board and the placement file")
        self.assertGreater(len(shipped), 30)

    def test_no_part_was_turned_except_the_registered_ones(self):
        board = _board_numbers_and_angles()
        registry = Registry(_spec())
        for ref, shipped in self.angle.items():
            number, rotation = board[ref]
            turn = round((shipped - rotation) % 360.0, 4)
            want = round(registry.offset(number) % 360.0, 4)
            self.assertEqual(turn, want,
                             "{} was turned {} but its part is registered at "
                             "{}".format(ref, turn, want))


# ---------------------------------------------------------------------------
# review status
# ---------------------------------------------------------------------------

class OnlyReviewedEntriesMayBeUsed(unittest.TestCase):
    """"Somebody started looking at this" is not "somebody finished".

    The registry is a list of statements about parts, and a half-written one
    is more dangerous than an absent one: it looks like coverage. The rule is
    enforced in the shared registry so the generator and the gate cannot come
    to different conclusions about the same row.
    """

    def _spec_with_status(self, status):
        spec = copy.deepcopy(_spec())
        for row in spec["registry"]:
            if row["lcsc"] == "C7668":
                if status is None:
                    row.pop("review_status", None)
                else:
                    row["review_status"] = status
        return spec

    def test_an_unreviewed_entry_cannot_be_used(self):
        registry = Registry(self._spec_with_status("unreviewed"))
        with self.assertRaises(OrientationError) as caught:
            registry.offset("C7668")
        self.assertIn("unreviewed", str(caught.exception))
        self.assertIn("C7668", str(caught.exception))
        self.assertFalse(registry.covers("C7668"))

    def test_a_defect_names_the_part_and_what_it_said(self):
        for status in (" reviewed ", "Reviewed", None, 7):
            spec = copy.deepcopy(_spec())
            for row in spec["registry"]:
                if row["lcsc"] == "C7668":
                    row["review_status"] = status
            defects = [d for d in Registry(spec).defects()
                       if d.get("lcsc") == "C7668"]
            self.assertTrue(defects, "{!r} produced no defect".format(status))
            self.assertIn("no reviewed orientation mapping is available",
                          defects[0]["issue"])
            self.assertIn(repr(status) if status is not None else "(null)",
                          defects[0]["issue"] + defects[0]["review_status"])

    def test_a_missing_status_cannot_be_used(self):
        registry = Registry(self._spec_with_status(None))
        with self.assertRaises(OrientationError):
            registry.offset("C7668")

    #: Every way of nearly saying "reviewed". None of them says it.
    NOT_REVIEWED = (" reviewed ", "reviewed\n", "reviewed ", " reviewed",
                    "\treviewed", "Reviewed", "REVIEWED", "reviewed?",
                    "unreviewed", "pending", "", "   ", None, True, 1, 0,
                    ["reviewed"], {"status": "reviewed"})

    def test_nothing_but_the_exact_string_is_accepted(self):
        for status in self.NOT_REVIEWED:
            spec = copy.deepcopy(_spec())
            for row in spec["registry"]:
                if row["lcsc"] == "C7668":
                    row["review_status"] = status
            registry = Registry(spec)
            self.assertFalse(registry.covers("C7668"),
                             "{!r} was accepted as a review".format(status))
            with self.assertRaises(OrientationError,
                                   msg="{!r} was accepted".format(status)):
                registry.offset("C7668")
            message = ""
            try:
                registry.offset("C7668")
            except OrientationError as exc:
                message = str(exc)
            self.assertIn("C7668", message)
            self.assertIn("no reviewed orientation mapping is available",
                          message)

    def test_whitespace_is_not_trimmed_away(self):
        """The value is JSON, not something to be tidied up before reading."""
        self.assertTrue(Registry.is_reviewed("reviewed"))
        for near in (" reviewed", "reviewed ", " reviewed ", "reviewed\n",
                     "reviewed\t", "\nreviewed"):
            self.assertFalse(Registry.is_reviewed(near),
                             "{!r} was trimmed into a review".format(near))

    def test_a_non_string_is_not_coerced(self):
        for value in (None, True, 1, 1.0, ["reviewed"], {"a": "reviewed"}):
            self.assertFalse(Registry.is_reviewed(value),
                             "{!r} was coerced into a review".format(value))

    def test_an_unusable_entry_is_reported_as_a_defect(self):
        registry = Registry(self._spec_with_status("pending"))
        defects = [d for d in registry.defects() if d.get("lcsc") == "C7668"]
        self.assertTrue(defects)
        self.assertIn("pending", str(defects[0]))

    def test_generation_refuses_it_and_names_the_part(self):
        spec = self._spec_with_status("unreviewed")
        board = _board_numbers_and_angles()
        populated = sorted(row["Designator"] for row in _cpl_rows())
        rows = [{"Designator": ref,
                 "Rotation": "{:.6f}".format(board[ref][1])}
                for ref in populated]
        numbers = {ref: board[ref][0] for ref in populated}
        applied, problems = apply_to_rows(rows, Registry(spec), numbers,
                                          "Designator", "Rotation")
        self.assertNotIn("U2", applied)
        self.assertEqual([p["reference"] for p in problems], ["U2"])
        self.assertEqual(problems[0]["lcsc"], "C7668")
        self.assertIn("review_status", problems[0]["issue"])

    def test_the_gate_refuses_it_too(self):
        doc = _manifest_doc()
        for row in doc["release_generation"]["cpl_orientation"]["registry"]:
            if row["lcsc"] == "C7668":
                row["review_status"] = "unreviewed"
        work = _scratch("pcbqa_orient_status_")
        doc["project_root"] = _project()
        doc["fixture"] = {"attributes_file": os.path.join(_project(),
                                                          ".gitattributes")}
        path = os.path.join(work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        gate = _run_gate(path)
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(any(f.get("lcsc") == "C7668"
                            for f in gate["findings"]), gate["findings"])

    def test_the_shipped_registry_marks_every_entry_reviewed(self):
        registry = Registry(_spec())
        self.assertEqual(registry.unusable, {})
        self.assertEqual(len(registry.entries), 15)


# ---------------------------------------------------------------------------
# the range, which is half-open
# ---------------------------------------------------------------------------

class TheAngleRangeIsHalfOpen(unittest.TestCase):
    """[0, 360) with a tolerance on the upper end is [0, 360].

    360 and 0 are the same orientation, so letting 360 through changes no
    part - but the file then contradicts what the manifest says it contains,
    and an assembly house that range-checks the column rejects the upload.
    Tolerance is for comparing two angles that should agree; it has no
    business widening the range itself.
    """

    def _gate_with_rotation(self, value):
        """Run the gate over a CPL whose one row carries `value`."""
        work = _scratch("pcbqa_orient_range_")
        release = os.path.join(work, "generated", "release")
        os.makedirs(release)
        shutil.copytree(os.path.join(_project(), "generated", "release"),
                        release, dirs_exist_ok=True)
        rows = _cpl_rows()
        rows[0]["Rotation"] = value
        with open(os.path.join(release, "cpl.csv"), "w", newline="",
                  encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        doc = _manifest_doc()
        doc["project_root"] = work
        doc["fixture"] = {"attributes_file": os.path.join(_project(),
                                                          ".gitattributes")}
        for key in ("pcb", "schematic", "project"):
            doc["sources"][key] = os.path.join(
                _project(), doc["sources"][key])
        doc["artifacts"]["bom"] = os.path.join(
            _project(), doc["artifacts"]["bom"])
        path = os.path.join(work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        return _run_gate(path), rows[0]["Designator"]

    def test_zero_is_inside_the_range(self):
        self.assertAlmostEqual(normalise(0.0), 0.0)
        self.assertAlmostEqual(normalise(720.0), 0.0)

    def test_just_below_the_top_is_inside_the_range(self):
        self.assertAlmostEqual(normalise(359.9999), 359.9999, places=6)
        self.assertLess(normalise(-0.0001), 360.0)

    def test_normalisation_never_returns_the_open_end(self):
        for angle in (360.0, -360.0, 720.0, -1e-18, -1e-13, 359.9999999999999):
            self.assertLess(normalise(angle), 360.0,
                            "{} normalised onto the open end".format(angle))
            self.assertGreaterEqual(normalise(angle), 0.0)

    def test_rounding_cannot_carry_a_value_up_to_the_open_end(self):
        """359.99999 written to four decimals is 360.0000, which must not ship."""
        registry = Registry(_spec())
        zero = next(lcsc for lcsc, row in registry.entries.items()
                    if float(row["offset_deg"]) == 0.0)
        rows = [{"Designator": "X", "Rotation": "359.99999"}]
        applied, problems = apply_to_rows(rows, registry, {"X": zero},
                                          "Designator", "Rotation")
        self.assertEqual(problems, [])
        self.assertEqual(rows[0]["Rotation"], "0.0000")
        self.assertEqual(applied["X"]["shipped_deg"], 0.0)

    def test_the_gate_rejects_exactly_360(self):
        gate, ref = self._gate_with_rotation("360.0000")
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(
            any(f.get("reference") == ref and "360" in str(f.get("issue"))
                for f in gate["findings"]), gate["findings"])

    def test_the_gate_rejects_a_negative_angle(self):
        gate, ref = self._gate_with_rotation("-90.0000")
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(any(f.get("reference") == ref
                            for f in gate["findings"]), gate["findings"])

    def test_the_gate_accepts_just_below_the_top(self):
        """The boundary is the only thing being rejected, not the neighbourhood."""
        gate, ref = self._gate_with_rotation("359.9999")
        outside = [f for f in gate["findings"]
                   if f.get("reference") == ref and "outside" in
                   str(f.get("issue"))]
        self.assertEqual(outside, [], gate["findings"])


# ---------------------------------------------------------------------------
# the evidence itself
# ---------------------------------------------------------------------------

@consumer.needed
class TheEvidenceIsTheCommittedResponse(unittest.TestCase):
    """What is committed is the response body, not a summary of it.

    An extract that only says what the response contained is a claim about
    evidence rather than evidence. Both files are committed, the extract is
    re-derived from the body every time it is read, and each of the three
    things a determined edit could touch is checked separately.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(_project(), "tools"))
        import jlc_orientation
        cls.jo = jlc_orientation

    def setUp(self):
        # Pinned rather than inherited: these tests must not depend on what
        # some earlier test in the same worker left the module pointing at.
        self.saved = self.jo.FIXTURES
        self.jo.FIXTURES = os.path.join(_project(), "fabrication",
                                        "jlc_orientation")

    def tearDown(self):
        self.jo.FIXTURES = self.saved

    def _sandbox(self):
        """A private copy of the frozen evidence, safe to damage."""
        work = _scratch("pcbqa_evidence_")
        shutil.copytree(self.saved, os.path.join(work, "jlc_orientation"))
        self.jo.FIXTURES = os.path.join(work, "jlc_orientation")
        return self.jo.FIXTURES

    def test_every_part_commits_its_raw_body(self):
        registry = Registry(_spec())
        for lcsc, row in sorted(registry.entries.items()):
            raw = os.path.join(_project(), row["raw_file"])
            self.assertTrue(os.path.isfile(raw),
                            "{}: raw response is not committed".format(lcsc))
            with open(raw, "rb") as fh:
                body = fh.read()
            with open(os.path.join(_project(), row["evidence_file"]),
                      encoding="utf-8") as fh:
                record = json.load(fh)
            self.assertEqual(hashlib.sha256(body).hexdigest(),
                             record["raw_sha256"], lcsc)
            self.assertEqual(len(body), record["raw_bytes"], lcsc)
            self.assertEqual(record["raw_sha256"], row["evidence_sha256"], lcsc)
            self.assertTrue(record["source_url"].startswith("https://"))
            self.assertTrue(record["retrieved_utc"].endswith("Z"))

    def test_the_extract_is_derivable_from_the_body(self):
        for lcsc in self.jo.frozen_parts():
            problems, pads = self.jo.verify(lcsc)
            self.assertEqual(problems, [], lcsc)
            record = self.jo.load(lcsc)
            self.assertEqual(record["pads"], pads, lcsc)
            self.assertEqual(record["kind"], "normalised extract", lcsc)

    def test_editing_the_raw_body_is_caught(self):
        root = self._sandbox()
        path = os.path.join(root, "raw", "C7668.json")
        with open(path, "rb") as fh:
            body = fh.read()
        with open(path, "wb") as fh:
            fh.write(body + b" ")           # one byte, still valid JSON
        problems, _pads = self.jo.verify("C7668")
        self.assertTrue(problems)
        self.assertIn("digest", " ".join(p["issue"] for p in problems))

    def test_editing_the_extract_is_caught(self):
        root = self._sandbox()
        path = os.path.join(root, "C7668.json")
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        record["pads"]["1"] = [0.0, 0.0]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        problems, _pads = self.jo.verify("C7668")
        self.assertIn("not what the raw response derives",
                      " ".join(p["issue"] for p in problems))

    def test_a_missing_raw_body_is_caught(self):
        root = self._sandbox()
        os.remove(os.path.join(root, "raw", "C7668.json"))
        problems, pads = self.jo.verify("C7668")
        self.assertIsNone(pads)
        self.assertIn("not committed", problems[0]["issue"])

    def test_scoring_reads_the_body_rather_than_the_extract(self):
        """An edited extract cannot move an offset even if verify were skipped."""
        root = self._sandbox()
        path = os.path.join(root, "C7668.json")
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        record["pads"] = {n: [-p[0], -p[1]] for n, p in record["pads"].items()}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        derived = self.jo.derive("LCSC")
        self.assertAlmostEqual(derived["C7668"]["best_offset_deg"], -90.0,
                               places=3)
        self.assertTrue(derived["C7668"]["evidence_problems"])

    def test_the_offline_commands_never_touch_the_network(self):
        """A release must not depend on EasyEDA being up."""
        def refuse(*_a, **_k):
            raise AssertionError("the offline path reached the network")
        saved = self.jo.fetch
        self.jo.fetch = refuse
        try:
            derived = self.jo.derive("LCSC")
        finally:
            self.jo.fetch = saved
        self.assertEqual(len(derived), 15)
        for lcsc, row in derived.items():
            self.assertEqual(row["evidence_problems"], [], lcsc)


# ---------------------------------------------------------------------------
# the code that derives the offsets
# ---------------------------------------------------------------------------

class TheImplementationIsPinnedToo(unittest.TestCase):
    """An offset is derived, not read, so the deriving code is an input.

    Pinning the evidence and leaving the program that reads it unpinned would
    make the result reproducible only by accident. And hashing the code by
    path would not do it either: what has to be recorded is the module that
    was imported, because a stale copy at a tracked path is exactly the thing
    that would go unnoticed.
    """

    def setUp(self):
        from pcbqa import canonical, closure as closure_mod
        from pcbqa.core import Manifest
        self.closure_mod = closure_mod
        self.manifest = Manifest(_live())
        self.policy = canonical.AttributePolicy.load(
            self.manifest.resolve(self.manifest.get("fixture.attributes_file")))
        self.required = _spec()["reproduction_inputs"]["required_modules"]

    def _closure(self):
        return self.closure_mod.source_closure(self.manifest, self.policy)

    def test_every_required_module_is_in_the_closure(self):
        closure = self._closure()
        for name in self.required:
            self.assertIn("<executed>" + name, closure)

    def test_the_recorded_digest_is_the_loaded_module(self):
        import importlib
        closure = self._closure()
        for name in self.required:
            module = importlib.import_module(name)
            self.assertEqual(closure["<executed>" + name],
                             _sha256_file(module.__file__), name)

    def test_modifying_an_implementation_file_changes_the_closure(self):
        """Provenance must not survive an edit to the code it describes."""
        import importlib
        before = self.closure_mod.closure_digest(self._closure())
        name = "pcbqa.orientation"
        module = importlib.import_module(name)
        original = module.__file__
        work = _scratch("pcbqa_impl_")
        edited = os.path.join(work, "orientation.py")
        with open(original, "rb") as fh:
            body = fh.read()
        with open(edited, "wb") as fh:
            fh.write(body + b"\n# one comment, and the release is a different one\n")
        module.__file__ = edited
        try:
            after = self.closure_mod.closure_digest(self._closure())
        finally:
            module.__file__ = original
        self.assertNotEqual(before, after,
                            "editing the orientation implementation left the "
                            "source closure unchanged")

    def test_removing_one_from_the_closure_fails_the_gate(self):
        doc = _manifest_doc()
        doc["reports"]["implementation_closure"] = [
            name for name in doc["reports"]["implementation_closure"]
            if name != "pcbqa.orientation"]
        work = _scratch("pcbqa_impl_gate_")
        doc["project_root"] = _project()
        doc["fixture"] = {"attributes_file": os.path.join(_project(),
                                                          ".gitattributes")}
        path = os.path.join(work, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        gate = _run_gate(path, "PROV.SOURCE_CLOSURE")
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(any(f.get("module") == "pcbqa.orientation"
                            for f in gate["findings"]), gate["findings"])

    def test_a_module_that_cannot_be_imported_is_refused(self):
        with self.assertRaises(Exception):
            self.closure_mod.executed_implementation(["pcbqa.not_a_module"])


# ---------------------------------------------------------------------------
# the release, end to end
# ---------------------------------------------------------------------------

class ACleanReleaseRefusesAnUnreviewedPart(unittest.TestCase):
    """The whole path, not the two halves of it.

    apply_to_rows() refusing and the gate objecting are each worth testing and
    neither proves that a release cannot ship. This drives the same entry point
    a real build uses, against an isolated copy of the project whose registry
    has lost U2's part number, and requires that nothing was installed - not
    into the copy, and not into the release committed in this repository.
    """

    def _tree_digest(self, root):
        out = {}
        for base, _dirs, files in os.walk(root):
            for name in sorted(files):
                path = os.path.join(base, name)
                with open(path, "rb") as fh:
                    out[os.path.relpath(path, root)] = hashlib.sha256(
                        fh.read()).hexdigest()
        return out

    def test_it_is_rejected_and_nothing_is_installed(self):
        import run

        committed = os.path.join(_project(), "generated", "release")
        before = self._tree_digest(committed)

        work = _scratch("pcbqa_release_missing_")
        project = os.path.join(work, "project")
        _copy_project_safely(project)
        isolated = os.path.join(project, "generated", "release")
        isolated_before = self._tree_digest(isolated)

        doc = _manifest_doc()
        doc["board_id"] = "orientation-missing-mapping"
        doc["project_root"] = project
        spec = doc["release_generation"]["cpl_orientation"]
        was = len(spec["registry"])
        spec["registry"] = [row for row in spec["registry"]
                            if row["lcsc"] != "C7668"]
        self.assertEqual(len(spec["registry"]), was - 1,
                         "C7668 must have been in the registry to remove")
        manifest_path = os.path.join(work, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

        from pcbqa.parallel import ENV_OUTPUT_ROOT
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = work
        captured = io.StringIO()
        try:
            with contextlib.redirect_stdout(captured):
                code = run.cmd_build(manifest_path)
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved
        printed = captured.getvalue()

        self.assertNotEqual(code, 0, "a release shipped without U2 reviewed")
        self.assertIn("BUILD BLOCKED", printed)
        blamed = [line for line in printed.splitlines()
                  if "C7668" in line or "U2" in line]
        self.assertTrue(blamed,
                        "the refusal never names the missing part:\n"
                        + printed[-4000:])
        self.assertTrue(
            any("reviewed orientation" in line for line in blamed),
            "the refusal names the part but not the missing mapping:\n"
            + "\n".join(blamed))

        self.assertEqual(self._tree_digest(isolated), isolated_before,
                         "a blocked build installed artifacts anyway")
        self.assertEqual(self._tree_digest(committed), before,
                         "the committed release was modified by a failed run")


if __name__ == "__main__":
    unittest.main()
