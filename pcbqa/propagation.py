"""Analytic passive propagation: how long copper takes, and how well it is known.

This is the first and lowest-fidelity backend. It answers one question - how
long does a signal take to travel this copper - from closed-form transmission
line theory, using the board's geometry and its physical stackup. It models no
driver, no receiver, no reflection, no loss and no coupling.

Why not simply c/sqrt(Dk)
-------------------------
That is the velocity in a *homogeneous* dielectric. A stripline is in one, so
for a stripline it is exact. An outer-layer microstrip is not: part of its
field is in air, so the wave sees an effective permittivity somewhere between 1
and Dk, always lower than Dk and dependent on the trace width and the height to
the reference plane. Using Dk directly for a microstrip over-states its delay
by roughly fifteen to twenty-five per cent on ordinary FR-4 geometry, and the
error is not common-mode across traces of different widths, so it does not even
cancel in a skew comparison.

So the microstrip case goes through a documented closed form for the effective
permittivity, and which form was used is recorded with the result.

Models implemented
------------------
``hammerstad``
    Hammerstad's zero-thickness microstrip effective-permittivity
    approximation (E. O. Hammerstad, "Equations for Microstrip Circuit
    Design", Proc. 5th European Microwave Conference, 1975), in the form
    reproduced as equation 3.195 in Pozar, *Microwave Engineering*:

        e_eff = (er+1)/2 + (er-1)/2 * (1 + 12 h/w)^(-1/2)                 w/h>=1
        e_eff = (er+1)/2 + (er-1)/2 * [(1 + 12 h/w)^(-1/2)
                                       + 0.04 (1 - w/h)^2]                w/h<1

``hammerstad-thickness-corrected``
    The same, evaluated at an effective width that accounts for finite
    conductor thickness (Hammerstad and Bekkadal's correction, as given in
    Wadell, *Transmission Line Design Handbook*, section 3.5):

        dw = (t/pi) * [1 + ln(2h/t)]        w/h >= 1/(2 pi)
        dw = (t/pi) * [1 + ln(4 pi w/t)]    w/h <  1/(2 pi)

``declared-effective``
    No formula at all: the board states an effective permittivity, or a
    propagation constant in ps/mm, per layer - from a fabricator's impedance
    report or a measurement. Used as given, recorded as declared.

For stripline the dielectric is homogeneous, the mode is TEM and e_eff is er
exactly, whether or not the two plane spacings are equal. That case therefore
needs no approximation and is marked as such.

Everything else refuses. Coplanar waveguide, broadside- and edge-coupled
differential geometry, mixed-dielectric striplines, embedded microstrip - an
inner layer with only one reference plane, whose field is in dielectric on both
sides rather than air on one - and any layer with no identifiable reference
plane are all outside these formulas, and returning a number for them anyway
would be the one thing this module exists not to do.

Two assumptions the microstrip case does make, stated because they are not
free: the trace is treated as bare rather than covered by solder mask, which
understates its effective permittivity slightly, and the reference plane is
treated as continuous under the whole run. Neither is checked here.
"""

from __future__ import annotations

import math

# Speed of light in vacuum, mm/ps. Exact by definition of the metre.
C_MM_PER_PS = 0.299792458

MICROSTRIP = "microstrip"
EMBEDDED_MICROSTRIP = "embedded_microstrip"
STRIPLINE = "stripline"
ASYMMETRIC_STRIPLINE = "asymmetric_stripline"

HAMMERSTAD = "hammerstad"
HAMMERSTAD_T = "hammerstad-thickness-corrected"
DECLARED_EFFECTIVE = "declared-effective"

MODELS = (HAMMERSTAD, HAMMERSTAD_T, DECLARED_EFFECTIVE)

# How much a result is worth. A PASS from first-order microstrip arithmetic
# must not read the same as a PASS from a validated broadband model, so every
# portion of every result carries one of these and a path takes the weakest of
# them. The ladder applies to any portion - a conductor run, a via transit, a
# component traversal - not just to copper.
GEOMETRY_ONLY = "geometry-only"
#: A portion whose contribution is real, positive and not modelled at all.
#: Ranks below every model, because a total containing one is a lower bound
#: rather than a value, and nothing derived from it may be read as a value.
UNKNOWN_CONTRIBUTION = "unmodelled-contribution"
ANALYTIC_TRANSMISSION_LINE = "analytic-transmission-line-estimate"
#: The same closed forms, applied across a reference discontinuity only
#: because the board declared an assumption about it. A value, not a bound -
#: but a value standing on an engineering judgement rather than on geometry
#: that was physically established, and it must never read as the latter.
ASSUMED_TRANSMISSION_LINE = "analytic-estimate-under-declared-assumptions"
DECLARED_PROPAGATION = "declared-propagation-constant"
DECLARED_MODEL = "declared-model"
QUASI_STATIC_EXTRACTED = "quasi-static-extracted"      # reserved for a solver
FULL_WAVE_EXTRACTED = "full-wave-extracted"            # reserved for a solver
DEVICE_AWARE = "device-aware-timing"                   # reserved, needs models

#: Rank, not order: two kinds of declared value are equally good and neither is
#: below the other. A rank comparison also means an unrecognised fidelity - one
#: from a backend this release does not know - sorts below everything rather
#: than raising or, worse, ranking high.
FIDELITY_RANK = {
    GEOMETRY_ONLY: 0,
    UNKNOWN_CONTRIBUTION: 1,
    ASSUMED_TRANSMISSION_LINE: 2,
    ANALYTIC_TRANSMISSION_LINE: 3,
    DECLARED_PROPAGATION: 4,
    DECLARED_MODEL: 4,
    QUASI_STATIC_EXTRACTED: 5,
    FULL_WAVE_EXTRACTED: 6,
    DEVICE_AWARE: 7,
}

FIDELITY_ORDER = tuple(sorted(FIDELITY_RANK, key=lambda f: (FIDELITY_RANK[f],
                                                            f)))


def fidelity_rank(name):
    """Where a fidelity sits. Anything unrecognised sits below everything."""
    return FIDELITY_RANK.get(name, -1)


def _stub_extent(record):
    """Which parts of the barrel this model does not describe."""
    top, bottom = record.get("via_top_layer"), record.get("via_bottom_layer")
    entered, left = record.get("from_layer"), record.get("to_layer")
    above = top if top not in (entered, left) else None
    below = bottom if bottom not in (entered, left) else None
    if not above and not below:
        return None
    return {"above": above, "below": below,
            "note": "barrel outside the traversed span; stub effects are not "
                    "modelled by this release"}


def weakest(fidelities):
    """The weakest fidelity in a set.

    A result is worth exactly what its worst modelled portion is worth. Ties
    break on the name so the answer is deterministic across runs.
    """
    if not fidelities:
        return GEOMETRY_ONLY
    return min(sorted(fidelities), key=fidelity_rank)

# Via vertical-transit treatments.
VIA_NONE = "none"
VIA_GEOMETRIC = "geometric"
VIA_MODELS = (VIA_NONE, VIA_GEOMETRIC)

#: Recognised but not implemented, so a board asking for one is refused by name
#: rather than by "unknown model".
VIA_RESERVED = ("extracted", "sparameter", "quasi_static", "full_wave")


class ViaPolicy:
    """What a board has decided about the vertical transit through a hole.

    Four states, and the difference between the middle two is the whole point.

    ``absent``      nothing declared. The transit is real and positive and
                    nothing is attributed to it, so a total containing one is
                    a lower bound.
    ``none``        the board named the treatment but justified nothing. It
                    has chosen to omit the transit, not established that it is
                    negligible - so this is a lower bound, and saying otherwise
                    would turn "not modelled yet" into an asserted exact zero.
    ``none`` + justification
                    the same, with a reason recorded. Prose explains a decision;
                    it does not measure a barrel, so this is *still* a lower
                    bound. A sentence cannot establish more physical certainty
                    than the sentence contains.
    ``none`` + ``max_delay_ps``
                    the board states an upper bound on what it omitted, per
                    transit: a path through two barrels accrues the bound
                    twice. That is
                    substance rather than explanation: the total is still a
                    lower bound, but a *bounded* one, so a maximum-delay
                    requirement can be decided against `delay + bound` instead
                    of being unevaluable.
    ``geometric``   first-order: the barrel as a length of line in the
                    surrounding dielectric. The only treatment here that is
                    exact, and refused outright when the stackup lacks the
                    data it reads.
    """

    __slots__ = ("model", "justification", "declared", "max_delay_ps",
                 "provenance")

    def __init__(self, model, justification=None, declared=True,
                 max_delay_ps=None, provenance=None):
        self.model = model
        self.justification = justification
        self.declared = declared
        self.max_delay_ps = max_delay_ps
        self.provenance = provenance

    @property
    def exact(self):
        """Does this contribute a value rather than an acknowledged omission?"""
        return self.model == VIA_GEOMETRIC

    @property
    def omitted_bound_ps(self):
        """How much the omission could be worth, when the board says."""
        return None if self.exact else self.max_delay_ps

    @property
    def fidelity(self):
        if self.model == VIA_GEOMETRIC:
            return ANALYTIC_TRANSMISSION_LINE
        return UNKNOWN_CONTRIBUTION

    def note(self):
        if self.model == VIA_GEOMETRIC:
            return ("first-order: barrel treated as a length of line in the "
                    "surrounding dielectric; no inductance, no stub, no "
                    "discontinuity")
        if self.max_delay_ps is not None:
            return ("vertical extent measured and no delay attributed, but "
                    "the board bounds what it omitted at {} ps, so a maximum "
                    "can still be decided against the bound{}".format(
                        self.max_delay_ps,
                        ": " + self.justification if self.justification
                        else ""))
        if self.justification:
            return ("vertical extent measured, no delay attributed, on a "
                    "declared reason: {}. A reason records a decision; it does "
                    "not measure the transit, so this remains a lower "
                    "bound".format(self.justification))
        if self.declared:
            return ("vertical extent measured and no delay attributed. This "
                    "board named the 'none' treatment but justified nothing, "
                    "so the transit is omitted rather than shown to be "
                    "negligible, and any total containing it is a lower bound")
        return ("vertical extent measured, no delay attributed, and this board "
                "declared no via treatment at all. The transit is real and "
                "positive, so any total containing it is a lower bound")

    def to_dict(self):
        return {"model": self.model, "declared": self.declared,
                "justified": bool(self.justification),
                "bounded": self.max_delay_ps is not None,
                **({"max_delay_ps": self.max_delay_ps,
                    "provenance": self.provenance}
                   if self.max_delay_ps is not None else {}),
                **({"justification": self.justification}
                   if self.justification else {})}


def via_policy(declaration):
    """Read a board's `via_delay_model` declaration. Absent is a state too."""
    if declaration is None:
        return ViaPolicy(VIA_NONE, None, declared=False)
    bound = None
    if isinstance(declaration, str):
        model, justification = declaration, None
    elif isinstance(declaration, dict):
        model = declaration.get("model")
        justification = declaration.get("justification")
        bound = declaration.get("max_delay_ps")
        if not model:
            raise PropagationError(
                "the via delay model declares no `model`")
        if bound is not None:
            if isinstance(bound, bool) or not isinstance(bound, (int, float)):
                raise PropagationError(
                    "via max_delay_ps is {!r}, not a number".format(bound))
            if bound < 0 or bound != bound or bound in (float("inf"),
                                                        float("-inf")):
                raise PropagationError(
                    "via max_delay_ps is {!r}, which is not a usable "
                    "bound".format(bound))
            if not justification:
                raise PropagationError(
                    "a via max_delay_ps bounds what the board omitted, so it "
                    "requires a `justification` saying why the omission is "
                    "acceptable")
            if not declaration.get("provenance"):
                raise PropagationError(
                    "a via max_delay_ps is a number that PASS/FAIL arithmetic "
                    "will lean on, so it requires a `provenance` saying where "
                    "the number came from - a datasheet, a calculation, a "
                    "measurement. A justification explains the decision; it "
                    "does not source the figure")
    else:
        raise PropagationError(
            "via_delay_model is a {}, not a string or an object".format(
                type(declaration).__name__))
    if model in VIA_RESERVED:
        raise PropagationError(
            "via delay model {!r} is recognised but this release implements no "
            "evaluation for it; implemented: {}".format(
                model, ", ".join(VIA_MODELS)))
    if model not in VIA_MODELS:
        raise PropagationError(
            "via delay model {!r} is not implemented; this validator has "
            "{}".format(model, ", ".join(VIA_MODELS)))
    if bound is not None and model == VIA_GEOMETRIC:
        raise PropagationError(
            "the geometric via model computes the transit, so bounding an "
            "omission alongside it states two different things about the same "
            "barrel")
    return ViaPolicy(model, justification, declared=True, max_delay_ps=bound,
                     provenance=(declaration.get("provenance")
                                 if isinstance(declaration, dict) else None))


#: The result contract every backend produces, whatever it is inside.
#:
#: This exists so the gates can stay backend-agnostic and a board's manifest
#: never has to know which one ran. A full-wave extraction and a closed-form
#: estimate answer the same question at different quality, so they return the
#: same shape and differ in `fidelity` - which is the field the gates already
#: use to refuse to overstate a result.
#:
#: Per conductor run: `layer`, `width_mm`, `length_mm`, `ps_per_mm`,
#: `delay_ps`, `fidelity`, and either `mode` plus `geometry` (derived from a
#: stackup) or `provenance` (declared or measured).
CONDUCTOR_RESULT_FIELDS = ("layer", "width_mm", "ps_per_mm", "fidelity",
                           "delay_ps")

#: Per via transition: where it went and what, if anything, that cost.
VIA_RESULT_FIELDS = ("from_layer", "to_layer", "model", "vertical_length_mm",
                     "delay_ps", "fidelity")

#: Per path: the totals, how good they are, and what stopped them.
#: `delay_ps` is None when some portion could not be evaluated at all;
#: `delay_is_lower_bound` is True when every portion was evaluable but at
#: least one omitted a known-nonnegative contribution, so the truth's upper
#: side exceeds the modelled sum by an unknown (or separately bounded)
#: amount. What the flag does NOT claim: that `delay_ps` is the interval's
#: lower endpoint. `delay_lower_ps` is - geometric length uncertainty can
#: place the true delay BELOW the modelled nominal even while the flag is
#: true, and the interval endpoints, not this flag, are what the gates
#: compare against limits. The flag names the presence of omissions; a
#: backend that cannot honour that distinction cannot be plugged in here.
PATH_RESULT_FIELDS = ("path", "source", "destination", "delay_ps",
                      "delay_lower_ps", "delay_upper_ps",
                      "delay_is_lower_bound", "omitted_bound_ps",
                      "geometric_uncertainty_ps", "fidelity", "insufficient",
                      "backend", "conductors", "vias",
                      "component_traversals")


class PropagationError(Exception):
    """The model cannot be evaluated as asked. Always blocks; never guesses."""


class Unsupported(PropagationError):
    """The geometry is outside the implemented formulas."""


# ---------------------------------------------------------------------------
# effective permittivity
# ---------------------------------------------------------------------------

def hammerstad_effective_permittivity(epsilon_r, width_mm, height_mm):
    """Hammerstad's zero-thickness microstrip e_eff. See the module docstring."""
    _positive("relative permittivity", epsilon_r)
    _positive("trace width", width_mm)
    _positive("dielectric height", height_mm)
    if epsilon_r < 1.0:
        raise PropagationError(
            "relative permittivity {} is below 1, which no dielectric "
            "has".format(epsilon_r))
    ratio = width_mm / height_mm
    mean = (epsilon_r + 1.0) / 2.0
    half = (epsilon_r - 1.0) / 2.0
    base = (1.0 + 12.0 / ratio) ** -0.5
    if ratio >= 1.0:
        return mean + half * base
    return mean + half * (base + 0.04 * (1.0 - ratio) ** 2)


def thickness_corrected_width(width_mm, height_mm, conductor_mm):
    """Effective width for a conductor of finite thickness."""
    _positive("trace width", width_mm)
    _positive("dielectric height", height_mm)
    if conductor_mm is None or conductor_mm <= 0:
        raise PropagationError(
            "the thickness-corrected model needs a copper thickness and the "
            "stackup states none")
    ratio = width_mm / height_mm
    if ratio >= 1.0 / (2.0 * math.pi):
        delta = (conductor_mm / math.pi) * (1.0 + math.log(2.0 * height_mm
                                                           / conductor_mm))
    else:
        delta = (conductor_mm / math.pi) * (
            1.0 + math.log(4.0 * math.pi * width_mm / conductor_mm))
    return width_mm + delta


def _positive(label, value):
    if value is None:
        raise PropagationError("{} is not known".format(label))
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PropagationError("{} is {!r}, not a number".format(label, value))
    if value <= 0 or value != value or value in (float("inf"), float("-inf")):
        raise PropagationError(
            "{} is {!r}, which is not a usable positive length".format(
                label, value))


def delay_ps_per_mm(epsilon_effective):
    """Propagation delay per millimetre for a given effective permittivity."""
    _positive("effective permittivity", epsilon_effective)
    return math.sqrt(epsilon_effective) / C_MM_PER_PS


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------

class DeclaredLayerModel:
    """A board's own propagation constant for one layer.

    Either an effective permittivity or a delay directly in ps/mm. Nothing is
    derived from geometry, so nothing about the geometry has to be known - this
    is the escape hatch for a board that has a fabricator's impedance and delay
    report but no dielectric breakdown, and for geometry the closed forms do
    not cover.
    """

    def __init__(self, layer, spec):
        self.layer = layer
        self.epsilon_effective = spec.get("epsilon_effective")
        self.ps_per_mm = spec.get("ps_per_mm")
        self.provenance = spec.get("provenance")
        #: The reference plane(s) this constant assumes continuous beneath the
        #: layer. A per-layer characterisation describes an intact structure;
        #: it says nothing about a route crossing a split in that structure,
        #: so declaring a constant must not bypass the continuity check. Left
        #: unstated, the check runs against every candidate reference plane -
        #: the conservative reading. A future path-specific measured or
        #: extracted model would carry its own scope instead of this one.
        self.reference_layers = spec.get("reference_layers")
        if self.reference_layers is not None and (
                not isinstance(self.reference_layers, list)
                or not self.reference_layers
                or not all(isinstance(name, str) and name
                           for name in self.reference_layers)):
            raise PropagationError(
                "the declared propagation model for layer {!r} has a "
                "reference_layers that is not a non-empty list of layer "
                "names. An empty list would scope the continuity check to "
                "nothing at all - a bypass wearing a scope's clothes; omit "
                "the field to check every candidate plane".format(layer))
        if self.epsilon_effective is None and self.ps_per_mm is None:
            raise PropagationError(
                "the declared propagation model for layer {!r} states neither "
                "epsilon_effective nor ps_per_mm, so it declares "
                "nothing".format(layer))
        if self.epsilon_effective is not None and self.ps_per_mm is not None:
            raise PropagationError(
                "the declared propagation model for layer {!r} states both "
                "epsilon_effective and ps_per_mm; they can disagree, so only "
                "one may be given".format(layer))
        if not self.provenance:
            raise PropagationError(
                "the declared propagation model for layer {!r} records no "
                "provenance; a propagation constant nobody can trace to a "
                "measurement or a fabricator's report is a guess with a "
                "units label".format(layer))

    def resolve(self):
        if self.ps_per_mm is not None:
            _positive("declared ps_per_mm", self.ps_per_mm)
            return self.ps_per_mm, None
        _positive("declared epsilon_effective", self.epsilon_effective)
        return delay_ps_per_mm(self.epsilon_effective), self.epsilon_effective


def required_stackup_fields(model, via_model, declared_layers=None,
                            layers=None):
    """Exactly the stackup fields a set of model choices will read.

    The point of asking is that different analyses need different subsets, and
    a stackup that is incomplete for one may be perfectly sufficient for
    another. A first-order delay needs a dielectric height and a permittivity.
    The thickness-corrected form additionally needs a copper thickness. A loss
    figure would need a loss tangent, which no model here computes, so nothing
    here asks for one - and refusing an answer for want of a figure the
    calculation never reads would be refusing it for no reason.

    `layers` narrows the question to the copper layers the paths under analysis
    actually run on. A layer nobody routes on cannot make an analysis
    impossible, and a stackup silent about it is not incomplete for any purpose
    this board has.

    A free function rather than only a method because the gate needs the answer
    to report what a stackup is missing *for this board*, and it needs it even
    when the model itself could not be constructed.
    """
    from .stackup_physical import (NEEDS_COPPER_THICKNESS,
                                   NEEDS_DIELECTRIC_THICKNESS,
                                   NEEDS_EPSILON_R)
    declared = set(declared_layers or ())
    wanted = set()
    for layer in (layers or ()):
        if layer in declared:
            # A declared propagation constant replaces the geometry it would
            # otherwise have been derived from, so that layer's dielectric is
            # not read at all.
            continue
        wanted.add(NEEDS_DIELECTRIC_THICKNESS)
        wanted.add(NEEDS_EPSILON_R)
        if model == HAMMERSTAD_T:
            wanted.add(NEEDS_COPPER_THICKNESS)
    if _via_model_name(via_model) == VIA_GEOMETRIC:
        # Everything inside a barrel's vertical span, not just the layers the
        # horizontal copper runs on. The caller widens `layers` to the span for
        # the same reason; this only says which fields get read there.
        wanted.add(NEEDS_DIELECTRIC_THICKNESS)
        wanted.add(NEEDS_EPSILON_R)
        wanted.add(NEEDS_COPPER_THICKNESS)
    return wanted


def _via_model_name(via_model):
    """The model name out of either declaration form."""
    if isinstance(via_model, dict):
        return via_model.get("model")
    if isinstance(via_model, ViaPolicy):
        return via_model.model
    return via_model


def via_span_needs_stackup(via_model):
    """Does this via treatment read the stackup inside a barrel's span?"""
    return _via_model_name(via_model) == VIA_GEOMETRIC


class ReferenceAssumption:
    """One declared assumption about one physical condition, with its scope.

    An assumption exists to tolerate a *known local condition* - the antipad
    ring around a particular interface's vias, a documented pour edge - and a
    declaration that quietly excused every split, void and gap on every layer
    of the board would be a waiver wearing an assumption's name. So each entry
    says where it applies:

      * ``reference_layers`` (required): which plane's discontinuity is being
        assumed away. The plane is the physical thing with the gap in it, so
        an assumption that does not name one describes nothing.
      * ``signal_layers`` (optional): only traces on these layers.
      * ``paths`` (optional): a regex over resolved path ids, so an assumption
        can be pinned to the interface it was written for.

    And how much: ``up_to_mm`` bounds the *summed* unreferenced length per
    plane within one conductor run - several small gaps in a run cannot
    slip under a bound written for one. What one declaration cannot bound
    is accumulation across runs: a path crossing many separately-covered
    runs leans on the assumption once per run, so the evaluation surfaces
    `assumed_unreferenced_total_mm` on the path record and every use in
    `assumptions`, and nothing marked `ASSUMED_TRANSMISSION_LINE` ever
    reads as physically established geometry.

    A stated limitation, deliberate until a spatial model exists: the
    geometry walk sums missing-reference length per plane and never
    records where along the run each gap sits, so this assumption cannot
    tell the intended local condition (say, a via's antipad ring) from an
    unrelated void on the same plane that happens to fit inside the same
    bound. The declaration therefore authorizes a TOTAL, and a declarer
    must size ``up_to_mm`` to the intended condition alone - tight, not
    comfortable - and say in ``justification`` where that number comes
    from, because the justification is the only provenance a reviewer
    gets. Distinguishing gaps by position would need per-gap intervals
    carried out of the geometry walk; nothing here fakes that today.
    """

    __slots__ = ("up_to_mm", "justification", "reference_layers",
                 "signal_layers", "paths_pattern", "paths_source")

    def __init__(self, declaration):
        if not isinstance(declaration, dict):
            raise PropagationError(
                "a reference_discontinuity entry is a {}, not an "
                "object".format(type(declaration).__name__))
        treatment = declaration.get("treatment")
        if treatment != "assume_continuous":
            raise PropagationError(
                "reference discontinuity treatment {!r} is not one of refuse, "
                "assume_continuous. Refusal is the default and needs no "
                "declaration; the only thing worth declaring is an "
                "assumption".format(treatment))
        self.up_to_mm = declaration.get("up_to_mm")
        self.justification = declaration.get("justification")
        if self.up_to_mm is None:
            raise PropagationError(
                "assuming reference continuity requires `up_to_mm`: the "
                "assumption has to have a size, or nothing bounds what is "
                "being assumed away")
        if isinstance(self.up_to_mm, bool) \
                or not isinstance(self.up_to_mm, (int, float)) \
                or self.up_to_mm != self.up_to_mm \
                or self.up_to_mm in (float("inf"), float("-inf")) \
                or self.up_to_mm <= 0:
            raise PropagationError(
                "reference-continuity up_to_mm is {!r}, which is not a "
                "finite positive length; a malformed bound would compare as "
                "whatever Python happens to make of it".format(self.up_to_mm))
        if not self.justification:
            raise PropagationError(
                "assuming reference continuity requires a `justification` "
                "saying why the formula still applies over the gap")
        planes = declaration.get("reference_layers")
        if (not isinstance(planes, list) or not planes
                or not all(isinstance(p, str) and p for p in planes)):
            raise PropagationError(
                "assuming reference continuity requires `reference_layers` "
                "naming the plane(s) whose discontinuity is being assumed "
                "over. The plane is the physical thing with the gap in it; an "
                "assumption that names none is a board-wide waiver, and this "
                "mechanism deliberately is not one")
        self.reference_layers = set(planes)
        signal = declaration.get("signal_layers")
        if signal is not None and (
                not isinstance(signal, list)
                or not all(isinstance(s, str) and s for s in signal)):
            raise PropagationError(
                "reference_discontinuity signal_layers is not a list of "
                "layer names")
        self.signal_layers = set(signal) if signal is not None else None
        self.paths_source = declaration.get("paths")
        if self.paths_source is not None:
            import re as _re
            self.paths_pattern = _re.compile(self.paths_source)
        else:
            self.paths_pattern = None

    def covers(self, plane, signal_layer, path_id, missing_mm):
        if plane not in self.reference_layers:
            return False
        if self.signal_layers is not None and signal_layer \
                not in self.signal_layers:
            return False
        if self.paths_pattern is not None and (
                path_id is None or not self.paths_pattern.match(path_id)):
            return False
        return missing_mm <= self.up_to_mm

    def to_dict(self):
        return {"treatment": "assume_continuous",
                "up_to_mm": self.up_to_mm,
                "reference_layers": sorted(self.reference_layers),
                **({"signal_layers": sorted(self.signal_layers)}
                   if self.signal_layers is not None else {}),
                **({"paths": self.paths_source}
                   if self.paths_source is not None else {}),
                "justification": self.justification}


class ReferenceDiscontinuity:
    """What a board says about copper whose reference plane is interrupted.

    Two questions get confused here, and keeping them apart is the whole
    point. Whether the board is *acceptable* with N millimetres of missing
    reference copper is a design requirement, and belongs to a gate that
    measures the board. Whether the microstrip equation still *describes* a
    trace over that gap is a question about the formula, and no board-level
    tolerance makes the answer yes. This object answers only the second, one
    scoped assumption at a time; absent any, the model refuses, which is the
    conservative reading and the default.
    """

    __slots__ = ("assumptions",)

    def __init__(self, declaration=None):
        if declaration is None or declaration == {"treatment": "refuse"}:
            self.assumptions = []
            return
        entries = declaration if isinstance(declaration, list) \
            else [declaration]
        self.assumptions = [ReferenceAssumption(entry) for entry in entries]

    def covering(self, plane, signal_layer, path_id, missing_mm):
        """The first assumption covering this gap, or None."""
        for assumption in self.assumptions:
            if assumption.covers(plane, signal_layer, path_id, missing_mm):
                return assumption
        return None

    def to_dict(self):
        if not self.assumptions:
            return {"treatment": "refuse"}
        return {"assumptions": [a.to_dict() for a in self.assumptions]}


class PropagationModel:
    """Turns a resolved path plus a physical stackup into a delay.

    Constructed from the board's declared policy; it holds no defaults of its
    own, because every choice it makes - which closed form, whether vias
    contribute, what a layer's reference plane is - changes the answer and
    therefore belongs in configuration the board wrote down and provenance can
    hash.
    """

    def __init__(self, stackup, reference_layers, model=HAMMERSTAD,
                 via_model=None, declared_layers=None, backend="analytic",
                 discontinuity=None, unfilled_reference_layers=()):
        if model not in MODELS:
            raise PropagationError(
                "propagation model {!r} is not implemented; this validator has "
                "{}".format(model, ", ".join(MODELS)))
        self.via_treatment = via_policy(via_model)
        self.stackup = stackup
        self.reference_layers = set(reference_layers or ())
        self.model = model
        self.via_model = self.via_treatment.model
        #: What to do about copper whose reference plane is interrupted. This
        #: is a statement about the *formula*, not about the design: whether a
        #: microstrip equation still describes a trace that runs over a void is
        #: a different question from whether the board is acceptable with that
        #: void in it, and a board-level tolerance answering the second must
        #: not silently answer the first. `refuse` unless declared otherwise.
        self.discontinuity = ReferenceDiscontinuity(discontinuity)
        #: Reference layers whose zones were never filled. Coverage against one
        #: of these is unknown, and unknown is not covered.
        self.unfilled_reference_layers = set(unfilled_reference_layers or ())
        self.backend = backend
        self.declared = {
            layer: DeclaredLayerModel(layer, spec)
            for layer, spec in (declared_layers or {}).items()}
        if model == DECLARED_EFFECTIVE and not self.declared:
            raise PropagationError(
                "the declared-effective model was selected but no layer "
                "declares a propagation constant")
        self._cache = {}

    # -- what this configuration needs from a stackup ----------------------
    def required_stackup_fields(self, layers=None):
        """Exactly the stackup fields these model choices will read."""
        return required_stackup_fields(
            self.model, self.via_model,
            {name: True for name in self.declared},
            layers if layers is not None else self.stackup.copper_layer_names)

    # -- per-conductor -----------------------------------------------------
    def conductor(self, layer, width_mm):
        """Delay per millimetre on one layer at one width, and how it was got.

        Raises `Unsupported` or `PropagationError` rather than returning
        anything when the stackup does not support the question. The caller
        turns that into a finding.
        """
        key = (layer, width_mm)
        if key in self._cache:
            cached = self._cache[key]
            if isinstance(cached, Exception):
                raise cached
            return cached
        try:
            result = self._conductor(layer, width_mm)
        except PropagationError as exc:
            self._cache[key] = exc
            raise
        self._cache[key] = result
        return result

    def _conductor(self, layer, width_mm):
        declared = self.declared.get(layer)
        if declared is not None:
            ps_per_mm, epsilon_effective = declared.resolve()
            return {
                "layer": layer, "width_mm": width_mm,
                "model": DECLARED_EFFECTIVE,
                "fidelity": DECLARED_PROPAGATION,
                "epsilon_effective": epsilon_effective,
                "ps_per_mm": ps_per_mm,
                "provenance": declared.provenance,
                # A per-layer constant characterises an intact structure, so
                # continuity is still checked - against the planes the
                # declaration says it assumes, or conservatively against
                # every candidate plane when it says nothing. Declaring a
                # number is not declaring that the ground under it is whole.
                "reference_layers_used": sorted(
                    declared.reference_layers
                    if declared.reference_layers is not None
                    else self.reference_layers),
                "reference_scope": ("declared"
                                    if declared.reference_layers is not None
                                    else "all_candidate_planes"),
                "geometry": None,
            }
        if self.model == DECLARED_EFFECTIVE:
            raise PropagationError(
                "the declared-effective model is selected but layer {!r} "
                "declares no propagation constant".format(layer))

        geometry = self.stackup.reference_geometry(layer,
                                                   self.reference_layers)
        if geometry.mode is None:
            raise Unsupported(
                "layer {!r}: {}".format(layer, "; ".join(
                    p["issue"] for p in geometry.problems)
                    or "no transmission-line geometry could be identified"))
        if geometry.problems:
            raise Unsupported(
                "layer {!r} is {} but the stackup is insufficient: {}".format(
                    layer, geometry.mode,
                    "; ".join(p["issue"] for p in geometry.problems)))

        if geometry.mode in (STRIPLINE, ASYMMETRIC_STRIPLINE):
            # Homogeneous dielectric: the mode is TEM and e_eff is er exactly,
            # symmetric or not. This is the one place c/sqrt(Dk) is right.
            epsilon_effective = geometry.epsilon_r
            record_model = "homogeneous-dielectric"
        elif geometry.mode == MICROSTRIP:
            width = width_mm
            if self.model == HAMMERSTAD_T:
                width = thickness_corrected_width(
                    width_mm, geometry.height_mm, geometry.copper_thickness_mm)
            epsilon_effective = hammerstad_effective_permittivity(
                geometry.epsilon_r, width, geometry.height_mm)
            record_model = self.model
        elif geometry.mode == EMBEDDED_MICROSTRIP:
            raise Unsupported(
                "layer {!r} has one reference plane but is not an outer layer, "
                "so the other side of the trace is dielectric rather than air. "
                "The microstrip approximation would understate its effective "
                "permittivity and therefore its delay. Give the layer a second "
                "reference plane, or declare an effective propagation constant "
                "for it".format(layer))
        else:
            raise Unsupported(
                "layer {!r} is {}, which none of the implemented closed forms "
                "cover; declare an effective propagation constant for it or "
                "use a higher-fidelity backend".format(layer, geometry.mode))

        return {
            "layer": layer, "width_mm": width_mm,
            "mode": geometry.mode,
            # The specific planes this result is about. Coverage is checked
            # against exactly these, because these are the conductors the
            # formula assumes are carrying the return current.
            "reference_layers_used": [name for name in
                                      (geometry.reference_above,
                                       geometry.reference_below)
                                      if name],
            "model": record_model,
            "fidelity": ANALYTIC_TRANSMISSION_LINE,
            "epsilon_r": geometry.epsilon_r,
            "epsilon_effective": round(epsilon_effective, 6),
            "ps_per_mm": round(delay_ps_per_mm(epsilon_effective), 6),
            "geometry": {
                "dielectric_height_mm": geometry.height_mm,
                "copper_thickness_mm": geometry.copper_thickness_mm,
                "reference_above": geometry.reference_above,
                "reference_below": geometry.reference_below,
                "material": geometry.material,
                "width_over_height": (round(width_mm / geometry.height_mm, 6)
                                      if geometry.height_mm else None),
            },
        }

    # -- vias --------------------------------------------------------------
    def via(self, transition):
        """One via transition's vertical length, and its delay if modelled."""
        record = {
            "from_layer": transition.get("from_layer"),
            "to_layer": transition.get("to_layer"),
            "via_top_layer": transition.get("via_top_layer"),
            "via_bottom_layer": transition.get("via_bottom_layer"),
            "model": self.via_treatment.model,
            "through": transition.get("through", "via"),
        }
        span = self._vertical_mm(transition.get("from_layer"),
                                 transition.get("to_layer"))
        # A stackup that states no thickness gives no vertical length. That is
        # a missing measurement, reported as absent - not a zero, and not a
        # crash on rounding it.
        length = None if span is None else span["length_mm"]
        record["vertical_length_mm"] = (None if length is None
                                        else round(length, 6))
        record["layers_crossed"] = None if span is None else span["crossed"]
        record["policy"] = self.via_treatment.to_dict()
        if self.via_treatment.model == VIA_NONE:
            record["delay_ps"] = 0.0
            record["fidelity"] = self.via_treatment.fidelity
            record["exact"] = self.via_treatment.exact
            record["note"] = self.via_treatment.note()
            return record
        if span is None:
            raise Unsupported(
                "a via transition between {!r} and {!r} cannot be measured "
                "against this stackup".format(transition.get("from_layer"),
                                              transition.get("to_layer")))
        if span["length_mm"] is None:
            raise Unsupported(
                "the stackup states no thickness for the layers a via crosses "
                "between {!r} and {!r}".format(transition.get("from_layer"),
                                               transition.get("to_layer")))
        if span["epsilon_r"] is None:
            raise Unsupported(
                "the dielectric a via passes through between {!r} and {!r} "
                "states no relative permittivity".format(
                    transition.get("from_layer"), transition.get("to_layer")))
        # First-order and labelled as such: the barrel treated as a length of
        # line in the surrounding dielectric. It attributes no inductance and
        # models no stub, which is why it is named `geometric` in the report.
        record["epsilon_r"] = span["epsilon_r"]
        # What this model computes is the transit between the layers the signal
        # actually changed between. Any barrel above or below that is stub, and
        # stub effects are not modelled here at all - not as zero, but as
        # outside what this calculation describes.
        record["unmodelled_stub"] = _stub_extent(record)
        record["fidelity"] = self.via_treatment.fidelity
        record["exact"] = True
        record["delay_ps"] = round(
            span["length_mm"] * math.sqrt(span["epsilon_r"]) / C_MM_PER_PS, 6)
        record["note"] = self.via_treatment.note()
        record["models"] = ("delay of the actively traversed barrel span "
                            "only, as a first-order line in the surrounding "
                            "dielectric; stub resonance, inductance and "
                            "broadband behaviour are outside this model")
        return record

    def _vertical_mm(self, from_layer, to_layer):
        if not from_layer or not to_layer or from_layer == to_layer:
            return None
        names = [l.name for l in self.stackup.layers]
        if from_layer not in names or to_layer not in names:
            return None
        low, high = sorted((names.index(from_layer), names.index(to_layer)))
        crossed, epsilons = [], []
        total, known, complete = 0.0, True, True
        for entry in self.stackup.layers[low + 1:high]:
            crossed.append(entry.name)
            if entry.thickness_mm is None:
                known = False
            else:
                total += entry.thickness_mm
            if not entry.is_dielectric:
                continue
            if entry.epsilon_r is None:
                # A dielectric inside the barrel that states no permittivity
                # makes the whole span unknown. Taking the value from the
                # dielectrics either side of it would be filling a gap with a
                # neighbour's number - which is the guess this refuses to make,
                # and is invisible in the answer once made.
                complete = False
            else:
                epsilons.append(entry.epsilon_r)
        unique = set(epsilons)
        return {"length_mm": total if known else None,
                "crossed": crossed,
                "epsilon_r": (unique.pop()
                              if complete and len(unique) == 1 else None)}

    # -- whole paths -------------------------------------------------------
    def _missing_on_used_planes(self, conductor, model):
        """(per-plane missing, combined missing, unfilled, unknown).

        `per plane` is how much of this run each used plane fails to cover.
        `combined` is how much of the run the *required geometry* is
        incomplete over - the union of the gaps, because a stripline is not a
        stripline wherever either of its planes is absent. Two planes with
        disjoint 3 mm gaps leave 6 mm of the run without the geometry, and
        taking the maximum of the scalars would have called it 3.

        The union comes from the resolver, which measured this run against
        the pairwise intersection of the planes while the shapes still
        existed. Where that key is absent the fallback is the sum of the
        per-plane figures capped at the run length - an over-estimate of the
        union, never an under-estimate, so the conservative direction.
        """
        used = model.get("reference_layers_used") or []
        per_layer = conductor.get("unreferenced_by_layer_mm") or {}
        unfilled = [name for name in used
                    if name in self.unfilled_reference_layers]
        per_plane = {}
        unknown = []
        for name in used:
            if name in unfilled:
                continue
            if name not in per_layer:
                unknown.append(name)
                continue
            per_plane[name] = per_layer[name]
        combined = None
        if not unfilled and not unknown and len(per_plane) == len(used):
            if len(used) <= 1:
                combined = next(iter(per_plane.values()), 0.0)
            else:
                combined = per_layer.get("&".join(sorted(used)))
                if combined is None:
                    combined = min(sum(per_plane.values()),
                                   conductor["length_mm"])
        return per_plane, combined, unfilled, unknown

    def _reference_problem(self, conductor, model, path_id=None):
        """Why this run cannot be modelled against its planes, or None.

        Distinct states, and only the last is "there is a gap": a plane whose
        zone was never filled, so nothing is known; a plane no coverage was
        computed for at all; a gap no declared assumption covers.
        """
        if not model.get("reference_layers_used"):
            return None
        if not conductor.get("reference_checked"):
            return None
        per_plane, combined, unfilled, unknown = self._missing_on_used_planes(
            conductor, model)
        common = {"portion": "conductor", "layer": conductor["layer"],
                  "width_mm": conductor["width_mm"],
                  "length_mm": conductor["length_mm"],
                  "reference_layers_used": model["reference_layers_used"]}
        if unfilled:
            return {**common, "unfilled_reference_layers": unfilled,
                    "issue": "the reference plane(s) {} carry no filled "
                             "polygons, so whether copper is actually under "
                             "this run was never established. An unfilled "
                             "zone is not an empty one: refill the board and "
                             "re-run".format(", ".join(unfilled))}
        if unknown:
            return {**common, "reference_layers_unmeasured": unknown,
                    "issue": "no reference copper was found on {} at all, so "
                             "the plane this run is referenced to does not "
                             "exist as poured copper".format(
                                 ", ".join(unknown))}
        gapped = {plane: value for plane, value in per_plane.items()
                  if value > 0.0}
        if not gapped:
            return None
        signal_layer = conductor["layer"]
        uncovered = sorted(
            plane for plane, value in gapped.items()
            if self.discontinuity.covering(plane, signal_layer, path_id,
                                           value) is None)
        if not uncovered:
            return None
        return {**common,
                "unreferenced_mm": round(combined, 4),
                "unreferenced_by_plane_mm": {p: round(v, 4)
                                             for p, v in sorted(
                                                 gapped.items())},
                "issue": "the required transmission-line geometry is "
                         "incomplete over {} mm of this run: plane(s) {} have "
                         "gaps beneath it that no declared "
                         "reference_discontinuity assumption covers. An "
                         "assumption names the plane, the size and the "
                         "reason; nothing here matched".format(
                             round(combined, 4), ", ".join(uncovered))}

    def _assumed_continuity(self, conductor, model, path_id=None):
        """The assumptions this run leaned on, so nothing absorbs them."""
        if not conductor.get("reference_checked"):
            return None
        per_plane, combined, unfilled, unknown = self._missing_on_used_planes(
            conductor, model)
        if unfilled or unknown:
            return None
        gapped = {plane: value for plane, value in per_plane.items()
                  if value > 0.0}
        if not gapped:
            return None
        used = []
        for plane, value in sorted(gapped.items()):
            assumption = self.discontinuity.covering(
                plane, conductor["layer"], path_id, value)
            if assumption is not None:
                used.append({"plane": plane,
                             "unreferenced_mm": round(value, 4),
                             **assumption.to_dict()})
        if not used:
            return None
        return {"assumption": "reference continuity",
                "layer": conductor["layer"],
                "combined_unreferenced_mm": (round(combined, 4)
                                             if combined is not None
                                             else None),
                "covered_gaps": used}

    def evaluate(self, resolved_path):
        """Total passive interconnect delay for one resolved path.

        The result is one of three things, and which one it is has to survive
        into the report intact:

          * a value - every portion was modelled and evaluated;
          * a lower bound - every portion was evaluable but at least one
            contributes an unknown positive amount, so the total is less than
            the truth by an unknown margin;
          * nothing - some portion could not be evaluated at all, either
            because the stackup does not support it or because the board asked
            for a model this release does not implement.

        `delay_ps` is None in the third case. Otherwise the truth is bracketed
        by `delay_lower_ps` and `delay_upper_ps`: the nominal total shifted
        down by the geometric length uncertainty, and up by that uncertainty
        plus every bounded omission. `delay_upper_ps` is None the moment one
        omission is unbounded, because an upper bound with an unbounded term
        in it is not an upper bound. Gates decide against the interval - a
        FAIL needs the lower endpoint over the limit, a PASS needs the upper
        one within it, and anything between is undecidable rather than met.
        """
        record = {
            "path": resolved_path.id,
            "source": resolved_path.source.to_dict(),
            "destination": resolved_path.destination.to_dict(),
            "copper_length_mm": round(resolved_path.copper_length_mm, 6),
            "length_by_layer_mm": resolved_path.length_by_layer_mm(),
            "component_traversals": [],
            "conductors": [], "vias": [],
            "insufficient": [],
            "physical_stackup_source": self.stackup.source,
            "propagation_model": self.model,
            "via_delay_model": self.via_model,
            "via_policy": self.via_treatment.to_dict(),
            "reference_discontinuity": self.discontinuity.to_dict(),
            "backend": self.backend,
            # Summed across steps because each step's figure is already a sum
            # over the distinct junctions that step crossed, and no junction
            # can be crossed by two steps of one path: steps are on different
            # nets. See `_walk_record` for what the number bounds.
            "length_uncertainty_mm": round(
                sum(s.record.get("length_uncertainty_mm") or 0.0
                    for s in getattr(resolved_path, "steps", ())), 6),
            "ambiguous_junctions": sum(
                s.record.get("ambiguous_junctions") or 0
                for s in getattr(resolved_path, "steps", ())),
        }
        total = 0.0
        fidelities = set()
        exact = True
        # What the omissions could add up to. `None` the moment one omission
        # is unbounded, because an upper bound with an unbounded term in it is
        # not an upper bound.
        omitted = 0.0
        omissions_bounded = True
        worst_ps_per_mm = 0.0

        for conductor in resolved_path.conductors():
            try:
                model = self.conductor(conductor["layer"],
                                       conductor["width_mm"])
            except PropagationError as exc:
                record["insufficient"].append({
                    "portion": "conductor",
                    "layer": conductor["layer"],
                    "width_mm": conductor["width_mm"],
                    "length_mm": conductor["length_mm"],
                    "issue": str(exc)})
                continue
            problem = self._reference_problem(conductor, model,
                                              path_id=resolved_path.id)
            if problem is not None:
                record["insufficient"].append(problem)
                continue
            assumed = self._assumed_continuity(conductor, model,
                                               path_id=resolved_path.id)
            if assumed:
                record.setdefault("assumptions", []).append(assumed)
                # A value that stands on a declared assumption must never read
                # as one whose geometry was physically established.
                fidelities.add(ASSUMED_TRANSMISSION_LINE)
            delay = conductor["length_mm"] * model["ps_per_mm"]
            total += delay
            fidelities.add(model["fidelity"])
            worst_ps_per_mm = max(worst_ps_per_mm, model["ps_per_mm"])
            record["conductors"].append({**conductor, **model,
                                         "delay_ps": round(delay, 6)})

        for transition in resolved_path.via_transitions():
            try:
                via = self.via(transition)
            except PropagationError as exc:
                record["insufficient"].append({
                    "portion": "via",
                    "via_between": [transition.get("from_layer"),
                                    transition.get("to_layer")],
                    "issue": str(exc)})
                continue
            total += via.get("delay_ps") or 0.0
            if via.get("fidelity"):
                fidelities.add(via["fidelity"])
            if via.get("exact") is False:
                exact = False
                bound = self.via_treatment.omitted_bound_ps
                if bound is None:
                    omissions_bounded = False
                else:
                    omitted += bound
            if via.get("unmodelled_stub"):
                record["unmodelled_via_stubs"] = record.get(
                    "unmodelled_via_stubs", 0) + 1
            record["vias"].append(via)

        # Components. A traversal is never silently worth zero: an unmodelled
        # one makes the total a lower bound, and one whose declared model this
        # release cannot evaluate makes the total impossible.
        for traversal in resolved_path.component_traversals():
            contribution = traversal.get("contribution")
            if contribution is None:                      # pragma: no cover
                raise PropagationError(
                    "component traversal of {} carries no evaluated "
                    "contribution".format(traversal.get("reference")))
            entry = {"reference": traversal["reference"],
                     "from_net": traversal["from_net"],
                     "to_net": traversal["to_net"],
                     **contribution.to_dict()}
            record["component_traversals"].append(entry)
            if not contribution.evaluable:
                record["insufficient"].append({
                    "portion": "component",
                    "reference": traversal["reference"],
                    "issue": contribution.reason})
                continue
            total += contribution.delay_ps or 0.0
            fidelities.add(contribution.fidelity)
            if not contribution.exact:
                exact = False
                bound = contribution.omitted_bound_ps
                if bound is None:
                    omissions_bounded = False
                else:
                    omitted += bound

        unmodelled = [t["reference"] for t in record["component_traversals"]
                      if t["model_status"] == "unmodelled"]
        if unmodelled:
            record["unmodelled_component_delay"] = unmodelled

        if record.get("assumptions"):
            # One declaration bounds each run; nothing bounds how many runs
            # a path crosses. The accumulated length the whole path assumed
            # over is therefore computed here and surfaced, so a reviewer
            # reads one number instead of summing a nested report by hand.
            record["assumed_unreferenced_total_mm"] = round(sum(
                gap["unreferenced_mm"]
                for use in record["assumptions"]
                for gap in use["covered_gaps"]), 4)

        if record["insufficient"]:
            record["delay_ps"] = None
            record["delay_lower_ps"] = None
            record["delay_upper_ps"] = None
            record["omitted_bound_ps"] = None
            record["geometric_uncertainty_ps"] = None
            record["delay_is_lower_bound"] = False
            record["fidelity"] = GEOMETRY_ONLY
            return record

        if not record["conductors"] and not record["vias"]:
            # No copper was modelled, so a total of zero would be a number
            # standing where a measurement never happened.
            record["delay_ps"] = None
            record["delay_lower_ps"] = None
            record["delay_upper_ps"] = None
            record["omitted_bound_ps"] = None
            record["geometric_uncertainty_ps"] = None
            record["delay_is_lower_bound"] = False
            record["fidelity"] = GEOMETRY_ONLY
            record["insufficient"].append({
                "portion": "path",
                "issue": "no conductor or via contributed a delay, so there is "
                         "no propagation result to report"})
            return record

        # The geometric length uncertainty, converted at the fastest velocity
        # any of this path's conductors uses. That is the worst case for the
        # shift - not false precision, because the constant is the same one
        # the delay itself was computed with, applied to the same millimetres.
        u_ps = round(record["length_uncertainty_mm"] * worst_ps_per_mm, 6)
        record["geometric_uncertainty_ps"] = u_ps
        record["delay_ps"] = round(total, 6)
        # The interval bracketing the truth. Lower: the copper could be up to
        # the geometric uncertainty shorter than the nominal walk. Upper: up
        # to that much longer, plus everything omitted, when every omission
        # was bounded. Equal to each other exactly when nothing was omitted
        # and no junction was ambiguous, which is what keeps a clean path
        # decidable in the ordinary way without a special case.
        record["delay_lower_ps"] = round(max(0.0, total - u_ps), 6)
        record["delay_is_lower_bound"] = not exact
        record["delay_upper_ps"] = (
            round(total + omitted + u_ps, 6)
            if (exact or omissions_bounded) else None)
        record["omitted_bound_ps"] = (round(omitted, 6)
                                      if omissions_bounded else None)
        record["fidelity"] = weakest(fidelities)
        return record


