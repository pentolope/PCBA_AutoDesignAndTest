"""Generate MicArrayV2.kicad_sym from the pin definitions in design.py.

Only parts that carry researched, project-specific pin knowledge get a custom
symbol. Generic passives reuse KiCad's own Device library.
"""

import os
import design as d

GRID = 2.54
HEADER = (
    "(kicad_symbol_lib\n"
    "\t(version 20251024)\n"
    "\t(generator \"microphone-array-v2\")\n"
    "\t(generator_version \"10.0\")\n"
)


def _property(name, value, x, y, hide=False, justify=None):
    lines = [f"\t\t(property \"{name}\" \"{value}\"",
             f"\t\t\t(at {x:g} {y:g} 0)"]
    if hide:
        lines.append("\t\t\t(hide yes)")
    lines.append("\t\t\t(effects")
    lines.append("\t\t\t\t(font")
    lines.append("\t\t\t\t\t(size 1.27 1.27)")
    lines.append("\t\t\t\t)")
    if justify:
        lines.append(f"\t\t\t\t(justify {justify})")
    lines.append("\t\t\t)")
    lines.append("\t\t)")
    return "\n".join(lines)


def _pin(etype, x, y, angle, length, name, number):
    return (
        f"\t\t\t(pin {etype} line\n"
        f"\t\t\t\t(at {x:g} {y:g} {angle:g})\n"
        f"\t\t\t\t(length {length:g})\n"
        f"\t\t\t\t(name \"{name}\"\n"
        f"\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t\t(number \"{number}\"\n"
        f"\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n"
        f"\t\t\t\t)\n"
        f"\t\t\t)"
    )


def make_symbol(name, ref_prefix, pins, footprint="", datasheet="",
                description="", keywords=""):
    """Lay pins out in two columns on a rectangle, in the given order."""
    half = (len(pins) + 1) // 2
    left, right = pins[:half], pins[half:]
    rows = max(len(left), len(right))
    height = (rows + 1) * GRID
    width = 12 * GRID if len(pins) > 12 else 8 * GRID
    top = height / 2.0
    hw = width / 2.0

    out = [f"\t(symbol \"{name}\"",
           "\t\t(pin_names",
           "\t\t\t(offset 0.508)",
           "\t\t)",
           "\t\t(exclude_from_sim no)",
           "\t\t(in_bom yes)",
           "\t\t(on_board yes)",
           _property("Reference", ref_prefix, -hw, top + GRID, justify="left"),
           _property("Value", name, -hw, top + GRID * 2, justify="left"),
           _property("Footprint", footprint, 0, -top - GRID, hide=True),
           _property("Datasheet", datasheet, 0, -top - GRID * 2, hide=True),
           _property("Description", description, 0, -top - GRID * 3, hide=True),
           _property("ki_keywords", keywords, 0, 0, hide=True),
           f"\t\t(symbol \"{name}_0_1\"",
           f"\t\t\t(rectangle",
           f"\t\t\t\t(start {-hw:g} {top:g})",
           f"\t\t\t\t(end {hw:g} {-top:g})",
           "\t\t\t\t(stroke",
           "\t\t\t\t\t(width 0.254)",
           "\t\t\t\t\t(type default)",
           "\t\t\t\t)",
           "\t\t\t\t(fill",
           "\t\t\t\t\t(type background)",
           "\t\t\t\t)",
           "\t\t\t)",
           "\t\t)",
           f"\t\t(symbol \"{name}_1_1\""]

    for i, (number, pname, etype) in enumerate(left):
        y = top - GRID * (i + 1)
        out.append(_pin(etype, -hw - GRID, y, 0, GRID, pname, number))
    for i, (number, pname, etype) in enumerate(right):
        y = top - GRID * (i + 1)
        out.append(_pin(etype, hw + GRID, y, 180, GRID, pname, number))

    out.append("\t\t)")
    out.append("\t)")
    return "\n".join(out)


def connector_pins(labels, net_map=None, ref=None):
    """Build pin tuples for a header symbol, naming each pin by its function."""
    pins = []
    for position, label in enumerate(labels, start=1):
        net = net_map.get((ref, position)) if net_map else None
        if label in ("5V", "GND", "3V3"):
            name, etype = label, "passive"
        else:
            name = f"P{position}_FPGA{label}"
            etype = "passive"
        if net:
            name = f"{name}_{net}"
        pins.append((str(position), name.replace(" ", "_"), etype))
    return pins


def build_library():
    symbols = []

    symbols.append(make_symbol(
        "MSM261DHP006", "MK", d.SYMBOL_PINS["MSM261DHP006"],
        footprint=d.KI_FP["mic"],
        datasheet="https://datasheet.lcsc.com/datasheet/pdf/55899cff9747905d6fde524be8eff620.pdf",
        description="Omnidirectional top-port PDM MEMS microphone, LGA-8, VDD 1.6-3.6 V",
        keywords="microphone MEMS PDM"))

    symbols.append(make_symbol(
        "LP5907MFX-3.3", "U", d.SYMBOL_PINS["LP5907MFX-3.3"],
        footprint=d.KI_FP["sot235"],
        description="250 mA ultra-low-noise 3.3 V LDO, 10 uVrms, 82 dB PSRR at 1 kHz",
        keywords="LDO regulator low noise"))

    symbols.append(make_symbol(
        "SN74LVC244A", "U", d.SYMBOL_PINS["SN74LVC244A"],
        footprint=d.KI_FP["tssop20"],
        description="Octal buffer with tri-state outputs, used as an 8-way clock fan-out",
        keywords="buffer clock fanout"))

    symbols.append(make_symbol(
        "USBLC6-4SC6", "U", d.SYMBOL_PINS["USBLC6-4SC6"],
        footprint=d.KI_FP["sot236"],
        description="Four-channel ESD protection array",
        keywords="ESD TVS protection"))

    symbols.append(make_symbol(
        "Oscillator_4pin", "X", d.SYMBOL_PINS["Oscillator_4pin"],
        footprint=d.KI_FP["osc"],
        description="4-pad CMOS crystal oscillator, EN/GND/OUT/VCC",
        keywords="oscillator XO clock"))

    symbols.append(make_symbol(
        "TangNano9K_J2", "J", connector_pins(d.TANG_J5, d.TANG_NET_MAP, "J2"),
        footprint=d.KI_FP["hdr1x24"],
        description="Socket for the Tang Nano 9K J5 header row (USB-C end is position 1)",
        keywords="connector socket FPGA module"))

    symbols.append(make_symbol(
        "TangNano9K_J3", "J", connector_pins(d.TANG_J6, d.TANG_NET_MAP, "J3"),
        footprint=d.KI_FP["hdr1x24"],
        description="Socket for the Tang Nano 9K J6 header row; positions 2-9 are the 1.8 V bank",
        keywords="connector socket FPGA module"))

    pi_pins = []
    for pin in range(1, 27):
        net = d.PI_HEADER.get(pin)
        pi_pins.append((str(pin), f"P{pin}_{net}" if net else f"P{pin}_NC",
                        "passive"))
    symbols.append(make_symbol(
        "RPi_P1_26", "J", pi_pins,
        footprint=d.KI_FP["idc2x13"],
        description="26-way P1 header of the 2012 Raspberry Pi Model B",
        keywords="connector raspberry pi IDC"))

    return HEADER + "\n".join(symbols) + "\n)\n"


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "MicArrayV2.kicad_sym")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(build_library())
    print(f"wrote {path}")
