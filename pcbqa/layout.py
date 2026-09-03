"""The one place that turns a board identity into a filesystem path.

Every managed directory this tool creates or removes is derived here, from a
validated board id, beneath a single canonical root::

    out/<board_id>/<run_id>/        scratch for one invocation, and all it owns

Nothing durable lives here. Fabrication artifacts are committed files in the
project, and a release is a Git tag; this tree is working space that any run
may delete.
"""

from __future__ import annotations

import datetime
import os
import re
import secrets
import shutil

# A board id names one directory. It comes from a manifest, and manifests are
# input. A conservative slug admits every board id in this repository and no
# path syntax at all: no separator, no drive letter, no `..`, no leading dot,
# no whitespace, no NUL.
BOARD_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
BOARD_ID_MAX = 100


class LayoutError(Exception):
    """A path was requested that the layout will not produce."""


def valid_board_id(value):
    """True only for a name safe to use as a single path component."""
    if not isinstance(value, str) or not value or len(value) > BOARD_ID_MAX:
        return False
    if not BOARD_ID_RE.match(value):
        return False
    if value in (os.curdir, os.pardir):
        return False
    if os.path.basename(value) != value:
        return False
    return not os.path.splitdrive(value)[0]


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


class Workspace:
    """Scratch space for one board, all provably inside the root."""

    def __init__(self, board_id, base):
        if not valid_board_id(board_id):
            raise LayoutError(
                "refusing to build an output path from board id {!r}: a board "
                "id must be a single conservative slug".format(board_id))
        self.board_id = board_id
        self.root = os.path.realpath(os.path.join(base, "out"))
        self.board = self._contain(os.path.join(self.root, board_id))

    @classmethod
    def for_manifest(cls, manifest, base):
        """The only supported way to get a workspace. Requires a manifest."""
        return cls(manifest.board_id, base)

    def _contain(self, path):
        resolved = os.path.realpath(path)
        if resolved == self.root:
            raise LayoutError("refusing to treat the output root as a target")
        try:
            common = os.path.commonpath([resolved, self.root])
        except ValueError:                  # no common root to speak of
            raise LayoutError(
                "{!r} shares no common root with {!r}".format(path, self.root))
        if common != self.root:
            raise LayoutError(
                "{!r} resolves outside the managed output root {!r}".format(
                    path, self.root))
        return resolved

    def contains(self, path):
        try:
            self._contain(path)
        except LayoutError:
            return False
        return True

    def new_run(self):
        """Create and return a directory this invocation exclusively owns."""
        os.makedirs(self.board, exist_ok=True)
        for _ in range(64):
            run_id = "{}-{}".format(_stamp(), secrets.token_hex(4))
            path = self._contain(os.path.join(self.board, run_id))
            try:
                os.makedirs(path)
            except FileExistsError:
                continue
            return Run(self, run_id, path)
        raise LayoutError("could not create a unique run directory")

    def existing_runs(self):
        if not os.path.isdir(self.board):
            return []
        return sorted(d for d in os.listdir(self.board)
                      if os.path.isdir(os.path.join(self.board, d)))

    def hold(self, purpose):
        """An advisory exclusive hold on this board's workspace.

        Two processes generating into one board's tree at the same time leave
        it holding a mixture neither of them produced. Tree-writing commands
        take this hold; a second holder is refused by name rather than
        silently interleaved. The lock is a kernel `flock` held for the
        hold's whole duration, so a crashed holder's claim is released with
        its process by the kernel itself - there is no staleness heuristic
        and nothing for a human to remove.
        """
        return Hold(os.path.join(self.board, ".hold"), purpose)


def tree_hold(directory, board_id, purpose):
    """The hold for tree-writing commands, anchored to the tree itself.

    The lock file lives in the project directory being written, not under
    the relocatable output base: two invocations that differ only in
    `PCBQA_OUTPUT_ROOT` write the same project tree, so they must contend
    for the same lock. The board id names the lock so two boards sharing
    one directory do not exclude each other.
    """
    if not valid_board_id(board_id):
        raise LayoutError(
            "refusing to build a hold path from board id {!r}".format(
                board_id))
    return Hold(os.path.join(os.path.realpath(directory),
                             ".pcbqa-hold-" + board_id), purpose)


class Hold:
    """Advisory exclusive hold on a directory tree, via a kernel lock.

    `flock` on a lock file that stays open for the hold's duration. The
    kernel releases a dead holder's lock with its process, so no liveness
    guessing and no takeover protocol exist to race. The file's CONTENT is
    only the refusal message's material - who holds it and why; exclusion
    never depends on it, so a truncated, garbage or empty lock file changes
    nothing. The file is unlinked on release; a leftover one (from a crash)
    is inert FOR LOCKING and the next claimant reuses and then removes it -
    but until that next claim it sits on disk, so a consumer project should
    ignore `.pcbqa-hold-*` the way it ignores its output scratch, or a
    crash dirties the release gate's clean-tree check.
    """

    def __init__(self, path, purpose):
        self.path = path
        self.purpose = purpose
        self._fd = None

    def _record(self):
        import socket
        return {"pid": os.getpid(), "host": socket.gethostname(),
                "purpose": self.purpose, "created_utc": _stamp()}

    @staticmethod
    def _read(fd):
        import json
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return json.loads(os.read(fd, 65536).decode("utf-8"))
        except (OSError, ValueError):
            return {}

    def __enter__(self):
        import fcntl
        import json
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        except OSError as exc:
            raise LayoutError(
                "the hold file's directory cannot be created ({}): "
                "{}".format(self.path, exc)) from exc
        for _ in range(64):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
            except OSError as exc:
                # A project root the caller cannot write is a refusal in the
                # caller's terms, never a traceback out of the CLI.
                raise LayoutError(
                    "the hold file cannot be created ({}): {}".format(
                        self.path, exc)) from exc
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = self._read(fd)
                os.close(fd)
                if holder:
                    raise LayoutError(
                        "this board's tree is held by pid {} on {} ({!r} "
                        "since {}); a second writer would interleave with "
                        "it - rerun when it finishes (a crashed holder's "
                        "lock is released by the kernel, so a standing "
                        "refusal means the process is live)".format(
                            holder.get("pid"), holder.get("host"),
                            holder.get("purpose"),
                            holder.get("created_utc"))) from None
                raise LayoutError(
                    "this board's tree is held by another process (its "
                    "record is not yet written); rerun when it "
                    "finishes") from None
            # A releasing holder unlinks the file while we may already have
            # it open: a lock on that dangling inode excludes nobody who
            # opens the path afresh, so the claim only stands if the path
            # still names the inode we locked.
            try:
                current = os.stat(self.path)
            except FileNotFoundError:
                os.close(fd)
                continue
            mine = os.fstat(fd)
            if (mine.st_dev, mine.st_ino) != (current.st_dev,
                                              current.st_ino):
                os.close(fd)
                continue
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(self._record()).encode("utf-8"))
            self._fd = fd
            return self
        raise LayoutError(
            "could not settle a claim on {}: the lock file kept being "
            "replaced underneath the claim".format(self.path))

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            # Unlink only while still holding the lock, and only if the
            # path still names our inode - never strip a file some other
            # actor put there.
            try:
                current = os.stat(self.path)
                mine = os.fstat(self._fd)
                if (mine.st_dev, mine.st_ino) == (current.st_dev,
                                                  current.st_ino):
                    os.unlink(self.path)
            except OSError:
                pass
            os.close(self._fd)               # releases the flock
            self._fd = None
        return False


class Run:
    """One invocation's private directory, and the only thing it may delete."""

    def __init__(self, workspace, run_id, path):
        self.workspace = workspace
        self.id = run_id
        self.path = path
        self.work = self._sub("work")
        self.build = self._sub("build")

    def _sub(self, name):
        path = self.workspace._contain(os.path.join(self.path, name))
        os.makedirs(path, exist_ok=True)
        return path

    def owns(self, path):
        if not path:
            return False
        resolved = os.path.realpath(path)
        mine = os.path.realpath(self.path)
        if resolved == mine:
            return True
        try:
            return os.path.commonpath([resolved, mine]) == mine
        except ValueError:
            return False

    def discard(self):
        """Remove the whole run directory. Only ever its own."""
        if os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)
