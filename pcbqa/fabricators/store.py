"""Approved and observed fabricator knowledge, kept apart on purpose.

Two directories, two meanings, one door between them:

    <root>/catalog/approved.json      what design work may trust
    <root>/catalog/evidence/          the raw source bytes behind it
    <root>/catalog/promotions.json    the audit log of every promotion
    <root>/catalog/verification.json  when the approved semantics were last
                                      re-verified against a fresh acquisition
    <root>/observed/latest.json       the newest acquisition ATTEMPT -
                                      successful or not; trusted for nothing
    <root>/observed/previous.json     the attempt latest displaced (one deep)
    <root>/observed/evidence/         raw bytes behind observations

Every acquisition attempt becomes ``latest.json``, whatever its outcome:

    complete      every source fetched and parsed; promotable after review
    incomplete    a fetch failed; never promotable
    parse-failed  fetched but unreadable; never promotable, raw bytes kept

so a newer failed acquisition supersedes an older successful one as "the
latest known state of the source" - an old observation can never masquerade
as the newest just because the newest attempt went badly. The displaced
attempt survives as ``previous.json`` for inspection, labelled as history.

Raw evidence is load-bearing. A snapshot's recorded ``sha256_raw`` digests
are re-verified against the evidence files every time the snapshot is
loaded; a snapshot whose evidence is missing or altered refuses to load at
all, because "the values survive but the proof is gone" must not be
indistinguishable from a fully auditable state.

Freshness separates two clocks. The approved snapshot's ``retrieved_utc``
is when its evidence was fetched; ``verification.json`` records the last
time a fresh acquisition parsed to semantics identical to the approved
catalog. A semantically identical refresh therefore renews freshness
without rewriting one byte of approved data and without a promotion; only
a *differing* observation needs a human, and it is reported as pending
until one acts.

Writes are atomic (a uniquely named temp file in the same directory, then
``os.replace``), and every multi-file transition - recording an attempt,
verifying, promoting - runs under an exclusive lock file, so two concurrent
operations serialize instead of interleaving their file writes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
import time

from . import model

#: How long fabrication-process knowledge is presumed current. Process pages
#: move slowly; a month-old catalog is worth re-checking, not worth blocking.
FRESHNESS_DAYS_DEFAULT = 30

SNAPSHOT_SCHEMA_VERSION = 3

#: Snapshot schema versions this loader understands. Anything else refuses:
#: reading half of an unknown format is how provenance quietly rots.
#: Version 3 added `declared_source_ids` - the adapter's required source
#: set at acquisition time - so completeness is checkable without the
#: store knowing any fabricator's names.
KNOWN_SNAPSHOT_SCHEMAS = (1, 2, 3)

VERIFICATION_SCHEMA_VERSION = 1

OUTCOME_COMPLETE = "complete"
OUTCOME_INCOMPLETE = "incomplete"
OUTCOME_PARSE_FAILED = "parse-failed"

_LOCK_TIMEOUT_S = 10.0
_LOCK_STALE_S = 300.0


class StoreError(Exception):
    """The stored state cannot be used as asked. Always blocks."""


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_utc(text):
    return datetime.datetime.fromisoformat(text)


def _atomic_write_json(path, document):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".partial-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8",
                       newline="\n") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _atomic_write_bytes(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".partial-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class _Lock:
    """One exclusive lock file per store root.

    ``O_CREAT | O_EXCL`` is atomic on every filesystem this runs on, which
    is all the mutual exclusion a same-machine, few-writers store needs. A
    lock older than ``_LOCK_STALE_S`` is presumed abandoned by a killed
    process and broken; a live contender simply waits its turn.
    """

    def __init__(self, root):
        self.path = os.path.join(root, ".lock")

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.monotonic() + _LOCK_TIMEOUT_S
        while True:
            try:
                descriptor = os.open(self.path,
                                     os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write("{} pid={}\n".format(
                        _utcnow().isoformat(), os.getpid()))
                return self
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.path)
                    if age > _LOCK_STALE_S:
                        os.unlink(self.path)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise StoreError(
                        "another fabricator-store operation holds the lock "
                        "at {} and did not finish within {} seconds; "
                        "refusing to interleave writes with it".format(
                            self.path, _LOCK_TIMEOUT_S))
                time.sleep(0.05)

    def __exit__(self, *_exc):
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def verify_evidence(snapshot, directory):
    """Every recorded raw digest, checked against the actual bytes on disk.

    Returns a list of problem strings, empty when the whole chain holds.
    The check is by content, not by name: a wrong file under the right
    name and a right file that has since been altered both surface.
    """
    problems = []
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
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
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
    return problems


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
        self.verification_path = os.path.join(self.root, "catalog",
                                              "verification.json")
        self.observed_path = os.path.join(self.root, "observed",
                                          "latest.json")
        self.previous_path = os.path.join(self.root, "observed",
                                          "previous.json")
        self.observed_evidence = os.path.join(self.root, "observed",
                                              "evidence")

    def _lock(self):
        return _Lock(self.root)

    # -- loading -----------------------------------------------------------
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
        if snapshot["fabricator"] != self.fabricator:
            raise StoreError(
                "the {} snapshot describes {!r}, not {!r}".format(
                    kind, snapshot["fabricator"], self.fabricator))
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
                    "and a corrupt snapshot can neither be trusted nor "
                    "promoted".format(kind,
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

    def observed(self):
        """The newest acquisition attempt, whatever its outcome."""
        return self._load_snapshot(self.observed_path, "observed",
                                   self.observed_evidence)

    def previous_observed(self):
        """The attempt that ``latest`` displaced. History, never 'latest'."""
        return self._load_snapshot(self.previous_path, "displaced observed",
                                   self.observed_evidence)

    def verification(self):
        """The verification ledger, validated - or None, never a guess.

        The ledger can renew freshness, which makes it operationally
        trust-relevant even though it can never alter approved semantics.
        Any structural or semantic problem - unreadable JSON, an unknown
        schema, missing fields, unparseable or reversed timestamps -
        makes the ledger worth nothing rather than worth anything:
        freshness falls back to the approved evidence age, which can only
        make the catalog look older, never fresher.
        """
        if not os.path.isfile(self.verification_path):
            return None
        try:
            with open(self.verification_path, encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        if record.get("schema_version") != VERIFICATION_SCHEMA_VERSION:
            return None
        for field in ("verified_utc", "approved_normalized_sha256",
                      "observed_retrieved_utc", "parser"):
            if not record.get(field):
                return None
        if not isinstance(record["parser"], dict) \
                or not record["parser"].get("id"):
            return None
        try:
            verified = _parse_utc(record["verified_utc"])
            retrieved = _parse_utc(record["observed_retrieved_utc"])
        except (ValueError, TypeError):
            return None
        if verified < retrieved:
            # A verification cannot predate the acquisition it verified.
            return None
        return record

    # -- freshness ---------------------------------------------------------
    def freshness(self, max_age_days=FRESHNESS_DAYS_DEFAULT, now=None):
        """How current the approved knowledge is, and what needs attention.

        Freshness is evidence age: the last time the live sources were
        fetched AND found to carry the approved semantics - either the
        approved acquisition itself, or a later verification recorded
        against the same approved digest. Attention items are everything a
        selection consumer should see before quietly trusting the catalog:
        a pending differing observation, a failed newest acquisition, a
        parser mismatch. Reported, never enforced.
        """
        now = now or _utcnow()
        result = {"max_age_days": max_age_days, "attention": []}
        try:
            snapshot = self.approved()
        except StoreError as exc:
            result["state"] = "unusable"
            result["detail"] = str(exc)
            return result
        if snapshot is None:
            result["state"] = "no-baseline"
            result["detail"] = ("no approved catalog exists; acquire and "
                                "promote an initial baseline")
            return result

        verified = _parse_utc(snapshot["retrieved_utc"])
        verification = self.verification()
        if verification and verification.get(
                "approved_normalized_sha256") == snapshot.get(
                    "normalized_sha256"):
            candidate = _parse_utc(verification["observed_retrieved_utc"])
            if candidate > verified:
                verified = candidate
                result["renewed_by_verification_utc"] = \
                    verification["verified_utc"]
        result["evidence_utc"] = snapshot["retrieved_utc"]
        result["verified_utc"] = verified.isoformat()

        age = now - verified
        if age.total_seconds() < 0:
            result["state"] = "anomalous"
            result["age_days"] = round(age.total_seconds() / 86400.0, 2)
            result["detail"] = ("the verification evidence is dated in the "
                                "future; check the clock before trusting "
                                "any freshness conclusion")
        else:
            days = age.total_seconds() / 86400.0
            result["age_days"] = round(days, 2)
            if days <= max_age_days:
                result["state"] = "current"
                result["detail"] = ("the approved semantics were last "
                                    "confirmed against the live sources "
                                    "{} days ago".format(round(days, 2)))
            else:
                result["state"] = "stale"
                result["detail"] = (
                    "not confirmed against the live sources for {} days; "
                    "refresh and review when convenient - approved data "
                    "remains usable".format(round(days, 2)))

        self._attention(snapshot, result)
        return result

    def _attention(self, approved, result):
        try:
            observed = self.observed()
        except StoreError as exc:
            result["attention"].append(
                "the newest acquisition attempt is unusable: {}".format(exc))
            return
        if observed is None:
            return
        outcome = observed["outcome"]
        if outcome != OUTCOME_COMPLETE:
            errors = "; ".join(
                "{}: {}".format(e.get("source"), e.get("error"))
                for e in observed.get("errors", [])) or "no detail recorded"
            result["attention"].append(
                "the newest acquisition ({}) {}: {}. The approved catalog "
                "is unaffected, but the live source could not be read at "
                "that time".format(
                    observed["retrieved_utc"],
                    "could not fetch every source"
                    if outcome == OUTCOME_INCOMPLETE
                    else "fetched but could not be parsed", errors))
        elif observed.get("normalized_sha256") != approved.get(
                "normalized_sha256"):
            result["attention"].append(
                "a differing observation ({}.., retrieved {}) awaits "
                "review; run `fab diff` and promote or reject it".format(
                    (observed.get("normalized_sha256") or "")[:12],
                    observed["retrieved_utc"]))
        if observed.get("parser") != approved.get("parser"):
            result["attention"].append(
                "parser identity differs between approved ({} v{}) and the "
                "newest observation ({} v{}); differences may reflect the "
                "extractor, not the fabricator".format(
                    approved["parser"].get("id"),
                    approved["parser"].get("version"),
                    observed["parser"].get("id"),
                    observed["parser"].get("version")))

    # -- observing ---------------------------------------------------------
    def record_observation(self, normalized, raw_sources, parser_identity,
                           source_specs, outcome, retrieved_utc=None,
                           errors=()):
        """Record one acquisition attempt as the newest, whatever happened.

        Never touches the approved state. The attempt that was ``latest``
        until now is kept as ``previous.json`` - history for inspection,
        no longer anyone's idea of current.

        Crash recovery, stated: evidence files are written first (content-
        addressed, so a re-run overwrites nothing meaningful), then the old
        ``latest`` is atomically renamed to ``previous``, then the new
        ``latest`` is atomically written. A crash in the gap leaves
        ``latest`` absent and the last acquisition intact at ``previous`` -
        a visible degraded state (``observed()`` returns None,
        ``previous_observed()`` still answers), never a corrupt or lying
        one. What that gap can cost is the *older* history layer that the
        rename displaced; the newest completed acquisition itself is never
        the only casualty. The next refresh rebuilds ``latest`` normally.
        """
        if outcome not in (OUTCOME_COMPLETE, OUTCOME_INCOMPLETE,
                           OUTCOME_PARSE_FAILED):
            raise StoreError("unknown observation outcome {!r}".format(
                outcome))
        if outcome == OUTCOME_COMPLETE:
            if normalized is None:
                raise StoreError("a complete observation must carry its "
                                 "normalized catalog")
            model.validate_catalog(normalized)
        elif normalized is not None:
            raise StoreError(
                "a {} observation must not carry a normalized catalog; a "
                "half-parsed catalog is not evidence of anything".format(
                    outcome))
        retrieved = retrieved_utc or _utcnow().isoformat()
        with self._lock():
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
                                     "{}-{}.raw".format(spec["id"],
                                                        digest[:12])),
                        raw)
                sources.append(entry)
            snapshot = {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "kind": "observed",
                "fabricator": self.fabricator,
                "retrieved_utc": retrieved,
                "parser": dict(parser_identity),
                "sources": sources,
                "declared_source_ids": [spec["id"] for spec in source_specs],
                "outcome": outcome,
                "errors": list(errors),
                "normalized_sha256": (model.normalized_digest(normalized)
                                      if normalized is not None else None),
                "normalized": normalized,
            }
            if os.path.isfile(self.observed_path):
                os.replace(self.observed_path, self.previous_path)
            _atomic_write_json(self.observed_path, snapshot)
        return snapshot

    # -- verification ------------------------------------------------------
    def record_verification(self, observed):
        """A fresh acquisition matched the approved semantics: note when.

        This renews freshness without touching one byte of approved data
        and without a promotion - there is nothing to review when nothing
        changed. Refuses unless the observation is complete and its digest
        equals the approved digest, because "roughly the same" is not a
        verification of anything.
        """
        approved = self.approved()
        if approved is None:
            raise StoreError("nothing is approved, so nothing can be "
                             "verified as unchanged")
        if observed.get("outcome") != OUTCOME_COMPLETE:
            raise StoreError(
                "a {} acquisition verifies nothing; the source could not "
                "even be read".format(observed.get("outcome")))
        if observed.get("normalized_sha256") != approved.get(
                "normalized_sha256"):
            raise StoreError(
                "the observation ({}..) differs from the approved catalog "
                "({}..); a differing observation is reviewed and promoted, "
                "never recorded as a verification".format(
                    (observed.get("normalized_sha256") or "")[:12],
                    approved["normalized_sha256"][:12]))
        record = {
            "schema_version": VERIFICATION_SCHEMA_VERSION,
            "verified_utc": _utcnow().isoformat(),
            "approved_normalized_sha256": approved["normalized_sha256"],
            "observed_retrieved_utc": observed["retrieved_utc"],
            "parser": observed.get("parser"),
            "sources": [{key: source.get(key) for key in
                         ("id", "url", "sha256_raw")}
                        for source in observed.get("sources", [])],
        }
        with self._lock():
            _atomic_write_json(self.verification_path, record)
        return record

    # -- promotion ---------------------------------------------------------
    def promote(self, expected_normalized_sha256, changes, initial=False,
                allow_older=False, note=None):
        """Make the latest observation the approved state. Explicit, audited.

        `expected_normalized_sha256` is the digest the caller reviewed; if
        the observed snapshot is no longer that one - a newer refresh
        landed, a file was replaced - the promotion refuses, because
        approving something other than what was reviewed is the exact
        failure this parameter exists to prevent. A prefix of at least 12
        characters is accepted.

        Crash recovery, stated: evidence copies land first (verified,
        content-addressed, idempotent), then ``approved.json`` is
        atomically replaced, then the audit entry is appended. The
        approved snapshot itself carries its promotion identity -
        ``approved_utc`` and ``replaced_normalized_sha256`` - so a crash
        after the approved write but before the audit append leaves a
        fully self-describing approved state whose missing audit entry is
        reconstructible from the file it describes; the audit log is
        corroborating history, never the only proof a promotion happened.
        A crash before the approved write leaves the old approved state
        untouched with at most some orphaned (correct, content-addressed)
        evidence copies.
        """
        with self._lock():
            observed = self.observed()
            if observed is None:
                raise StoreError("no observed snapshot exists to promote; "
                                 "run a refresh first")
            if observed["outcome"] != OUTCOME_COMPLETE:
                raise StoreError(
                    "the newest acquisition attempt is {} ({} error(s) "
                    "recorded); an attempt that could not read the source "
                    "cannot become the approved truth - and any older "
                    "observation it displaced is history, not a promotion "
                    "candidate".format(observed["outcome"],
                                       len(observed.get("errors", []))))
            digest = observed["normalized_sha256"]
            expected = (expected_normalized_sha256 or "").strip()
            if len(expected) < 12 or not digest.startswith(expected):
                raise StoreError(
                    "promotion names normalized digest {!r} but the newest "
                    "observation is {}...; promote exactly what was "
                    "reviewed, by at least twelve digest characters".format(
                        expected, digest[:16]))
            approved = self.approved()
            if approved is None and not initial:
                raise StoreError(
                    "no approved baseline exists; creating the first one is "
                    "a distinct, deliberate act - pass initial=True (CLI: "
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
                        "approved evidence ({}); promoting it would move "
                        "trust backwards in time. Refresh first, or pass "
                        "allow_older=True deliberately".format(
                            observed["retrieved_utc"],
                            approved["retrieved_utc"]))

            # Evidence first, verified after the copy; then the approved
            # snapshot; then the audit record. If anything fails part-way
            # the approved file is either old or new, never mixed, and the
            # audit record only describes a promotion that completed.
            os.makedirs(self.approved_evidence, exist_ok=True)
            for source in observed["sources"]:
                digest_raw = source.get("sha256_raw")
                if not digest_raw:
                    continue
                name = "{}-{}.raw".format(source["id"], digest_raw[:12])
                origin = os.path.join(self.observed_evidence, name)
                if not os.path.isfile(origin):
                    raise StoreError(
                        "observed evidence {} is missing; it was verified "
                        "at load and has since disappeared".format(name))
                with open(origin, "rb") as handle:
                    data = handle.read()
                if hashlib.sha256(data).hexdigest() != digest_raw:
                    raise StoreError(
                        "observed evidence {} changed between verification "
                        "and promotion; refusing to promote bytes that are "
                        "not the reviewed bytes".format(name))
                _atomic_write_bytes(
                    os.path.join(self.approved_evidence, name), data)

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
                "from_normalized_sha256":
                    promoted["replaced_normalized_sha256"],
                "to_normalized_sha256": digest,
                "retrieved_utc": observed["retrieved_utc"],
                "parser": observed["parser"],
                "sources": [{key: source.get(key) for key in
                             ("id", "url", "sha256_raw")}
                            for source in observed["sources"]],
                "semantic_changes": len(changes),
                "change_summary": [
                    "{}: {}".format(change.get("kind"),
                                    change.get("subject"))
                    for change in changes[:20]],
                **({"note": note} if note else {}),
            })
            _atomic_write_json(self.promotions_path, log)
        return promoted
