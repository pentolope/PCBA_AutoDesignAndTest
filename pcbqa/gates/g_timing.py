"""Passive interconnect timing gates: path integrity, delay, skew, setup/hold.

What these gates measure is the printed circuit board and nothing else. A
declared electrical path is walked across every net and every series component
it crosses, its copper is measured layer by layer and width by width, and - if
and only if the board's physical stackup supports it - the propagation delay of
that copper is estimated from a documented closed form.

That is not clock timing. The arrival of an edge at a receiver also depends on
the driver's output delay and its output-to-output skew, on package delay at
both ends, on the receiver's switching threshold and its setup and hold
requirements, and on process, voltage and temperature. None of that is on the
board and none of it is guessable from geometry, so none of it is included
here. `TIMING.INTERCONNECT_DELAY` and `TIMING.INTERCONNECT_SKEW` say
*interconnect* in their names for that reason, and `TIMING.SETUP_HOLD` - the
gate that would answer the whole question - stays non-applicable until a board
supplies the device timing it needs.

Three separately cached layers, because they fail for separate reasons
---------------------------------------------------------------------
``geometry``     the declared paths, resolved against the copper. Needs no
                 material data, no model and no solver, so it answers "does
                 this path exist" whether or not anything else is available.
``stackup``      the physical stack, and which of its layers are planes.
``propagation``  the model and a delay claim per path.

They were one cache, which made a question about connectivity unanswerable
whenever propagation data was insufficient - precisely backwards, since
connectivity is the question that never needed a delay model.

Every threshold, path, endpoint group, model choice and material figure comes
from the board's manifest. Nothing in this module names a net, a designator, an
interface or a number.
"""

from __future__ import annotations

import os
import re

from ..core import gate
from .. import claim, electrical_path, geom, propagation, stackup_physical
from ..constraints import implementation_constant
from ..electrical_path import PathError
from ..stackup_physical import StackupError

MINIMUM_SPREAD_MEMBERS = implementation_constant(
    2, "a spread requires two distinct members")

# ---------------------------------------------------------------------------
# layer 1: geometry
# ---------------------------------------------------------------------------

class GeometryAnalysis:
    """Declared paths resolved against copper. No materials, no model."""

    def __init__(self, interfaces, reference_copper=None, unfilled_layers=(),
                 resolver=None):
        self.interfaces = interfaces
        self.resolver = resolver
        self.reference_copper = reference_copper or {}
        #: Layers a reference net is assigned to but whose zones carry no
        #: filled polygons. Not the same as having no copper: it is the board
        #: never having been refilled, and a coverage answer from it would be
        #: an answer about nothing.
        self.unfilled_layers = list(unfilled_layers)

    def all_paths(self):
        for name, interface in sorted(self.interfaces.items()):
            for record in interface["paths"]:
                yield name, record

    def all_problems(self):
        out = []
        for name, interface in sorted(self.interfaces.items()):
            for problem in interface["problems"]:
                out.append({**problem, "interface": name})
        return out

    def layers_used(self):
        """Every copper layer a resolved path actually runs on."""
        used = set()
        for _name, record in self.all_paths():
            for conductor in record["resolved"].conductors():
                used.add(conductor["layer"])
        return used

    def via_span_layers(self, stack):
        """Every copper layer a resolved path's barrels pass through.

        A trace on the outer layers of a four-layer board tells you nothing
        about the two dielectrics in the middle - until a via joins those
        outer layers, at which point a model that attributes delay to the
        transit reads every one of them. Scoping the stackup question to the
        layers carrying horizontal copper therefore asks for less data than
        the calculation consumes, and a stackup silent about the middle would
        have been called complete.
        """
        spanned = set()
        for _name, record in self.all_paths():
            for transition in record["resolved"].via_transitions():
                # The layers the signal actually changed between, which is what
                # the geometric model integrates over. The rest of the barrel
                # is stub, and this release models no stub effect - so asking
                # for the stackup around it would be demanding data the
                # calculation never reads.
                spanned.update(stack.copper_layers_between(
                    transition.get("from_layer"),
                    transition.get("to_layer")))
        return spanned

    @staticmethod
    def key(record):
        """What identifies one resolved path: its route and its destination."""
        return (record["resolved"].id, record["resolved"].destination.label)


def _unreferenced_by_path(state, only=None):
    """Per resolved path, copper with no reference conductor anywhere below.

    Three decisions, each of which had a wrong alternative:

      * *Per path*, never summed across paths. The old total accumulated
        every resolved endpoint path, so a shared branch counted its shared
        copper once per load hanging off it - a number whose meaning
        changed with the fan-out. Each path is measured on its own, and a
        limit reads "no single endpoint path may carry more than this".
      * The ``<any>`` union measure, not a per-plane one. This is the design
        question - is there reference *anywhere* below - which is exactly the
        measure the resolver computed geometrically against the union of the
        pours. Any per-plane figure would overstate it.
      * Unknown is unknown, never zero. A reference layer whose zones were
        never filled, or reference structure that was never measured at all,
        yields None - and a None measurement cannot satisfy a declared limit.
    """
    if state.unfilled_layers:
        return None, "reference layer(s) {} carry no filled polygons, so " \
                     "coverage was never established".format(
                         ", ".join(state.unfilled_layers))
    per_path = {}
    for name, record in state.all_paths():
        if only is not None and name != only:
            continue
        total = 0.0
        for conductor in record["resolved"].conductors():
            per_layer = conductor.get("unreferenced_by_layer_mm") or {}
            if not per_layer:
                if conductor.get("reference_checked"):
                    return None, "a conductor was reference-checked but " \
                                 "carries no coverage figures"
                return None, "reference coverage was never measured: no " \
                             "poured reference copper exists to measure " \
                             "against"
            union = per_layer.get("<any>")
            if union is None:
                union = min(per_layer.values())
            total += union
        key = (record["resolved"].id, record["resolved"].destination.label)
        per_path["{}->{}".format(*key)] = round(total, 4)
    return per_path, None


def _identifier(kind, name):
    """Names that index into the manifest cannot contain a dot.

    A limit is traced back to the manifest by dotted key, so a name with a dot
    in it would cite a key that does not exist and `Manifest.constraint` would
    be unable to prove where the number came from.
    """
    if not isinstance(name, str) or not name or "." in name:
        raise PathError(
            "{} name {!r} is not usable: it indexes the manifest by dotted "
            "key, so it must be a non-empty string containing no dot".format(
                kind, name))
    return name


def geometry(ctx):
    """Resolve every declared path. Cached; depends on nothing but the board."""
    def build():
        manifest = ctx.manifest
        board = ctx.board()
        geom.configure(manifest.geometry_profile()
                       .tolerance("polygon_chord_error_mm").value)
        # Reference copper is board geometry - no material figure, no model,
        # no solver - so it is gathered here and PATH_INTEGRITY stays
        # answerable without any of those. It is only *used* by propagation.
        spec = manifest.get("timing.physical_stackup", {}) or {}
        poured, unfilled = ({}, [])
        if spec.get("reference_nets"):
            poured, unfilled = stackup_physical.reference_copper(
                board, spec["reference_nets"])
        resolver = electrical_path.PathResolver(
            board, geom.pad_copper_polygon, reference_copper=poured,
            unfilled_reference_layers=unfilled)
        interfaces = {}
        for name, declared in sorted(
                (manifest.get("timing.interfaces") or {}).items()):
            _identifier("interface", name)
            interfaces[name] = _interface(name, declared, resolver)
        return GeometryAnalysis(interfaces, poured, unfilled, resolver)
    return ctx.cache("timing_geometry", build)


def _interface(name, declared, resolver):
    """One interface: its declared paths, resolved against the board."""
    records = []
    routes = declared.get("routes")
    if not routes:
        raise PathError(
            "timing interface {!r} declares no routes, so it describes no "
            "path to measure".format(name))
    paths, problems = electrical_path.build_paths(routes)
    for path in paths:
        try:
            resolved = resolver.resolve(path)
        except PathError as exc:
            problems.append({"path": path.id, "issue": str(exc)})
            continue
        for concrete in resolved:
            records.append({"declared": path, "resolved": concrete})
    return {"spec": declared, "declared_paths": paths, "paths": records,
            "problems": problems}


# ---------------------------------------------------------------------------
# layer 2: the physical stackup
# ---------------------------------------------------------------------------

class StackupAnalysis:
    def __init__(self, stack, reference_layers, contradictions):
        self.stackup = stack
        self.reference_layers = reference_layers
        self.contradictions = contradictions


def stackup(ctx):
    """Native stackup, board supplement, plane layers and contradictions."""
    def build():
        spec = ctx.manifest.get("timing.physical_stackup", {}) or {}
        board = ctx.board()
        native = stackup_physical.from_board_file(ctx.board_path())
        supplement = spec.get("supplement")
        if supplement:
            import json
            full = ctx.manifest.resolve(supplement)
            root = os.path.realpath(ctx.manifest.resolve("."))
            resolved = os.path.realpath(full)
            try:
                inside = os.path.commonpath((root, resolved)) == root
            except ValueError:
                inside = False
            if not inside:
                raise StackupError(
                    "timing.physical_stackup.supplement resolves outside "
                    "the project this manifest describes ({} -> {}); a "
                    "verdict input lives with the design it "
                    "shapes".format(supplement, resolved))
            if not os.path.isfile(full):
                raise StackupError(
                    "timing.physical_stackup.supplement names {}, which does "
                    "not exist; a declared model file that is not there is a "
                    "missing input, not an empty one".format(supplement))
            with open(full, encoding="utf-8") as handle:
                declared = stackup_physical.from_declaration(json.load(handle))
            stack = stackup_physical.merge(native, declared)
        else:
            stack = native
        board_layers = stackup_physical.board_copper_layers(board)
        stack.board_copper_layers = board_layers
        nets = spec.get("reference_nets")
        planes = (set() if nets is None
                  else stackup_physical.plane_layers(board, nets))
        return StackupAnalysis(stack, planes,
                               stack.contradictions(board_layers))
    return ctx.cache("timing_stackup", build)


# ---------------------------------------------------------------------------
# layer 3: propagation
# ---------------------------------------------------------------------------

class PropagationAnalysis:
    """The model and one delay record per resolved path.

    `error` is set instead of raising when the board's declared propagation
    policy cannot be honoured. Carrying the failure rather than raising it is
    what lets the gates that need propagation block while the gate that only
    needs geometry carries on.
    """

    def __init__(self, model=None, delays=None, error=None):
        self.model = model
        self.delays = delays or {}
        self.error = error

    @property
    def usable(self):
        return self.error is None


def propagation_analysis(ctx):
    def build():
        spec = ctx.manifest.get("timing.propagation", {}) or {}
        shape = stackup(ctx)
        paths = geometry(ctx)
        try:
            model = propagation.PropagationModel(
                shape.stackup, shape.reference_layers,
                model=spec.get("model", propagation.HAMMERSTAD),
                via_model=spec.get("via_delay_model"),
                declared_layers=spec.get("declared_layers"),
                discontinuity=spec.get("reference_discontinuity"),
                unfilled_reference_layers=paths.unfilled_layers)
        except propagation.PropagationError as exc:
            return PropagationAnalysis(
                error="{}: {}".format(type(exc).__name__, exc))
        delays = {}
        for _name, record in paths.all_paths():
            delays[paths.key(record)] = model.evaluate(record["resolved"])
        return PropagationAnalysis(model, delays)
    return ctx.cache("timing_propagation", build)


def _requested_fields(ctx, layers):
    """The stackup fields this board's declared analyses will actually read."""
    spec = ctx.manifest.get("timing.propagation", {}) or {}
    return propagation.required_stackup_fields(
        spec.get("model", propagation.HAMMERSTAD),
        spec.get("via_delay_model"),
        spec.get("declared_layers"), layers)


# ---------------------------------------------------------------------------
# path integrity
# ---------------------------------------------------------------------------

@gate("TIMING.PATH_INTEGRITY",
      "Declared electrical paths exist, end to end, across every component",
      gate_class="design", requires=("timing.interfaces",), order=310)
def path_integrity(ctx, res):
    """Does the board contain the paths the timing policy describes?

    Geometry and connectivity only. It reads no material figure, builds no
    propagation model, so a board with no usable stackup still gets a real
    answer to a real question: does this declared path physically exist, and
    does it cross what it says it crosses.
    """
    state = geometry(ctx)
    problems = list(state.all_problems())
    summary = {}
    for name, interface in sorted(state.interfaces.items()):
        spec = interface["spec"]
        rows = []
        for record in interface["paths"]:
            resolved = record["resolved"]
            rows.append({
                "path": resolved.id,
                "source": resolved.source.label,
                "destination": resolved.destination.label,
                "nets": [s.record["net"] for s in resolved.steps
                         if s.kind == electrical_path.COPPER],
                "crosses": [t["reference"]
                            for t in resolved.component_traversals()],
                "copper_length_mm": round(resolved.copper_length_mm, 4),
                "length_by_layer_mm": resolved.length_by_layer_mm(),
                "via_transitions": len(resolved.via_transitions()),
                # How the path decomposes. Without this a reader can see the
                # total but not which part of it a net-scoped measurement
                # would have missed, which is most of the point.
                "steps": [
                    {"kind": s.kind,
                     "net": s.record.get("net"),
                     "reference": s.record.get("reference"),
                     "from": s.record.get("from") or s.record.get("from_pad"),
                     "to": s.record.get("to") or s.record.get("to_pad"),
                     "length_mm": round(s.length_mm, 4)}
                    for s in resolved.steps],
            })
        summary[name] = {
            "description": spec.get("description"),
            "declared_routes": len(interface["declared_paths"]),
            "resolved_paths": len(interface["paths"]),
            "paths": rows,
        }

        expected = spec.get("expected_path_count")
        if expected is not None:
            res.limit(ctx.manifest.constraint(
                "timing.interfaces.{}.expected_path_count".format(name),
                units="paths",
                cid="timing.{}.expected_path_count".format(name)))
            if len(interface["paths"]) != expected:
                problems.append({
                    "interface": name,
                    "issue": "the board resolves a different number of paths "
                             "than the interface declares",
                    "expected": expected,
                    "resolved": len(interface["paths"])})

        required = spec.get("required_component_crossings")
        if required is not None:
            res.limit(ctx.manifest.constraint(
                "timing.interfaces.{}.required_component_crossings".format(name),
                units="component crossings per path",
                cid="timing.{}.required_component_crossings".format(name)))
            for row in rows:
                if len(row["crosses"]) < required:
                    problems.append({
                        "interface": name, "path": row["path"],
                        "issue": "the path crosses fewer components than the "
                                 "interface requires; a path that does not "
                                 "cross its series part starts on the wrong "
                                 "side of it",
                        "required": required, "crosses": row["crosses"]})
    # The *design* question about reference continuity, which is not the same
    # as whether a propagation formula applies over the gap. Measured here
    # because it is pure geometry; compared here only when a board states a
    # requirement. `timing.propagation.reference_discontinuity` answers the
    # other question and does not affect this one.
    per_path, unknown_reason = _unreferenced_by_path(state)
    if per_path:
        worst = max(per_path, key=per_path.get)
        res.measurements["worst_path_unreferenced_mm"] = per_path[worst]
        res.measurements["worst_unreferenced_path"] = worst
        res.measurements["unreferenced_mm_by_path"] = {
            key: value for key, value in sorted(per_path.items()) if value}
    else:
        res.measurements["worst_path_unreferenced_mm"] = None
        res.measurements["unreferenced_unknown"] = unknown_reason
    res.measurements["reference_layers_not_filled"] = state.unfilled_layers
    for name, interface in sorted(state.interfaces.items()):
        key = "timing.interfaces.{}.max_unreferenced_mm".format(name)
        if not ctx.manifest.has(key):
            continue
        limit = res.limit(ctx.manifest.constraint(
            key, units="mm",
            cid="timing.{}.max_unreferenced_mm".format(name)))
        measured, unknown = _unreferenced_by_path(state, only=name)
        if measured is None:
            # Unknown coverage cannot satisfy a declared limit. An unfilled
            # or unmeasurable reference structure is not zero exposure; it is
            # a question nobody has answered yet.
            problems.append({
                "interface": name,
                "issue": "max_unreferenced_mm is declared but the exposure "
                         "cannot be measured: {}. The requirement is "
                         "unevaluated, not met".format(unknown),
                "limit_mm": limit.value})
            continue
        offenders = {path: value for path, value in measured.items()
                     if limit.violated_maximum(value)}
        for path_name, value in sorted(offenders.items()):
            problems.append({
                "interface": name, "path": path_name,
                "issue": "this path carries more copper with no reference "
                         "conductor anywhere beneath it than the board "
                         "accepts on any single path",
                "measured_mm": value, "limit_mm": limit.value})

    res.measurements["interfaces"] = summary
    res.measurements["paths_resolved"] = sum(
        len(i["paths"]) for i in state.interfaces.values())
    res.measurements["scope"] = (
        "geometry and connectivity only; independent of the physical stackup, "
        "the propagation model and any solver")
    for problem in problems[:60]:
        res.finding(**problem)
    if problems:
        return res.failed(
            "{} declared electrical path(s) are not what the board "
            "contains".format(len(problems)))
    return res.passed(
        "all {} declared electrical path(s) across {} interface(s) resolve end "
        "to end, each crossing the series component(s) it declares".format(
            res.measurements["paths_resolved"], len(state.interfaces)))


# ---------------------------------------------------------------------------
# physical stackup
# ---------------------------------------------------------------------------

@gate("STACK.PHYSICAL",
      "The physical stackup supports the analysis this board asks for",
      gate_class="design", requires=("timing.physical_stackup",), order=305)
def physical_stackup(ctx, res):
    """Is enough known about the materials to do what this board asked for?

    Deliberately separate from STACK.NATIVE_VS_MANIFEST, whose subject is
    copper layer order and plane assignment and whose semantics are unchanged.

    "Complete" is not absolute here. It is complete *for the analyses this
    board declared*: the fields a first-order delay reads are not the fields a
    thickness-corrected model reads, and neither reads a loss tangent. A
    stackup missing something no declared analysis will ever consult does not
    block one; a stackup missing something a declared analysis needs does.
    """
    shape = stackup(ctx)
    stack = shape.stackup
    layers = None
    if ctx.manifest.has("timing.interfaces"):
        paths = geometry(ctx)
        touched = paths.layers_used()
        if propagation.via_span_needs_stackup(
                ctx.manifest.get("timing.propagation.via_delay_model", None)):
            touched |= paths.via_span_layers(stack)
        layers = sorted(touched)
    required = _requested_fields(ctx, layers)
    missing = stack.completeness(required=required, layers=layers)

    res.measurements["physical_stackup"] = stack.to_dict()
    res.measurements["reference_plane_layers"] = sorted(shape.reference_layers)
    if ctx.manifest.has("timing.interfaces"):
        paths = geometry(ctx)
        res.measurements["reference_copper_layers"] = sorted(
            paths.reference_copper)
        res.measurements["reference_layers_not_filled"] = paths.unfilled_layers
    res.measurements["layers_in_use"] = layers
    res.measurements["fields_required_by_declared_analyses"] = sorted(required)
    res.measurements["insufficient_fields"] = missing
    res.measurements["full_inventory_gaps"] = stack.completeness()
    res.measurements["contradictions"] = shape.contradictions
    complete = res.limit(ctx.manifest.constraint(
        "timing.physical_stackup.require_complete", units="policy",
        cid="timing.physical_stackup.require_complete")).value

    # A contradiction is never tolerable, whatever a board's policy says about
    # completeness. Absence stops an analysis; a contradiction means the thing
    # being reasoned about is not a stackup.
    for problem in shape.contradictions[:40]:
        res.finding(**problem)
    if shape.contradictions:
        return res.failed(
            "the physical stackup contradicts itself or the board in {} "
            "respect(s); that is not a question of completeness and no policy "
            "makes it acceptable".format(len(shape.contradictions)))

    for layer in paths.unfilled_layers if ctx.manifest.has(
            "timing.interfaces") else []:
        missing.append({
            "layer": layer, "field": "reference_fill",
            "needed_for": "any propagation delay on a layer referenced to it",
            "issue": "a reference net is assigned to this layer but its zones "
                     "carry no filled polygons, so whether copper is actually "
                     "under a route there cannot be established; refill the "
                     "board and re-run"})
    no_planes = bool(layers) and not shape.reference_layers
    if no_planes:
        res.finding(issue="no copper layer carries a zone on any declared "
                          "reference net, so no trace on this board has an "
                          "identifiable reference plane",
                    reference_nets=ctx.manifest.get(
                        "timing.physical_stackup.reference_nets", None))
    for problem in missing[:40]:
        res.finding(**problem)

    if missing or no_planes:
        if complete:
            return res.failed(
                "the physical stackup does not state {} field(s) that this "
                "board's declared analyses read, and this board requires it to "
                "be complete; source={}".format(
                    len(missing) + (0 if not no_planes else 1), stack.source))
        return res.not_applicable(
            "this board does not require a complete physical stackup, and the "
            "one available ({}) does not state {} field(s) that its declared "
            "analyses read; propagation delay is therefore not derivable and "
            "every gate that needs it says so rather than substituting a "
            "material".format(stack.source, len(missing)))
    return res.passed(
        "the physical stackup ({}) states every field the declared analyses "
        "read ({}), contradicts neither itself nor the board, and {} reference "
        "plane layer(s) were found".format(
            stack.source, ", ".join(sorted(required)) or "none",
            len(shape.reference_layers)))


# ---------------------------------------------------------------------------
# interconnect delay
# ---------------------------------------------------------------------------

@gate("TIMING.INTERCONNECT_DELAY",
      "Passive PCB propagation delay of each declared path is within its limit",
      gate_class="design", requires=("timing.interfaces",), order=320)
def interconnect_delay(ctx, res):
    """Board copper only. Not device-aware, and the report says so.

    A path whose limit is declared but whose delay cannot be derived is
    blocking, not a pass: an unevaluated requirement is not a satisfied one.
    So is a limit compared against a lower bound that does not already exceed
    it - a bound below a maximum proves nothing about the value above it.
    """
    state = propagation_analysis(ctx)
    res.measurements["scope"] = (
        "passive PCB interconnect only: copper propagation and, where a model "
        "is declared, via vertical transit. Excludes driver output delay and "
        "output-to-output skew, package delay, receiver threshold behaviour "
        "and PVT variation.")
    if not state.usable:
        return res.errored(
            "this board's declared propagation policy could not be honoured, "
            "so no delay was evaluated: {}".format(state.error))

    paths = geometry(ctx)
    rows, problems, unresolved = [], [], []
    limited = 0
    evidence_classes = set()
    for name, record in paths.all_paths():
        delay = state.delays[paths.key(record)]
        rows.append(_delay_row(name, delay))
        evidence_class = delay["claim"]["evidence"]["evidence_class"]
        if evidence_class:
            evidence_classes.add(evidence_class)
        if delay["insufficient"]:
            unresolved.append({"interface": name, "path": delay["path"],
                               "destination": delay["destination"]["pad"],
                               "insufficient": delay["insufficient"][:3]})
        limit = _limit_for(ctx, res, name, "max_delay_ps", "ps")
        if limit is None:
            continue
        limited += 1
        problem = _maximum_problem(delay["claim"], limit,
                                   delay["insufficient"],
                                   delay["modelled_delay_ps"])
        if problem:
            problems.append({"interface": name, "path": delay["path"],
                             **problem})
    res.measurements["paths"] = rows
    res.measurements["evidence_classes"] = sorted(evidence_classes)
    res.measurements["physical_stackup_source"] = state.model.stackup.source
    res.measurements["propagation_model"] = state.model.model
    res.measurements["via_delay_model"] = state.model.via_model
    res.measurements["paths_without_derivable_delay"] = len(unresolved)
    res.measurements["paths_with_bounded_or_incomplete_delay"] = sum(
        1 for r in rows if r["claim"]["knowledge"] != claim.EXACT)
    for entry in unresolved[:20]:
        res.finding(**entry)
    for problem in problems[:40]:
        res.finding(**problem)

    if problems:
        return res.failed(
            "{} of {} path(s) with a declared delay limit are not within "
            "it".format(len(problems), limited))
    if not limited:
        return res.not_applicable(
            "no timing interface declares a maximum interconnect delay, so "
            "there is nothing to compare the {} measured path(s) against; the "
            "measurements are recorded".format(len(rows)))
    return res.passed(
        "all {} path(s) with a declared delay limit are within it, from {} at "
        "evidence {}, model {}".format(
            limited, state.model.stackup.source,
            ", ".join(sorted(evidence_classes)),
            state.model.model))


def _maximum_problem(numeric_claim, limit, insufficient, modelled):
    """Format a maximum-requirement problem from the shared verdict."""
    linked = claim.with_requirement(
        numeric_claim,
        claim.requirement(limit.id, limit.provenance,
                          {"op": "<=", "value": limit.value}))
    decision = claim.verdict(linked)
    if decision["result"] == claim.PASS:
        return None
    lower, upper = claim.bounds(linked)
    if numeric_claim["knowledge"] == claim.UNKNOWN:
        return {"issue": "a limit is declared but the value could not be "
                         "derived; the requirement is unevaluated, not met",
                "insufficient": list(insufficient)[:3]}
    if decision["result"] == claim.FAIL:
        return {"issue": "the measured value exceeds the declared limit over "
                         "the whole of its uncertainty interval",
                "modelled": modelled, "lower_bound": lower,
                "limit": limit.value, "verdict": decision}
    if upper is None:
        return {"issue": "the value has no upper bound, because some portion "
                         "contributes an unmodelled amount of unstated size. "
                         "A figure without an upper end can never prove a "
                         "maximum is met: model the portion, state a bound "
                         "with provenance for what was omitted, or drop the "
                         "limit",
                "modelled": modelled, "lower_bound": lower,
                "limit": limit.value, "verdict": decision}
    return {"issue": "the limit falls inside the uncertainty interval: the "
                     "requirement is met at the interval's lower end and "
                     "violated at its upper end, so it is undecided rather "
                     "than met",
            "modelled": modelled, "lower_bound": lower, "upper_bound": upper,
            "limit": limit.value, "verdict": decision}


def _delay_row(interface, delay):
    lower, upper = claim.bounds(delay["claim"])
    return {
        "interface": interface,
        "path": delay["path"],
        "source": delay["source"]["pad"],
        "destination": delay["destination"]["pad"],
        "copper_length_mm": delay["copper_length_mm"],
        "length_by_layer_mm": delay["length_by_layer_mm"],
        "modelled_delay_ps": delay["modelled_delay_ps"],
        "claim": delay["claim"],
        "delay_lower_ps": lower,
        "delay_upper_ps": upper,
        "geometric_uncertainty_ps": delay.get("geometric_uncertainty_ps"),
        "length_uncertainty_mm": delay.get("length_uncertainty_mm"),
        "assumptions": delay["claim"]["evidence"]["assumptions"],
        "evidence_class": delay["claim"]["evidence"]["evidence_class"],
        "crosses": [t["reference"] for t in delay["component_traversals"]],
        "component_traversals": delay["component_traversals"],
        "vias": len(delay["vias"]),
        "insufficient": delay["insufficient"],
    }


# ---------------------------------------------------------------------------
# interconnect skew
# ---------------------------------------------------------------------------

@gate("TIMING.INTERCONNECT_SKEW",
      "Passive PCB arrival spread within each declared endpoint group",
      gate_class="design", requires=("timing.interfaces",), order=330)
def interconnect_skew(ctx, res):
    """The spread of passive interconnect delay across a group of endpoints.

    This is not total clock skew and must not be read as one. It omits the
    driver's own output-to-output skew, both packages, the receivers' threshold
    behaviour and PVT. On a fan-out buffer those terms are commonly larger than
    the copper term this gate measures.
    """
    state = propagation_analysis(ctx)
    paths = geometry(ctx)
    res.measurements["scope"] = (
        "passive PCB interconnect only. This is NOT total clock arrival skew: "
        "it excludes driver output-to-output skew, driver and receiver package "
        "delay, receiver threshold behaviour and PVT variation.")
    if not state.usable:
        return res.errored(
            "this board's declared propagation policy could not be honoured, "
            "so no spread was evaluated: {}".format(state.error))

    groups, problems = [], []
    limited = 0
    for name, interface in sorted(paths.interfaces.items()):
        declared = interface["spec"].get("groups") or {}
        for group_name, group in sorted(declared.items()):
            _identifier("endpoint group", group_name)
            members = _members(interface, group)
            record = _group_record(name, group_name, group, members,
                                   state.delays, paths)
            groups.append(record)
            if not members:
                problems.append({
                    "interface": name, "group": group_name,
                    "issue": "the group selects no path at all, so its skew "
                             "requirement checks nothing",
                    "selector": group.get("paths")})
                continue
            for key, units, field in (
                    ("max_skew_ps", "ps", "skew_ps"),
                    ("max_length_spread_mm", "mm", "length_spread_mm")):
                if key not in group:
                    continue
                limit = res.limit(ctx.manifest.constraint(
                    "timing.interfaces.{}.groups.{}.{}".format(
                        name, group_name, key), units=units,
                    cid="timing.{}.{}.{}".format(name, group_name, key)))
                limited += 1
                problem = _maximum_problem(
                    record["claims"][field], limit, record["insufficient"],
                    record[field])
                if problem:
                    problems.append({"interface": name, "group": group_name,
                                     "units": units,
                                     "earliest": record["earliest"],
                                     "latest": record["latest"], **problem})
    res.measurements["groups"] = groups
    res.measurements["physical_stackup_source"] = state.model.stackup.source
    res.measurements["propagation_model"] = state.model.model
    for problem in problems[:40]:
        res.finding(**problem)
    if problems:
        return res.failed(
            "{} endpoint group(s) do not meet their declared spread".format(
                len(problems)))
    if not limited:
        return res.not_applicable(
            "no endpoint group declares a skew or length-spread limit, so "
            "there is nothing to compare the {} measured group(s) against; the "
            "measurements are recorded".format(len(groups)))
    return res.passed(
        "all {} declared spread limit(s) across {} endpoint group(s) are "
        "met".format(limited, len(groups)))


def _members(interface, group):
    selector = group.get("paths")
    if not selector:
        return list(interface["paths"])
    pattern = re.compile(selector)
    return [r for r in interface["paths"]
            if pattern.match(r["resolved"].id)]


def _spread_upper(los, his):
    """The largest spread the intervals can actually realise.

    ``max(his) - min(los)`` was tried first and is sound but not tight: when
    one member supplies both extremes it claims a spread that member cannot
    produce, since it would have to arrive at two times at once. Self-review
    caught it manufacturing undecidable verdicts where a proven PASS existed,
    and reporting nonzero possible skew for a single-member group. The
    realisable maximum pairs one member's latest against a *different*
    member's earliest:

        upper = max over i != j of (his[i] - los[j])

    which is exact - some pair realises it, and no assignment exceeds it,
    because the realised extremes always belong to some ordered pair. For a
    single member it is zero, as a single arrival's spread is. Verified
    against brute-force enumeration over random interval sets.
    """
    n = len(los)
    if n < MINIMUM_SPREAD_MEMBERS:
        return 0.0
    best = None
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            candidate = his[i] - los[j]
            best = candidate if best is None else max(best, candidate)
    return max(0.0, best)


def _group_record(interface, name, group, members, delays, paths):
    """One endpoint group's arrival spread, as an interval.

    Each member arrives somewhere in `[lo_i, hi_i]`. The spread of the
    nominal arrivals is reported, but it is neither a lower nor an upper
    bound on the true skew: an omission on the *earliest* path can close the
    gap just as one on the latest can widen it. What brackets the truth is
    interval arithmetic over the group:

        skew_lower = max(0, max_i(lo_i) - min_i(hi_i))
        skew_upper = max over i != j of (hi_i - lo_j)

    The lower bound: some path must arrive no earlier than the largest lower
    endpoint, and some path must arrive no later than the smallest upper
    endpoint, so the spread cannot be less than their difference - and when
    every interval overlaps a common point, zero true skew is possible and
    the bound is zero. The upper bound pairs one member's latest against a
    *different* member's earliest - see `_spread_upper` for why the naive
    max(hi) - min(lo) form was rejected. Members with no upper endpoint make
    `skew_upper`
    unknowable, but `skew_lower` survives them: an unbounded member cannot
    *lower* anyone else's earliest arrival.

    The same arithmetic covers the length spread, with each member's length
    in `[L_i - u_i, L_i + u_i]` from the junction uncertainty - which is
    always a finite interval, so the length spread always has both ends.
    """
    endpoints, nominals, lows, highs, insufficient = [], [], [], [], []
    length_lows, length_highs, lengths = [], [], []
    assumptions = 0
    evidence_assumptions, omitted_contributions = [], []
    for record in members:
        delay = delays.get(paths.key(record))
        resolved = record["resolved"]
        length = round(resolved.copper_length_mm, 6)
        u_mm = (delay or {}).get("length_uncertainty_mm") or 0.0
        entry = {"path": resolved.id,
                 "destination": resolved.destination.label,
                 "source": resolved.source.label,
                 "copper_length_mm": length,
                 "length_uncertainty_mm": u_mm,
                 "modelled_delay_ps": (None if delay is None else
                                        delay["modelled_delay_ps"])}
        if delay is not None:
            entry["claim"] = delay["claim"]
            lower, upper = claim.bounds(delay["claim"])
            entry["delay_lower_ps"] = lower
            entry["delay_upper_ps"] = upper
            path_evidence = delay["claim"]["evidence"]
            evidence_assumptions.extend(path_evidence["assumptions"])
            omitted_contributions.extend(
                path_evidence["omitted_contributions"])
            assumptions += len(path_evidence["assumptions"])
            if delay["modelled_delay_ps"] is None:
                insufficient.extend(delay["insufficient"][:2])
            else:
                nominals.append((delay["modelled_delay_ps"],
                                 resolved.destination.label))
                lows.append((lower,
                             resolved.destination.label))
                highs.append(upper)
        endpoints.append(entry)
        lengths.append((length, resolved.destination.label))
        length_lows.append(length - u_mm)
        length_highs.append(length + u_mm)

    have_all = bool(nominals) and len(nominals) == len(members)
    skew_lower = skew_upper = skew_nominal = None
    if have_all:
        skew_nominal = round(max(nominals)[0] - min(nominals)[0], 6)
        finite_highs = [h for h in highs if h is not None]
        # An unbounded member contributes +infinity to the his, which cannot
        # lower a minimum - so the lower bound stands on whatever is finite,
        # and is zero when nothing is.
        skew_lower = (round(max(0.0, max(lows)[0] - min(finite_highs)), 6)
                      if finite_highs else 0.0)
        skew_upper = (round(_spread_upper([lo for lo, _label in lows],
                                          finite_highs), 6)
                      if len(finite_highs) == len(highs) else None)

    spread_nominal = spread_lower = spread_upper = None
    if lengths:
        spread_nominal = round(max(lengths)[0] - min(lengths)[0], 6)
        spread_lower = round(max(0.0, max(length_lows) - min(length_highs)),
                             6)
        spread_upper = round(_spread_upper(length_lows, length_highs), 6)

    ordered = nominals if have_all else lengths
    skew_claim = _spread_claim(
        "{}:{}:skew".format(interface, name), "ps", skew_nominal,
        skew_lower, skew_upper, insufficient, "derived-path-delay-intervals",
        "propagation_delay", evidence_assumptions, omitted_contributions)
    length_claim = _spread_claim(
        "{}:{}:length-spread".format(interface, name), "mm", spread_nominal,
        spread_lower, spread_upper, insufficient, "board-geometry",
        "interconnect_geometry", [], [])
    return {
        "interface": interface, "group": name,
        "description": group.get("description"),
        "members": len(members),
        "endpoints": endpoints,
        "skew_ps": skew_nominal,
        "length_spread_mm": spread_nominal,
        "claims": {"skew_ps": skew_claim,
                   "length_spread_mm": length_claim},
        "earliest": min(ordered)[1] if ordered else None,
        "latest": max(ordered)[1] if ordered else None,
        "insufficient": insufficient,
        "assumptions_relied_on": assumptions,
        "measured_in": "ps" if have_all else "mm",
    }


def _spread_claim(identity, units, modelled, lower, upper, insufficient,
                  evidence_class, phenomenon, assumptions, omissions):
    if modelled is None or lower is None:
        return claim.claim(
            "group", identity, units, claim.UNKNOWN, {},
            claim.evidence(
                phenomenon, None,
                {"source": "pcbqa.gates.g_timing interval arithmetic"},
                applicability={
                    "status": claim.UNSUPPORTED,
                    "detail": "the contributing path claims do not establish "
                              "this spread"}),
            "no group-spread conclusion is available")
    if upper is None:
        knowledge, quantity = claim.LOWER_BOUND, {"value": lower}
    elif lower == upper:
        knowledge, quantity = claim.EXACT, {"value": lower}
    else:
        knowledge, quantity = claim.INTERVAL, {
            "lower": lower, "upper": upper}
    return claim.claim(
        "group", identity, units, knowledge, quantity,
        claim.evidence(
            phenomenon, evidence_class,
            {"source": "pcbqa.gates.g_timing interval arithmetic"},
            assumptions=assumptions,
            omitted_contributions=([] if knowledge == claim.EXACT else
                                   omissions)),
        "arrival spread across the declared endpoint group",
        knowledge_basis=claim.knowledge_basis(
            claim.DERIVED,
            "group bounds are mechanically derived from member intervals"))


def _limit_for(ctx, res, interface, key, units):
    path = "timing.interfaces.{}.limits.{}".format(interface, key)
    if not ctx.manifest.has(path):
        return None
    value = ctx.manifest.get(path)
    if value is None:
        return None
    return res.limit(ctx.manifest.constraint(
        path, units=units, cid="timing.{}.{}".format(interface, key)))


# ---------------------------------------------------------------------------
# setup / hold
# ---------------------------------------------------------------------------

@gate("TIMING.SETUP_HOLD",
      "Device-aware setup and hold margin at each declared receiver",
      gate_class="design", requires=("timing.device_timing",), order=340)
def setup_hold(ctx, res):
    """Only answerable with device data, so only applicable with device data.

    Setup and hold margin needs the source's clock-to-output, the receiver's
    setup and hold windows, the clock relationship between them, package delay
    and a PVT assumption - none of which is on the board. A board that supplies
    them gets this gate; a board that does not gets NOT_APPLICABLE from the
    registry before this function is even called, which is the honest answer
    rather than a geometry-only pass wearing a timing gate's name.
    """
    spec = ctx.manifest.get("timing.device_timing")
    required = ("source_clock_relationship", "source_tco_ps",
                "receiver_setup_ps", "receiver_hold_ps")
    res.measurements["declared_receivers"] = sorted(
        (spec.get("receivers") or {}).keys())
    if not spec.get("receivers"):
        return res.not_applicable(
            "timing.device_timing declares no receivers, so there is no "
            "endpoint whose setup and hold could be evaluated")
    missing = []
    for receiver, entry in sorted((spec.get("receivers") or {}).items()):
        absent = [f for f in required if entry.get(f) is None]
        if absent:
            missing.append({"receiver": receiver, "missing": absent,
                            "issue": "the receiver's timing model is "
                                     "incomplete, so no margin can be "
                                     "computed for it"})
    for entry in missing:
        res.finding(**entry)
    if missing:
        return res.errored(
            "{} declared receiver(s) have incomplete timing models; a setup "
            "and hold result derived from a partial model would be a number "
            "with no meaning".format(len(missing)))
    return res.errored(
        "device-aware setup and hold evaluation is not implemented in this "
        "release: the interconnect layer is in place and the device models are "
        "declared, but the margin arithmetic that combines them is not. This "
        "blocks rather than passes, because a board that asked for setup and "
        "hold checking has not received it")


# ---------------------------------------------------------------------------
# model provenance
# ---------------------------------------------------------------------------

@gate("PROV.TIMING_MODELS",
      "Every timing model file is present and inside the source closure",
      gate_class="design", requires=("timing.models",), order=350)
def timing_models(ctx, res):
    """A timing PASS may not rest on a file whose bytes nothing tracks.

    Material data, device models, S-parameter files and cable models are
    inputs to a result exactly as the board is. If one can change without
    changing the source closure, every committed timing report keeps looking
    fresh after the number it reports has stopped being true.
    """
    from .. import closure as closure_mod

    declared = ctx.manifest.get("timing.models")
    res.limit(ctx.manifest.constraint("timing.models", units="model role",
                                      cid="timing.models"))
    closure = closure_mod.source_closure(ctx.manifest,
                                         closure_mod.policy_for(ctx.manifest))
    res.measurements["source_closure_files"] = len(closure)

    root = ctx.manifest.resolve(".")
    problems, covered = [], {}
    for role, relative in sorted((declared or {}).items()):
        if relative is None:
            covered[role] = None
            continue
        full = ctx.manifest.resolve(relative)
        rel = os.path.relpath(full, root).replace("\\", "/")
        covered[role] = rel
        if not os.path.isfile(full):
            problems.append({
                "role": role, "file": relative,
                "issue": "a declared timing model file does not exist"})
            continue
        res.evidence_file(full, name=rel)
        if rel not in closure:
            problems.append({
                "role": role, "file": rel,
                "issue": "is a declared timing model but is not in the source "
                         "closure, so a change to it would leave every "
                         "committed timing result looking fresh"})
    if "<configuration>" not in closure:
        problems.append({"issue": "the manifest's configuration identity is "
                                  "not in the closure, so the timing policy "
                                  "itself is untracked"})
    res.measurements["models"] = covered
    for problem in problems[:40]:
        res.finding(**problem)
    if problems:
        return res.failed(
            "{} timing model input(s) are missing or untracked".format(
                len(problems)))
    stated = [r for r, v in covered.items() if v]
    return res.passed(
        "all {} declared timing model file(s) exist and are inside the "
        "{}-file source closure, alongside the manifest's configuration "
        "identity".format(len(stated), len(closure)))


# ---------------------------------------------------------------------------
# derivation provenance
# ---------------------------------------------------------------------------
