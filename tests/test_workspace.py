"""Scratch-space lifecycle: one run per invocation, and it owns only itself.

The durable outputs of this toolkit are committed files in a project and a Git
tag over the commit that carries them. What is left here is working space::

    out/<board_id>/<run_id>/{work,build}

Two properties. A manifest is validated before anything filesystem-shaped
exists, so untrusted text never becomes a path. And an invocation can only
delete inside the directory it created.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run as run_cli                                    # noqa: E402
from tests import paths                                  # noqa: E402
from pcbqa import core, layout                           # noqa: E402
from pcbqa.layout import LayoutError, Workspace          # noqa: E402
from pcbqa.parallel import ENV_OUTPUT_ROOT               # noqa: E402

REVA = paths.REVA_MANIFEST
FIXTURE = paths.REVA_PROJECT
ATTRIBUTES = paths.ATTRIBUTES

COMMANDS = ("validate", "build", "release-check")


def digest_tree(root):
    """Every file under `root` with its digest. The unit of "unchanged"."""
    out = []
    if not os.path.exists(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out.append((os.path.relpath(full, root).replace(os.sep, "/"),
                            hashlib.sha256(fh.read()).hexdigest()))
    return sorted(out)


class _Board:
    """A writable board fixture with its own output root."""

    def __init__(self, board_id="workspace-board", mutate=None):
        self.work = tempfile.mkdtemp(prefix="pcbqa_ws_")
        self.project = os.path.join(self.work, "project")
        shutil.copytree(FIXTURE, self.project)
        with open(REVA, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["board_id"] = board_id
        doc["project_root"] = self.project
        doc["fixture"] = {"attributes_file": ATTRIBUTES}
        if mutate:
            mutate(doc)
        self.manifest_path = os.path.join(self.work, "manifest.json")
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        self.board_id = board_id
        self.out = os.path.join(self.work, "out")
        self.board = os.path.join(self.out, board_id)

    def write_raw_manifest(self, text):
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def run(self, command):
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = self.work
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                if command == "validate":
                    code = run_cli.cmd_validate(self.manifest_path)[0]
                elif command == "build":
                    code = run_cli.cmd_build(self.manifest_path)
                else:
                    code = run_cli.cmd_release_check(self.manifest_path)
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved
        return code, captured.getvalue()

    def runs(self):
        if not os.path.isdir(self.board):
            return []
        return sorted(d for d in os.listdir(self.board)
                      if os.path.isdir(os.path.join(self.board, d)))

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


class _Base(unittest.TestCase):
    def _board(self, board_id="workspace-board", mutate=None):
        board = _Board(board_id, mutate)
        self.addCleanup(board.close)
        return board


class UntrustedManifestsNeverReachTheFilesystem(_Base):
    """One load path, and nothing is created or removed before it succeeds."""

    HOSTILE = [
        ("malformed json", '{ "board_id": "workspace-board", oops }'),
        ("json list", '["board_id", "workspace-board"]'),
        ("json scalar", '"workspace-board"'),
        ("no board_id", '{"schema_version": 2}'),
        ("traversal id", '{"schema_version": 2, "board_id": "../victim"}'),
        ("absolute id", '{"schema_version": 2, "board_id": "/tmp/victim"}'),
        ("separator id", '{"schema_version": 2, "board_id": "a/b"}'),
        ("backslash id", '{"schema_version": 2, "board_id": "a\\\\b"}'),
        ("dotdot id", '{"schema_version": 2, "board_id": ".."}'),
        ("empty id", '{"schema_version": 2, "board_id": ""}'),
        ("non-string id", '{"schema_version": 2, "board_id": 17}'),
        ("wrong schema", '{"schema_version": 99, "board_id": "workspace-board"}'),
    ]

    def test_every_hostile_manifest_is_refused_by_every_command(self):
        for label, text in self.HOSTILE:
            for command in COMMANDS:
                with self.subTest(manifest=label, command=command):
                    board = self._board()
                    # A bystander outside the output root, in its own
                    # directory: rewriting the manifest must not be mistakeable
                    # for the run having touched something.
                    bystander = os.path.join(board.work, "bystander")
                    os.makedirs(bystander)
                    with zipfile.ZipFile(
                            os.path.join(bystander, "unrelated.zip"), "w") as zf:
                        zf.writestr("x.gbr", "G04*")
                    os.makedirs(board.board, exist_ok=True)
                    with zipfile.ZipFile(
                            os.path.join(board.board, "prior.zip"), "w") as zf:
                        zf.writestr("y.gbr", "G04*")
                    before = digest_tree(board.out)
                    bystander_before = digest_tree(bystander)

                    board.write_raw_manifest(text)
                    code, output = board.run(command)

                    self.assertEqual(code, 1, output)
                    self.assertIn("REFUSED", output)
                    self.assertNotIn("Traceback", output)
                    self.assertEqual(digest_tree(board.out), before,
                                     "the output tree was mutated")
                    self.assertEqual(digest_tree(bystander), bystander_before,
                                     "something outside the output root moved")
                    board.close()

    def test_a_valid_board_id_still_works(self):
        for board_id in ("microphone_array_v2-revA", "widget_b",
                         "pcbqa-clean-fixture", "a", "A1.b_c-d"):
            with self.subTest(board_id=board_id):
                self.assertTrue(layout.valid_board_id(board_id))
        board = self._board("microphone_array_v2-revA")
        manifest = core.load_manifest(board.manifest_path)
        derived = Workspace.for_manifest(manifest, board.work)
        self.assertEqual(manifest.board_id, "microphone_array_v2-revA")
        self.assertTrue(derived.contains(derived.board))
        self.assertEqual(os.path.dirname(derived.board), derived.root)

    def test_the_workspace_refuses_to_leave_its_root(self):
        base = tempfile.mkdtemp(prefix="pcbqa_ws_root_")
        self.addCleanup(shutil.rmtree, base, True)
        for hostile in ("../victim", "..", "/etc", "a/b", "", ".", "x" * 200):
            with self.subTest(board_id=hostile):
                with self.assertRaises(LayoutError):
                    Workspace(hostile, base)
        good = Workspace("safe-board", base)
        self.assertFalse(good.contains(good.root))
        self.assertFalse(good.contains(os.path.dirname(good.root)))
        self.assertTrue(good.contains(good.board))

    def test_there_is_exactly_one_manifest_loading_path(self):
        """No production module may parse a manifest for itself."""
        offenders = []
        for directory, _dirs, files in os.walk(paths.PACKAGE):
            if "__pycache__" in directory:
                continue
            for name in files:
                if not name.endswith(".py") or name == "core.py":
                    continue
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if "Manifest(" in text and "load_manifest" not in text:
                    offenders.append(os.path.relpath(path, HERE))
        self.assertFalse(offenders,
                         "modules constructing manifests outside the loader: "
                         + str(offenders))


class ARunOwnsOnlyItself(_Base):
    """The only directory an invocation may delete is the one it created."""

    def test_a_run_can_only_delete_inside_itself(self):
        base = tempfile.mkdtemp(prefix="pcbqa_own_")
        self.addCleanup(shutil.rmtree, base, True)
        workspace = Workspace("own-board", base)
        first = workspace.new_run()
        second = workspace.new_run()
        outside = os.path.join(base, "not_ours")
        os.makedirs(outside)

        self.assertTrue(first.owns(first.path))
        self.assertTrue(first.owns(os.path.join(first.work, "x")))
        self.assertFalse(first.owns(second.path))
        self.assertFalse(first.owns(outside))
        self.assertFalse(first.owns(workspace.board))
        self.assertFalse(first.owns(""))

        first.discard()
        self.assertFalse(os.path.exists(first.path))
        self.assertTrue(os.path.isdir(second.path))
        self.assertTrue(os.path.isdir(outside))

    def test_validate_writes_only_into_its_own_run(self):
        board = self._board()
        marker = os.path.join(board.board, "PRE_EXISTING.txt")
        os.makedirs(board.board, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("kept")
        code, output = board.run("validate")
        self.assertEqual(code, 1, output[-800:])       # a negative fixture
        self.assertEqual(len(board.runs()), 1, board.runs())
        self.assertTrue(os.path.isfile(marker), "a run removed a sibling file")
        self.assertTrue(os.path.isfile(os.path.join(
            board.board, board.runs()[0], "validation.json")))

    def test_two_runs_do_not_share_a_directory(self):
        board = self._board()
        board.run("validate")
        board.run("validate")
        self.assertEqual(len(board.runs()), 2, board.runs())


if __name__ == "__main__":
    unittest.main()
