# PCBA_AutoDesignAndTest — generic KiCad/JLCPCB design verification toolkit

## Mission

A board-agnostic, fail-closed verification, routing and release toolkit for
KiCad projects manufactured by JLCPCB. It is consumed by board repositories as
a pinned Git submodule at `tooling/PCBA_AutoDesignAndTest`.

It contains **no production consumer board**. Concrete boards are permitted
only as explicitly isolated examples or test fixtures, under `tests/fixtures/`
and `examples/`.

It **never modifies an authoritative source board in place.** Routing or
transformation operations may write only to fresh candidate paths.

The toolkit **never imports from a consumer repository.** Dependencies run one
way: a board may call the toolkit; the toolkit knows nothing about any board.

## Genericity is the prime directive

No board name, reference designator, net name, coordinate, dimension, expected
count, filename or board-specific waiver may appear in:

- `pcbqa/` — all production code, including gates and rules
- `schemas/`
- `profiles/`
- any default value

Concrete identifiers belong in a board's manifest, or inside `tests/fixtures/`
and `examples/`. This is enforced mechanically by
`tests/test_suite.py::GenericSourceHygiene`, which extracts identifiers from
every manifest the repository ships — fixtures included — and fails if one
appears in framework source. **That test failing is a real failure. Do not add
a board's name to its allowlist.**

A test that needs a real board takes it from outside, through
`tests/consumer.py` and the `PCBQA_CONSUMER_MANIFEST` environment variable. It
skips with a reason when no consumer is registered. Do not import a consumer's
files by path.

## Authority

Native KiCad files are the design authority. The toolkit measures; it does not
decide what the design should be, and it never repairs a board. Nothing is
believed because a report, a label or a Python model says so.

Every gate reads its policy from the manifest. A gate whose policy block is
absent reports `NOT_APPLICABLE` **with a reason** — absence is never a silent
pass, and the gate still appears in the matrix. (A stricter model, where a
missing required block is an `ERROR`, is a possible future change; it is not
the current behaviour and must not be introduced as a side effect of other
work.)

## Fail-closed

- A gate that raises is `ERROR` and blocks the release.
- An unknown Gerber aperture, macro primitive or open region raises rather than
  being skipped. Silently skipping macro apertures is precisely how an earlier
  manual review missed a via-in-pad population.
- A missing manifest key raises. There are no hidden defaults.
- `build` installs all of its output or none of it: a build that could not
  produce a complete set leaves the previous artifacts alone.
- `release-check` exits nonzero unless every requirement is met, and it never
  creates a tag. It writes nothing and changes no Git state.

## A release is a Git tag

- **commit** = the complete project state
- **committed fabrication artifacts** = the exact historical manufacturing
  outputs, kept as ordinary files so an old board can be refabricated later
  without regenerating anything
- **Git tag** = the release identity
- **release gates** = engineering proof that the tagged state is acceptable

Git supplies immutable history, branches, diffs, rollback and exact source
identity. Active alternatives belong on branches, history in commits, and
release identity in tags. The toolkit owns the engineering validity of the
state a tag names.

What the toolkit owns instead is engineering correctness:

1. the working tree is clean;
2. submodules are clean and exactly at their committed gitlinks;
3. the committed fabrication artifacts correspond to the current design, proven
   by the source closure recorded in `fabrication.json`;
4. required ERC/DRC and toolkit gates pass;
5. required fabrication checks pass;
6. stale fabrication outputs cannot silently be released;
7. the exact committed artifacts being released are the ones that were
   validated;
8. tool, model and fabricator provenance is recorded where useful.

`fabrication.json` is the binding, and it deliberately names no tag and no
commit: it is committed *before* the tag and the commit that carry it exist, so
its provenance is content-addressed - artifact digests and a source closure -
rather than a name that would be a lie at the moment it was written.

## Never weaken a check

Do not relax a threshold, add a waiver, suppress a finding, or edit an expected
result to make a test pass. A negative fixture passes the suite **by being
rejected**; if its expectation moves, investigate the toolkit first, and record
the reason for any change beside the fixture.

## JLCPCB is the only manufacturer target

`profiles/jlcpcb/` holds the one supported manufacturer's sources, parser and
catalog. Manufacturer-independent physics remains in generic modules such as
`pcbqa/transmission_line.py` and `pcbqa/overlay_reference.py`.

The catalog is committed data. `fab refresh` fetches, parses and shows a
semantic diff in a scratch directory. Adoption replaces
`profiles/jlcpcb/catalog` as one exact catalog/evidence set before commit; a
directory merge is incorrect because it can retain orphan evidence. The loader
requires every referenced evidence file to exist and hash correctly and refuses
every unreferenced entry. The commit is the approval and Git is the history.

Every value promoted to a JLCPCB-wide requirement must record: the
authoritative source, a URL or document identifier, the retrieval or effective
date, units, the applicable service or process, and any conditions or
exceptions. A value is **never** promoted merely because one board uses it. If
it cannot be substantiated, classify it instead as configurable toolkit policy,
a selected board constraint, a conservative design target, or a board waiver —
and leave it with the board.

No live network lookup may change a validation or release result. Catalogue and
capability evidence is frozen, cached and hashed; a cache miss is an error, not
a fetch.

## Routing

KiCad Routing Tools is the only supported router, and it is a submodule of
this repository at `tooling/KiCadRoutingTools`, pinned to a commit on its
`pcba-autonomy` branch. `pcbqa.krt` resolves it by a declared order - explicit
override, `PCB_KRT_PATH`, a consumer's configured checkout, that submodule,
then exactly one active plugin installation - so a recursive clone can route
with no sibling checkout and no absolute path. It is vendored, never edited
here: changes belong upstream, and the pin moves deliberately.

It is also the one directory a build must not copy. A build runs ERC, DRC and
the exports and never routes, so the router is not a build input. Nothing
copies it, because a build stages only the design reached from the declared
sources and the router is not one of them. Which router drew the copper stays
provenance, recorded at routing time by `krt.provenance`.

Every routing attempt records: source board digest; project and configuration digests;
KiCad and KiCad-Python versions; the router's package identity, resolved path
and source digest; the exact plan and its hash; stage net selections; the
environment; full stdout, stderr, exit code and runtime; every generated
candidate with its configuration; post-route DRC; and the declared metrics used
to accept or reject.

- Never overwrite the authoritative source board. Route only into fresh
  candidate paths.
- Never silently relax DRC or JLCPCB manufacturing constraints.
- No hidden or unrecorded retry loops.
- Selection among multiple candidates uses declared metrics. One candidate is
  valid when the board's manifest configures one; multiple are recommended
  where routing is nondeterministic, but never required.
- **Automated routing must not move, remove, resize, redrill, re-layer, retype
  or reassign an existing via.** A needed change is made in the authoritative
  input and the candidate regenerated.
- Never claim bitwise determinism that has not been demonstrated. Where a
  router is not bit-reproducible, compare candidates semantically and record
  the differences.

## One evidence model

`pcbqa/claim.py` is the shared shape a producer of a number states its claim
in: phenomenon, scope, units, how well it is known (exact / lower bound /
upper bound / interval / approximate / unknown), evidence class, provenance,
applicability, assumptions, omissions, optional requirement - and the
conservative verdict rule that decides from it.

Propagation paths and vias, component traversals, extracted DC resistance,
geometry-only coupling and simulated measurements produce this shape directly.
Simulation model records use the same evidence facts through
`pcbqa/sim/model_registry.py`; scenario measurements declare shared knowledge,
and ngspice assertions use the shared verdict. A verdict records whether a PASS
was exact or conservatively derived from a bound, and whether a bound was
derived or assumed.

Evidence classes may remain phenomenon-specific. Propagation has a local
ordering for propagation-delay evidence; simulation coverage accepts explicit
sets per phenomenon. There is no quality ranking across unrelated phenomena.

## Constraint policy

Engineering policy comparisons use typed `Constraint` methods. The selftest
runs a focused AST audit over gate modules to catch policy-looking numeric
comparisons that bypass those methods. Intrinsic mathematical and algorithmic
constants use `implementation_constant(value, rationale)`. This audit is a
toolkit development check, not a consumer validation gate.

## Fixtures

`tests/fixtures/` holds the toolkit's own test material. Fixture integrity is
held to an **exact inventory**: every recorded file present and unchanged, and
every present file recorded. A stray `__pycache__` inside a fixture is
therefore a gate failure — never run `compileall` over `tests/`, and never
commit byte-compiled files.

A curated real project may serve as a negative fixture. It must carry a README
saying plainly that it is intentionally defective, that the test passes when
the toolkit rejects it, that it is not manufacturing-valid, and that it must
never be a production release input. Its integrity hashes are regenerated only
alongside a written reconciliation of what changed and why.

## Versioning

Manifest `schema_version` and profile changes are additive. A consumer pinned
to an older commit must keep working.

## Git

No commit, push or tag without explicit authorisation from the user.

## Definition of done

- Self-tests pass standalone, with no consumer registered.
- The portability fixture and the onboarding example demonstrate that a
  materially different JLCPCB-targeted board works **without modifying toolkit
  production code**.
- `GenericSourceHygiene` passes with no board name on its allowlist.

## Headless discipline

No code path may raise a dialog a human must dismiss: a modal box on
an unwatched screen freezes an autonomous run (KiCad's debug asserts
- e.g. `PCB_VIA::GetWidth()` without a layer argument - become
blocking wxWidgets alerts whenever a GUI application object exists
and a display is reachable). Every entry point calls
`pcbqa.headless.suppress_blocking_ui()` first (`run.py` does this
for all commands); long-running consumer scripts must do the same.
When no wx application exists a `wx.AppConsole` is created - never a
GUI `wx.App`, which terminates the whole process outright when
$DISPLAY is unset or unreachable, before any handler runs - so every
command works identically with no display, a dead one, and a live
one. A console application's assert handler has no dialog branch:
asserts are printed to stderr and continued, never modal and never
silent (an assert is a report point, not a control-flow guard; the
continue path is the same one the dialog's own button took). When a
GUI application already exists - this interpreter embedded in KiCad
itself - its assert mode is routed to LOG instead. The protection is
QUERYABLE STATE (`headless.protection_state()`): the canary asserts
`modal_unreachable` BEFORE triggering anything, so a regression
fails in milliseconds by name - dialogs are never detected or
excluded by timeouts - and only then triggers the known misuse to
prove the assert is REPORTED on stderr, not silenced. Via geometry
reads always pass a layer argument.

## Running

Ubuntu, and the system Python 3. `pcbnew` comes from the `kicad`
distribution package and Shapely from `python3-shapely`; both land in
this interpreter's `dist-packages`, so there is no bundled KiCad
interpreter to hunt for. See `docs/prerequisites.md`.

```bash
python3 run.py preflight
```

```bash
python3 run.py selftest
```

```bash
python3 run.py build <manifest>
```

```bash
python3 run.py validate <manifest> --write
```

```bash
python3 run.py check-board <manifest>
```

```bash
python3 run.py release-check <manifest>
```

The manifest is checked against `schemas/manifest.v2.json` before any command
runs: a key the toolkit does not implement is refused by name. Board-local
data lives under `x_`-prefixed keys; `description`, `note`, `why` and
`rationale` are annotation strings, allowed at every level. Keep the schema
exactly as wide as what `pcbqa/` reads — a key added to one must be added to
the other in the same change.
