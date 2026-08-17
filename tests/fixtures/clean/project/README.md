# clean - a deliberately boring positive fixture

Rev A is a permanent *negative* fixture: it must be rejected. That makes it
useless for proving the opposite property - that a check which passes is
capable of failing. A gate that always fails and a gate that always passes look
identical when you only ever point them at a board that should fail.

This project is the positive control. It is a 20 mm square outline, an empty
schematic, and a project file that leaves no rule disabled. It has no
components on purpose: schematic-parity would otherwise report a footprint with
no symbol, and the point here is a board with nothing whatsoever to report.

    ERC  exit 0, 0 violations, 0 ignored checks
    DRC  exit 0, 0 violations, 0 unconnected items, 0 parity issues,
         0 ignored checks

Tests mutate a *copy* of it to produce a real finding and assert the gate turns
from PASS to FAIL. Keep it clean; if a KiCad upgrade makes it report something,
that is a finding about the upgrade and belongs in the test output, not in a
quiet edit to this fixture.
