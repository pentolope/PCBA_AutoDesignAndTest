"""Authoritative ERC and DRC gates, and the fixture-integrity gate.

Fail-closed. A gate here fails unless every one of these holds:

  * KiCad exited zero
  * the report parses against the schema this validator implements
  * no violation, warning, exclusion, unconnected item or parity issue remains
    after exact, hash-bound waivers
  * the run ignored no checks
  * the run requested every severity the manifest demands
  * the report names the source we asked it to check, and that source still
    hashes to the value recorded with the result
  * the command line carried every required option
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os

from ..core import Status, gate, sha256_file, utcnow
from .. import canonical, reports
from ..constraints import implementation_constant


# ---------------------------------------------------------------------------
# frozen fixture integrity, hashed canonically
# ---------------------------------------------------------------------------

@gate("PROV.FIXTURE_INTEGRITY", "Frozen fixture matches its canonical digests",
      requires=("fixture.hash_file",))
def fixture_integrity(ctx, res):
    """An exact inventory, not a spot check.

    Every recorded file must be present, of the recorded classification and of
    the recorded canonical digest; and every file present must be recorded. An
    unrecorded file is as much a change to the frozen copy as an edited one -
    it is how a stray export, an editor backup or a lock file gets in. Anything
    that is not a regular file (a symlink, a device node, a junction) fails
    outright: the digest of a link says nothing about what it points at.
    """
    hash_file = ctx.manifest.resolve(ctx.manifest.get("fixture.hash_file"))
    res.evidence_file(hash_file)
    meta = json.load(open(hash_file, encoding="utf-8"))
    policy_path = ctx.manifest.resolve(ctx.manifest.get("fixture.attributes_file"))
    policy = canonical.AttributePolicy.load(policy_path)
    reject = res.limit(ctx.manifest.constraint(
        "fixture.reject_globs", units="path glob",
        cid="fixture.reject_globs")).value
    res.measurements["digest_policy"] = meta.get("digest_policy")
    res.measurements["normalization_commit"] = meta.get("normalization_commit")
    res.measurements["files_recorded"] = len(meta["files"])

    base = os.path.join(os.path.dirname(hash_file), "project")
    changed, missing, reclassified, extra, badtype, rejected = [], [], [], [], [], []

    present = set()
    for dirpath, dirs, names in os.walk(base):
        for name in sorted(dirs):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                badtype.append({"file": os.path.relpath(full, base).replace(
                    "\\", "/"), "issue": "directory symlink in the frozen copy"})
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace("\\", "/")
            present.add(rel)
            if os.path.islink(full):
                badtype.append({"file": rel, "issue": "symbolic link, not a file"})
            elif not os.path.isfile(full):
                badtype.append({"file": rel, "issue": "not a regular file"})
            if _matches_any(rel, reject):
                rejected.append({"file": rel,
                                 "issue": "lock or scratch file has no place in a "
                                          "frozen design copy"})

    for rel, record in meta["files"].items():
        path = os.path.join(base, rel)
        if not os.path.isfile(path) or os.path.islink(path):
            missing.append(rel)
            continue
        kind = policy.classify(rel)
        if kind != record["kind"]:
            reclassified.append({"file": rel, "recorded": record["kind"],
                                 "now": kind,
                                 "issue": "gitattributes classification changed"})
            continue
        if canonical.digest(path, kind) != record["sha256"]:
            changed.append({"file": rel, "kind": kind,
                            "issue": "canonical content changed since freeze"})

    for rel in sorted(present - set(meta["files"])):
        extra.append({"file": rel,
                      "issue": "present in the frozen copy but not in the "
                               "inventory; the frozen set is exact"})

    res.measurements["files_present"] = len(present)
    for entry in changed + reclassified + extra + badtype + rejected:
        res.finding(**entry)
    for rel in missing:
        res.finding(file=rel, issue="missing from frozen copy")
    total = len(changed) + len(missing) + len(reclassified) + len(extra) \
        + len(badtype) + len(rejected)
    if total:
        return res.failed(
            "frozen fixture altered: {} changed, {} missing, {} extra, {} "
            "reclassified, {} not a regular file, {} rejected by policy".format(
                len(changed), len(missing), len(extra), len(reclassified),
                len(badtype), len(rejected)))
    return res.passed(
        "the frozen copy holds exactly the {} recorded files, each a regular "
        "file matching its canonical digest (text hashed over LF bytes, "
        "production output over raw bytes)".format(len(meta["files"])))


def _matches_any(rel, patterns):
    name = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
               for p in patterns)


# ---------------------------------------------------------------------------
# ERC / DRC
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# what a check must be told to do
#
# These are the validator's requirements, not a board's. A board manifest may
# ask for more, but it cannot ask for less: a project that could switch off
# `--schematic-parity` or drop `--severity-exclusions` could pass this gate
# while never having run the check the gate claims to have run.
# ---------------------------------------------------------------------------

REQUIRED_OPTIONS = {
    "erc": ("--severity-all", "--severity-exclusions", "--exit-code-violations"),
    "drc": ("--severity-all", "--severity-exclusions", "--all-track-errors",
            "--schematic-parity", "--refill-zones", "--save-board",
            "--exit-code-violations"),
}

# `--save-board` is only meaningful with `--refill-zones`, and refilling
# without saving would check a board nobody ships. KiCad enforces the pairing;
# so does this, so a future edit cannot half-remove it.
OPTION_PAIRS = {"--save-board": "--refill-zones"}

# Severities a run must have asked for. A run that omitted exclusions reports
# an excluded violation as nothing at all.
REQUIRED_SEVERITIES = ("error", "warning", "exclusion")

# Documented by KiCad for `--exit-code-violations`: the check ran and found
# something. Any other nonzero status means the check did not complete.
VIOLATIONS_EXIT_CODE = implementation_constant(
    5, "KiCad CLI's documented exit status for completed checks with findings")

# A waiver names the whole violation, not a corner of it: every affected item
# and where each one is. Waiving "the clearance error at these two places" is a
# reviewable statement; waiving "a clearance error involving this one pad" is
# not, because the other end could move anywhere and the waiver would hold.
WAIVER_REQUIRED_FIELDS = ("gate", "rule", "category", "items",
                          "reason", "reviewed_by", "reviewed_utc",
                          "approved_source_sha256", "approved_rules_sha256",
                          "approved_command_sha256", "approved_report_sha256")


def required_options(kind):
    """The mandatory option list for `erc` or `drc`, validated for coherence."""
    options = tuple(REQUIRED_OPTIONS[kind])
    for option, needs in OPTION_PAIRS.items():
        if option in options and needs not in options:
            raise ValueError(
                "{} requires {} but the required set omits it".format(
                    option, needs))
    return options


def _canonical_command(args):
    """The command, with absolute paths reduced to basenames.

    A waiver must not stop matching because the checkout moved, but it must
    stop matching if an option changed - the options are what decide which
    checks ran at all.
    """
    return " ".join(os.path.basename(a) if os.path.sep in a else a for a in args)


def _finding_key(f):
    """Everything about a finding that a reviewer would have looked at.

    The canonical item set carries every affected object *and* its position, so
    moving any one of them - not just the first - changes the digest and
    retires every waiver bound to it.
    """
    return {
        "category": f.get("category"), "rule": f.get("rule"),
        "severity": f.get("severity"),
        "items": f.get("canonical_items") or [],
        "description": f.get("description"),
    }


def _report_digest(findings):
    """A digest of what the reviewer actually saw.

    Deliberately not the digest of the report file: KiCad stamps a timestamp
    into every report, so a file digest would expire a waiver on every run and
    teach people to disable it. This digest covers the findings themselves, so
    it survives a re-run of an unchanged design and changes the moment a new
    finding appears or an existing one moves.
    """
    payload = json.dumps(sorted((json.dumps(_finding_key(f), sort_keys=True)
                                 for f in findings)), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _waiver_defects(w, tol):
    """Why a waiver record cannot be honoured. Empty means it is well formed."""
    bad = []
    if not isinstance(w, dict):
        return ["waiver is not an object"]
    for field in WAIVER_REQUIRED_FIELDS:
        if w.get(field) in (None, "", [], {}):
            bad.append("waiver omits {!r}".format(field))
    entries = w.get("items")
    if entries is not None:
        if not isinstance(entries, list) or not entries:
            bad.append("waiver `items` must be a non-empty list; a waiver that "
                       "names no affected item is a blanket waiver")
        else:
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    bad.append(f"waiver item {index} is not an object")
                    continue
                if not isinstance(entry.get("description"), str) \
                        or not entry["description"].strip():
                    bad.append(f"waiver item {index} names no object")
                loc = entry.get("location_mm")
                if (not isinstance(loc, (list, tuple)) or len(loc) != 2
                        or not all(isinstance(v, (int, float))
                                   and not isinstance(v, bool) for v in loc)
                        or not all(math.isfinite(v) for v in loc)):
                    bad.append(f"waiver item {index} needs a finite [x, y] "
                               f"location in mm")
    for field in ("rule", "category"):
        value = w.get(field)
        if isinstance(value, str) and value.strip() in ("*", "any", "all"):
            bad.append("waiver {!r} is a wildcard; waivers are exact".format(field))
    if tol is None:
        bad.append("no location tolerance is declared for waiver matching")
    return bad


def _waivers_for(ctx, gate_id, bindings, tol):
    """Waivers that are well formed, for this gate, and still bound to reality."""
    live, rejected = [], []
    for w in ctx.manifest.get("waivers", []):
        if not isinstance(w, dict) or w.get("gate") != gate_id:
            continue
        defects = _waiver_defects(w, tol)
        if defects:
            rejected.append({"waiver": w.get("id") or w.get("rule"),
                             "issue": "; ".join(defects)})
            continue
        stale = [name for name, current in bindings.items()
                 if w.get(name) != current]
        if stale:
            rejected.append({
                "waiver": w.get("id") or w.get("rule"),
                "issue": "waiver was approved against different inputs and no "
                         "longer applies",
                "changed": sorted(stale)})
            continue
        live.append(w)
    return live, rejected


def _waiver_items(entries):
    """A waiver's affected set, canonicalised the same way a finding's is."""
    return sorted(
        ({"description": e["description"],
          "x_mm": float(e["location_mm"][0]),
          "y_mm": float(e["location_mm"][1]),
          "uuid": e.get("uuid")} for e in entries),
        key=lambda e: (e["description"], e["uuid"] or "", e["x_mm"], e["y_mm"]))


def _placed(entries):
    """True only when every item has a real, finite position."""
    for entry in entries:
        x, y = entry.get("x_mm"), entry.get("y_mm")
        if x is None or y is None:
            return False
        # A non-finite coordinate makes every comparison false, which means
        # `abs(nan - wx) > tol` is false and the waiver would *match*. Schema
        # validation rejects such a report before it gets here; this is the
        # second lock on the same door, because the failure mode is a silent
        # waiver rather than a visible error.
        if not (math.isfinite(x) and math.isfinite(y)):
            return False
    return True


def _waived(finding, waivers, tol):
    """Exact match: same rule, same category, and the same complete item set.

    Every affected item must be accounted for, at the position the reviewer
    approved. One more item, one fewer, or one that moved, and the waiver no
    longer describes what was found.
    """
    found = finding.get("canonical_items")
    if found is None:
        found = sorted(finding.get("items") or [],
                       key=lambda e: (e.get("description", ""),
                                      e.get("uuid") or "",
                                      e.get("x_mm") or 0.0,
                                      e.get("y_mm") or 0.0))
    if not found or not _placed(found):
        return None
    for w in waivers:
        if w["rule"] != finding.get("rule"):
            continue
        if w["category"] != finding.get("category"):
            continue
        wanted = _waiver_items(w["items"])
        if len(wanted) != len(found):
            continue
        if all(_same_item(a, b, tol) for a, b in zip(wanted, found)):
            return w
    return None


def _same_item(waived, found, tol):
    if waived["description"] != found.get("description"):
        return False
    if waived.get("uuid") and waived["uuid"] != found.get("uuid"):
        return False
    return (abs(found["x_mm"] - waived["x_mm"]) <= tol
            and abs(found["y_mm"] - waived["y_mm"]) <= tol)


def _run(ctx, res, kind, gate_id):
    spec = ctx.manifest.get("checks.{}".format(kind), {}) or {}
    relative = (ctx.manifest.get("sources.schematic") if kind == "erc"
                else ctx.manifest.get("sources.pcb"))
    source = ctx.manifest.resolve(relative)
    if not os.path.isfile(source):
        return res.errored("{} source not found: {}".format(kind.upper(), source))
    source_hash = sha256_file(source)
    rules_hash = (sha256_file(ctx.project_path())
                  if os.path.isfile(ctx.project_path()) else None)

    try:
        working = ctx.check_path(relative)
    except OSError as exc:
        return res.errored(
            "could not prepare an isolated copy to check: {}".format(exc))

    try:
        options = list(required_options(kind))
    except ValueError as exc:
        return res.errored("incoherent required option set: {}".format(exc))
    extra = [f for f in spec.get("extra_flags", []) if f not in options]
    waiver_tol = res.limit(ctx.manifest.geometry_profile()
                           .tolerance("waiver_location_mm")).value

    out_json = os.path.join(ctx.workdir, "{}_authoritative.json".format(kind))
    args = [ctx.kicad_cli, ("sch" if kind == "erc" else "pcb"), kind,
            "--format", "json", "-o", out_json] + options + extra + [working]
    command = _canonical_command(args)
    command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()

    try:
        proc = ctx.run_tool(args)
    except Exception as exc:                    # timeout, missing binary, ...
        return res.errored(
            "{} could not be invoked: {}: {}".format(kind.upper(),
                                                     type(exc).__name__, exc))

    res.measurements.update({
        "command": command,
        "command_sha256": command_hash,
        "required_options": options,
        "exit_status": proc.returncode,
        "violations_exit_code": VIOLATIONS_EXIT_CODE,
        "source": os.path.basename(source),
        "source_sha256": source_hash,
        "checked_copy_sha256": (sha256_file(working)
                                if os.path.isfile(working) else None),
        "rules_sha256": rules_hash,
        "constraint_manifest_sha256": ctx.manifest.sha256,
        "kicad_version": ctx.kicad_version(),
        "generated_utc": utcnow(),
    })

    # The gate is worthless if the command it ran was not the command it says
    # it ran, so the recorded argv is re-checked rather than trusted.
    missing_options = [o for o in options if o not in args]
    if missing_options:
        return res.errored(
            "{} was invoked without required option(s) {}; the result cannot "
            "be interpreted".format(kind.upper(), missing_options))

    # A tool that could not do its job is an ERROR, never a FAIL: "the design
    # is bad" and "we do not know whether the design is bad" are different
    # answers, and only one of them can be argued with. No waiver applies to
    # any of the paths below - there is no finding to waive.
    if proc.returncode not in (0, VIOLATIONS_EXIT_CODE):
        return res.errored(
            "{} invocation failed with exit {} (documented codes: 0=clean, "
            "{}=violations found): {}".format(
                kind.upper(), proc.returncode, VIOLATIONS_EXIT_CODE,
                (proc.stderr or "").strip()[:300]))
    if not os.path.isfile(out_json):
        return res.errored(
            "{} exited {} but produced no report at all: {}".format(
                kind.upper(), proc.returncode, (proc.stderr or "").strip()[:300]))
    res.evidence_file(out_json)
    try:
        # Strict: NaN and Infinity are not JSON, and a report carrying one is
        # rejected here rather than producing a coordinate that compares false
        # against every tolerance and every waiver.
        doc = reports.load_report(out_json)
    except (ValueError, OSError) as exc:
        return res.errored("{} report is not readable JSON: {}".format(
            kind.upper(), exc))
    try:
        findings, meta = (reports.parse_erc(doc) if kind == "erc"
                          else reports.parse_drc(doc))
    except reports.ReportSchemaError as exc:
        # Before any waiver is looked at: a report we cannot validate must not
        # be waivable into a pass.
        return res.errored("unsupported {} report schema: {}".format(
            kind.upper(), exc))

    report_hash = _report_digest(findings)
    res.measurements["report_meta"] = meta
    res.measurements["coordinate_units"] = meta["coordinate_units"]
    res.measurements["locations_stored_in"] = "mm"
    res.measurements["report_findings_sha256"] = report_hash

    problems = []
    if os.path.basename(str(meta["source"])) != os.path.basename(source):
        problems.append({"issue": "report names a different source than we checked",
                         "report_source": meta["source"],
                         "checked": os.path.basename(source)})
    if meta["ignored_checks"]:
        problems.append({"issue": "run ignored one or more checks",
                         "ignored": meta["ignored_checks"]})
    absent_sev = [s for s in REQUIRED_SEVERITIES
                  if s not in meta["included_severities"]]
    if absent_sev:
        problems.append({"issue": "run did not include every required severity",
                         "missing": absent_sev,
                         "included": meta["included_severities"]})
    if proc.returncode == VIOLATIONS_EXIT_CODE and not findings:
        problems.append({"issue": "KiCad reported violations by exit code but the "
                                  "report lists none",
                         "exit_status": proc.returncode})
    if proc.returncode == 0 and findings:
        problems.append({"issue": "KiCad exited clean but the report lists "
                                  "findings",
                         "findings": len(findings)})

    bindings = {
        "approved_source_sha256": source_hash,
        "approved_rules_sha256": rules_hash,
        "approved_command_sha256": command_hash,
        "approved_report_sha256": report_hash,
    }
    waivers, rejected = _waivers_for(ctx, gate_id, bindings, waiver_tol)
    problems.extend(rejected)

    waived = 0
    counts = {}
    used = []
    for f in findings:
        counts[f["category"]] = counts.get(f["category"], 0) + 1
        match = _waived(f, waivers, waiver_tol)
        if match:
            waived += 1
            used.append({"rule": f["rule"], "reviewed_by": match["reviewed_by"]})
            continue
        problems.append(f)
    res.measurements["counts"] = counts
    res.measurements["waived"] = waived
    res.measurements["waivers_live"] = len(waivers)
    res.measurements["waivers_rejected"] = len(rejected)
    res.measurements["waivers_used"] = used

    for p in problems[:200]:
        res.finding(**p)
    if problems:
        return res.failed(
            "{} blocking {} condition(s); findings={}, exit={}, "
            "ignored_checks={}".format(len(problems), kind.upper(),
                                       counts or 0, proc.returncode,
                                       len(meta["ignored_checks"])))
    return res.passed(
        "{} clean: exit {}, schema {} validated, no ignored checks, severities "
        "{}, all {} required options used, source {}".format(
            kind.upper(), proc.returncode, meta["schema"],
            meta["included_severities"], len(options), source_hash[:12]))


# `requires` names the *sources* a check needs, never its options. A board that
# declares no schematic has not opted into ERC and reports NOT_APPLICABLE - and
# because the release profile lists both gates as mandatory, NOT_APPLICABLE
# still blocks a release. What a board cannot do is declare a schematic and
# then ask for a weaker check of it.
@gate("ERC.AUTHORITATIVE", "Fresh ERC on the exact final schematic",
      requires=("sources.schematic",))
def erc(ctx, res):
    return _run(ctx, res, "erc", "ERC.AUTHORITATIVE")


@gate("DRC.AUTHORITATIVE", "Fresh DRC on the exact final board",
      requires=("sources.pcb", "sources.project"))
def drc(ctx, res):
    return _run(ctx, res, "drc", "DRC.AUTHORITATIVE")


@gate("DRC.NO_SUPPRESSED_RULES", "No design rule is silently disabled",
      requires=("checks.drc.forbidden_severities",))
def suppressed(ctx, res):
    forbidden = res.limit(ctx.manifest.constraint(
        "checks.drc.forbidden_severities", units="severity",
        cid="checks.drc.forbidden_severities")).value
    allowed = set(ctx.manifest.get("checks.drc.permitted_ignored_rules", []))
    project = ctx.project_path()
    res.evidence_file(project)
    doc = json.load(open(project, encoding="utf-8"))
    board = doc.get("board", {})
    sev = board.get("design_settings", {}).get("rule_severities", {})
    offenders = sorted(k for k, v in sev.items()
                       if v in forbidden and k not in allowed)
    exclusions = board.get("drc_exclusions") or []
    erc_sev = doc.get("erc", {}).get("rule_severities", {})
    erc_off = sorted(k for k, v in erc_sev.items()
                     if v in forbidden and k not in allowed)
    erc_excl = doc.get("erc", {}).get("erc_exclusions") or []
    res.measurements.update({
        "drc_ignored_rules": offenders, "drc_exclusions": len(exclusions),
        "erc_ignored_rules": erc_off, "erc_exclusions": len(erc_excl),
    })
    for rule in offenders:
        res.finding(domain="drc", rule=rule, severity=sev[rule],
                    issue="rule disabled without an approved waiver")
    for rule in erc_off:
        res.finding(domain="erc", rule=rule, severity=erc_sev[rule],
                    issue="rule disabled without an approved waiver")
    for item in exclusions + erc_excl:
        res.finding(issue="stored exclusion", detail=str(item)[:120])
    if offenders or erc_off or exclusions or erc_excl:
        return res.failed(
            f"{len(offenders)} DRC and {len(erc_off)} ERC rule(s) disabled, "
            f"{len(exclusions) + len(erc_excl)} stored exclusion(s)")
    return res.passed("no rule is disabled outside the approved list")


# ---------------------------------------------------------------------------
# design-rule floors

@gate("DRC.CONSTRAINT_FLOOR",
      "No design rule is weaker than the floor the board declared",
      requires=("checks.drc.constraint_floor", "sources.project"))
def drc_constraint_floor(ctx, res):
    """A disabled rule is loud; a loosened one is silent.

    DRC.NO_SUPPRESSED_RULES catches a check switched off. It cannot catch a
    check left on and quietly given a smaller number to measure against - and
    that is the easier mistake to make, because tools that write a project
    back (routers, plugins, an editor session) may lower a floor to match what
    they produced and every checker afterwards, KiCad's own DRC included,
    grades the board against the weakened value and reads clean.

    The board declares its floors; this gate proves the project still carries
    them. Declared values are minimums: a project value below one is a
    weakened rule, an absent one cannot be proven at all.
    """
    floor = ctx.manifest.get("checks.drc.constraint_floor")
    if not isinstance(floor, dict) or not floor:
        return res.errored("checks.drc.constraint_floor declares no limits")

    project_path = ctx.project_path()
    if not os.path.isfile(project_path):
        return res.failed("project not found: {}".format(project_path))
    with open(project_path, "r", encoding="utf-8") as handle:
        project = json.load(handle)

    rules = (project.get("board", {}).get("design_settings", {})
             .get("rules", {}))
    classes = {entry.get("name"): entry
               for entry in project.get("net_settings", {}).get("classes", [])}

    checked = 0
    for name, declared in sorted((floor.get("rules") or {}).items()):
        checked += 1
        limit = res.limit(ctx.manifest.constraint(
            "checks.drc.constraint_floor.rules." + name, units="mm",
            cid="drc.floor." + name))
        _compare_floor(res, "rules." + name, rules.get(name), limit.value)
    for class_name, wanted in sorted((floor.get("net_classes") or {}).items()):
        entry = classes.get(class_name)
        if entry is None:
            res.finding(issue="net class absent from the project",
                        net_class=class_name)
            continue
        for name, declared in sorted(wanted.items()):
            checked += 1
            limit = res.limit(ctx.manifest.constraint(
                "checks.drc.constraint_floor.net_classes.{}.{}".format(
                    class_name, name),
                units="mm",
                cid="drc.floor.{}.{}".format(class_name, name)))
            _compare_floor(res, "net_classes.{}.{}".format(class_name, name),
                           entry.get(name), limit.value)

    if res.findings:
        return res.failed(
            "{} of {} declared design-rule floor(s) are missing or weaker in "
            "the project".format(len(res.findings), checked),
            project=os.path.basename(project_path), checked=checked)
    return res.passed(
        "every one of the {} declared design-rule floors is still carried by "
        "the project".format(checked),
        project=os.path.basename(project_path), checked=checked)


def _compare_floor(res, key, effective, declared):
    if effective is None:
        res.finding(issue="rule absent from the project, so the floor cannot "
                          "be proven", rule=key, declared_mm=declared)
        return
    if not isinstance(effective, (int, float)) or isinstance(effective, bool):
        res.finding(issue="rule is not a number", rule=key,
                    effective=effective, declared_mm=declared)
        return
    if effective < declared:
        res.finding(issue="rule is weaker than the declared floor", rule=key,
                    effective_mm=effective, declared_mm=declared)
