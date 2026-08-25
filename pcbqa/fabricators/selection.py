"""Requirement-driven fabrication profile selection, feasibility first.

The question an automated designer asks is: of everything the approved
catalog says this fabricator builds, what is the least sophisticated process
that satisfies this board's requirements? The ordering principle is fixed:

    feasibility first - a profile that cannot make the board, or whose
        ability to make it is UNKNOWN in the approved catalog, is rejected,
        with the reason. Unknown is not supported; a requirement that cannot
        be checked is not met.
    then standardness - options the fabricator itself lists as its ordinary
        set or states as its default are preferred, on that evidence.
    then complexity - among feasible, standard options, the least: no
        impedance control unless required, the lightest copper that
        satisfies the requirement, nothing premium for its own sake.

"Cheap" never outranks correctness because price never enters the feasibility
stage at all - and this pass carries no price model. Standard/default here
means what the fabricator's own published pages label as such, plus two
generic preferences this module owns and says it owns: lighter copper over
heavier, and no impedance-grade process when none is required.

Every selection explains itself: why the chosen options, why the rejected
alternatives, and on which approved evidence. A selection from a stale or
missing catalog says so. Where several published stackups remain legitimate
candidates, the ambiguity is preserved and reported rather than resolved by
coin toss - unless the fabricator's own data names a default, in which case
that evidence decides and is cited.
"""

from __future__ import annotations

from . import model

#: Requirement keys this release understands. An unknown key refuses:
#: a misspelled requirement silently ignored is a requirement never checked.
REQUIREMENT_KEYS = (
    "copper_layers", "board_thickness_mm", "min_track_mm", "min_space_mm",
    "min_drill_mm", "min_via_diameter_mm", "outer_copper_oz",
    "inner_copper_oz", "impedance_control", "material",
)


class SelectionError(Exception):
    pass


def _require_number(requirements, key, required=False):
    value = requirements.get(key)
    if value is None:
        if required:
            raise SelectionError(
                "requirements state no {!r}, without which no profile can "
                "be judged".format(key))
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionError("{!r} is {!r}, not a number".format(key, value))
    return value


def select(catalog, requirements):
    """The least-complex feasible fabrication profile, or an honest refusal.

    Returns a dict:
      feasible        - whether any acceptable profile exists
      profile         - the chosen option set, each option with its reason
      stackup         - the uniquely identified published stackup id, or None
      stackup_candidates - every published stackup still in contention
      rejections      - requirement checks that failed, with reasons
      explanations    - why each choice fell out the way it did
    """
    unknown = sorted(set(requirements) - set(REQUIREMENT_KEYS))
    if unknown:
        raise SelectionError(
            "requirements carry key(s) {} this selector does not implement; "
            "an unrecognised requirement cannot be silently ignored".format(
                unknown))
    layers = requirements.get("copper_layers")
    if not isinstance(layers, int) or isinstance(layers, bool) or layers < 1:
        raise SelectionError(
            "requirements must state copper_layers as a positive integer")
    thickness = _require_number(requirements, "board_thickness_mm",
                                required=True)
    impedance = bool(requirements.get("impedance_control", False))

    capabilities = catalog.get("capabilities", {})
    rejections = []
    explanations = []

    def capability(identity, needed_for):
        record = capabilities.get(identity)
        if record is None:
            rejections.append({
                "requirement": needed_for,
                "issue": "capability {!r} is not in the approved catalog, so "
                         "whether the fabricator supports this cannot be "
                         "established; unknown is not supported".format(
                             identity)})
        return record

    # -- layer count -------------------------------------------------------
    layer_range = capability("layer_count_range", "copper_layers")
    if layer_range is not None:
        low = layer_range["value"]["min"]
        high = layer_range["value"]["max"]
        if not low <= layers <= high:
            rejections.append({
                "requirement": "copper_layers",
                "issue": "{} copper layers is outside the fabricator's "
                         "stated {}-{} range".format(layers, low, high)})
        else:
            explanations.append(
                "{} copper layers is within the stated {}-{} range "
                "[{}]".format(layers, low, high, layer_range["source"]))

    # -- board thickness ---------------------------------------------------
    thickness_options = capability("fr4_thickness_options_mm",
                                   "board_thickness_mm")
    material = requirements.get("material", "FR4")
    if material != "FR4":
        rejections.append({
            "requirement": "material",
            "issue": "the approved catalog carries thickness options for "
                     "FR-4 only; {!r} cannot be checked".format(material)})
    elif thickness_options is not None:
        if thickness not in thickness_options["value"]:
            rejections.append({
                "requirement": "board_thickness_mm",
                "issue": "{} mm is not among the fabricator's stated FR-4 "
                         "options {} ({})".format(
                             thickness, thickness_options["value"],
                             thickness_options.get("conditions"))})
        else:
            explanations.append(
                "{} mm is a stated standard FR-4 thickness "
                "[{}]".format(thickness, thickness_options["source"]))

    # -- trace / space -----------------------------------------------------
    outer_oz = _require_number(requirements, "outer_copper_oz")
    track = _require_number(requirements, "min_track_mm")
    space = _require_number(requirements, "min_space_mm")
    if track is not None or space is not None:
        trace = capability("min_trace_space_masked_1oz_mm",
                           "min_track_mm/min_space_mm")
        if trace is not None:
            if outer_oz is not None and outer_oz > 1.0:
                rejections.append({
                    "requirement": "min_track_mm/min_space_mm",
                    "issue": "the stated trace/space limit is conditioned on "
                             "1 oz outer copper ({}); the limit at {} oz is "
                             "not in the approved catalog and is not "
                             "guessed".format(trace.get("conditions"),
                                              outer_oz)})
            else:
                minimum_track = trace["value"]["track"]
                minimum_space = trace["value"]["space"]
                if track is not None and track < minimum_track:
                    rejections.append({
                        "requirement": "min_track_mm",
                        "issue": "the design needs {} mm tracks; the stated "
                                 "minimum is {} mm".format(track,
                                                           minimum_track)})
                if space is not None and space < minimum_space:
                    rejections.append({
                        "requirement": "min_space_mm",
                        "issue": "the design needs {} mm clearance; the "
                                 "stated minimum is {} mm".format(
                                     space, minimum_space)})
                if not rejections or all(
                        r["requirement"] not in ("min_track_mm",
                                                 "min_space_mm")
                        for r in rejections):
                    explanations.append(
                        "trace/space {}/{} mm clears the stated minimum "
                        "{}/{} mm [{}]".format(
                            track, space, minimum_track, minimum_space,
                            trace["source"]))

    # -- drill and via -----------------------------------------------------
    drill = _require_number(requirements, "min_drill_mm")
    if drill is not None and layers >= 2:
        drill_capability = capability("drill_diameter_multilayer_mm",
                                      "min_drill_mm")
        if drill_capability is not None:
            if drill < drill_capability["value"]["min"]:
                rejections.append({
                    "requirement": "min_drill_mm",
                    "issue": "the design drills {} mm; the stated minimum is "
                             "{} mm".format(drill,
                                            drill_capability["value"]["min"])})
    via = _require_number(requirements, "min_via_diameter_mm")
    if via is not None and layers >= 2:
        via_capability = capability("via_multilayer_mm",
                                    "min_via_diameter_mm")
        if via_capability is not None:
            if via < via_capability["value"]["diameter"]:
                rejections.append({
                    "requirement": "min_via_diameter_mm",
                    "issue": "the design uses {} mm vias; the stated minimum "
                             "diameter is {} mm".format(
                                 via, via_capability["value"]["diameter"])})

    # -- copper weights ----------------------------------------------------
    profile = {"copper_layers": layers, "board_thickness_mm": thickness,
               "impedance_control": impedance}
    section = "{}L".format(layers)
    outer_options = capabilities.get(section + " outer_copper_options") \
        or capabilities.get("outer_copper_multilayer_oz")
    inner_options = capabilities.get(section + " inner_copper_options") \
        or capabilities.get("inner_copper_oz")
    inner_default = capabilities.get("inner_copper_default_oz")

    profile["outer_copper_oz"] = _pick_weight(
        "outer", outer_oz, outer_options, None,
        rejections, explanations)
    profile["inner_copper_oz"] = _pick_weight(
        "inner", _require_number(requirements, "inner_copper_oz"),
        inner_options, inner_default, rejections, explanations)

    # -- stackup candidates ------------------------------------------------
    stackup_id, candidates = _stackups(catalog, layers, thickness, impedance,
                                       explanations)
    if impedance:
        explanations.append(
            "impedance control is required by the requirements, so the "
            "impedance-grade constructions are in scope")
    else:
        explanations.append(
            "no impedance requirement is stated, so no impedance-grade "
            "process is selected: the least sophisticated process that "
            "satisfies the requirements is preferred")

    feasible = not rejections
    return {
        "feasible": feasible,
        "fabricator": catalog.get("fabricator"),
        "profile": profile if feasible else None,
        "stackup": stackup_id if feasible else None,
        "stackup_candidates": candidates,
        "rejections": rejections,
        "explanations": explanations,
    }


def _pick_weight(which, required, options, stated_default, rejections,
                 explanations):
    """One copper weight: the requirement, checked; else the least."""
    if options is None:
        if required is not None:
            rejections.append({
                "requirement": which + "_copper_oz",
                "issue": "no {} copper options are in the approved catalog, "
                         "so {} oz cannot be verified as "
                         "offered".format(which, required)})
        return None
    offered = options["value"]
    if required is not None:
        if required not in offered:
            rejections.append({
                "requirement": which + "_copper_oz",
                "issue": "{} oz {} copper is not among the offered options "
                         "{}".format(required, which, offered)})
            return None
        explanations.append(
            "{} oz {} copper is an offered option [{}]".format(
                required, which, options["source"]))
        return required
    if stated_default is not None and stated_default["value"] in offered:
        explanations.append(
            "{} copper defaults to {} oz, the fabricator's stated default "
            "[{}]".format(which, stated_default["value"],
                          stated_default["source"]))
        return stated_default["value"]
    chosen = min(offered)
    explanations.append(
        "{} copper defaults to {} oz, the lightest offered option - a "
        "generic lowest-complexity preference of this selector, not a "
        "fabricator statement".format(which, chosen))
    return chosen


#: How closely a published construction's summed thickness must approach the
#: requested nominal board thickness before the construction is treated as
#: describing that build. The finished board adds solder mask over the copper
#: stack, so the sum is legitimately below nominal by a mask-scale amount.
STACKUP_NOMINAL_TOLERANCE_MM = 0.1


def _stackups(catalog, layers, thickness, impedance, explanations):
    candidates = []
    for identity in sorted(catalog.get("stackups", {})):
        stackup = catalog["stackups"][identity]
        if model.stackup_copper_count(stackup) != layers:
            continue
        total = model.stackup_total_mm(stackup)
        if total is None or abs(total - thickness) > \
                STACKUP_NOMINAL_TOLERANCE_MM:
            continue
        default = stackup.get("default_when_no_impedance_requirement", False)
        if impedance and default:
            continue
        if not impedance and not default:
            continue
        candidates.append(identity)
    if not candidates:
        explanations.append(
            "no published construction matches {} copper layers at {} mm "
            "nominal for this requirement profile; the fabricator's stackup "
            "tables describe specific builds and nothing is scaled or "
            "invented for others".format(layers, thickness))
        return None, []
    if len(candidates) == 1:
        identity = candidates[0]
        stackup = catalog["stackups"][identity]
        if stackup.get("default_when_no_impedance_requirement"):
            explanations.append(
                "stackup {} is the fabricator's own published default when "
                "no impedance requirement is stated [{}]".format(
                    identity, stackup["source"]))
        else:
            explanations.append(
                "stackup {} is the only published construction matching the "
                "requirements".format(identity))
        return identity, candidates
    explanations.append(
        "{} published constructions match ({}); nothing in the approved "
        "catalog ranks them, so the ambiguity is preserved for a deliberate "
        "choice rather than resolved arbitrarily".format(
            len(candidates), ", ".join(candidates)))
    return None, candidates


# ---------------------------------------------------------------------------
# feeding the timing/SI system
# ---------------------------------------------------------------------------

def export_physical_stackup(approved_snapshot, stackup_id,
                            copper_layer_names):
    """A board-supplement physical stackup from one approved construction.

    The output is the exact document `timing.physical_stackup.supplement`
    consumes, with the provenance the timing rules demand: every quantitative
    value traces to the approved snapshot's digest, source and retrieval
    date. The board that commits this file pins those numbers; validation
    then never needs the fabricator's website again, and a later catalog
    change shows up as a reviewable difference against a *file the board
    owns*, not as a silent shift under its results.

    `copper_layer_names` are the BOARD's copper layers, outermost first: the
    board is the authority on what its layers are called, and the count has
    to match the construction or the export refuses.
    """
    catalog = approved_snapshot["normalized"]
    stackup = catalog.get("stackups", {}).get(stackup_id)
    if stackup is None:
        raise SelectionError(
            "the approved catalog has no stackup {!r}".format(stackup_id))
    copper_names = list(copper_layer_names)
    if model.stackup_copper_count(stackup) != len(copper_names):
        raise SelectionError(
            "stackup {} has {} copper layers; the board names {}".format(
                stackup_id, model.stackup_copper_count(stackup),
                len(copper_names)))
    materials = catalog.get("materials", {})
    provenance = (
        "{fab} approved catalog {digest}, source {source}, retrieved {when}, "
        "stackup {stackup}".format(
            fab=catalog.get("fabricator"),
            digest=approved_snapshot["normalized_sha256"][:12],
            source=stackup["source"],
            when=approved_snapshot["retrieved_utc"][:10],
            stackup=stackup_id))

    layers = []
    copper_index = 0
    dielectric_index = 0
    for layer in stackup["layers"]:
        if layer["role"] == model.COPPER:
            layers.append({
                "name": copper_names[copper_index],
                "kind": "copper", "type": "copper",
                "thickness_mm": layer["thickness_mm"],
            })
            copper_index += 1
            continue
        dielectric_index += 1
        form = layer.get("form")
        if form == model.PREPREG:
            material_key = "prepreg {}".format(layer.get("material"))
        else:
            material_key = "core"
        material = materials.get(material_key)
        entry = {
            "name": "dielectric {}".format(dielectric_index),
            "kind": "dielectric", "type": form,
            "thickness_mm": layer["thickness_mm"],
            "material": (layer.get("material") or form),
            # Stated by the fabricator where stated; absent where not. A
            # dielectric constant is never borrowed from a similar material.
            "epsilon_r": material["dk"] if material else None,
            "loss_tangent": None,
        }
        if material is None:
            entry["epsilon_r_note"] = (
                "the approved catalog states no dielectric constant for "
                "{!r}".format(material_key))
        layers.append(entry)

    return {
        "schema_version": 1,
        "title": "Physical stackup exported from the approved {} "
                 "catalog".format(catalog.get("fabricator")),
        "provenance": provenance,
        "generated_from": {
            "fabricator": catalog.get("fabricator"),
            "stackup": stackup_id,
            "approved_normalized_sha256":
                approved_snapshot["normalized_sha256"],
            "retrieved_utc": approved_snapshot["retrieved_utc"],
            "sources": [{key: source.get(key) for key in
                         ("id", "url", "sha256_raw")}
                        for source in approved_snapshot["sources"]],
        },
        "notes": [
            "Every value here restates the approved fabricator catalog "
            "entry named in `provenance`; none is measured from a board and "
            "none is a general-knowledge default.",
            "Loss tangent is not published by these sources and is left "
            "null rather than assumed.",
        ],
        "layers": layers,
    }
