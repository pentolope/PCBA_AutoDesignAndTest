"""Contract, BOM/CPL parity, archive and constraint-parity gates."""

from __future__ import annotations

import ast
import csv
import glob
import io
import json
import os
import fnmatch
import re
import zipfile
from collections import Counter

from ..core import Status, gate, sha256_file, sha256_bytes
from .. import gerber, geom
from ..rules import NetTopologyRule, ConnectorContractRule, PlacementRule


def _docs(ctx, patterns):
    root = ctx.manifest.resolve(".")
    out = {}
    for pat in patterns:
        for path in glob.glob(os.path.join(root, pat), recursive=True):
            if os.path.isfile(path):
                rel = os.path.relpath(path, root).replace("\\", "/")
                try:
                    out[rel] = open(path, encoding="utf-8", errors="ignore").read()
                except OSError:
                    pass
    return out


# ---------------------------------------------------------------------------
# net topology
# ---------------------------------------------------------------------------

@gate("NET.TOPOLOGY", "Critical-net topology and length matching",
      requires=("net_topology.rules",),
      # These lengths are computed, not read: a shortest connected walk over
      # copper polygons is an answer this code produces, so a board pinning
      # its inputs has to pin this too.
      derives=("pcbqa.connectivity", "pcbqa.rules", "pcbqa.gates.g_contracts"))
def net_topology(ctx, res):
    board = ctx.board()
    # Connectivity is decided by whether copper shapes actually intersect, so
    # the chord error used to approximate them is part of the answer.
    geom.configure(res.limit(ctx.manifest.geometry_profile()
                             .tolerance("polygon_chord_error_mm")).value)
    problems = []
    for index, spec in enumerate(ctx.manifest.get("net_topology.rules")):
        rule = NetTopologyRule(spec)
        measured, issues = rule.evaluate(board, geom.pad_copper_polygon)
        issues += rule.check_limits(measured)
        res.measurements[spec["id"]] = {
            "nets": len(measured),
            "per_net": [{"net": m["net"], "max_path_mm": m["max_path_mm"],
                         "min_path_mm": m["min_path_mm"], "vias": m["vias"],
                         "layers": m["layers"], "branch_points": m["branch_points"],
                         "total_track_copper_mm": m["total_track_copper_mm"]}
                        for m in measured],
        }
        maxima = [m["max_path_mm"] for m in measured if m["max_path_mm"] is not None]
        if maxima:
            res.measurements[spec["id"]]["spread_mm"] = round(max(maxima) - min(maxima), 3)
        for key, units in (("max_spread_mm", "mm"), ("max_vias_per_net", "vias")):
            if key in spec:
                res.limit(ctx.manifest.constraint(
                    f"net_topology.rules.{index}.{key}", units=units,
                    cid=f"net_topology.{spec['id']}.{key}"))
        for issue in issues:
            problems.append({**issue, "rule": spec["id"]})
    for p in problems[:60]:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} critical-net topology violation(s)")
    return res.passed("every critical net meets its topology contract")


# ---------------------------------------------------------------------------
# connector contract
# ---------------------------------------------------------------------------

@gate("CONTRACT.CONNECTOR", "Connector mating contract is consistent everywhere",
      requires=("connector_contracts",))
def connector_contract(ctx, res):
    tokens = ctx.manifest.get("connector_gender_tokens")
    doc_texts = _docs(ctx, ctx.manifest.get("documentation_globs"))
    board = ctx.board()
    problems = []
    for spec in ctx.manifest.get("connector_contracts"):
        rule = ConnectorContractRule(spec, tokens)
        issues, facts = rule.evaluate(board, doc_texts)
        res.measurements[spec["id"]] = facts
        for issue in issues:
            problems.append({**issue, "contract": spec["id"]})
    for p in problems[:60]:
        res.finding(**p)
    if problems:
        kinds = Counter(p["issue"] for p in problems)
        return res.failed("; ".join(f"{v}x {k}" for k, v in kinds.most_common()))
    return res.passed("every connector matches its contract in board, model and docs")


# ---------------------------------------------------------------------------
# placement contract
# ---------------------------------------------------------------------------

@gate("CONTRACT.PLACEMENT", "Component placement and orientation contracts",
      requires=("placement_rules",))
def placement_contract(ctx, res):
    origin = ctx.manifest.get("board_origin_mm")
    board = ctx.board()
    problems = []
    for spec in ctx.manifest.get("placement_rules"):
        rule = PlacementRule(spec)
        measured, issues = rule.evaluate(board, origin)
        radii = [m["radius_mm"] for m in measured]
        res.measurements[spec["id"]] = {
            "members": len(measured),
            "radius_min_mm": min(radii) if radii else None,
            "radius_max_mm": max(radii) if radii else None,
        }
        for issue in issues:
            problems.append({**issue, "rule": spec["id"]})
    for p in problems[:60]:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} placement contract violation(s)")
    return res.passed("every placement contract holds")


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------

@gate("ARCH.CONTENTS", "Production archive contains only approved fabrication data",
      requires=("archive.zip", "archive.allow"))
def archive_contents(ctx, res):
    zpath = ctx.manifest.resolve(ctx.manifest.get("archive.zip"))
    if not os.path.isfile(zpath):
        return res.errored(f"archive not found: {zpath}")
    res.evidence_file(zpath)
    allow = ctx.manifest.get("archive.allow")
    deny = ctx.manifest.get("archive.deny", [])
    res.limit(ctx.manifest.constraint("archive.allow", units="file function",
                                      cid="archive.allow"))

    problems = []
    seen = Counter()
    with zipfile.ZipFile(zpath) as zf:
        names = sorted(zf.namelist())
        res.measurements["entries"] = len(names)
        for name in names:
            data = zf.read(name)
            kind, function, empty = _classify(name, data)
            rule = archive_rule(allow, name, function)
            banned = archive_rule(deny, name, function)
            seen[rule_key(rule) if rule else name] += 1
            if banned:
                problems.append({"entry": name, "issue": banned["reason"]})
                continue
            if rule is None:
                problems.append({"entry": name, "file_function": function,
                                 "issue": "not on the archive allowlist"})
                continue
            # The name declares the role; the content has to back it up. A
            # file named as a copper layer that does not parse as a Gerber,
            # or parses but
            # draws nothing, is exactly the failure this archive exists to
            # make impossible.
            # Where a board identifies its files by name, the name is only a
            # claim; the content still has to be the kind of thing it says.
            role = rule.get("role")
            if role:
                expected_kind = "drill" if role == "drill" else "gerber"
                if kind != expected_kind:
                    problems.append({"entry": name, "role": role,
                                     "issue": "content is {}, not {}".format(
                                         kind, expected_kind)})
                    continue
            if rule.get("require_payload", False) and empty:
                problems.append({"entry": name, "role": role,
                                 "issue": "layer is present but carries no geometry"})
    for rule in allow:
        need = rule.get("min_count")
        if need is not None and seen[rule_key(rule)] < need:
            problems.append({"artifact": rule_key(rule),
                             "issue": "required artifact missing from the archive",
                             "expected_min": need,
                             "found": seen[rule_key(rule)]})
    res.measurements["by_artifact"] = dict(seen)
    for p in problems[:60]:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} archive content problem(s)")
    return res.passed("archive contains exactly the approved fabrication artifacts")


def archive_rule(rules, name, function):
    """The allow/deny entry covering an archive member, or None.

    A board identifies its fabrication data one of two ways. Most declare a
    Gerber X2 file function, which is what the format was designed for. A
    board whose fabricator does not read X2 - and some do not - has to say
    what each file is in its *name* instead, and declares `file` rather than
    `file_function`. Both are honoured, so a board moving to filenames does
    not silently disarm every other board's archive check.
    """
    for rule in rules:
        if "file" in rule:
            if fnmatch.fnmatch(name, rule["file"]):
                return rule
        elif rule.get("file_function") == function:
            return rule
        elif "file_glob" in rule and fnmatch.fnmatch(name, rule["file_glob"]):
            return rule
    return None


def rule_key(rule):
    """How an allow/deny entry names what it covers, for counting and errors."""
    return rule.get("file") or rule.get("file_glob") or rule["file_function"]


def _classify(name, data):
    """What kind of file this is, from its content alone.

    Deliberately blind to the filename: the caller decides what a file is
    *supposed* to be from its name, and this says what it actually is, so the
    two can be compared. The X2 file function is reported when present but is
    not relied on - this board's export switches X2 off, because the fab does
    not read it.
    """
    text = data.decode("utf-8", errors="ignore")
    if text.lstrip().startswith("M48") or "\nM48" in text[:200]:
        m = re.search(r"TF\.FileFunction,([^\r\n*]+)", text)
        fn = (m.group(1) if m else "Drill,Unknown").strip()
        plated = fn.split(",")[0].lower()
        return "drill", f"Drill/{plated}", not re.search(r"^X-?[\d.]+Y", text, re.M)
    if "%FSLA" in text or "%MOMM" in text:
        m = re.search(r"%TF\.FileFunction,([^*]+)\*%", text)
        fn = (m.group(1) if m else "Unknown").strip()
        has_geometry = bool(re.search(r"D0?[13]\*", text))
        return "gerber", fn, not has_geometry
    if name.lower().endswith(".gbrjob") or '"GeneralSpecs"' in text:
        return "job", "JobFile", not text.strip()
    return "other", f"Unclassified:{os.path.splitext(name)[1] or 'none'}", not data


@gate("ARCH.PROVENANCE",
      "The committed fabrication artifacts are the ones the record describes",
      requires=("artifacts.fabrication_manifest",))
def archive_provenance(ctx, res):
    """Bind the committed artifacts to the design they were generated from.

    Three separate claims, each checkable without trusting the others:

    * the record carries the provenance a release needs at all;
    * every artifact it names is present with exactly the digest it names, and
      nothing sits in the release directories that it does not name;
    * the source closure it was generated against is the closure of the design
      as it stands now, so artifacts left behind by an earlier design cannot be
      released beside sources that have moved on.
    """
    from .. import artifacts as artifact_set
    from .. import closure as closure_mod

    path = ctx.manifest.resolve(
        ctx.manifest.get("artifacts.fabrication_manifest"))
    res.measurements["fabrication_manifest"] = os.path.basename(path)
    if not os.path.isfile(path):
        res.finding(file=os.path.basename(path),
                    issue="the design carries no fabrication record, so "
                          "nothing ties its committed artifacts to it")
        return res.failed("no fabrication manifest at " + path)
    res.evidence_file(path)
    try:
        record = json.load(open(path, encoding="utf-8"))
    except ValueError as exc:
        return res.errored("fabrication manifest is not readable JSON: "
                           "{}".format(exc))

    problems = []
    version = record.get("schema_version")
    if version != artifact_set.FABRICATION_SCHEMA_VERSION:
        problems.append({"issue": "fabrication manifest declares schema "
                                  "version {!r}; this validator implements "
                                  "{}".format(
                                      version,
                                      artifact_set.FABRICATION_SCHEMA_VERSION)})
    for field in artifact_set.REQUIRED_PROVENANCE:
        if not record.get(field):
            problems.append({"field": field,
                             "issue": "the fabrication manifest records no "
                                      "such provenance"})

    recorded = record.get("artifacts") or {}
    res.measurements["artifacts_recorded"] = len(recorded)
    base = os.path.dirname(path)
    for name, digest in sorted(recorded.items()):
        full = os.path.join(base, name)
        if not os.path.isfile(full):
            problems.append({"artifact": name,
                             "issue": "recorded by the fabrication manifest "
                                      "but not present"})
            continue
        actual = sha256_file(full)
        if actual != digest:
            problems.append({"artifact": name,
                             "issue": "has changed since its digest was "
                                      "recorded",
                             "recorded": str(digest)[:16],
                             "actual": actual[:16]})

    present = artifact_set.generated_files(ctx.manifest)
    res.measurements["artifacts_present"] = len(present)
    for full in present:
        name = artifact_set.record_key(ctx.manifest, full)
        if name not in recorded:
            problems.append({"artifact": name,
                             "issue": "is in the release directory but not in "
                                      "the fabrication manifest, so it is left "
                                      "over from another build or was added by "
                                      "hand"})

    try:
        _entries, now = closure_mod.current(ctx.manifest)
    except Exception as exc:                                   # noqa: BLE001
        return res.errored("the current source closure could not be computed, "
                           "so artifact staleness cannot be decided: "
                           "{}: {}".format(type(exc).__name__, exc))
    was = record.get("source_closure_sha256")
    res.measurements["source_closure_sha256"] = now
    res.measurements["recorded_source_closure_sha256"] = was
    if was != now:
        problems.append({
            "issue": "the committed artifacts were generated from a different "
                     "design than the one in the tree; rebuild before "
                     "releasing",
            "recorded": str(was)[:16], "recomputed": now[:16]})

    for problem in problems[:40]:
        res.finding(**problem)
    if problems:
        return res.failed("{} fabrication-provenance problem(s)".format(
            len(problems)))
    return res.passed(
        "all {} committed artifact(s) match the digests recorded for them, "
        "nothing else is in the release directories, and they were generated "
        "from the source closure the design still has".format(len(recorded)))


# ---------------------------------------------------------------------------
# constraint / checker parity
# ---------------------------------------------------------------------------

@gate("CFG.THRESHOLD_PARITY", "Every gate limit is a typed manifest constraint",
      requires=("constraint_parity",), order=900)
def threshold_parity(ctx, res):
    """Prove each applied limit resolves to the manifest key it names."""
    applied = ctx.cache("applied_limits", dict)
    res.measurements["limits_applied"] = len(applied)
    res.measurements["by_kind"] = {}
    problems = []
    for name, record in applied.items():
        kind = record.get("kind")
        res.measurements["by_kind"][kind] = res.measurements["by_kind"].get(kind, 0) + 1
        key = record.get("manifest_key")
        if not key or not record.get("provenance"):
            problems.append({"limit": name, "issue": "limit carries no provenance"})
            continue
        if record.get("units") is None:
            problems.append({"limit": name, "issue": "limit declares no units"})
        if not ctx.manifest.has(key):
            problems.append({"limit": name, "key": key,
                             "issue": "limit cites a manifest key that does not exist"})
            continue
        if _leaf(record["value"]) != _leaf(ctx.manifest.get(key)):
            problems.append({"limit": name, "key": key,
                             "issue": "gate applied a value that is not the "
                                      "manifest value",
                             "applied": record["value"],
                             "manifest": ctx.manifest.get(key)})
    for p in problems:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} gate limit(s) are not typed manifest "
                          f"constraints")
    return res.passed(f"all {len(applied)} applied limits are typed constraints "
                      f"traced to the manifest")


def _leaf(value):
    if isinstance(value, list):
        return [_leaf(v) for v in value]
    if isinstance(value, dict):
        return {k: _leaf(v) for k, v in sorted(value.items())}
    return value


@gate("CFG.NO_RIVAL_THRESHOLDS", "No checker outside the manifest defines its own limits",
      requires=("constraint_parity.rival_scan",))
def rival_thresholds(ctx, res):
    spec = ctx.manifest.get("constraint_parity.rival_scan")
    root = ctx.manifest.resolve(".")
    watched = spec["watched_constants"]
    res.limit(ctx.manifest.constraint(
        "constraint_parity.rival_scan.watched_constants", units="constant name",
        cid="constraint_parity.watched_constants"))
    problems = []
    for pat in spec["files"]:
        for path in sorted(glob.glob(os.path.join(root, pat), recursive=True)):
            rel = os.path.relpath(path, root).replace("\\", "/")
            try:
                source = open(path, encoding="utf-8", errors="ignore").read()
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    entry = watched.get(target.id)
                    if entry is None:
                        continue
                    try:
                        value = ast.literal_eval(node.value)
                    except ValueError:
                        continue
                    canonical = ctx.manifest.get(entry["manifest_key"])
                    if _leaf(value) != _leaf(canonical):
                        problems.append({
                            "file": rel, "line": node.lineno, "constant": target.id,
                            "declares": value, "manifest_key": entry["manifest_key"],
                            "manifest_value": canonical,
                            "issue": "a second, divergent copy of a canonical limit"})
    for p in problems:
        res.finding(**p)
    if problems:
        return res.failed(f"{len(problems)} rival threshold definition(s) outside "
                          f"the canonical manifest")
    return res.passed("no rival threshold definitions found")
