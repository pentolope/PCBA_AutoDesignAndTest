# PCB_AutoDesignAndTest — generic KiCad/JLCPCB design verification toolkit

## Mission

A board-agnostic, fail-closed verification, routing and release toolkit for
KiCad projects manufactured by JLCPCB. It is consumed by board repositories as
a pinned Git submodule at `tooling/PCB_AutoDesignAndTest`.

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

A test that needs a real, released board takes it from outside, through
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
- `release` publishes only after every mandatory gate has passed, by renaming a
  candidate directory into a name that has never existed. A failed attempt
  removes its own build directory, keeps `DO_NOT_ORDER.txt`, and leaves any
  previously published release byte-identical.

## Never weaken a check

Do not relax a threshold, add a waiver, suppress a finding, or edit an expected
result to make a test pass. A negative fixture passes the suite **by being
rejected**; if its expectation moves, investigate the toolkit first, and record
the reason for any change beside the fixture.

## JLCPCB is the only manufacturer target

Do not build a multi-manufacturer abstraction, provider plug-in layer, or
additional manufacturer profiles for hypothetical future use. `profiles/jlcpcb/`
is organisation, not indirection.

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

KiCad Routing Tools is the only supported router. The superseded external
autorouter and its Specctra DSN/SES exchange have been removed, and neither is
to be reintroduced.

Every attempt records: source board digest; project and configuration digests;
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
- The working tree names no superseded autorouter. The only permitted mentions
  of removed tooling are the curation records inside a fixture — `HASHES.json`,
  `PRE_NORMALIZATION_HASHES.json` and the fixture README — which exist to say
  what was taken out and why.

## Headless discipline

No code path may raise a dialog a human must dismiss: a modal box on
an unwatched screen freezes an autonomous run (KiCad's debug asserts
- e.g. `PCB_VIA::GetWidth()` without a layer argument - become
blocking wxWidgets alerts whenever a display is reachable). Every
entry point calls
`pcbqa.headless.suppress_blocking_ui()` first (`run.py` does this
for all commands); long-running consumer scripts must do the same.
Asserts are routed to LOG mode - printed to stderr and continued,
never modal and never silent (an assert is a report point, not a
control-flow guard; the continue path is the same one the dialog's
own button took). The protection is QUERYABLE STATE
(`headless.protection_state()`): the canary asserts the wx assert
mode BEFORE triggering anything, so a
regression fails in milliseconds by name - dialogs are never
detected or excluded by timeouts - and only then triggers the known
misuse to prove the assert is REPORTED on stderr, not silenced. Via
geometry reads always pass a layer argument.

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
python3 run.py validate <manifest>
```
