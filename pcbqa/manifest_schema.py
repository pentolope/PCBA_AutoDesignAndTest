"""The manifest preflight: every declaration is one the toolkit implements.

A key the toolkit does not read is a declaration that silently does nothing -
which is exactly how a reviewed orientation registry once shipped unapplied,
and how a mis-nested timing key cost a full validation cycle to find. So the
manifest is checked against `schemas/manifest.v2.json` before any command
touches the filesystem, and an unimplemented key is refused by name, the same
fail-closed rule the model registry applies.

Two kinds of content are legitimately not the toolkit's vocabulary and are
stripped before the schema is applied:

* keys prefixed ``x_`` at any level - board-local data, carried in the
  manifest so it shares the configuration identity, never read by the toolkit;
* the annotation keys ``description``, ``note``, ``why`` and ``rationale`` at
  any level, which must be strings - prose about a declaration, not a
  declaration.
"""

from __future__ import annotations

import copy
import os

from . import schema

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schemas", "manifest.v2.json")

ANNOTATION_KEYS = ("description", "note", "why", "rationale")
EXTENSION_PREFIX = "x_"


class ManifestSchemaError(Exception):
    """The manifest declares something this toolkit does not implement."""


def _strip(node, where, problems):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if isinstance(key, str) and key.startswith(EXTENSION_PREFIX):
                continue
            if key in ANNOTATION_KEYS:
                if not isinstance(value, str):
                    problems.append(
                        "{}.{}: an annotation key carries prose, so it must "
                        "be a string, not {}".format(
                            where, key, type(value).__name__))
                continue
            out[key] = _strip(value, "{}.{}".format(where, key), problems)
        return out
    if isinstance(node, list):
        return [_strip(value, "{}[{}]".format(where, index), problems)
                for index, value in enumerate(node)]
    return node


def check(data, label):
    """Validate manifest `data` (a parsed dict). Raises ManifestSchemaError.

    `label` names the manifest in messages, usually its path.
    """
    problems = []
    stripped = _strip(copy.deepcopy(data), "$", problems)
    if problems:
        raise ManifestSchemaError(
            "{}: {}".format(label, "; ".join(problems)))
    try:
        loaded = schema.load_at(SCHEMA_PATH)
    except (OSError, ValueError, schema.SchemaError) as exc:
        raise ManifestSchemaError(
            "the manifest schema itself cannot be used ({}): {}".format(
                SCHEMA_PATH, exc)) from exc
    try:
        schema.validate(stripped, loaded)
    except schema.ValidationError as exc:
        raise ManifestSchemaError(
            "{}: {} (the implemented keys are enumerated in {}; board-local "
            "data belongs under an x_-prefixed key)".format(
                label, exc, os.path.basename(SCHEMA_PATH))) from exc
