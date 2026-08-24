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
    ANALYTIC_TRANSMISSION_LINE: 2,
    DECLARED_PROPAGATION: 3,
    DECLARED_MODEL: 3,
    QUASI_STATIC_EXTRACTED: 4,
    FULL_WAVE_EXTRACTED: 5,
    DEVICE_AWARE: 6,
}

FIDELITY_ORDER = tuple(sorted(FIDELITY_RANK, key=lambda f: (FIDELITY_RANK[f],
                                                            f)))


def fidelity_rank(name):
    """Where a fidelity sits. Anything unrecognised sits below everything."""
    return FIDELITY_RANK.get(name, -1)


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
                    negligible - so this is *also* a lower bound, and saying
                    otherwise would turn "not modelled yet" into an asserted
                    exact zero. That was the bug.
    ``none`` + justification
                    the board has taken responsibility for the omission on a
                    stated reason. Exact.
    ``geometric``   first-order: the barrel as a length of line in the
                    surrounding dielectric. Exact given the data, and refused
                    outright when the stackup lacks it.
    """

    __slots__ = ("model", "justification", "declared")

    def __init__(self, model, justification=None, declared=True):
        self.model = model
        self.justification = justification
        self.declared = declared

    @property
    def exact(self):
        """Does this contribute a value rather than an acknowledged omission?"""
        if self.model == VIA_GEOMETRIC:
            return True
        return bool(self.justification)

    @property
    def fidelity(self):
        if self.model == VIA_GEOMETRIC:
            return ANALYTIC_TRANSMISSION_LINE
        return DECLARED_MODEL if self.justification else UNKNOWN_CONTRIBUTION

    def note(self):
        if self.model == VIA_GEOMETRIC:
            return ("first-order: barrel treated as a length of line in the "
                    "surrounding dielectric; no inductance, no stub, no "
                    "discontinuity")
        if self.justification:
            return ("vertical extent measured, no delay attributed, on a "
                    "declared justification: {}".format(self.justification))
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
                **({"justification": self.justification}
                   if self.justification else {})}


def via_policy(declaration):
    """Read a board's `via_delay_model` declaration. Absent is a state too."""
    if declaration is None:
        return ViaPolicy(VIA_NONE, None, declared=False)
    if isinstance(declaration, str):
        model, justification = declaration, None
    elif isinstance(declaration, dict):
        model = declaration.get("model")
        justification = declaration.get("justification")
        if not model:
            raise PropagationError(
                "the via delay model declares no `model`")
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
    return ViaPolicy(model, justification, declared=True)


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
#: least one contributes an unknown positive amount. A backend that cannot
#: honour that distinction cannot be plugged in here, which is deliberate:
#: it is the distinction the gates make their decisions on.
PATH_RESULT_FIELDS = ("path", "source", "destination", "delay_ps",
                      "delay_is_lower_bound", "fidelity", "insufficient",
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
                 max_unreferenced_mm=0.0):
        if model not in MODELS:
            raise PropagationError(
                "propagation model {!r} is not implemented; this validator has "
                "{}".format(model, ", ".join(MODELS)))
        self.via_treatment = via_policy(via_model)
        self.stackup = stackup
        self.reference_layers = set(reference_layers or ())
        self.model = model
        self.via_model = self.via_treatment.model
        #: How much copper a board accepts having no reference conductor under
        #: it. Zero unless the board declares otherwise, because a route with
        #: no return path underneath is not the geometry any of these formulas
        #: describe.
        self.max_unreferenced_mm = max_unreferenced_mm
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
        record["fidelity"] = self.via_treatment.fidelity
        record["exact"] = True
        record["delay_ps"] = round(
            span["length_mm"] * math.sqrt(span["epsilon_r"]) / C_MM_PER_PS, 6)
        record["note"] = self.via_treatment.note()
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

        `delay_ps` is None in the third case. In the second it is a number and
        `delay_is_lower_bound` is True, which the gates honour: a lower bound
        can prove a maximum is exceeded and can never prove one is met.
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
            "backend": self.backend,
            "length_ambiguity_mm": round(
                sum(s.record.get("length_ambiguity_mm") or 0.0
                    for s in getattr(resolved_path, "steps", ())), 6),
        }
        total = 0.0
        fidelities = set()
        exact = True

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
            unreferenced = conductor.get("unreferenced_mm") or 0.0
            if unreferenced > self.max_unreferenced_mm:
                # Every closed form here assumes a reference conductor under
                # the whole run. Copper crossing a split, a void, or the edge
                # of the pour has none there, and the formula does not describe
                # it - so the answer is no answer, not the nearest-looking one.
                record["insufficient"].append({
                    "portion": "conductor",
                    "layer": conductor["layer"],
                    "width_mm": conductor["width_mm"],
                    "length_mm": conductor["length_mm"],
                    "unreferenced_mm": round(unreferenced, 4),
                    "issue": "{} mm of this run has no poured reference "
                             "conductor beneath it, so the transmission-line "
                             "geometry the model assumes does not exist there"
                             .format(round(unreferenced, 4))})
                continue
            delay = conductor["length_mm"] * model["ps_per_mm"]
            total += delay
            fidelities.add(model["fidelity"])
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

        unmodelled = [t["reference"] for t in record["component_traversals"]
                      if t["model_status"] == "unmodelled"]
        if unmodelled:
            record["unmodelled_component_delay"] = unmodelled

        if record["insufficient"]:
            record["delay_ps"] = None
            record["delay_is_lower_bound"] = False
            record["fidelity"] = GEOMETRY_ONLY
            return record

        if not record["conductors"] and not record["vias"]:
            # No copper was modelled, so a total of zero would be a number
            # standing where a measurement never happened.
            record["delay_ps"] = None
            record["delay_is_lower_bound"] = False
            record["fidelity"] = GEOMETRY_ONLY
            record["insufficient"].append({
                "portion": "path",
                "issue": "no conductor or via contributed a delay, so there is "
                         "no propagation result to report"})
            return record

        record["delay_ps"] = round(total, 6)
        record["delay_is_lower_bound"] = not exact
        record["fidelity"] = weakest(fidelities)
        return record


