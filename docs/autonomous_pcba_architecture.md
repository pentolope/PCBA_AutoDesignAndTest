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
  (`accept_for_comparison`, kept loose so failed candidates still
  teach), and permitted-to-win (`search_winner_eligible`): a
  winner passes EVERY correctness class through the quality gates
  AND every requirement-linked electrical assertion. Electrical
  evidence AVAILABILITY and requirement OUTCOME are separate
  classes: a trustworthy FAIL is valuable evidence and still a
  design failure, an unresolved assertion stays unknown, and the
  count of usable simulations is never rewarded without its
  verdicts.
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
  direction still concludes. Bound directions are ESTABLISHED,
  not asserted: for supported monotonic templates (today, the
  series divider) the direction is derived mechanically from the
  model's own declared resistance bound and re-verified at run
  time - a 'derived' claim the deriver cannot reproduce refuses;
  outside the templates an explicit 'assumed' declaration is
  required and recorded as an assumption, never theorem-level
  provenance. An assertion fed by a non-exact-bound model refuses
  to run without a declared bound - silence never defaults to an
  exact claim - and `assertions_claimable` reads the verdicts
  (None when nothing was asserted), so a numeric pass on a
  bounded value is never actionable by itself. Path uniqueness
  itself is bridge-rigorous: every resistive traversal element must
  be a bridge in the ELECTRICAL node graph (junction pivots pass,
  any rejoining detour refuses).
- **Parasitic result contract** (`pcbqa/parasitics.py`): every
  extracted electrical quantity is a CLAIM with declared
  semantics - exact (omissions refuse), bound, interval, or
  approximate (assumptions required, never able to decide a
  requirement) - plus model fidelity, provenance, applicability
  (out-of-domain refuses; blockages are records, not silence),
  and OPTIONAL requirement linkage yielding conservative
  PASS/FAIL/UNKNOWN. A metric with no linked requirement is
  descriptive: it ranks nothing and never becomes an invented
  gate. Comparisons refuse unmatched phenomenon, units, semantics
  or fidelity. First producers: traversal DC resistance,
  stackup-evidenced propagation-delay lower bounds, and the
  geometry-only coupling parallelism inventory
  (`pcbqa/coupling_geometry.py`) - coupled millimetres, never a
  crosstalk voltage.
- **Placement feedback** (`pcbqa/feedback.py`): downstream
  refusals become structured records - kind, references, pads,
  nets, location, required vs observed margin or refusal reason,
  suggested movables, movement domain, source artifact - that a
  DESCENDANT candidate consumes as targeted placement moves, with
  lineage recorded in its derivation. Cheap hard geometry is
  enforced where it is cheap (pad-accurate board-edge findings
  join placement repair); the fabrication DRC remains the
  authority.
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

### Router identity and the seed-search shape (measured 2026-08-28)

The router is evidence, not ambience: `pcbqa/krt.py` resolves ONE
KiCadRoutingTools source by declared order (explicit override →
configured development checkout → single active plugin
installation → refusal; a disabled installation never executes)
and records the identity a run actually used — VERSION, git sha,
dirty state, upstream base, the native grid_router binary's hash
and self-reported version probed under the SAME interpreter, and
that interpreter — with `identity_digest` for freshness closures.
A different sha, dirty state or native hash is a different router
and downstream routing-derived artifacts go stale with it. The
invoking interpreter is the consumer's configured KiCad Python,
verified to import pcbnew, never the ambient one.

The seed-search benchmark (frozen placements, same stage
arguments, historical evidence labelled as such) established the
durable shape and its measured caveats:

- Full routing is the scarce resource. Cheap filters in front of
  it are worth their cost: a critical-net probe (one scoped
  route.py call on the bare placement) ranked seven
  known-outcome placements with directional agreement
  (12 concordant pairs, 1 discordant, 8 ties), at ~1–2 minutes
  against 17–73 minutes of historical full routing each — but
  most of its resolution separates catastrophic placements from
  the frontier, it compresses badly-different placements at the
  bottom of its range, and it is blind to placement-fabrication
  dooms, so the LEGALITY AND FABRICATION GATES ALWAYS RUN FIRST
  and the probe is a filter, never a verdict.
- Static proxies misrank. In a real portfolio slate the
  crossings-best variant probe-routed worse than a
  crossings-worse one; probe reordering, not proxy score,
  chooses what graduates.
- The router's own repair loop optimizes routability, not intent.
  An external accept gate (the consumer's placement policy,
  courtyard collisions, pad-accurate edge clearance) must hold
  veto power: measured, four of five repair rounds "improved"
  failures only by scattering a decoupling/functional-block
  structure and were rejected and reverted. A router-side
  improvement claim is confirmed only by the board-file
  connectivity arbiter, never by the router's own tally.
- Externally produced placements (portfolio variants, repair
  outputs) enter the pipeline only through an ingest path that
  judges them exactly like generated ones — measured: a
  portfolio "viable" winner violated the consumer's decoupling
  proximity constraint and was refused before any routing was
  spent, because the router's viability gates cannot see
  consumer semantics.
- Route outputs are not byte-deterministic run to run; identical
  inputs reproduced the identical missing-net set (semantic
  determinism) and that is the comparison the evidence uses.
- Compute is a first-class artifact: `pcbqa/compute.py` keeps
  spend in disjoint categories whose sum must equal the measured
  total or the summary refuses, so "compute avoided" claims bind
  to a ledger that adds up.

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
