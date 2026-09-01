# openEMS targeted feasibility (technical design note)

Status: engineering research only. openEMS is not a toolkit dependency, no
openEMS runtime integration is implemented, and validation does not invoke it.
This note records the technical work needed to make a full-wave result
trustworthy; it does not define a backend registry or dispatch abstraction.

## Targeted extraction scope

The useful simulation unit is a bounded region around one targeted
`ElectricalPath`, not an invented whole-board simulation mode:

```text
KiCad board + targeted ElectricalPath
    -> path copper, vias, reference planes and dielectric geometry
    -> bounded EM submodel with ports at the path endpoints
    -> converged openEMS FDTD solve
    -> Touchstone S-parameters and explicitly derived quantities
    -> shared claims carrying full-wave evidence and exact provenance
```

The extraction must retain the selected path identity and the source board
digest. Exact geometry and stackup provenance includes the clipped copper and
plane shapes, layer elevations, dielectric definitions, finished conductor and
via geometry, solder mask treatment when modeled, extraction bounds, and every
transformation applied before solving. A result whose geometry cannot be bound
back to those inputs is unusable regardless of solver convergence.

## Ports and reference structure

Ports are part of the physical claim, not incidental solver configuration.
Each port records its location, excitation, reference conductor, orientation,
de-embedding plane and relationship to the clipped `ElectricalPath` endpoint.
The extraction boundary must preserve a physically valid return path. A naive
endpoint port can invalidate S11 and impedance results even when the mesh and
solver converge.

## Mesh convergence

The mesh resolves trace edges, conductor thickness, dielectric interfaces, via
barrels and antipads, ports, and nearby reference-plane discontinuities. One
mesh is not evidence of convergence. At least two successively refined meshes
must be solved, with the compared frequency band, S-parameter delta and
acceptance tolerance recorded. Failure to meet the declared convergence
criterion leaves the requested quantity unknown.

## Output and claim shape

The primary exchange artifact is Touchstone data over an explicitly recorded
frequency grid. Any impedance, delay, loss or coupling quantity derived from it
records the Touchstone digest and the derivation. The resulting numeric record
uses the shared claim contract: phenomenon, scope, units, knowledge shape,
full-wave evidence class, provenance, applicability, assumptions, omitted
contributions and any linked requirement. Solver completion by itself is not a
PASS.

## Reference-fixture validation

Before a board-derived result is trusted, the same extraction, port, mesh and
Touchstone pipeline must reproduce a reference fixture with independently known
behavior. A controlled microstrip or stripline fixture provides analytic or
measured impedance and S-parameter expectations over a stated band. Validation
compares the converged result against that reference with declared tolerances
and retains the complete geometry, stackup, port and mesh provenance. This
fixture validates the pipeline; it does not substitute for provenance or
convergence on a later board extraction.
