"""The committed fabrication artifact set, as a board declares it.

A release is a Git tag over a commit that already contains these files. This
module is the single vocabulary for which files those are, so the builder that
writes them, the gate that binds them and the release check that requires them
committed cannot disagree about the set.
"""

from __future__ import annotations

import glob
import os
import re

#: Roles that name exactly one file.
FILE_ROLES = ("bom", "cpl", "archive", "fabrication_manifest",
              "validation_report")

#: What `fabrication.json` must record about a build for it to be provenance.
REQUIRED_PROVENANCE = ("schema_version", "board_id", "constraint_version",
                       "source_closure_sha256", "tools", "commands",
                       "artifacts")

FABRICATION_SCHEMA_VERSION = 1


def leaf(path):
    """The last segment of a recorded path, whichever separator wrote it."""
    return re.split(r"[\\/]", str(path or ""))[-1]


def paths(manifest):
    """Every declared release location, resolved. Absent roles are omitted."""
    out = {}
    for role, key in (("bom", "artifacts.bom"),
                      ("cpl", "artifacts.cpl"),
                      ("archive", "archive.zip"),
                      ("gerber_dir", "artifacts.gerber_dir"),
                      ("reports_dir", "artifacts.reports_dir"),
                      ("fabrication_manifest", "artifacts.fabrication_manifest"),
                      ("validation_report", "artifacts.validation_report")):
        if manifest.has(key):
            out[role] = manifest.resolve(manifest.get(key))
    return out


def report_files(manifest):
    """The check reports the build produces, by the steps that produce them.

    A required step with no generation output raises: silently dropping it
    would leave a declared requirement with no trace in any result - a
    manifest key that can mean nothing (the caller is a gate, so this
    surfaces as a fail-closed ERROR with the step named).
    """
    from .core import ManifestError

    out = []
    for step in manifest.get("reports.required_steps", []):
        key = "release_generation.{}.output".format(step)
        if not manifest.has(key):
            raise ManifestError(
                "reports.required_steps names {!r}, but {} does not exist; "
                "a required report no build step generates cannot be "
                "required".format(step, key))
        out.append(manifest.get(key))
    return out


def generated_files(manifest):
    """Every file a build writes into the tree, absolute, sorted.

    The fabrication manifest and the validation report are excluded: the first
    is the record of this set and cannot contain its own digest, the second is
    written by a later command.
    """
    found = []
    declared = paths(manifest)
    for role in ("bom", "cpl", "archive"):
        if role in declared:
            found.append(declared[role])
    for role in ("gerber_dir", "reports_dir"):
        directory = declared.get(role)
        if not directory or not os.path.isdir(directory):
            continue
        for path in glob.glob(os.path.join(directory, "*")):
            if os.path.isfile(path):
                found.append(path)
    return sorted(os.path.abspath(p) for p in found)


def record_key(manifest, path):
    """How `fabrication.json` names a file: relative to its own directory."""
    base = os.path.dirname(paths(manifest)["fabrication_manifest"])
    return os.path.relpath(path, base).replace("\\", "/")
