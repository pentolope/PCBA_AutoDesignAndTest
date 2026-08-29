# Onboarding a board

The toolkit is consumed as a submodule and driven by one manifest. Nothing in
`pcbqa/` needs editing to support a new board — if you find yourself editing it,
you have found either a genuine gap in a rule type, or a value that belongs in
your manifest.

## 1. Add the submodule

```bash
git submodule add https://github.com/pentolope/PCB_AutoDesignAndTest tooling/PCB_AutoDesignAndTest
```

## 2. Start with the minimum manifest

```json
{
  "schema_version": 2,
  "board_id": "my_board",
  "constraint_version": "v1",
  "project_root": "..",
  "tools": { "kicad_cli": "kicad-cli" },
  "sources": { "pcb": "my_board.kicad_pcb" },
  "board_origin_mm": [0.0, 0.0],
  "documentation_globs": [],
  "waivers": []
}
```

```bash
python3 tooling/PCB_AutoDesignAndTest/run.py validate board/manifest.json
```

Every gate you have not configured reports `NOT_APPLICABLE` **with a reason**
and still appears in the matrix. Absence is never a silent pass, so the report
tells you exactly what you have not yet opted into.

## 3. Add policy blocks until the matrix says what you need

| Manifest key | Gates it enables |
|---|---|
| `fixture.hash_file` | `PROV.FIXTURE_INTEGRITY` |
| `source_authority` | `PROV.SOURCE_AUTHORITY` |
| `reports` | `PROV.REPORT_FRESHNESS` |
| `checks.erc` / `checks.drc` | `ERC.AUTHORITATIVE`, `DRC.AUTHORITATIVE` |
| `checks.drc.forbidden_severities` | `DRC.NO_SUPPRESSED_RULES` |
| `stackup.expected` | `STACK.NATIVE_VS_MANIFEST` (+ `STACK.GERBER_PARITY` with `artifacts.gerber_dir`) |
| `via_mask.*` | the four `VIA.*` gates |
| `routing.*` | the three `ROUTE.*` gates |
| `net_topology.rules` | `NET.TOPOLOGY` |
| `connector_contracts` + `connector_gender_tokens` | `CONTRACT.CONNECTOR` |
| `placement_rules` | `CONTRACT.PLACEMENT` |
| `artifacts.bom` + `artifacts.cpl` | `BOM.NATIVE_PARITY`, `CPL.NATIVE_PARITY` |
| `archive.zip` + `archive.allow` | `ARCH.CONTENTS` |
| `archive.manifest` | `ARCH.PROVENANCE` |
| `fabrication_naming` | `FAB.LAYER_IDENTITY` |
| `release_generation.cpl_orientation` | `CPL.ORIENTATION` |
| `constraint_parity.rival_scan` | `CFG.NO_RIVAL_THRESHOLDS` |

`CFG.THRESHOLD_PARITY` always runs last and proves that every limit any gate
applied resolves to the manifest key it cited.

## 4. Express board rules as rule instances, not code

`NetTopologyRule` measures true electrical path length through the copper graph
from a driver pad pattern to load pad patterns:

```json
{ "id": "CLK", "net_regex": "^CLK_B\\d+$", "source_pad_regex": "^R\\d+\\.2$",
  "load_pad_regex": "^U\\d+\\.3$", "max_spread_mm": 5.0,
  "max_vias_per_net": 0, "permitted_layers": ["F.Cu"] }
```

`ConnectorContractRule` checks positions, rows, pitch, side, DNP/BOM state,
pin-to-net map, gender agreement across footprint id / 3D model / description /
value, and documentation consistency.

`PlacementRule` checks polar radius, azimuth grid and radial rotation for a
family of references, with an optional local offset so a feature — an acoustic
port, a fiducial, an optical centre — is measured instead of the footprint
origin.

## 5. Where your numbers go

| Kind of value | Where it belongs |
|---|---|
| A substantiated JLCPCB process limit | `profiles/jlcpcb/` in the toolkit, with source and date |
| Your board's tighter design target | your manifest |
| A threshold your own checker also uses | your manifest, with the checker reading it — `CFG.NO_RIVAL_THRESHOLDS` catches a second copy |
| A finding you have reviewed and accepted | a waiver, bound to the exact objects and digests |
| A gate whose finding you accept but want to keep measuring | `advisory_gates`, with a reason |

## 6. Release

```bash
python3 tooling/PCB_AutoDesignAndTest/run.py release board/manifest.json
```

The release copies your project, purges every previously generated output *from
that copy*, regenerates everything in one run, and publishes only after every
mandatory gate passes. A published release is a **candidate**, not an order.

## A worked example

`tests/fixtures/negative/microphone_array_reva/` is a complete, real project
with a manifest, an expected-results file and integrity hashes, packaged exactly
as described above. Read it for the shape — but note that it is
**intentionally defective**: the suite passes by proving the validator rejects
it, and it is not an example of a good design or a manufacturable board.
