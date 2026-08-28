# Autonomous PCBA development — architecture

**Status:** maintained architecture and roadmap. This supersedes the
2026-08-26 planning brief (`AUTONOMOUS_PCBA_AGENT_ARCHITECTURE.md`,
retired) — reconciled against the system that now exists. Where this
document and the code disagree, the code and its tests are the
authority; fix the document.

**Goal:** a toolkit that lets an AI develop functional PCBAs with
minimal human interaction, using structured evidence to decide what
is known, what is unresolved, what action is allowed, what design to
try next, and whether a board is ready for fabrication.

## 1. Division of labor (durable)

- **AI agent** — electrical intent, constraint authorship, failure
  interpretation, architecture choice. It does not draw copper and
  does not hand-place parts except deliberately fixed structures.
- **Numerical engines** — placement search, routing, tuning
  (currently KiCadRoutingTools). Their claims are never trusted:
  the board file is the arbiter, and every invocation carries an
  attempt identity (input SHA, tool SHA, configuration, unique
  output) so a stale artifact can never masquerade as fresh work.
- **Simulation engines** — ngspice for board-level electrical
  simulation (a real engine is discovered via `NGSPICE_LIBRARY`,
  PATH, or the shared library beside the interpreter — KiCad's own
  bundled engine under KiCad's python); Verilator for RTL execution
  (`VERILATOR` override or PATH). Absence is
  `backend-unavailable` — never a pass, never a fabricated failure.
- **This toolkit** — the authority for provenance, constraints,
  connectivity, coverage, condition semantics, validation gates and
  release lifecycle. Fail-closed everywhere: refusals over guesses,
  unknown keys refuse, silence never reads as evidence.
- **KiCad** — the authoritative board representation. Native files
  are the design authority; generators produce candidates.
- **Headless discipline** (`pcbqa/headless.py`) — no code path may
  raise a dialog a human must dismiss: wx asserts and Windows error
  boxes are disarmed at every entry point, and a suite canary fails
  if the blocking path ever becomes reachable again. An autonomous
  pipeline that can be stalled by a popup is not autonomous.

## 2. Repository organization (durable)

The PCBA repository owns the physical/electrical contract: board,
schematic, fabrication profile, constraints, behavioral boundary
models (the board's expectations at its interfaces), simulation
scenarios, extracted parasitics, and the assumptions and coverage of
every model. A firmware repository owns production RTL/firmware and
may consume the PCBA repository as a submodule, substituting its RTL
as the DUT while the PCBA-owned checker — the contract — stays
byte-identical (implemented: `pcbqa/sim/digital.py`; a fingerprinted
contract, a generated wrapper and main, DUT swapped per run).

## 3. Evidence contracts (implemented; extend, do not weaken)

- **Connectivity** (`pcbqa/connectivity.py`): a net's real state is
  computed from copper intersection — `no-pads`, `no-copper`,
  `partial-copper`, `connectivity-complete` (every pad in ONE
  connected component; filled zones participate). Completeness never
  implies required topology — `NetTopologyRule` and the
  electrical-path gates own that judgment. "Has tracks" is never
  routing completion, and router logs are never the arbiter.
- **Coverage** (`pcbqa/sim/fidelity.py`, `scenario.py`): phenomenon
  → evidence class, judged per measurement over its contribution
  closure. Every contributor must account for each required
  phenomenon: covered at an accepted class, explicitly
  `not-applicable`, or explicitly `unsupported` — an unaccounted
  phenomenon blocks. One strong model never blesses a weak or
  silent one. (This supersedes the planning brief's flat fidelity
  ladder.)
- **Operating conditions**: models declare `fixed-reference` or
  `parameterized` condition support; undeclared is not insensitive.
  Results carry a usability policy — assertions passing never
  overrides an inapplicable condition, `usable_for_release` is
  false at this layer, always.
- **Physical provenance** (`pcbqa/extract.py`): every physical
  input is a provenance record; `approved-evidence` can only be
  minted by resolvers that read the approved catalog, and TRUST is
  verified against the actual snapshot (`verify_approved_parameter`)
  — a well-shaped forgery refuses. Derived models embed their full
  derivation with truthful roots (mixed stays mixed) and carry the
  physical-input digest in their identity.
- **Benchmark** (`pcbqa/benchmark.py`): typed measured/unmeasured
  metrics with stable semantic definition identities;
  `compare_reports` is the only comparison path and refuses unknown
  schemas, different resolved physical constructions, different
  metric definitions, or mismatched units. Unmeasured never becomes
  zero; partial copper never pairs with a complete route.
- **Zone inheritance** (`pcbqa/zone_inheritance.py`): a declarative,
  EXECUTABLE policy decides which zones a derived candidate inherits
  as architecture and which placement-derived zones survive only
  near requirement-fixed geometry; an unclassified zone refuses.
- **Netlist contract** (`pcbqa/netlist_contract.py`): required
  connectivity is derived from the AUTHORITATIVE product intent -
  every footprint, pad and pad-to-net assignment - never from the
  candidate, so a dropped part or an invented net can never shrink
  or reshape the denominator; parity differences are named
  machine-readably and judged before anything else.
- **Progression** (`pcbqa/progression.py`): candidate advancement is
  ordered correctness classes - NETLIST PARITY against the
  authoritative contract first, then placement policy, critical
  structures by POLICY-owned path/topology truth (net connectivity
  is never sufficient), BOARD-required connectivity (the benchmark
  inventory never stands in for it), hard fabrication geometry,
  blocking gates, quality gates, usable electrical evidence - with
  a lexicographic rank key no scalar can override. Three states
  stay distinct: ready-for-next-stage, worth-comparing
  (`accept_for_comparison`), and permitted-to-win
  (`search_winner_eligible`) - a measured candidate whose critical
  truths are failed or unresolved is never presented as best.
- **Freshness** (`pcbqa/freshness.py`): derived artifacts carry a
  deliberate producer closure (named code, inputs, schema digests);
  consumers refuse stale or tampered artifacts and are told exactly
  which dependency moved. Unrelated changes invalidate nothing.
  Identities are canonical per kind - JSON by canonical
  serialization, text and KiCad files by LF-normalized content,
  binaries by raw bytes - so byte conventions never masquerade as
  change; and freshness is TRANSITIVE: a downstream closure names
  its upstream artifacts by canonical content, so a regenerated
  gates artifact moves the decision, a regenerated decision moves
  the search, link by link. No artifact may outrun its evidence.
- **Bound-classified verdicts** (`pcbqa/extract.py`,
  `pcbqa/sim/scenario.py`): a value produced under omitted positive
  physics is a BOUND, not a truth. Path-scoped resistance declares
  `resistance_bound` ('lower' with via barrels omitted, 'exact',
  or 'uncertain' when junction ambiguity is nonzero - a symmetric
  uncertainty is never a one-sided bound), and every assertion
  classifies as exact-PASS / exact-FAIL / conservative-PASS /
  conservative-FAIL / unresolved - the omission's optimistic
  direction can never manufacture a PASS, while the pessimistic
  direction still concludes. The scenario author must DECLARE the
  measurement's `value_bound` (its direction follows from the
  circuit): an assertion fed by a model that declares a non-exact
  bound refuses to run without one - silence never defaults to an
  exact claim - and the result policy's `assertions_claimable`
  reads the verdicts, so a numeric pass on a bounded value is
  never actionable by itself. Path uniqueness
  itself is bridge-rigorous: every resistive traversal element must
  be a bridge in the ELECTRICAL node graph (junction pivots pass,
  any rejoining detour refuses).
- **Implementation identity** (`pcbqa/core.py`): every validation
  document stamps the toolkit commit (and dirty state) that judged
  it - the same board under a different implementation is a
  different claim.
- **Assumptions**: ideal scenario primitives are first-class
  declared assumptions; a result depending on an undeclared or
  unaccepted assumption is structurally unusable for a design
  decision, however its assertions read.

## 4. Placement and routing (implemented core, open frontier)

Implemented: a semantic constraint vocabulary
(`pcbqa/placement.py`) with `evaluate_placement` judging actual
candidate positions and courtyard collision checking; consumers
build constraint-satisfying seed placements (fanout-aware series
parts facing their targets), translate constraints into optimizer
locks, let the quench move the rest, and repair what it breaks —
the toolkit proves the result or the candidate fails.

Routing is staged by board semantics with bounded runtimes,
checkpointed per attempt, judged by connectivity classification,
finished by a scoped cleanup stage. Declared fabrication minima are
HARD inside the loop: the router may not rewrite a candidate's DRC
floors (the authoritative project rules travel beside every
artifact), a per-stage geometry DRC at those rules gates checkpoint
advance, and connectivity obtained below a declared minimum is
never stage success. Router heuristics that misclassify parts (e.g.
BGA auto-detection walling a microphone's own clock pad) are
disabled with the finding recorded.

The critical-topology planner exists (`pcbqa/critical_topology.py`)
for its first structure class: verified LOCAL copper the general
router cannot produce at declared values - guard-ring pad escapes,
process-checked stitching vias (mask annulus, hole-to-hole,
keepouts, actual plane fill), last-mile group joins - generated
then exactly re-verified, with the gates still the authority. Its
obstacle model is netclass-aware: every foreign obstacle is grouped
by the clearance REQUIRED AGAINST IT (the DRC's pairwise-max rule),
a through via clears foreign copper on every layer it traverses,
and foreign filled zones are deliberately not obstacles (refill
semantics; the post-stage DRC on the refilled board remains the
authority). A planner that verifies one scalar builds copper the
DRC rejects - that lesson is now a regression test.
Open frontier, in the brief's durable ordering: matched-length
branch/tree generation (the declared cross-branch spread limits),
length/time tuning mapped from electrical intent, and richer
optimizer freedom beyond lock-and-repair.

## 5. Simulation strategy (durable, partially implemented)

Mixed-fidelity composite board model: each element at the strongest
justified coverage, every model carrying provenance, coverage
dispositions and condition declarations. Implemented today:
deterministic ngspice decks (op/tran), extracted DC interconnect
models entering scenarios with automatic or recorded two-terminal
assertions, and Verilator behavioral-contract execution. The
planning brief's scenario families (power sequencing, clock
networks, digital interfaces, sensor models) and the digital↔
electrical bridge (RTL transitions driving electrical boundary
models through extracted interconnect) remain the roadmap — each
lands only with its evidence contract.

Targeted EM (openEMS) stays the intended producer for
`interconnect_si`: extract specific structures (ports, meshes,
geometry from the board), never whole-board full-wave. Nothing may
claim SI coverage until that producer exists.

## 6. The A/B benchmark (running)

Board A — the frozen authoritative consumer board — against Board B
candidates generated by the autonomous workflow under identical
requirements, measured by the identical extractor under the
identical resolved physical construction, compared only through the
typed contract. Ranking is completeness-first: critical-path
connectivity, overall connectivity, gates, then electrical and
geometric quality — a candidate with partial routes never beats a
complete one by carrying less copper.

## 7. The loop (target invariant)

```text
candidate has proven electrical connectivity
    → valid topology
    → DRC/process compliance
    → extracted evidence
    → applicable simulation
    → comparable metrics
    → autonomous design decision
    → next candidate
```

A board graduates from one confidence level to the next only when
the corresponding evidence exists; every failure points back to a
design variable the agent can change; and the final PCBA is
supported by reproducible evidence rather than visual plausibility.
