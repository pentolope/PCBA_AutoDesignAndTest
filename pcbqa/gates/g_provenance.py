"""Provenance gates: are the committed results about the design in the tree?

Generic: every path, rule name and tolerance comes from the manifest.
"""

from __future__ import annotations

import glob
import json
import os

from ..core import gate


def _expand(root, patterns):
    out = []
    for pat in patterns:
        for path in glob.glob(os.path.join(root, pat), recursive=True):
            if os.path.isfile(path):
                out.append(os.path.relpath(path, root).replace("\\", "/"))
    return sorted(set(out))


# ---------------------------------------------------------------------------
# report freshness
# ---------------------------------------------------------------------------

@gate("PROV.REPORT_FRESHNESS", "Committed reports match the current sources",
      requires=("reports", "reports.source_closure",
                "fixture.attributes_file"))
def report_freshness(ctx, res):
    """A report is fresh only if the inputs it was made from still hash the same.

    Timestamps prove nothing: a file can be touched, and a report can be newer
    than a source it never saw. So the gate recomputes the canonical digest of
    every input a check result depends on - the schematic and its sheets, the
    board, the project settings, the design rules and the manifest itself - and
    compares those *values* against the hashes bound into each report. A report
    that records no hashes cannot be tied to a revision and is stale by
    definition.
    """
    from .. import canonical
    from .. import closure as closure_mod

    spec = ctx.manifest.get("reports")
    root = ctx.manifest.resolve(".")
    res.limit(ctx.manifest.constraint("reports.source_closure",
                                      units="path glob",
                                      cid="reports.source_closure"))
    policy = closure_mod.policy_for(ctx.manifest)
    closure = closure_mod.source_closure(ctx.manifest, policy)
    closure_hash = closure_mod.closure_digest(closure)
    res.measurements["source_closure_files"] = len(closure)
    res.measurements["source_closure_sha256"] = closure_hash

    def identity(path):
        """The same canonical digest the binding side records."""
        rel = os.path.relpath(path, root).replace("\\", "/")
        return canonical.digest(path, policy.classify(rel))

    sources = {"pcb": ctx.board_path(), "schematic": ctx.schematic_path()}
    live = {k: identity(v) for k, v in sources.items() if os.path.isfile(v)}
    by_basename = {os.path.basename(v): identity(v)
                   for v in sources.values() if os.path.isfile(v)}
    res.measurements["source_sha256"] = {k: v[:16] for k, v in live.items()}

    hash_field = spec.get("source_hash_field", "source_sha256")
    closure_field = spec.get("closure_field", "source_closure_sha256")
    require_hash = spec.get("require_source_hash", True)

    stale = []
    examined = _expand(root, spec["files"])
    for rel in examined:
        path = os.path.join(root, rel)
        record = {"file": rel}
        try:
            doc = json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError) as exc:
            stale.append({**record, "issue": "unreadable: {}".format(exc)})
            continue
        declared = doc.get(spec.get("source_field", "source"))
        record["declares_source"] = declared
        record["date"] = doc.get(spec.get("date_field", "date"))
        declared_base = os.path.basename(str(declared)) if declared else None
        if declared and declared_base not in by_basename:
            stale.append({**record,
                          "issue": "declares a source file that is not a current "
                                   "design source"})
            continue

        recorded = doc.get(hash_field)
        if not recorded:
            if require_hash:
                stale.append({**record,
                              "issue": "records no source hash, so it cannot be "
                                       "tied to a specific revision"})
            continue
        expected = by_basename.get(declared_base)
        if expected is None:
            stale.append({**record,
                          "issue": "records a source hash but names no source it "
                                   "can be checked against"})
            continue
        if recorded != expected:
            stale.append({**record, "issue": "source hash bound into the report "
                                             "does not match the current source",
                          "recorded": recorded[:16], "recomputed": expected[:16]})
            continue

        recorded_closure = doc.get(closure_field)
        if not recorded_closure:
            stale.append({**record,
                          "issue": "records no source-closure hash, so a change to "
                                   "the project settings or design rules would "
                                   "leave it looking fresh"})
            continue
        if recorded_closure != closure_hash:
            entry = {**record,
                     "issue": "source closure changed since the report was made",
                     "recorded": str(recorded_closure)[:16],
                     "recomputed": closure_hash[:16]}
            bound = doc.get("source_closure")
            if isinstance(bound, dict):
                changed = sorted(k for k in set(bound) & set(closure)
                                 if bound[k] != closure[k])
                entry["changed_inputs"] = changed[:8]
                entry["added_inputs"] = sorted(set(closure) - set(bound))[:8]
                entry["removed_inputs"] = sorted(set(bound) - set(closure))[:8]
            stale.append(entry)

    # A gate that examines nothing passes for the wrong reason. The reports a
    # release must produce are named by the generation steps that produce
    # them, so "the glob found nothing" and "this board has no reports" are
    # told apart by configuration rather than by an empty result.
    from ..artifacts import leaf, report_files
    required = [leaf(name) for name in report_files(ctx.manifest)]
    seen = {leaf(rel) for rel in examined}
    res.measurements["reports_required"] = required
    res.measurements["reports_examined"] = len(examined)
    res.measurements["reports_examined_files"] = sorted(examined)
    for name in required:
        if name not in seen:
            stale.append({
                "file": name,
                "issue": "the release generates this check report, but "
                         "reports.files finds no such file to check; a "
                         "freshness gate that never sees a report cannot "
                         "report staleness"})
    if not examined:
        stale.append({"issue": "no reports were examined at all, so this gate "
                               "passed without checking anything",
                      "patterns": spec["files"]})

    for s_ in stale:
        res.finding(**s_)
    if stale:
        return res.failed("{} committed report(s) cannot be tied to the current "
                          "sources".format(len(stale)))
    return res.passed(
        "all {} committed report(s) bind the canonical digest of all {} source "
        "inputs, and every one still matches".format(len(examined),
                                                     len(closure)))


# ---------------------------------------------------------------------------
# reproduction inputs
# ---------------------------------------------------------------------------

@gate("PROV.SOURCE_CLOSURE",
      "Everything the result was derived from is inside the source closure",
      requires=("reports.source_closure",
                "release_generation.cpl_orientation.reproduction_inputs"))
def source_closure_covers_derivations(ctx, res):
    """A derived result is only as reproducible as its inputs are tracked.

    The orientation offsets are not read off the board; they are derived from
    frozen evidence by a script. If that script or that evidence can leave the
    closure without anything objecting, the release keeps claiming a
    provenance it no longer has - and the first sign would be a re-derivation
    that quietly produces something else.

    So the inputs are named in the manifest and checked to be closure members,
    both as globs and per registry entry. Losing a single evidence file, or
    dropping the glob that carries them, fails here rather than later.

    Which toolkit code derived them is not asked here. The toolkit enters the
    closure as its own commit, and a release requires that commit clean and
    pinned; the board's own derivation script is not toolkit code, so it is
    still checked by content below.
    """
    from .. import canonical
    from .. import closure as closure_mod
    from ..orientation import Registry

    spec = ctx.manifest.get(
        "release_generation.cpl_orientation.reproduction_inputs")
    res.limit(ctx.manifest.constraint(
        "release_generation.cpl_orientation.reproduction_inputs.required_globs",
        units="path glob",
        cid="cpl_orientation.reproduction_inputs.required_globs"))

    policy = closure_mod.policy_for(ctx.manifest)
    closure = closure_mod.source_closure(ctx.manifest, policy)
    res.measurements["source_closure_files"] = len(closure)

    root = ctx.manifest.resolve(".")
    problems = []
    covered = set()
    for pattern in spec.get("required_globs", []):
        matched = [os.path.relpath(p, root).replace("\\", "/")
                   for p in sorted(glob.glob(os.path.join(root, pattern),
                                             recursive=True))
                   if os.path.isfile(p)]
        if not matched:
            problems.append({
                "glob": pattern,
                "issue": "names no file at all, so whatever it was meant to "
                         "keep in the closure is gone"})
            continue
        for rel in matched:
            covered.add(rel)
            if rel not in closure:
                problems.append({
                    "file": rel,
                    "issue": "is a declared reproduction input but is not in "
                             "the source closure, so a change to it would "
                             "leave every committed result looking fresh"})

    # per entry, because a glob that still matches fourteen of fifteen files
    # is a glob that still matches
    registry = Registry(ctx.manifest.get("release_generation.cpl_orientation"))
    for lcsc, row in sorted(registry.entries.items()):
        for field in ("evidence_file", "raw_file"):
            rel = str(row.get(field, "")).strip()
            if not rel:
                problems.append({
                    "lcsc": lcsc,
                    "issue": "the entry names no {}, so its evidence cannot be "
                             "located let alone tracked".format(field)})
                continue
            covered.add(rel)
            if not os.path.isfile(os.path.join(root, rel)):
                problems.append({"lcsc": lcsc, "file": rel,
                                 "issue": "the entry's {} does not "
                                          "exist".format(field)})
            elif rel not in closure:
                problems.append({"lcsc": lcsc, "file": rel,
                                 "issue": "the entry's {} is outside the "
                                          "source closure".format(field)})

    if "<configuration>" not in closure:
        problems.append({"issue": "the manifest's configuration identity is "
                                  "not in the closure, so the registry's "
                                  "configuration is untracked"})

    # The derivation script travels inside the project, so the toolkit's own
    # commit says nothing about it: prove the file that ran has the content
    # the closure recorded.
    #
    # Asked of what CPL.ORIENTATION loaded, not of sys.modules. The import
    # cache is per process, and a process that had already loaded another
    # project's copy under the same module name would answer about that one -
    # which is a fact about the process, not about the release.
    from .g_orientation import LAST_DERIVATION
    tool_rel = next((rel for rel in covered
                     if rel.endswith("jlc_orientation.py")), None)
    if tool_rel and LAST_DERIVATION:
        ran = LAST_DERIVATION["file"]
        recorded = closure.get(tool_rel)
        actual = (canonical.digest(ran, policy.classify(tool_rel))
                  if os.path.isfile(ran) else None)
        if actual != recorded:
            problems.append({
                "file": tool_rel, "executed": ran,
                "issue": "the derivation script that ran is not the one the "
                         "closure recorded, so the recorded provenance is of "
                         "code that did not derive these offsets",
                "closure": str(recorded)[:16], "executed_sha256": str(actual)[:16]})
        res.measurements["executed_derivation_script"] = tool_rel

    res.measurements["reproduction_inputs"] = sorted(covered)
    res.measurements["reproduction_inputs_tracked"] = len(covered)
    for problem in problems[:40]:
        res.finding(**problem)
    if problems:
        return res.failed("{} reproduction input(s) are not tracked by the "
                          "source closure".format(len(problems)))
    return res.passed(
        "all {} declared reproduction inputs - the derivation script, its "
        "schema, and both evidence files for each of the {} registry entries - "
        "are inside the {}-file source closure".format(
            len(covered), len(registry.entries), len(closure)))


# ---------------------------------------------------------------------------
# routing provenance

@gate("ROUTE.PROVENANCE",
      "The routed board in the tree is the candidate that was accepted",
      requires=("routing.provenance",))
def routing_provenance(ctx, res):
    """Copper looks like copper: a board cannot show how it was produced.

    Routing is a search that writes candidates, may transform one after the
    router produced it, and promotes exactly one. Every step between "the
    router wrote this" and "this is the design" is a place where the tree can
    end up holding copper nothing judged - a later derivative the record does
    not describe, an attempt routed on top of the previous one, a failing
    candidate left behind when none was accepted, or an unnamed edit over the
    router's output. This gate makes the record prove the chain and agree with
    the board it claims to describe.
    """
    from .. import routing_record
    from ..core import sha256_file

    relative = ctx.manifest.get("routing.provenance")
    path = ctx.manifest.resolve(relative)
    if not os.path.isfile(path):
        return res.failed(
            "routing record {} is declared but absent".format(relative))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except ValueError as exc:
        return res.failed("routing record {} does not parse: {}".format(
            relative, exc))

    try:
        routing_record.validate(record)
    except routing_record.RoutingRecordError as exc:
        return res.failed("routing record {} is not acceptable: {}".format(
            relative, exc))

    board_sha256 = sha256_file(ctx.board_path())
    problems = routing_record.compare_to_board(record, board_sha256)
    for problem in problems:
        res.finding(**problem)
    if problems:
        return res.failed(
            "{} routing provenance problem(s): the board in the tree is not "
            "the accepted candidate".format(len(problems)),
            record=relative, board_sha256=board_sha256)

    declared = routing_record.transforms(record)
    return res.passed(
        "the adopted board is the accepted candidate of {} recorded "
        "attempt(s), every attempt started from the same declared source, and "
        "each of the {} post-router transform(s) states what it changed"
        .format(len(record["attempts"]), len(declared)),
        record=relative,
        board_sha256=board_sha256,
        attempts=len(record["attempts"]),
        accepted_attempt=record["accepted_attempt"],
        post_router_transforms=[stage["stage"] for stage in declared])
