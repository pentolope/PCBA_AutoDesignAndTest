"""Compare the netlist KiCad extracts from the generated schematic with the
netlist in netlist.py.

This is the gate that proves the schematic really encodes the intended design
rather than merely being syntactically valid. Run it after every schematic
regeneration.
"""

import os
import subprocess
import sys
import sexpr
import netlist as nl

KICAD_CLI = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"


def export(project_root):
    out_dir = os.path.join(project_root, "generated")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "netlist.net")
    subprocess.run(
        [KICAD_CLI, "sch", "export", "netlist", "--format", "kicadsexpr",
         "-o", path, os.path.join(project_root, "microphone_array_v2.kicad_sch")],
        check=True, capture_output=True, text=True)
    return path


def read_exported(path):
    with open(path, "r", encoding="utf-8") as handle:
        document = sexpr.parse(handle.read())[0]
    nets = {}
    nets_node = sexpr.first(document, "nets")
    for net in sexpr.children(nets_node, "net"):
        # Local labels on the root sheet come back as "/NAME".
        name = sexpr.first(net, "name")[1].lstrip("/")
        pins = set()
        for node in sexpr.children(net, "node"):
            ref = sexpr.first(node, "ref")[1]
            pin = sexpr.first(node, "pin")[1]
            pins.add((ref, pin))
        nets[name] = pins

    components = {}
    comps_node = sexpr.first(document, "components")
    for comp in sexpr.children(comps_node, "comp"):
        ref = sexpr.first(comp, "ref")[1]
        value = sexpr.first(comp, "value")
        footprint = sexpr.first(comp, "footprint")
        components[ref] = {
            "value": value[1] if value else "",
            "footprint": footprint[1] if footprint else "",
        }
    return nets, components


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    exported_nets, exported_components = read_exported(export(root))
    want_components, want_nets = nl.build()

    problems = []

    want_net_sets = {name: {(r, p) for r, p in pins}
                     for name, pins in want_nets.items()}
    # Power flag pins are schematic-only and carry '#' references.
    got_net_sets = {name: {(r, p) for r, p in pins if not r.startswith("#")}
                    for name, pins in exported_nets.items()}

    # Pins KiCad reports as unconnected must be exactly the declared open set.
    got_open = set()
    for name, pins in list(got_net_sets.items()):
        if name.startswith("unconnected-"):
            got_open |= pins
            del got_net_sets[name]
    want_open = nl.expected_unconnected()
    for pin in sorted(want_open - got_open):
        problems.append(f"pin expected open but is connected: {pin[0]}.{pin[1]}")
    for pin in sorted(got_open - want_open):
        problems.append(f"pin unexpectedly left unconnected: {pin[0]}.{pin[1]}")

    missing = sorted(set(want_net_sets) - set(got_net_sets))
    extra = sorted(n for n in set(got_net_sets) - set(want_net_sets)
                   if got_net_sets[n])
    for name in missing:
        problems.append(f"net missing from schematic: {name}")
    for name in extra:
        problems.append(f"unexpected net in schematic: {name} -> "
                        f"{sorted(got_net_sets[name])}")

    for name in sorted(set(want_net_sets) & set(got_net_sets)):
        want, got = want_net_sets[name], got_net_sets[name]
        if want != got:
            problems.append(
                f"net {name}: missing {sorted(want - got)} extra {sorted(got - want)}")

    board_components = {c["ref"]: c for c in want_components}
    for ref, component in sorted(board_components.items()):
        got = exported_components.get(ref)
        if got is None:
            problems.append(f"component missing from schematic: {ref}")
            continue
        if got["footprint"] != component["footprint"]:
            problems.append(
                f"{ref}: footprint {got['footprint']!r} != {component['footprint']!r}")
        if got["value"] != component["value"]:
            problems.append(
                f"{ref}: value {got['value']!r} != {component['value']!r}")

    for ref in sorted(set(exported_components) - set(board_components)):
        if not ref.startswith("#"):
            problems.append(f"unexpected component in schematic: {ref}")

    if problems:
        print(f"NETLIST PARITY FAILED ({len(problems)} problems)")
        for line in problems[:60]:
            print("  " + line)
        if len(problems) > 60:
            print(f"  ... and {len(problems) - 60} more")
        return 1

    print(f"netlist parity OK: {len(want_net_sets)} nets, "
          f"{len(board_components)} components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
