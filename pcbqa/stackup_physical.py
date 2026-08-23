"""The physical (electromagnetic) stackup, as distinct from the structural one.

Two different questions get called "the stackup".

The structural one - how many copper layers, in what order, which of them are
planes and on which net - is what `STACK.NATIVE_VS_MANIFEST` checks, and it is
answerable from the board alone. Nothing here changes it.

The physical one - how thick each copper layer and each dielectric is, what the
dielectric is made of, what its relative permittivity and loss tangent are - is
what a propagation model needs, and a board is under no obligation to record
it. KiCad stores it in the board file's own `(setup (stackup ...))` block when
the designer has filled the stackup in, and stores nothing at all when they
have not.

So this module answers with what is actually there, in a preferred order:

  1. the board's native KiCad physical stackup;
  2. a board-owned supplemental declaration, for what the native data omit;
  3. otherwise: incomplete, named field by field.

There is deliberately no fourth case. A generic FR-4 assumed on the board's
behalf would be indistinguishable in the report from a stackup the fabricator
actually confirmed, and every delay derived from it would inherit that
confusion. An incomplete stackup is a fact about the board, and is reported as
one.

Read from the board *file* rather than through `pcbnew`: KiCad 10's Python
bindings return the stackup descriptor as an opaque SWIG pointer with no
accessors, so the file's own S-expression is the only place the native data can
be read from. It is still the native KiCad data - the same bytes KiCad wrote.
"""

from __future__ import annotations

from . import sexpr

# Where a physical stackup came from. These strings go into reports, so a
# reader can tell a fabricator-confirmed stackup from one a board wrote down.
NATIVE = "native_kicad_board"
DECLARED = "board_declaration"
MERGED = "native_kicad_board+board_declaration"

COPPER = "copper"
DIELECTRIC = "dielectric"
OTHER = "other"

# KiCad's own layer type strings, mapped to what a field solver cares about.
_DIELECTRIC_TYPES = ("core", "prepreg")


class StackupError(Exception):
    """The physical stackup cannot be used as asked. Always blocks."""


class StackupLayer:
    """One layer of the physical stack, in top-to-bottom order."""

    __slots__ = ("name", "kind", "type_name", "thickness_mm", "material",
                 "epsilon_r", "loss_tangent", "sublayers", "source")

    def __init__(self, name, kind, type_name=None, thickness_mm=None,
                 material=None, epsilon_r=None, loss_tangent=None,
                 sublayers=None, source=None):
        self.name = name
        self.kind = kind
        self.type_name = type_name
        self.thickness_mm = thickness_mm
        self.material = material
        self.epsilon_r = epsilon_r
        self.loss_tangent = loss_tangent
        self.sublayers = list(sublayers or [])
        self.source = source

    @property
    def is_copper(self):
        return self.kind == COPPER

    @property
    def is_dielectric(self):
        return self.kind == DIELECTRIC

    @property
    def uniform(self):
        """Is this one dielectric, or several with different permittivities?

        KiCad lets one dielectric layer carry sub-layers. Summing their
        thicknesses and picking one permittivity would be a model, not a
        reading, so a non-uniform layer says so and the analytic backend
        refuses it.
        """
        if len(self.sublayers) < 2:
            return True
        values = {s.get("epsilon_r") for s in self.sublayers}
        return len(values) == 1 and None not in values

    def missing(self, fields):
        return [f for f in fields if getattr(self, f, None) is None]

    def to_dict(self):
        record = {"name": self.name, "kind": self.kind,
                  "type": self.type_name,
                  "thickness_mm": self.thickness_mm,
                  "source": self.source}
        if self.is_dielectric:
            record.update({"material": self.material,
                           "epsilon_r": self.epsilon_r,
                           "loss_tangent": self.loss_tangent,
                           "uniform": self.uniform})
            if len(self.sublayers) > 1:
                record["sublayers"] = self.sublayers
        return record

    def __repr__(self):
        return "<StackupLayer {} {} {}mm>".format(self.name, self.kind,
                                                  self.thickness_mm)


class ReferenceGeometry:
    """What a signal layer sees: its reference plane(s) and the dielectric between.

    `mode` is the transmission-line geometry the layer is in, which is what
    decides whether a closed-form model applies at all:

        microstrip            an OUTER layer with one reference plane, so the
                              other side of the trace really is air
        embedded_microstrip   an INNER layer with one reference plane: the
                              other side is dielectric, not air, so the
                              microstrip formulas do not apply to it
        stripline             two reference planes, equal spacing
        asymmetric_stripline  two reference planes, different spacings
        none                  no reference plane could be identified

    The microstrip/embedded distinction is not cosmetic. Hammerstad's
    approximation exists because part of a microstrip's field is in air, and
    it returns an effective permittivity below the laminate's. Applying it to
    a buried trace, whose field is entirely in dielectric, understates the
    permittivity and therefore the delay - by a wide margin, and silently.

    Whether a mode is *supported* is the propagation backend's decision, not
    this module's; this reports the geometry it found.
    """

    __slots__ = ("layer", "mode", "height_mm", "height_below_mm",
                 "reference_above", "reference_below", "epsilon_r",
                 "loss_tangent", "material", "copper_thickness_mm", "problems")

    def __init__(self, layer, mode, height_mm=None, height_below_mm=None,
                 reference_above=None, reference_below=None, epsilon_r=None,
                 loss_tangent=None, material=None, copper_thickness_mm=None,
                 problems=None):
        self.layer = layer
        self.mode = mode
        self.height_mm = height_mm
        self.height_below_mm = height_below_mm
        self.reference_above = reference_above
        self.reference_below = reference_below
        self.epsilon_r = epsilon_r
        self.loss_tangent = loss_tangent
        self.material = material
        self.copper_thickness_mm = copper_thickness_mm
        self.problems = list(problems or [])

    @property
    def complete(self):
        return not self.problems

    def to_dict(self):
        return {"layer": self.layer, "mode": self.mode,
                "dielectric_height_mm": self.height_mm,
                "dielectric_height_below_mm": self.height_below_mm,
                "reference_above": self.reference_above,
                "reference_below": self.reference_below,
                "epsilon_r": self.epsilon_r,
                "loss_tangent": self.loss_tangent,
                "material": self.material,
                "copper_thickness_mm": self.copper_thickness_mm,
                "insufficient": self.problems}


class PhysicalStackup:
    """An ordered physical stack, and what can be asked of it."""

    def __init__(self, layers, source, declared_total_thickness_mm=None,
                 notes=None):
        self.layers = list(layers)
        self.source = source
        self.declared_total_thickness_mm = declared_total_thickness_mm
        self.notes = list(notes or [])

    # -- shape -------------------------------------------------------------
    @property
    def copper_layers(self):
        return [l for l in self.layers if l.is_copper]

    @property
    def copper_layer_names(self):
        return [l.name for l in self.copper_layers]

    def layer(self, name):
        for entry in self.layers:
            if entry.name == name:
                return entry
        return None

    def summed_thickness_mm(self):
        values = [l.thickness_mm for l in self.layers
                  if l.thickness_mm is not None]
        return round(sum(values), 6) if values else None

    @property
    def empty(self):
        return not self.layers

    # -- what is missing ---------------------------------------------------
    def completeness(self):
        """Every field a propagation model would need and does not have."""
        problems = []
        if self.empty:
            problems.append({
                "issue": "no physical stackup is available at all",
                "detail": "the board file carries no (stackup ...) block and "
                          "no supplemental declaration was provided"})
            return problems
        for entry in self.layers:
            if entry.is_copper and entry.thickness_mm is None:
                problems.append({
                    "layer": entry.name,
                    "issue": "copper thickness is not stated",
                    "needed_for": "the thickness-corrected microstrip model "
                                  "and for via geometry, not for the "
                                  "zero-thickness model"})
            if not entry.is_dielectric:
                continue
            for field, label, needed in (
                    ("thickness_mm", "thickness", "delay"),
                    ("epsilon_r", "relative permittivity", "delay"),
                    ("loss_tangent", "loss tangent", "loss, not delay")):
                if getattr(entry, field) is None:
                    problems.append({"layer": entry.name,
                                     "issue": "{} is not stated".format(label),
                                     "needed_for": needed})
            if not entry.uniform:
                problems.append({
                    "layer": entry.name,
                    "issue": "is built from sub-layers with different "
                             "permittivities, which no single-dielectric "
                             "model represents"})
        return problems

    # -- geometry ----------------------------------------------------------
    def reference_geometry(self, signal_layer, reference_layers):
        """The transmission-line geometry a signal layer is in.

        `reference_layers` names the copper layers that are reference planes.
        Deciding that is a board's business - a layer is a plane because it is
        poured on a reference net, and this module is not the place that knows
        which nets those are - so it is passed in.
        """
        names = [l.name for l in self.layers]
        if signal_layer not in names:
            return ReferenceGeometry(
                signal_layer, None,
                problems=[{"issue": "the physical stackup describes no layer "
                                    "by this name",
                           "known_layers": names}])
        index = names.index(signal_layer)
        above, above_gap = self._search(index, -1, reference_layers)
        below, below_gap = self._search(index, +1, reference_layers)
        entry = self.layers[index]

        if above is None and below is None:
            return ReferenceGeometry(
                signal_layer, None,
                copper_thickness_mm=entry.thickness_mm,
                problems=[{"issue": "no reference plane can be identified "
                                    "either side of this layer",
                           "reference_layers": sorted(reference_layers)}])

        if above is not None and below is not None:
            mode = "stripline"
            gaps = [above_gap, below_gap]
            if (above_gap["thickness_mm"] is not None
                    and below_gap["thickness_mm"] is not None
                    and abs(above_gap["thickness_mm"]
                            - below_gap["thickness_mm"]) > 1e-9):
                mode = "asymmetric_stripline"
        else:
            # One reference plane. Whether that makes this a microstrip
            # depends on what is on the other side, and the only thing that
            # puts air there is being an outer copper layer.
            outer = self.copper_layer_names[:1] + self.copper_layer_names[-1:]
            mode = "microstrip" if signal_layer in outer else "embedded_microstrip"
            gaps = [above_gap if above is not None else below_gap]

        # `problems` is what stops a PROPAGATION DELAY from being derived, and
        # is deliberately narrower than "the stackup is incomplete", which is
        # `completeness()`'s question. Delay needs the dielectric height and
        # its permittivity. It does not need the loss tangent, which sets
        # attenuation rather than velocity, and it does not need the copper
        # thickness unless a thickness-corrected model was selected - and that
        # model refuses on its own when it is missing. Requiring either here
        # would block a perfectly derivable delay on a figure it never uses.
        problems = []
        epsilon = _one_value(gaps, "epsilon_r", problems,
                             "relative permittivity")
        loss = _one_value(gaps, "loss_tangent", [], "loss tangent")
        for gap in gaps:
            if gap["thickness_mm"] is None:
                problems.append({
                    "issue": "the dielectric between this layer and its "
                             "reference plane states no thickness",
                    "dielectrics": gap["layers"]})
            if not gap["uniform"]:
                problems.append({
                    "issue": "the dielectric between this layer and its "
                             "reference plane is not one uniform material",
                    "dielectrics": gap["layers"]})

        nearest = gaps[0]
        return ReferenceGeometry(
            signal_layer, mode,
            height_mm=nearest["thickness_mm"],
            height_below_mm=(below_gap["thickness_mm"]
                             if (above is not None and below is not None)
                             else None),
            reference_above=above, reference_below=below,
            epsilon_r=epsilon, loss_tangent=loss,
            material=nearest["material"],
            copper_thickness_mm=entry.thickness_mm,
            problems=problems)

    def _search(self, index, direction, reference_layers):
        """Walk away from a layer until a reference plane is met."""
        thickness = 0.0
        known = True
        uniform = True
        materials, epsilons, losses, crossed = [], [], [], []
        position = index + direction
        while 0 <= position < len(self.layers):
            entry = self.layers[position]
            if entry.is_copper:
                if entry.name in reference_layers:
                    return entry.name, {
                        "thickness_mm": (round(thickness, 6) if known
                                         else None),
                        "layers": crossed,
                        "material": materials[0] if materials else None,
                        "epsilon_r": _single(epsilons),
                        "loss_tangent": _single(losses),
                        "uniform": uniform and len(set(epsilons)) <= 1,
                    }
                # A signal layer in the way means the plane behind it is not
                # this layer's reference; stop rather than reach past it.
                return None, _empty_gap()
            if entry.is_dielectric:
                crossed.append(entry.name)
                if entry.thickness_mm is None:
                    known = False
                else:
                    thickness += entry.thickness_mm
                if entry.epsilon_r is not None:
                    epsilons.append(entry.epsilon_r)
                if entry.loss_tangent is not None:
                    losses.append(entry.loss_tangent)
                if entry.material:
                    materials.append(entry.material)
                uniform = uniform and entry.uniform
            position += direction
        return None, _empty_gap()

    # -- reporting ---------------------------------------------------------
    def to_dict(self):
        return {
            "source": self.source,
            "layers": [l.to_dict() for l in self.layers],
            "copper_layers": self.copper_layer_names,
            "summed_thickness_mm": self.summed_thickness_mm(),
            "declared_total_thickness_mm": self.declared_total_thickness_mm,
            "notes": self.notes,
        }


def _empty_gap():
    return {"thickness_mm": None, "layers": [], "material": None,
            "epsilon_r": None, "loss_tangent": None, "uniform": True}


def _single(values):
    unique = {v for v in values if v is not None}
    return unique.pop() if len(unique) == 1 else None


def _one_value(gaps, field, problems, label):
    values = {g[field] for g in gaps}
    if None in values:
        problems.append({"issue": "the dielectric around this layer states no "
                                  "{}".format(label)})
        values.discard(None)
    if len(values) > 1:
        problems.append({
            "issue": "the dielectrics above and below this layer disagree "
                     "about {}, which no single-dielectric model "
                     "represents".format(label),
            "values": sorted(values)})
        return None
    return values.pop() if values else None


# ---------------------------------------------------------------------------
# native KiCad extraction
# ---------------------------------------------------------------------------

def from_board_file(path):
    """The board's own `(setup (stackup ...))`, or an empty stackup.

    An absent block is not an error here. Plenty of perfectly good boards never
    have the stackup filled in, and the caller - not the reader - decides
    whether that blocks.
    """
    with open(path, encoding="utf-8", errors="ignore") as handle:
        text = handle.read()
    block = _stackup_text(text)
    if block is None:
        return PhysicalStackup(
            [], NATIVE,
            declared_total_thickness_mm=_general_thickness(text),
            notes=["the board file carries no (setup (stackup ...)) block, so "
                   "KiCad holds no physical stackup for this design"])
    document = sexpr.parse(block)
    root = document[0]
    layers = [_layer_from_sexpr(node) for node in sexpr.children(root, "layer")]
    notes = []
    finish = sexpr.first(root, "copper_finish")
    if finish and len(finish) > 1:
        notes.append("copper finish: {}".format(finish[1]))
    return PhysicalStackup(layers, NATIVE,
                           declared_total_thickness_mm=_general_thickness(text),
                           notes=notes)


def _stackup_text(text):
    """The balanced `(stackup ...)` substring, or None."""
    start = text.find("(stackup")
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise StackupError(
        "the board file's (stackup ...) block is not balanced; refusing to "
        "read a physical stackup out of a truncated board")


def _general_thickness(text):
    """`(general (thickness x))`: overall board thickness, which KiCad keeps
    whether or not the stackup itself was ever filled in."""
    marker = text.find("(general")
    if marker < 0:
        return None
    window = text[marker:marker + 400]
    node = window.find("(thickness")
    if node < 0:
        return None
    end = window.find(")", node)
    try:
        return float(window[node + len("(thickness"):end].strip())
    except ValueError:
        return None


def _layer_from_sexpr(node):
    name = node[1] if len(node) > 1 and isinstance(node[1], str) else "?"
    type_name = _token(node, "type")
    thicknesses = [_number(child) for child in sexpr.children(node, "thickness")]
    materials = [child[1] if len(child) > 1 else None
                 for child in sexpr.children(node, "material")]
    epsilons = [_number(child) for child in sexpr.children(node, "epsilon_r")]
    losses = [_number(child) for child in sexpr.children(node, "loss_tangent")]

    kind = OTHER
    if type_name == COPPER:
        kind = COPPER
    elif type_name in _DIELECTRIC_TYPES:
        kind = DIELECTRIC

    sublayers = []
    if kind == DIELECTRIC and len(thicknesses) > 1:
        for index in range(len(thicknesses)):
            sublayers.append({
                "thickness_mm": thicknesses[index],
                "material": materials[index] if index < len(materials) else None,
                "epsilon_r": epsilons[index] if index < len(epsilons) else None,
                "loss_tangent": losses[index] if index < len(losses) else None,
            })
    total = None
    if thicknesses and all(t is not None for t in thicknesses):
        total = round(sum(thicknesses), 6)
    return StackupLayer(
        name, kind, type_name=type_name, thickness_mm=total,
        material=materials[0] if materials else None,
        epsilon_r=(epsilons[0] if len(set(epsilons)) == 1 and epsilons
                   else (epsilons[0] if len(epsilons) == 1 else None)),
        loss_tangent=(losses[0] if len(set(losses)) == 1 and losses
                      else (losses[0] if len(losses) == 1 else None)),
        sublayers=sublayers, source=NATIVE)


def _token(node, tag):
    child = sexpr.first(node, tag)
    return child[1] if child is not None and len(child) > 1 else None


def _number(child):
    try:
        return float(child[1])
    except (IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# board-owned supplemental declaration
# ---------------------------------------------------------------------------

REQUIRED_DECLARATION_FIELDS = ("layers",)


def from_declaration(document, source=DECLARED):
    """A board's own physical stackup, in the same shape KiCad's is read into.

    Every layer needs a `name` and a `kind`; everything else is optional and a
    field that is absent stays absent. A declaration is allowed to be
    incomplete - `null` is a legitimate value meaning "this board does not know
    this yet" - and it is `completeness()` and the propagation backend, not the
    loader, that decide whether what is there is enough.
    """
    if not isinstance(document, dict):
        raise StackupError(
            "a physical stackup declaration must be a JSON object, got "
            "{}".format(type(document).__name__))
    for field in REQUIRED_DECLARATION_FIELDS:
        if field not in document:
            raise StackupError(
                "a physical stackup declaration must state {!r}".format(field))
    layers = []
    for index, entry in enumerate(document["layers"]):
        if not isinstance(entry, dict):
            raise StackupError(
                "stackup layer {} is a {}, not an object".format(
                    index, type(entry).__name__))
        for field in ("name", "kind"):
            if not entry.get(field):
                raise StackupError(
                    "stackup layer {} states no {!r}".format(index, field))
        if entry["kind"] not in (COPPER, DIELECTRIC, OTHER):
            raise StackupError(
                "stackup layer {!r} declares kind {!r}; permitted kinds are "
                "{}".format(entry["name"], entry["kind"],
                            ", ".join((COPPER, DIELECTRIC, OTHER))))
        layers.append(StackupLayer(
            entry["name"], entry["kind"], type_name=entry.get("type"),
            thickness_mm=entry.get("thickness_mm"),
            material=entry.get("material"),
            epsilon_r=entry.get("epsilon_r"),
            loss_tangent=entry.get("loss_tangent"),
            sublayers=entry.get("sublayers"), source=source))
    return PhysicalStackup(
        layers, source,
        declared_total_thickness_mm=document.get("total_thickness_mm"),
        notes=document.get("notes") or [])


def merge(native, declared):
    """Native data, with a board's declaration filling only what is absent.

    The board file wins wherever it says anything: it is the design authority,
    and a supplemental file that could quietly override it would be a second
    one. The declaration supplies fields KiCad holds no value for, which on a
    board whose stackup was never filled in is all of them.

    A declaration that contradicts the native data is an error rather than a
    silent overwrite - that disagreement is a finding about the project, and
    resolving it by preferring one side would hide it.
    """
    if native is None or native.empty:
        # The board file holds no layer structure, so the declaration supplies
        # all of it. The label still has to say whether anything native was
        # used: overall board thickness is recorded by KiCad whether or not the
        # stackup was ever filled in, and if it came from there the result is
        # not purely a declaration.
        native_thickness = (native.declared_total_thickness_mm
                            if native is not None else None)
        return PhysicalStackup(
            declared.layers,
            MERGED if native_thickness is not None else DECLARED,
            declared_total_thickness_mm=(
                native_thickness or declared.declared_total_thickness_mm),
            notes=(list(native.notes) if native else []) + list(declared.notes))
    by_name = {l.name: l for l in declared.layers}
    conflicts = []
    layers = []
    for entry in native.layers:
        supplement = by_name.get(entry.name)
        merged = StackupLayer(
            entry.name, entry.kind, entry.type_name, entry.thickness_mm,
            entry.material, entry.epsilon_r, entry.loss_tangent,
            entry.sublayers, NATIVE)
        if supplement is not None:
            for field in ("thickness_mm", "material", "epsilon_r",
                          "loss_tangent"):
                offered = getattr(supplement, field)
                if offered is None:
                    continue
                held = getattr(merged, field)
                if held is None:
                    setattr(merged, field, offered)
                    merged.source = MERGED
                elif held != offered:
                    conflicts.append({
                        "layer": entry.name, "field": field,
                        "native": held, "declared": offered})
        layers.append(merged)
    if conflicts:
        raise StackupError(
            "the board's supplemental stackup contradicts the board file's own "
            "stackup in {} place(s): {}. The board file is the design "
            "authority; a supplement may fill in what it does not say, and may "
            "not disagree with what it does".format(
                len(conflicts),
                "; ".join("{} {} native={} declared={}".format(
                    c["layer"], c["field"], c["native"], c["declared"])
                    for c in conflicts[:6])))
    extra = [name for name in by_name if native.layer(name) is None]
    notes = list(native.notes) + list(declared.notes)
    if extra:
        notes.append("supplemental declaration names layer(s) the board file "
                     "does not have: {}".format(", ".join(sorted(extra))))
    return PhysicalStackup(layers, MERGED,
                           declared_total_thickness_mm=(
                               native.declared_total_thickness_mm
                               or declared.declared_total_thickness_mm),
                           notes=notes)


# ---------------------------------------------------------------------------
# which copper layers are reference planes
# ---------------------------------------------------------------------------

def plane_layers(board, reference_nets):
    """Copper layers carrying a zone on one of the reference nets.

    Read from the board rather than declared, so a layer counts as a plane
    because copper is actually poured on it. Rule areas are skipped: a keep-out
    pours nothing and references nothing.
    """
    import pcbnew
    wanted = set(reference_nets)
    found = set()
    for zone in board.Zones():
        if zone.GetIsRuleArea():
            continue
        if zone.GetNetname() not in wanted:
            continue
        for layer in zone.GetLayerSet().CuStack():
            found.add(pcbnew.LayerName(layer))
    return found
