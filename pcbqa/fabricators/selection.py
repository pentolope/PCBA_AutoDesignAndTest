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

#: Manufacturing properties a real board can depend on that this selector
#: does NOT inspect. They are listed so no consumer can mistake "feasible
#: against the checked requirements" for "manufacturable in every respect":
#: a board whose manufacturability turns on one of these has NOT been
#: checked for it here, and an unrepresented requirement never silently
#: becomes "no requirement" - it is simply outside this vocabulary, and the
#: selection result says so.
OUT_OF_VOCABULARY = (
    "board dimensions and outline",
    "surface finish",
    "annular ring",
    "NPTH and slot geometry",
    "plated slots",
    "castellated edges",
    "edge-to-copper clearance",
    "solder-mask constraints",
    "via covering (tented / plugged / filled)",
    "via annular-ring and hole-to-diameter relationships",
    "blind and buried via technology",
    "gold fingers",
    "controlled-depth routing",
    "special materials beyond standard FR-4",
    "panelization",
    "assembly constraints",
)


class SelectionError(Exception):
    pass


def _require_number(requirements, key, required=False):
    """One numeric requirement, validated to death at the boundary.

    Every numeric requirement here is a physical dimension or a copper
    weight: a finite, strictly positive number. Everything else refuses -
    booleans (which Python would happily compare as 0 and 1), strings
    (even numeric-looking ones; the schema is JSON numbers), NaN (which
    silently fails BOTH sides of every comparison, so a NaN dimension
    would sail past pass and fail checks alike), infinities, zeros and
    negatives. A malformed requirement must never become a skipped one.
    """
    value = requirements.get(key)
    if value is None:
        if required:
            raise SelectionError(
                "requirements state no {!r}, without which no profile can "
                "be judged".format(key))
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionError("{!r} is {!r}, not a number".format(key, value))
    if value != value or value in (float("inf"), float("-inf")):
        raise SelectionError(
            "{!r} is {!r}, which is not a finite number; NaN and infinity "
            "compare as nothing and would skip every check".format(
                key, value))
    if value <= 0:
        raise SelectionError(
            "{!r} is {!r}; a physical requirement must be strictly "
            "positive".format(key, value))
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
    impedance = requirements.get("impedance_control", False)
    if impedance is None:
        impedance = False
    if not isinstance(impedance, bool):
        # bool("false") is True; 1 is not a decision. The requirement is a
        # JSON boolean or absent (absent means no impedance requirement),
        # and anything else is a malformed input, not a lenient no.
        raise SelectionError(
            "impedance_control is {!r}; it must be true, false, or absent "
            "(absent means no impedance requirement)".format(impedance))
    material = requirements.get("material", "FR4")
    if not isinstance(material, str) or not material:
        raise SelectionError(
            "material is {!r}, not a material name".format(material))

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

    # -- layer count: the discrete offered set, not a numeric range --------
    discrete = capabilities.get("fr4_copper_layer_options")
    if discrete is not None:
        if layers not in discrete["value"]:
            rejections.append({
                "requirement": "copper_layers",
                "issue": "{} copper layers is not among the fabricator's "
                         "discrete offered counts {}; a count inside the "
                         "stated 1-32 range that no statement supports is "
                         "not offered ({})".format(
                             layers, discrete["value"],
                             discrete.get("conditions"))})
        else:
            explanations.append(
                "{} copper layers is among the discrete offered counts "
                "[{}]".format(layers, discrete["source"]))
    else:
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
                    "[{}]; no discrete count list is in this catalog, so "
                    "the range is the only evidence".format(
                        layers, low, high, layer_range["source"]))

    # -- board thickness, coupled to the layer count and mode --------------
    if material != "FR4":
        rejections.append({
            "requirement": "material",
            "issue": "the approved catalog carries thickness options for "
                     "FR-4 only; {!r} cannot be checked".format(material)})
    elif impedance:
        # An impedance build is ordered against a layer-count section; its
        # thickness options are stated per section, and a thickness that
        # section does not state is not available for THIS combination,
        # however standard it is elsewhere in the catalog.
        section_options = capabilities.get(
            "{}L thickness_options".format(layers))
        if section_options is None:
            rejections.append({
                "requirement": "board_thickness_mm",
                "issue": "no thickness options are published for {}-layer "
                         "impedance-controlled builds; whether {} mm is "
                         "available for this combination cannot be "
                         "established, and unknown is not "
                         "supported".format(layers, thickness)})
        elif thickness not in section_options["value"]:
            rejections.append({
                "requirement": "board_thickness_mm",
                "issue": "{} mm is not among the stated thickness options "
                         "{} for {}-layer impedance-controlled builds; the "
                         "value exists elsewhere in the catalog but not "
                         "for this combination".format(
                             thickness, section_options["value"], layers)})
        else:
            explanations.append(
                "{} mm is a stated thickness option for {}-layer "
                "impedance-controlled builds [{}]".format(
                    thickness, layers, section_options["source"]))
    else:
        thickness_options = capability("fr4_thickness_options_mm",
                                       "board_thickness_mm")
        if thickness_options is not None:
            heavy = capabilities.get("fr4_thickness_2p5mm_plus")
            if thickness in thickness_options["value"]:
                problem = _thickness_restriction(capabilities, thickness,
                                                 layers)
                if problem is not None:
                    rejections.append({
                        "requirement": "board_thickness_mm",
                        "issue": problem})
                else:
                    explanations.append(
                        "{} mm is a stated standard FR-4 thickness with no "
                        "stated restriction for {} layers "
                        "[{}]".format(thickness, layers,
                                      thickness_options["source"]))
            elif heavy is not None and \
                    thickness >= heavy["value"]["min"]:
                minimum_layers = (heavy.get("applies") or {}).get(
                    "min_layers")
                rejections.append({
                    "requirement": "board_thickness_mm",
                    "issue": "{} mm falls in the range the fabricator "
                             "states exists only for {}+ layer boards, and "
                             "its discrete values are not published; a "
                             "thickness that cannot be confirmed for this "
                             "layer count is not assumed ({})".format(
                                 thickness, minimum_layers,
                                 heavy.get("conditions"))})
            else:
                rejections.append({
                    "requirement": "board_thickness_mm",
                    "issue": "{} mm is not among the fabricator's stated "
                             "FR-4 options {} ({})".format(
                                 thickness, thickness_options["value"],
                                 thickness_options.get("conditions"))})

    # -- copper weights, resolved first ------------------------------------
    # The trace/space limits and the stackup tables are both conditioned on
    # copper weight, so the profile's weights are fixed before anything that
    # depends on them is judged: one configuration, judged coherently.
    # Only records whose stated scope covers THIS board class are consulted
    # - a 2-layer board never borrows the multilayer option list and a
    # 1-layer board never borrows anyone's, because "an option exists for
    # some other class" proves nothing about this one.
    inner_default = capabilities.get("inner_copper_default_oz")

    outer_oz = _pick_weight(
        "outer", _require_number(requirements, "outer_copper_oz"),
        _copper_records(capabilities, "outer", layers), None,
        rejections, explanations)
    inner_oz = None
    if layers > 2:
        inner_oz = _pick_weight(
            "inner", _require_number(requirements, "inner_copper_oz"),
            _copper_records(capabilities, "inner", layers), inner_default,
            rejections, explanations)
    elif requirements.get("inner_copper_oz") is not None:
        rejections.append({
            "requirement": "inner_copper_oz",
            "issue": "a {}-layer board has no inner copper layers to "
                     "weigh".format(layers)})

    # -- trace / space, against the limits published for THESE weights -----
    track = _require_number(requirements, "min_track_mm")
    space = _require_number(requirements, "min_space_mm")
    if track is not None or space is not None:
        needed = [("outer", outer_oz)]
        if layers > 2:
            needed.append(("inner", inner_oz))
        for which, weight in needed:
            if weight is None:
                rejections.append({
                    "requirement": "min_track_mm/min_space_mm",
                    "issue": "no {} copper weight could be established, and "
                             "every published trace/space limit is "
                             "conditioned on one; an unconditioned limit "
                             "does not exist and is not invented".format(
                                 which)})
                continue
            applicable = _trace_limits(capabilities, weight, layers)
            if not applicable:
                rejections.append({
                    "requirement": "min_track_mm/min_space_mm",
                    "issue": "no published trace/space limit covers {} oz "
                             "{} copper on a {}-layer board; unknown is "
                             "not supported".format(which_weight(weight),
                                                    which, layers)})
                continue
            minimum_track = max(r["value"]["track"] for _i, r in applicable)
            minimum_space = max(r["value"]["space"] for _i, r in applicable)
            cited = ", ".join(identity for identity, _r in applicable)
            if track is not None and track < minimum_track:
                rejections.append({
                    "requirement": "min_track_mm",
                    "issue": "the design needs {} mm tracks; the strictest "
                             "published limit for {} oz {} copper at {} "
                             "layers is {} mm [{}]".format(
                                 track, which_weight(weight), which, layers,
                                 minimum_track, cited)})
            if space is not None and space < minimum_space:
                rejections.append({
                    "requirement": "min_space_mm",
                    "issue": "the design needs {} mm clearance; the "
                             "strictest published limit for {} oz {} "
                             "copper at {} layers is {} mm [{}]".format(
                                 space, which_weight(weight), which, layers,
                                 minimum_space, cited)})
            if (track is None or track >= minimum_track) and \
                    (space is None or space >= minimum_space):
                explanations.append(
                    "trace/space {}/{} mm clears the strictest published "
                    "{}/{} mm for {} oz {} copper at {} layers [{}]".format(
                        track, space, minimum_track, minimum_space,
                        which_weight(weight), which, layers, cited))

    # -- drill and via, from the board class's own published rules ---------
    # The page states these separately for 1-layer, 2-layer and multilayer
    # boards, and the selector consults only the record whose stated scope
    # covers this board - never a neighbouring class's rule. A record that
    # does not carry the expected published quantities refuses rather than
    # guesses which of its numbers means what.
    drill = _require_number(requirements, "min_drill_mm")
    if drill is not None:
        record, problem = _scoped_rule(capabilities, "drill", layers,
                                       {"min": "floor", "max": "ceiling"})
        if problem is not None:
            rejections.append({"requirement": "min_drill_mm",
                               "issue": problem})
        elif drill < record["value"]["min"]:
            rejections.append({
                "requirement": "min_drill_mm",
                "issue": "the design drills {} mm; the stated minimum for "
                         "this board class is {} mm [{}]".format(
                             drill, record["value"]["min"],
                             record["source"])})
        else:
            explanations.append(
                "the {} mm minimum drill clears the stated {}-{} mm "
                "drilled-hole range for this board class [{}]".format(
                    drill, record["value"]["min"], record["value"]["max"],
                    record["source"]))
    via = _require_number(requirements, "min_via_diameter_mm")
    if via is not None:
        record, problem = _scoped_rule(capabilities, "via", layers,
                                       {"hole": "floor",
                                        "diameter": "floor"})
        if problem is not None:
            rejections.append({"requirement": "min_via_diameter_mm",
                               "issue": problem})
        elif via < record["value"]["diameter"]:
            rejections.append({
                "requirement": "min_via_diameter_mm",
                "issue": "the design uses {} mm vias; the stated minimum "
                         "via diameter for this board class is {} mm "
                         "[{}]".format(via, record["value"]["diameter"],
                                       record["source"])})
        else:
            explanations.append(
                "the {} mm minimum via diameter clears the stated {} mm "
                "minimum for this board class ({}) [{}]".format(
                    via, record["value"]["diameter"],
                    record.get("conditions"), record["source"]))

    profile = {"copper_layers": layers, "board_thickness_mm": thickness,
               "impedance_control": impedance,
               "outer_copper_oz": outer_oz, "inner_copper_oz": inner_oz}

    # -- controlled impedance is itself a conditioned capability -----------
    if impedance:
        offered = capabilities.get("controlled_impedance_layer_counts")
        if offered is None:
            rejections.append({
                "requirement": "impedance_control",
                "issue": "the approved catalog does not state which layer "
                         "counts controlled impedance is offered for; "
                         "unknown is not supported"})
        elif layers not in offered["value"]:
            rejections.append({
                "requirement": "impedance_control",
                "issue": "controlled impedance is stated as offered for "
                         "layer counts {}; {} layers is not among "
                         "them".format(offered["value"], layers)})

    # -- stackup candidates: the same configuration, or none ---------------
    stackup_id, candidates = _stackups(catalog, layers, thickness, impedance,
                                       outer_oz, inner_oz, explanations)
    if impedance:
        if not candidates:
            # A controlled-impedance build IS its construction: impedance
            # is achieved by a specific published stackup, so a profile
            # with no compatible published construction has no established
            # controlled-impedance process - unlike ordinary fabrication,
            # where an unpublished construction is merely unpublished.
            rejections.append({
                "requirement": "impedance_control",
                "issue": "controlled impedance requires a published "
                         "construction compatible with this exact "
                         "configuration ({}L, {} mm, {} oz outer, {} oz "
                         "inner), and none is in the approved "
                         "catalog".format(
                             layers, thickness,
                             "?" if outer_oz is None
                             else which_weight(outer_oz),
                             "?" if inner_oz is None
                             else which_weight(inner_oz))})
        explanations.append(
            "impedance control is required by the requirements, so the "
            "impedance-grade constructions are in scope and one of them "
            "must describe this exact configuration")
    else:
        explanations.append(
            "no impedance requirement is stated, so no impedance-grade "
            "process is selected: the least sophisticated process that "
            "satisfies the requirements is preferred")

    feasible = not rejections
    checked = sorted(key for key in requirements
                     if requirements.get(key) is not None)
    return {
        "feasible": feasible,
        "fabricator": catalog.get("fabricator"),
        "profile": profile if feasible else None,
        "stackup": stackup_id if feasible else None,
        "stackup_candidates": candidates,
        "rejections": rejections,
        "explanations": explanations,
        "requirements_checked": checked,
        "not_in_vocabulary": list(OUT_OF_VOCABULARY),
        "vocabulary_note":
            "feasible means feasible against the checked requirements "
            "only; the properties under not_in_vocabulary were NOT "
            "inspected and must be judged separately",
    }


def which_weight(weight):
    """0.5 -> '0.5', 1.0 -> '1' - the way the sources themselves write it."""
    return "{:g}".format(weight)


def _trace_limits(capabilities, weight, layer_count):
    """Every published trace/space record applicable to a weight and layer
    count. Only records whose stated conditions cover BOTH are returned; a
    limit published for a neighbouring weight or layer class is not
    borrowed, however close."""
    applicable = []
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") != "trace":
            continue
        applies = record.get("applies") or {}
        weights = applies.get("copper_weights_oz") or []
        if weight not in weights:
            continue
        low = applies.get("min_layers") or 1
        high = applies.get("max_layers")
        if layer_count < low:
            continue
        if high is not None and layer_count > high:
            continue
        applicable.append((identity, record))
    return applicable


def _copper_records(capabilities, position, layers):
    """Every copper option record whose stated scope covers this board."""
    records = []
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") not in ("copper", "stackup-options"):
            continue
        applies = record.get("applies") or {}
        if applies.get("position") != position:
            continue
        low = applies.get("min_layers") or 1
        high = applies.get("max_layers")
        if layers < low or (high is not None and layers > high):
            continue
        records.append(record)
    return records


def _pick_weight(which, required, records, stated_default, rejections,
                 explanations):
    """One copper weight, from records scoped to this board class.

    A value counts as offered when a covering record with UNCONDITIONAL
    availability lists it. A record the fabricator itself qualifies with
    "availability depends on <unpublished factors>" poisons everything but
    the stated default: it announces that the bare list is not the whole
    truth, so a non-default weight backed only by such lists - or listed
    beside them - is unknown, and unknown is not supported.
    """
    if not records:
        if required is not None:
            rejections.append({
                "requirement": which + "_copper_oz",
                "issue": "no {} copper option record in the approved "
                         "catalog covers this board class, so {} oz cannot "
                         "be verified as offered; an option stated for "
                         "another class is not borrowed".format(
                             which, "{:g}".format(required))})
        return None
    unconditional = [r for r in records
                     if r.get("availability") != "conditional"]
    conditional = [r for r in records
                   if r.get("availability") == "conditional"]
    offered = sorted({v for r in unconditional
                      for v in (r["value"] if isinstance(r["value"], list)
                                else [r["value"]])})
    default_value = stated_default["value"] if stated_default else None
    if required is not None:
        if conditional and required != default_value:
            caveat = conditional[0]
            rejections.append({
                "requirement": which + "_copper_oz",
                "issue": "the fabricator states that {} copper "
                         "availability is conditional ({} [{}]); a "
                         "non-default weight of {} oz cannot be proven "
                         "from an option list the fabricator itself "
                         "qualifies, and unknown is not supported".format(
                             which, caveat.get("conditions"),
                             caveat["source"], "{:g}".format(required))})
            return None
        if required not in offered:
            rejections.append({
                "requirement": which + "_copper_oz",
                "issue": "{} oz {} copper is not among the options offered "
                         "for this board class ({})".format(
                             "{:g}".format(required), which, offered)})
            return None
        cited = ", ".join(sorted({r["source"] for r in unconditional
                                  if required in (
                                      r["value"]
                                      if isinstance(r["value"], list)
                                      else [r["value"]])}))
        explanations.append(
            "{} oz {} copper is an offered option for this board class "
            "[{}]".format("{:g}".format(required), which, cited))
        return required
    if stated_default is not None and default_value in {
            v for r in records for v in (
                r["value"] if isinstance(r["value"], list)
                else [r["value"]])}:
        explanations.append(
            "{} copper defaults to {} oz, the fabricator's stated default "
            "[{}]".format(which, default_value, stated_default["source"]))
        return default_value
    if not offered:
        rejections.append({
            "requirement": which + "_copper_oz",
            "issue": "every {} copper option record covering this board "
                     "class carries the fabricator's own availability "
                     "caveat and no stated default applies; nothing can "
                     "be chosen from a list the fabricator "
                     "qualifies".format(which)})
        return None
    chosen = min(offered)
    explanations.append(
        "{} copper defaults to {} oz, the lightest offered option - a "
        "generic lowest-complexity preference of this selector, not a "
        "fabricator statement".format(which, chosen))
    return chosen


def _scoped_rule(capabilities, category, layers, expected_keys):
    """The one record of `category` whose stated scope covers this board.

    `expected_keys` maps each published quantity to its direction:
    "floor" for a minimum the design must clear, "ceiling" for a maximum
    it must stay under. Returns (record, None) or (None, refusal text).
    No record covering the class means the capability is unknown for it;
    a covering record whose value does not carry the expected published
    quantities means the published terminology no longer maps to this
    requirement, and both refuse rather than borrow or guess. Multiple
    covering records intersect conservatively - floors take the highest
    stated floor, ceilings the lowest stated ceiling - and an empty
    intersection refuses outright: contradictory evidence is a fact to
    report, never a number to manufacture.
    """
    covering = []
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") != category:
            continue
        applies = record.get("applies") or {}
        low = applies.get("min_layers") or 1
        high = applies.get("max_layers")
        if layers < low or (high is not None and layers > high):
            continue
        covering.append(record)
    if not covering:
        return None, ("no published {} rule covers a {}-layer board; "
                      "unknown is not supported and another class's rule "
                      "is not borrowed".format(category, layers))
    for record in covering:
        value = record.get("value")
        if not isinstance(value, dict) or any(
                not isinstance(value.get(key), (int, float))
                for key in expected_keys):
            return None, (
                "the published {} rule covering this board class does not "
                "carry the quantities this requirement needs ({}); the "
                "page's terminology no longer maps safely and guessing "
                "which number means what is refused".format(
                    category, ", ".join(sorted(expected_keys))))
    if len(covering) == 1:
        merged = covering[0]
    else:
        merged = dict(covering[0])
        merged["value"] = {
            key: (max if direction == "floor" else min)(
                r["value"][key] for r in covering)
            for key, direction in expected_keys.items()}
        merged["source"] = ", ".join(sorted({r["source"]
                                             for r in covering}))
    floors = [merged["value"][k] for k, d in expected_keys.items()
              if d == "floor"]
    ceilings = [merged["value"][k] for k, d in expected_keys.items()
                if d == "ceiling"]
    if floors and ceilings and max(floors) > min(ceilings):
        return None, (
            "the published {} rules covering this board class contradict "
            "each other (a floor of {} against a ceiling of {}); "
            "contradictory evidence is refused, not averaged".format(
                category, max(floors), min(ceilings)))
    return merged, None


def _thickness_restriction(capabilities, thickness, layers):
    """The stated restriction forbidding this pair, if one exists.

    Returns the rejection text, or None when no stated restriction names
    this thickness and layer count. A restriction record that cannot be
    read as its published shape refuses the pair outright - a restriction
    half-understood must not be a restriction skipped.
    """
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") != "board-thickness-restriction":
            continue
        value = record.get("value")
        if not isinstance(value, dict) \
                or not isinstance(value.get("thickness_mm"), (int, float)) \
                or not isinstance(value.get("excluded_layer_counts"), list):
            return ("a thickness-restriction record ({}) cannot be read "
                    "as its published shape; a restriction that cannot be "
                    "applied refuses the pair rather than being "
                    "skipped".format(identity))
        if abs(value["thickness_mm"] - thickness) > 1e-9:
            continue
        if layers in value["excluded_layer_counts"]:
            return ("{} mm is stated as not available for {}-layer boards "
                    "({}) [{}]".format(thickness, layers,
                                       record.get("conditions"),
                                       record["source"]))
    return None


def _stackups(catalog, layers, thickness, impedance, outer_oz, inner_oz,
              explanations):
    """Published constructions describing exactly this profile, or none.

    The coupling is the invariant: a candidate must state (or carry as its
    reviewed applicability interpretation) the same nominal thickness, the
    same outer copper weight, and - when inner layers exist - the same
    inner copper weight the profile selected. A construction published for
    a different copper build does not describe this board, however similar
    its dielectrics, and returning it anyway would compose a process
    configuration the fabricator never published.
    """
    candidates = []
    copper_mismatched = 0
    for identity in sorted(catalog.get("stackups", {})):
        stackup = catalog["stackups"][identity]
        if model.stackup_copper_count(stackup) != layers:
            continue
        applicability = stackup.get("applicability") or {}
        nominal = applicability.get("nominal_thickness_mm")
        if nominal is None or abs(nominal - thickness) > 1e-9:
            continue
        default = stackup.get("default_when_no_impedance_requirement", False)
        if impedance and default:
            continue
        if not impedance and not default:
            continue
        stackup_outer = applicability.get("outer_copper_weight_oz")
        stackup_inner = applicability.get("inner_copper_weight_oz")
        if outer_oz is None or stackup_outer is None \
                or abs(stackup_outer - outer_oz) > 1e-9:
            copper_mismatched += 1
            continue
        if layers > 2 and (
                inner_oz is None or stackup_inner is None
                or abs(stackup_inner - inner_oz) > 1e-9):
            copper_mismatched += 1
            continue
        candidates.append(identity)
    if not candidates:
        if copper_mismatched:
            explanations.append(
                "{} published construction(s) match {} layers at {} mm but "
                "describe a different copper build than the selected {} oz "
                "outer / {} oz inner; a construction is only claimed for "
                "the exact configuration it is published for, and none is "
                "scaled or invented".format(
                    copper_mismatched, layers, thickness,
                    "?" if outer_oz is None else which_weight(outer_oz),
                    "?" if inner_oz is None else which_weight(inner_oz)))
        else:
            explanations.append(
                "no published construction matches {} copper layers at {} "
                "mm nominal for this requirement profile; the fabricator's "
                "stackup tables describe specific builds and nothing is "
                "scaled or invented for others".format(layers, thickness))
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

def export_physical_stackup(approved_snapshot, requirements,
                            copper_layer_names, stackup_id=None):
    """A board-supplement physical stackup from one approved construction.

    The output is the exact document `timing.physical_stackup.supplement`
    consumes, with the provenance the timing rules demand: every quantitative
    value traces to the approved snapshot's digest, source and retrieval
    date. The board that commits this file pins those numbers; validation
    then never needs the fabricator's website again, and a later catalog
    change shows up as a reviewable difference against a *file the board
    owns*, not as a silent shift under its results.

    The export runs the SAME selection the profile decision runs, from the
    board's `requirements`, and only a construction that selection names as
    a candidate for that profile can come out. There is deliberately no way
    to hand this function a bare stackup id and have it trusted: an export
    that bypassed profile compatibility would let a caller dress a board in
    a construction the fabricator never published for its configuration.
    `stackup_id` exists only to resolve a preserved ambiguity - it must be
    one of the selection's own candidates.

    `copper_layer_names` are the BOARD's copper layers, outermost first: the
    board is the authority on what its layers are called, and the count has
    to match the construction or the export refuses.
    """
    catalog = approved_snapshot["normalized"]
    result = select(catalog, requirements)
    if not result["feasible"]:
        raise SelectionError(
            "the requirements do not select a feasible fabrication profile, "
            "so no construction can be exported as describing them: {}".format(
                "; ".join(r["issue"] for r in result["rejections"][:3])))
    candidates = result["stackup_candidates"]
    if stackup_id is None:
        stackup_id = result["stackup"]
        if stackup_id is None:
            raise SelectionError(
                "no unique construction describes this profile ({}); "
                "name one of the selection's own candidates explicitly, or "
                "none exists to export".format(
                    ", ".join(candidates) if candidates
                    else "no candidates at all"))
    elif stackup_id not in candidates:
        raise SelectionError(
            "stackup {!r} is not among the constructions selection "
            "establishes for this profile ({}); exporting it would dress "
            "the board in a construction the fabricator did not publish "
            "for its configuration".format(
                stackup_id, ", ".join(candidates) if candidates
                else "none"))
    stackup = catalog["stackups"][stackup_id]
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
