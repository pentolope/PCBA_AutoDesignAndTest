"""A small JSON Schema draft-07 validator, and the vendored KiCad schemas.

KiCad's own Python has no `jsonschema` and this framework does not install into
an environment it does not own, so the subset of draft-07 the KiCad report
schemas actually use is implemented here: types, `required`, `properties`,
`additionalProperties`, `enum`, `items`, `pattern`, and `$ref` into
`#/definitions/`. Anything in a schema that is not implemented raises rather
than being skipped - a validator that silently ignores a keyword it does not
understand is worse than no validator, because it reports success.

Numbers are additionally required to be finite. JSON has no NaN or Infinity,
but Python's `json` module emits and accepts them by default, so a report can
carry a coordinate of `NaN` and be read back without complaint. A NaN
coordinate compares false against every waiver location and every tolerance,
which means such a finding can never be waived and must never be treated as
placed. It is rejected here instead.
"""

from __future__ import annotations

import json
import os
import re

SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "schemas", "kicad10")

SUPPORTED_KEYWORDS = {
    "$schema", "$id", "$ref", "title", "description", "default", "format",
    "type", "properties", "required", "additionalProperties", "enum", "items",
    "pattern", "definitions", "patternProperties",
}

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class SchemaError(Exception):
    """The schema itself cannot be used. Never a statement about a document."""


class ValidationError(Exception):
    """A document does not satisfy the schema."""


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _strip_trailing_commas(text):
    """Tolerate `[a, b,]` / `{...,}`. Returns (text, how_many_removed).

    Only used for the vendored upstream schemas, which are stored byte-for-byte
    as published; `drc.v1.json` has one such comma and does not parse without
    this. See schemas/kicad10/PROVENANCE.md.
    """
    out = []
    removed = 0
    in_string = False
    escape = False
    for index, ch in enumerate(text):
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            continue
        if ch == ",":
            rest = text[index + 1:]
            stripped = rest.lstrip(" \t\r\n")
            if stripped[:1] in ("]", "}"):
                removed += 1
                continue
        out.append(ch)
    return "".join(out), removed


_CACHE = {}


def load_at(path):
    """Load one of this repository's own schemas by path. Strict JSON."""
    if path in _CACHE:
        return _CACHE[path]
    with open(path, encoding="utf-8") as fh:
        doc = loads(fh.read())
    _check_supported(doc, doc, "#")
    _CACHE[path] = doc
    return doc


def load_schema(name):
    """Load a vendored schema by kind: 'drc' or 'erc'."""
    if name in _CACHE:
        return _CACHE[name]
    path = os.path.join(SCHEMA_DIR, f"{name}.v1.json")
    if not os.path.isfile(path):
        raise SchemaError(f"vendored schema not found: {path}")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        doc = json.loads(text)
        tolerated = 0
    except ValueError:
        cleaned, tolerated = _strip_trailing_commas(text)
        if not tolerated:
            raise
        try:
            doc = json.loads(cleaned)
        except ValueError as exc:
            raise SchemaError(
                f"{path} is not valid JSON even after removing {tolerated} "
                f"trailing comma(s): {exc}") from exc
    doc["__tolerated_trailing_commas__"] = tolerated
    _check_supported(doc, doc, "#")
    _CACHE[name] = doc
    return doc


def _check_supported(node, root, where):
    """Refuse to run against a schema using keywords we do not implement."""
    if isinstance(node, dict):
        unknown = [k for k in node
                   if k not in SUPPORTED_KEYWORDS
                   and not k.startswith("__")]
        if unknown:
            raise SchemaError(
                f"{where} uses unimplemented schema keyword(s) {unknown}")
        for key, value in node.items():
            if key in ("properties", "definitions", "patternProperties"):
                for sub, subschema in value.items():
                    _check_supported(subschema, root, f"{where}/{key}/{sub}")
            elif key == "items":
                _check_supported(value, root, f"{where}/items")
            elif key == "additionalProperties" and isinstance(value, dict):
                _check_supported(value, root, f"{where}/additionalProperties")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate(document, schema, root=None, where="$"):
    """Raise ValidationError unless `document` satisfies `schema`."""
    root = root if root is not None else schema

    if "$ref" in schema:
        return validate(document, _resolve(schema["$ref"], root), root, where)

    expected = schema.get("type")
    if expected is not None and not _is_type(document, expected):
        raise ValidationError(
            f"{where}: expected {expected}, found "
            f"{_name(document)}")

    if isinstance(document, (int, float)) and not isinstance(document, bool):
        if document != document or document in (float("inf"), float("-inf")):
            raise ValidationError(
                f"{where}: {document!r} is not a finite number; JSON has no "
                f"NaN or Infinity and a non-finite coordinate can never be "
                f"compared against a tolerance or a waiver")

    if "enum" in schema and document not in schema["enum"]:
        raise ValidationError(
            f"{where}: {document!r} is not one of {schema['enum']}")

    if "pattern" in schema and isinstance(document, str):
        if not re.search(schema["pattern"], document):
            raise ValidationError(
                f"{where}: {document!r} does not match the required form "
                f"{schema['pattern']!r}")

    if isinstance(document, dict):
        for key in schema.get("required", []):
            if key not in document:
                raise ValidationError(f"{where}: missing required field {key!r}")
        properties = schema.get("properties", {})
        patterns = schema.get("patternProperties", {})
        additional = schema.get("additionalProperties", True)

        def pattern_for(key):
            for pat in patterns:
                if re.search(pat, key):
                    return pat
            return None

        if additional is False:
            extra = sorted(k for k in document
                           if k not in properties and pattern_for(k) is None)
            if extra:
                raise ValidationError(
                    f"{where}: field(s) {extra} are not permitted here; the "
                    f"schema declares additionalProperties: false")
        for key, value in document.items():
            if key in properties:
                validate(value, properties[key], root, f"{where}.{key}")
                continue
            pattern = pattern_for(key)
            if pattern is not None:
                validate(value, patterns[pattern], root, f"{where}.{key}")
            elif isinstance(additional, dict):
                validate(value, additional, root, f"{where}.{key}")

    if isinstance(document, list) and "items" in schema:
        for index, value in enumerate(document):
            validate(value, schema["items"], root, f"{where}[{index}]")

    return True


def _resolve(ref, root):
    if not ref.startswith("#/"):
        raise SchemaError(f"only local refs are implemented, got {ref!r}")
    node = root
    for part in ref[2:].split("/"):
        if part not in node:
            raise SchemaError(f"unresolvable ref {ref!r}")
        node = node[part]
    return node


def _name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def _is_type(value, expected):
    if isinstance(expected, list):
        return any(_is_type(value, entry) for entry in expected)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    wanted = _TYPES.get(expected)
    if wanted is None:
        raise SchemaError(f"unimplemented schema type {expected!r}")
    if wanted is dict or wanted is list or wanted is str:
        return isinstance(value, wanted) and not isinstance(value, bool)
    return isinstance(value, wanted)


# ---------------------------------------------------------------------------
# strict JSON loading
# ---------------------------------------------------------------------------

def _reject_constant(token):
    raise ValueError(
        f"report contains the non-JSON constant {token!r}; NaN and Infinity "
        f"are not JSON and cannot be compared against any tolerance")


def loads(text):
    """`json.loads` that refuses NaN, Infinity and -Infinity."""
    return json.loads(text, parse_constant=_reject_constant)


def load_report(path):
    with open(path, encoding="utf-8") as fh:
        return loads(fh.read())
