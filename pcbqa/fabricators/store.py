"""Approved and observed fabricator knowledge, kept apart on purpose.

Two directories, two meanings, one door between them:

    <root>/catalog/approved.json      what design work may trust
    <root>/catalog/evidence/          the raw source bytes behind it
    <root>/catalog/promotions.json    the audit log of every promotion
    <root>/observed/latest.json       the newest acquisition; trusted for
    <root>/observed/evidence/         nothing until promoted

A refresh writes only under ``observed/``. Promotion is the only code path
that writes ``approved.json``, it is explicit, it re-verifies that what it is
promoting is what the caller reviewed, and it records what replaced what. A
failed or malformed fetch therefore cannot damage the approved state by any
route: it either fails before writing, or writes an observed snapshot that
sits there until someone looks at it.

Writes are atomic - a temp file in the same directory, then ``os.replace`` -
so an interrupted write leaves either the old file or the new one, never a
truncated hybrid.

Freshness is a property of the *evidence*, not of the approval: an approved
snapshot is as old as the day its sources were fetched, however recently it
was promoted. The default policy treats fabrication-process data as stale
after 30 days; staleness is reported, never silently acted on, and never
blocks offline work by itself.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil

from . import model

#: How long fabrication-process knowledge is presumed current. Process pages
#: move slowly; a month-old catalog is worth re-checking, not worth blocking.
FRESHNESS_DAYS_DEFAULT = 30

SNAPSHOT_SCHEMA_VERSION = 1


class StoreError(Exception):
    """The stored state cannot be used as asked. Always blocks."""


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(text):
    return datetime.datetime.fromisoformat(text)


def _atomic_write_json(path, document):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _atomic_write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


class CatalogStore:
    """One fabricator's approved and observed knowledge under one root."""

    def __init__(self, root, fabricator):
        self.root = os.path.abspath(root)
        self.fabricator = fabricator
        self.approved_path = os.path.join(self.root, "catalog",
                                          "approved.json")
        self.approved_evidence = os.path.join(self.root, "catalog",
                                              "evidence")
        self.promotions_path = os.path.join(self.root, "catalog",
                                            "promotions.json")
        self.observed_path = os.path.join(self.root, "observed",
                                          "latest.json")
        self.observed_evidence = os.path.join(self.root, "observed",
                                              "evidence")

    # -- loading -----------------------------------------------------------
    def _load_snapshot(self, path, kind):
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
        for field in ("schema_version", "fabricator", "retrieved_utc",
                      "parser", "sources", "normalized_sha256", "normalized"):
            if field not in snapshot:
                raise StoreError(
                    "the {} snapshot at {} carries no {!r}; a snapshot "
                    "missing its provenance cannot be trusted".format(
                        kind, path, field))
        if snapshot["fabricator"] != self.fabricator:
            raise StoreError(
                "the {} snapshot describes {!r}, not {!r}".format(
                    kind, snapshot["fabricator"], self.fabricator))
        recomputed = model.normalized_digest(snapshot["normalized"])
        if recomputed != snapshot["normalized_sha256"]:
            raise StoreError(
                "the {} snapshot's normalized data does not match its own "
                "digest ({}.. recorded, {}.. recomputed): the file has been "
                "altered or corrupted since it was written, and a corrupt "
                "snapshot can neither be trusted nor promoted".format(
                    kind, snapshot["normalized_sha256"][:12],
                    recomputed[:12]))
        model.validate_catalog(snapshot["normalized"])
        return snapshot

    def approved(self):
        """The approved snapshot, or None when no baseline exists yet."""
        return self._load_snapshot(self.approved_path, "approved")

    def observed(self):
        return self._load_snapshot(self.observed_path, "observed")

    # -- freshness ---------------------------------------------------------
    def freshness(self, max_age_days=FRESHNESS_DAYS_DEFAULT, now=None):
        """How current the approved evidence is. Reported, never enforced."""
        snapshot = self.approved()
        if snapshot is None:
            return {"state": "missing",
                    "detail": "no approved catalog exists; acquire and "
                              "promote an initial baseline"}
        retrieved = _parse_utc(snapshot["retrieved_utc"])
        now = now or _utcnow()
        age = now - retrieved
        if age.total_seconds() < 0:
            return {"state": "anomalous",
                    "age_days": round(age.total_seconds() / 86400.0, 2),
                    "detail": "the approved evidence is dated in the future; "
                              "check the clock before trusting any freshness "
                              "conclusion"}
        days = age.total_seconds() / 86400.0
        state = "fresh" if days <= max_age_days else "stale"
        return {"state": state, "age_days": round(days, 2),
                "max_age_days": max_age_days,
                "retrieved_utc": snapshot["retrieved_utc"],
                "detail": ("within the freshness policy" if state == "fresh"
                           else "older than the freshness policy; refresh "
                                "and review when convenient - approved data "
                                "remains usable")}

    # -- observing -----------------------------------------------------------
    def record_observation(self, normalized, raw_sources, parser_identity,
                           source_specs, retrieved_utc=None, complete=True,
                           errors=()):
        """Write the latest observation. Never touches the approved state."""
        model.validate_catalog(normalized)
        retrieved = retrieved_utc or _utcnow().isoformat()
        sources = []
        for spec in source_specs:
            entry = {"id": spec["id"], "url": spec.get("url"),
                     "kind": spec.get("kind")}
            raw = raw_sources.get(spec["id"])
            if raw is not None:
                digest = hashlib.sha256(raw).hexdigest()
                entry["sha256_raw"] = digest
                entry["bytes"] = len(raw)
                _atomic_write_bytes(
                    os.path.join(self.observed_evidence,
                                 "{}-{}.raw".format(spec["id"], digest[:12])),
                    raw)
            sources.append(entry)
        snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "kind": "observed",
            "fabricator": self.fabricator,
            "retrieved_utc": retrieved,
            "parser": dict(parser_identity),
            "sources": sources,
            "complete": bool(complete),
            "errors": list(errors),
            "normalized_sha256": model.normalized_digest(normalized),
            "normalized": normalized,
        }
        _atomic_write_json(self.observed_path, snapshot)
        return snapshot

    # -- promotion -----------------------------------------------------------
    def promote(self, expected_normalized_sha256, changes, initial=False,
                allow_older=False, note=None):
        """Make the latest observation the approved state. Explicit, audited.

        `expected_normalized_sha256` is the digest the caller reviewed; if the
        observed snapshot is no longer that one - a newer refresh landed, a
        file was replaced - the promotion refuses, because approving something
        other than what was reviewed is the exact failure this parameter
        exists to prevent. A prefix of at least 12 characters is accepted.
        """
        observed = self.observed()
        if observed is None:
            raise StoreError("no observed snapshot exists to promote; run a "
                             "refresh first")
        if not observed.get("complete", False):
            raise StoreError(
                "the observed snapshot is marked incomplete ({} error(s) "
                "during acquisition); an incomplete observation cannot "
                "become the approved truth".format(
                    len(observed.get("errors", []))))
        digest = observed["normalized_sha256"]
        expected = (expected_normalized_sha256 or "").strip()
        if len(expected) < 12 or not digest.startswith(expected):
            raise StoreError(
                "promotion names normalized digest {!r} but the observed "
                "snapshot is {}...; promote exactly what was reviewed, by at "
                "least twelve digest characters".format(
                    expected, digest[:16]))
        approved = self.approved()
        if approved is None and not initial:
            raise StoreError(
                "no approved baseline exists; creating the first one is a "
                "distinct, deliberate act - pass initial=True (CLI: "
                "--initial) to say so")
        if approved is not None and initial:
            raise StoreError(
                "an approved baseline already exists; --initial would "
                "misdescribe this promotion")
        if approved is not None and not allow_older:
            if (_parse_utc(observed["retrieved_utc"])
                    <= _parse_utc(approved["retrieved_utc"])):
                raise StoreError(
                    "the observed snapshot ({}) is not newer than the "
                    "approved evidence ({}); promoting it would move trust "
                    "backwards in time. Refresh first, or pass "
                    "allow_older=True deliberately".format(
                        observed["retrieved_utc"],
                        approved["retrieved_utc"]))

        # Evidence first, then the approved snapshot, then the audit record:
        # if anything fails part-way the approved file is either old or new,
        # and the audit record only describes a promotion that completed.
        os.makedirs(self.approved_evidence, exist_ok=True)
        for source in observed["sources"]:
            digest_raw = source.get("sha256_raw")
            if not digest_raw:
                continue
            name = "{}-{}.raw".format(source["id"], digest_raw[:12])
            origin = os.path.join(self.observed_evidence, name)
            destination = os.path.join(self.approved_evidence, name)
            if os.path.isfile(origin) and not os.path.isfile(destination):
                shutil.copyfile(origin, destination)

        promoted = dict(observed)
        promoted["kind"] = "approved"
        promoted["approved_utc"] = _utcnow().isoformat()
        promoted["replaced_normalized_sha256"] = (
            approved["normalized_sha256"] if approved else None)
        _atomic_write_json(self.approved_path, promoted)

        log = []
        if os.path.isfile(self.promotions_path):
            with open(self.promotions_path, encoding="utf-8") as handle:
                log = json.load(handle)
        log.append({
            "promoted_utc": promoted["approved_utc"],
            "initial": bool(initial),
            "from_normalized_sha256": promoted["replaced_normalized_sha256"],
            "to_normalized_sha256": digest,
            "retrieved_utc": observed["retrieved_utc"],
            "parser": observed["parser"],
            "sources": [{k: source.get(k) for k in
                         ("id", "url", "sha256_raw")}
                        for source in observed["sources"]],
            "semantic_changes": len(changes),
            "change_summary": [
                "{}: {}".format(change.get("kind"), change.get("subject"))
                for change in changes[:20]],
            **({"note": note} if note else {}),
        })
        _atomic_write_json(self.promotions_path, log)
        return promoted
