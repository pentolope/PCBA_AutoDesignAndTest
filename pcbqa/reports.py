"""KiCad ERC/DRC report readers, validated against the official schemas.

KiCad 10 does not use one shape for both reports:

  DRC  top-level `violations`, `unconnected_items`, `schematic_parity`
  ERC  no top-level `violations` at all - findings live under
       `sheets[].violations`, one entry per sheet

Reading `doc["violations"]` for ERC therefore always yields an empty list and
the gate passes no matter what the schematic contains. That was a real
false-PASS in an earlier implementation, and it is the reason nothing here is
inferred from the shape of the document in front of it. Every report is checked
against the vendored copy of the schema it declares (see
`schemas/kicad10/PROVENANCE.md`) before a single field is read.

Two things this module is careful about:

**Coordinates are not millimetres until they are converted.** Both schemas
permit `mm`, `mils` and `in`, and ERC does not even require the units field to
be present. Every position is converted to millimetres here and stored only in
millimetres, so nothing downstream - a clearance comparison, a waiver location,
a report line - can be reading inches under a name that says mm.

**A malformed report is an error, not an empty one.** Validation happens before
anything is normalised and long before waivers are considered, so a report that
cannot be trusted can never be quietly waived into a pass.
"""

from __future__ import annotations

from . import schema as _schema
from .schema import ValidationError, loads, load_report      # noqa: F401


class ReportSchemaError(Exception):
    pass


# Schema markers this reader implements, and the vendored file each maps to.
SUPPORTED_SCHEMAS = {
    "ERC": {"https://schemas.kicad.org/erc.v1.json": "erc"},
    "DRC": {"https://schemas.kicad.org/drc.v1.json": "drc"},
}

# Major versions whose report structure has been verified against the real
# tool. A newer major may move findings again, exactly as KiCad 10 did.
SUPPORTED_KICAD_MAJOR = (10,)

# Units the schemas permit, and what one of each is in millimetres.
UNITS_TO_MM = {"mm": 1.0, "in": 25.4, "mils": 0.0254}

# Fields this framework adds to a report it generated, for its own provenance.
# They are stripped before schema validation, because the schemas declare
# `additionalProperties: false` and would otherwise reject our own annotations
# as if KiCad had written them.
VALIDATOR_ANNOTATIONS = ("source_sha256", "source_closure_sha256",
                         "source_closure")

# Required by this validator, though optional in the schema: a report that does
# not say which severities it included cannot be evidence that anything was
# checked, and one that hides its ignored checks cannot be evidence that
# everything was.
REQUIRED_BEYOND_SCHEMA = ("included_severities", "ignored_checks")

DRC_BUCKETS = ("violations", "unconnected_items", "schematic_parity")

# How much of a description a one-line summary shows. Display only: canonical
# identity, digests and waiver matching always use the complete string. Two
# objects whose names agree for 160 characters and differ after that are two
# different objects, and truncating before comparing made them one.
DISPLAY_CHARS = 160


def _validate(doc, kind):
    """Schema-validate and return (document, units, mm_per_unit)."""
    if not isinstance(doc, dict):
        raise ReportSchemaError(
            f"{kind} report is a {type(doc).__name__}, not a JSON object")

    marker = doc.get("$schema")
    known = SUPPORTED_SCHEMAS[kind]
    if marker not in known:
        raise ReportSchemaError(
            f"{kind} report declares schema {marker!r}; this validator "
            f"implements {sorted(known)} and refuses to read a layout it has "
            f"not verified")

    try:
        vendored = _schema.load_schema(known[marker])
    except _schema.SchemaError as exc:
        raise ReportSchemaError(
            f"the vendored {kind} schema is unusable: {exc}") from exc

    subject = {k: v for k, v in doc.items() if k not in VALIDATOR_ANNOTATIONS}
    try:
        _schema.validate(subject, vendored)
    except ValidationError as exc:
        raise ReportSchemaError(
            f"{kind} report does not satisfy {marker}: {exc}") from exc

    version = str(doc.get("kicad_version") or "")
    major = version.split(".")[0]
    if not major.isdigit() or int(major) not in SUPPORTED_KICAD_MAJOR:
        raise ReportSchemaError(
            f"{kind} report was produced by KiCad {version!r}; report structure "
            f"is version-specific and only major version(s) "
            f"{list(SUPPORTED_KICAD_MAJOR)} have been verified")

    missing = [k for k in REQUIRED_BEYOND_SCHEMA if k not in doc]
    if missing:
        raise ReportSchemaError(
            f"{kind} report omits {missing}. The published schema makes these "
            f"optional; this validator does not, because a report that does "
            f"not state which severities it included is not evidence that "
            f"anything was checked")

    units = doc.get("coordinate_units")
    if units is None:
        raise ReportSchemaError(
            f"{kind} report does not state its coordinate units, so no "
            f"position in it can be placed on the board")
    if units not in UNITS_TO_MM:
        raise ReportSchemaError(
            f"{kind} report declares coordinate units {units!r}; supported "
            f"units are {sorted(UNITS_TO_MM)}")
    return doc, units, UNITS_TO_MM[units]


def parse_drc(doc):
    doc, units, scale = _validate(doc, "DRC")
    findings = []
    for bucket in DRC_BUCKETS:
        for item in doc[bucket]:
            findings.append(_normalise(item, bucket, scale, units))
    return findings, _meta(doc)


def parse_erc(doc):
    if isinstance(doc, dict) and "violations" in doc:
        # Caught before schema validation only so the message names the actual
        # mistake rather than "additionalProperties".
        raise ReportSchemaError(
            "ERC report has an unexpected top-level `violations`; the schema "
            "this validator implements puts them under sheets[].violations")
    doc, units, scale = _validate(doc, "ERC")
    if not doc["sheets"]:
        raise ReportSchemaError(
            "ERC report lists no sheets at all; an empty sheet list is not the "
            "same as a schematic with no problems")
    findings = []
    for sheet in doc["sheets"]:
        for item in sheet["violations"]:
            entry = _normalise(item, "violations", scale, units)
            entry["sheet"] = sheet.get("path") or sheet.get("uuid_path")
            findings.append(entry)
    return findings, _meta(doc)


def canonical_items(items):
    """A deterministic, order-independent form of an affected-item set.

    A violation names every object involved in it - both ends of a clearance
    error, every pad of a shorted net. KiCad lists them in whatever order it
    found them, and that order is incidental: the same violation reported with
    its two items swapped is the same violation. Sorting on the identity fields
    means a digest taken over this form is stable against that, and changes the
    moment any item actually moves, appears or disappears.
    """
    def key(entry):
        return (entry["description"],
                entry["uuid"] or "",
                _sortable(entry["x_mm"]),
                _sortable(entry["y_mm"]))
    return [
        {"description": e["description"], "uuid": e["uuid"],
         "x_mm": _round(e["x_mm"]), "y_mm": _round(e["y_mm"])}
        for e in sorted(items, key=key)
    ]


def _sortable(value):
    # None sorts before every real coordinate, deterministically.
    return (0, 0.0) if value is None else (1, value)


def _round(value):
    # Below any tolerance this framework applies, and far below KiCad's own
    # report precision; present so that two identical positions cannot differ
    # by a float artefact in a digest.
    return None if value is None else round(value, 6)


def _normalise(item, bucket, scale, units):
    """One finding, with *every* affected item converted to millimetres."""
    entries = []
    for sub in item["items"]:
        pos = sub.get("pos") or {}
        x, y = pos.get("x"), pos.get("y")
        entries.append({
            "uuid": sub.get("uuid"),
            # Complete and exact. The schema bounds this only by being a
            # string, so nothing here may shorten it.
            "description": sub.get("description") or "",
            "x_mm": None if x is None else x * scale,
            "y_mm": None if y is None else y * scale,
        })
    first = entries[0] if entries else {"description": "", "x_mm": None,
                                        "y_mm": None}
    return {
        "category": bucket,
        "severity": item["severity"],
        "rule": item["type"],
        "description": item.get("description") or "",
        # Every affected object, not just the first: a waiver that names one
        # end of a two-object violation is not an exact waiver, and a defect
        # that moves the second end is still a defect that moved.
        "items": entries,
        "canonical_items": canonical_items(entries),
        "objects": [e["description"] for e in entries],
        "objects_display": [e["description"][:DISPLAY_CHARS] for e in entries],
        "uuids": [e["uuid"] for e in entries if e["uuid"]],
        "excluded": bool(item.get("excluded", False)),
        # Kept for one-line reporting only. Nothing compares against these:
        # matching and digesting use the full item set above.
        "object": first["description"][:DISPLAY_CHARS],
        "description_display": (item.get("description") or "")[:200],
        "x_mm": first["x_mm"],
        "y_mm": first["y_mm"],
        "source_units": units,
    }


def _meta(doc):
    return {
        "schema": doc.get("$schema"),
        "date": doc.get("date"),
        "kicad_version": doc.get("kicad_version"),
        "source": doc.get("source"),
        "ignored_checks": [c.get("key") for c in doc.get("ignored_checks") or []],
        "included_severities": doc.get("included_severities") or [],
        "coordinate_units": doc.get("coordinate_units"),
    }
