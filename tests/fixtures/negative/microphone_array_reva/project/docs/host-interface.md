# Raspberry Pi host interface

## Physical connection

`J1` is a keyed 2x13, 2.54 mm shrouded IDC header on the board bottom,
hand-soldered and marked DNP for assembly. A straight-through 26-conductor
ribbon connects it to the P1 header of a 2012 Raspberry Pi Model B. Pin 1 is
marked on the silkscreen and by a square pad.

Only pins that are identical on Model B revision 1.0 and revision 2.0 are used,
so the board works with either. In particular the revision-dependent pins 3, 5
and 13 are left unconnected, and the P5 header carrying I2S is not used at all -
that header does not exist on revision 1.0 boards.

| Pi physical pin | BCM signal | Array function | Direction at Pi |
|---:|---|---|---|
| 2, 4 | 5V | board supply | power out |
| 6, 9, 14, 20, 25 | GND | return | power |
| 16 | GPIO23 | `HOST_SYNC` | input |
| 18 | GPIO24 | `HOST_STATUS` | input |
| 19 | GPIO10 / MOSI | `SPI_MOSI` | output |
| 21 | GPIO9 / MISO | `SPI_MISO` | input |
| 22 | GPIO25 | `HOST_IRQ` | input |
| 23 | GPIO11 / SCLK | `SPI_SCLK` | output |
| 24 | GPIO8 / CE0 | `SPI_CS_N` | output |
| 26 | GPIO7 / CE1 | `HOST_RESET_N` | output |

Pin 1 (3V3) is deliberately **not** connected. The ESD arrays clamp to the
board's own `TANG_3V3` rail instead, which is present whenever the board is
powered; clamping to the Pi's 3.3 V would pull the signals towards 0.7 V if the
Pi were unpowered while the module was running from USB.

Every host line carries a 33 ohm series resistor at the connector and passes
through one of two `USBLC6-4SC6` four-channel ESD arrays.

## Bandwidth and clock rate

The sustained payload at full rate is

```
16 channels x 48000 samples/s x 16 bits = 12.288 Mbit/s
```

A 2012 Model B drives SPI0 from a 250 MHz core clock divided by an even
integer, so the usable settings near the top are 15.625 MHz (divider 16) and
25 MHz (divider 10).

- At **25 MHz** the payload occupies about 49 % of the link, which leaves
  comfortable headroom for framing and for the Pi's DMA servicing.
- At **15.625 MHz** it occupies about 79 %, which is workable but tight.
- Decimating to 16 kHz in the FPGA instead reduces the payload to 4.096 Mbit/s
  and runs comfortably at any supported clock.

The hardware is designed for 25 MHz: series damping on every line, ESD arrays
with 3 pF loading, and short runs between the socket and the connector. Whether
25 MHz is actually reliable depends on ribbon length and on the single-ground
return described in [architecture.md](architecture.md); treat 16 kHz mode as the
fallback if it is not.

## Proposed wire protocol

This repository freezes the electrical interface and pin constraints. The
gateware implementation can evolve without a board revision.

- SPI mode 0, MSB first, 25 MHz maximum.
- The FPGA raises `HOST_IRQ` when at least one complete frame is buffered.
- The Pi asserts `SPI_CS_N` and clocks a fixed 32-byte header followed by the
  payload.
- `HOST_RESET_N` is active low and resets the acquisition FIFO without
  reconfiguring the FPGA.
- `HOST_SYNC` pulses on the first sample of a frame; `HOST_STATUS` is high while
  acquisition is locked to the 24.576 MHz reference.

Header fields are little-endian after the byte-oriented magic:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 4 | ASCII `PDM1` |
| 4 | 2 | protocol version, initially 1 |
| 6 | 2 | header size, 32 |
| 8 | 4 | frame sequence number |
| 12 | 4 | sample sets in payload |
| 16 | 8 | running 48 kHz sample counter |
| 24 | 4 | status flags: overflow, clock fault, reset seen |
| 28 | 4 | CRC-32 of payload, or zero when disabled |

The payload is interleaved signed 16-bit PCM: sample set 0 channels 0 to 15,
then sample set 1, and so on. Channel *n* is the microphone whose acoustic port
lies at azimuth 22.5*n* degrees counter-clockwise from +X.

## Channel-to-edge mapping in gateware

Each PDM data line carries two channels multiplexed by clock edge:

| Data net | Falling edge (`L/R` low) | Rising edge (`L/R` high) |
|---|---|---|
| `PDM_D0` | CH0 | CH1 |
| `PDM_D1` | CH2 | CH3 |
| `PDM_D2` | CH4 | CH5 |
| `PDM_D3` | CH6 | CH7 |
| `PDM_D4` | CH8 | CH9 |
| `PDM_D5` | CH10 | CH11 |
| `PDM_D6` | CH12 | CH13 |
| `PDM_D7` | CH14 | CH15 |
