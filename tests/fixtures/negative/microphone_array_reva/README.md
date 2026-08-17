# microphone_array_reva - intentionally defective negative fixture

**This project is defective on purpose. The test passes when the toolkit
rejects it.**

It is a curated copy of Rev A of a 16-channel PDM microphone-array carrier,
derived from
[PCB_MicrophoneArrayV2](https://github.com/pentolope/PCB_MicrophoneArrayV2) at
commit `9ba07cf720ed0bbb2023d121d544f0144bba7b2f`, path
`verification/fixtures/reva/project`.

## What this is

A real, complete KiCad project with a real fabrication package, carrying a
known set of real defects. It exists so the toolkit can be exercised end to end
against something a synthetic fixture cannot imitate:

- that a genuinely defective board is **rejected**, gate by gate, for the
  reasons recorded in `expected.json`;
- that a release attempt on it **fails closed** and publishes nothing;
- that fixture integrity, source closure, lifecycle, clean-room and archive
  behaviour all work on a project of realistic size and shape;
- how a project, its manifest, its expected results and its integrity hashes
  are packaged so another board can be onboarded the same way.

## What this is not

- **Not a manufacturing-valid design.** Do not fabricate it.
- **Not a production release input.** Never point a release at this fixture.
- **Not an example of how to design a microphone array.** It is an example of
  how the toolkit detects failure. Its defects are the point.
- **Not a byte-for-byte historical archive.** The upstream repository at the
  commit above is the historical record. This copy has been curated (below).

## Curation

Assets belonging to a superseded external autorouter were removed when this
fixture moved into the toolkit, which supports KiCad Routing Tools only and
carries no reference to that router.

Removed (14 files): `pcbflow.json`; `tools/` copies of `apply_escapes.py`,
`close_gaps.py`, `merge_routing.py`, `patch_dsn.py`, `pcbflow.py`,
`kicad_specctra.py`; and the whole of `generated/route/`.

Edited to drop references to it (4 files): `README.md`, `docs/routing.md`,
`docs/sources.md`, `tools/gen_pcb.py`. In each case the technical reasoning was
kept and only the tool's name and its version-specific workarounds were
removed.

Everything else is unchanged: board geometry, the netlist, the release archive,
and **every defect the negative tests depend on** — including the divergent
threshold constants in `tools/check_routes.py` and the Python-model derivation
in `tools/make_release.py`, both of which are supposed to be caught.

`HASHES.json` was regenerated for the curated file set (84 files → 70). Its
`curation` block records exactly what was removed and edited, and the
regeneration was gated on a reconciliation that showed **zero** unexplained
digest changes among the files that were kept.

## Running it

Through the same public interface any board uses:

```bash
python run.py validate tests/fixtures/negative/microphone_array_reva/manifest.json
```

Expected: exit status 1, verdict `REJECTED`. A release attempt likewise exits
nonzero, writes `DO_NOT_ORDER.txt`, and publishes nothing.

`expected.json` records the per-gate status this fixture must produce. If a
gate's result moves, that is a change in the toolkit, not in the fixture —
**investigate the toolkit before touching the expectation.** Never edit an
expected result to make a suite green.
