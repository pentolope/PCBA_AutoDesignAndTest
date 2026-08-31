"""Provenance, source authority and authoritative ERC/DRC gates.

Generic: every path, rule name, tolerance and claim pattern comes from the
manifest.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

from ..core import Status, gate, sha256_file


# ---------------------------------------------------------------------------
# source-of-truth authority
# ---------------------------------------------------------------------------

@gate("PROV.SOURCE_AUTHORITY", "KiCad is the sole design authority",
      requires=("source_authority",))
def source_authority(ctx, res):
    policy = ctx.manifest.get("source_authority")
    root = ctx.manifest.resolve(".")
    res.limit(ctx.manifest.constraint("source_authority.authority",
                                      units="policy", cid="source_authority"))

    claims = []
    for spec in policy.get("claim_scan", []):
        pattern = re.compile(spec["pattern"], re.I)
        for rel in _expand(root, spec["files"]):
            path = os.path.join(root, rel)
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in pattern.finditer(text):
                line = text[:m.start()].count("\n") + 1
                claims.append({"file": rel, "line": line,
                               "claim": spec["claim"],
                               "text": m.group(0)[:110]})
    by_claim = {}
    for c in claims:
        by_claim.setdefault(c["claim"], []).append(c)

    # Contradiction: two mutually exclusive claims both asserted somewhere.
    conflicts = []
    for pair in policy.get("mutually_exclusive_claims", []):
        present = [c for c in pair if c in by_claim]
        if len(present) > 1:
            conflicts.append(present)

    # A non-KiCad model must not be a data source for released artifacts.
    derived = []
    for spec in policy.get("forbidden_derivations", []):
        pattern = re.compile(spec["pattern"])
        for rel in _expand(root, spec["files"]):
            path = os.path.join(root, rel)
            try:
                text = open(path, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for m in pattern.finditer(text):
                derived.append({"file": rel,
                                "line": text[:m.start()].count("\n") + 1,
                                "issue": spec["issue"], "text": m.group(0)[:110]})

    for group in conflicts:
        members = []
        for claim in group:
            for c in by_claim[claim][:4]:
                members.append(f"{c['file']}:{c['line']}")
        res.finding(issue="contradictory source-of-truth claims",
                    claims=group, seen_at=members)
    for d in derived:
        res.finding(**d)

    res.measurements["claims_found"] = {k: len(v) for k, v in by_claim.items()}
    if conflicts or derived:
        return res.failed(
            f"{len(conflicts)} contradictory authority claim group(s), "
            f"{len(derived)} non-authoritative derivation(s) of released data")
    return res.passed("a single, consistent design authority is asserted")


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

    # The code that derives the offsets, checked as code that ran rather than
    # as a file lying at a path. Hashing an unused copy would prove nothing.
    import importlib
    executed = {}
    for name in spec.get("required_modules", []):
        key = "<executed>" + name
        if key not in closure:
            problems.append({
                "module": name,
                "issue": "is required to reproduce the offsets but is not in "
                         "the source closure, so the code that computed them "
                         "is untracked"})
            continue
        module = importlib.import_module(name)
        path = getattr(module, "__file__", "")
        digest = sha256_file(path) if path and os.path.isfile(path) else None
        executed[name] = path
        if digest != closure[key]:
            problems.append({
                "module": name, "file": path,
                "issue": "the module that is loaded is not the one the closure "
                         "recorded, so the recorded provenance is of code that "
                         "did not run",
                "closure": str(closure[key])[:16],
                "loaded": str(digest)[:16]})
    res.measurements["executed_implementation"] = {
        name: os.path.basename(path) for name, path in sorted(executed.items())}

    # And the derivation script, which travels inside the project: prove the
    # file that ran has the content the closure recorded.
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
