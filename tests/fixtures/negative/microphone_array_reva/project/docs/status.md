# Status: routed, DRC clean, release sealed

All 270 connections are routed. KiCad reports **0 DRC violations, 0 unconnected
items and 0 schematic parity issues**, with zones refilled. A fabrication
package is sealed at `generated/release/`; see
[manufacturing.md](manufacturing.md).

| Gate | Result |
|---|---|
| Schematic ERC | 0 violations |
| Board DRC (errors + warnings) | 0 violations |
| Unconnected items | 0 |
| Schematic parity | 0 issues |
| Netlist parity, schematic vs `tools/netlist.py` | clean, 83 nets, 124 components |
| Placement and array geometry audit | clean, 124 footprints |
| Clock branch layers and via budget | clean - zero vias, F.Cu only |
| Clock branch length matching | 21.5 mm spread against a 25 mm limit |
| PDM data length | clean |
| Different-net crossings | none |
| Acid traps (interior angle < 90 deg) | none |
| Track segments under 0.05 mm | none |

Copper: 2555 mm over 1000 segments, 184 vias. Both inner layers are still solid
ground, no tracks on either. 124 footprints, including 14 probe pads.

## How it got here

The generated routing in `tools/gen_pcb.py` closed the microphone array, the
clock tree, the data spokes, the supply ring and the power block, but left the
host block open - 73 connections. Those were finished with an external
autorouter, and the test points were removed at the same time. Fourteen of
them are now back, placed against the routed copper - see below.

The autorouter left the parts that matter alone. All eight PDM clock branches
are still F.Cu only with zero vias, and each PDM data net still uses exactly
two. Net classes, via geometry (0.45 mm pad / 0.30 mm drill throughout) and the
ground planes came through untouched.

## What was repaired afterwards

`tools/patch_board.py` fixes the board in place, because it is no longer
regenerated from the netlist. It found and fixed:

- **Two hard shorts.** A ground stitching via and its stub sat on U1 pin 3, tying
  the regulator's enable pin to ground - the whole 3.3 V rail would have been
  dead. Another shorted the 5 V input lane to ground. Both come from the
  `POWER_ROW_REFS` special case in `place_ground_stitching()`, which pushes the
  via 1.45 mm straight down with no obstacle test at all, even though the
  function builds an obstacle list a few lines earlier. Moving
  `POWER_ROW2_Y` from -20.2 to -19.8 mm, to open the 5 V lane, is what put the
  second one on top of that lane.
- **A third instance of the same defect**, 0.067 mm from RH4's SPI_MISO pad.
- **Three clearance failures caused by a single wrong constant.** The generator
  checks everything against `TRACK_CLEARANCE = 0.15`, but POWER wants 0.25 mm and
  Default 0.20 mm, so near misses against those nets passed the guard and
  failed DRC. The +3V3A feed threading the module socket needed 1.30 mm of the
  1.27 mm between two pins; it is now 0.25 mm wide and fits.
- **A dangling 1.6 mm tail** on the +5V bus, left over from turning D1 round so
  its anode faces the fuse.
- **Two 0.3 mm drills 0.204 mm apart** where the supply feed landed on its own
  via next to the ring's. Same net, so the feed now runs into the ring's via.

## Test points

14 of the 24 are back, placed by `tools/place_testpoints.py`. It does not put
them on a ring any more. Each pad goes **on top of a piece of its own net that
is already routed**, so it needs no track of its own - which turns the problem
from "can this be wired" into "is there room", and removes any chance of a
probe pad shorting something on the way to its net. GND is the exception: the
planes run under the whole board, so its two pads take a via of their own and
can go anywhere clear.

A candidate has to clear every other net by that pair's net class clearance,
keep its courtyard out of every other courtyard, stay outside the Tang Nano's
70 x 26 mm outline so a probe can physically reach it, and hold 2.5-3.5 mm from
the other pads. Where a 1.5 mm pad will not fit, a 1.0 mm one is tried.

The pads carry no silkscreen - the stock outline collides with the legend of
whatever they are tucked beside - so they use silk-free variants saved in
`MicArrayV2.pretty` rather than locally edited copies, which keeps KiCad from
reporting that the board no longer matches its library. Positions are on the
fabrication layer and in `generated/test_points.py`.

**Ten nets have no probe pad.** `AUDIO_MCLK` and `PDM_CLK_IN` live entirely in
the central cluster, underneath the FPGA module, so nothing on the top layer
can reach them. `TANG_3V3`, `SPI_SCLK`, `SPI_MOSI`, `SPI_MISO`, `SPI_CS_N`,
`HOST_IRQ`, `HOST_SYNC` and `HOST_RESET_N` run through the host block, which is
too dense for even a 1.0 mm pad. All eight host signals are still reachable at
the Pi header itself, and the clocks at the module socket pins once the module
is unplugged.

## The cleanup pass

`tools/cleanup_tracks.py` tidied the copper the autorouter left, in four passes
that each stay inside the shape the copper already occupied, so nothing that
passed clearance before could fail after:

| | before | after |
|---|---:|---:|
| Segments under 0.05 mm | 31 | **0** (shortest is now 0.071 mm) |
| Corners with interior angle < 90° | 30 | **2**, both on via copper |
| Segments retracing themselves | 3 | **0** |
| Collinear joins | 78 | 10 |
| Track segments | 1099 | 1000 |

Endpoints within 60 µm are snapped together, which is what removes the slivers -
they are wider than a coincidence tolerance would catch. Collinear pairs merge,
retraces drop, and any corner still turning more than 90° has its tip cut off
by a short chord, splitting it into two corners of half the angle.

The two acute corners that remain sit on vias, where the round pad copper fills
the notch, so there is nothing to trap etchant. `check_routes.py` now tests for
that condition rather than for the old 45° grid rule: the host block came from
an external autorouter and is not on the grid, so a grid rule described a style
the board no longer has. 69 corners are off-grid and none of them is acute.

## Still outstanding

Nothing that fails a gate. Worth knowing:

- **Clock branch spread is 21.5 mm**, up from 7.1 mm, because branches 5 and 6
  take lateral corridors around the host block. Inside the 25 mm limit, and
  21.5 mm of FR-4 is about 130 ps against a 325 ns PDM clock period, but the
  margin is thinner than it was.
- **`PDM_CLK_IN` uses 3 vias.** Its budget was 2 and is now 4, with the reason
  recorded in `check_routes.py`: the buffer's inputs interleave with its outputs
  on 0.65 mm pitch, so the input bus has to leave on the bottom layer. That is a
  different case from a clock branch fanning out into open board, which still
  takes none.
- **The release is a candidate, not approved production data.** Everything that
  can be checked locally has been; what cannot is whether each LCSC part's zero
  orientation in JLCPCB's library matches the rotation in `cpl.csv`. That needs
  their live preview.

## Reproducibility

**The board is no longer reproducible from `tools/gen_pcb.py`.** Re-running the
generator would discard the autorouted host block and the repairs above. The
schematic still is: `tools/gen_schematic.py` regenerates it from
`tools/netlist.py`, and netlist parity passes.

Treat `microphone_array_v2.kicad_pcb` as the source of truth for routing, and
`tools/patch_board.py` as the record of what was changed by hand.
