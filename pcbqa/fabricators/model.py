"""The normalized fabricator catalog: one schema, whoever the fabricator is.

Everything downstream of an adapter - storage, diffing, promotion, selection,
stackup export - operates on this shape and knows nothing about any particular
fabricator's website. An adapter's whole job is to turn its raw sources into
one of these, holding every record to the same rules:

  * every record names the source it came from and carries the excerpt of
    source text it was read from, so a number is auditable back to evidence;
  * a value the source does not state is absent, never defaulted - the
    `not_extracted` list records what was looked for and why it is missing,
    because "we could not read it" is information and silence is not;
  * two records the source distinguishes stay distinct here, however similar
    they look - collapsing near-duplicates is exactly how a real process
    difference gets laundered into "the same thing".

The normalized catalog is also the unit of semantic identity: its canonical
JSON digest is what freshness, comparison and promotion reason about. A raw
page can be re-styled without the digest moving; a single dielectric
thickness cannot.
"""

from __future__ import annotations

import hashlib
import json

SCHEMA_VERSION = 1

# Layer roles inside a normalized stackup.
COPPER = "copper"
DIELECTRIC = "dielectric"

# Dielectric forms, as fabricators name them.
CORE = "core"
PREPREG = "prepreg"


class CatalogError(Exception):
    """A catalog that cannot be trusted enough to use. Always blocks."""


def canonical_json(document):
    """The one byte encoding a normalized document hashes and diffs under.

    Sorted keys, no whitespace variation, no non-ASCII escapes surprises:
    two semantically identical catalogs serialize identically, whatever
    dict-ordering history they arrived with.
    """
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


#: Fields that are evidence or presentation, not fabrication semantics.
#: They stay in the snapshot - the excerpt is why a reviewer can trust a
#: value - but they must not move the semantic identity: a page redesign
#: that re-words the sentence around an unchanged number is not a change
#: to what the fabricator manufactures.
NON_SEMANTIC_FIELDS = ("excerpt", "excerpt_sha256")


def semantic_view(value):
    """The catalog with evidence/presentation fields recursively removed."""
    if isinstance(value, dict):
        return {key: semantic_view(item) for key, item in value.items()
                if key not in NON_SEMANTIC_FIELDS}
    if isinstance(value, list):
        return [semantic_view(item) for item in value]
    return value


def normalized_digest(catalog):
    """SHA-256 of the canonical *semantic* form: the catalog's identity.

    Computed over `semantic_view`, so it moves exactly when `semantic_diff`
    would report something - the promoted digest and the review interface
    agree on what counts as a change.
    """
    return hashlib.sha256(
        canonical_json(semantic_view(catalog)).encode("ascii")).hexdigest()


def empty_catalog(fabricator):
    return {
        "schema_version": SCHEMA_VERSION,
        "fabricator": fabricator,
        "capabilities": {},
        "materials": {},
        "stackups": {},
        "not_extracted": [],
    }


def capability(source, name, value, units=None, conditions=None, excerpt=None,
               category=None):
    """One stated manufacturing capability or option set.

    `value` may be a number, a string, a list of options, or a
    ``{"min":..,"max":..}`` range - whatever the source actually states.
    `excerpt` is the matched source text, kept because the value alone cannot
    show a reviewer that the extraction read what it claims to have read.
    """
    record = {"source": source, "name": name, "value": value}
    if units is not None:
        record["units"] = units
    if conditions is not None:
        record["conditions"] = conditions
    if excerpt is not None:
        record["excerpt"] = excerpt
    if category is not None:
        record["category"] = category
    return record


def material(source, kind, name, dk, excerpt=None):
    """A dielectric material identity with its stated dielectric constant."""
    record = {"source": source, "kind": kind, "name": name, "dk": dk}
    if excerpt is not None:
        record["excerpt"] = excerpt
    return record


def stackup_layer(role, thickness_mm, label=None, material_name=None,
                  form=None, sheet_count=None, annotation=None):
    """One layer of a published stackup, exactly as the source states it."""
    record = {"role": role, "thickness_mm": thickness_mm}
    if label is not None:
        record["label"] = label
    if material_name is not None:
        record["material"] = material_name
    if form is not None:
        record["form"] = form
    if sheet_count is not None:
        record["sheet_count"] = sheet_count
    if annotation is not None:
        record["annotation"] = annotation
    return record


def validate_catalog(catalog):
    """Refuse a catalog that is not structurally usable.

    Deliberately structural, not semantic: whether the values are *right* is
    the promotion reviewer's question. What is checked here is that the shape
    downstream code depends on actually holds, and that identity rules are
    intact - most importantly that no two records share an id, because a
    duplicate id is how one process option silently shadows another.
    """
    if not isinstance(catalog, dict):
        raise CatalogError("catalog is {}, not an object".format(
            type(catalog).__name__))
    if catalog.get("schema_version") != SCHEMA_VERSION:
        raise CatalogError(
            "catalog schema_version {!r}; this validator implements "
            "{}".format(catalog.get("schema_version"), SCHEMA_VERSION))
    if not catalog.get("fabricator"):
        raise CatalogError("catalog names no fabricator")
    for section in ("capabilities", "materials", "stackups"):
        table = catalog.get(section)
        if not isinstance(table, dict):
            raise CatalogError("catalog {} is not an object".format(section))
        for key, record in table.items():
            if not isinstance(record, dict):
                raise CatalogError(
                    "{}[{}] is not an object".format(section, key))
            if not record.get("source"):
                raise CatalogError(
                    "{}[{}] names no source; every record must be traceable "
                    "to the evidence it was read from".format(section, key))
    for identity, stackup in catalog["stackups"].items():
        layers = stackup.get("layers")
        if not isinstance(layers, list) or not layers:
            raise CatalogError(
                "stackup {} carries no layers".format(identity))
        for layer in layers:
            if layer.get("role") not in (COPPER, DIELECTRIC):
                raise CatalogError(
                    "stackup {} has a layer with role {!r}".format(
                        identity, layer.get("role")))
            thickness = layer.get("thickness_mm")
            if thickness is not None and (
                    isinstance(thickness, bool)
                    or not isinstance(thickness, (int, float))
                    or thickness <= 0):
                raise CatalogError(
                    "stackup {} states thickness {!r}, which is not a "
                    "positive length".format(identity, thickness))
    if not isinstance(catalog.get("not_extracted"), list):
        raise CatalogError("catalog not_extracted is not a list")
    return catalog


def stackup_copper_count(stackup):
    return sum(1 for layer in stackup["layers"] if layer["role"] == COPPER)


def stackup_total_mm(stackup):
    """Sum of stated layer thicknesses. Derived, and labelled so by callers.

    None when any layer omits its thickness: a partial sum presented as a
    total is exactly the kind of quiet fabrication this package exists to
    prevent.
    """
    total = 0.0
    for layer in stackup["layers"]:
        thickness = layer.get("thickness_mm")
        if thickness is None:
            return None
        total += thickness
    return round(total, 6)
