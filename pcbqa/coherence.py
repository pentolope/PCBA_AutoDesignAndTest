"""Is this directory one release, or several releases piled together?

Every file in a release package makes claims about the others. `MANIFEST.md`
names the archive's digest, the validation report names the digest of the
archive it validated, the ERC and DRC reports name the source closure they were
produced against, and `UNSEALED.txt` says the whole lot came from one run. Each
of those is checkable, and if they are never checked together a package can
drift into a state where every individual file is well formed and the set is a
lie - a report describing artifacts that are not there, beside artifacts no
report describes.

That is not hypothetical: it is what happened here. A validation report was
refreshed on its own to describe a newer validator, and left sitting beside an
archive from an older run, with a note explaining the discrepancy. A note is
not a check. This module is the check.

The ordering problem, and the receipt
-------------------------------------
A validation report cannot contain its own digest, and it is written after
validation finishes, so validation cannot check it. Rather than pretend
otherwise, the release writes `RECEIPT.json` last: an inventory of every other
file in the package, taken once the package is complete and every mandatory
gate has passed. The receipt is what makes the set checkable afterwards, and it
is deliberately not part of its own inventory.

So there are two moments:

  * during a release, the package is a candidate - archive, manifest, BOM and
    CPL exist, the reports do not yet - and what can be checked is that the
    manifest describes the artifacts beside it;
  * after installation, the package is complete, and everything below applies.
"""

from __future__ import annotations

import json
import os
import re

from .core import sha256_file, utcnow

RECEIPT_NAME = "RECEIPT.json"
#: Where a published package keeps the check reports, relative to itself.
REPORTS_DIR = "reports"

#: What a complete package must contain, by role. The names themselves come
#: from the manifest, because they are a property of the board and not of this
#: framework.
COMPLETE_MEMBERS = ("archive", "manifest", "bom", "cpl",
                    "validation", "clean_room", "unsealed")

_SHA256 = re.compile(r"\b([0-9a-f]{64})\b")


def inventory(root, skip=(RECEIPT_NAME,)):
    """{relative path: sha256} for every file under root, raw bytes."""
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            if rel in skip:
                continue
            out[rel] = sha256_file(path)
    return out


def write_receipt(root, meta):
    """Record what the package contains, once it is complete.

    Written after the last artifact and after the gates have passed, which is
    the only moment at which an inventory of the package is true. It carries
    no digest of itself and makes no claim to; what it proves is that the
    files beside it are the ones the run produced, all of them and nothing
    else.
    """
    document = dict(meta)
    document["schema_version"] = 1
    document["written_utc"] = utcnow()
    document["what_is_hashed"] = (
        "sha256 of the raw bytes of every file in this directory except "
        "RECEIPT.json itself, taken after the package was assembled and after "
        "every mandatory gate passed. Paths are relative to this directory.")
    document["files"] = inventory(root)
    path = os.path.join(root, RECEIPT_NAME)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _gate(document, gate_id):
    for entry in document.get("gates", []):
        if entry.get("gate") == gate_id:
            return entry
    return None


def leaf(path):
    """The last segment of a path, whichever separator wrote it.

    os.path.basename answers with the host's separator rules, so a report
    written on Windows reads as one long filename on Linux and the file it
    names can never be found. The recorded path is data, not a path on this
    machine, and is split on both separators everywhere.
    """
    return re.split(r"[\\/]", str(path or ""))[-1]


def _evidence_digest(gate, member):
    """The digest a gate recorded for the package member it examined.

    Matched on the stable `name` a gate records where there is one, and
    otherwise on the tail of the recorded path. The comparison of digests
    themselves is untouched: this only decides which record is about which
    file.
    """
    for item in (gate or {}).get("evidence", []):
        recorded = item.get("name") or item.get("path")
        if leaf(recorded) == leaf(member):
            return item.get("sha256")
    return None


def _manifest_claims(text):
    """{basename: sha256} and the source closure, as MANIFEST.md states them."""
    artifacts, closure = {}, None
    for line in text.splitlines():
        if "source closure sha256" in line:
            found = _SHA256.search(line)
            closure = found.group(1) if found else None
            continue
        found = _SHA256.search(line)
        name = re.search(r"`([^`]+)`", line)
        if found and name and line.lstrip().startswith("-"):
            artifacts[os.path.basename(name.group(1))] = found.group(1)
    return artifacts, closure


def check(root, names, require_receipt=True):
    """Cross-check every claim the package makes about itself.

    Returns (problems, facts). A problem is a dict with an `issue`; facts are
    the values that agreed, for the report to carry.
    """
    problems, facts = [], {}

    def missing(rel, why):
        problems.append({"file": rel, "issue": why})

    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return [{"file": root, "issue": "the release directory does not exist"}], facts

    present = inventory(root)
    facts["files_in_package"] = len(present)

    archive = names["archive"]
    manifest_name = names["manifest"]
    for role in ("archive", "manifest", "bom", "cpl"):
        if names[role] not in present:
            missing(names[role], "a release must contain its {}".format(role))
    if problems:
        return problems, facts

    # 1. MANIFEST.md describes the artifacts that are actually beside it.
    with open(os.path.join(root, manifest_name), encoding="utf-8") as fh:
        claimed, manifest_closure = _manifest_claims(fh.read())
    facts["manifest_source_closure_sha256"] = manifest_closure
    for role in ("archive", "bom", "cpl"):
        name = names[role]
        want = claimed.get(name)
        got = present[name]
        if want is None:
            problems.append({"file": name,
                             "issue": "{} records no digest for it".format(
                                 manifest_name)})
        elif want != got:
            problems.append({
                "file": name,
                "issue": "{} records a digest that is not this file's; the "
                         "package holds an artifact from a different "
                         "run".format(manifest_name),
                "recorded": want[:16], "actual": got[:16]})
    facts["archive_sha256"] = present[archive]

    receipt_path = os.path.join(root, RECEIPT_NAME)
    if not os.path.isfile(receipt_path):
        if require_receipt:
            problems.append({
                "file": RECEIPT_NAME,
                "issue": "the package carries no receipt, so there is nothing "
                         "that says which files belong to it"})
        else:
            facts["stage"] = "candidate"
        return problems, facts
    facts["stage"] = "installed"

    # 2. the receipt's inventory is exactly what is here
    receipt = _load(receipt_path)
    recorded = receipt.get("files") or {}
    for rel in sorted(set(recorded) | set(present)):
        if rel not in present:
            problems.append({"file": rel,
                             "issue": "the receipt lists it but it is not in "
                                      "the package"})
        elif rel not in recorded:
            problems.append({"file": rel,
                             "issue": "is in the package but not in the "
                                      "receipt, so it is left over from "
                                      "another run or was added by hand"})
        elif recorded[rel] != present[rel]:
            problems.append({"file": rel,
                             "issue": "has changed since the receipt was "
                                      "written",
                             "recorded": recorded[rel][:16],
                             "actual": present[rel][:16]})
    # The attempt, not the release id: the receipt is written before the
    # rename that names the release, because an inventory taken after the
    # package moved would be describing a package nobody checked.
    facts["attempt_id"] = receipt.get("attempt_id")

    for role in ("validation", "clean_room", "unsealed"):
        if names.get(role) and names[role] not in present:
            missing(names[role], "a complete release must contain it")

    # The check reports are not optional decoration: without them the package
    # asserts that ERC and DRC passed and carries nothing that says so. A
    # receipt that faithfully inventories a package with no reports is an
    # accurate inventory of an incomplete release.
    required_reports = names.get("reports") or []
    facts["required_reports"] = list(required_reports)
    found_reports = sorted(rel for rel in present
                           if rel.startswith("reports/") and
                           rel.endswith(".json"))
    facts["reports_present"] = found_reports
    for rel in required_reports:
        if rel not in present:
            missing(rel, "a complete release must carry this check report; "
                         "without it nothing in the package says the check "
                         "was run against these artifacts")
    if required_reports and not found_reports:
        problems.append({"file": "reports/",
                         "issue": "the package carries no check reports at "
                                  "all"})
    if names["validation"] not in present or names["clean_room"] not in present:
        # Absent, so there is nothing to read. A file that is merely *wrong*
        # is still read: "it changed since the receipt" and "it accepted a
        # different archive" are separate facts and both are worth saying.
        return problems, facts

    # 3. the validation report describes THIS package
    validation = _load(os.path.join(root, names["validation"]))
    verdict = (validation.get("summary") or {}).get("verdict")
    facts["verdict"] = verdict
    if verdict != "ACCEPTED":
        problems.append({"file": names["validation"],
                         "issue": "the package carries a validation report "
                                  "that did not accept it",
                         "verdict": verdict})

    arch = _evidence_digest(_gate(validation, "ARCH.CONTENTS"), archive)
    if arch is None:
        problems.append({"file": names["validation"],
                         "issue": "ARCH.CONTENTS recorded no digest for the "
                                  "archive, so what it examined is unknown"})
    elif arch != present[archive]:
        problems.append({
            "file": archive,
            "issue": "ARCH.CONTENTS validated a different archive from the one "
                     "in this package",
            "validated": arch[:16], "installed": present[archive][:16]})

    prov = _evidence_digest(_gate(validation, "ARCH.PROVENANCE"), manifest_name)
    if prov is None:
        problems.append({"file": names["validation"],
                         "issue": "ARCH.PROVENANCE recorded no digest for the "
                                  "release manifest"})
    elif prov != present[manifest_name]:
        problems.append({
            "file": manifest_name,
            "issue": "ARCH.PROVENANCE validated a different release manifest",
            "validated": prov[:16], "installed": present[manifest_name][:16]})

    # 4. one source closure, agreed by everything that names one
    clean_room = _load(os.path.join(root, names["clean_room"]))
    embedded = validation.get("clean_room") or {}
    freshness = (_gate(validation, "PROV.REPORT_FRESHNESS") or {}).get(
        "measurements", {})
    saying = {
        manifest_name: manifest_closure,
        names["clean_room"]: clean_room.get("source_closure_sha256"),
        names["validation"] + ":clean_room": embedded.get(
            "source_closure_sha256"),
        names["validation"] + ":PROV.REPORT_FRESHNESS": freshness.get(
            "source_closure_sha256"),
    }
    for rel in sorted(present):
        if rel.startswith("reports/") and rel.endswith(".json"):
            saying[rel] = _load(os.path.join(root, rel)).get(
                "source_closure_sha256")
    distinct = {value for value in saying.values() if value}
    facts["source_closure_sha256"] = sorted(distinct)[0] if len(
        distinct) == 1 else sorted(distinct)
    for rel, value in sorted(saying.items()):
        if value is None:
            problems.append({"file": rel,
                             "issue": "names no source closure, so it cannot "
                                      "be tied to the rest of the package"})
    if len(distinct) > 1:
        problems.append({
            "issue": "the package was assembled from more than one clean-room "
                     "run: its files name {} different source "
                     "closures".format(len(distinct)),
            "by_file": {rel: (value or "")[:16]
                        for rel, value in sorted(saying.items())}})

    # 5. the standalone clean-room record and the embedded one are the same run
    for field in ("run_root", "build_root", "origin", "source_closure_sha256"):
        if embedded.get(field) != clean_room.get(field):
            problems.append({
                "file": names["clean_room"],
                "issue": "disagrees with the clean_room section of {} about "
                         "{}".format(names["validation"], field),
                "standalone": str(clean_room.get(field))[-60:],
                "embedded": str(embedded.get(field))[-60:]})
    if json.dumps(embedded.get("steps"), sort_keys=True) != json.dumps(
            clean_room.get("steps"), sort_keys=True):
        problems.append({"file": names["clean_room"],
                         "issue": "records different build steps from the "
                                  "clean_room section of {}".format(
                                      names["validation"])})

    # 6. the receipt is about the run the reports describe
    if receipt.get("source_closure_sha256") and manifest_closure and \
            receipt["source_closure_sha256"] != manifest_closure:
        problems.append({"file": RECEIPT_NAME,
                         "issue": "was written for a different run from the "
                                  "one the package describes"})
    return problems, facts


def member_names(manifest):
    """The package's file names, as this board declares them.

    Nothing here is a filename this framework chose for the board: the
    artifact names come from the manifest, and the check reports are named by
    the generation steps that produce them, so a board that calls its DRC
    report something else is still covered.
    """
    names = {
        "archive": leaf(manifest.get("archive.zip")),
        "manifest": leaf(manifest.get("archive.manifest")),
        "bom": leaf(manifest.get("artifacts.bom")),
        "cpl": leaf(manifest.get("artifacts.cpl")),
        "validation": "validation.json",
        "clean_room": "clean_room.json",
        "unsealed": "UNSEALED.txt",
    }
    names["reports"] = ["{}/{}".format(REPORTS_DIR, leaf(name))
                        for name in required_report_files(manifest)]
    return names


def required_report_files(manifest):
    """The check reports this board's release generates, by configuration.

    Read from the generation steps rather than written down again here: the
    same block that tells the release to run ERC and DRC is what says which
    files must come out of it.
    """
    steps = manifest.get("reports.required_steps", [])
    out = []
    for step in steps:
        key = "release_generation.{}.output".format(step)
        if manifest.has(key):
            out.append(manifest.get(key))
    return out
