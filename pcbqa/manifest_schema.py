"""The manifest preflight: every declaration is one the toolkit implements.

A key the toolkit does not read is a declaration that silently does nothing -
which is exactly how a reviewed orientation registry once shipped unapplied,
and how a mis-nested timing key cost a full validation cycle to find. So the
manifest is checked against `schemas/manifest.v2.json` before any command
touches the filesystem, and an unimplemented key is refused by name, the same
fail-closed rule the model registry applies.

Two kinds of content are legitimately not the toolkit's vocabulary:

* keys prefixed ``x_`` at any level - board-local data, carried in the
  manifest so it shares the configuration identity, never read by the toolkit;
* the annotation keys ``description``, ``note``, ``why`` and ``rationale``,
  which must be strings - prose about a declaration, not a declaration.

Both are permitted only where the schema encloses the object and enumerates
its keys. Inside an open-key map - `sources`, `timing.interfaces`,
`connector_gender_tokens` and the like - every key is data the code iterates
wholesale, so an annotation there would be consumed as an engineering value;
it is refused rather than stripped.
"""

from __future__ import annotations

import os

from . import schema

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "manifest.v2.json")

ANNOTATION_KEYS = ("description", "note", "why", "rationale")
EXTENSION_PREFIX = "x_"


class ManifestSchemaError(Exception):
    """The manifest declares something this toolkit does not implement."""


def _deref(node, root):
    while isinstance(node, dict) and "$ref" in node:
        target = root
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        node = target
    return node if isinstance(node, dict) else {}


def _strip(node, schema_node, root, where, problems):
    schema_node = _deref(schema_node, root)
    if isinstance(node, list):
        items = schema_node.get("items", {})
        return [_strip(value, items, root, "{}[{}]".format(where, index),
                       problems)
                for index, value in enumerate(node)]
    if not isinstance(node, dict):
        return node

    properties = schema_node.get("properties", {})
    additional = schema_node.get("additionalProperties", True)
    open_map = isinstance(additional, dict)
    closed = additional is False
    out = {}
    for key, value in node.items():
        annotation = key in ANNOTATION_KEYS
        extension = isinstance(key, str) and key.startswith(EXTENSION_PREFIX)
        if key in properties:
            out[key] = _strip(value, properties[key], root,
                              "{}.{}".format(where, key), problems)
            continue
        if open_map:
            if annotation or extension:
                problems.append(
                    "{}.{}: inside an open-key map every key is data the "
                    "toolkit consumes, so annotations and x_ keys are not "
                    "permitted here".format(where, key))
                continue
            out[key] = _strip(value, additional, root,
                              "{}.{}".format(where, key), problems)
            continue
        if closed and extension:
            continue
        if closed and annotation:
            if not isinstance(value, str):
                problems.append(
                    "{}.{}: an annotation key carries prose, so it must be "
                    "a string, not {}".format(where, key,
                                              type(value).__name__))
            continue
        # An opaque subtree the schema does not describe, or an unknown key
        # in a closed object: kept verbatim, so the validator can refuse the
        # latter by name.
        out[key] = value
    return out


def check(data, label):
    """Validate manifest `data` (a parsed dict). Raises ManifestSchemaError.

    `label` names the manifest in messages, usually its path.
    """
    try:
        loaded = schema.load_at(SCHEMA_PATH)
    except (OSError, ValueError, schema.SchemaError) as exc:
        raise ManifestSchemaError(
            "the manifest schema itself cannot be used ({}): {}".format(
                SCHEMA_PATH, exc)) from exc
    problems = []
    stripped = _strip(data, loaded, loaded, "$", problems)
    if problems:
        raise ManifestSchemaError(
            "{}: {}".format(label, "; ".join(problems)))
    try:
        schema.validate(stripped, loaded)
    except schema.ValidationError as exc:
        raise ManifestSchemaError(
            "{}: {} (the implemented keys are enumerated in {}; board-local "
            "data belongs under an x_-prefixed key)".format(
                label, exc, os.path.basename(SCHEMA_PATH))) from exc
