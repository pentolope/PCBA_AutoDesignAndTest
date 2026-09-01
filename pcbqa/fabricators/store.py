"""The committed JLCPCB catalog, and the raw evidence behind it.

    <root>/catalog/approved.json   what design work may trust
    <root>/catalog/evidence/       the raw source bytes behind it

The two paths form one exact committed set. Git is the history and a commit is
the approval. A refresh acquires into scratch, parses, and shows what changed;
after review the catalog and evidence directory are replaced together.

Raw evidence is load-bearing. A snapshot's recorded ``sha256_raw`` digests are
re-verified against the evidence files every time it is loaded; a snapshot
whose evidence is missing or altered refuses to load at all, because "the
values survive but the proof is gone" must not be indistinguishable from a
fully auditable state.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from . import model

SNAPSHOT_SCHEMA_VERSION = 3

#: Snapshot schema versions this loader understands. Anything else refuses:
#: reading half of an unknown format is how provenance quietly rots.
#: Version 3 added `declared_source_ids` - the adapter's required source
#: set at acquisition time - so completeness is checkable without the
#: store knowing any fabricator's names.
KNOWN_SNAPSHOT_SCHEMAS = (1, 2, 3)

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_PARSE_FAILED = "parse-failed"

class StoreError(Exception):
    """The stored state cannot be used as asked. Always blocks."""


def verify_evidence(snapshot, directory):
    """The exact recorded raw-evidence inventory, checked against disk.

    Returns a list of problem strings, empty when the whole chain holds.
    The check is by content, not by name: a wrong file under the right
    name and a right file that has since been altered both surface. Files
    not named by the snapshot also surface: the committed evidence directory
    is one exact set, not an archive of earlier refreshes.
    """
    problems = []
    expected = set()
    for source in snapshot.get("sources", []):
        digest = source.get("sha256_raw")
        if not digest:
            if snapshot.get("outcome") == OUTCOME_COMPLETE:
                problems.append(
                    "source {!r} records no raw digest although the "
                    "acquisition claims to be complete".format(
                        source.get("id")))
            continue
        name = "{}-{}.raw".format(source.get("id"), digest[:12])
        if os.path.basename(name) != name or os.path.isabs(name):
            problems.append(
                "source {!r} produces an unsafe evidence filename".format(
                    source.get("id")))
            continue
        expected.add(name)
        path = os.path.join(directory, name)
        if os.path.islink(path) or not os.path.isfile(path):
            problems.append(
                "evidence file {} is missing; the snapshot's values "
                "survive but their proof does not".format(name))
            continue
        with open(path, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if actual != digest:
            problems.append(
                "evidence file {} hashes to {}.. where the snapshot "
                "recorded {}..; the bytes on disk are not the bytes the "
                "snapshot was parsed from".format(name, actual[:12],
                                                 digest[:12]))
    try:
        entries = set(os.listdir(directory))
    except FileNotFoundError:
        entries = set()
    except OSError as exc:
        problems.append("evidence directory cannot be read: {}".format(exc))
        entries = set()
    for name in sorted(entries - expected):
        problems.append(
            "unreferenced evidence entry {} is committed; the catalog must "
            "carry exactly the evidence named by approved.json".format(name))
    return problems


class CatalogStore:
    """The committed catalog under one root. Read-only."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.approved_path = os.path.join(self.root, "catalog",
                                          "approved.json")
        self.approved_evidence = os.path.join(self.root, "catalog",
                                              "evidence")

    def _load_snapshot(self, path, kind, evidence_dir):
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError) as exc:
            raise StoreError(
                "the {} snapshot at {} cannot be read: {}. It is left in "
                "place for inspection; nothing here repairs evidence".format(
                    kind, path, exc))
        if snapshot.get("schema_version") not in KNOWN_SNAPSHOT_SCHEMAS:
            raise StoreError(
                "the {} snapshot declares schema_version {!r}, which this "
                "code does not understand ({}); refusing to half-read a "
                "file written under unknown rules".format(
                    kind, snapshot.get("schema_version"),
                    ", ".join(str(v) for v in KNOWN_SNAPSHOT_SCHEMAS)))
        for field in ("fabricator", "retrieved_utc", "parser", "sources"):
            if field not in snapshot:
                raise StoreError(
                    "the {} snapshot at {} carries no {!r}; a snapshot "
                    "missing its provenance cannot be trusted".format(
                        kind, path, field))
        if snapshot["fabricator"] != model.FABRICATOR:
            raise StoreError(
                "the {} snapshot describes {!r}, not {!r}".format(
                    kind, snapshot["fabricator"], model.FABRICATOR))
        if "outcome" not in snapshot:
            # Schema 1 recorded a boolean; read it as the outcome it meant.
            snapshot["outcome"] = (OUTCOME_COMPLETE
                                   if snapshot.get("complete")
                                   else OUTCOME_INCOMPLETE)
        if snapshot["outcome"] not in (OUTCOME_COMPLETE, OUTCOME_INCOMPLETE,
                                       OUTCOME_PARSE_FAILED):
            raise StoreError(
                "the {} snapshot records outcome {!r}, which is not an "
                "outcome this code knows".format(kind, snapshot["outcome"]))
        source_ids = [s.get("id") for s in snapshot["sources"]]
        if len(source_ids) != len(set(source_ids)):
            raise StoreError(
                "the {} snapshot lists a source id twice; a duplicated "
                "identity means one evidence entry silently shadows "
                "another".format(kind))
        declared = snapshot.get("declared_source_ids")
        if declared is not None and snapshot.get("outcome") == \
                OUTCOME_COMPLETE and sorted(declared) != sorted(source_ids):
            raise StoreError(
                "the {} snapshot claims a complete acquisition of {} but "
                "its evidence list carries {}; part of the claimed evidence "
                "universe has vanished (or grown), and a complete snapshot "
                "with a mutilated source set cannot be trusted".format(
                    kind, sorted(declared), sorted(source_ids)))
        if snapshot["outcome"] != OUTCOME_COMPLETE:
            # A committed catalog is trusted by design work, so an incomplete
            # acquisition must refuse on the load path.
            raise StoreError(
                "the {} snapshot records outcome {!r}; only a complete "
                "acquisition may be a committed catalog, because an "
                "incomplete one carries no values to trust".format(
                    kind, snapshot["outcome"]))
        normalized = snapshot.get("normalized")
        if snapshot["outcome"] == OUTCOME_COMPLETE:
            if normalized is None or not snapshot.get("normalized_sha256"):
                raise StoreError(
                    "the {} snapshot claims a complete acquisition but "
                    "carries no normalized catalog or digest".format(kind))
            recomputed = model.normalized_digest(normalized)
            if recomputed != snapshot["normalized_sha256"]:
                raise StoreError(
                    "the {} snapshot's normalized data does not match its "
                    "own digest ({}.. recorded, {}.. recomputed): the file "
                    "has been altered or corrupted since it was written, "
                    "and a corrupt snapshot cannot be trusted".format(kind,
                                      snapshot["normalized_sha256"][:12],
                                      recomputed[:12]))
            model.validate_catalog(normalized)
            referenced = set()
            for section in ("capabilities", "materials", "stackups"):
                for record in normalized.get(section, {}).values():
                    referenced.add(record.get("source"))
            unbacked = sorted(referenced - set(source_ids))
            if unbacked:
                raise StoreError(
                    "the {} snapshot's normalized records cite source(s) "
                    "{} that its evidence list does not carry; a value "
                    "whose claimed source has vanished from the snapshot "
                    "is a value nobody can audit".format(kind, unbacked))
        problems = verify_evidence(snapshot, evidence_dir)
        if problems:
            raise StoreError(
                "the {} snapshot's raw-evidence chain is broken: {}. "
                "Values without their evidence are indistinguishable from "
                "values nobody can audit, so the snapshot refuses to "
                "load".format(kind, "; ".join(problems)))
        return snapshot

    def approved(self):
        """The approved snapshot, or None when no baseline exists yet."""
        return self._load_snapshot(self.approved_path, "approved",
                                   self.approved_evidence)


def write_catalog(root, snapshot, raw_sources):
    """Lay a snapshot out in the committed layout, evidence and all.

    A plain writer: what makes the result trusted is a person reading it and
    committing it. The evidence directory is replaced as an exact inventory;
    obsolete evidence from an earlier write is removed.
    """
    catalog = os.path.join(root, "catalog")
    evidence = os.path.join(catalog, "evidence")
    os.makedirs(evidence, exist_ok=True)
    bodies = {}
    for source in snapshot.get("sources", []):
        digest = source.get("sha256_raw")
        body = raw_sources.get(source["id"])
        if not digest:
            continue
        if body is None:
            raise StoreError(
                "source {!r} records raw evidence {}.. but the adoption "
                "input carries no bytes for it".format(source["id"],
                                                       digest[:12]))
        actual = hashlib.sha256(body).hexdigest()
        if actual != digest:
            raise StoreError(
                "source {!r} records raw evidence {}.. but the adoption "
                "input hashes to {}..".format(source["id"], digest[:12],
                                              actual[:12]))
        name = "{}-{}.raw".format(source["id"], digest[:12])
        if os.path.basename(name) != name or os.path.isabs(name):
            raise StoreError(
                "source {!r} produces an unsafe evidence filename".format(
                    source["id"]))
        bodies[name] = body

    for name in os.listdir(evidence):
        path = os.path.join(evidence, name)
        if name in bodies:
            if os.path.islink(path):
                os.unlink(path)
            elif not os.path.isfile(path):
                raise StoreError(
                    "cannot replace evidence file {} because that path is "
                    "not a regular file".format(path))
            continue
        if os.path.islink(path) or os.path.isfile(path):
            os.unlink(path)
            continue
        raise StoreError(
            "cannot replace the evidence inventory because unexpected entry "
            "{} is not a regular file".format(path))

    for name, body in bodies.items():
        with open(os.path.join(evidence, name), "wb") as handle:
            handle.write(body)
    # The raw bytes live in evidence/, never inside the JSON.
    document = {k: v for k, v in snapshot.items() if k != "raw"}
    path = os.path.join(catalog, "approved.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
