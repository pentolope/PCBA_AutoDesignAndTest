"""The one place that turns a board identity into a filesystem path.

Every managed directory this tool creates or removes is derived here, from a
validated board id, beneath a single canonical root. Production code never
joins raw manifest data onto a path itself, and never hands an arbitrary
directory to a recursive delete: it asks for an attempt and is given one.

The layout::

    out/                                    canonical root, resolved once
      <board_id>/
        attempts/
          <attempt_id>/                     one invocation, owns everything here
            ATTEMPT_NOT_A_RELEASE.txt
            work/                           reports, clean-room copy, scratch
            build/                          the candidate, while it is still a candidate
            diagnostics/                    kept on failure; never an archive
        published/
          <release_id>/                     immutable once created
        latest.json                         pointer, replaced atomically

Two properties this shape exists to guarantee. An invocation can only delete
inside the attempt directory it created, so a failing run cannot touch a
sibling attempt or a published release. And publication is the rename of a
directory into a name that did not previously exist, so it never has to remove
an old release to install a new one - which on Windows could not be atomic
anyway.
"""

from __future__ import annotations

import datetime
import json
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

# Anything a fabricator could accept as an order.
ORDERABLE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".tgz", ".tar.gz",
                      ".tar.bz2", ".tar.xz", ".gz")

ATTEMPT_MARKER = "ATTEMPT_NOT_A_RELEASE.txt"
LATEST_POINTER = "latest.json"

MARKER_TEXT = """This directory is one verification attempt, not a release.

Nothing here has been published. A candidate under `build/` has not passed the
gates yet - if it had, it would have been moved to `../../published/` and this
directory would contain no archive at all. Do not send anything from here to a
fabricator.
"""


class LayoutError(Exception):
    """A path was requested that the layout will not produce."""


def valid_board_id(value):
    """True only for a name safe to use as a single path component."""
    if not isinstance(value, str) or not value or len(value) > BOARD_ID_MAX:
        return False
    if not BOARD_ID_RE.match(value):
        return False
    # The regex already excludes these; asserting them directly costs nothing
    # next to the cost of being wrong about the regex.
    if value in (os.curdir, os.pardir):
        return False
    if os.path.basename(value) != value:
        return False
    return not os.path.splitdrive(value)[0]


def orderable_archives(root):
    """Every archive-shaped file under `root`, recursively."""
    hits = []
    if not root or not os.path.isdir(root):
        return hits
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(ORDERABLE_SUFFIXES):
                hits.append(os.path.join(dirpath, name))
    return sorted(hits)


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ")


class OutputLayout:
    """Every managed path for one board, all provably inside the root."""

    def __init__(self, board_id, base):
        if not valid_board_id(board_id):
            raise LayoutError(
                "refusing to build an output path from board id {!r}: a board "
                "id must be a single conservative slug".format(board_id))
        self.board_id = board_id
        self.root = os.path.realpath(os.path.join(base, "out"))
        self.board = self._contain(os.path.join(self.root, board_id))
        self.attempts = self._contain(os.path.join(self.board, "attempts"))
        self.published = self._contain(os.path.join(self.board, "published"))
        self.latest_pointer = self._contain(
            os.path.join(self.board, LATEST_POINTER))

    @classmethod
    def for_manifest(cls, manifest, base):
        """The only supported way to get a layout. Requires a loaded manifest."""
        return cls(manifest.board_id, base)

    # -- containment -------------------------------------------------------
    def _contain(self, path):
        resolved = os.path.realpath(path)
        if resolved == self.root:
            raise LayoutError("refusing to treat the output root as a target")
        try:
            common = os.path.commonpath([resolved, self.root])
        except ValueError:                  # different drives on Windows
            raise LayoutError(
                "{!r} is not on the same drive as {!r}".format(path, self.root))
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

    # -- attempts ----------------------------------------------------------
    def new_attempt(self):
        """Create and return a directory this invocation exclusively owns."""
        os.makedirs(self.attempts, exist_ok=True)
        for _ in range(64):
            attempt_id = "{}-{}".format(_stamp(), secrets.token_hex(4))
            path = self._contain(os.path.join(self.attempts, attempt_id))
            try:
                os.makedirs(path)
            except FileExistsError:
                continue
            return Attempt(self, attempt_id, path)
        raise LayoutError("could not create a unique attempt directory")

    def existing_attempts(self):
        if not os.path.isdir(self.attempts):
            return []
        return sorted(os.listdir(self.attempts))

    # -- publication -------------------------------------------------------
    def new_release_id(self):
        return "{}-{}".format(_stamp(), secrets.token_hex(3))

    def release_dir(self, release_id):
        if not valid_board_id(release_id):
            raise LayoutError("unsafe release id {!r}".format(release_id))
        return self._contain(os.path.join(self.published, release_id))

    def published_releases(self):
        if not os.path.isdir(self.published):
            return []
        return sorted(d for d in os.listdir(self.published)
                      if os.path.isdir(os.path.join(self.published, d)))

    def read_latest(self):
        try:
            with open(self.latest_pointer, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def write_latest(self, release_id, extra=None):
        """Replace the pointer atomically, after the release is in place.

        Written to a temporary name in the same directory and moved over the
        old one with `os.replace`, which is atomic for files on Windows as
        well as POSIX. A reader sees the old pointer or the new one.
        """
        if not os.path.isdir(self.release_dir(release_id)):
            raise LayoutError(
                "refusing to point `latest` at {!r}, which is not a published "
                "release".format(release_id))
        payload = {"release_id": release_id,
                   "path": os.path.relpath(self.release_dir(release_id),
                                           self.board).replace("\\", "/"),
                   "updated_utc": datetime.datetime.now(
                       datetime.timezone.utc).isoformat()}
        if extra:
            payload.update(extra)
        temporary = self.latest_pointer + ".incoming"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, self.latest_pointer)
        return payload


class Attempt:
    """One invocation's private directory, and the only thing it may delete."""

    def __init__(self, layout, attempt_id, path):
        self.layout = layout
        self.id = attempt_id
        self.path = path
        self.work = self._sub("work")
        self.build = self._sub("build")
        self.diagnostics = self._sub("diagnostics")
        self.published_as = None
        with open(os.path.join(self.path, ATTEMPT_MARKER), "w",
                  encoding="utf-8") as fh:
            fh.write(MARKER_TEXT)

    def _sub(self, name):
        path = self.layout._contain(os.path.join(self.path, name))
        os.makedirs(path, exist_ok=True)
        return path

    # -- ownership ---------------------------------------------------------
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

    def discard_build(self):
        """Remove this attempt's candidate. Nothing else, ever."""
        removed = []
        if os.path.isdir(self.build):
            shutil.rmtree(self.build, ignore_errors=True)
            removed.append(self.build)
        # Belt and braces: no archive may remain anywhere in this attempt.
        for path in orderable_archives(self.path):
            if self.owns(path):
                try:
                    os.unlink(path)
                    removed.append(path)
                except OSError:
                    pass
        return removed

    def discard(self):
        """Remove the whole attempt directory. Only ever its own."""
        if os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)

    # -- publication -------------------------------------------------------
    def publish(self, release_id=None):
        """Move the finished build into `published/<release_id>`.

        A rename into a name that does not exist yet. There is no
        delete-the-old-one step, so nothing is destroyed if this fails, and
        nothing has to be atomically replaced - which is the operation Windows
        will not give us for a non-empty directory.
        """
        if not os.path.isdir(self.build):
            raise LayoutError("this attempt has no build to publish")
        release_id = release_id or self.layout.new_release_id()
        destination = self.layout.release_dir(release_id)
        if os.path.exists(destination):
            raise LayoutError(
                "release {!r} already exists; published releases are "
                "immutable".format(release_id))
        os.makedirs(self.layout.published, exist_ok=True)
        os.rename(self.build, destination)
        self.published_as = release_id
        return release_id, destination
