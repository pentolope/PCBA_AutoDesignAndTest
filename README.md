# pcbqa — board-agnostic KiCad / JLCPCB verification and release

A fail-closed validator that takes a native KiCad project plus a declarative
manifest and produces a machine-readable pass/fail matrix, generates the
fabrication outputs a release commit carries, and proves that a commit is one a
release tag may name.

**A release is a Git tag.**

- **commit** = the complete project state
- **committed fabrication artifacts** = the exact historical manufacturing
  outputs, kept as ordinary files so an old board can be refabricated years
  later without regenerating anything
- **Git tag** = the release identity
- **toolkit release gates** = engineering proof that the tagged state is
  acceptable

Git already provides immutable history, branches, diffs, rollback and exact
source identity. This toolkit does not reimplement any of that; it answers the
engineering question Git cannot: are these artifacts the ones this design
produces, and does this design pass.

KiCad is the sole design authority: nothing is believed because a report, a
label or a Python model says so. This repository contains no production board —
concrete designs appear only as isolated fixtures and examples.

Consume it from a board repository as a pinned Git submodule at
`tooling/PCBA_AutoDesignAndTest`. It carries one submodule of its own:
KiCad Routing Tools at `tooling/KiCadRoutingTools`, pinned to a commit on its
`pcba-autonomy` branch, so clone recursively.

## Commands

Ubuntu, and the system Python 3: the `kicad` package installs `pcbnew` into
this interpreter's `dist-packages` and `kicad-cli` onto PATH, and Shapely is
`python3-shapely`. See `docs/prerequisites.md`.

```bash
python3 run.py preflight
```
```bash
python3 run.py selftest
```
```bash
python3 run.py build <manifest.json>
```
```bash
python3 run.py validate <manifest.json> --write
```
```bash
python3 run.py release-check <manifest.json>
```

`<manifest.json>` is a path. For this repository's own fixtures a bare name also
works — `portability`, `clean`, or a negative fixture's directory name.

## The release workflow

```text
develop on a branch
        ↓  run.py build          regenerate the fabrication outputs into the tree
        ↓  run.py validate -w    judge the design and those exact artifacts
        ↓  git commit            artifacts included; they are ordinary files
        ↓  run.py release-check  clean tree, exact submodules, gates pass now
        ↓  git tag -a            the release exists
```

There is no "published but unsealed release candidate". A branch is the
work-in-progress mechanism; a tag is the release.

`build` runs KiCad against a private copy — the design is never opened for
writing — and installs into the paths the manifest declares, all or nothing. It
also writes `fabrication.json`: the digest of every artifact it produced and the
source closure it produced them from.

`validate` exits nonzero on a blocking result. It is read-only unless `--write`
is given, so ordinary development validation never touches the working tree.

`release-check` writes nothing and changes no Git state. It exits zero only when
Git can say the tree is exactly the commit (submodules included), every release
artifact and every piece of required evidence is tracked, every mandatory gate
passes now, and the committed verdict is about this same design. Creating the
tag is a deliberate act and is left to the user.

To re-verify a historical release, check the tag out and run `release-check`
against it: the artifacts, the reports and the verdict are all in that commit,
and nothing has to be regenerated. Prefer annotated tags (`git tag -a`, or
`-s` to sign); protecting them from being moved or deleted is a property of the
remote — GitHub tag-protection rules, or `receive.denyDeleteTag` on a bare
repository — not something a validator can enforce locally.

Scratch for one invocation lives in `out/<board_id>/<run_id>/` and nothing
durable is kept there.

## Layout

| Path | Role |
|---|---|
| `pcbqa/core.py` | statuses, results, manifest, provenance, gate registry, reporting |
| `pcbqa/geom.py` | native pad/mask/via geometry via KiCad's own effective shapes |
| `pcbqa/gerber.py` | independent Gerber X2 + Excellon readers, incl. an aperture-macro interpreter |
| `pcbqa/canonical.py` | checkout-independent digests, driven by `.gitattributes` |
| `pcbqa/closure.py` | the source closure: what a result depends on |
| `pcbqa/build.py` | generating the fabrication outputs and installing them |
| `pcbqa/artifacts.py` | which committed files are the release artifact set |
| `pcbqa/release.py` | the Git preconditions of a release tag |
| `pcbqa/connectivity.py` | copper graph built from geometric intersection |
| `pcbqa/rules/` | reusable rule types: `NetTopologyRule`, `ConnectorContractRule`, `PlacementRule` |
| `pcbqa/gates/` | the gates themselves |
| `profiles/jlcpcb/` | substantiated JLCPCB capability and process data |
| `schemas/` | manifest and KiCad report schemas |
| `tests/manifests/` | manifests for this repository's own fixtures |
| `tests/fixtures/` | fixtures, including one curated negative integration fixture |
| `tests/paths.py` | where test assets live — one place, so fixtures can move |
| `tests/consumer.py` | optional external board for tests that need a real design |

## Scope: JLCPCB only

There is no multi-manufacturer abstraction and none is wanted. `profiles/jlcpcb/`
is organisation, not indirection.

A value only becomes a JLCPCB-wide requirement if it carries its provenance:
source, URL or document id, retrieval date, units, applicable service or
process, and any exceptions. A value is never promoted because one board happens
to use it — it is classified instead as configurable toolkit policy, a selected
board constraint, a conservative design target, or a board waiver, and left with
the board. The existing split between `via_mask.design_target_mm` (a board's
choice) and `via_mask.process.limit_mm` (a substantiated JLCPCB limit) is the
pattern to follow.

No live network lookup may change a validation or release result.

## Onboarding another board

Minimum manifest:

```json
{
  "schema_version": 2,
  "board_id": "my_board",
  "constraint_version": "v1",
  "project_root": "..",
  "tools": { "kicad_cli": "<path to kicad-cli>" },
  "sources": { "pcb": "my_board.kicad_pcb" },
  "board_origin_mm": [0.0, 0.0],
  "documentation_globs": [],
  "waivers": []
}
```

That alone runs the geometry-only gates. Every other gate stays
`NOT_APPLICABLE` **with a reason** until you add its policy block. Absence is
never a silent pass, and the gate still appears in the matrix.

`CFG.THRESHOLD_PARITY` always runs last and proves every limit any gate applied
resolves to the manifest key it cited. `CFG.NO_RIVAL_THRESHOLDS` proves no
checker outside the manifest defines its own copy of one.

## The generic/board split is enforced, not documented

`tests/test_suite.py::GenericSourceHygiene` extracts identifiers from every
manifest this repository ships — fixtures included — and fails if any appears in
`pcbqa/`, `schemas/` or `profiles/`. No board name is on its allowlist, and none
may be added.

Tests that need a real, released board take it from outside:

```bash
set PCBQA_CONSUMER_MANIFEST=<path to a board manifest>
```

With nothing set they skip, with a reason. That is how the toolkit keeps
coverage of installed-release behaviour without keeping a board.

## Fixtures are held to an exact inventory

`PROV.FIXTURE_INTEGRITY` requires every recorded file present and unchanged, and
every present file recorded. A stray `__pycache__` inside a fixture is therefore
a gate failure — do not run `compileall` over `tests/`.

`tests/fixtures/negative/microphone_array_reva/` is a curated real project that
is **intentionally defective**; the suite passes by proving the validator
rejects it. See its README before touching it.
