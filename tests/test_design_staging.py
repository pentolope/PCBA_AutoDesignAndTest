"""Why a KiCad tool never opens the authoritative design, and what it opens.

Git is the recovery mechanism for everything else here, so the copy that
survives has to earn its place against a demonstrated tool behaviour rather
than a fear. Two behaviours are demonstrated below, and together they force
it: an export ships whatever zone fill is stored, and the only way to refresh
that fill through `kicad-cli` also rewrites the board file.

What is staged is the design reached from the declared sources - not the
repository that happens to hold it.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from tests import paths                                           # noqa: E402
from pcbqa import core                                            # noqa: E402
from pcbqa.core import Context, design_inputs, stage_design        # noqa: E402
from pcbqa.gates.g_checks import required_options                 # noqa: E402


def _digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _manifest_for(project, tag):
    with open(paths.REVA_MANIFEST, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["board_id"] = "staging-" + tag
    doc["project_root"] = project
    doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
    path = os.path.join(os.path.dirname(project), "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return core.load_manifest(path)


class _Fixture(unittest.TestCase):
    def project(self, tag):
        work = tempfile.mkdtemp(prefix="pcbqa_stage_" + tag + "_")
        self.addCleanup(shutil.rmtree, work, True)
        project = os.path.join(work, "project")
        shutil.copytree(paths.REVA_PROJECT, project)
        return work, project, _manifest_for(project, tag)


class TheToolsRequireAStagedDesign(_Fixture):
    """The two behaviours that make staging necessary, each reproduced."""

    def test_an_export_ships_whatever_fill_is_stored(self):
        """So a build cannot simply export from the tree: it must refill."""
        import pcbnew

        work, project, manifest = self.project("fill")
        board = os.path.join(project, manifest.get("sources.pcb"))
        ctx = Context(manifest, os.path.join(work, "w"))
        flags = list(manifest.get("artifacts.gerber_export_flags"))

        filled = os.path.join(work, "filled")
        ctx.run_tool([ctx.kicad_cli, "pcb", "export", "gerbers",
                      "--output", filled] + flags + [board])

        loaded = pcbnew.LoadBoard(board)
        zones = 0
        for zone in loaded.Zones():
            zone.UnFill()
            zones += 1
        self.assertGreater(zones, 0, "the fixture must have zones to unfill")
        stale_board = os.path.join(project, "stale.kicad_pcb")
        pcbnew.SaveBoard(stale_board, loaded)
        stale = os.path.join(work, "stale")
        ctx.run_tool([ctx.kicad_cli, "pcb", "export", "gerbers",
                      "--output", stale] + flags + [stale_board])

        def plane_bytes(directory):
            found = [os.path.getsize(os.path.join(directory, name))
                     for name in sorted(os.listdir(directory))
                     if "GND" in name or "In1" in name or "In2" in name]
            self.assertTrue(found, os.listdir(directory))
            return max(found)

        self.assertGreater(
            plane_bytes(filled), plane_bytes(stale) * 4,
            "an export with a stale fill must ship a visibly emptier plane; "
            "if this ever stops being true, the refill step - and the staging "
            "it forces - can be reconsidered")

    def test_refilling_through_kicad_cli_rewrites_the_board(self):
        """And this is the only refill `kicad-cli` offers."""
        import pcbnew

        work, project, manifest = self.project("save")
        board = os.path.join(project, manifest.get("sources.pcb"))
        loaded = pcbnew.LoadBoard(board)
        for zone in loaded.Zones():
            zone.UnFill()
        pcbnew.SaveBoard(board, loaded)
        before = _digest(board)

        ctx = Context(manifest, os.path.join(work, "w"))
        ctx.run_tool([ctx.kicad_cli, "pcb", "drc", "--output",
                      os.path.join(work, "drc.json"), "--format", "json"]
                     + list(required_options("drc")) + [board])
        self.assertNotEqual(
            _digest(board), before,
            "`drc --refill-zones --save-board` no longer rewrites a board "
            "whose fill is stale; if that becomes permanently true, staging "
            "exists for no reason")

    def test_a_run_leaves_a_lock_beside_the_project_it_opens(self):
        """Which is why the staged copy, not the design, is what gets opened."""
        work, project, manifest = self.project("lock")
        staged = os.path.join(work, "staged")
        stage_design(manifest, staged)
        ctx = Context(manifest, os.path.join(work, "w"))
        ctx.run_tool([ctx.kicad_cli, "pcb", "drc", "--output",
                      os.path.join(work, "drc.json"), "--format", "json"]
                     + list(required_options("drc"))
                     + [os.path.join(staged, manifest.get("sources.pcb"))])
        # The lock is removed when the run ends; what matters is that it was
        # created in the staged directory and never beside the design.
        left_behind = [name for name in os.listdir(project)
                       if name.startswith("~") or name.endswith(".lck")]
        self.assertEqual(left_behind, [],
                         "a run against the staged design touched the design")


class StagingTakesTheDesignAndNotTheRepository(_Fixture):

    def test_it_reaches_the_libraries_erc_needs(self):
        _work, _project, manifest = self.project("reach")
        staged = design_inputs(manifest)
        for expected in ("microphone_array_v2.kicad_pcb",
                         "microphone_array_v2.kicad_sch",
                         "microphone_array_v2.kicad_pro",
                         "fp-lib-table", "sym-lib-table",
                         "MicArrayV2.kicad_sym"):
            self.assertIn(expected, staged, staged)
        self.assertTrue(any(rel.startswith("MicArrayV2.pretty/")
                            for rel in staged),
                        "the project footprint library the tables name is "
                        "not staged: %s" % staged)

    def test_it_takes_no_generated_output(self):
        _work, _project, manifest = self.project("outputs")
        for rel in design_inputs(manifest):
            self.assertFalse(rel.startswith("generated/"), rel)

    def test_an_unrelated_board_in_the_tree_is_not_staged(self):
        """A sibling candidate or another board's fixture is not this design."""
        work, project, manifest = self.project("sibling")
        stray = os.path.join(project, "candidates")
        os.makedirs(stray)
        shutil.copy2(os.path.join(project, manifest.get("sources.pcb")),
                     os.path.join(stray, "someone_elses.kicad_pcb"))
        staged = design_inputs(manifest)
        self.assertNotIn("candidates/someone_elses.kicad_pcb", staged, staged)

        destination = os.path.join(work, "staged")
        stage_design(manifest, destination)
        self.assertFalse(os.path.exists(
            os.path.join(destination, "candidates")))

    def test_the_staged_tree_keeps_relative_paths(self):
        """`check_path` joins a manifest-relative path onto it."""
        work, project, manifest = self.project("relative")
        destination = os.path.join(work, "staged")
        staged = stage_design(manifest, destination)
        for rel in staged:
            self.assertTrue(os.path.isfile(os.path.join(destination, rel)),
                            rel)
        self.assertTrue(os.path.isfile(os.path.join(
            destination, manifest.get("sources.pcb"))))
        self.assertFalse(os.path.isfile(os.path.join(
            project, "~microphone_array_v2.kicad_pro.lck")))

    def test_the_whole_project_copy_is_gone(self):
        """The clean room copied the repository. Nothing does now."""
        self.assertFalse(hasattr(core, "copy_project"))
        self.assertFalse(hasattr(core, "NEVER_COPY"))
        self.assertFalse(hasattr(core, "ORDERABLE_SUFFIXES"))


if __name__ == "__main__":
    unittest.main()
