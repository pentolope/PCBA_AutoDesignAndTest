# openEMS targeted feasibility (design note)

Status: design note only. openEMS is NOT a dependency, is not
installed in the reference environment, and nothing in validation
invokes it.

## Scope discipline

Whole-board FDTD is out of scope indefinitely. The useful unit is one
ElectricalPath region:

    KiCad region / ElectricalPath
        -> traces + vias + planes + dielectric (geometry export)
        -> EM submodel (bounded box, ports at path endpoints)
        -> openEMS FDTD
        -> Touchstone S-parameters / extracted quantities
        -> toolkit fidelity class "full-wave-extracted" + gates

## The genuinely hard parts (in order)

1. **Geometry truthfulness.** The exported submodel must be provably
   the board's copper: same outline clip, same stackup heights, same
   finished thicknesses as the approved evidence. The extractor in
   `pcbqa/extract.py` (segment inventories, hashes of the source
   board) is the natural provenance anchor.
2. **Ports.** Port placement/reference definition at clipped path
   endpoints dominates result validity; naive ports invalidate S11
   long before meshing does.
3. **Meshing.** Thirds-rule mesh lines on trace edges, dielectric
   interfaces, via barrels; a mesh-convergence check (two densities,
   compared) is mandatory before any number is trusted.
4. Launching openEMS is the easy part (python API, offline).

## Integration contract (future)

- Backend discovery like ngspice/verilator: absent -> explicit
  `backend-unavailable`, never fabricated data.
- Results enter as fidelity "full-wave-extracted" with the submodel
  geometry hash, mesh parameters, and convergence delta recorded.
- A synthetic reference fixture FIRST: one microstrip of known
  analytic impedance; the pipeline is proven when its extracted Z0
  agrees with the analytic model within a stated tolerance band.

No code beyond this note ships until the fixture pipeline can be run
by someone with openEMS installed; the note exists so that work starts
at the geometry/ports problem, not at the solver invocation.
