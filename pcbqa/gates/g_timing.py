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

Every threshold, path, endpoint group, model choice and material figure comes
from the board's manifest. Nothing in this module names a net, a designator, an
interface or a number.
"""

from __future__ import annotations

import os
import re

from ..core import Status, gate
from .. import electrical_path, geom, propagation, stackup_physical
from ..electrical_path import PathError
from ..propagation import PropagationError
from ..stackup_physical import StackupError


# ---------------------------------------------------------------------------
# shared analysis
# ---------------------------------------------------------------------------

class TimingAnalysis:
    """Everything the timing gates look at, built once per validation run."""

    def __init__(self, stackup, reference_layers, model, interfaces,
                 stackup_problems, backend):
        self.stackup = stackup
        self.reference_layers = reference_layers
        self.model = model
        self.interfaces = interfaces
        self.stackup_problems = stackup_problems
        self.backend = backend

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


def _identifier(kind, name):
    """Names that index into the manifest cannot contain a dot.

    A limit is traced back to the manifest by dotted key, so a name with a dot
    in it would cite a key that does not exist and CFG.THRESHOLD_PARITY would
    be unable to prove where the number came from.
    """
    if not isinstance(name, str) or not name or "." in name:
        raise PathError(
            "{} name {!r} is not usable: it indexes the manifest by dotted "
            "key, so it must be a non-empty string containing no dot".format(
                kind, name))
    return name


def analysis(ctx):
    """Resolve and measure every declared path. Cached across the gates."""
    def build():
        manifest = ctx.manifest
        board = ctx.board()
        geom.configure(manifest.geometry_profile()
                       .tolerance("polygon_chord_error_mm").value)

        stack, stack_problems = _physical_stackup(ctx)
        reference_layers = _reference_layers(ctx, board)
        spec = manifest.get("timing.propagation", {}) or {}
        backend = spec.get("backend", "analytic")
        if backend != "analytic":
            # Another backend is a deliberate choice a board makes, and this
            # gate does not silently fall back to the cheap one when the
            # expensive one is unavailable. `pcbqa.backends` decides.
            from .. import backends
            backends.require(backend, spec)
        model = propagation.PropagationModel(
            stack, reference_layers,
            model=spec.get("model", propagation.HAMMERSTAD),
            via_model=spec.get("via_delay_model", propagation.VIA_NONE),
            declared_layers=spec.get("declared_layers"))

        resolver = electrical_path.PathResolver(board, geom.pad_copper_polygon)
        interfaces = {}
        for name, declared in sorted(
                (manifest.get("timing.interfaces") or {}).items()):
            _identifier("interface", name)
            interfaces[name] = _interface(name, declared, resolver, model)
        return TimingAnalysis(stack, reference_layers, model, interfaces,
                              stack_problems, backend)
    return ctx.cache("timing_analysis", build)


def _interface(name, declared, resolver, model):
    """One interface: its declared paths, resolved and measured."""
    problems = []
    records = []
    routes = declared.get("routes")
    if not routes:
        raise PathError(
            "timing interface {!r} declares no routes, so it describes no "
            "path to measure".format(name))
    paths = electrical_path.paths_from_spec(routes)
    for path in paths:
        try:
            resolved = resolver.resolve(path)
        except PathError as exc:
            problems.append({"path": path.id, "issue": str(exc)})
            continue
        for concrete in resolved:
            records.append({
                "declared": path,
                "resolved": concrete,
                "delay": model.evaluate(concrete),
            })
    return {"spec": declared, "declared_paths": paths, "paths": records,
            "problems": problems}


def _physical_stackup(ctx):
    """Native KiCad stackup, then a board's supplement, then what is missing."""
    spec = ctx.manifest.get("timing.physical_stackup", {}) or {}
    native = stackup_physical.from_board_file(ctx.board_path())
    supplement_path = spec.get("supplement")
    if supplement_path:
        import json
        full = ctx.manifest.resolve(supplement_path)
        if not os.path.isfile(full):
            raise StackupError(
                "timing.physical_stackup.supplement names {}, which does not "
                "exist; a declared model file that is not there is a missing "
                "input, not an empty one".format(supplement_path))
        with open(full, encoding="utf-8") as handle:
            declared = stackup_physical.from_declaration(json.load(handle))
        stack = stackup_physical.merge(native, declared)
    else:
        stack = native
    return stack, stack.completeness()


def _reference_layers(ctx, board):
    """Which copper layers are reference planes, from poured copper.

    A board says which nets are references; the board file says which layers
    actually carry them. Neither half is guessed.
    """
    spec = ctx.manifest.get("timing.physical_stackup", {}) or {}
    nets = spec.get("reference_nets")
    if nets is None:
        return set()
    return stackup_physical.plane_layers(board, nets)


# ---------------------------------------------------------------------------
# path integrity
# ---------------------------------------------------------------------------

@gate("TIMING.PATH_INTEGRITY",
      "Declared electrical paths exist, end to end, across every component",
      requires=("timing.interfaces",), order=310)
def path_integrity(ctx, res):
    """Does the board contain the paths the timing policy describes?

    This is the geometric half of the analysis and it needs no material data at
    all, so it is the half that can always be answered. It proves each declared
    route resolves as one connected electrical path from its source pad to its
    destination pad, that each component crossing really does bridge the two
    nets either side of it, and that a route which is supposed to start at a
    driver has not quietly been measured from the far side of its series part.
    """
    state = analysis(ctx)
    problems = list(state.all_problems())
    summary = {}
    for name, interface in sorted(state.interfaces.items()):
        spec = interface["spec"]
        records = interface["paths"]
        rows = []
        for record in records:
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
            "resolved_paths": len(records),
            "paths": rows,
        }

        expected = spec.get("expected_path_count")
        if expected is not None:
            res.limit(ctx.manifest.constraint(
                "timing.interfaces.{}.expected_path_count".format(name),
                units="paths",
                cid="timing.{}.expected_path_count".format(name)))
            if len(records) != expected:
                problems.append({
                    "interface": name,
                    "issue": "the board resolves a different number of paths "
                             "than the interface declares",
                    "expected": expected, "resolved": len(records)})

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
    res.measurements["interfaces"] = summary
    res.measurements["paths_resolved"] = sum(
        len(i["paths"]) for i in state.interfaces.values())
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
      requires=("timing.physical_stackup",), order=305)
def physical_stackup(ctx, res):
    """Is enough known about the materials to model propagation?

    Deliberately separate from STACK.NATIVE_VS_MANIFEST, whose subject is
    copper layer order and plane assignment and whose semantics are unchanged.
    This one is about thicknesses, materials and permittivities: the physical
    stackup, which KiCad holds only if somebody filled it in.
    """
    state = analysis(ctx)
    stack = state.stackup
    res.measurements["physical_stackup"] = stack.to_dict()
    res.measurements["reference_plane_layers"] = sorted(state.reference_layers)
    res.measurements["insufficient_fields"] = state.stackup_problems
    required = res.limit(ctx.manifest.constraint(
        "timing.physical_stackup.require_complete", units="policy",
        cid="timing.physical_stackup.require_complete")).value

    if not state.reference_layers:
        nets = ctx.manifest.get("timing.physical_stackup.reference_nets", None)
        res.finding(issue="no copper layer carries a zone on any declared "
                          "reference net, so no trace on this board has an "
                          "identifiable reference plane",
                    reference_nets=nets)
    for problem in state.stackup_problems[:40]:
        res.finding(**problem)

    if state.stackup_problems or not state.reference_layers:
        if required:
            return res.failed(
                "the physical stackup is incomplete in {} respect(s) and this "
                "board requires it to be complete; source={}".format(
                    len(state.stackup_problems) + (
                        0 if state.reference_layers else 1), stack.source))
        return res.not_applicable(
            "this board does not require a complete physical stackup, and the "
            "one available ({}) is incomplete in {} respect(s); propagation "
            "delay is therefore not derivable and every gate that needs it "
            "says so rather than substituting a material".format(
                stack.source, len(state.stackup_problems)))
    return res.passed(
        "the physical stackup ({}) states a thickness for every copper layer "
        "and a thickness, material and permittivity for every dielectric, and "
        "{} reference plane layer(s) were found".format(
            stack.source, len(state.reference_layers)))


# ---------------------------------------------------------------------------
# interconnect delay
# ---------------------------------------------------------------------------

@gate("TIMING.INTERCONNECT_DELAY",
      "Passive PCB propagation delay of each declared path is within its limit",
      requires=("timing.interfaces",), order=320)
def interconnect_delay(ctx, res):
    """Board copper only. Not device-aware, and the report says so.

    A path whose limit is declared but whose delay cannot be derived is an
    ERROR, not a pass: an unevaluated requirement is not a satisfied one.
    """
    state = analysis(ctx)
    res.measurements["scope"] = (
        "passive PCB interconnect only: copper propagation and, where a model "
        "is declared, via vertical transit. Excludes driver output delay and "
        "output-to-output skew, package delay, receiver threshold behaviour "
        "and PVT variation.")
    rows, problems, unresolved = [], [], []
    limited = 0
    fidelities = set()
    for name, record in state.all_paths():
        delay = record["delay"]
        rows.append(_delay_row(name, delay))
        fidelities.add(delay["fidelity"])
        if delay["insufficient"]:
            unresolved.append({"interface": name, "path": delay["path"],
                               "destination": delay["destination"]["pad"],
                               "insufficient": delay["insufficient"][:3]})
        limit = _limit_for(ctx, res, name, "max_delay_ps", "ps")
        if limit is None:
            continue
        limited += 1
        if delay["delay_ps"] is None:
            problems.append({
                "interface": name, "path": delay["path"],
                "issue": "a delay limit is declared but the delay could not be "
                         "derived; the requirement is unevaluated, not met",
                "insufficient": delay["insufficient"][:3]})
        elif delay["delay_ps"] > limit.value:
            problems.append({
                "interface": name, "path": delay["path"],
                "issue": "passive interconnect delay exceeds the declared "
                         "limit",
                "delay_ps": delay["delay_ps"], "limit_ps": limit.value})
    res.measurements["paths"] = rows
    res.measurements["fidelity"] = sorted(fidelities)
    res.measurements["physical_stackup_source"] = state.stackup.source
    res.measurements["propagation_model"] = state.model.model
    res.measurements["via_delay_model"] = state.model.via_model
    res.measurements["backend"] = state.backend
    res.measurements["paths_without_derivable_delay"] = len(unresolved)
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
        "fidelity {}".format(limited, state.stackup.source,
                             ", ".join(sorted(fidelities))))


def _delay_row(interface, delay):
    return {
        "interface": interface,
        "path": delay["path"],
        "source": delay["source"]["pad"],
        "destination": delay["destination"]["pad"],
        "copper_length_mm": delay["copper_length_mm"],
        "length_by_layer_mm": delay["length_by_layer_mm"],
        "delay_ps": delay["delay_ps"],
        "fidelity": delay["fidelity"],
        "crosses": [t["reference"] for t in delay["component_traversals"]],
        "vias": len(delay["vias"]),
        "insufficient": delay["insufficient"],
    }


# ---------------------------------------------------------------------------
# interconnect skew
# ---------------------------------------------------------------------------

@gate("TIMING.INTERCONNECT_SKEW",
      "Passive PCB arrival spread within each declared endpoint group",
      requires=("timing.interfaces",), order=330)
def interconnect_skew(ctx, res):
    """The spread of passive interconnect delay across a group of endpoints.

    This is not total clock skew and must not be read as one. It omits the
    driver's own output-to-output skew, both packages, the receivers' threshold
    behaviour and PVT. On a fan-out buffer those terms are commonly larger than
    the copper term this gate measures.
    """
    state = analysis(ctx)
    res.measurements["scope"] = (
        "passive PCB interconnect only. This is NOT total clock arrival skew: "
        "it excludes driver output-to-output skew, driver and receiver package "
        "delay, receiver threshold behaviour and PVT variation.")
    groups, problems = [], []
    limited = 0
    for name, interface in sorted(state.interfaces.items()):
        declared = interface["spec"].get("groups") or {}
        for group_name, group in sorted(declared.items()):
            _identifier("endpoint group", group_name)
            members = _members(interface, group)
            record = _group_record(name, group_name, group, members)
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
                measured = record[field]
                if measured is None:
                    problems.append({
                        "interface": name, "group": group_name,
                        "issue": "a {} limit is declared but the spread could "
                                 "not be derived; the requirement is "
                                 "unevaluated, not met".format(units),
                        "insufficient": record["insufficient"][:3]})
                elif measured > limit.value:
                    problems.append({
                        "interface": name, "group": group_name,
                        "issue": "arrival spread exceeds the declared limit",
                        "measured": measured, "limit": limit.value,
                        "units": units,
                        "earliest": record["earliest"],
                        "latest": record["latest"]})
    res.measurements["groups"] = groups
    res.measurements["physical_stackup_source"] = state.stackup.source
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


def _group_record(interface, name, group, members):
    """One endpoint group's arrival spread, in time and in length."""
    endpoints = []
    delays, lengths, insufficient = [], [], []
    for record in members:
        delay = record["delay"]
        endpoints.append({
            "path": delay["path"],
            "destination": delay["destination"]["pad"],
            "source": delay["source"]["pad"],
            "copper_length_mm": delay["copper_length_mm"],
            "delay_ps": delay["delay_ps"],
        })
        lengths.append((delay["copper_length_mm"], delay["destination"]["pad"]))
        if delay["delay_ps"] is None:
            insufficient.extend(delay["insufficient"][:2])
        else:
            delays.append((delay["delay_ps"], delay["destination"]["pad"]))

    have_all_delays = bool(delays) and len(delays) == len(members)
    skew = (round(max(delays)[0] - min(delays)[0], 6)
            if have_all_delays else None)
    spread = (round(max(lengths)[0] - min(lengths)[0], 6)
              if lengths else None)
    ordered = delays if have_all_delays else lengths
    return {
        "interface": interface, "group": name,
        "description": group.get("description"),
        "members": len(members),
        "endpoints": endpoints,
        "skew_ps": skew,
        "length_spread_mm": spread,
        "earliest": min(ordered)[1] if ordered else None,
        "latest": max(ordered)[1] if ordered else None,
        "insufficient": insufficient,
        "measured_in": "ps" if have_all_delays else "mm",
    }


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
      requires=("timing.device_timing",), order=340)
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
    state = analysis(ctx)
    res.measurements["declared_receivers"] = sorted(
        (spec.get("receivers") or {}).keys())
    missing = []
    for receiver, entry in sorted((spec.get("receivers") or {}).items()):
        absent = [f for f in required if entry.get(f) is None]
        if absent:
            missing.append({"receiver": receiver, "missing": absent,
                            "issue": "the receiver's timing model is "
                                     "incomplete, so no margin can be "
                                     "computed for it"})
    if not spec.get("receivers"):
        return res.not_applicable(
            "timing.device_timing declares no receivers, so there is no "
            "endpoint whose setup and hold could be evaluated")
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
      requires=("timing.models",), order=350)
def timing_models(ctx, res):
    """A timing PASS may not rest on a file whose bytes nothing tracks.

    Material data, device models, S-parameter files and cable models are
    inputs to a result exactly as the board is. If one can change without
    changing the source closure, every committed timing report keeps looking
    fresh after the number it reports has stopped being true.
    """
    from .. import canonical, cleanroom

    declared = ctx.manifest.get("timing.models")
    res.limit(ctx.manifest.constraint("timing.models", units="model role",
                                      cid="timing.models"))
    policy = canonical.AttributePolicy.load(
        ctx.manifest.resolve(ctx.manifest.get("fixture.attributes_file")))
    closure = cleanroom.source_closure(ctx.manifest, policy)
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
