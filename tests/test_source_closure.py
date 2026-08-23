"""What the source closure covers, and whether it says the same thing twice.

A closure is an identity for "the inputs this result came from". It is only
worth having if two machines looking at the same inputs compute the same value,
and if a real change to any of those inputs changes it. Both were broken:

  * a recursive `**/*.kicad_sch` glob swept in every other board's fixture and
    every past attempt's copy, so the identity depended on what happened to be
    left lying in the validator's output tree;
  * the manifest entered provenance as its file digest, and a clean room must
    rewrite the manifest's paths, so the reports a run produced could never be
    checked from the repository that produced them;
  * text files were hashed as raw bytes, so a checkout with the other line
    ending had a different identity for a file nobody had edited.
"""

from __future__ import annotations

import copy
import json
import re
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import canonical, cleanroom                        # noqa: E402
from pcbqa.core import Manifest                               # noqa: E402

from tests import consumer                                               # noqa: E402


def _live():
    """The registered consumer board, or skip. See tests/consumer.py."""
    return consumer.require()


def _project():
    return consumer.project_root()


def _attributes():
    return os.path.join(consumer.project_root(), ".gitattributes")

_SCRATCH = []


def _scratch(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    _SCRATCH.append(path)
    return path


def tearDownModule():
    for path in _SCRATCH:
        shutil.rmtree(path, ignore_errors=True)
    del _SCRATCH[:]


def _doc():
    with open(_live(), encoding="utf-8") as fh:
        return json.load(fh)


def _manifest(document, directory=None):
    """Write a manifest somewhere scratch, still pointed at the real project.

    project_root is relative to the manifest file, so a copy written into a
    temporary directory has to be re-anchored or it resolves to nothing and
    every question about the design answers "no such file".
    """
    directory = directory or _scratch("pcbqa_closure_")
    if not os.path.isabs(document.get("project_root", "")):
        document = dict(document, project_root=_project())
    path = os.path.join(directory, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2)
    return Manifest(path)


def _policy():
    return canonical.AttributePolicy.load(_attributes())


def _flat(document, prefix=""):
    """Every dotted key a manifest document contains."""
    keys = set()
    for key, value in document.items():
        full = "{}.{}".format(prefix, key) if prefix else key
        keys.add(full)
        if isinstance(value, dict):
            keys |= _flat(value, full)
    return keys


def _live_closure():
    manifest = Manifest(_live())
    return cleanroom.source_closure(manifest, _policy())


@consumer.needed
class TheClosureCoversTheDesignAndNothingElse(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.closure = _live_closure()

    def test_it_covers_the_board_the_schematic_and_the_project(self):
        """Whatever the consumer declared as sources, not a fixed set of names."""
        for role, declared in Manifest(_live()).get("sources").items():
            self.assertIn(declared, self.closure,
                          "sources.{} is not in the closure".format(role))

    def test_it_covers_the_orientation_inputs(self):
        """Every declared reproduction input, counted from the manifest."""
        spec = Manifest(_live()).get(
            "release_generation.cpl_orientation.reproduction_inputs")
        registry = Manifest(_live()).get(
            "release_generation.cpl_orientation.registry")
        for pattern in spec["required_globs"]:
            if "*" in pattern:
                prefix = pattern.split("*")[0]
                self.assertTrue(
                    any(k.startswith(prefix) for k in self.closure),
                    "nothing in the closure matches {}".format(pattern))
            else:
                self.assertIn(pattern, self.closure)
        # One extract and one raw body per reviewed entry, both individually.
        for entry in registry:
            for key in ("evidence_file", "raw_file"):
                self.assertIn(entry[key].replace("\\", "/"), self.closure,
                              "{} of {} is not in the closure".format(
                                  key, entry.get("part_number")))

    def test_the_closure_carries_exactly_what_the_board_pinned(self):
        """A mechanical property: the closure reflects the declaration.

        Worth checking, and worth being clear about what it does *not* check.
        Both sides come from `reports.implementation_closure`, so this cannot
        notice a module that should have been declared and was not - delete an
        entry and both sides shrink together. That omission is what
        `test_the_toolkit_and_not_the_board_decides_what_must_be_pinned`
        below is for.
        """
        self.assertIn("<configuration>", self.closure)
        declared = Manifest(_live()).get("reports.implementation_closure")
        self.assertTrue(declared,
                        "the consumer pins no implementation at all")
        executed = sorted(k for k in self.closure
                          if k.startswith("<executed>"))
        self.assertEqual(executed,
                         sorted("<executed>" + name for name in declared))
        import importlib
        for name in declared:
            module = importlib.import_module(name)
            self.assertTrue(os.path.isfile(getattr(module, "__file__", "")),
                            "{} has no importable file".format(name))

    def test_the_toolkit_and_not_the_board_decides_what_must_be_pinned(self):
        """The independent half, and the one that can actually fail.

        Each gate states which modules compute what it reports. That statement
        lives in the toolkit, so a board cannot satisfy it by editing its own
        manifest - which is precisely the failure a declaration-versus-closure
        comparison is blind to.
        """
        from pcbqa import core
        from pcbqa.gates import (g_assembly, g_checks, g_contracts,   # noqa: F401
                                 g_export_parity, g_fabrication, g_geometry,
                                 g_orientation, g_provenance, g_timing)
        manifest = Manifest(_live())
        applicable = [entry["id"] for entry in core.registered()
                      if all(manifest.has(key) for key in entry["requires"])]
        needed = core.derivation_modules(applicable)
        self.assertTrue(needed,
                        "no applicable gate derives anything, so this board "
                        "cannot demonstrate the property")
        missing = sorted(module for module in needed
                         if "<executed>" + module not in self.closure)
        self.assertFalse(
            missing,
            "these modules compute results this board reports and are not in "
            "its source closure: {}. Required by: {}".format(
                missing, {m: needed[m] for m in missing}))

    def test_dropping_a_required_module_is_detected(self):
        """Prove the check above can fail, on a manifest that is consistent.

        The manifest and the closure agree with each other throughout; only
        the toolkit's own statement disagrees, which is the whole point.
        """
        from pcbqa import core
        from pcbqa.gates import g_timing                              # noqa: F401
        document = _doc()
        pinned = list(document["reports"]["implementation_closure"])
        applicable = [entry["id"] for entry in core.registered()
                      if all(k in _flat(document) or
                             Manifest(_live()).has(k)
                             for k in entry["requires"])]
        needed = core.derivation_modules(applicable)
        droppable = sorted(set(needed) & set(pinned))
        if not droppable:
            self.skipTest("this consumer pins none of the modules the "
                          "registry requires, so there is nothing to drop")
        document["reports"]["implementation_closure"] = [
            name for name in pinned if name != droppable[0]]
        closure = cleanroom.source_closure(_manifest(document), _policy())
        # Consistent: the reduced declaration and the closure still match.
        executed = sorted(k[len("<executed>"):] for k in closure
                          if k.startswith("<executed>"))
        self.assertEqual(executed,
                         sorted(document["reports"]["implementation_closure"]))
        # And the toolkit still says the dropped module is load-bearing.
        self.assertNotIn("<executed>" + droppable[0], closure)
        self.assertIn(droppable[0], needed)

    def test_no_validator_fixture_or_output_leaks_in(self):
        """These exist on some machines and not others."""
        for key in self.closure:
            if key.startswith("<"):
                continue
            for forbidden in ("verification/fixtures/", "verification/out/",
                              "verification/", "generated/", "build/",
                              "candidates/", ".git/"):
                self.assertFalse(
                    key.startswith(forbidden),
                    "{} is in the live source closure; a closure that depends "
                    "on what is left in {} identifies nothing".format(
                        key, forbidden))

    def test_no_released_artifact_is_treated_as_an_input(self):
        for key in self.closure:
            self.assertNotIn("cpl.csv", key)
            self.assertNotIn("bom.csv", key)
            self.assertNotIn(".zip", key)


class LineEndingsAreNotAChange(unittest.TestCase):
    """A checkout is allowed either line ending; that is not an edit."""

    def _project(self, newline):
        """A copy of the closure's inputs, written with the given line ending."""
        root = _scratch("pcbqa_eol_")
        project = os.path.join(root, "project")
        os.makedirs(project)
        shutil.copy2(_attributes(), os.path.join(project, ".gitattributes"))
        manifest = Manifest(_live())
        for pattern in manifest.get("reports.source_closure"):
            import glob
            for path in glob.glob(os.path.join(_project(), pattern)):
                if not os.path.isfile(path):
                    continue
                rel = os.path.relpath(path, _project())
                target = os.path.join(project, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(path, "rb") as fh:
                    body = fh.read()
                body = body.replace(b"\r\n", b"\n")
                if newline == b"\r\n":
                    body = body.replace(b"\n", b"\r\n")
                with open(target, "wb") as fh:
                    fh.write(body)
        document = _doc()
        document["project_root"] = project
        return _manifest(document, root), project

    def test_lf_and_crlf_checkouts_have_one_identity(self):
        lf, _ = self._project(b"\n")
        crlf, _ = self._project(b"\r\n")
        policy = _policy()
        left = cleanroom.source_closure(lf, policy)
        right = cleanroom.source_closure(crlf, policy)
        self.assertEqual(sorted(left), sorted(right))
        self.assertEqual(cleanroom.closure_digest(left),
                         cleanroom.closure_digest(right),
                         "the same design checked out with the other line "
                         "ending is not the same design")

    def test_a_real_edit_still_changes_the_identity(self):
        """Otherwise the fix above would be indistinguishable from ignoring it."""
        crlf, project = self._project(b"\r\n")
        policy = _policy()
        before = cleanroom.closure_digest(
            cleanroom.source_closure(crlf, policy))
        target = os.path.join(project, "fabrication", "jlc_orientation",
                              "raw", "C7668.json")
        with open(target, "ab") as fh:
            fh.write(b" ")
        after = cleanroom.closure_digest(
            cleanroom.source_closure(crlf, policy))
        self.assertNotEqual(before, after,
                            "an edited evidence body left the closure alone")

    def test_the_binding_and_checking_sides_agree_on_a_source_digest(self):
        """The report writes one identity and the gate recomputes the same one."""
        policy = _policy()
        board = os.path.join(_project(), "microphone_array_v2.kicad_pcb")
        rel = "microphone_array_v2.kicad_pcb"
        recorded = canonical.digest(board, policy.classify(rel))
        installed = os.path.join(_project(), "generated", "release", "reports",
                                 "drc.json")
        if not os.path.isfile(installed):
            self.skipTest("no installed DRC report to compare against")
        with open(installed, encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(report.get("source_sha256"), recorded,
                         "the committed DRC report is bound to a different "
                         "identity of the board than the gate computes")


class TheConfigurationIdentitySurvivesTheCleanRoom(unittest.TestCase):
    """The clean room rewrites paths. It must not rewrite the identity."""

    def _rewritten_like_a_clean_room(self, document):
        """Exactly the pointers derive_manifest() is entitled to change."""
        out = copy.deepcopy(document)
        out["board_id"] = str(out["board_id"]) + "-cleanroom"
        out["project_root"] = "fixture/project"
        out["fixture"] = dict(out["fixture"])
        out["fixture"]["hash_file"] = "../HASHES.json"
        out["fixture"]["attributes_file"] = "../.gitattributes"
        out["reports"] = dict(out["reports"])
        out["reports"]["files"] = ["../../reports/*.json"]
        out["artifacts"] = dict(out["artifacts"])
        for key in ("gerber_dir", "bom", "cpl"):
            out["artifacts"][key] = "/run/build/" + key
        out["artifacts"]["cpl_fields"] = {"Designator": "Designator"}
        out["artifacts"]["cpl_origin"] = {"frame": "somewhere else"}
        out["assembly"] = dict(out["assembly"])
        out["assembly"]["bom_fields"] = {"designators": "Designator"}
        out["archive"] = dict(out["archive"])
        out["archive"]["zip"] = "/run/build/x.zip"
        out["archive"]["manifest"] = "/run/build/MANIFEST.md"
        out["archive"].pop("pre_normalization_digests", None)
        return out

    def test_rewriting_paths_does_not_change_the_identity(self):
        origin = _manifest(_doc())
        derived = _manifest(self._rewritten_like_a_clean_room(_doc()))
        self.assertNotEqual(origin.sha256, derived.sha256,
                            "the two manifests must really differ as files")
        self.assertEqual(cleanroom.configuration_identity(origin),
                         cleanroom.configuration_identity(derived),
                         "a clean room's path rewrites changed the "
                         "configuration identity, so its reports can never be "
                         "checked from the repository")

    def test_changing_a_covered_value_does_change_the_identity(self):
        origin = _manifest(_doc())
        edited = _doc()
        spec = edited["release_generation"]["cpl_orientation"]
        spec["registry"][0]["offset_deg"] = 90.0
        self.assertNotEqual(cleanroom.configuration_identity(origin),
                            cleanroom.configuration_identity(_manifest(edited)),
                            "a changed reviewed offset left the configuration "
                            "identity alone")

    def test_a_new_threshold_is_covered_without_being_listed(self):
        """Coverage is by default; only the path pointers are excluded."""
        edited = _doc()
        edited["geometry_profile"]["invented_tolerance_mm"] = 1.0
        self.assertNotEqual(cleanroom.configuration_identity(_manifest(_doc())),
                            cleanroom.configuration_identity(_manifest(edited)))

    def test_the_excluded_pointers_are_only_pointers(self):
        excluded = Manifest(_live()).get("reports.configuration_excludes")
        for pointer in excluded:
            self.assertFalse(
                pointer.startswith("release_generation"),
                "{} would exclude release-affecting configuration".format(
                    pointer))
            self.assertFalse(pointer.startswith("release_profile"), pointer)

    def test_the_permitted_set_is_exactly_what_the_clean_room_rewrites(self):
        """Read off derive_manifest rather than trusted.

        Every permitted exclusion must correspond to an assignment in
        derive_manifest, and every assignment there must be permitted. A leaf
        that stops being rewritten should stop being excusable.
        """
        import inspect
        source = inspect.getsource(cleanroom.CleanRun.derive_manifest)
        assigned = set(re.findall(r'data\["(\w+)"\](?:\["(\w+)"\])?\s*(?:=|\.pop)',
                                  source))
        pointers = set()
        for head, tail in assigned:
            pointers.add("{}.{}".format(head, tail) if tail else head)
        permitted = set(cleanroom.REWRITTEN_BY_THE_CLEAN_ROOM)

        # derive_manifest also reassigns whole objects purely to copy them
        # before editing a leaf; those are not rewrites of the object.
        copied_before_editing = {"fixture", "artifacts", "assembly", "archive",
                                 "reports"}
        rewritten = {p for p in pointers if p not in copied_before_editing}
        self.assertEqual(
            rewritten - permitted, set(),
            "derive_manifest rewrites values the identity still covers")
        for pointer in permitted:
            head = pointer.split(".")[0]
            self.assertIn(head, pointers | copied_before_editing,
                          "{} is excused but derive_manifest never touches "
                          "it".format(pointer))

    def test_whole_objects_that_are_not_rewritten_are_not_excused(self):
        for pointer in cleanroom.REWRITTEN_BY_THE_CLEAN_ROOM:
            self.assertNotIn(pointer, ("sources", "tools", "fixture"),
                             "{} is copied through untouched or only partly "
                             "rewritten; excusing the whole object hides real "
                             "configuration".format(pointer))


class TheIdentityCoversWhatWasSelected(unittest.TestCase):
    """The closure has to change when the release would produce something else.

    Excluding whole objects because they contain a path made three real
    changes invisible: pointing the release at a different board, pointing it
    at a different schematic, and changing which files the fixture rejects.
    """

    def _digest(self, document):
        return cleanroom.closure_digest(
            cleanroom.source_closure(_manifest(document), _policy()))

    def test_changing_the_selected_pcb_changes_the_closure(self):
        candidate = os.path.join(_project(), "candidates")
        boards = sorted(f for f in os.listdir(candidate)
                        if f.endswith(".kicad_pcb")) if os.path.isdir(
                            candidate) else []
        if not boards:
            self.skipTest("this project keeps no candidate boards")
        edited = _doc()
        edited["sources"]["pcb"] = "candidates/" + boards[0]
        self.assertNotEqual(
            self._digest(_doc()), self._digest(edited),
            "selecting a different board left the closure unchanged")

    def test_changing_the_selected_schematic_changes_the_closure(self):
        edited = _doc()
        edited["sources"]["schematic"] = "MicArrayV2.kicad_sym"
        self.assertNotEqual(self._digest(_doc()), self._digest(edited))

    def test_changing_the_fixture_rejection_policy_changes_the_closure(self):
        edited = _doc()
        edited["fixture"]["reject_globs"] = []
        self.assertNotEqual(
            self._digest(_doc()), self._digest(edited),
            "emptying fixture.reject_globs left the closure unchanged")

    def test_changing_the_toolchain_changes_the_closure(self):
        edited = _doc()
        edited["tools"]["kicad_cli"] = "/somewhere/else/kicad-cli"
        self.assertNotEqual(self._digest(_doc()), self._digest(edited))

    def test_a_selected_source_outside_the_globs_is_still_covered(self):
        """Included because it was selected, not because a glob reached it."""
        candidate = os.path.join(_project(), "candidates")
        boards = sorted(f for f in os.listdir(candidate)
                        if f.endswith(".kicad_pcb")) if os.path.isdir(
                            candidate) else []
        if not boards:
            self.skipTest("this project keeps no candidate boards")
        rel = "candidates/" + boards[0]
        edited = _doc()
        edited["sources"]["pcb"] = rel
        closure = cleanroom.source_closure(_manifest(edited), _policy())
        self.assertIn(rel, closure,
                      "the selected board is under an excluded directory and "
                      "was not covered at all")
        self.assertEqual(
            closure[rel],
            canonical.digest(os.path.join(_project(), rel),
                             _policy().classify(rel)))

    def test_a_selected_source_that_does_not_exist_is_refused(self):
        edited = _doc()
        edited["sources"]["pcb"] = "no_such_board.kicad_pcb"
        with self.assertRaises(cleanroom.CleanRoomError) as caught:
            self._digest(edited)
        self.assertIn("sources.pcb", str(caught.exception))

    def test_all_three_declared_sources_are_in_the_closure(self):
        manifest = Manifest(_live())
        closure = cleanroom.source_closure(manifest, _policy())
        for role in ("pcb", "schematic", "project"):
            declared = manifest.get("sources." + role)
            rel = declared.replace("\\", "/")
            self.assertIn(rel, closure,
                          "sources.{} is not represented in the "
                          "closure".format(role))

    def test_a_board_cannot_widen_the_exclusions(self):
        edited = _doc()
        edited["reports"]["configuration_excludes"] = list(
            edited["reports"]["configuration_excludes"]) + \
            ["release_generation"]
        with self.assertRaises(cleanroom.CleanRoomError) as caught:
            cleanroom.configuration_identity(_manifest(edited))
        self.assertIn("release_generation", str(caught.exception))

    def test_the_script_that_ran_is_checked_by_content(self):
        """Not by import cache, and not vacuously.

        The check used to compare `sys.modules["jlc_orientation"].__file__`
        against the tracked path. That is a fact about the process: a worker
        that had already loaded another project's copy answered about that
        copy, and the gate failed for a project that was perfectly fine. What
        matters is whether the file that ran has the content the closure
        recorded.
        """
        import tempfile as tf
        from pcbqa.core import Context
        from pcbqa.gates import g_orientation, g_provenance   # noqa: F401

        def run():
            ctx = Context(Manifest(_live()), _scratch("pcbqa_ran_"))
            from pcbqa import core as pcbqa_core
            results = pcbqa_core.run_all(ctx, only={"PROV.SOURCE_CLOSURE"})
            return {r.gate_id: r.to_dict()
                    for r in results}["PROV.SOURCE_CLOSURE"]

        edited = os.path.join(_scratch("pcbqa_ranfile_"), "jlc_orientation.py")
        with open(os.path.join(_project(), "tools", "jlc_orientation.py"),
                  "rb") as fh:
            body = fh.read()
        with open(edited, "wb") as fh:
            fh.write(body + b"\n# not the script the closure recorded\n")

        saved = g_orientation.LAST_DERIVATION
        g_orientation.LAST_DERIVATION = {"file": edited, "project": _project()}
        try:
            result = run()
        finally:
            g_orientation.LAST_DERIVATION = saved
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(
            any("did not derive these offsets" in f.get("issue", "")
                for f in result["findings"]), result["findings"])

        # And the same gate passes once the executed file is the tracked one.
        g_orientation.LAST_DERIVATION = {
            "file": os.path.join(_project(), "tools", "jlc_orientation.py"),
            "project": _project()}
        try:
            self.assertEqual(run()["status"], "PASS")
        finally:
            g_orientation.LAST_DERIVATION = saved
        del tf

    def test_a_board_may_narrow_them(self):
        edited = _doc()
        edited["reports"]["configuration_excludes"] = ["board_id",
                                                       "project_root"]
        cleanroom.configuration_identity(_manifest(edited))


if __name__ == "__main__":
    unittest.main()
