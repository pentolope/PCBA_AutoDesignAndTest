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
    Narrow strips use the IPC-2141 closed form, wide strips Cohn's
    fringing-capacitance form; the exact equations, sources and validity
    limits are stated at ``stripline_z0``.

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
#: Version 2 replaced the narrow-stripline branch: version 1 carried an
#: equivalent-diameter expansion whose 0.51*pi*(t/w)^2 term could not be
#: pinned to any printed source, so it was replaced outright by the
#: IPC-2141 / Wadell closed form, which can. Version 3 made the inverse
#: problem branch-aware: the piecewise formula seams are located
#: analytically, each branch interval is solved separately under its own
#: (provable) monotonicity, and a target that a seam makes ambiguous or
#: unreachable is reported as exactly that instead of being bisected
#: across an unproven interval. Version 4 made seam ownership exact
#: (each seam point belongs to precisely the branch the production
#: inequality gives it, and each interval is solved under branch-forced
#: evaluation with half-open acceptance on the non-owner side), removed
#: the heuristic width-distance root merging that could have collapsed
#: two genuinely distinct roots, and stopped the result claiming a
#: requested target is a fabrication requirement when nothing binds it
#: to any board- or order-side specification. Version 5 extended exact
#: seam ownership to the closed search domain: a seam equal to either
#: domain endpoint stays in the partition, its owner reduced to the one
#: point it still holds there, so an endpoint seam can no longer lose
#: its owned root, fabricate a root from the neighbouring branch's
#: values, or collapse a two-root answer to one; and the published
#: process tolerance was separated from any claim of applying to this
#: still-unbound target. Version 6 made microstrip seam discovery a
#: fact of the closed domain itself: branch membership is decided at
#: the domain endpoints, an exact endpoint tie is owned per the
#: production inequality, and the bisected interior crossing is clamped
#: into the closed domain, so no endpoint seam depends on the luck of a
#: bracket evaluated elsewhere; and the tolerance prose was reworded to
#: stay true when no width is solved at all.
MODEL_VERSION = "6"

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

def microstrip_z0(epsilon_r, width_mm, height_mm, conductor_mm,
                  _force_branch=None):
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
    narrow = u <= 1.0 if _force_branch is None \
        else _force_branch == "narrow"
    if narrow:
        z_air = 60.0 * math.log(8.0 / u + u / 4.0)
        return z_air / math.sqrt(epsilon_effective), epsilon_effective
    z_air = (FREE_SPACE_ETA_OHM
             / (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z_air / math.sqrt(epsilon_effective), epsilon_effective


def stripline_z0(epsilon_r, width_mm, plate_gap_mm, conductor_mm,
                 _force_branch=None):
    """Symmetric-stripline Z0 with finite conductor thickness.

    `plate_gap_mm` is b: the span between the two reference planes.

    Narrow strips - w/(b-t) < 0.35 - implement, exactly:

        Z0 = 60/sqrt(er) * ln( 4*b / (0.67*pi*(0.8*w + t)) )

    which is the IPC-2141 / Wadell closed form. Analog Devices tutorial
    MT-094 prints the algebraically near-equivalent Z0 = 60/sqrt(er) *
    ln(1.9*b/(0.8*w + t)) - "near" because 4/(0.67*pi) = 1.9004, so the
    two prints differ by 0.02% in the constant; what runs here is the
    0.67*pi form, with no other transformation.

    Wide strips implement Cohn's fringing-capacitance characterisation
    (Cohn, "Characteristic Impedance of the Shielded-Strip Transmission
    Line", IRE MTT 1954, as reproduced by Wadell), untransformed.

    Stated validity, enforced here: w/(b-t) < 0.35 selects the narrow
    branch, t/b <= 0.25 throughout, w < b for the wide branch's
    derivation. Accuracy, measured not assumed: at t -> 0 against Cohn's
    exact elliptic solution the narrow form reads 2-4.5% low (worst near
    the branch seam) and the wide form tracks within 0.6%. At finite
    conductor thickness NO independent exact reference is available
    here, so the finite-thickness model error is NOT quantitatively
    bounded; the zero-thickness characterisation is the only measured
    anchor, and nothing here converts it into a finite-thickness claim.

    The two branches meet at w = 0.35*(b-t) with a thickness-dependent
    step: upward in width at small t/b, closing toward zero near t/b ~
    0.03. The inverse solver treats that seam as a first-class model
    boundary - see ``_solve_width``.

    Version note: the model-1 narrow branch used an equivalent-diameter
    expansion whose transcription could not be pinned to a printed
    source; it was replaced, not repaired.
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
    narrow = (w / (b - t) < 0.35) if _force_branch is None \
        else _force_branch == "narrow"
    if narrow:
        z0 = (60.0 / math.sqrt(epsilon_r)) * math.log(
            4.0 * b / (0.67 * math.pi * (0.8 * w + t)))
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


def _target_range(capabilities, mode, target, enforced):
    """The fabricator's controlled-process target range, in its scope.

    For a profile that SELECTS impedance control, the published range is
    a fabrication constraint and a target outside it refuses. For an
    uncontrolled nominal analysis the range describes a process nobody
    ordered: it is attached to the result as information, and the
    analytic model's own validity governs instead.
    """
    identity = ("impedance_range_single_ended_ohm"
                if mode == SINGLE_ENDED
                else "impedance_range_differential_ohm")
    record = capabilities.get(identity)
    if enforced:
        if record is None:
            raise ImpedanceError(
                "the profile selects controlled impedance but the "
                "approved catalog states no accepted {} range; whether "
                "{} ohm is supported cannot be established, and unknown "
                "is not supported".format(mode, target))
        low, high = record["value"]["min"], record["value"]["max"]
        if not low <= target <= high:
            raise ImpedanceError(
                "{} ohm is outside the fabricator's stated {} "
                "controlled-impedance range of {}-{} ohm [{}]".format(
                    target, mode, low, high, record["source"]))
        return {"value": record["value"], "source": record["source"],
                "enforced": True,
                "note": "the profile selects controlled impedance, so "
                        "the fabricator's stated target range is a "
                        "fabrication constraint"}
    if record is None:
        return {"enforced": False,
                "note": "no published range in the approved catalog; "
                        "uncontrolled nominal analysis is governed by "
                        "the analytic model's validity only"}
    return {"value": record["value"], "source": record["source"],
            "enforced": False,
            "note": "the stated range describes the fabricator's "
                    "controlled-impedance process, which this profile "
                    "does not select; it does not constrain an "
                    "uncontrolled nominal analysis"}


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
        if soldermask_present:
            raise ImpedanceError(
                "soldermask_present=true contradicts an internal routing "
                "layer: soldermask coats the board's outer surfaces and "
                "cannot touch a stripline. Declare it false for internal "
                "layers - the input contract is strict everywhere, and a "
                "contradiction is not quietly ignored")
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
        context["notes"].append(
            "model characterisation, measured against Cohn's exact "
            "zero-thickness elliptic solution: the IPC-2141 narrow closed "
            "form reads 2-4.5% low across its window (worst near the "
            "w/(b-t)=0.35 branch edge); the wide fringing form tracks "
            "within 0.6%. The nominal below carries that model-class "
            "bias, separate from any fabrication tolerance")
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

def _impedance_at(context, width_mm, _force_branch=None):
    """Z at one base width. `_force_branch` is solver-internal.

    When set ("narrow"/"wide") the piecewise top-level form is evaluated
    under that branch regardless of which side of the seam the width
    falls on - this is how the inverse works each branch over the
    CLOSURE of its own domain, so the open side of a seam is priced as
    the branch's continuous limit instead of leaking into the
    neighbouring branch. Public callers never pass it: the unforced
    evaluation is exactly the production piecewise formula.
    """
    mean_width = width_mm - context["trapezoid_delta_mm"] / 2.0
    if mean_width <= 0:
        raise propagation.Unsupported(
            "width {} mm is not wider than the stated trapezoid "
            "narrowing".format(width_mm))
    if context["topology"] == MICROSTRIP:
        return microstrip_z0(context["epsilon_r"], mean_width,
                             context["height_mm"],
                             context["conductor_thickness_mm"],
                             _force_branch=_force_branch)
    return stripline_z0(context["epsilon_r"], mean_width,
                        context["span_mm"],
                        context["conductor_thickness_mm"],
                        _force_branch=_force_branch)


def _solve(approved_snapshot, request):
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

    # The fabricator's published target range describes its CONTROLLED
    # process. A profile that selects impedance control is held to it; an
    # uncontrolled nominal analysis is governed by the analytic model's
    # own validity and the caller's search domain, and the range is
    # attached as information, never as a constraint on arithmetic.
    controlled = bool(context["profile"].get("impedance_control"))
    range_record = _target_range(catalog["capabilities"], mode, target,
                                 enforced=controlled)

    try:
        roots, diagnostics = _solve_width(context, low, high, target)
    except propagation.Unsupported as exc:
        raise ImpedanceError(
            "the width search domain reaches geometry outside the "
            "model's stated validity ({}); narrow the domain to where "
            "the model is defined rather than have it extrapolate".format(
                exc))

    if len(roots) > 1:
        return _result(context, request, range_record, numeric=None,
                       manufacturing=None,
                       ambiguous=[{"width_mm": round(w, 6),
                                   "impedance_ohm": round(z, 3)}
                                  for w, z, _e in roots],
                       failure="the target is reached by {} distinct "
                               "widths because a model branch seam makes "
                               "impedance non-monotonic in this domain "
                               "({}); no root is silently chosen - "
                               "narrow the search domain to the intended "
                               "side of the seam".format(
                                   len(roots), diagnostics))
    if not roots:
        return _result(context, request, range_record, numeric=None,
                       manufacturing=None, ambiguous=None,
                       failure="no width in [{} , {}] mm reaches {} ohm "
                               "({}). The nearest value is NOT returned: "
                               "a target no interval brackets has no "
                               "solution in this domain".format(
                                   low, high, target, diagnostics))

    width, z_final, eps_final = roots[0]
    width = round(width, 6)
    checks = _manufacturing(catalog["capabilities"], context, width)
    return _result(context, request, range_record, numeric={
        "width_mm": width,
        "impedance_ohm": round(z_final, 3),
        "epsilon_effective": round(eps_final, 4),
    }, manufacturing=checks, ambiguous=None, failure=None)


def _seam_positions(context, low, high):
    """Top-level branch boundaries in the closed domain, with owners.

    Stated precisely: every impedance-model branch boundary CAPABLE OF
    CHANGING THE INVERSE is represented here - the stripline narrow/wide
    split at w_mean = 0.35*(b - t) and the microstrip Hammerstad split
    at w_eff = h, mapped to base-width space through the trapezoid
    delta. Lower-level helpers are also piecewise (the Wheeler
    thickness-correction switch, the effective-permittivity sub-terms),
    but they are continuous and the composition stays strictly monotone
    across them, so they create no inverse branches and are deliberately
    not partitioned.

    Each seam carries the owner the production inequality gives it: the
    stripline formula reads `< 0.35 -> narrow, else wide`, so the exact
    seam point belongs to the WIDE branch; the microstrip formula reads
    `u <= 1 -> narrow`, so its seam point belongs to the NARROW branch.
    Sampling is not used: a seam is a fact of the formula.

    Returns [(base_width, owner)] with owner "left" (the lower-width
    branch owns the point) or "right". A seam equal to `low` or `high`
    is returned too: the caller's domain is CLOSED, so its endpoints
    are points like any other and each belongs to exactly one branch.
    Microstrip discovery is therefore decided AT the domain endpoints:
    branch membership is evaluated at `low` and `high` themselves, an
    exact tie there (thickness-corrected width equal to the height to
    the last bit) IS the seam at that endpoint, and an interior
    crossing is bisected from a fixed anchor and clamped into the
    closed domain. At float precision the seam is normally a crossing
    between two adjacent representable widths, so the reported position
    is production-consistent to within the bisection tolerance rather
    than a representable point of exact branch change.
    """
    delta = context["trapezoid_delta_mm"]
    seams = []
    if context["topology"] == STRIPLINE:
        b = context["span_mm"]
        t = context["conductor_thickness_mm"]
        seams.append((0.35 * (b - t) + delta / 2.0, "right"))
    else:
        h = context["height_mm"]
        t = context["conductor_thickness_mm"]
        if low - delta / 2.0 <= 0:
            raise propagation.Unsupported(
                "width {} mm is not wider than the stated trapezoid "
                "narrowing".format(low))

        def excess(width_base):
            return propagation.thickness_corrected_width(
                width_base - delta / 2.0, h, t) - h

        low_excess = excess(low)
        high_excess = excess(high)
        if low_excess == 0.0:
            seams.append((low, "left"))
        elif high_excess == 0.0:
            seams.append((high, "left"))
        elif low_excess < 0.0 < high_excess:
            # The anchor is fixed (not the caller's low) so the same
            # construction reports the same seam float regardless of
            # how the caller frames the domain around it; monotonicity
            # gives excess(anchor) <= excess(low) < 0, so the bracket
            # is valid without evaluating it.
            a = delta / 2.0 + 1e-6
            if not a < low:
                a = low
            c = high
            for _ in range(200):
                middle = (a + c) / 2.0
                if excess(middle) > 0.0:
                    c = middle
                else:
                    a = middle
                if c - a < 1e-12:
                    break
            seams.append((min(max((a + c) / 2.0, low), high), "left"))
    return sorted((s, owner) for s, owner in seams if low <= s <= high)


def _solve_width(context, low, high, target):
    """Roots of Z(width) = target, one branch interval at a time.

    The domain is partitioned at every top-level branch seam, and each
    interval is solved under BRANCH-FORCED evaluation over the closure
    of its branch's domain: the branch that owns the seam point (per the
    production inequality) treats it as a closed endpoint; the
    neighbouring branch prices it as its continuous one-sided limit and
    accepts a root there only strictly inside its own territory. No
    point of the domain is deleted, no point is owned twice, and a
    target equal to Z(seam) resolves on the owner branch - while a
    second root on the neighbouring branch, however close in width, is
    reported alongside it, because two roots are only ever one root
    when they are the same point, not when they are near each other.

    A seam equal to a domain endpoint keeps its owner: the owning
    branch is reduced to a degenerate closed interval - the single
    point it still holds inside the domain, where root existence is
    decided exactly - while the neighbouring branch spans the rest, so
    the closed domain [low, high] partitions without loss at its edges
    and no bisection ever crosses an unrepresented branch change.

    Within one interval a single closed-form branch applies, and each
    implemented branch is strictly decreasing in width on its whole
    domain - provable from the forms themselves (logarithms of strictly
    decreasing arguments over strictly increasing effective width; a
    reciprocal of a strictly increasing denominator) - so endpoint
    bracketing plus bisection is exact there, and the endpoints are
    still checked as a guard against a future branch that breaks the
    theorem.
    """
    seams = _seam_positions(context, low, high)
    intervals = []
    cursor, cursor_closed = low, True
    branches = ["narrow", "wide"]
    for index, (seam, owner) in enumerate(seams):
        intervals.append((cursor, cursor_closed, seam, owner == "left",
                          branches[min(index, 1)]))
        cursor, cursor_closed = seam, owner == "right"
    intervals.append((cursor, cursor_closed, high, True,
                      branches[min(len(seams), 1)]))

    roots = []
    spans = []
    for a, a_closed, b, b_closed, branch in intervals:
        force = branch if seams else None
        if a == b:
            # A seam at a domain endpoint leaves its owner exactly one
            # point. Open on either side the interval is empty; closed
            # on both it is the owned point itself, where root
            # existence is decided exactly - no bracketing, no
            # bisection, no tolerance.
            if not (a_closed and b_closed):
                continue
            z_point, eps_point = _impedance_at(context, a,
                                               _force_branch=force)
            spans.append("[{0:.4f}, {0:.4f}] mm is the owned seam "
                         "point at {1:.2f} ohm".format(a, z_point))
            if z_point == target:
                roots.append((a, z_point, eps_point))
            continue
        z_a, _e = _impedance_at(context, a, _force_branch=force)
        z_b, _e = _impedance_at(context, b, _force_branch=force)
        spans.append("[{:.4f}, {:.4f}] mm spans {:.2f} down to "
                     "{:.2f} ohm".format(a, b, z_a, z_b))
        if not z_a > z_b:
            raise ImpedanceError(
                "impedance does not decrease across the branch interval "
                "[{:.4f}, {:.4f}] mm (Z {:.3f} -> {:.3f}); the model's "
                "per-branch monotonicity assumption is violated and no "
                "root there would be trustworthy".format(a, b, z_a, z_b))
        if target > z_a or target < z_b:
            continue
        if target == z_a and not a_closed:
            # The branch's one-sided limit, not a point it owns.
            continue
        if target == z_b and not b_closed:
            continue
        left, right = a, b
        for _iteration in range(200):
            middle = (left + right) / 2.0
            z_middle, _eps = _impedance_at(context, middle,
                                           _force_branch=force)
            if abs(z_middle - target) < 1e-9 or (right - left) < 1e-12:
                break
            if z_middle > target:
                left = middle
            else:
                right = middle
        z_root, eps_root = _impedance_at(context, middle,
                                         _force_branch=force)
        roots.append((middle, z_root, eps_root))
    return sorted(roots), "; ".join(spans)



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


def _result(context, request, range_record, numeric, manufacturing,
            ambiguous, failure):
    """One result shape, with every success concept its own field.

    There is deliberately no single field spelled like success. The
    concepts an autonomous caller must not conflate are separated:
    `numeric_solution` (the analytic root, diagnostics even when
    unbuildable), `geometry_feasible` (root exists AND the width
    survives the profile's published routing limits - the most a
    nominal, uncontrolled calculation can ever establish),
    `fabrication_control.target_eligible_for_controlled_fabrication`
    (that same feasible geometry under a profile that also selects the
    controlled-impedance process), and
    `fabrication_control.target_bound_to_fabrication_specification`
    (always false here: no board- or order-side specification binds a
    solver-request target). The prose note is rendered from those same
    booleans, so it can never call an ineligible result eligible.
    """
    geometry_feasible = bool(numeric) and bool(manufacturing) \
        and manufacturing.get("established", False)
    controlled = bool(context["profile"].get("impedance_control"))
    if controlled and geometry_feasible:
        control_note = (
            "the profile selects the controlled-impedance process and "
            "this target sits inside its stated range with a "
            "manufacturable geometry, so it is ELIGIBLE for controlled "
            "fabrication - but the target came from this solver "
            "request, and no board- or order-side impedance "
            "specification binds it yet; nothing here proves the "
            "target has been specified to the fabricator")
    elif controlled:
        control_note = (
            "the profile selects the controlled-impedance process, but "
            "this result establishes no feasible geometry for the "
            "target (see numeric_solution, ambiguous_roots, "
            "manufacturing and failure), so the target is NOT eligible "
            "for controlled fabrication on this result")
    else:
        control_note = (
            "the profile does not select controlled impedance: a "
            "feasible geometry is an analytic nominal estimate, and "
            "nothing here establishes that the fabricated line meets "
            "the requested impedance")
    return {
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
        "target_range": range_record,
        "numeric_solution": numeric,
        "ambiguous_roots": ambiguous,
        "manufacturing": manufacturing,
        "geometry_feasible": geometry_feasible,
        "fabrication_control": {
            "impedance_control_selected": controlled,
            "target_eligible_for_controlled_fabrication":
                geometry_feasible and controlled,
            "target_bound_to_fabrication_specification": False,
            "note": control_note,
        },
        "failure": failure,
    }


def _fabrication_tolerance(approved_snapshot, context):
    """Process tolerance capability, kept apart from target applicability.

    Three facts an autonomous caller must not conflate: the fabricator
    PUBLISHES a standard tolerance for its controlled-impedance process
    (`process_tolerance_published`, with the verbatim figure and its
    source when it does); the profile SELECTS that process
    (`impedance_control_selected`); and whether that tolerance applies
    to THIS solver target (`applies_to_this_target`) - false in every
    case, because no board- or order-side impedance specification binds
    a solver-request target, exactly as
    fabrication_control.target_bound_to_fabrication_specification
    records. "The fabricator offers plus-minus ten percent" never
    becomes "plus-minus ten percent applies to this line" without that
    binding. The record is a pure function of the profile and the
    approved catalog - it never sees the numeric outcome - so its
    prose is written to stay true when no width is solved at all.
    """
    controlled = bool(context["profile"].get("impedance_control"))
    tolerance = approved_snapshot["normalized"]["capabilities"].get(
        "impedance_tolerance_standard_percent")
    record = {
        "impedance_control_selected": controlled,
        "process_tolerance_published": tolerance is not None,
        "applies_to_this_target": False,
    }
    if not controlled:
        record["note"] = (
            "the fabrication profile does not select controlled "
            "impedance, so the fabricator's stated controlled-impedance "
            "tolerance does NOT apply; any solved width here is an "
            "uncontrolled nominal analytic estimate only")
        return record
    if tolerance is None:
        record["note"] = (
            "no impedance tolerance is normalized from the approved "
            "sources; any solved width here is nominal only")
        return record
    record["stated_percent"] = tolerance["value"]
    record["source"] = tolerance["source"]
    record["note"] = (
        "the fabricator publishes this standard tolerance for its "
        "controlled-impedance process, quoted verbatim and never "
        "computed into an interval here, and the profile selects that "
        "process - but this solver target is not bound into any "
        "fabrication specification, so nothing here proves the "
        "fabricator will hold THIS line to the stated percent; any "
        "solved width remains a nominal analytic estimate, and a "
        "result without one carries no value for this tolerance to "
        "describe")
    return record


def solve(approved_snapshot, request):
    """The one public solve. Every result carries its evidence chain.

    There is deliberately no provenance-free variant: an AI caller must
    not be able to receive a geometry without the approved digest, parser
    identity, source hashes and model identity attached. The numerical
    internals stay private.
    """
    document = _solve(approved_snapshot, request)
    document["fabrication_tolerance"] = _fabrication_tolerance(
        approved_snapshot, document["context"])
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
