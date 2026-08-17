# Vendored KiCad 10 report schemas

Retrieved 2026-08-04 from the addresses the reports themselves declare in their
`$schema` field:

| File | Source | sha256 |
|---|---|---|
| `drc.v1.json` | `https://schemas.kicad.org/drc.v1.json` | `4699a2ecde9f3d1341187fa6805991a08c385fb6e44c17b0a5c74441886d8e9c` |
| `erc.v1.json` | `https://schemas.kicad.org/erc.v1.json` | `b8637f15b60138685c68b8c6a0b5357657162bd3a06d361b801d955ac2ab1690` |

They are stored **byte-for-byte as retrieved**, so the digests above can be
checked against the upstream files at any time. They are vendored rather than
fetched at validation time for the obvious reason: a checker that needs the
network to decide whether a board is manufacturable will one day decide it is,
because the network was down and someone added a fallback.

## Upstream defect: `drc.v1.json` is not valid JSON

The DRC schema has a trailing comma in its top-level `required` array:

```json
  "required": [
    "source",
    "date",
    "kicad_version",
    "violations",
    "unconnected_items",
    "schematic_parity",
    "coordinate_units",
  ],
```

`json.loads` rejects this at line 93. The file has been left exactly as
published; `pcbqa.schema.load_schema` strips trailing commas before parsing and
records that it had to. Correcting the vendored copy would have made the
digests above unverifiable against upstream, which is a worse trade than
tolerating one comma in a loader that says out loud what it is tolerating.

The ERC schema parses cleanly.

## Two things the schemas say that are worth knowing

**`severity` is `["error", "warning"]` only.** "exclusion" appears in
`included_severities` — it is a severity you can *ask for* — but a violation
carries `excluded: true` rather than a severity of "exclusion". A checker that
looked for `severity == "exclusion"` would never find one.

**ERC does not require `coordinate_units`; DRC does.** Both schemas allow
`mm`, `mils` and `in`. Coordinates are therefore not millimetres until
something converts them, which is why `pcbqa.reports` converts to millimetres
at parse time and stores nothing in the units it was handed.

`included_severities` and `ignored_checks` are optional in both schemas. This
validator requires them anyway: a report that does not say which severities it
included cannot be evidence that anything was checked. That requirement is the
validator's, is applied after schema validation, and says so in its message.
