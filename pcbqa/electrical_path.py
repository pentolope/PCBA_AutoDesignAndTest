"""Electrical paths: what a signal traverses, across nets and through parts.

A KiCad net is not an electrical path. A signal leaving a driver and arriving
at a receiver commonly crosses a series component - a termination resistor, a
bead, a connector - and every crossing starts a new net. Measuring one net
therefore measures a fragment, and a length taken from the net on the far side
of a series resistor silently omits everything before it.

This module composes the fragments. The copper inside one net is measured by
`pcbqa.connectivity.NetGraph`, which is unchanged and remains the primitive:
connectivity is still decided by copper shapes actually intersecting, and a
length is still the shortest connected walk from one pad to another. What is
added here is the layer above it:

    ElectricalPath
        CopperSegment(net=A, from=driver pad, to=part pad 1)
        ComponentTraversal(ref=RC1, pad 1 -> pad 2)
        CopperSegment(net=B, from=part pad 2, to=receiver pad)

A traversal joins two net-local measurements into one logical path and carries
whatever the board can say about the part's own contribution. It contributes
zero delay unless a model is declared; a framework that guessed one would be
inventing the number the analysis exists to produce.

The step list is deliberately open. Today only copper and component steps are
implemented, but nothing in the representation assumes a step is one of those
two, so a connector, a cable, a package, an IBIS-derived delay or a Touchstone
network can be added later as further step kinds without the path, the gates
or a board's manifest changing shape.

Nothing here names a board, a net, a designator or a limit: every selector,
identifier and expansion comes from the manifest.
"""

from __future__ import annotations

import re

from . import component_models

#: How a copper step picks its start when the selector matches several pads.
#: `unique` is the default and refuses ambiguity: a declaration that quietly
#: measures from whichever pad happens to be nearest is not a declaration, it
#: is a coincidence that will change the day someone adds a driver.
SOURCE_UNIQUE = "unique"
SOURCE_SHORTEST = "shortest"
SOURCE_SELECTION = (SOURCE_UNIQUE, SOURCE_SHORTEST)

# Step kinds. Strings rather than an enum because they travel into JSON
# reports and into manifests, where a stable spelling is the interface.
COPPER = "copper"
COMPONENT = "component"

KNOWN_STEP_KINDS = (COPPER, COMPONENT)


class PathError(Exception):
    """A declared path cannot be built or resolved. Always blocks."""


# ---------------------------------------------------------------------------
# selectors and endpoints
# ---------------------------------------------------------------------------

class PadSelector:
    """How a manifest names one or more pads.

    Either an exact ``REF.PAD`` label or, when the declaration starts with a
    regex anchor, a pattern. Being explicit about which is which matters: a
    step that must land on one particular part pin has to fail when it would
    match two, and that is only checkable if "exactly one" was the intent.
    """

    def __init__(self, declared):
        if not isinstance(declared, str) or not declared:
            raise PathError(
                "a pad selector must be a non-empty string, got {!r}".format(
                    declared))
        self.declared = declared
        self.is_pattern = declared.startswith("^") or declared.endswith("$")
        self._re = re.compile(declared) if self.is_pattern else None

    def matches(self, label):
        if self._re is not None:
            return bool(self._re.match(label))
        return label == self.declared

    def select(self, labels):
        return sorted(l for l in labels if self.matches(l))

    def __repr__(self):
        return "<PadSelector {!r}>".format(self.declared)


class Endpoint:
    """One terminal of a path: a pad on a net, named the way reports name it."""

    __slots__ = ("label", "net", "role")

    def __init__(self, label, net, role):
        self.label = label
        self.net = net
        self.role = role

    def to_dict(self):
        return {"pad": self.label, "net": self.net, "role": self.role}

    def __repr__(self):
        return "<Endpoint {} on {}>".format(self.label, self.net)


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

class CopperSegment:
    """Copper inside one net, from one pad to another.

    Resolution is delegated to `NetGraph`, so this adds no connectivity rules
    of its own; it only decides which two pads to ask about and records what
    the walk went through.
    """

    kind = COPPER

    def __init__(self, net, source, target, index=0,
                 source_selection=SOURCE_UNIQUE):
        if source_selection not in SOURCE_SELECTION:
            raise PathError(
                "copper step {}: source_selection {!r} is not one of "
                "{}".format(index, source_selection,
                            ", ".join(SOURCE_SELECTION)))
        self.net = net
        self.source = PadSelector(source)
        self.target = PadSelector(target)
        self.index = index
        self.source_selection = source_selection
        if (not self.source.is_pattern and not self.target.is_pattern
                and self.source.declared == self.target.declared):
            raise PathError(
                "copper step {} runs from {} to itself, which measures "
                "nothing".format(index, self.source.declared))

    def describe(self):
        return {"kind": self.kind, "net": self.net,
                "from": self.source.declared, "to": self.target.declared,
                "source_selection": self.source_selection}

    def __repr__(self):
        return "<CopperSegment {} {}->{}>".format(
            self.net, self.source.declared, self.target.declared)


class ComponentTraversal:
    """Crossing a component from one of its pads to another.

    This is what makes a path an electrical path rather than a net
    measurement. It asserts, and checks, that the part really does bridge the
    two nets either side of it, and it contributes the part's own propagation
    delay - which is zero, and recorded as unmodelled, unless the board
    declares a model for it.
    """

    kind = COMPONENT

    def __init__(self, reference, from_pad, to_pad, delay_model=None, index=0,
                 assume_populated=None):
        self.reference = reference
        self.from_pad = str(from_pad)
        self.to_pad = str(to_pad)
        self.delay_model = delay_model
        self.index = index
        self.assume_populated = assume_populated
        if self.from_pad == self.to_pad:
            raise PathError(
                "component step {}: {} is crossed from pad {} to the same "
                "pad, which traverses nothing".format(
                    index, reference, self.from_pad))
        if assume_populated is not None:
            if not isinstance(assume_populated, dict) or not \
                    assume_populated.get("justification"):
                raise PathError(
                    "component step {}: assume_populated overrides the board's "
                    "own do-not-populate marking, so it requires a "
                    "`justification`".format(index))
        # Validated at declaration time so a malformed model is caught before
        # any board is opened, rather than once per resolved path. Re-raised
        # as a PathError because that is what the declaration layer refuses
        # with, and what lets one bad route be a finding against itself rather
        # than an error that blinds every other route in the interface.
        try:
            component_models.validate(delay_model, self.entry_label)
        except component_models.ComponentModelError as exc:
            raise PathError("component step {}: {}".format(index, exc)) from exc

    @property
    def entry_label(self):
        return "{}.{}".format(self.reference, self.from_pad)

    @property
    def exit_label(self):
        return "{}.{}".format(self.reference, self.to_pad)

    def describe(self):
        return {"kind": self.kind, "reference": self.reference,
                "from_pad": self.from_pad, "to_pad": self.to_pad,
                "delay_model": self.delay_model,
                **({"assume_populated": self.assume_populated}
                   if self.assume_populated else {})}

    def __repr__(self):
        return "<ComponentTraversal {} {}->{}>".format(
            self.reference, self.from_pad, self.to_pad)


def step_from_spec(spec, index):
    """Build one step from its manifest declaration. Unknown kinds refuse."""
    if not isinstance(spec, dict):
        raise PathError("step {} is a {}, not an object".format(
            index, type(spec).__name__))
    kind = spec.get("kind")
    if kind == COPPER:
        for field in ("net", "from", "to"):
            if field not in spec:
                raise PathError(
                    "copper step {} declares no {!r}".format(index, field))
        return CopperSegment(spec["net"], spec["from"], spec["to"], index,
                             spec.get("source_selection", SOURCE_UNIQUE))
    if kind == COMPONENT:
        for field in ("reference", "from_pad", "to_pad"):
            if field not in spec:
                raise PathError(
                    "component step {} declares no {!r}".format(index, field))
        return ComponentTraversal(spec["reference"], spec["from_pad"],
                                  spec["to_pad"], spec.get("delay_model"),
                                  index, spec.get("assume_populated"))
    raise PathError(
        "step {} declares kind {!r}; this validator implements {}. A step kind "
        "it does not implement is refused rather than skipped, because a "
        "skipped step is a length the result silently does not "
        "contain".format(index, kind, ", ".join(KNOWN_STEP_KINDS)))


# ---------------------------------------------------------------------------
# the path
# ---------------------------------------------------------------------------

class ElectricalPath:
    """A declared chain of steps, before it has been measured.

    One declaration can resolve to several concrete paths, because the final
    step may legitimately name a family of receivers - one branch feeding two
    loads is one route and two arrivals. Every earlier step must be
    unambiguous: an intermediate fan-out is not a path, it is two paths that
    were not declared.
    """

    def __init__(self, path_id, steps, attributes=None):
        if not steps:
            raise PathError("path {!r} declares no steps".format(path_id))
        self.id = path_id
        self.steps = list(steps)
        self.attributes = dict(attributes or {})
        self._check_shape()

    def _check_shape(self):
        """Everything about a declaration that is wrong before a board is opened.

        These are declaration errors, not board findings, so they are raised
        where the declaration is read. A path that cannot be a path on any
        board should never get as far as being measured against one.
        """
        path_id = self.id
        if self.steps[0].kind != COPPER or self.steps[-1].kind != COPPER:
            raise PathError(
                "path {!r} starts or ends on a {} step; a path begins and ends "
                "on copper at a pad, because that is where an arrival time is "
                "defined".format(path_id, self.steps[0].kind))
        # Copper and component steps must alternate. Two copper steps in a row
        # would be a net change with nothing bridging it - which is not an
        # electrical path, it is two of them - and two component steps in a row
        # would be a part reached without copper.
        for position, step in enumerate(self.steps):
            expected = COPPER if position % 2 == 0 else COMPONENT
            if step.kind != expected:
                raise PathError(
                    "path {!r}: step {} is a {} step where a {} step is "
                    "required. Copper and component steps alternate: a net "
                    "changes only where something bridges it, and a part is "
                    "reached only over copper".format(
                        path_id, position, step.kind, expected))
        nets = [s.net for s in self.steps if s.kind == COPPER]
        repeated = sorted({n for n in nets if nets.count(n) > 1})
        if repeated:
            raise PathError(
                "path {!r} enters net(s) {} more than once. A signal that "
                "returns to a net it has already left is a loop, and a loop "
                "has no arrival time".format(path_id, ", ".join(repeated)))
        parts = [s.reference for s in self.steps if s.kind == COMPONENT]
        twice = sorted({p for p in parts if parts.count(p) > 1})
        if twice:
            raise PathError(
                "path {!r} crosses {} more than once".format(
                    path_id, ", ".join(twice)))

    @classmethod
    def from_spec(cls, spec):
        if "id" not in spec:
            raise PathError("a path declaration carries no id")
        steps = [step_from_spec(s, i)
                 for i, s in enumerate(spec.get("steps", []))]
        attributes = {k: v for k, v in spec.items()
                      if k not in ("id", "steps")}
        return cls(spec["id"], steps, attributes)

    def describe(self):
        return {"id": self.id, "steps": [s.describe() for s in self.steps],
                **self.attributes}

    def component_references(self):
        return [s.reference for s in self.steps if s.kind == COMPONENT]

    def nets(self):
        return [s.net for s in self.steps if s.kind == COPPER]

    def __repr__(self):
        return "<ElectricalPath {} ({} steps)>".format(self.id, len(self.steps))


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_template(template, bindings):
    """Substitute ``{token}`` throughout a declaration, once per binding.

    Plain textual substitution and nothing else: no arithmetic, no conditions,
    no board knowledge. Eight near-identical branches are written once and
    bound eight times, and what each binding produces is still readable in the
    manifest rather than computed in Python.
    """
    out = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise PathError("a template binding must be an object, got "
                            "{}".format(type(binding).__name__))
        out.append(_substitute(template, binding))
    return out


def _substitute(node, binding):
    if isinstance(node, dict):
        return {k: _substitute(v, binding) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, binding) for v in node]
    if isinstance(node, str):
        def replace(match):
            token = match.group(1)
            if token not in binding:
                raise PathError(
                    "template uses {{{}}} but the binding {} does not define "
                    "it".format(token, sorted(binding)))
            return str(binding[token])
        return _TOKEN.sub(replace, node)
    return node


def declared_entries(spec):
    """The raw path declarations a spec produces, templates expanded."""
    declared = list(spec.get("paths", []) or [])
    template = spec.get("template")
    if template is not None:
        bindings = spec.get("bindings")
        if not bindings:
            raise PathError(
                "a path template declares no bindings, so it instantiates "
                "nothing")
        declared.extend(expand_template(template, bindings))
    if not declared:
        raise PathError("declaration produces no paths at all")
    return declared


def build_paths(spec):
    """`(paths, problems)` - one malformed route does not blind the rest.

    A declaration error is a fact about one route, and on a board with dozens
    of them, refusing to report any of the others because one is wrong makes
    the report less useful exactly when it is most needed. Each entry is built
    on its own, and the ones that cannot be are returned as problems for the
    gate to report as findings against their own ids.

    `paths_from_spec` remains the strict form, for a caller that wants a bad
    declaration to raise.
    """
    paths, problems, seen = [], [], {}
    for index, entry in enumerate(declared_entries(spec)):
        identity = entry.get("id") if isinstance(entry, dict) else None
        try:
            path = ElectricalPath.from_spec(entry)
        except PathError as exc:
            problems.append({"path": identity or "<declaration {}>".format(
                index), "issue": str(exc)})
            continue
        if path.id in seen:
            problems.append({
                "path": path.id,
                "issue": "two declared paths share this id; a path id is how a "
                         "measurement is identified later, so it has to be "
                         "unique"})
            continue
        seen[path.id] = path
        paths.append(path)
    return paths, problems


def paths_from_spec(spec):
    """Every `ElectricalPath` a manifest declaration produces, or raise.

    A declaration may list `paths` outright, or give one `template` plus the
    `bindings` that instantiate it, or both.
    """
    paths, problems = build_paths(spec)
    if problems:
        first = problems[0]
        raise PathError("{}: {}".format(first["path"], first["issue"]))
    return paths


# ---------------------------------------------------------------------------
# resolution against a real board
# ---------------------------------------------------------------------------

class ResolvedStep:
    """One step of a path, measured. What a report shows and a model consumes."""

    def __init__(self, kind, record):
        self.kind = kind
        self.record = record

    @property
    def length_mm(self):
        return self.record.get("length_mm", 0.0) or 0.0

    def to_dict(self):
        record = dict(self.record)
        contribution = record.pop("contribution", None)
        if contribution is not None:
            record["contribution"] = contribution.to_dict()
        return record


class ResolvedPath:
    """One concrete source-to-destination path, measured on the board."""

    def __init__(self, path_id, source, destination, steps, declaration):
        self.id = path_id
        self.source = source
        self.destination = destination
        self.steps = steps
        self.declaration = declaration

    # -- aggregate geometry ------------------------------------------------
    @property
    def copper_length_mm(self):
        return sum(s.length_mm for s in self.steps if s.kind == COPPER)

    def length_by_layer_mm(self):
        totals = {}
        for step in self.steps:
            for layer, value in (step.record.get("length_by_layer_mm")
                                 or {}).items():
                totals[layer] = totals.get(layer, 0.0) + value
        return {k: round(v, 6) for k, v in sorted(totals.items())}

    def conductors(self):
        """Every (layer, width) run of copper the path traverses, merged.

        The copper with no reference conductor beneath it is summed alongside
        the length, because it is the propagation model that has to refuse on
        it and the model never sees the individual pieces.
        """
        totals, unreferenced = {}, {}
        checked = False
        for step in self.steps:
            for record in step.record.get("conductors") or []:
                key = (record["layer"], record["width_mm"])
                totals[key] = totals.get(key, 0.0) + record["length_mm"]
                unreferenced[key] = unreferenced.get(key, 0.0) + (
                    record.get("unreferenced_mm") or 0.0)
                checked = checked or record.get("reference_checked", False)
        return [{"layer": layer, "width_mm": width,
                 "length_mm": round(length, 6),
                 "unreferenced_mm": round(unreferenced.get(
                     (layer, width), 0.0), 6),
                 "reference_checked": checked}
                for (layer, width), length in sorted(totals.items())]

    def via_transitions(self):
        out = []
        for step in self.steps:
            out.extend(step.record.get("via_transitions") or [])
        return out

    def component_traversals(self):
        return [s.record for s in self.steps if s.kind == COMPONENT]

    def to_dict(self):
        return {
            "path": self.id,
            "source": self.source.to_dict(),
            "destination": self.destination.to_dict(),
            "copper_length_mm": round(self.copper_length_mm, 4),
            "length_by_layer_mm": {k: round(v, 4) for k, v
                                   in self.length_by_layer_mm().items()},
            "conductors": self.conductors(),
            "via_transition_count": len(self.via_transitions()),
            "component_traversals": [t["reference"]
                                     for t in self.component_traversals()],
            "steps": [s.to_dict() for s in self.steps],
        }


class PathResolver:
    """Turns declared paths into measured ones against one board.

    Holds the per-net `NetGraph`s, which are expensive to build and are shared
    by every path crossing the same net: two branches off one buffer share
    nothing, two loads on one branch share everything.
    """

    def __init__(self, board, pad_polygon, reference_copper=None):
        self.board = board
        self.pad_polygon = pad_polygon
        #: {layer name: poured reference copper}, when the caller knows it.
        #: Used to check that a trace has the reference conductor underneath
        #: it that a transmission-line model assumes; absent means the question
        #: was not asked, which is different from asked and answered no.
        self.reference_copper = dict(reference_copper or {})
        self._prepared = {}
        self._graphs = {}
        self._pads = None
        self._footprints = None

    # -- board indices -----------------------------------------------------
    def _index(self):
        if self._pads is None:
            # A label maps to a *list*. KiCad lets one footprint carry several
            # physical pads with the same number - split pads, a thermal pad
            # numbered with its signal pad, a connector shell - and keeping
            # only the last one seen made `REF.1` mean whichever pad the
            # iteration order happened to end on. Duplicates are legitimate
            # when they are one electrical node; they are an ambiguity when
            # they are not, and only a list can tell the two apart.
            pads, footprints = {}, {}
            for footprint in self.board.Footprints():
                ref = footprint.GetReference()
                footprints[ref] = footprint
                for pad in footprint.Pads():
                    label = "{}.{}".format(ref, pad.GetNumber())
                    pads.setdefault(label, []).append(pad)
            self._pads, self._footprints = pads, footprints
        return self._pads, self._footprints

    def graph(self, net):
        """The net's copper graph, split at every junction.

        Split because these lengths become propagation delays. The unsplit
        graph charges a walk the whole of any track it enters, which is exact
        only when copper meets copper end to end; a stub landing part-way along
        a track makes every measurement through that track wrong by the part
        the signal never travels. `NetTopologyRule` keeps the unsplit graph, so
        no existing measurement changes meaning.
        """
        if net not in self._graphs:
            from .connectivity import NetGraph
            self._graphs[net] = NetGraph(self.board, net, self.pad_polygon,
                                         split_at_junctions=True)
        return self._graphs[net]

    def pads_on_net(self, net):
        pads, _footprints = self._index()
        return sorted(label for label, group in pads.items()
                      if any(p.GetNetname() == net for p in group))

    def pad_nets(self, label):
        """Every net the physical pads behind one label are on."""
        pads, _footprints = self._index()
        return sorted({p.GetNetname() for p in pads.get(label, [])})

    def _one_node(self, label, where):
        """The pads behind a label, once they are proven to be one node.

        Several physical pads sharing a number are fine as long as they are on
        one net: electrically they are one place, and a length measured to any
        of them is a length measured to it. On different nets they are not one
        place, and no measurement to "that pad" has an answer.
        """
        pads, _footprints = self._index()
        group = pads.get(label, [])
        nets = {p.GetNetname() for p in group}
        if len(nets) > 1:
            raise PathError(
                "{}: {} names {} physical pads and they are not on one net "
                "({}). A selector has to name one electrical place".format(
                    where, label, len(group), ", ".join(sorted(nets))))
        return group

    def layer_name(self, layer_id):
        """The canonical layer name, which is what a stackup is written in.

        A board may rename `In1.Cu` to something it finds more readable; a
        physical stackup and a propagation model are about the layer, not about
        what one board decided to call it.
        """
        import pcbnew
        return pcbnew.LayerName(layer_id)

    # -- resolution --------------------------------------------------------
    def resolve(self, path):
        """Every concrete measured path this declaration produces.

        Raises `PathError` with the reason when a declaration cannot be
        satisfied. Nothing is skipped and nothing is approximated: a path the
        board does not contain is a finding, not a shorter path.
        """
        last = path.steps[-1]
        candidates = last.target.select(self.pads_on_net(last.net))
        if not candidates:
            raise PathError(
                "path {!r}: nothing on net {!r} matches the destination "
                "selector {!r}".format(path.id, last.net,
                                       last.target.declared))
        return [self._resolve_one(path, destination)
                for destination in candidates]

    def _resolve_one(self, path, destination_label):
        steps = []
        source_endpoint = None
        for position, step in enumerate(path.steps):
            final = position == len(path.steps) - 1
            if step.kind == COPPER:
                target = (destination_label if final
                          else self._unique_target(path, step))
                record, first_label = self._measure_copper(path, step, target)
                steps.append(ResolvedStep(COPPER, record))
                if source_endpoint is None:
                    source_endpoint = Endpoint(first_label, step.net, "source")
            else:
                steps.append(ResolvedStep(
                    COMPONENT, self._measure_component(path, step, position)))
        return ResolvedPath(
            path.id, source_endpoint,
            Endpoint(destination_label, path.steps[-1].net, "destination"),
            steps, path)

    def _unique_target(self, path, step):
        """An intermediate step has to land on exactly one pad."""
        found = step.target.select(self.pads_on_net(step.net))
        if len(found) == 1:
            return found[0]
        if not found:
            raise PathError(
                "path {!r}: step {} selects {!r} on net {!r}, which matches no "
                "pad on that net".format(path.id, step.index,
                                         step.target.declared, step.net))
        raise PathError(
            "path {!r}: step {} selects {!r} on net {!r}, which matches {} "
            "pads ({}). Only the final step of a path may fan out; an "
            "intermediate one that does is two undeclared paths".format(
                path.id, step.index, step.target.declared, step.net,
                len(found), ", ".join(found)))

    def _measure_copper(self, path, step, target_label):
        graph = self.graph(step.net)
        on_net = self.pads_on_net(step.net)
        sources = step.source.select(on_net)
        if not sources:
            raise PathError(
                "path {!r}: step {} selects source {!r} on net {!r}, which "
                "matches no pad on that net".format(
                    path.id, step.index, step.source.declared, step.net))
        if target_label not in on_net:
            raise PathError(
                "path {!r}: step {} targets {} which is not a pad on net "
                "{!r}".format(path.id, step.index, target_label, step.net))
        where = "path {!r}: step {}".format(path.id, step.index)
        for label in list(sources) + [target_label]:
            self._one_node(label, where)
        if len(sources) > 1 and step.source_selection == SOURCE_UNIQUE:
            raise PathError(
                "path {!r}: step {} selects source {!r} on net {!r}, which "
                "matches {} pads ({}). A path that starts from whichever of "
                "them happens to be nearest is not a stable declaration - name "
                "one, or declare source_selection \"shortest\" to say that "
                "the shortest is what you mean".format(
                    path.id, step.index, step.source.declared, step.net,
                    len(sources), ", ".join(sources)))
        if target_label in sources and len(sources) == 1:
            raise PathError(
                "path {!r}: step {} runs from {} to itself".format(
                    path.id, step.index, target_label))
        length, chain = graph.trace(sources, target_label)
        if length is None:
            raise PathError(
                "path {!r}: step {} finds no connected copper from {} to {} on "
                "net {!r}; the signal cannot get there along the copper that "
                "is drawn".format(path.id, step.index, "/".join(sources),
                                  target_label, step.net))
        record = self._walk_record(graph, chain)
        record.update({
            "kind": COPPER, "net": step.net, "step": step.index,
            "from": graph.elements[chain[0]].ref, "to": target_label,
            "source_candidates": sources,
            "length_mm": round(length, 6),
        })
        return record, graph.elements[chain[0]].ref

    def _walk_record(self, graph, chain):
        """Attribute the walk's length to layers, and record its transitions.

        The cost model is `NetGraph`'s own: entering an element costs that
        element's length, so the walk's total is the sum over every element
        after the first. Each of those lengths is attributed to the layer its
        element is on, which is what a propagation model needs - an outer-layer
        millimetre and an inner-layer millimetre are not the same delay.
        """
        by_layer = {}
        by_geometry = {}
        uncovered = {}
        ambiguity = 0.0
        transitions = []
        elements = graph.elements
        for position, index in enumerate(chain):
            element = elements[index]
            if position and element.kind == "track":
                layer = self.layer_name(element.obj.GetLayer())
                by_layer[layer] = by_layer.get(layer, 0.0) + element.length_mm
                # Width as well as layer, because a propagation model needs
                # both: two millimetres on the same layer at different widths
                # are two different transmission lines, and averaging them
                # would be a model nobody chose.
                width = round(element.obj.GetWidth() / 1e6, 6)
                key = (layer, width)
                by_geometry[key] = by_geometry.get(key, 0.0) + element.length_mm
                ambiguity = max(ambiguity, element.ambiguity_mm)
                uncovered[key] = uncovered.get(key, 0.0) + self._uncovered_mm(
                    element)
            if element.kind == "via":
                transitions.append(
                    self._via_record(element, elements, chain, position))
            elif element.kind == "pad" and position:
                # A plated through-hole pad is a barrel like any other. When a
                # walk enters it on one layer and leaves on another, the signal
                # went down the hole - and recording nothing there would drop
                # that transit from the result silently, which is the one thing
                # a pad must not get away with for being inside a footprint.
                crossing = self._pad_transition(element, elements, chain,
                                                position)
                if crossing is not None:
                    transitions.append(crossing)
        return {
            "length_by_layer_mm": {k: round(v, 6)
                                   for k, v in sorted(by_layer.items())},
            "conductors": [
                {"layer": layer, "width_mm": width,
                 "length_mm": round(length, 6),
                 "unreferenced_mm": round(uncovered.get((layer, width), 0.0), 6),
                 "reference_checked": bool(self.reference_copper)}
                for (layer, width), length in sorted(by_geometry.items())],
            "via_transitions": transitions,
            "length_ambiguity_mm": round(ambiguity, 6),
            "elements_traversed": len(chain),
        }

    def _via_record(self, element, elements, chain, position):
        """One via the walk went through: where it is and which layers it joined.

        Both the layers the signal actually changed between and the layers the
        via itself spans are recorded. They are different questions - a
        through-hole via used between two inner layers still has barrel above
        and below it - and a later model that wants to reason about the stub
        has to be told the difference rather than guess it.
        """
        import pcbnew
        via = element.obj
        where = via.GetPosition()
        return {
            "x_mm": round(where.x / 1e6, 4),
            "y_mm": round(where.y / 1e6, 4),
            "from_layer": _neighbour_layer(elements, chain, position, -1, self),
            "to_layer": _neighbour_layer(elements, chain, position, +1, self),
            "via_top_layer": self.layer_name(via.TopLayer()),
            "via_bottom_layer": self.layer_name(via.BottomLayer()),
            "drill_mm": round(via.GetDrill() / 1e6, 4),
            "pad_mm": round(via.GetWidth(pcbnew.F_Cu) / 1e6, 4),
        }

    def _pad_transition(self, element, elements, chain, position):
        """A layer change made through a pad, recorded like the barrel it is.

        Returns None when the walk entered and left the pad on the same layer,
        which is the ordinary case and no transition at all.

        The pad's own copper extent is what bounds the barrel, so a pad present
        only on the outer layers spans the whole board and one on an inner pair
        spans only between them. `via_top_layer`/`via_bottom_layer` keep the
        names the via record uses, because the consumer of both is one model
        and it should not have to care which kind of hole it was.
        """
        import pcbnew
        entered = _neighbour_layer(elements, chain, position, -1, self)
        left = _neighbour_layer(elements, chain, position, +1, self)
        if entered is None or left is None or entered == left:
            return None
        pad = element.obj
        stack = [layer for layer in self.board.GetEnabledLayers().CuStack()
                 if pad.IsOnLayer(layer)]
        names = [self.layer_name(layer) for layer in stack]
        where = pad.GetPosition()
        return {
            "through": "pad",
            "pad": element.ref,
            "x_mm": round(where.x / 1e6, 4),
            "y_mm": round(where.y / 1e6, 4),
            "from_layer": entered,
            "to_layer": left,
            "via_top_layer": names[0] if names else None,
            "via_bottom_layer": names[-1] if names else None,
            "drill_mm": round(pad.GetDrillSizeX() / 1e6, 4),
            "plated": pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH,
        }

    def _uncovered_mm(self, element):
        """How much of one copper piece has no reference conductor beneath it.

        Zero when the caller asked no reference question. Otherwise the length
        of this piece whose footprint is not covered by poured reference
        copper on any reference layer - a route past the edge of a pour, over a
        void, or across a split.

        Measured as a fraction of area rather than by cutting the piece again:
        the piece is a constant-width rectangle, so the uncovered fraction of
        its area is the uncovered fraction of its length, and that is accurate
        enough to decide whether a closed-form model applies at all.
        """
        if not self.reference_copper:
            return 0.0
        shape = element.shape
        if shape.is_empty or shape.area <= 0:
            return 0.0
        remaining = shape
        for layer, copper in self.reference_copper.items():
            prepared = self._prepared.get(layer)
            if prepared is None:
                from shapely.prepared import prep
                prepared = self._prepared[layer] = prep(copper)
            if prepared.intersects(remaining):
                remaining = remaining.difference(copper)
                if remaining.is_empty:
                    return 0.0
        return round(element.length_mm * (remaining.area / shape.area), 6)

    def _measure_component(self, path, step, position):
        """Check the part really does bridge the nets either side of it."""
        pads, footprints = self._index()
        footprint = footprints.get(step.reference)
        if footprint is None:
            raise PathError(
                "path {!r}: step {} crosses {}, which is not on the "
                "board".format(path.id, step.index, step.reference))
        where = "path {!r}: step {}".format(path.id, step.index)
        entry_group = self._one_node(step.entry_label, where)
        leave_group = self._one_node(step.exit_label, where)
        for label, group in ((step.entry_label, entry_group),
                             (step.exit_label, leave_group)):
            if not group:
                raise PathError(
                    "path {!r}: step {} names pad {}, which {} does not "
                    "have".format(path.id, step.index, label, step.reference))
        entry, leave = entry_group[0], leave_group[0]
        before = path.steps[position - 1] if position else None
        after = (path.steps[position + 1]
                 if position + 1 < len(path.steps) else None)
        for neighbour, pad, label in ((before, entry, step.entry_label),
                                      (after, leave, step.exit_label)):
            if neighbour is None or neighbour.kind != COPPER:
                continue
            if pad.GetNetname() != neighbour.net:
                raise PathError(
                    "path {!r}: step {} crosses {} expecting {} to be on net "
                    "{!r}, but the board has it on {!r}; the declared path is "
                    "not the circuit that is built".format(
                        path.id, step.index, step.reference, label,
                        neighbour.net, pad.GetNetname()))
        if entry.GetNetname() == leave.GetNetname():
            raise PathError(
                "path {!r}: step {} crosses {} from {} to {}, but both pads are "
                "on net {!r}; a traversal that does not change net joins "
                "nothing".format(path.id, step.index, step.reference,
                                 step.entry_label, step.exit_label,
                                 entry.GetNetname()))

        # A footprint is not a component. Two pads on two nets are connected
        # by the part between them, and a part the board says is not fitted is
        # not between them - so the declared path does not exist on a board
        # built to this data. Refusing is the only safe answer: the alternative
        # is reporting a propagation delay along copper that ends in air.
        dnp = bool(footprint.IsDNP())
        if dnp and step.assume_populated is None:
            raise PathError(
                "path {!r}: step {} crosses {}, which the board marks "
                "do-not-populate. An unfitted part does not join {} to {}, so "
                "this path does not exist on a board built to this data. If a "
                "build variant does fit it, say so with `assume_populated` and "
                "a justification".format(
                    path.id, step.index, step.reference,
                    entry.GetNetname(), leave.GetNetname()))

        contribution = component_models.evaluate(step.delay_model,
                                                 step.entry_label)
        record = {
            "kind": COMPONENT, "step": step.index,
            "reference": step.reference,
            "from_pad": step.from_pad, "to_pad": step.to_pad,
            "from_net": entry.GetNetname(), "to_net": leave.GetNetname(),
            "value": footprint.GetValue(),
            "footprint": footprint.GetFPIDAsString(),
            "dnp": dnp,
            "excluded_from_bom": bool(footprint.IsExcludedFromBOM()),
            "declared_delay_model": step.delay_model,
            "contribution": contribution,
            "length_mm": 0.0,
        }
        if dnp:
            record["assumed_populated"] = step.assume_populated
        return record


def _neighbour_layer(elements, chain, position, direction, resolver):
    """The copper layer of the nearest track on one side of a via in the walk."""
    index = position + direction
    while 0 <= index < len(chain):
        element = elements[chain[index]]
        if element.kind == "track":
            return resolver.layer_name(element.obj.GetLayer())
        index += direction
    return None
