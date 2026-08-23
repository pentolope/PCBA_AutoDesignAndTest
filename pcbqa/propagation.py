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
# result carries one of these and reports print it.
GEOMETRY_ONLY = "geometry-only"
ANALYTIC_TRANSMISSION_LINE = "analytic-transmission-line-estimate"
DECLARED_PROPAGATION = "declared-propagation-constant"
QUASI_STATIC_EXTRACTED = "quasi-static-extracted"      # reserved for a solver
FULL_WAVE_EXTRACTED = "full-wave-extracted"            # reserved for a solver
DEVICE_AWARE = "device-aware-timing"                   # reserved, needs models

FIDELITY_ORDER = (GEOMETRY_ONLY, ANALYTIC_TRANSMISSION_LINE,
                  DECLARED_PROPAGATION, QUASI_STATIC_EXTRACTED,
                  FULL_WAVE_EXTRACTED, DEVICE_AWARE)

# Via vertical-transit treatments.
VIA_NONE = "none"
VIA_GEOMETRIC = "geometric"
VIA_MODELS = (VIA_NONE, VIA_GEOMETRIC)


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


class PropagationModel:
    """Turns a resolved path plus a physical stackup into a delay.

    Constructed from the board's declared policy; it holds no defaults of its
    own, because every choice it makes - which closed form, whether vias
    contribute, what a layer's reference plane is - changes the answer and
    therefore belongs in configuration the board wrote down and provenance can
    hash.
    """

    def __init__(self, stackup, reference_layers, model=HAMMERSTAD,
                 via_model=VIA_NONE, declared_layers=None):
        if model not in MODELS:
            raise PropagationError(
                "propagation model {!r} is not implemented; this validator has "
                "{}".format(model, ", ".join(MODELS)))
        if via_model not in VIA_MODELS:
            raise PropagationError(
                "via delay model {!r} is not implemented; this validator has "
                "{}".format(via_model, ", ".join(VIA_MODELS)))
        self.stackup = stackup
        self.reference_layers = set(reference_layers or ())
        self.model = model
        self.via_model = via_model
        self.declared = {
            layer: DeclaredLayerModel(layer, spec)
            for layer, spec in (declared_layers or {}).items()}
        if model == DECLARED_EFFECTIVE and not self.declared:
            raise PropagationError(
                "the declared-effective model was selected but no layer "
                "declares a propagation constant")
        self._cache = {}

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
            "model": self.via_model,
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
        if self.via_model == VIA_NONE:
            record["delay_ps"] = 0.0
            record["note"] = ("vertical extent measured, no delay attributed: "
                              "this board declares no via delay model")
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
        record["delay_ps"] = round(
            span["length_mm"] * math.sqrt(span["epsilon_r"]) / C_MM_PER_PS, 6)
        record["note"] = ("first-order: barrel treated as a length of line in "
                          "the surrounding dielectric; no inductance, no stub, "
                          "no discontinuity")
        return record

    def _vertical_mm(self, from_layer, to_layer):
        if not from_layer or not to_layer or from_layer == to_layer:
            return None
        names = [l.name for l in self.stackup.layers]
        if from_layer not in names or to_layer not in names:
            return None
        low, high = sorted((names.index(from_layer), names.index(to_layer)))
        crossed, epsilons = [], []
        total, known = 0.0, True
        for entry in self.stackup.layers[low + 1:high]:
            crossed.append(entry.name)
            if entry.thickness_mm is None:
                known = False
            else:
                total += entry.thickness_mm
            if entry.is_dielectric and entry.epsilon_r is not None:
                epsilons.append(entry.epsilon_r)
        unique = set(epsilons)
        return {"length_mm": total if known else None,
                "crossed": crossed,
                "epsilon_r": unique.pop() if len(unique) == 1 else None}

    # -- whole paths -------------------------------------------------------
    def evaluate(self, resolved_path):
        """Total passive interconnect delay for one resolved path.

        Returns a record whose `delay_ps` is None when the delay could not be
        derived, with `insufficient` saying exactly what was missing. It never
        substitutes a value; a caller that needs a number and has none has a
        finding, not a default.
        """
        record = {
            "path": resolved_path.id,
            "source": resolved_path.source.to_dict(),
            "destination": resolved_path.destination.to_dict(),
            "copper_length_mm": round(resolved_path.copper_length_mm, 6),
            "length_by_layer_mm": resolved_path.length_by_layer_mm(),
            "component_traversals": [
                {"reference": t["reference"], "from_net": t["from_net"],
                 "to_net": t["to_net"],
                 "delay_model": t.get("declared_delay_model"),
                 "delay_ps": 0.0}
                for t in resolved_path.component_traversals()],
            "conductors": [], "vias": [],
            "insufficient": [],
            "physical_stackup_source": self.stackup.source,
            "propagation_model": self.model,
            "via_delay_model": self.via_model,
        }
        total = 0.0
        fidelities = set()
        for conductor in resolved_path.conductors():
            try:
                model = self.conductor(conductor["layer"],
                                       conductor["width_mm"])
            except PropagationError as exc:
                record["insufficient"].append({
                    "layer": conductor["layer"],
                    "width_mm": conductor["width_mm"],
                    "length_mm": conductor["length_mm"],
                    "issue": str(exc)})
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
                    "via_between": [transition.get("from_layer"),
                                    transition.get("to_layer")],
                    "issue": str(exc)})
                continue
            total += via.get("delay_ps") or 0.0
            record["vias"].append(via)

        # A component contributes nothing unless a model says otherwise, and
        # no model is implemented yet. Recorded rather than assumed away: the
        # zero is a statement that nothing was modelled, and a reader can see
        # which parts it applies to.
        unmodelled = [t["reference"] for t in record["component_traversals"]
                      if not t["delay_model"]]
        if unmodelled:
            record["unmodelled_component_delay"] = unmodelled

        if record["insufficient"]:
            record["delay_ps"] = None
            record["fidelity"] = GEOMETRY_ONLY
        else:
            record["delay_ps"] = round(total, 6)
            record["fidelity"] = _lowest(fidelities)
        return record


def _lowest(fidelities):
    """The weakest fidelity in a set: a result is only as good as its worst part."""
    if not fidelities:
        return GEOMETRY_ONLY
    return min(fidelities, key=lambda f: FIDELITY_ORDER.index(f)
               if f in FIDELITY_ORDER else 0)
