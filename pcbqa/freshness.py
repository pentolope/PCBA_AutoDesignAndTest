"""Producer closures: derived artifacts that know what made them.

A derived artifact (a candidate decision, a gate summary, a metric
report, a comparison) is only as current as the code and inputs that
produced it. This module gives such artifacts a PRODUCER CLOSURE - a
deliberate, named set of dependency digests plus one digest over the
set - and a verifier that answers, machine-readably, whether the
artifact is still fresh and, when it is not, exactly which
dependency moved.

The closure is deliberate, not automatic: the producer names the
files, texts and versions its output actually depends on, so an
unrelated change (a doc, an untouched module) does not invalidate
anything, while a change to a named dependency always does. A
consumer that finds a stale or tampered closure REFUSES the
artifact; it never quietly consumes it.

Identities are chosen per component KIND, because "the same
artifact" means different things for different bytes:

- ``path``      - raw bytes; the only honest identity for binaries.
- ``json_path`` - the canonical serialization of the parsed JSON
  content (sorted keys, minimal separators, UTF-8), so line
  endings, indentation and key order never masquerade as change.
  Unparseable JSON REFUSES - a corrupt artifact never gets a
  quiet raw-bytes identity.
- ``text_path`` - UTF-8 text with CRLF/CR normalized to LF; a
  checkout convention is not a source change.
- ``text``      - literal string content (e.g. a schema version).
- ``digest``    - an identity computed elsewhere (e.g. a board SHA).

Transitive freshness is built from these: a downstream closure
names its upstream ARTIFACTS (json_path), whose canonical content
embeds their own closures, so replacing an upstream artifact -
even with identical formatting - moves every closure downstream
of it, link by link.
"""

from __future__ import annotations

import hashlib
import json


class FreshnessError(Exception):
    """The closure cannot be built or the record cannot be trusted."""


_HEX = set("0123456789abcdef")


def canonical_json_digest(obj):
    """The identity of JSON-serializable content, independent of
    the bytes it happens to be stored in."""
    try:
        serialized = json.dumps(obj, sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                                allow_nan=False)
    except (TypeError, ValueError) as error:
        raise FreshnessError(
            "content is not canonically JSON-serializable: "
            "{}".format(error))
    return hashlib.sha256(
        serialized.encode("utf-8")).hexdigest()


def _read_bytes(name, value):
    try:
        with open(value, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise FreshnessError(
            "component {!r} names unreadable path {!r}: "
            "{}".format(name, value, error))


def _component_digest(name, spec):
    if isinstance(spec, str):
        spec = {"path": spec}
    if not isinstance(spec, dict) or len(spec) != 1:
        raise FreshnessError(
            "component {!r} must be a path string or exactly one of "
            "{{path|text|digest}}".format(name))
    kind, value = next(iter(spec.items()))
    if kind == "path":
        return hashlib.sha256(_read_bytes(name, value)).hexdigest()
    if kind == "json_path":
        raw = _read_bytes(name, value)
        try:
            content = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise FreshnessError(
                "component {!r} at {!r} is not valid JSON; a "
                "corrupt artifact refuses rather than receiving a "
                "raw-bytes identity: {}".format(name, value, error))
        return canonical_json_digest(content)
    if kind == "text_path":
        raw = _read_bytes(name, value)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FreshnessError(
                "component {!r} at {!r} is not UTF-8 text; use "
                "kind 'path' for binary content: {}".format(
                    name, value, error))
        normalized = text.replace("\r\n", "\n").replace(
            "\r", "\n")
        return hashlib.sha256(
            normalized.encode("utf-8")).hexdigest()
    if kind == "text":
        if not isinstance(value, str):
            raise FreshnessError(
                "component {!r} text must be a string".format(name))
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    if kind == "digest":
        if not (isinstance(value, str) and len(value) == 64
                and set(value) <= _HEX):
            raise FreshnessError(
                "component {!r} digest must be 64 hex characters, "
                "not {!r}".format(name, value))
        return value
    raise FreshnessError(
        "component {!r} kind {!r} is not path, json_path, "
        "text_path, text or digest".format(name, kind))


def closure(components):
    """Build one producer closure from named dependencies.

    ``components`` maps a dependency name to a path string, or to
    exactly one of {"path": p} (file bytes), {"json_path": p}
    (canonical JSON content), {"text_path": p} (LF-normalized
    text), {"text": t} (literal content, e.g. a schema version),
    {"digest": d} (an identity already computed elsewhere, e.g. a
    board SHA).
    """
    if not isinstance(components, dict) or not components:
        raise FreshnessError(
            "a closure needs a nonempty dict of named components")
    digests = {}
    for name in sorted(components):
        if not isinstance(name, str) or not name:
            raise FreshnessError("component names must be nonempty "
                                 "strings")
        digests[name] = _component_digest(name, components[name])
    overall = hashlib.sha256(json.dumps(
        digests, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"kind": "producer-closure", "components": digests,
            "digest": overall}


def verify(recorded, current_components):
    """Is an artifact's recorded closure still current?

    First the RECORD itself is checked: its digest must equal the
    digest of its own recorded components, so a hand-edited closure
    refuses as tampered rather than comparing. Then the current
    dependencies are digested and compared. The verdict names every
    moved, missing and added component - the machine-readable
    failure attribution a search loop acts on.
    """
    if not isinstance(recorded, dict) or \
            recorded.get("kind") != "producer-closure" or \
            set(recorded) != {"kind", "components", "digest"}:
        raise FreshnessError(
            "the recorded closure is not a producer-closure record")
    recomputed = hashlib.sha256(json.dumps(
        recorded["components"], sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    if recomputed != recorded["digest"]:
        raise FreshnessError(
            "the recorded closure's digest does not match its own "
            "components; a tampered record is refused, not "
            "compared")
    current = closure(current_components)
    recorded_names = set(recorded["components"])
    current_names = set(current["components"])
    moved = sorted(
        name for name in recorded_names & current_names
        if recorded["components"][name]
        != current["components"][name])
    return {
        "fresh": recorded["digest"] == current["digest"],
        "moved": moved,
        "missing": sorted(recorded_names - current_names),
        "added": sorted(current_names - recorded_names),
        "recorded_digest": recorded["digest"],
        "current_digest": current["digest"],
    }
