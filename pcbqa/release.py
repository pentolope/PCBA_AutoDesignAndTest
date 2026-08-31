"""Release readiness: what Git must be able to say before a tag is created.

A release is a Git tag naming one commit whose committed fabrication artifacts
passed the project's release policy. Git provides historical coherence; this
module provides the engineering preconditions Git does not check by itself -
that the tree really is the commit, that the submodule pins really are the code
that ran, and that the artifacts being released are tracked files rather than
build residue.
"""

from __future__ import annotations

import os
import subprocess

from . import artifacts


class GitError(Exception):
    """Git could not answer, so nothing about the tree can be asserted."""


def git(root, *args, check=True):
    proc = subprocess.run(("git", "-C", root) + args,
                          capture_output=True, text=True, timeout=300)
    if check and proc.returncode != 0:
        raise GitError("git {} failed in {}: {}".format(
            " ".join(args), root, (proc.stderr or "").strip()[:200]))
    return proc


def repository_root(path):
    return git(path, "rev-parse", "--show-toplevel").stdout.strip()


def head_commit(root):
    return git(root, "rev-parse", "HEAD").stdout.strip()


def worktree_entries(root):
    """Porcelain status lines. Empty means clean, including untracked files."""
    out = git(root, "status", "--porcelain", "--untracked-files=all").stdout
    return [line for line in out.splitlines() if line.strip()]


def submodules(root):
    """One record per submodule, recursively, with why it is not in sync."""
    out = git(root, "submodule", "status", "--recursive").stdout
    records = []
    for line in out.splitlines():
        if not line.strip():
            continue
        marker, rest = line[0], line[1:].strip()
        parts = rest.split(" ", 2)
        record = {"sha": parts[0], "path": parts[1] if len(parts) > 1 else "",
                  "in_sync": marker == " "}
        if marker == "+":
            record["issue"] = ("the checked-out commit is not the one the "
                               "superproject records")
        elif marker == "-":
            record["issue"] = "is not initialised"
        elif marker == "U":
            record["issue"] = "has unmerged conflicts"
        records.append(record)
    return records


def tracked(root, paths):
    """Which of `paths` Git tracks. Absolute paths; result keyed the same."""
    if not paths:
        return {}
    proc = git(root, "ls-files", "-z", "--", *paths, check=False)
    if proc.returncode != 0:
        return {path: False for path in paths}
    known = {os.path.realpath(os.path.join(root, name))
             for name in proc.stdout.split("\0") if name}
    return {path: os.path.realpath(path) in known for path in paths}


def required_evidence(manifest):
    return [manifest.resolve(name)
            for name in manifest.get("release_profile.required_evidence", [])]


def readiness(manifest):
    """(problems, facts) for the Git preconditions of a release tag."""
    problems, facts = [], {}
    project = manifest.resolve(".")
    try:
        root = repository_root(project)
        facts["repository"] = root
        facts["commit"] = head_commit(root)
    except GitError as exc:
        return [{"issue": str(exc)}], facts

    dirty = worktree_entries(root)
    facts["worktree_entries"] = len(dirty)
    if dirty:
        # One condition, not one per file. A tree with fifty edits in it is
        # one fact about the tree, and listing it fifty times buries the
        # engineering findings underneath it.
        problems.append({
            "file": "the working tree",
            "issue": "{} path(s) differ from HEAD, so the commit being tagged "
                     "is not what is on disk".format(len(dirty)),
            "paths": [line[3:] for line in dirty[:12]]})

    records = submodules(root)
    facts["submodules"] = len(records)
    facts["submodules_in_sync"] = sum(1 for r in records if r["in_sync"])
    for record in records:
        if not record["in_sync"]:
            problems.append({"file": record["path"], "issue": record["issue"],
                             "checked_out": record["sha"][:12]})

    declared = artifacts.paths(manifest)
    wanted = [p for role, p in sorted(declared.items())
              if role not in ("gerber_dir", "reports_dir")]
    wanted += artifacts.generated_files(manifest)
    wanted = sorted(set(wanted))
    facts["release_files"] = len(wanted)
    present = tracked(root, wanted)
    missing = [os.path.relpath(p, root) for p in wanted
               if not os.path.isfile(p)]
    untracked = [os.path.relpath(p, root) for p in wanted
                 if os.path.isfile(p) and not present.get(p)]
    if missing:
        problems.append({"file": "release artifacts",
                         "issue": "{} declared release artifact(s) are "
                                  "missing".format(len(missing)),
                         "paths": missing[:12]})
    if untracked:
        problems.append({"file": "release artifacts",
                         "issue": "{} release artifact(s) are not tracked by "
                                  "Git, so the tag would not carry "
                                  "them".format(len(untracked)),
                         "paths": untracked[:12]})

    evidence = required_evidence(manifest)
    facts["required_evidence"] = len(evidence)
    present = tracked(root, evidence)
    for path in evidence:
        rel = os.path.relpath(path, root)
        if not os.path.isfile(path):
            problems.append({"file": rel,
                             "issue": "required release evidence is absent"})
        elif not present.get(path):
            problems.append({"file": rel,
                             "issue": "required release evidence is not "
                                      "tracked by Git"})
    return problems, facts
