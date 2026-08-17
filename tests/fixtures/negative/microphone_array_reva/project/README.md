# 16-channel circular PDM microphone array

A 120 mm circular, four-layer KiCad 10 carrier for sixteen `MSM261DHP006` PDM
MEMS microphones running at 3.3 V. A socketed Sipeed Tang Nano 9K performs
acquisition and decimation; a 2012 Raspberry Pi Model B collects the audio over
SPI0 through a 26-way ribbon.

The sixteen acoustic ports sit on a 54.00 mm radius at 22.5 degree intervals,
channel 0 on +X, numbered counter-clockwise viewed from the component side.

![top side](generated/render/top.png)

## Design summary

| | |
|---|---|
| Board | 120 mm circle, 1.6 mm, 4 layers, ENIG, green / white |
| Stackup | signal / GND / GND / signal - both inner layers solid ground |
| Microphones | 16 x `MSM261DHP006`, top-ported, 3.3 V, ports up at the rim |
| Channel pairing | adjacent channels share a data line by clock edge, 8 data nets |
| Reference clock | 24.576 MHz oscillator to a global-clock FPGA pin |
| PDM clock | 3.072 MHz, buffered to 8 series-terminated branches |
| Sample rate | 48 kHz after decimate-by-64 |
| Level translation | none - microphones, FPGA banks and Pi GPIO are all 3.3 V |
| Power | Raspberry Pi 5 V only, via PTC and series Schottky |
| Microphone supply | `LP5907MFX-3.3`, 10 uVrms, 82 dB PSRR at 1 kHz |
| Host link | SPI0, 25 MHz design target, ESD-protected and series-damped |
| Assembly | all SMT on top; module sockets and Pi header hand-soldered |

The `MSM261DHP006` is a top-ported part, so "outward-facing" is realised as
ports facing up at the rim rather than radially. The capsules are
omnidirectional, so the array response is set by the port geometry, not by
package orientation; the alternative - sixteen perpendicular daughter-cards -
cannot be assembled by JLCPCB. This is discussed in
[docs/architecture.md](docs/architecture.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) - signal, clock, power and
  stackup decisions, Tang Nano 9K pin assignments, and known limitations.
- [docs/host-interface.md](docs/host-interface.md) - Raspberry Pi pinout,
  bandwidth analysis and the wire protocol.
- [docs/routing.md](docs/routing.md) - what is pre-routed and why, and the
  microphone escape geometry.
- [docs/manufacturing.md](docs/manufacturing.md) - order options, order remark,
  microphone handling limits and bring-up sequence.
- [docs/sources.md](docs/sources.md) - every datasheet and manufacturer rule the
  design relies on, with the facts taken from each.

## Repository layout

| Path | Contents |
|---|---|
| `microphone_array_v2.kicad_sch` | generated schematic - do not hand-edit |
| `microphone_array_v2.kicad_pcb` | the board; no longer regenerated, see below |
| `microphone_array_v2.kicad_pro` | project settings, net classes, design rules |
| `MicArrayV2.kicad_sym` | generated project symbols |
| `MicArrayV2.pretty/` | project footprints: microphone, oscillator, silk-free probe pads |
| `constraints.json` | frozen requirements and manufacturing profile |
| `tools/` | design source of truth, generators and checkers |
| `docs/` | engineering documentation |
| `generated/` | reports, renders and release artefacts |

## Source of truth

`tools/netlist.py` holds the complete component list, netlist and placement.
The schematic is generated from it. The board was too, until the host block was
finished with an external autorouter; it is now edited in place by
`tools/patch_board.py`, `tools/cleanup_tracks.py` and
`tools/place_testpoints.py`, each of which records what it did. Three
independent gates keep schematic and board in step:

- `tools/check_netlist_parity.py` extracts the netlist from the generated
  schematic with `kicad-cli` and compares it against `netlist.py`, including an
  exact match on which pins are deliberately left unconnected.
- KiCad's `--schematic-parity` DRC compares the board against the schematic.
- `tools/check_placement.py` re-derives the array geometry from the board:
  every microphone's acoustic port radius and azimuth, every Tang Nano 9K
  socket pin coordinate, courtyard overlaps by exact polygon distance, JLCPCB
  package-pair body spacing, and edge clearance.

`tools/check_routes.py` adds the post-route constraints KiCad cannot express:
allowed layers per net, via budgets on the clock nets, branch length matching
across the eight microphone pairs, different-net crossings, and acid traps -
corners left with an interior angle under 90 degrees.

Regeneration commands are in [docs/manufacturing.md](docs/manufacturing.md).

## Status

**Fully routed and DRC clean.** KiCad reports 0 DRC violations, 0 unconnected
items and 0 schematic parity issues; ERC, netlist parity and the placement audit
all pass. The eight PDM clock branches keep their intended topology — F.Cu only,
zero vias, length-matched to 21.5 mm — and both inner layers are still solid
ground.

Two hard shorts found after routing (the regulator's enable pin and the 5 V
input lane, both tied to ground by misplaced stitching vias) have been repaired
by [tools/patch_board.py](tools/patch_board.py).

The copper has been through a cleanup pass ([tools/cleanup_tracks.py](tools/cleanup_tracks.py)):
no segment is under 0.05 mm and no corner is left with an interior angle under
90°, so there are no acid traps. Fourteen of the twenty-four test points are
back, each placed on top of a piece of its own net so it needs no track of its
own; the other ten have nowhere on the top layer to go, and are listed in
[docs/status.md](docs/status.md).

The board is no longer regenerated by `tools/gen_pcb.py`.

A release package is sealed at `generated/release/` — Gerbers, drills, BOM, CPL,
schematic PDF and a SHA-256 manifest, all built from one board revision in a
single run. It is a release *candidate*: JLCPCB's own part orientations cannot
be checked locally, so step through their previews before ordering. See
[docs/manufacturing.md](docs/manufacturing.md).
