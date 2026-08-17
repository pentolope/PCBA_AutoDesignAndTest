"""Can a fabricator tell how many copper layers this is, from filenames alone?

Not every fabricator reads Gerber X2 `FileFunction` attributes. A board whose
inner copper is named after the net it carries therefore has nothing in the
upload that says those files are copper, let alone where they sit in the
stack, and a multilayer board can be quoted and built with layers missing. A
board that has to deal with such a fabricator declares `fabrication_naming`,
exports without X2, and says everything through the filenames instead.

This gate then reads the archive the way that fabricator does: by name, with
the metadata gone. It proves every declared copper layer is present and
distinguishable, that each carries real drawing rather than a header, that no
X2 attribute or job file is there to be leaned on, and - the part a filename
cannot prove by itself - that each inner layer really is the plot of the KiCad
layer it claims, by comparing it against a fresh export of that layer alone.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import zipfile

from ..core import gate
from .. import gerber

# Anything that would let a reader identify a layer by metadata instead of by
# its name. The point of the export is that none of it is present.
X2_MARKERS = (
    re.compile(r"%TF\.", re.I),        # file attributes
    re.compile(r"%TA\.", re.I),        # aperture attributes
    re.compile(r"%TO\.", re.I),        # object (net) attributes
)
JOB_FILE = re.compile(r"\.gbrjob$", re.I)
# A Gerber that draws something has at least one D01/D02/D03 operation or a
# region. Headers, aperture definitions and comments alone are not geometry.
DRAWING = re.compile(r"(?:^|\*)\s*(?:X-?\d+Y-?\d+)?D0?[123]\*", re.M)
REGION = re.compile(r"%?G3[67]\*", re.M)


@gate("FAB.LAYER_IDENTITY",
      "Four copper layers identifiable from filenames, without X2 metadata",
      requires=("archive.zip", "fabrication_naming"))
def layer_identity(ctx, res):
    spec = ctx.manifest.get("fabrication_naming")
    zpath = ctx.manifest.resolve(ctx.manifest.get("archive.zip"))
    if not os.path.isfile(zpath):
        return res.errored("archive not found: " + zpath)
    res.evidence_file(zpath)

    copper = [row for row in spec["files"] if row["role"] == "copper"]
    problems = []

    with zipfile.ZipFile(zpath) as zf:
        names = zf.namelist()
        payload = {name: zf.read(name).decode("utf-8", "ignore")
                   for name in names}
    res.measurements["entries"] = sorted(names)

    # 1. exactly the four copper files, told apart by name alone
    present = [row["ship_as"] for row in copper if row["ship_as"] in names]
    res.measurements["copper_files"] = present
    res.measurements["copper_layer_count"] = len(present)
    if len(present) != len(copper):
        problems.append({
            "issue": "the archive does not carry all four copper layers under "
                     "the names the fab reads",
            "expected": [row["ship_as"] for row in copper], "found": present})
    if len(set(present)) != len(present):
        problems.append({"issue": "two copper layers share a filename",
                         "found": present})

    # 2. each copper file actually draws something
    for row in copper:
        text = payload.get(row["ship_as"])
        if text is None:
            continue
        strokes = len(DRAWING.findall(text)) + len(REGION.findall(text))
        res.measurements.setdefault("copper_operations", {})[
            row["ship_as"]] = strokes
        if strokes == 0:
            problems.append({"file": row["ship_as"],
                             "issue": "copper file carries no drawing, flash "
                                      "or region geometry"})

    # 3. no X2 attribute or job file anywhere in the archive
    metadata = []
    for name, text in payload.items():
        if JOB_FILE.search(name):
            metadata.append({"file": name, "issue": "job file in the archive"})
            continue
        for marker in X2_MARKERS:
            hit = marker.search(text)
            if hit:
                metadata.append({"file": name,
                                 "issue": "X2 attribute present: "
                                          + hit.group(0)})
                break
    res.measurements["x2_or_job_entries"] = len(metadata)
    problems.extend(metadata)

    # 4. the inner layers are the plots they claim to be
    problems.extend(_inner_layers_are_native(ctx, res, spec, zpath))
    for problem in problems[:40]:
        res.finding(**problem)
    if problems:
        return res.failed("{} fabrication-identity problem(s)".format(
            len(problems)))
    return res.passed(
        "four copper layers named {}, each with geometry, and no X2 attribute "
        "or job file to read them by".format(", ".join(present)))


def _inner_layers_are_native(ctx, res, spec, zpath):
    """Compare the shipped inner layers against a fresh single-layer export.

    A filename is a claim. This is the check that the claim is true: export
    each inner layer on its own and require the shipped file that claims it to
    be the same copper to within the geometry tolerance. Exporting one layer at
    a time removes any question of which output belongs to which layer.
    """
    inner = [row for row in spec["files"]
             if row["role"] == "copper" and row["kicad_layer"].startswith("In")]
    if not inner:
        return []
    tolerance = res.limit(ctx.manifest.geometry_profile()
                          .tolerance("layer_symmetric_difference_mm2")).value
    board = ctx.manifest.resolve(ctx.manifest.get("sources.pcb"))
    flags = [f for f in ctx.manifest.get("artifacts.gerber_export_flags")]
    problems = []
    work = tempfile.mkdtemp(prefix="fab_identity_")
    try:
        with zipfile.ZipFile(zpath) as zf:
            shipped_dir = os.path.join(work, "shipped")
            os.makedirs(shipped_dir)
            zf.extractall(shipped_dir)
        shipped, _drills, _extra = gerber.load_layers(shipped_dir)

        for row in inner:
            out = os.path.join(work, row["kicad_layer"].replace(".", "_"))
            os.makedirs(out)
            args = [ctx.kicad_cli, "pcb", "export", "gerbers", "--output", out]
            skip = False
            for flag in flags:
                if skip:
                    skip = False
                    continue
                if flag in ("--layers", "-l"):
                    skip = True
                    continue
                args.append(flag)
            args += ["--layers", row["kicad_layer"], board]
            proc = ctx.run_tool(args)
            if proc.returncode != 0:
                problems.append({"layer": row["kicad_layer"],
                                 "issue": "single-layer export failed",
                                 "stderr": (proc.stderr or "")[:160]})
                continue
            fresh, _d, _e = gerber.load_layers(out)
            if len(fresh) != 1:
                problems.append({"layer": row["kicad_layer"],
                                 "issue": "single-layer export produced {} "
                                          "files".format(len(fresh))})
                continue
            native = list(fresh.values())[0]
            claimed = shipped.get(row["ship_as"])
            if claimed is None:
                problems.append({"file": row["ship_as"],
                                 "issue": "not in the archive to compare"})
                continue
            a, b = claimed.union(), native.union()
            if a is None or b is None:
                problems.append({"file": row["ship_as"],
                                 "issue": "no geometry to compare"})
                continue
            difference = a.symmetric_difference(b).area
            res.measurements.setdefault("inner_layer_identity", {})[
                row["ship_as"]] = {
                    "kicad_layer": row["kicad_layer"],
                    "shipped_area_mm2": round(a.area, 4),
                    "native_area_mm2": round(b.area, 4),
                    "symmetric_difference_mm2": round(difference, 6),
            }
            if difference > tolerance:
                problems.append({
                    "file": row["ship_as"], "layer": row["kicad_layer"],
                    "issue": "shipped inner layer is not this KiCad layer's "
                             "own output",
                    "symmetric_difference_mm2": round(difference, 6),
                    "limit_mm2": tolerance})
            if a.area <= 0:
                problems.append({"file": row["ship_as"],
                                 "issue": "inner layer renders no copper"})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return problems
