# Source manifest

Every production-affecting fact in this design traces to one of the following.
Retrieved 2026-08-01 unless stated otherwise.

## Microphone

- MEMSensing `MSM261DHP006` datasheet V1.0, September 2022, DOC NO: DS-042,
  retrieved from LCSC's datasheet CDN:
  <https://datasheet.lcsc.com/datasheet/pdf/55899cff9747905d6fde524be8eff620.pdf>

Facts taken from it and used directly:

| Fact | Value | Where used |
|---|---|---|
| Port location | Top-ported, 0.325 mm lid aperture | array orientation decision |
| Supply range | 1.6 - 3.6 V (abs max 4.0 V) | confirms 3.3 V operation |
| Clock, standard mode | 1.1 - 4.8 MHz | 3.072 MHz PDM clock |
| Clock, low-power mode | 150 - 900 kHz | documented alternative |
| Supply current | 670 uA typical at 1.8 V, 2.4 MHz | power budget |
| Sensitivity / SNR | -26 dBFS / 64 dB(A) | expected performance |
| Logic thresholds | V_IH 0.7 x VDD, V_OH VDD - 0.45 V | no-translator justification |
| Pinout | 1 VDD, 2 L/R, 3 CLK, 4 DATA, 5-8 GND | symbol and footprint |
| L/R behaviour | high = data on rising edge, low = falling | pair sharing scheme |
| Land pattern | section 13 bottom view, mirrored to top | project footprint |
| Package | 3.0 x 4.0 x 1.0 mm | placement geometry |
| Handling | MSL 1, max 3 reflows, do not wash, no compressed air | legend and order notes |
| Recommended interface | 0.1 uF plus 100 ohm at VDD | per-channel supply filter |

The land pattern was read off the datasheet's dimensioned bottom view and
cross-checked against the stated 3.0 x 4.0 mm body: pad extents 2.8 mm across
and 3.8 mm along, which reconciles with the quoted 2.1 mm and 3.1 mm inner
gaps at a 0.35 mm pad width.

## FPGA module

- Sipeed Tang Nano 9K wiki: <https://wiki.sipeed.com/hardware/en/tang/Tang-Nano-9K/Nano-9K.html>
- Official pin diagram: <https://wiki.sipeed.com/hardware/zh/tang/Tang-Nano-9K/assets/clip_image010.gif>
- Board schematic `Tang_Nano_9K_3672`, KiCad 6.0.4, dated 2021-09-22.

Used for: the full J5/J6 header map; bank voltages (banks 0, 1, 2 at 3.3 V from
`VCCO0_1_2_3V3`, bank 3 at 1.8 V from `VCCO3_1V8`, so J6 positions 2-9 are
avoided); the position of the 5 V, GND and 3V3 header pins; the fact that
module 5 V is common with USB-C VBUS; and the 4-pin oscillator convention
EN / GND / OUT / VCC confirmed by the module's own 27 MHz part.

## Host

- Raspberry Pi Model B revision 1.0 and 2.0 P1 header documentation. Only pins
  common to both revisions are used; the revision-2-only P5 I2S header is not
  used.

## Other components

| Part | LCSC | Package | Notes |
|---|---|---|---|
| `LP5907MFX-3.3/NOPB` | C80670 | SOT-23-5 | TI, 250 mA, 10 uVrms, 82 dB PSRR at 1 kHz |
| `SN74LVC244APWR` | C7668 | TSSOP-20 | TI, 1.65-3.6 V, octal buffer used as clock fan-out |
| `OT322524.576MJBA4SL` | C2831388 | SMD3225-4P | YXC, 1.8-3.3 V CMOS, +/-10 ppm |
| `USBLC6-4SC6` | C111212 | SOT-23-6 | ST, four-channel ESD array, 3 pF |
| `SS14` | C2480 | SMA | 40 V 1 A Schottky, JLCPCB Basic part |
| `JK-MSMD050-30` | C369168 | 1812 | 500 mA hold, 1 A trip, 150 mohm |
| `GZ2012D601TF` | C1017 | 0805 | Sunlord, 600 ohm at 100 MHz, 500 mA, Basic part |
| `0402WGF1000TCE` | C25076 | 0402 | 100 ohm, Basic part |
| `0402WGF220JTCE` | C25092 | 0402 | 22 ohm, Basic part |
| `0402WGF330JTCE` | C25105 | 0402 | 33 ohm, Basic part |
| `CL05B104KO5NNNC` | C1525 | 0402 | 100 nF 16 V X7R, Basic part |
| `CC0603KRX7R8BB105` | C106858 | 0603 | 1 uF 25 V X7R |
| `CL21A106KAYNNNE` | C15850 | 0805 | 10 uF 25 V X5R, Basic part |
| `C2012X5R1C226KT000E` | C76637 | 0805 | 22 uF 16 V X5R |

The oscillator land pattern comes from the YXC YSO110TR datasheet's
"Recommended soldering Pattern" for the 3.2 x 2.5 mm size: four 1.3 x 1.2 mm
pads with 0.8 mm horizontal and 0.7 mm vertical gaps. The 2.5 x 2.0 mm sibling
part was rejected because that datasheet does not document its land pattern.

Availability was confirmed against JLCPCB's live assembly catalogue with
`tools/jlc_lookup.py`; every part above had non-zero stock at the time of
writing. All are Extended parts unless noted as Basic.

## Manufacturer rules

- JLCPCB PCB capabilities: <https://jlcpcb.com/capabilities/pcb-capabilities>
- JLCPCB via covering: <https://jlcpcb.com/help/article/pcb-via-covering>
- JLCPCB minimum SMD component spacing:
  <https://jlcpcb.com/help/article/minimum-spacing-for-smd-components>
- JLCPCB pick-and-place file format:
  <https://jlcpcb.com/help/article/pick-place-file-for-pcb-assembly>

The conservative four-layer profile these were distilled into is recorded in
`constraints.json` and enforced by `microphone_array_v2.kicad_pro` plus the
scripts in `tools/`.

## Toolchain

| Tool | Version | Identity |
|---|---|---|
| KiCad | 10.0.5 | `kicad-cli`, `pcbnew` Python |
