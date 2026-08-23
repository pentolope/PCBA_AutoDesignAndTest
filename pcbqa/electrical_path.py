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

    def __init__(self, net, source, target, index=0):
        self.net = net
        self.source = PadSelector(source)
        self.target = PadSelector(target)
        self.index = index

    def describe(self):
        return {"kind": self.kind, "net": self.net,
                "from": self.source.declared, "to": self.target.declared}

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

    def __init__(self, reference, from_pad, to_pad, delay_model=None, index=0):
        self.reference = reference
        self.from_pad = str(from_pad)
        self.to_pad = str(to_pad)
        self.delay_model = delay_model
        self.index = index

    @property
    def entry_label(self):
        return "{}.{}".format(self.reference, self.from_pad)

    @property
    def exit_label(self):
        return "{}.{}".format(self.reference, self.to_pad)

    def describe(self):
        return {"kind": self.kind, "reference": self.reference,
                "from_pad": self.from_pad, "to_pad": self.to_pad,
                "delay_model": self.delay_model}

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
        return CopperSegment(spec["net"], spec["from"], spec["to"], index)
    if kind == COMPONENT:
        for field in ("reference", "from_pad", "to_pad"):
            if field not in spec:
                raise PathError(
                    "component step {} declares no {!r}".format(index, field))
        return ComponentTraversal(spec["reference"], spec["from_pad"],
                                  spec["to_pad"], spec.get("delay_model"),
                                  index)
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
        if self.steps[0].kind != COPPER or self.steps[-1].kind != COPPER:
            raise PathError(
                "path {!r} starts or ends on a {} step; a path begins and ends "
                "on copper at a pad, because that is where an arrival time is "
                "defined".format(path_id, self.steps[0].kind))

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


def paths_from_spec(spec):
    """Every `ElectricalPath` a manifest declaration produces.

    A declaration may list `paths` outright, or give one `template` plus the
    `bindings` that instantiate it, or both.
    """
    declared = []
    for entry in spec.get("paths", []) or []:
        declared.append(entry)
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
    paths = [ElectricalPath.from_spec(entry) for entry in declared]
    seen = {}
    for path in paths:
        if path.id in seen:
            raise PathError(
                "two declared paths share the id {!r}; a path id is how a "
                "measurement is identified later, so it has to be "
                "unique".format(path.id))
        seen[path.id] = path
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
        return dict(self.record)


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
        """Every (layer, width) run of copper the path traverses, merged."""
        totals = {}
        for step in self.steps:
            for record in step.record.get("conductors") or []:
                key = (record["layer"], record["width_mm"])
                totals[key] = totals.get(key, 0.0) + record["length_mm"]
        return [{"layer": layer, "width_mm": width,
                 "length_mm": round(length, 6)}
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

    def __init__(self, board, pad_polygon):
        self.board = board
        self.pad_polygon = pad_polygon
        self._graphs = {}
        self._pads = None
        self._footprints = None

    # -- board indices -----------------------------------------------------
    def _index(self):
        if self._pads is None:
            pads, footprints = {}, {}
            for footprint in self.board.Footprints():
                ref = footprint.GetReference()
                footprints[ref] = footprint
                for pad in footprint.Pads():
                    pads["{}.{}".format(ref, pad.GetNumber())] = pad
            self._pads, self._footprints = pads, footprints
        return self._pads, self._footprints

    def graph(self, net):
        if net not in self._graphs:
            from .connectivity import NetGraph
            self._graphs[net] = NetGraph(self.board, net, self.pad_polygon)
        return self._graphs[net]

    def pads_on_net(self, net):
        pads, _footprints = self._index()
        return sorted(label for label, pad in pads.items()
                      if pad.GetNetname() == net)

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
            if element.kind == "via":
                transitions.append(
                    self._via_record(element, elements, chain, position))
        return {
            "length_by_layer_mm": {k: round(v, 6)
                                   for k, v in sorted(by_layer.items())},
            "conductors": [{"layer": layer, "width_mm": width,
                            "length_mm": round(length, 6)}
                           for (layer, width), length
                           in sorted(by_geometry.items())],
            "via_transitions": transitions,
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

    def _measure_component(self, path, step, position):
        """Check the part really does bridge the nets either side of it."""
        pads, footprints = self._index()
        footprint = footprints.get(step.reference)
        if footprint is None:
            raise PathError(
                "path {!r}: step {} crosses {}, which is not on the "
                "board".format(path.id, step.index, step.reference))
        entry = pads.get(step.entry_label)
        leave = pads.get(step.exit_label)
        for label, pad in ((step.entry_label, entry), (step.exit_label, leave)):
            if pad is None:
                raise PathError(
                    "path {!r}: step {} names pad {}, which {} does not "
                    "have".format(path.id, step.index, label, step.reference))
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
        return {
            "kind": COMPONENT, "step": step.index,
            "reference": step.reference,
            "from_pad": step.from_pad, "to_pad": step.to_pad,
            "from_net": entry.GetNetname(), "to_net": leave.GetNetname(),
            "value": footprint.GetValue(),
            "footprint": footprint.GetFPIDAsString(),
            "dnp": bool(footprint.IsDNP()),
            "declared_delay_model": step.delay_model,
            "length_mm": 0.0,
        }


def _neighbour_layer(elements, chain, position, direction, resolver):
    """The copper layer of the nearest track on one side of a via in the walk."""
    index = position + direction
    while 0 <= index < len(chain):
        element = elements[chain[index]]
        if element.kind == "track":
            return resolver.layer_name(element.obj.GetLayer())
        index += direction
    return None
