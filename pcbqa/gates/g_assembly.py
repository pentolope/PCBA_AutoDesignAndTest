"""Assembly-data parity, with the schematic as the authority.

What gets soldered onto a board is decided by the schematic: it is where a part
number, a value and a do-not-populate flag are authored. The PCB carries the
same parts, but only as a consequence, and it can disagree - a footprint can
exist on the board with no symbol behind it at all.

So the assembly truth here is built from the *native schematic*, exported by
kicad-cli, and then joined to the native board for placement. Three comparisons
follow from that one truth:

  * schematic against board       - does the layout carry the design's parts?
  * schematic against packaged BOM - does the order match the design?
  * schematic + board against CPL - is each part placed where the board says?

The previous implementation read the assembly fields off the PCB footprints and
called that "native". It could not have detected a footprint with no symbol, a
part number that exists only in the layout, or a DNP flag set in one file and
not the other, because it only ever consulted one file.
"""

from __future__ import annotations

import csv
import os
from collections import Counter

from ..core import gate
from ..orientation import Registry


# ---------------------------------------------------------------------------
# the two native sources
# ---------------------------------------------------------------------------

def _read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _truthy(text, tokens):
    return str(text or "").strip().lower() in {t.lower() for t in tokens}


def _schematic_parts(ctx):
    """{ref: {...}} straight from the native schematic, one row per symbol."""
    def build():
        spec = ctx.manifest.get("assembly.schematic_export")
        out = os.path.join(ctx.workdir, "native_schematic_bom.csv")
        args = [ctx.kicad_cli, "sch", "export", "bom",
                "--output", out,
                "--fields", ",".join(spec["fields"]),
                "--labels", ",".join(spec["labels"]),
                # One row per symbol and no reference ranges: grouping is a
                # presentation choice, and this is the authority, not a report.
                "--group-by", "",
                "--ref-range-delimiter", "",
                "--sort-field", spec["reference_label"]]
        args += list(spec.get("flags", [])) + [ctx.schematic_path()]
        proc = ctx.run_tool(args)
        if proc.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(
                "could not export the native schematic BOM (exit {}): {}".format(
                    proc.returncode, (proc.stderr or "").strip()[:300]))
        parts = {}
        for row in _read_csv(out):
            ref = (row.get(spec["reference_label"]) or "").strip()
            if not ref:
                continue
            dnp = _truthy(row.get(spec["dnp_label"]), spec["true_tokens"])
            excluded = _truthy(row.get(spec["exclude_label"]), spec["true_tokens"])
            parts[ref] = {
                "reference": ref,
                "value": (row.get(spec["value_label"]) or "").strip(),
                "footprint": (row.get(spec["footprint_label"]) or "").strip(),
                "dnp": dnp,
                "excluded_from_bom": excluded,
                "populated": not (dnp or excluded),
                "fields": {name: (row.get(name) or "").strip()
                           for name in ctx.manifest.get("assembly.schematic_fields")
                           if name in row},
                "source_row": row,
            }
        return parts
    return ctx.cache("schematic_parts", build)


def _board_parts(ctx):
    """{ref: {...}} from the native board: placement and layout-side flags."""
    def build():
        board = ctx.board()
        parts = {}
        for fp in board.Footprints():
            pos = fp.GetPosition()
            parts[fp.GetReference()] = {
                "reference": fp.GetReference(),
                "value": fp.GetValue(),
                "footprint": fp.GetFPIDAsString(),
                "dnp": bool(fp.IsDNP()),
                "excluded_from_bom": bool(fp.IsExcludedFromBOM()),
                "side": "Bottom" if fp.IsFlipped() else "Top",
                "x_mm": round(pos.x / 1e6, 4),
                "y_mm": round(-pos.y / 1e6, 4),
                "rotation_deg": round(fp.GetOrientationDegrees() % 360.0, 4),
            }
        return parts
    return ctx.cache("board_parts", build)


def _assembly_truth(ctx):
    """Join the two natives and record every way in which they disagree."""
    def build():
        sch = _schematic_parts(ctx)
        brd = _board_parts(ctx)
        problems = []
        # The schematic is read through its BOM export, and a BOM export cannot
        # carry a symbol marked exclude-from-BOM: KiCad 10 deprecated
        # --include-excluded-from-bom and it now has no effect. So a mounting
        # hole, a test pad or a hand-fitted connector is absent from the export
        # by construction, and comparing every board footprint against it
        # reported twenty-one parts as missing from a schematic that has all of
        # them. Only parts the export could have contained are compared; the
        # rest are recorded, so an unexpected one is still visible.
        comparable = {ref: entry for ref, entry in brd.items()
                      if not entry["excluded_from_bom"]}
        not_in_bom = sorted(set(brd) - set(comparable))
        for ref in sorted(set(comparable) - set(sch)):
            entry = brd[ref]
            problems.append({
                "reference": ref,
                "issue": "on the board but in no schematic symbol; the schematic "
                         "is the assembly authority, so this part is not part of "
                         "the design",
                "board_footprint": entry["footprint"],
                "board_excluded_from_bom": entry["excluded_from_bom"],
                "board_dnp": entry["dnp"]})
        for ref in sorted(set(sch) - set(brd)):
            problems.append({"reference": ref,
                             "issue": "in the schematic but placed on no board "
                                      "footprint"})
        parts = {}
        for ref in sorted(set(sch) & set(brd)):
            s, b = sch[ref], brd[ref]
            if s["value"] != b["value"]:
                problems.append({"reference": ref, "issue": "value differs between "
                                                            "schematic and board",
                                 "schematic": s["value"], "board": b["value"]})
            if s["footprint"] != b["footprint"]:
                problems.append({"reference": ref,
                                 "issue": "footprint differs between schematic and "
                                          "board",
                                 "schematic": s["footprint"],
                                 "board": b["footprint"]})
            if s["dnp"] != b["dnp"]:
                problems.append({"reference": ref,
                                 "issue": "do-not-populate differs between "
                                          "schematic and board",
                                 "schematic": s["dnp"], "board": b["dnp"]})
            if s["excluded_from_bom"] != b["excluded_from_bom"]:
                problems.append({"reference": ref,
                                 "issue": "exclude-from-BOM differs between "
                                          "schematic and board",
                                 "schematic": s["excluded_from_bom"],
                                 "board": b["excluded_from_bom"]})
            merged = dict(s)
            merged.update({k: b[k] for k in
                           ("side", "x_mm", "y_mm", "rotation_deg")})
            parts[ref] = merged
        return {"parts": parts, "parity": problems,
                "schematic_only": sorted(set(sch) - set(brd)),
                "board_only": sorted(set(comparable) - set(sch)),
                "board_not_in_bom_export": not_in_bom}
    return ctx.cache("assembly_truth", build)


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

@gate("BOM.NATIVE_PARITY", "Packaged BOM matches the native schematic",
      requires=("artifacts.bom", "assembly.bom_fields",
                "assembly.schematic_export"))
def bom_parity(ctx, res):
    path = ctx.manifest.resolve(ctx.manifest.get("artifacts.bom"))
    if not os.path.isfile(path):
        return res.errored("packaged BOM not found: {}".format(path))
    res.evidence_file(path)
    res.evidence_file(ctx.schematic_path())
    fields = ctx.manifest.get("assembly.bom_fields")
    required = res.limit(ctx.manifest.constraint(
        "assembly.required_part_fields", units="field name",
        cid="assembly.required_part_fields")).value
    compared = res.limit(ctx.manifest.constraint(
        "assembly.compared_part_fields", units="field name",
        cid="assembly.compared_part_fields")).value

    try:
        truth = _assembly_truth(ctx)
    except RuntimeError as exc:
        return res.errored(str(exc))
    native = truth["parts"]
    expected = {r: e for r, e in native.items() if e["populated"]}
    rows = _read_csv(path)
    res.measurements["bom_lines"] = len(rows)
    res.measurements["schematic_symbols"] = len(_schematic_parts(ctx))
    res.measurements["board_footprints"] = len(_board_parts(ctx))
    res.measurements["schematic_populated"] = len(expected)
    res.measurements["board_not_in_bom_export"] = truth["board_not_in_bom_export"]
    res.measurements["uncomparable_fields"] = [f for f in compared
                                               if fields.get(f) is None]

    problems = list(truth["parity"])
    seen = Counter()
    packaged = {}
    for index, row in enumerate(rows, start=2):
        refs = [r.strip() for r in (row.get(fields["designators"]) or "").split(",")
                if r.strip()]
        if not refs:
            problems.append({"line": index, "issue": "BOM line names no designator"})
            continue
        try:
            quantity = int(float(row.get(fields["quantity"]) or 0))
        except ValueError:
            quantity = None
        if quantity is None:
            problems.append({"line": index,
                             "issue": "BOM line carries no readable quantity"})
        elif quantity != len(refs):
            problems.append({"line": index,
                             "issue": "quantity does not match the designator list",
                             "quantity": quantity, "designators": len(refs)})
        for ref in refs:
            seen[ref] += 1
            packaged[ref] = {"line": index, "row": row}

    for ref, count in sorted(seen.items()):
        if count > 1:
            problems.append({"reference": ref, "count": count,
                             "issue": "designator appears on more than one BOM line"})
    for ref in sorted(set(expected) - set(packaged)):
        problems.append({"reference": ref,
                         "issue": "populated in the schematic but missing from the "
                                  "BOM"})
    for ref in sorted(set(packaged) - set(expected)):
        state = native.get(ref)
        problems.append({"reference": ref,
                         "issue": "in the BOM but not populated in the schematic",
                         "in_schematic": ref in _schematic_parts(ctx),
                         "dnp": state["dnp"] if state else None,
                         "excluded_from_bom": (state["excluded_from_bom"]
                                               if state else None)})

    for ref in sorted(set(expected) & set(packaged)):
        want, row = expected[ref], packaged[ref]["row"]
        line = packaged[ref]["line"]
        got_value = (row.get(fields["value"]) or "").strip()
        if got_value != want["value"]:
            problems.append({"reference": ref, "line": line, "issue": "value mismatch",
                             "schematic": want["value"], "packaged": got_value})
        got_fp = (row.get(fields["footprint"]) or "").strip()
        if not got_fp:
            problems.append({"reference": ref, "line": line,
                             "issue": "BOM line names no footprint"})
        elif got_fp not in want["footprint"]:
            problems.append({"reference": ref, "line": line,
                             "issue": "footprint mismatch",
                             "schematic": want["footprint"], "packaged": got_fp})
        for field_name in compared:
            column = fields.get(field_name)
            if column is None:
                continue                       # not carried by this BOM format
            got = (row.get(column) or "").strip()
            expect = (want["fields"].get(field_name) or "").strip()
            # An empty field is never a wildcard in either direction: a blank
            # in the design does not licence any value in the order, and a
            # blank in the order does not licence a missing part number.
            if expect and got and got != expect:
                problems.append({"reference": ref, "line": line,
                                 "issue": "{} disagrees with the schematic".format(
                                     field_name),
                                 "schematic": expect, "packaged": got})
            elif expect and not got:
                problems.append({"reference": ref, "line": line,
                                 "issue": "{} is in the schematic but blank in the "
                                          "BOM".format(field_name),
                                 "schematic": expect})
            elif got and not expect:
                problems.append({"reference": ref, "line": line,
                                 "issue": "{} appears in the BOM but the schematic "
                                          "carries no such value".format(field_name),
                                 "packaged": got})
            elif field_name in required and not expect and not got:
                problems.append({"reference": ref, "line": line,
                                 "issue": "required part field {!r} is empty in "
                                          "both the schematic and the "
                                          "BOM".format(field_name)})

    for p in problems[:80]:
        res.finding(**p)
    if problems:
        return res.failed("{} disagreement(s) between the native schematic, the "
                          "board and the packaged BOM".format(len(problems)))
    return res.passed(
        "the schematic, the board and the packaged BOM agree on all {} populated "
        "parts".format(len(expected)))


# ---------------------------------------------------------------------------
# CPL
# ---------------------------------------------------------------------------

@gate("CPL.NATIVE_PARITY", "Packaged CPL matches the native design",
      requires=("artifacts.cpl", "artifacts.cpl_fields",
                "assembly.schematic_export"))
def cpl_parity(ctx, res):
    path = ctx.manifest.resolve(ctx.manifest.get("artifacts.cpl"))
    if not os.path.isfile(path):
        return res.errored("packaged CPL not found: {}".format(path))
    res.evidence_file(path)
    fields = ctx.manifest.get("artifacts.cpl_fields")
    tolerance = res.limit(ctx.manifest.constraint(
        "artifacts.position_tolerance_mm", units="mm",
        cid="artifacts.position_tolerance_mm"))
    rotation_tolerance = res.limit(ctx.manifest.geometry_profile()
                                   .tolerance("rotation_match_deg"))
    origin = res.limit(ctx.manifest.constraint(
        "artifacts.cpl_origin", units="frame", cid="artifacts.cpl_origin")).value
    # A packaged rotation is not required to equal the board's. Where the
    # fab's library holds a part at a different zero orientation from the
    # footprint, the placement file carries the difference, and that is the
    # correction being shipped rather than a disagreement to report. What the
    # board is compared against is the board angle plus that declared offset.
    # CPL.ORIENTATION is what proves the offsets themselves are right.
    offsets, key = {}, None
    if ctx.manifest.has("release_generation.cpl_orientation"):
        registry = Registry(ctx.manifest.get("release_generation.cpl_orientation"))
        offsets = {number: registry.offset(number)
                   for number in registry.entries}
        key = registry.part_number_field
    lcsc_of = {}
    if offsets:
        for fp in ctx.board().Footprints():
            for field in fp.GetFields():
                if field.GetName() == key and field.GetText().strip():
                    lcsc_of[fp.GetReference()] = field.GetText().strip()

    try:
        truth = _assembly_truth(ctx)
    except RuntimeError as exc:
        return res.errored(str(exc))
    # Which parts belong on the CPL is a schematic question; where they go is a
    # board question. Both natives are consulted, neither is guessed at.
    expected = {r: e for r, e in truth["parts"].items() if e["populated"]}
    rows = {}
    duplicates = []
    for row in _read_csv(path):
        ref = (row.get(fields["designator"]) or "").strip()
        if ref in rows:
            duplicates.append(ref)
        rows[ref] = row
    res.measurements["cpl_rows"] = len(rows)
    res.measurements["schematic_populated"] = len(expected)
    res.measurements["coordinate_frame"] = origin

    dx, dy = origin.get("offset_mm", [0.0, 0.0])
    problems = [{"reference": r, "issue": "designator appears twice in the CPL"}
                for r in sorted(set(duplicates))]
    for ref in sorted(set(expected) | set(rows)):
        if ref not in rows:
            problems.append({"reference": ref,
                             "issue": "populated in the schematic but absent from "
                                      "the CPL"})
            continue
        if ref not in expected:
            problems.append({"reference": ref,
                             "issue": "in the CPL but not populated in the "
                                      "schematic"})
            continue
        want, row = expected[ref], rows[ref]
        got_side = (row.get(fields["side"]) or "").strip().lower()
        if got_side != want["side"].lower():
            problems.append({"reference": ref, "issue": "side mismatch",
                             "board": want["side"], "packaged": row.get(fields["side"])})
        for key, axis, shift in (("x", "x_mm", dx), ("y", "y_mm", dy)):
            try:
                got = float(row[fields[key]])
            except (KeyError, TypeError, ValueError):
                problems.append({"reference": ref,
                                 "issue": "unparseable {}".format(key)})
                continue
            delta = abs(got - (want[axis] + shift))
            if tolerance.violated_maximum(delta):
                problems.append({"reference": ref,
                                 "issue": "{} mismatch".format(key),
                                 "board_mm": want[axis], "packaged": got,
                                 "delta_mm": round(delta, 4)})
        try:
            got = float(row[fields["rotation"]]) % 360.0
        except (KeyError, TypeError, ValueError):
            problems.append({"reference": ref, "issue": "unparseable rotation"})
        else:
            offset = offsets.get(lcsc_of.get(ref), 0.0)
            want_rot = (want["rotation_deg"] + offset) % 360.0
            angular_difference = abs(((got - want_rot + 180) % 360) - 180)
            if rotation_tolerance.violated_maximum(angular_difference):
                problems.append({"reference": ref, "issue": "rotation mismatch",
                                 "board_deg": want["rotation_deg"],
                                 "library_zero_offset_deg": offset,
                                 "expected_deg": round(want_rot, 4),
                                 "packaged": got})
    for p in problems[:80]:
        res.finding(**p)
    if problems:
        return res.failed("{} disagreement(s) between the native design and the "
                          "packaged CPL".format(len(problems)))
    return res.passed(
        "all {} populated parts agree between the native design and the packaged "
        "CPL".format(len(expected)))
