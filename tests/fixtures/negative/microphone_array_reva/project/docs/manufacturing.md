# Manufacturing and bring-up

## Release package

`generated/release/`, built by `tools/make_release.py` from one board revision
in a single run:

| File | Contents |
|---|---|
| `microphone_array_v2-revA-fabrication.zip` | 11 Gerbers, 2 Excellon drills, 2 drill maps, X2 job file |
| `bom.csv` | JLCPCB BOM, 15 lines |
| `cpl.csv` | JLCPCB pick-and-place, 103 placements, all top side |
| `positions.csv` | KiCad native position export the CPL is derived from |
| `schematic.pdf` | full schematic |
| `renders/` | top and bottom |
| `MANIFEST.md` | SHA-256 of the archive, board and each data file |

Layer order is carried in the Gerber X2 `FileFunction` attributes - `Copper,L1,Top`,
`Copper,L2,Inr`, `Copper,L3,Inr`, `Copper,L4,Bot` - so the inner layers being
named `GND1` and `GND2` rather than `In1.Cu` and `In2.Cu` does not make the
stackup ambiguous.

Gerbers, drills and the CPL share one origin. Verified rather than assumed:
`MK1` appears in the CPL at 53.220 mm from the board centre on the +X axis at
270 degrees, which is exactly where the placement puts it, and the Edge.Cuts
outline spans the matching 120 mm.

**This is a release candidate, not an approved production file.** The one thing
that cannot be checked locally is whether each LCSC part's zero orientation in
JLCPCB's library matches the rotation in `cpl.csv` - their library models, not
ours. Upload the package and step through their Gerber and pick-and-place
previews before paying for it.

## Order options

| Option | Value |
|---|---|
| Layers | 4 |
| Dimensions | 120 mm diameter circle |
| Thickness | 1.6 mm |
| Outer copper | 1 oz |
| Inner copper | 0.5 oz |
| Surface finish | ENIG |
| Solder mask | Green |
| Silkscreen | White |
| Via covering | Plugged (routing vias only) |
| Assembly | Top side only |
| Impedance control | Not required |

## Order remark

```
Four-layer board: L1 top signal / L2 GND / L3 GND / L4 bottom signal.
Both inner layers are solid ground planes; this is intentional.
Plug only the routing vias, which are 0.30 mm finished hole with 0.45 mm pads
and are tented on both sides. Keep every plated through-hole for the two 1x24
module sockets and the 2x13 pin header open, and keep the four 3.2 mm NPTH
mounting holes open. Follow the supplied layer order and the confirm-production
file.
```

## Assembly notes

- All surface-mount parts are on the **top** side. There are no bottom-side
  SMT parts.
- `J1`, `J2` and `J3` are marked DNP and are excluded from the BOM and CPL.
  They are hand-soldered by the customer from the bottom side.
- The four mounting holes and the twenty-four test pads carry no parts.

### Microphone handling

The `MSM261DHP006` datasheet imposes handling limits that must reach the
assembler:

- Moisture sensitivity level 1.
- Maximum three reflow cycles, peak 260 C.
- **Do not board-wash or clean after reflow.** No aqueous wash, no solvents,
  no ultrasonic cleaning.
- Do not apply compressed air or vacuum over the acoustic port, and do not
  place a pick-up nozzle over the port hole.

The first two lines of the top silkscreen carry `PORTS FACE UP - DO NOT WASH`
for this reason. If the assembler's standard process includes a wash step, the
order must be flagged as no-clean.

## Hand-soldered parts

| Ref | Part | Side | Notes |
|---|---|---|---|
| `J2`, `J3` | 1x24 2.54 mm female header | bottom | 0.900 in row spacing; 1.0 mm drills give about 0.18 mm radial slack per pin |
| `J1` | 2x13 2.54 mm shrouded IDC | bottom | keyed; pin 1 marked by a square pad and a silkscreen triangle |

Fit the module sockets first, then check the Tang Nano 9K seats freely before
soldering the IDC header.

## Bring-up sequence

1. Inspect the assembled board: all sixteen microphone ports clear, no residue
   over any port, `U1` and `D1` orientation against the silkscreen.
2. With **no** Tang Nano 9K fitted and **no** ribbon connected, apply 5 V to
   test pad `5V` through a current-limited supply. Expect well under 5 mA.
   Confirm `3V3A` reads 3.3 V and `3V3C` reads 3.3 V.
3. Fit the Tang Nano 9K. Power from the Pi. Confirm `TANG` reads 3.3 V.
4. Load a bitstream over the module's USB-C. `D1` prevents the USB supply from
   back-feeding the Pi, so both may be connected.
5. Confirm 24.576 MHz on `MCLK` and 3.072 MHz on `PCLK`.
6. Confirm activity on `D0` to `D7`.

The Raspberry Pi must be fed from a supply of at least 1.5 A. This board adds
roughly 200 mA to the Pi's 5 V rail, which an original Model B's input polyfuse
can pass but a marginal phone charger cannot.

### USB-C access

The module hangs below the board on its sockets, with its USB-C connector
facing +X and about 15 mm of carrier overhanging it. With standard 8.5 mm
sockets there is enough gap for a slim USB-C plug, but a thick overmoulded
cable may not fit. Load the bitstream before final assembly, or use a slim or
right-angle cable.

## Regenerating the design

The schematic and the board are both generated from `tools/netlist.py`, which
is the single source of truth. Nothing in the KiCad files should be hand-edited.

```bash
python tools/gen_symbols.py
python tools/gen_schematic.py
python tools/check_netlist_parity.py
python tools/gen_pcb.py
python tools/check_placement.py
python tools/check_routes.py
```

`check_netlist_parity.py` proves the generated schematic carries exactly the
intended netlist. KiCad's own `--schematic-parity` DRC then proves the board
matches the schematic. Note that regenerating the board discards routing, so
run the generators before routing, not after.
