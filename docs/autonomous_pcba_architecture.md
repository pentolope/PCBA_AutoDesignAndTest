# Autonomous PCBA development — implemented architecture

**Status:** maintained description of the current toolkit. Code and behavioral
tests are authoritative when this document becomes stale.

**Goal:** let an autonomous agent distinguish established facts, conservative
bounds, unsupported physics and unresolved requirements while validating the
PCBA state it is given.

## 1. Authority and repository roles

Native KiCad files are the design authority. The toolkit measures them and
produces candidates or fabrication outputs; it does not repair authoritative
design files during validation. A consumer repository owns its schematic,
board, manifest, constraints, models and fabrication outputs. The toolkit is
generic and imports nothing from consumers.

Branches carry active alternatives and experiments, commits carry history, and
a Git tag over a valid committed state is a release. A release commit includes
the fabrication outputs and `fabrication.json` that binds their digests to the
source closure. `release-check` requires a clean, coherent Git state and
re-evaluates the required gates without creating a tag or changing Git state.

KiCad operations used during fabrication can rewrite a board, notably DRC with
zone refill and board save. `design_inputs()` therefore follows only the
manifest-declared design and its recursive project references.
`stage_design()` copies that targeted closure to scratch. Every source and
destination is authorized with resolved paths against the declared project
root or staging root; traversal, absolute and symlink escapes refuse.

## 2. Shared evidence and numeric claims

`pcbqa/claim.py` is the common numeric contract. A claim carries:

- phenomenon, scope and units;
- numeric knowledge: exact, lower bound, upper bound, interval, approximate or
  unknown;
- evidence class and provenance;
- applicability: applicable, unsupported or not applicable;
- assumptions and omitted contributions;
- a stated basis for bounded and approximate knowledge;
- an optional requirement.

One conservative verdict evaluates linked requirements as PASS, FAIL or
UNKNOWN. It records whether a conclusion is exact or bound-derived. A one-sided
bound decides only in the direction it establishes, an interval only when the
whole interval decides, and approximate or unknown knowledge cannot silently
pass.

An exact value may be direct or derived, but cannot have an assumed numeric
basis: an assumed premise declares the weaker knowledge shape it actually
supports. A simulated measurement with an assumed basis remains visible in the
top-level assumption policy and is not usable for an autonomous design decision.

The following producers emit this contract directly:

- passive path, via and component propagation delay;
- extracted path DC resistance;
- geometry-only coupling quantities;
- ngspice measurement results.

`pcbqa/sim/model_registry.py` stores model evidence with the same shared facts.
Coverage policy is per phenomenon: a scenario lists the evidence classes it
accepts for each required phenomenon, and every contributing model is checked.
There is no universal ranking across unrelated phenomena. Propagation retains a
local ordering only among propagation-delay evidence classes, where selecting
the weakest path contribution has physical meaning.

Unsupported, unknown and not-applicable remain distinct. Unsupported means the
implemented model cannot represent the requested physics; unknown means no
numeric conclusion is available; not-applicable means the phenomenon does not
apply in the declared scope. All three remain visible and fail closed when a
requirement needs a number.

## 3. Constraints and gates

Board and fabricator policy enters gates as typed `Constraint` values carrying
units and provenance. Policy comparisons use constraint methods, and applied
limits are recorded in the gate result.

Toolkit selftest performs a focused static audit of `pcbqa/gates/g_*.py` for
policy-looking ordered comparisons against numeric literals or extracted
constraint values. Numeric constants intrinsic to mathematics or algorithms
are marked with `implementation_constant(value, rationale)`. This is a
repository-development check, not a consumer-board runtime lint gate.

Gates return PASS, FAIL, ERROR or NOT_APPLICABLE with reasons and structured
measurements. Missing policy never becomes a silent pass. Connectivity is
derived from actual copper intersection; topology rules then judge whether that
connectivity is the required topology. Fabrication and exported-artifact checks
remain independent of routing or simulation claims.

## 4. Fabrication evidence, build and release

JLCPCB is the supported manufacturing target. Manufacturer-independent physics
stays outside `pcbqa/fabricators/`.

The committed catalog is exactly:

```text
profiles/jlcpcb/catalog/approved.json
profiles/jlcpcb/catalog/evidence/
```

Every evidence file referenced by `approved.json` must exist and match its raw
digest, and every entry in `evidence/` must be referenced. `fab refresh` writes
a candidate set to scratch and shows a semantic diff. Adoption replaces the
committed catalog directory as a whole before commit; merging evidence
directories can retain orphans and is invalid. Git provides approval and
history.

`build` runs the declared KiCad generation steps against the staged design and
installs Gerbers, drills, BOM, placement, reports and `fabrication.json` as one
complete output set. Validation is offline. Stale artifacts, missing or
tampered evidence, dirty or untracked release state and source-closure mismatch
all refuse release.

## 5. Routing and simulation

KiCad Routing Tools is the supported router. Router outputs are candidates;
the board file, connectivity, DRC, topology, placement and fabrication gates
remain the arbiters. Router provenance records the implementation and inputs
used. Route candidates are compared by declared engineering metrics, not by an
invented logical board identity.

Simulation currently provides deterministic ngspice operating-point/transient
decks and Verilator behavioral-contract execution. Missing optional engines are
reported as unavailable, not as a pass. Model lookup is fail closed, operating
condition coverage is explicit, and a numerically passing assertion is not
claimable unless its shared verdict, evidence coverage and accepted assumptions
allow it. Physics for which no producer exists remains unsupported or unknown.

## 6. Decision invariant

```text
candidate has proven electrical connectivity
    → valid topology
    → DRC/process compliance
    → extracted evidence
    → applicable simulation
    → conservative requirement verdicts
    → autonomous design decision
```

Each conclusion must be traceable to the board state, a typed policy and the
evidence class that justifies it. Unsupported physics and omitted contributions
stay visible instead of becoming optimistic passes.
