# Hardware architecture and engineering decisions

## Frozen requirements

- Board: 120.00 mm circular outline, 1.60 mm finished thickness, four copper
  layers, 1 oz outer / 0.5 oz inner copper, ENIG finish, green mask, white
  legend.
- Array: 16 JLCPCB-assembled `MSM261DHP006` PDM microphones running at 3.3 V,
  acoustic ports on a 54.00 mm radius at 22.5 degree intervals, channel 0 on
  +X, numbered counter-clockwise viewed from the component side.
- FPGA: Sipeed Tang Nano 9K in two hand-soldered 1x24 female sockets on the
  board bottom.
- Host: 2012 Raspberry Pi Model B, 26-pin P1 header, SPI0 transport.
- Power: Raspberry Pi 5 V only.
- Assembly: every SMT part on the top side. The two module sockets and the
  26-way IDC header are hand-fitted and marked DNP.

### How "outward-facing" is realised

The `MSM261DHP006` is a **top-ported** part: its acoustic port is a 0.325 mm
hole in the lid, so its acoustic axis is normal to the PCB and cannot be made
to point radially outward on a single flat disc. The array is therefore built
with the ports facing up at the rim, on a 54 mm radius. Because the capsules
are omnidirectional, this costs nothing in directivity: the array's spatial
response is set by the geometry of the port positions, not by the orientation
of each package. Each package is additionally rotated so that its port is the
outermost feature of the footprint, with the port sitting exactly on the
54.00 mm circle. `tools/check_placement.py` asserts that radius for all 16
channels on every regeneration.

A literally radial arrangement would need sixteen small daughter-cards soldered
perpendicular at the rim, which cannot be assembled by JLCPCB and was rejected.

## Signal architecture

`MSM261DHP006` (`C22390138`) is a 4.0 x 3.0 x 1.0 mm LGA-8 part specified for
VDD 1.6 - 3.6 V, so 3.3 V is inside its normal operating range and no dedicated
microphone rail voltage is needed.

Adjacent channels form a left/right pair and share one data line. The even
channel straps `L/R` low and drives data on the falling clock edge; the odd
channel straps `L/R` high and drives on the rising edge. Sixteen microphones
therefore reach the FPGA on eight data nets, `PDM_D0` to `PDM_D7`.

**No level translation is used anywhere.** The microphones, the Tang Nano 9K
3.3 V banks and the Raspberry Pi GPIO are all 3.3 V logic. The microphone
guarantees V_OH = VDD - 0.45 V = 2.85 V against the FPGA's 2.0 V LVCMOS33 input
threshold, and accepts V_IH = 0.7 x VDD = 2.31 V against the FPGA's 3.0 V-plus
output. This removes the two 8-bit translators and one single-bit translator
that a 1.8 V microphone rail would have required.

Each microphone output has its own 22 ohm series resistor before the pair join,
so both drivers on a shared net are source-damped. Each microphone supply pin
is fed through a 100 ohm resistor with a local 100 nF capacitor, which is the
arrangement the datasheet's recommended interface circuit calls for; it forms a
16 kHz supply filter per channel and decouples the sixteen capsules from each
other on the shared rail. Four 10 uF capacitors stabilise the ring.

## Clocking

`OT322524.576MJBA4SL` (`C2831388`) supplies an exact 24.576 MHz LVCMOS clock to
FPGA pin 51 (`GCLKC_3`), a global-clock-capable input. The intended gateware
divides it by 8 to 3.072 MHz and decimates by 64 to 48.000 ksample/s.

An external oscillator is required because the Tang Nano 9K's onboard 27 MHz
reference cannot produce an exact 48 kHz family rate: 3.072 MHz / 27 MHz is
128/1125, and the GW1NR-9C rPLL cannot reach a divider of 1125.

The 3.072 MHz PDM clock leaves the FPGA on pin 35 (`GCLKT_4`), passes a 33 ohm
series resistor and drives all eight inputs of an `SN74LVC244APWR` used as a
fan-out buffer. Its eight outputs each feed one microphone pair through a
33 ohm series resistor. Driving sixteen capsules spread around a 339 mm
circumference from one FPGA pin would present roughly 100 pF of load plus a
long distributed line; the buffer splits that into eight short, individually
terminated branches. Input-to-output skew of the buffer is a few nanoseconds
against a 325 ns clock period, which is irrelevant at this rate, and the
resulting inter-channel sampling skew is far below one part in 10^4 of a
48 kHz sample.

## Power architecture

The only supply path is:

```
Pi P1 pins 2 and 4 (5 V) -> F1 500 mA PTC -> D1 SS14 -> +5V
+5V -> Tang Nano 9K 5 V header pin
+5V -> U1 LP5907MFX-3.3 -> +3V3A  (microphone ring)
+3V3A -> FB1 600 ohm bead -> +3V3_CLK  (oscillator and clock buffer)
```

`D1` is in series rather than optional. The Tang Nano 9K's USB-C connector is
still needed to load a bitstream, and its VBUS is tied to the same module 5 V
net that this board feeds. The Schottky lets whichever supply is higher win
while preventing USB VBUS from back-feeding the Raspberry Pi's 5 V rail, so a
programming cable can be plugged in without contention. `F1` protects the Pi's
own polyfuse against a fault on this board.

`U1` is an `LP5907MFX-3.3/NOPB` chosen for 10 uVrms output noise and 82 dB PSRR
at 1 kHz, feeding only the microphone ring. The oscillator and the clock buffer
are the noisy loads, so they are filtered off the same regulator through a
ferrite bead rather than the microphones being filtered from them; that puts
the filter at the aggressor.

Estimated draw: 16 microphones about 24 mA, oscillator about 15 mA, buffer
about 6 mA, Tang Nano 9K about 150 mA, total about 200 mA. This is within what
an original Model B's 5 V rail can pass, but the Pi must be fed from a supply
of at least 1.5 A. See [manufacturing.md](manufacturing.md) for the bring-up
sequence.

## Tang Nano 9K assignments

Only 3.3 V bank pins are used. J6 positions 2 to 9 are FPGA bank 3 at 1.8 V and
are deliberately left unconnected, as are the TF-card and HDMI-multiplexed
pins.

The eight data lines are spread across **both** header rows and ordered along
each row to follow the azimuth of the channels they serve: the four upper pairs
land on J5 and the four lower pairs on J6. Putting all eight on adjacent pins
of one row forces every line from the lower rim to travel the length of the
module and cross its neighbours.

| Carrier signal | Module header | FPGA pin | Direction |
|---|---:|---:|---|
| `PDM_D0` (CH0/1, azimuth 11 deg) | J5.5 | 25 | input |
| `PDM_D1` (CH2/3, 56 deg) | J5.9 | 29 | input |
| `PDM_D2` (CH4/5, 101 deg) | J5.19 | 54 | input |
| `PDM_D3` (CH6/7, 146 deg) | J5.23 | 68 | input |
| `SPI_SCLK` | J5.13 | 40 | input |
| `PDM_CLK_FPGA` | J5.14 | 35 (`GCLKT_4`) | output |
| `SPI_MOSI` | J5.15 | 41 | input |
| `SPI_CS_N` | J5.16 | 42 | input |
| `AUDIO_MCLK` | J5.17 | 51 (`GCLKC_3`) | input, global clock |
| `HOST_SYNC` | J6.10 | 77 | output |
| `PDM_D7` (CH14/15, 326 deg) | J6.12 | 75 | input |
| `HOST_RESET_N` | J6.15 | 72 | input |
| `PDM_D6` (CH12/13, 281 deg) | J6.16 | 71 | input |
| `HOST_STATUS` | J6.17 | 70 | output |
| `+5V` | J6.18 | power | input to module |
| `SPI_MISO` | J6.19 | 48 | output |
| `HOST_IRQ` | J6.20 | 49 | output |
| `PDM_D5` (CH10/11, 236 deg) | J6.21 | 31 | input |
| `PDM_D4` (CH8/9, 191 deg) | J6.22 | 32 | input |
| `GND` | J6.23 | power | return |
| `TANG_3V3` | J6.24 | power | ESD clamp reference only |

FPGA pins 68, 70, 71, 72 and 75 are shared with the module's HDMI connector.
Driven as ordinary 3.3 V GPIO they carry an unterminated stub to that
connector, which is immaterial at the 3.072 MHz PDM rate and for the DC-ish
control lines. The 25 MHz SPI is deliberately kept off them, as are both clock
inputs. The microSD pins (J5.1-4) are left alone so the module's card socket
stays usable.

Socket position 1 is the module's USB-C end on both rows, matching Sipeed's
published pin diagram. `tools/check_placement.py` asserts every socket pin
lands on its expected board coordinate.

### Known limitation: module ground return

The Tang Nano 9K exposes exactly one ground pin across both header rows
(J6.23). Every signal between the carrier and the module shares that single
return. This is a property of the module, not of this board. It is mitigated by
keeping both inner layers as solid ground directly under the module, by series
damping every fast net, and by keeping the SPI runs between the socket and the
IDC header short. It remains the weakest part of the interconnect and is the
first thing to examine if high-rate SPI proves marginal.

## Layer usage and return paths

| Layer | Function |
|---|---|
| `F.Cu` | All components, primary routing, all clock nets |
| `In1.Cu` | Solid ground plane |
| `In2.Cu` | Solid ground plane |
| `B.Cu` | Secondary routing; no components |

This deliberately departs from the more usual signal / ground / power / signal
arrangement. The whole board draws about 200 mA, so the supplies are entirely
comfortable as 0.6 mm tracks, and dedicating the second inner layer to ground
instead buys an unbroken reference plane adjacent to **both** routing layers.
No PDM clock or data line can then cross a plane split, and the return current
for any trace on either outer layer has a continuous path directly beneath it.
Power-plane impedance, which is what the conventional stackup would buy, is not
a constraint at this current.

Ground is a plane net that the autorouter never sees. Because a plane on an
inner layer cannot reach a surface-mount pad on its own, `tools/gen_pcb.py`
places an explicit stitching via plus a short connecting track at every ground
pad - 110 of them - choosing each via position by scoring candidate directions
against all neighbouring pads rather than by a fixed offset.

Track widths come from net classes: 0.60 mm for supplies, 0.25 mm for the
per-channel microphone feeds and the clock nets, 0.20 mm elsewhere. Routing
vias are 0.45 mm pad on a 0.30 mm finished hole and are tented on both sides.
Copper-to-edge target is 0.30 mm.

## Test provisions

Twenty-four top-side test pads on two arcs expose the three supply rails, the
Tang 3.3 V sense, two grounds, the 24.576 MHz reference, the 3.072 MHz PDM
clock, all eight PDM data lines and all eight host signals. Each is labelled on
the silkscreen with a short mnemonic; the full net name is carried on the
fabrication layer.

## Primary sources

See [sources.md](sources.md) for the datasheets, JLCPCB capability pages and
Sipeed documentation this design was built from, with retrieval dates.
