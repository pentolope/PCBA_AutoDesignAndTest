"""Semantic comparison of normalized fabricator catalogs.

The review interface for a refresh. Raw hashes answer "did the bytes move";
this answers the question a person actually reviews: did the *fabrication
semantics* move, and where. A page redesign that leaves every normalized
record identical produces an empty diff and demands nobody's time; one
dielectric thickness produces exactly one change naming the stackup, the
layer and both values.

Every change record has the same shape:

    {"kind": ..., "subject": ..., "field": ..., "approved": ..., "observed": ...}

Kinds: capability-added/-removed/-changed, material-added/-removed/-changed,
stackup-added/-removed/-changed, stackup-renamed. Rename detection exists
because a fabricator renaming an entry may or may not be a real process
change: a removed id and an added id with byte-identical normalized content
are reported as one rename to review, not silently matched and not reported
as two unrelated events.
"""

from __future__ import annotations

from . import model


def _change(kind, subject, field=None, approved=None, observed=None):
    record = {"kind": kind, "subject": subject}
    if field is not None:
        record["field"] = field
    record["approved"] = approved
    record["observed"] = observed
    return record


def _flatten(prefix, value, out):
    if isinstance(value, dict):
        for key in sorted(value):
            _flatten("{}.{}".format(prefix, key) if prefix else key,
                     value[key], out)
    elif isinstance(value, list):
        # Lists are compared whole: element-wise pairing of, say, thickness
        # options would manufacture spurious per-index changes whenever one
        # option is inserted.
        out[prefix] = value
    else:
        out[prefix] = value


def _record_changes(kind, subject, approved, observed, ignore=(),
                    prefix=""):
    """Field-level changes between two records of the same identity.

    Both records pass through `model.semantic_view` first, so the
    evidence/presentation fields excluded from the semantic digest are
    excluded here identically - at every nesting depth, not just the top
    level. The two views of "what counts as a change" cannot drift apart.
    """
    flat_approved, flat_observed = {}, {}
    _flatten("", model.semantic_view(
        {k: v for k, v in approved.items() if k not in ignore}),
        flat_approved)
    _flatten("", model.semantic_view(
        {k: v for k, v in observed.items() if k not in ignore}),
        flat_observed)
    changes = []
    for field in sorted(set(flat_approved) | set(flat_observed)):
        before = flat_approved.get(field)
        after = flat_observed.get(field)
        if before != after:
            changes.append(_change(kind, subject, prefix + field,
                                   before, after))
    return changes


#: Provenance/presentation fields, excluded here for the same reason
#: `model.normalized_digest` excludes them: an excerpt re-wrapped by a page
#: redesign is not a manufacturing change, and treating it as one would bury
#: the changes that are.
_NON_SEMANTIC = model.NON_SEMANTIC_FIELDS


def semantic_diff(approved_catalog, observed_catalog):
    """Every fabrication-semantic difference, as reviewable change records."""
    changes = []
    for section, singular in (("capabilities", "capability"),
                              ("materials", "material"),
                              ("stackups", "stackup")):
        approved = approved_catalog.get(section, {})
        observed = observed_catalog.get(section, {})
        added = sorted(set(observed) - set(approved))
        removed = sorted(set(approved) - set(observed))

        # Rename detection: identical semantic content under a new identity.
        renames = []
        if section == "stackups":
            def essence(record):
                return model.canonical_json(model.semantic_view(
                    {k: v for k, v in record.items() if k != "name"}))
            removed_by_essence = {essence(approved[i]): i for i in removed}
            for identity in list(added):
                key = essence(observed[identity])
                if key in removed_by_essence:
                    old = removed_by_essence.pop(key)
                    renames.append((old, identity))
                    added.remove(identity)
                    removed.remove(old)
        for old, new in renames:
            changes.append(_change(
                "stackup-renamed", "{} -> {}".format(old, new),
                approved=old, observed=new))

        for identity in added:
            changes.append(_change(singular + "-added", identity,
                                   observed=_summary(observed[identity])))
        for identity in removed:
            changes.append(_change(singular + "-removed", identity,
                                   approved=_summary(approved[identity])))
        for identity in sorted(set(approved) & set(observed)):
            before, after = approved[identity], observed[identity]
            if section == "stackups":
                changes.extend(_layer_changes(identity, before, after))
                ignore = _NON_SEMANTIC + ("layers",)
            else:
                ignore = _NON_SEMANTIC
            changes.extend(_record_changes(
                singular + "-changed", identity, before, after,
                ignore=ignore))
    return changes


def _layer_changes(identity, approved, observed):
    """Layer-by-layer changes within one stackup identity.

    A stackup's layers are ordered construction, so with the layer count
    unchanged, position i corresponds to position i and a single edited
    thickness is reported as exactly that: `layers[i].thickness_mm`, old
    value, new value. When the count itself changes, positional pairing
    would manufacture a cascade of spurious per-layer diffs, so the change
    is reported once, as the construction summaries.
    """
    before = approved.get("layers", [])
    after = observed.get("layers", [])
    if len(before) != len(after):
        return [_change("stackup-changed", identity, "layers",
                        _summary(approved), _summary(observed))]
    changes = []
    for index, (one, other) in enumerate(zip(before, after)):
        changes.extend(_record_changes(
            "stackup-changed", identity, one, other, ignore=_NON_SEMANTIC,
            prefix="layers[{}].".format(index)))
    return changes


def _summary(record):
    if "layers" in record:
        return "{} layers, {} mm total".format(
            len(record["layers"]), model.stackup_total_mm(record))
    if "dk" in record:
        return "dk {}".format(record["dk"])
    return record.get("value")


def render(changes):
    """The diff as an engineer reads it, one change per stanza."""
    if not changes:
        return "no fabrication-semantic differences"
    lines = []
    for change in changes:
        head = "{} {}".format(change["kind"], change["subject"])
        if change.get("field"):
            lines.append("{}:".format(head))
            lines.append("  {}:".format(change["field"]))
            lines.append("      approved: {}".format(change["approved"]))
            lines.append("      observed: {}".format(change["observed"]))
        else:
            lines.append("{}".format(head))
            if change.get("approved") is not None:
                lines.append("      approved: {}".format(change["approved"]))
            if change.get("observed") is not None:
                lines.append("      observed: {}".format(change["observed"]))
    return "\n".join(lines)
