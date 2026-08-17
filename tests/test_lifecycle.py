"""Output lifecycle: one attempt per invocation, publication only on success.

This replaces `test_release_no_archive.py`, which grew around a different
contract. That one asserted that a failed invocation *cleans up* previously
existing candidates - it planted an old release, ran a failing command, and
checked the old release was gone. That behaviour is now deliberately obsolete:
a run that could not produce a release has learned nothing about the release
that came before it, and destroying it was never anyone's idea of a safe
default. What is asserted here is the opposite, and the invariants are
structural rather than a catalogue of hostile strings.

    out/<board_id>/attempts/<attempt_id>/{work,build,diagnostics}
    out/<board_id>/published/<release_id>/
    out/<board_id>/latest.json
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
from tests import paths                                                    # noqa: E402
from pcbqa import cleanroom, core, layout                # noqa: E402
from pcbqa.core import ManifestError, Status             # noqa: E402
from pcbqa.layout import LayoutError, OutputLayout       # noqa: E402
from pcbqa.parallel import ENV_OUTPUT_ROOT               # noqa: E402

REVA = paths.REVA_MANIFEST
FIXTURE = paths.REVA_PROJECT
ATTRIBUTES = paths.ATTRIBUTES


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

    def __init__(self, board_id="lifecycle-board", mutate=None):
        self.work = tempfile.mkdtemp(prefix="pcbqa_life_")
        self.project = os.path.join(self.work, "project")
        shutil.copytree(FIXTURE, self.project)
        doc = json.load(open(REVA, encoding="utf-8"))
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

    def run(self, command="release"):
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = self.work
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                if command == "release":
                    code = run_cli.cmd_release(self.manifest_path)
                else:
                    code = run_cli.cmd_validate(self.manifest_path)[0]
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved
        return code, captured.getvalue()

    def attempts(self):
        directory = os.path.join(self.board, "attempts")
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))

    def attempt_path(self, attempt_id):
        return os.path.join(self.board, "attempts", attempt_id)

    def published(self):
        directory = os.path.join(self.board, "published")
        if not os.path.isdir(directory):
            return []
        return sorted(os.listdir(directory))

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


def _all_gates_pass(real):
    def patched(context, only=None):
        results = real(context, only=only)
        for result in results:
            if result.status != Status.NOT_APPLICABLE:
                result.passed(result.reason or "forced for this test")
        return results
    return patched


class _Base(unittest.TestCase):
    def _board(self, board_id="lifecycle-board", mutate=None):
        board = _Board(board_id, mutate)
        self.addCleanup(board.close)
        return board

    @contextlib.contextmanager
    def _gates_pass(self):
        real = core.run_all
        core.run_all = _all_gates_pass(real)
        try:
            yield
        finally:
            core.run_all = real

    def _publish_one(self, board):
        with self._gates_pass():
            code, output = board.run("release")
        self.assertEqual(code, 0, output[-2000:])
        return board.published()[0]


# ---------------------------------------------------------------------------
# 1 + 2 + 8: the manifest is validated before anything exists
# ---------------------------------------------------------------------------

class UntrustedManifestsNeverReachTheFilesystem(_Base):
    """One load path, and nothing is created or removed before it succeeds."""

    HOSTILE = [
        ("malformed json", '{ "board_id": "lifecycle-board", oops }'),
        ("json list", '["board_id", "lifecycle-board"]'),
        ("json scalar", '"lifecycle-board"'),
        ("no board_id", '{"schema_version": 2}'),
        ("traversal id", '{"schema_version": 2, "board_id": "../victim"}'),
        ("absolute id", '{"schema_version": 2, "board_id": "/tmp/victim"}'),
        ("separator id", '{"schema_version": 2, "board_id": "a/b"}'),
        ("backslash id", '{"schema_version": 2, "board_id": "a\\\\b"}'),
        ("dotdot id", '{"schema_version": 2, "board_id": ".."}'),
        ("empty id", '{"schema_version": 2, "board_id": ""}'),
        ("non-string id", '{"schema_version": 2, "board_id": 17}'),
        ("wrong schema", '{"schema_version": 99, "board_id": "lifecycle-board"}'),
    ]

    def test_every_hostile_manifest_is_refused_by_both_commands(self):
        for label, text in self.HOSTILE:
            for command in ("validate", "release"):
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
        derived = OutputLayout.for_manifest(manifest, board.work)
        self.assertEqual(manifest.board_id, "microphone_array_v2-revA")
        self.assertTrue(derived.contains(derived.board))
        self.assertEqual(os.path.dirname(derived.board), derived.root)

    def test_the_layout_refuses_to_leave_its_root(self):
        base = tempfile.mkdtemp(prefix="pcbqa_layout_")
        self.addCleanup(shutil.rmtree, base, True)
        for hostile in ("../victim", "..", "/etc", "a/b", "", ".", "x" * 200):
            with self.subTest(board_id=hostile):
                with self.assertRaises(LayoutError):
                    OutputLayout(hostile, base)
        good = OutputLayout("safe-board", base)
        self.assertFalse(good.contains(good.root))
        self.assertFalse(good.contains(os.path.dirname(good.root)))
        self.assertTrue(good.contains(good.attempts))

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
                text = open(path, encoding="utf-8").read()
                if "Manifest(" in text and "load_manifest" not in text:
                    offenders.append(os.path.relpath(path, HERE))
        self.assertFalse(offenders,
                         "modules constructing manifests outside the loader: "
                         + str(offenders))


# ---------------------------------------------------------------------------
# 3 + 4 + 7: a failure is confined to the attempt that caused it
# ---------------------------------------------------------------------------

class FailureIsConfinedToItsOwnAttempt(_Base):
    """Each stage that can fail, and what survives when it does."""

    def _inject(self, stage):
        """Patch one stage to fail; returns (undo, label)."""
        if stage == "generation":
            real = cleanroom.CleanRun.generate

            def broken(self):
                raise RuntimeError("injected generation failure")
            cleanroom.CleanRun.generate = broken
            return lambda: setattr(cleanroom.CleanRun, "generate", real)
        if stage == "packaging":
            real = cleanroom.CleanRun.package

            def broken(self):
                real(self)                       # the archive now exists
                raise RuntimeError("injected packaging failure")
            cleanroom.CleanRun.package = broken
            return lambda: setattr(cleanroom.CleanRun, "package", real)
        if stage == "promotion":
            real = layout.Attempt.publish

            def broken(self, release_id=None):
                raise RuntimeError("injected promotion failure")
            layout.Attempt.publish = broken
            return lambda: setattr(layout.Attempt, "publish", real)
        raise AssertionError("unknown stage " + stage)

    def _assert_stage_is_confined(self, stage):
        """One failing stage; everything else must be untouched.

        Split one stage per test rather than looping: each case runs two full
        clean-room releases, and as a single test they had to run in one
        worker, serially, for well over half an hour. Independently
        addressable tests are the unit this runner distributes.
        """
        board = self._board()
        release_id = self._publish_one(board)
        published_root = os.path.join(board.board, "published")
        published_before = digest_tree(published_root)
        latest_before = [e for e in digest_tree(board.board)
                         if e[0] == "latest.json"]
        self.assertTrue(latest_before, "the control release wrote no pointer")

        # A sibling attempt from some other run, and a bystander outside.
        sibling = os.path.join(board.board, "attempts", "sibling")
        os.makedirs(sibling)
        with open(os.path.join(sibling, "note.txt"), "w") as fh:
            fh.write("another run's work")
        sibling_before = digest_tree(sibling)
        bystander = os.path.join(board.work, "bystander")
        os.makedirs(bystander)
        with zipfile.ZipFile(os.path.join(bystander, "unrelated.zip"), "w") as zf:
            zf.writestr("x.gbr", "G04*")
        bystander_before = digest_tree(bystander)

        known = set(board.attempts())
        if stage == "validation":
            code, output = board.run("release")       # Rev A's real gates fail
        else:
            undo = self._inject(stage)
            try:
                if stage == "promotion":
                    with self._gates_pass():
                        code, output = board.run("release")
                else:
                    code, output = board.run("release")
            finally:
                undo()

        self.assertEqual(code, 1, output[-1500:])
        new_attempts = [a for a in board.attempts() if a not in known]
        self.assertEqual(len(new_attempts), 1, new_attempts)
        failed = board.attempt_path(new_attempts[0])

        self.assertEqual(layout.orderable_archives(failed), [],
                         "the failed attempt kept an orderable archive")
        self.assertFalse(os.path.exists(os.path.join(failed, "build")))
        self.assertEqual(digest_tree(published_root), published_before,
                         "a failed attempt changed a published release")
        self.assertEqual([e for e in digest_tree(board.board)
                          if e[0] == "latest.json"], latest_before,
                         "a failed attempt changed the latest pointer")
        self.assertEqual(digest_tree(sibling), sibling_before,
                         "a failed attempt touched a sibling attempt")
        self.assertEqual(digest_tree(bystander), bystander_before,
                         "a failed attempt touched something outside out/")
        self.assertIn(release_id, board.published())

    def test_a_generation_failure_is_confined(self):
        self._assert_stage_is_confined("generation")

    def test_a_validation_failure_is_confined(self):
        self._assert_stage_is_confined("validation")

    def test_a_packaging_failure_is_confined(self):
        self._assert_stage_is_confined("packaging")

    def test_a_promotion_failure_is_confined(self):
        self._assert_stage_is_confined("promotion")

    def test_a_failed_attempt_keeps_diagnostics_but_no_artifacts(self):
        board = self._board()
        code, _output = board.run("release")
        self.assertEqual(code, 1)
        failed = board.attempt_path(board.attempts()[-1])
        diagnostics = os.path.join(failed, "diagnostics")
        self.assertTrue(os.path.isfile(
            os.path.join(diagnostics, "DO_NOT_ORDER.txt")))
        self.assertEqual(layout.orderable_archives(diagnostics), [])
        self.assertEqual(layout.orderable_archives(failed), [])
        self.assertTrue(os.path.isfile(
            os.path.join(failed, layout.ATTEMPT_MARKER)),
            "an attempt must say plainly that it is not a release")

    def test_an_attempt_can_only_delete_inside_itself(self):
        base = tempfile.mkdtemp(prefix="pcbqa_own_")
        self.addCleanup(shutil.rmtree, base, True)
        derived = OutputLayout("some-board", base)
        attempt = derived.new_attempt()
        for outside in (derived.board, derived.attempts, derived.root,
                        os.path.dirname(base), base):
            self.assertFalse(attempt.owns(outside),
                             "attempt claimed ownership of " + outside)
        self.assertTrue(attempt.owns(attempt.build))
        self.assertTrue(attempt.owns(attempt.path))

    def test_the_broad_cleanup_machinery_is_gone(self):
        """The old lifecycle must not still be running alongside the new one."""
        for name in ("purge_managed_output", "sweep_output_tree",
                     "CANDIDATE_DIR_NAMES", "contained",
                     "UncontainedTarget"):
            self.assertFalse(hasattr(cleanroom, name),
                             "cleanroom still exports " + name)
        for name in ("managed_output_dir", "valid_board_id",
                     "BOARD_ID_PATTERN", "BOARD_ID_RE"):
            self.assertFalse(hasattr(run_cli, name),
                             "run.py still exports " + name)


# ---------------------------------------------------------------------------
# 5 + 6: publication happens once, after everything passed
# ---------------------------------------------------------------------------

class PublicationHappensOnlyOnCompleteSuccess(_Base):
    def test_a_successful_run_publishes_and_points_latest_at_it(self):
        board = self._board()
        release_id = self._publish_one(board)
        release = os.path.join(board.board, "published", release_id)

        self.assertTrue(os.path.isdir(release))
        archives = layout.orderable_archives(release)
        self.assertEqual(len(archives), 1, archives)
        for name in ("UNSEALED.txt", "validation.json", "clean_room.json",
                     "reports"):
            self.assertTrue(os.path.exists(os.path.join(release, name)), name)

        pointer = json.load(open(os.path.join(board.board, "latest.json"),
                                 encoding="utf-8"))
        self.assertEqual(pointer["release_id"], release_id)
        self.assertEqual(pointer["board_id"], board.board_id)
        self.assertFalse(pointer["sealed"])
        # The attempt that produced it kept no copy.
        attempt = board.attempt_path(board.attempts()[-1])
        self.assertFalse(os.path.exists(os.path.join(attempt, "build")))
        self.assertEqual(layout.orderable_archives(attempt), [])

    def test_a_second_success_publishes_a_new_release_and_moves_latest(self):
        board = self._board()
        first = self._publish_one(board)
        first_digest = digest_tree(os.path.join(board.board, "published", first))
        second = [r for r in self._publish_and_list(board) if r != first][0]

        self.assertNotEqual(first, second)
        self.assertEqual(sorted(board.published()), sorted([first, second]))
        self.assertEqual(
            digest_tree(os.path.join(board.board, "published", first)),
            first_digest, "publishing a new release altered the old one")
        pointer = json.load(open(os.path.join(board.board, "latest.json"),
                                 encoding="utf-8"))
        self.assertEqual(pointer["release_id"], second)

    def _publish_and_list(self, board):
        with self._gates_pass():
            code, output = board.run("release")
        self.assertEqual(code, 0, output[-2000:])
        return board.published()

    def test_publishing_never_replaces_an_existing_release(self):
        base = tempfile.mkdtemp(prefix="pcbqa_pub_")
        self.addCleanup(shutil.rmtree, base, True)
        derived = OutputLayout("some-board", base)
        attempt = derived.new_attempt()
        with open(os.path.join(attempt.build, "f.txt"), "w") as fh:
            fh.write("first")
        release_id, destination = attempt.publish("fixed-id")
        self.assertTrue(os.path.isdir(destination))

        second = derived.new_attempt()
        with open(os.path.join(second.build, "f.txt"), "w") as fh:
            fh.write("second")
        with self.assertRaises(LayoutError):
            second.publish("fixed-id")
        self.assertEqual(open(os.path.join(destination, "f.txt")).read(),
                         "first", "an existing release was overwritten")

    def test_latest_cannot_point_at_something_unpublished(self):
        base = tempfile.mkdtemp(prefix="pcbqa_ptr_")
        self.addCleanup(shutil.rmtree, base, True)
        derived = OutputLayout("some-board", base)
        with self.assertRaises(LayoutError):
            derived.write_latest("never-published")
        self.assertIsNone(derived.read_latest())


# ---------------------------------------------------------------------------
# validate is subject to the same containment as release
# ---------------------------------------------------------------------------

class ValidateOwnsAnAttemptToo(_Base):
    def test_validate_writes_only_into_its_own_attempt(self):
        board = self._board()
        code, output = board.run("validate")
        self.assertEqual(code, 1, "Rev A must still be rejected")
        attempts = board.attempts()
        self.assertEqual(len(attempts), 1)
        attempt = board.attempt_path(attempts[0])
        self.assertTrue(os.path.isfile(
            os.path.join(attempt, "validation.json")))
        self.assertEqual(layout.orderable_archives(board.out), [])
        # Nothing was written to the board root except the attempts tree.
        self.assertEqual(sorted(os.listdir(board.board)), ["attempts"])

    def test_two_validate_runs_do_not_share_a_directory(self):
        board = self._board()
        board.run("validate")
        board.run("validate")
        self.assertEqual(len(board.attempts()), 2,
                         "invocations must not reuse an attempt directory")

    def test_validate_does_not_disturb_a_published_release(self):
        board = self._board()
        release_id = self._publish_one(board)
        before = digest_tree(os.path.join(board.board, "published"))
        board.run("validate")
        self.assertEqual(digest_tree(os.path.join(board.board, "published")),
                         before)
        self.assertIn(release_id, board.published())


if __name__ == "__main__":
    unittest.main()
