"""The impedance-target solver: one construction, one model, one answer.

Given an approved fabrication profile, one of ITS published constructions,
a routing copper layer, the caller's reference-plane choice and a target
impedance, this module solves for the trace width that meets the target
under a closed-form analytic model - and refuses everything it cannot tie
to exact approved evidence.

The core invariant is containment: every number entering the calculation
has an explicit reason it applies to THIS construction. The dielectric
constants come from the impedance-calculator model records whose family
and layer-count scope cover the profile; the conductor thickness comes
from the finished-thickness record for this position and this profile's
copper weight; the geometry comes from the construction's own layer table.
Nothing is borrowed from a neighbouring family, thickness, or process
because it "exists in the catalog", and a fact that cannot be mapped
unambiguously refuses rather than approximating.

Supported topologies, deliberately few and stated exactly:

  * bare external microstrip - the signal on an outer copper layer, one
    declared adjacent reference plane, exactly one dielectric layer
    between, no soldermask. Zero-thickness Hammerstad effective
    permittivity with the Wheeler effective-width thickness correction
    (both reused from ``pcbqa.propagation``), and the classic Hammerstad
    two-branch Z0.
  * symmetric stripline - an internal signal with BOTH adjacent copper
    layers declared as references, one dielectric layer to each, equal in
    stated thickness and mapped to the SAME dielectric-constant record.
    Cohn's characterisation: an equivalent-round-conductor form for
    narrow strips and the fringing-capacitance form for wide ones.

Everything else refuses by name: coated (soldermask-covered) microstrip,
asymmetric stripline, mixed-dielectric stripline, embedded microstrip,
and every differential topology are recognized and reported as
unsupported by this pass, never silently approximated.

What a result does NOT claim: reference-plane continuity (the named
planes are taken as the caller's electrical intent; whether they are
whole under the route is the board-side reference-continuity machinery's
question), fabrication yield (the fabricator's stated impedance tolerance
is quoted verbatim when the catalog carries it, never computed into an
interval), and losses or frequency dependence of any kind.
"""

from __future__ import annotations

import math

from . import model as catalog_model
from . import selection
from .. import propagation

#: Bumped when the analytic model or its composition changes meaning.
MODEL_VERSION = "1"

FREE_SPACE_ETA_OHM = 376.730313668

MIL_TO_MM = 0.0254

SINGLE_ENDED = "single-ended"
DIFFERENTIAL = "differential"

MICROSTRIP = "external-microstrip-bare"
STRIPLINE = "symmetric-stripline"

#: Topologies this module recognizes but does not solve. Naming them is
#: the point: a refusal that says WHICH unsupported thing was asked for is
#: reviewable; a generic "cannot" is not.
UNSUPPORTED_TOPOLOGIES = (
    "coated-microstrip (soldermask present)",
    "asymmetric-stripline",
    "mixed-dielectric-stripline",
    "embedded-microstrip",
    "differential (all topologies)",
)


class ImpedanceError(Exception):
    """The solve cannot be performed as asked. Always blocks."""


# ---------------------------------------------------------------------------
# closed forms
# ---------------------------------------------------------------------------

def microstrip_z0(epsilon_r, width_mm, height_mm, conductor_mm):
    """Hammerstad Z0 for a bare microstrip of finite conductor thickness.

    The effective width and effective permittivity come from the same
    helpers the timing propagation model uses, so impedance and delay can
    never quietly disagree about the geometry. The two-branch Hammerstad
    Z0 is used with its usual validity window on u = w_eff/h; a geometry
    outside it refuses rather than extrapolating.
    """
    effective_width = propagation.thickness_corrected_width(
        width_mm, height_mm, conductor_mm)
    epsilon_effective = propagation.hammerstad_effective_permittivity(
        epsilon_r, effective_width, height_mm)
    u = effective_width / height_mm
    if not 0.1 <= u <= 20.0:
        raise propagation.Unsupported(
            "microstrip w_eff/h = {:.4f} lies outside the 0.1-20 validity "
            "window of the Hammerstad impedance form".format(u))
    if u <= 1.0:
        z_air = 60.0 * math.log(8.0 / u + u / 4.0)
        return z_air / math.sqrt(epsilon_effective), epsilon_effective
    z_air = (FREE_SPACE_ETA_OHM
             / (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z_air / math.sqrt(epsilon_effective), epsilon_effective


def stripline_z0(epsilon_r, width_mm, plate_gap_mm, conductor_mm):
    """Cohn's symmetric-stripline Z0 with finite conductor thickness.

    `plate_gap_mm` is b: the dielectric span between the two reference
    planes. Narrow strips (w/(b-t) < 0.35) use the equivalent-round-
    conductor form; wide strips use the fringing-capacitance form. Both
    are the standard published characterisations; t/b beyond 0.25 is
    outside their stated validity and refuses.
    """
    propagation._positive("relative permittivity", epsilon_r)
    propagation._positive("trace width", width_mm)
    propagation._positive("plate gap", plate_gap_mm)
    propagation._positive("conductor thickness", conductor_mm)
    b = plate_gap_mm
    t = conductor_mm
    w = width_mm
    if t / b > 0.25:
        raise propagation.Unsupported(
            "stripline t/b = {:.3f} exceeds the 0.25 validity limit of "
            "the implemented forms".format(t / b))
    if w >= b:
        # Far outside the narrow/wide characterisations' derivations.
        raise propagation.Unsupported(
            "stripline w/b = {:.3f} is outside the implemented validity "
            "window".format(w / b))
    if w / (b - t) < 0.35:
        diameter = (w / 2.0) * (
            1.0 + (t / (math.pi * w))
            * (1.0 + math.log(4.0 * math.pi * w / t))
            + 0.51 * math.pi * (t / w) ** 2)
        z0 = (60.0 / math.sqrt(epsilon_r)) * math.log(
            4.0 * b / (math.pi * diameter))
        return z0, epsilon_r
    ratio = 1.0 / (1.0 - t / b)
    fringing = (0.0885 * epsilon_r / math.pi) * (
        2.0 * ratio * math.log(ratio + 1.0)
        - (ratio - 1.0) * math.log(ratio * ratio - 1.0))
    z0 = (94.15 / math.sqrt(epsilon_r)) / (
        (w / b) * ratio + fringing / (0.0885 * epsilon_r))
    return z0, epsilon_r


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

def _finite_positive(label, value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImpedanceError("{} is {!r}, not a number".format(label, value))
    if value != value or value in (float("inf"), float("-inf")):
        raise ImpedanceError(
            "{} is {!r}, which is not finite; NaN and infinity compare as "
            "nothing and would corrupt the search".format(label, value))
    if value <= 0:
        raise ImpedanceError(
            "{} is {!r}; it must be strictly positive".format(label, value))
    return float(value)


def _target_in_published_range(capabilities, mode, target):
    identity = ("impedance_range_single_ended_ohm"
                if mode == SINGLE_ENDED
                else "impedance_range_differential_ohm")
    record = capabilities.get(identity)
    if record is None:
        raise ImpedanceError(
            "the approved catalog states no accepted {} impedance range; "
            "whether {} ohm is supported cannot be established, and "
            "unknown is not supported".format(mode, target))
    low, high = record["value"]["min"], record["value"]["max"]
    if not low <= target <= high:
        raise ImpedanceError(
            "{} ohm is outside the fabricator's stated {} range of "
            "{}-{} ohm [{}]".format(target, mode, low, high,
                                    record["source"]))
    return record


# ---------------------------------------------------------------------------
# model-context resolution
# ---------------------------------------------------------------------------

def _material_family(capabilities, layers):
    covering = []
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") != "impedance-materials":
            continue
        applies = record.get("applies") or {}
        low = applies.get("min_layers") or 1
        high = applies.get("max_layers")
        if layers < low or (high is not None and layers > high):
            continue
        covering.append(record)
    if len(covering) != 1:
        raise ImpedanceError(
            "{} impedance-model material famil{} cover a {}-layer board; "
            "exactly one must, or the Dk data has no unambiguous "
            "scope".format(len(covering),
                           "ies" if len(covering) != 1 else "y", layers))
    return covering[0]


def _dielectric_record(materials, family, layer, layers):
    """The calculator-model Dk record for one construction dielectric.

    A prepreg maps by its stated sheet name inside the family; a core
    maps by its stated thickness - exactly, or through the family's
    open-ended "thicker than" record when the construction's core exceeds
    every tabulated value. No nearest-value fallback exists on purpose.
    """
    if layer.get("form") == catalog_model.PREPREG:
        identity = "prepreg {} ({}, impedance-calculator)".format(
            layer.get("material"), family)
        record = materials.get(identity)
        if record is None:
            raise ImpedanceError(
                "the construction uses prepreg {!r} but the {} "
                "impedance-model table for {}-layer boards has no such "
                "sheet; a dielectric constant is never borrowed from a "
                "neighbouring family".format(layer.get("material"),
                                            family, layers))
        return identity, record
    thickness = layer.get("thickness_mm")
    exact, over = [], []
    for identity in sorted(materials):
        record = materials[identity]
        if record.get("kind") != catalog_model.CORE:
            continue
        properties = record.get("properties") or {}
        if properties.get("family") != family:
            continue
        stated = properties.get("core_thickness_mm")
        bound = properties.get("core_thickness_over_mm")
        if stated is not None and abs(stated - thickness) < 1e-9:
            exact.append((identity, record))
        elif bound is not None and thickness > bound:
            over.append((identity, record))
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise ImpedanceError(
            "the {} impedance-model table states {} entries for a "
            "{} mm core; contradictory evidence is refused, not "
            "chosen from".format(family, len(exact), thickness))
    if len(over) == 1:
        return over[0]
    raise ImpedanceError(
        "the {} impedance-model table has no entry for a {} mm core and "
        "no applicable open-ended record; the value is not interpolated "
        "or borrowed".format(family, thickness))


def _conductor_record(capabilities, position, weight_oz):
    matches = []
    for identity in sorted(capabilities):
        record = capabilities[identity]
        if record.get("category") != "copper-finished":
            continue
        applies = record.get("applies") or {}
        if applies.get("position") != position:
            continue
        weights = applies.get("copper_weights_oz") or []
        if weight_oz not in weights:
            continue
        if record.get("units") != "mil":
            continue
        matches.append((identity, record))
    if len(matches) != 1:
        raise ImpedanceError(
            "{} finished-conductor-thickness record(s) cover {} {:g} oz "
            "copper; exactly one must, so the calculation refuses rather "
            "than guessing the conductor it is about".format(
                len(matches), position, weight_oz))
    identity, record = matches[0]
    return identity, record, record["value"] * MIL_TO_MM


def resolve_context(approved_snapshot, requirements, stackup_id,
                    copper_layer, reference_copper_layers,
                    soldermask_present):
    """Every fact the solve needs, resolved and attributed - or a refusal.

    `copper_layer` and `reference_copper_layers` are 1-based indices into
    the construction's copper layers, outermost first: the solver works
    in construction space, and mapping board layer names onto it is the
    caller's declaration to make.
    """
    catalog = approved_snapshot["normalized"]
    result = selection.select(catalog, requirements)
    if not result["feasible"]:
        raise ImpedanceError(
            "the requirements do not select a feasible fabrication "
            "profile; an impedance geometry for an unfabricable profile "
            "would be a number about nothing: {}".format(
                "; ".join(r["issue"] for r in result["rejections"][:3])))
    if stackup_id not in result["stackup_candidates"]:
        raise ImpedanceError(
            "stackup {!r} is not among the constructions selection "
            "establishes for this profile ({}); an impedance answer must "
            "belong to the exact approved construction".format(
                stackup_id, ", ".join(result["stackup_candidates"])
                or "none"))
    stackup = catalog["stackups"][stackup_id]
    layers = stackup["layers"]
    copper_indices = [index for index, layer in enumerate(layers)
                      if layer["role"] == catalog_model.COPPER]
    total_copper = len(copper_indices)
    if not isinstance(copper_layer, int) or isinstance(copper_layer, bool) \
            or not 1 <= copper_layer <= total_copper:
        raise ImpedanceError(
            "copper_layer {!r} is not a copper index of this {}-layer "
            "construction".format(copper_layer, total_copper))
    references = list(reference_copper_layers or [])
    if not references:
        raise ImpedanceError(
            "no reference plane was declared. The stackup alone cannot "
            "establish which plane the signal is referenced to - that is "
            "an electrical-design intent - so the caller must name the "
            "reference copper layer(s), and the result remains "
            "conditional on those layers actually being planes")
    if len(set(references)) != len(references) or any(
            not isinstance(r, int) or isinstance(r, bool)
            or not 1 <= r <= total_copper or r == copper_layer
            for r in references):
        raise ImpedanceError(
            "reference_copper_layers {!r} must be distinct copper indices "
            "of the construction, different from the signal "
            "layer".format(references))

    def dielectrics_between(one, other):
        low = copper_indices[min(one, other) - 1]
        high = copper_indices[max(one, other) - 1]
        return [layer for layer in layers[low + 1:high]
                if layer["role"] == catalog_model.DIELECTRIC]

    profile = result["profile"]
    family_record = _material_family(catalog["capabilities"],
                                     profile["copper_layers"])
    family = family_record["value"]
    materials = catalog["materials"]
    capabilities = catalog["capabilities"]
    external = copper_layer in (1, total_copper)

    context = {
        "stackup": stackup_id,
        "copper_layer": copper_layer,
        "reference_copper_layers": sorted(references),
        "material_family": family,
        "material_family_record": family_record["name"],
        "profile": profile,
        "notes": [
            "reference-plane continuity is not established here: the "
            "named layers are taken as the caller's electrical intent, "
            "and whether they are whole under the actual route is the "
            "board-side reference-continuity machinery's question",
        ],
    }

    if external:
        if soldermask_present:
            raise ImpedanceError(
                "coated-microstrip (soldermask present) is not a "
                "supported topology in this pass; the analytic model has "
                "no defensible representation of the mask, and a bare "
                "calculation silently presented as a coated one would "
                "overstate the impedance. Solve with "
                "soldermask_present=False for the bare nominal, or wait "
                "for the coated model")
        if len(references) != 1:
            raise ImpedanceError(
                "an external microstrip has one reference plane; {} were "
                "declared".format(len(references)))
        expected = 2 if copper_layer == 1 else total_copper - 1
        if references[0] != expected:
            raise ImpedanceError(
                "the declared reference (copper {}) is not the adjacent "
                "copper layer ({}); a farther plane would make this an "
                "embedded/multi-dielectric geometry this pass does not "
                "model".format(references[0], expected))
        between = dielectrics_between(copper_layer, references[0])
        if len(between) != 1:
            raise ImpedanceError(
                "{} dielectric layers lie between the signal and its "
                "reference; the bare-microstrip form models exactly "
                "one".format(len(between)))
        identity, record = _dielectric_record(materials, family,
                                              between[0],
                                              profile["copper_layers"])
        conductor_identity, conductor_record, conductor_mm = \
            _conductor_record(capabilities, "external",
                              profile["outer_copper_oz"])
        context.update({
            "topology": MICROSTRIP,
            "height_mm": between[0]["thickness_mm"],
            "epsilon_r": record["dk"],
            "dielectric_record": identity,
            "conductor_record": conductor_identity,
            "conductor_thickness_mm": round(conductor_mm, 6),
            "conductor_weight_oz": profile["outer_copper_oz"],
        })
    else:
        adjacent = sorted((copper_layer - 1, copper_layer + 1))
        if sorted(references) != adjacent:
            raise ImpedanceError(
                "an internal stripline is referenced to BOTH adjacent "
                "copper layers {}; {} were declared. A single-sided or "
                "skipped-plane internal geometry is not a topology this "
                "pass models".format(adjacent, sorted(references)))
        above = dielectrics_between(copper_layer - 1, copper_layer)
        below = dielectrics_between(copper_layer, copper_layer + 1)
        if len(above) != 1 or len(below) != 1:
            raise ImpedanceError(
                "the stripline form models exactly one dielectric layer "
                "to each reference; found {} above and {} below".format(
                    len(above), len(below)))
        identity_a, record_a = _dielectric_record(
            materials, family, above[0], profile["copper_layers"])
        identity_b, record_b = _dielectric_record(
            materials, family, below[0], profile["copper_layers"])
        thickness_a = above[0]["thickness_mm"]
        thickness_b = below[0]["thickness_mm"]
        if abs(thickness_a - thickness_b) > 1e-9:
            raise ImpedanceError(
                "asymmetric-stripline: the dielectric spans differ "
                "({} mm above, {} mm below); only the symmetric form is "
                "implemented in this pass and asymmetry is not "
                "averaged away".format(thickness_a, thickness_b))
        if identity_a != identity_b or record_a["dk"] != record_b["dk"]:
            raise ImpedanceError(
                "mixed-dielectric-stripline: the two spans map to "
                "different dielectric records ({} vs {}); a single "
                "permittivity is not invented for them".format(
                    identity_a, identity_b))
        conductor_identity, conductor_record, conductor_mm = \
            _conductor_record(capabilities, "internal",
                              profile["inner_copper_oz"])
        context.update({
            "topology": STRIPLINE,
            "span_mm": round(thickness_a + thickness_b
                             + conductor_mm, 6),
            "half_span_dielectric_mm": thickness_a,
            "epsilon_r": record_a["dk"],
            "dielectric_record": identity_a,
            "conductor_record": conductor_identity,
            "conductor_thickness_mm": round(conductor_mm, 6),
            "conductor_weight_oz": profile["inner_copper_oz"],
        })

    trapezoid = capabilities.get("trace_top_vs_base_width_mil")
    if trapezoid is not None:
        context["trapezoid_record"] = trapezoid["name"]
        context["trapezoid_delta_mm"] = round(
            trapezoid["value"] * MIL_TO_MM, 6)
        context["notes"].append(
            "the fabricator states the finished trace top is {} mil "
            "narrower than its base; the model evaluates the trapezoid "
            "at its mean width (base minus half the delta)".format(
                trapezoid["value"]))
    else:
        context["trapezoid_delta_mm"] = 0.0
        context["notes"].append(
            "no trace-trapezoid statement is in the approved catalog; "
            "the drawn width is used as the conductor width")
    return context


# ---------------------------------------------------------------------------
# the solve
# ---------------------------------------------------------------------------

def _impedance_at(context, width_mm):
    mean_width = width_mm - context["trapezoid_delta_mm"] / 2.0
    if mean_width <= 0:
        raise propagation.Unsupported(
            "width {} mm is not wider than the stated trapezoid "
            "narrowing".format(width_mm))
    if context["topology"] == MICROSTRIP:
        return microstrip_z0(context["epsilon_r"], mean_width,
                             context["height_mm"],
                             context["conductor_thickness_mm"])
    return stripline_z0(context["epsilon_r"], mean_width,
                        context["span_mm"],
                        context["conductor_thickness_mm"])


def solve(approved_snapshot, request):
    """Solve one impedance target against one approved construction.

    `request` keys: requirements (the fabrication requirements dict),
    stackup, copper_layer, reference_copper_layers, mode, target_ohm,
    width_search_mm ({"min":..,"max":..}), soldermask_present (bool).
    Unknown keys refuse - a misspelled input silently ignored is an
    input never applied.
    """
    known = {"requirements", "stackup", "copper_layer",
             "reference_copper_layers", "mode", "target_ohm",
             "width_search_mm", "soldermask_present"}
    unknown = sorted(set(request) - known)
    if unknown:
        raise ImpedanceError(
            "request carries key(s) {} this solver does not "
            "implement".format(unknown))
    mode = request.get("mode")
    if mode not in (SINGLE_ENDED, DIFFERENTIAL):
        raise ImpedanceError(
            "mode {!r} is not one of {!r}, {!r}".format(
                mode, SINGLE_ENDED, DIFFERENTIAL))
    target = _finite_positive("target_ohm", request.get("target_ohm"))
    catalog = approved_snapshot["normalized"]
    range_record = _target_in_published_range(
        catalog["capabilities"], mode, target)
    if mode == DIFFERENTIAL:
        raise ImpedanceError(
            "differential solving is not implemented in this pass: the "
            "analytic infrastructure carries no coupled-line model, and "
            "an uncoupled pair presented as a differential answer would "
            "be wrong by construction")
    soldermask = request.get("soldermask_present")
    if not isinstance(soldermask, bool):
        raise ImpedanceError(
            "soldermask_present must be explicitly true or false; the "
            "topology depends on it and it is not guessed")
    bounds = request.get("width_search_mm")
    if not isinstance(bounds, dict) or set(bounds) != {"min", "max"}:
        raise ImpedanceError(
            "width_search_mm must be an object with exactly min and max; "
            "the search domain is the caller's statement, not a solver "
            "invention")
    low = _finite_positive("width_search_mm.min", bounds["min"])
    high = _finite_positive("width_search_mm.max", bounds["max"])
    if low >= high:
        raise ImpedanceError(
            "width search bounds are inverted ({} >= {})".format(low, high))

    context = resolve_context(
        approved_snapshot, request.get("requirements") or {},
        request.get("stackup"), request.get("copper_layer"),
        request.get("reference_copper_layers"), soldermask)

    z_low, _eps = _impedance_at(context, low)
    z_high, _eps = _impedance_at(context, high)
    # Monotonicity is checked, not presumed: impedance must fall as width
    # grows across the whole domain, sampled densely enough to catch a
    # misbehaving composition of corrections.
    samples = 17
    previous = None
    for step in range(samples + 1):
        width = low + (high - low) * step / samples
        z, _e = _impedance_at(context, width)
        if previous is not None and z >= previous:
            raise ImpedanceError(
                "impedance is not strictly decreasing in width over the "
                "search domain (Z({:.4f}) = {:.3f} >= previous {:.3f}); "
                "the model's assumptions do not hold here and a bisection "
                "result would be meaningless".format(width, z, previous))
        previous = z
    if not z_high <= target <= z_low:
        return _result(context, request, range_record, solved=None,
                       failure="no width in [{} , {}] mm reaches {} ohm; "
                               "the domain spans {:.2f} down to {:.2f} "
                               "ohm. The nearest bound is NOT returned: "
                               "a target outside the domain has no "
                               "solution in it".format(
                                   low, high, target, z_low, z_high))

    a, b = low, high
    for _iteration in range(200):
        middle = (a + b) / 2.0
        z_middle, eps_middle = _impedance_at(context, middle)
        if abs(z_middle - target) < 1e-6 or (b - a) < 1e-9:
            break
        if z_middle > target:
            a = middle
        else:
            b = middle
    width = round(middle, 6)
    z_final, eps_final = _impedance_at(context, width)

    checks = _manufacturing(catalog["capabilities"], context, width)
    return _result(context, request, range_record, solved={
        "width_mm": width,
        "impedance_ohm": round(z_final, 3),
        "epsilon_effective": round(eps_final, 4),
        "manufacturing": checks,
    }, failure=None)


def _manufacturing(capabilities, context, width):
    """The solved geometry against the profile's own routing limits."""
    weight = context["conductor_weight_oz"]
    layers = context["profile"]["copper_layers"]
    applicable = selection._trace_limits(capabilities, weight, layers)
    if not applicable:
        return {"established": False,
                "issue": "no published trace/space limit covers {:g} oz "
                         "copper at {} layers; manufacturability of the "
                         "solved width is unknown, and unknown is not "
                         "supported".format(weight, layers)}
    minimum = max(record["value"]["track"] for _i, record in applicable)
    cited = ", ".join(identity for identity, _r in applicable)
    if width < minimum:
        return {"established": False,
                "minimum_track_mm": minimum,
                "issue": "the solved width {} mm is below the strictest "
                         "published minimum {} mm [{}]; a mathematically "
                         "valid geometry the fabricator does not route is "
                         "not a feasible result".format(width, minimum,
                                                        cited)}
    return {"established": True, "minimum_track_mm": minimum,
            "evidence": cited}


def _result(context, request, range_record, solved, failure):
    document = {
        "model": {
            "identity": context["topology"],
            "version": MODEL_VERSION,
            "notes": context["notes"],
        },
        "request": {
            "mode": request.get("mode"),
            "target_ohm": request.get("target_ohm"),
            "width_search_mm": request.get("width_search_mm"),
            "soldermask_present": request.get("soldermask_present"),
        },
        "context": {key: value for key, value in context.items()
                    if key != "notes"},
        "target_range": {"value": range_record["value"],
                         "source": range_record["source"]},
        "solved": solved,
        "failure": failure,
    }
    return document


def solve_with_provenance(approved_snapshot, request):
    """`solve`, wrapped with the evidence chain a reviewer reconstructs."""
    document = solve(approved_snapshot, request)
    tolerance = approved_snapshot["normalized"]["capabilities"].get(
        "impedance_tolerance_standard_percent")
    document["fabrication_tolerance"] = (
        {"stated_percent": tolerance["value"],
         "source": tolerance["source"],
         "note": "the fabricator's stated impedance tolerance, quoted "
                 "verbatim; it is NOT computed into an interval here, "
                 "and the solved value above is nominal only"}
        if tolerance is not None else
        {"note": "no impedance tolerance is normalized from the approved "
                 "sources; the solved value is nominal only"})
    document["provenance"] = {
        "approved_normalized_sha256": approved_snapshot["normalized_sha256"],
        "parser": approved_snapshot.get("parser"),
        "retrieved_utc": approved_snapshot.get("retrieved_utc"),
        "sources": [{key: source.get(key) for key in
                     ("id", "url", "sha256_raw")}
                    for source in approved_snapshot.get("sources", [])],
        "model_version": MODEL_VERSION,
    }
    return document
