"""Generate microphone_array_v2.kicad_sch from the netlist in netlist.py.

Each symbol is placed inside a titled block; every pin gets a short wire stub
ending in a net label. That yields a schematic whose extracted netlist is
exactly the one in netlist.py, which the PCB generator also consumes. KiCad's
`--schematic-parity` DRC is what proves the two stayed in step.
"""

import hashlib
import os

import design as d
import netlist as nl
import sexpr

GRID = 2.54
STUB = 2.54
PAGE_W, PAGE_H = 1180.0, 900.0

SYMBOL_LIBS = {
    "Device": r"C:\Program Files\KiCad\10.0\share\kicad\symbols\Device.kicad_sym",
    "Connector": r"C:\Program Files\KiCad\10.0\share\kicad\symbols\Connector.kicad_sym",
    "Mechanical": r"C:\Program Files\KiCad\10.0\share\kicad\symbols\Mechanical.kicad_sym",
    "power": r"C:\Program Files\KiCad\10.0\share\kicad\symbols\power.kicad_sym",
}

# Nets whose only sources are passive pins need an explicit power flag so ERC
# can see them as driven.
POWER_FLAG_NETS = ["GND", "PI_5V", "5V_FUSED", "+5V", "+3V3_CLK", "TANG_3V3"]


def snap(value, step=1.27):
    """Snap a coordinate to the schematic connection grid."""
    return round(value / step) * step


def uuid_for(*parts):
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return f"{digest[0:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def extract_symbol_text(library_text, name):
    """Return the raw, balanced `(symbol "name" ...)` block from a library.

    Copying the library's own text verbatim avoids re-serialising it and
    losing the quoting KiCad requires on property names and values.
    """
    needle = f"(symbol \"{name}\""
    start = library_text.find(needle)
    if start < 0:
        return None
    depth, index, in_string, escaped = 0, start, False, False
    while index < len(library_text):
        char = library_text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "\"":
                in_string = False
        elif char == "\"":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return library_text[start:index + 1]
        index += 1
    return None


def load_pin_geometry(project_root):
    libs = dict(SYMBOL_LIBS)
    libs["MicArrayV2"] = os.path.join(project_root, "MicArrayV2.kicad_sym")
    geometry, definitions = {}, {}
    for nickname, path in libs.items():
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        pins = sexpr.symbol_pins(text)
        for name, pin_list in pins.items():
            geometry[f"{nickname}:{name}"] = pin_list
        document = sexpr.parse(text)[0]
        for symbol in sexpr.children(document, "symbol"):
            raw = extract_symbol_text(text, symbol[1])
            if raw:
                definitions[f"{nickname}:{symbol[1]}"] = (symbol[1], raw)
    return geometry, definitions


def block_layout(components):
    """Assign every component to a titled block and a position on the sheet."""
    blocks = []

    for k in range(d.MIC_COUNT):
        blocks.append((f"Channel {k}  (L/R {'low' if k % 2 == 0 else 'high'}, pair {k // 2})",
                       [f"MK{k + 1}", f"RV{k + 1}", f"CM{k + 1}", f"RD{k + 1}"]))

    blocks.append(("Microphone ring bulk", ["CB1", "CB2", "CB3", "CB4"]))
    blocks.append(("Audio master clock", ["X1", "C8", "R1"]))
    blocks.append(("PDM clock fan-out", ["U2", "C6", "C7", "R2"]))
    blocks.append(("PDM clock branch termination",
                   [f"RC{n + 1}" for n in range(8)]))
    blocks.append(("Pi 5 V input protection", ["F1", "D1", "C4", "C5"]))
    blocks.append(("3.3 V microphone supply", ["U1", "C1", "C2", "C3", "FB1", "C9"]))
    blocks.append(("Host ESD and series damping",
                   ["U3", "U4"] + [f"RH{i + 1}" for i in range(8)]))
    blocks.append(("Tang Nano 9K sockets", ["J2", "J3"]))
    blocks.append(("Raspberry Pi P1 header", ["J1"]))
    blocks.append(("Mounting holes", ["H1", "H2", "H3", "H4"]))
    for start in range(0, len(d.TEST_POINTS), 8):
        chunk = d.TEST_POINTS[start:start + 8]
        blocks.append((f"Test points {chunk[0][0]}-{chunk[-1][0]}",
                       [tp[0] for tp in chunk]))

    return blocks


def generate(project_root):
    components, nets = nl.build()
    by_ref = {c["ref"]: c for c in components}
    geometry, _ = load_pin_geometry(project_root)

    pin_net = {}
    for net, pins in nets.items():
        for ref, pad in pins:
            pin_net[(ref, pad)] = net

    out = ["(kicad_sch",
           "\t(version 20251006)",
           "\t(generator \"microphone-array-v2\")",
           "\t(generator_version \"10.0\")",
           f"\t(uuid \"{uuid_for('sheet')}\")",
           "\t(paper \"User\" %g %g)" % (PAGE_W, PAGE_H),
           "\t(title_block",
           "\t\t(title \"16-channel PDM microphone array carrier\")",
           "\t\t(rev \"A\")",
           "\t\t(comment 1 \"Tang Nano 9K acquisition, SPI0 to a 2012 Raspberry Pi\")",
           "\t\t(comment 2 \"Generated from tools/netlist.py - do not hand edit\")",
           "\t)"]

    lib_symbols_needed = sorted({c["symbol"] for c in components} | {"power:PWR_FLAG"})
    out.append("\t(lib_symbols")
    _, definitions = load_pin_geometry(project_root)
    for lib_id in lib_symbols_needed:
        definition = definitions.get(lib_id)
        if definition is None:
            raise SystemExit(f"symbol not found: {lib_id}")
        out.append(render_symbol_definition(lib_id, definition))
    out.append("\t)")

    wires, labels, texts, instances, no_connects = [], [], [], [], []
    column_x, column_y, column_width = snap(20.0), snap(20.0), 0.0

    for title, refs in block_layout(components):
        present = [r for r in refs if r in by_ref]
        if not present:
            continue
        block_height, block_width = 0.0, 0.0
        entries = []
        for ref in present:
            component = by_ref[ref]
            pins = geometry[component["symbol"]]
            span_y = (max(p[2] for p in pins) - min(p[2] for p in pins)) if pins else 5.08
            span_x = (max(p[1] for p in pins) - min(p[1] for p in pins)) if pins else 5.08
            entries.append((ref, component, pins, span_x, span_y))
            block_height += span_y + 6 * GRID
            block_width = max(block_width, span_x + 26 * GRID)

        if column_y + block_height + 12 * GRID > PAGE_H:
            column_x += column_width + 8 * GRID
            column_y = 20.0
            column_width = 0.0
        column_width = max(column_width, block_width)

        texts.append((title, column_x, column_y))
        cursor = column_y + 4 * GRID

        for ref, component, pins, span_x, span_y in entries:
            origin_x = round((column_x + 13 * GRID) / GRID) * GRID
            origin_y = round((cursor + span_y / 2.0) / GRID) * GRID
            instances.append((ref, component, origin_x, origin_y))

            for number, px, py, angle, _length in pins:
                sx = origin_x + px
                sy = origin_y - py
                outward = (angle + 180.0) % 360.0
                dx = STUB if abs(outward) < 1e-6 else (-STUB if abs(outward - 180.0) < 1e-6 else 0.0)
                dy = 0.0
                if abs(outward - 90.0) < 1e-6:
                    dy = -STUB
                elif abs(outward - 270.0) < 1e-6:
                    dy = STUB
                ex, ey = sx + dx, sy + dy
                net = pin_net.get((ref, number))
                if net is None:
                    # Deliberately open pin: mark it so ERC treats it as intended.
                    no_connects.append((sx, sy))
                    continue
                wires.append((sx, sy, ex, ey))
                label_angle = 0 if dx >= 0 else 180
                if dy < 0:
                    label_angle = 90
                elif dy > 0:
                    label_angle = 270
                labels.append((net, ex, ey, label_angle))

            cursor += span_y + 6 * GRID

        column_y += block_height + 6 * GRID

    # Power flags
    flag_pins = geometry["power:PWR_FLAG"]
    for index, net in enumerate(POWER_FLAG_NETS):
        fx = snap(20.0 + index * 8 * GRID)
        fy = snap(PAGE_H - 30.0)
        ref = f"#FLG{index + 1:02d}"
        instances.append((ref, {"ref": ref, "symbol": "power:PWR_FLAG",
                                "value": "PWR_FLAG", "footprint": "",
                                "dnp": False, "in_bom": False, "lcsc": "",
                                "mpn": "", "manufacturer": "",
                                "description": "ERC power source marker"}, fx, fy))
        # PWR_FLAG's single pin points up out of its connection point, so the
        # stub leaves downwards and clears the flag graphic.
        _, px, py, _angle, _length = flag_pins[0]
        sx, sy = fx + px, fy - py
        wires.append((sx, sy, sx, sy + STUB))
        labels.append((net, sx, sy + STUB, 270))

    for x1, y1, x2, y2 in wires:
        out.append(f"\t(wire\n\t\t(pts\n\t\t\t(xy {x1:g} {y1:g}) (xy {x2:g} {y2:g})\n\t\t)\n"
                   f"\t\t(stroke\n\t\t\t(width 0)\n\t\t\t(type default)\n\t\t)\n"
                   f"\t\t(uuid \"{uuid_for('w', x1, y1, x2, y2)}\")\n\t)")

    # Global labels rather than local ones: a local label on the root sheet is
    # reported by KiCad as "/NAME", which would not match the plain net names
    # used on the board.
    for net, x, y, angle in labels:
        justify = "left" if angle in (0, 90) else "right"
        out.append(f"\t(global_label \"{net}\"\n\t\t(shape bidirectional)\n"
                   f"\t\t(at {x:g} {y:g} {angle:g})\n"
                   f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n\t\t\t)\n"
                   f"\t\t\t(justify {justify})\n\t\t)\n"
                   f"\t\t(uuid \"{uuid_for('l', net, x, y)}\")\n"
                   f"\t\t(property \"Intersheetrefs\" \"${{INTERSHEET_REFS}}\"\n"
                   f"\t\t\t(at {x:g} {y:g} 0)\n\t\t\t(hide yes)\n"
                   f"\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n"
                   f"\t\t)\n\t)")

    for x, y in no_connects:
        out.append(f"\t(no_connect\n\t\t(at {x:g} {y:g})\n"
                   f"\t\t(uuid \"{uuid_for('nc', x, y)}\")\n\t)")

    for title, x, y in texts:
        out.append(f"\t(text \"{title}\"\n\t\t(exclude_from_sim no)\n\t\t(at {x:g} {y:g} 0)\n"
                   f"\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 2 2)\n\t\t\t\t(bold yes)\n\t\t\t)\n"
                   f"\t\t\t(justify left bottom)\n\t\t)\n"
                   f"\t\t(uuid \"{uuid_for('t', title)}\")\n\t)")

    for ref, component, x, y in instances:
        out.append(render_instance(ref, component, x, y, geometry))

    out.append("\t(sheet_instances")
    out.append("\t\t(path \"/\"\n\t\t\t(page \"1\")\n\t\t)")
    out.append("\t)")
    out.append(")")
    return "\n".join(out) + "\n"


def render_symbol_definition(lib_id, definition):
    """Emit a library symbol verbatim under its fully qualified lib_id."""
    name, raw = definition
    body = raw.replace(f"(symbol \"{name}\"", f"(symbol \"{lib_id}\"", 1)
    return "\n".join("\t\t" + line.lstrip("\t") for line in body.splitlines())


def render_instance(ref, component, x, y, geometry):
    lib_id = component["symbol"]
    uuid = uuid_for("sym", ref)
    pins = geometry[lib_id]
    top = max(p[2] for p in pins) if pins else 2.54
    bottom = min(p[2] for p in pins) if pins else -2.54

    lines = [f"\t(symbol",
             f"\t\t(lib_id \"{lib_id}\")",
             f"\t\t(at {x:g} {y:g} 0)",
             "\t\t(unit 1)",
             "\t\t(exclude_from_sim no)",
             "\t\t(in_bom %s)" % ("yes" if component.get("in_bom") else "no"),
             "\t\t(on_board %s)" % ("no" if ref.startswith("#") else "yes"),
             "\t\t(dnp %s)" % ("yes" if component.get("dnp") else "no"),
             f"\t\t(uuid \"{uuid}\")"]

    def prop(name, value, py, hide):
        lines.append(f"\t\t(property \"{name}\" \"{value}\"")
        lines.append(f"\t\t\t(at {x:g} {py:g} 0)")
        if hide:
            lines.append("\t\t\t(hide yes)")
        lines.append("\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)")
        lines.append("\t\t)")

    prop("Reference", ref, y - top - 2.54, ref.startswith("#"))
    prop("Value", component["value"], y - bottom + 2.54, ref.startswith("#"))
    prop("Footprint", component.get("footprint", ""), y, True)
    prop("Datasheet", d.DATASHEETS.get(lib_id, ""), y, True)
    prop("Description", component.get("description", ""), y, True)
    if component.get("lcsc"):
        prop("LCSC", component["lcsc"], y, True)
        prop("MPN", component["mpn"], y, True)
        prop("Manufacturer", component["manufacturer"], y, True)

    for number, px, py, _angle, _length in pins:
        lines.append(f"\t\t(pin \"{number}\"\n\t\t\t(uuid \"{uuid_for('pin', ref, number)}\")\n\t\t)")

    lines.append("\t\t(instances")
    lines.append("\t\t\t(project \"microphone_array_v2\"")
    lines.append(f"\t\t\t\t(path \"/{uuid_for('sheet')}\"")
    lines.append(f"\t\t\t\t\t(reference \"{ref}\")\n\t\t\t\t\t(unit 1)")
    lines.append("\t\t\t\t)")
    lines.append("\t\t\t)")
    lines.append("\t\t)")
    lines.append("\t)")
    return "\n".join(lines)


if __name__ == "__main__":
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "microphone_array_v2.kicad_sch")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(generate(root))
    print(f"wrote {path}")
