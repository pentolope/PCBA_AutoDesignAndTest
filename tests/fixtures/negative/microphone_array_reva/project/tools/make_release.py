"""Produce the JLCPCB fabrication and assembly package.

Everything is generated from one board revision in a single run into an empty
directory: Gerbers, drills, the native position export, the JLCPCB BOM and CPL,
and renders. The result is a release *candidate* - it is not an approved
production file until a human has checked JLCPCB's live previews.
"""

import csv
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

import pcbnew

import netlist as nl

KICAD_CLI = r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"
GERBER_LAYERS = ",".join([
    "F.Cu", "In1.Cu", "In2.Cu", "B.Cu",
    "F.Paste", "B.Paste",
    "F.Silkscreen", "B.Silkscreen",
    "F.Mask", "B.Mask",
    "Edge.Cuts",
])


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"command failed: {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    board_path = os.path.join(root, "microphone_array_v2.kicad_pcb")
    schematic = os.path.join(root, "microphone_array_v2.kicad_sch")
    out = os.path.join(root, "generated", "release")
    if os.path.isdir(out):
        shutil.rmtree(out)
    gerbers = os.path.join(out, "gerbers")
    os.makedirs(gerbers)

    # --- fabrication -----------------------------------------------------
    run(KICAD_CLI, "pcb", "export", "gerbers", "--output", gerbers,
        "--layers", GERBER_LAYERS, "--no-protel-ext", "--use-drill-file-origin",
        "--subtract-soldermask", board_path)
    run(KICAD_CLI, "pcb", "export", "drill", "--output", gerbers,
        "--format", "excellon", "--drill-origin", "plot",
        "--excellon-separate-th", "--generate-map", "--map-format", "gerberx2",
        board_path)

    # --- assembly --------------------------------------------------------
    # --use-drill-file-origin matches the Gerber and drill exports above, so
    # all three artefacts share one coordinate origin.
    positions = os.path.join(out, "positions.csv")
    run(KICAD_CLI, "pcb", "export", "pos", "--output", positions,
        "--format", "csv", "--units", "mm", "--side", "front",
        "--use-drill-file-origin", "--exclude-dnp", board_path)

    write_bom_and_cpl(board_path, out, positions)

    # --- renders ---------------------------------------------------------
    renders = os.path.join(out, "renders")
    os.makedirs(renders)
    for side in ("top", "bottom"):
        run(KICAD_CLI, "pcb", "render", "--output",
            os.path.join(renders, f"{side}.png"), "--side", side,
            "--width", "1600", "--height", "1600", "--quality", "high",
            board_path)
    run(KICAD_CLI, "sch", "export", "pdf", "--output",
        os.path.join(out, "schematic.pdf"), schematic)

    # --- archive ---------------------------------------------------------
    archive = os.path.join(out, "microphone_array_v2-revA-fabrication.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(gerbers)):
            zf.write(os.path.join(gerbers, name), name)

    write_manifest(out, archive, board_path)
    print(f"release candidate written to {out}")
    return 0


def write_bom_and_cpl(board_path, out, positions_path):
    """Write a JLCPCB BOM and CPL derived from the native position export."""
    components, _nets = nl.build()
    by_ref = {c["ref"]: c for c in components}
    board = pcbnew.LoadBoard(board_path)

    with open(positions_path, newline="", encoding="utf-8") as handle:
        placements = list(csv.DictReader(handle))

    cpl_rows, bom_groups = [], {}
    for row in placements:
        ref = row["Ref"]
        component = by_ref.get(ref)
        if component is None or not component["in_bom"] or component["dnp"]:
            continue
        cpl_rows.append({
            "Designator": ref,
            "Mid X": f"{float(row['PosX']):.4f}",
            "Mid Y": f"{float(row['PosY']):.4f}",
            "Layer": "Top" if row["Side"].lower() == "top" else "Bottom",
            "Rotation": f"{float(row['Rot']) % 360.0:.4f}",
        })
        key = (component["value"], component["footprint"], component["lcsc"])
        bom_groups.setdefault(key, []).append(ref)

    with open(os.path.join(out, "cpl.csv"), "w", newline="",
              encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["Designator", "Mid X", "Mid Y", "Layer", "Rotation"])
        writer.writeheader()
        writer.writerows(sorted(cpl_rows, key=lambda r: r["Designator"]))

    with open(os.path.join(out, "bom.csv"), "w", newline="",
              encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Comment", "Designator", "Footprint", "LCSC Part #",
                         "Quantity"])
        for (value, footprint, lcsc), refs in sorted(
                bom_groups.items(), key=lambda kv: kv[0][0]):
            writer.writerow([value, ",".join(sorted(refs)),
                             footprint.split(":", 1)[-1], lcsc, len(refs)])

    # Cross-check: every populated reference appears in both files exactly once.
    cpl_refs = {r["Designator"] for r in cpl_rows}
    bom_refs = {ref for refs in bom_groups.values() for ref in refs}
    if cpl_refs != bom_refs:
        raise SystemExit(
            f"BOM/CPL reference mismatch: only in CPL {sorted(cpl_refs - bom_refs)}, "
            f"only in BOM {sorted(bom_refs - cpl_refs)}")

    expected = {c["ref"] for c in components
                if c["in_bom"] and not c["dnp"]
                and not board.FindFootprintByReference(c["ref"]).IsFlipped()}
    if cpl_refs != expected:
        raise SystemExit(
            f"CPL does not match the populated top-side set: "
            f"missing {sorted(expected - cpl_refs)}, extra {sorted(cpl_refs - expected)}")
    print(f"  BOM lines {len(bom_groups)}, placements {len(cpl_rows)}")


def write_manifest(out, archive, board_path):
    lines = ["# Release candidate manifest", ""]
    for path in (archive, board_path,
                 os.path.join(out, "bom.csv"),
                 os.path.join(out, "cpl.csv"),
                 os.path.join(out, "positions.csv")):
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        lines.append(f"- `{os.path.basename(path)}` sha256 `{digest}`")
    lines += [
        "",
        "This is a release candidate. Before ordering, inspect JLCPCB's live",
        "Gerber and pick-and-place previews and confirm every component match",
        "and placement orientation. Local renders cannot prove JLCPCB's",
        "library-model zero orientation.",
        "",
    ]
    with open(os.path.join(out, "MANIFEST.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
