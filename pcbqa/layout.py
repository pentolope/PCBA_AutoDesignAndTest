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
        silently interleaved. A hold left by a dead process on this host is
        broken and taken over; one held by a live process, or by another
        host, stands.
        """
        return Hold(self, purpose)


class Hold:
    """Context manager for Workspace.hold. Advisory, single-file, atomic."""

    def __init__(self, workspace, purpose):
        self.workspace = workspace
        self.purpose = purpose
        self.path = os.path.join(workspace.board, ".hold")
        self._fd = None

    def _claim(self):
        import json
        import socket
        os.makedirs(self.workspace.board, exist_ok=True)
        record = {"pid": os.getpid(), "host": socket.gethostname(),
                  "purpose": self.purpose, "created_utc": _stamp()}
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        self._fd = True

    def _holder(self):
        import json
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _stale(self, holder):
        import socket
        pid = holder.get("pid")
        if not isinstance(pid, int):
            # An unreadable hold cannot name a live owner; treat it as
            # abandoned rather than wedging every future run behind it.
            return True
        if holder.get("host") != socket.gethostname():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        return False

    def __enter__(self):
        try:
            self._claim()
            return self
        except FileExistsError:
            holder = self._holder()
            if self._stale(holder):
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
                self._claim()          # a second racer gets FileExistsError
                return self
            raise LayoutError(
                "the workspace for this board is held by pid {} on {} "
                "({!r} since {}); a second writer would interleave with it - "
                "remove {} only if that process is truly gone".format(
                    holder.get("pid"), holder.get("host"),
                    holder.get("purpose"), holder.get("created_utc"),
                    self.path))

    def __exit__(self, exc_type, exc, tb):
        if self._fd:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
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
