"""Per-object parity between the native board and the exported fabrication data.

Two gates live here, both of which used to compare only totals:

  VIA.NATIVE_GERBER_AGREEMENT   matches every via individually - by coordinate,
                                drill, annulus diameter and geometry - and
                                compares its full mask classification on both
                                sides of the export boundary.

  STACK.GERBER_PARITY           compares the shipped copper layers against a
                                fresh export of the same native board, layer by
                                layer, by geometric symmetric difference.

Counting agreement is not agreement: a defect that moves from one via to
another, or a layer that was edited after export, both preserve totals.
"""

from __future__ import annotations

import os
import shutil

from shapely.geometry import Point

from ..core import gate
from .. import geom, gerber


# Gerber space is the KiCad page with Y mirrored. Native geometry keeps page
# coordinates, so this is the whole transform.
def _to_gerber(x_mm, y_mm):
    return (x_mm, -y_mm)


def _find_layers(directory, manifest=None):
    """Every exported layer, indexed by the role its filename declares.

    Layers are found through their Gerber X2 file function where one exists.
    A board whose fabricator does not read X2 exports without it, and a lookup
    through an attribute that is not there finds nothing at all and quietly
    compares no layers. Such a board declares `fabrication_naming` - the same
    mapping the release renames by - so what is looked up here is what was
    shipped.
    """
    layers, drills, _extra = gerber.load_layers(directory)
    by_role = {}
    spec = manifest.get("fabrication_naming", None) if manifest else None
    if spec:
        for row in spec["files"]:
            # A directory here is either the shipped archive, where files
            # carry their fabrication names, or a fresh export straight out of
            # KiCad, where they still carry KiCad's. Both have to resolve,
            # because the whole point of comparing them is to show they are
            # the same copper under two names.
            name, entry = None, None
            for candidate in (row["ship_as"],):
                if candidate in layers:
                    name, entry = candidate, layers[candidate]
                    break
            if entry is None:
                suffix = row.get("kicad_suffix") or ("." + row["kicad_ext"])
                for candidate in sorted(layers):
                    if candidate.lower().endswith(suffix.lower()):
                        name, entry = candidate, layers[candidate]
                        break
            if entry is None:
                continue
            key = (row["role"], row.get("side"))
            by_role.setdefault(key, []).append((name, entry))
            by_role.setdefault(("layer", row["kicad_layer"]), []).append(
                (name, entry))
    else:                                   # boards that still ship X2
        for name, f in layers.items():
            fn = (f.file_function or "").strip().lower()
            if fn.startswith("copper,"):
                side = "front" if fn.endswith(",top") else (
                    "back" if fn.endswith(",bot") else None)
                by_role.setdefault(("copper", side), []).append((name, f))
                # The identity a copper layer is compared *by*. For a board
                # naming its files, that is the KiCad layer; for one shipping
                # X2, the file function. Without this the per-layer comparison
                # below finds no layers on an X2 board and passes having
                # compared nothing, which is worse than failing.
                by_role.setdefault(("layer", fn), []).append((name, f))
            elif fn.startswith("soldermask,"):
                side = "front" if fn.endswith(",top") else "back"
                by_role.setdefault(("soldermask", side), []).append((name, f))
    return layers, drills, by_role


def _mask_for(by_role, side):
    entries = by_role.get(("soldermask", side))
    return entries[0] if entries else None


def _copper_for(by_role, side):
    entries = by_role.get(("copper", side))
    return entries[0] if entries else None


def _classify(annulus, centre, openings, target, process, contact_tol, tie_tol):
    """Full mask classification for one via against a set of opening polygons.

    `tie` lists every opening as close as the nearest one to within `tie_tol`.
    A via sitting exactly between two pads has no single "nearest" opening, and
    two implementations may pick different members of that set; comparing
    identity against the whole tie set is the honest comparison.
    """
    best = None
    distances = []
    for index, poly in openings:
        d_ann = annulus.distance(poly)
        distances.append((d_ann, poly))
        if best is None or d_ann < best["annulus_to_opening_mm"]:
            inside = poly.contains(centre)
            best = {
                "opening_index": index,
                "opening_centroid": (round(poly.centroid.x, 4),
                                     round(poly.centroid.y, 4)),
                "annulus_to_opening_mm": round(d_ann, 4),
                "centre_inside_opening": bool(inside),
                "contacts": bool(not contact_tol.violated_maximum(d_ann)),
                "overlaps": bool(annulus.intersection(poly).area > 0.0),
            }
    if best is None:
        return None
    floor = min(d for d, _p in distances)
    best["tie_centroids"] = [(round(p.centroid.x, 4), round(p.centroid.y, 4))
                             for d, p in distances
                             if tie_tol.within(d, floor)]
    best["below_target"] = target.violated_minimum(
        best["annulus_to_opening_mm"])
    best["below_process"] = process.violated_minimum(
        best["annulus_to_opening_mm"])
    return best


@gate("VIA.NATIVE_GERBER_AGREEMENT",
      "Every via agrees between the native board and the export",
      gate_class="release-artifact", requires=("via_mask.design_target_mm", "via_mask.process.limit_mm",
                "artifacts.gerber_dir"))
def via_export_parity(ctx, res):
    from .g_geometry import _via_survey

    directory = ctx.manifest.resolve(ctx.manifest.get("artifacts.gerber_dir"))
    if not os.path.isdir(directory):
        return res.errored("gerber directory not found: " + directory)

    profile = ctx.manifest.geometry_profile()
    geom.configure(res.limit(
        profile.tolerance("polygon_chord_error_mm")).value)
    coord_tol = res.limit(profile.tolerance("coordinate_match_mm"))
    dim_tol = res.limit(profile.tolerance("dimension_match_mm"))
    dist_tol = res.limit(profile.tolerance("clearance_match_mm"))
    contact_tol = res.limit(profile.tolerance("contact_mm"))
    target = res.limit(ctx.manifest.constraint(
        "via_mask.design_target_mm", units="mm",
        cid="via_mask.design_target_mm"))
    process = res.limit(ctx.manifest.constraint(
        "via_mask.process.limit_mm", units="mm",
        cid="via_mask.process.limit_mm"))

    _geom, native_rows = _via_survey(ctx)
    _layers, drills, by_function = _find_layers(directory, ctx.manifest)
    plated = [(x, y, dia) for f in drills.values() if f.plated
              for (x, y, dia, _p) in f.holes]

    sides = {}
    for side in ("front", "back"):
        mask = _mask_for(by_function, side)
        copper = _copper_for(by_function, side)
        if mask is None or copper is None:
            return res.errored(f"export has no {side} mask or copper layer")
        sides[side] = {
            "mask_name": mask[0],
            "openings": list(enumerate(p for p, dark in mask[1].shapes if dark)),
            "copper_circles": copper[1].circular_flashes(),
            "copper_name": copper[0],
        }
        res.evidence_file(os.path.join(directory, mask[0]))

    problems = []
    matched = 0
    for row in native_rows:
        gx, gy = _to_gerber(row["x_mm"], row["y_mm"])
        label = f"via[{row['net']}]@{row['x_mm']},{row['y_mm']}"

        hits = [h for h in plated
                if coord_tol.within(h[0], gx) and coord_tol.within(h[1], gy)]
        if not hits:
            problems.append({"via": label,
                             "issue": "no plated drill hit at this coordinate"})
            continue
        if all(dim_tol.differs_by_more_than(h[2], row["drill_mm"])
               for h in hits):
            problems.append({"via": label, "issue": "drill diameter disagrees",
                             "native_mm": row["drill_mm"],
                             "export_mm": round(hits[0][2], 4)})
            continue

        annuli = {}
        bad_annulus = False
        for side, data in sides.items():
            circles = [c for c in data["copper_circles"]
                       if coord_tol.within(c[0], gx)
                       and coord_tol.within(c[1], gy)
                       and dim_tol.within(c[2], row["pad_mm"])]
            if not circles:
                problems.append({"via": label, "side": side,
                                 "issue": "no matching annulus in the exported copper",
                                 "expected_diameter_mm": row["pad_mm"]})
                bad_annulus = True
                continue
            annuli[side] = circles[0][3]
        if bad_annulus:
            continue
        matched += 1

        for side, native in row["sides"].items():
            if side not in annuli:
                continue
            export = _classify(annuli[side], Point(gx, gy),
                               sides[side]["openings"], target, process,
                               contact_tol, dist_tol)
            if export is None:
                problems.append({"via": label, "side": side,
                                 "issue": "no mask openings on this side of the export"})
                continue

            native_class = {
                "annulus_to_opening_mm": native["annulus_to_opening_mm"],
                "centre_inside_opening": native["centre_inside_opening"],
                "contacts": native["annulus_contacts_opening"],
                "overlaps": native["annulus_overlaps_opening"],
                "below_target": target.violated_minimum(
                    native["annulus_to_opening_mm"]),
                "below_process": process.violated_minimum(
                    native["annulus_to_opening_mm"]),
            }
            if dist_tol.differs_by_more_than(
                    export["annulus_to_opening_mm"],
                    native_class["annulus_to_opening_mm"]):
                problems.append({
                    "via": label, "side": side, "issue": "signed clearance disagrees",
                    "native_mm": native_class["annulus_to_opening_mm"],
                    "export_mm": export["annulus_to_opening_mm"],
                    "nearest_pad_native": native["pad"]})
            for field in ("centre_inside_opening", "contacts", "overlaps",
                          "below_target", "below_process"):
                if export[field] != native_class[field]:
                    problems.append({
                        "via": label, "side": side,
                        "issue": f"{field} disagrees",
                        "native": native_class[field], "export": export[field],
                        "nearest_pad_native": native["pad"]})

            # opening identity: the nearest opening must be the same object
            native_poly = _native_opening_polygon(ctx, native["pad"], side)
            if native_poly is not None:
                want = _to_gerber(native_poly.centroid.x, native_poly.centroid.y)
                got = export["opening_centroid"]
                tied = any(coord_tol.within(want[0], c[0])
                           and coord_tol.within(want[1], c[1])
                           for c in export["tie_centroids"])
                if not tied:
                    problems.append({
                        "via": label, "side": side,
                        "issue": "nearest mask opening is a different object",
                        "native_pad": native["pad"],
                        "native_centroid": (round(want[0], 4), round(want[1], 4)),
                        "export_centroid": got,
                        "export_tie_set": export["tie_centroids"][:4]})

    res.measurements.update({
        "vias_native": len(native_rows),
        "vias_matched_in_export": matched,
        "plated_drill_hits": len(plated),
        "mask_openings_front": len(sides["front"]["openings"]),
        "mask_openings_back": len(sides["back"]["openings"]),
        "comparisons_per_via": ["coordinate", "drill", "annulus diameter",
                                "signed clearance", "contact", "positive overlap",
                                "centre inside", "target class", "process class",
                                "nearest-opening identity"],
    })
    for p in problems[:100]:
        res.finding(**p)
    if problems:
        return res.failed(
            f"{len(problems)} per-object disagreement(s) between the native board "
            f"and the exported fabrication data")
    return res.passed(
        f"all {matched} vias match the export object by object: coordinate, drill, "
        f"annulus, clearance, contact, overlap, centre-inside, both limit "
        f"classifications and nearest-opening identity")


def _native_opening_polygon(ctx, label, side):
    def build():
        geometry, _rows = None, None
        from .g_geometry import _via_survey
        geometry, _rows = _via_survey(ctx)
        table = {}
        for entry in geometry.pads:
            if side in entry["mask"]:
                table.setdefault(side, {})[entry["label"]] = entry["mask"][side]
        return table
    table = ctx.cache(f"native_openings_{side}", build)
    return table.get(side, {}).get(label)


# ---------------------------------------------------------------------------
# per-layer copper parity against a fresh export
# ---------------------------------------------------------------------------

@gate("STACK.GERBER_PARITY",
      "Shipped copper layers match a fresh export of the same board",
      gate_class="release-artifact", requires=("artifacts.gerber_dir", "artifacts.gerber_export_flags"))
def stack_gerber_parity(ctx, res):
    shipped_dir = ctx.manifest.resolve(ctx.manifest.get("artifacts.gerber_dir"))
    if not os.path.isdir(shipped_dir):
        return res.errored("gerber directory not found: " + shipped_dir)
    flags = res.limit(ctx.manifest.constraint(
        "artifacts.gerber_export_flags", units="cli option",
        cid="artifacts.gerber_export_flags")).value
    tolerance = res.limit(ctx.manifest.geometry_profile()
                          .tolerance("layer_symmetric_difference_mm2"))

    fresh_dir = os.path.join(ctx.workdir, "fresh_gerbers")
    if os.path.isdir(fresh_dir):
        shutil.rmtree(fresh_dir)
    os.makedirs(fresh_dir)
    args = [ctx.kicad_cli, "pcb", "export", "gerbers", "--output", fresh_dir]
    # Exported from the private checked copy; authoritative design files stay
    # untouched while the generated data is compared with committed outputs.
    args += list(flags) + [ctx.check_path(ctx.manifest.get("sources.pcb"))]
    proc = ctx.run_tool(args)
    res.measurements["fresh_export_command"] = " ".join(args[1:])
    res.measurements["fresh_export_exit"] = proc.returncode
    if proc.returncode != 0:
        return res.errored("fresh Gerber export failed: " + proc.stderr.strip()[:300])

    _s_layers, _s_drills, shipped = _find_layers(shipped_dir, ctx.manifest)
    _f_layers, _f_drills, fresh = _find_layers(fresh_dir, ctx.manifest)

    def coppers(table):
        # Keyed on whatever identifies a copper layer for this board, which
        # is the one name both directories agree on: an inner layer ships
        # under one extension and comes out of KiCad under another, and they
        # are the same plot.
        return {key[1]: entries[0] for key, entries in table.items()
                if key[0] == "layer" and (key[1].endswith(".Cu")
                                          or key[1].startswith("copper,"))}

    shipped_cu, fresh_cu = coppers(shipped), coppers(fresh)
    res.measurements["shipped_copper_layers"] = sorted(shipped_cu)
    res.measurements["fresh_copper_layers"] = sorted(fresh_cu)

    problems = []
    for fn in sorted(set(shipped_cu) | set(fresh_cu)):
        if fn not in shipped_cu:
            problems.append({"layer": fn,
                             "issue": "layer present in a fresh export but not shipped"})
            continue
        if fn not in fresh_cu:
            problems.append({"layer": fn,
                             "issue": "layer shipped but not produced by the board"})
            continue
        s_name, s_file = shipped_cu[fn]
        f_name, f_file = fresh_cu[fn]
        s_union, f_union = s_file.union(), f_file.union()
        if s_union is None or f_union is None:
            problems.append({"layer": fn, "shipped": s_name,
                             "issue": "copper layer carries no geometry"})
            continue
        diff = s_union.symmetric_difference(f_union).area
        res.measurements.setdefault("per_layer", {})[fn] = {
            "shipped_file": s_name,
            "shipped_area_mm2": round(s_union.area, 4),
            "fresh_area_mm2": round(f_union.area, 4),
            "symmetric_difference_mm2": round(diff, 6),
        }
        if tolerance.violated_maximum(diff):
            problems.append({
                "layer": fn, "shipped": s_name,
                "issue": "shipped copper geometry differs from a fresh export",
                "symmetric_difference_mm2": round(diff, 6),
                "limit_mm2": tolerance.value,
                "shipped_area_mm2": round(s_union.area, 4),
                "fresh_area_mm2": round(f_union.area, 4)})

    for p in problems:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} copper layer(s) disagree with a fresh "
                          f"export of the same board")
    return res.passed(
        f"{len(shipped_cu)} shipped copper layers match a fresh export by "
        f"geometric symmetric difference (limit {tolerance.value} mm2)")
