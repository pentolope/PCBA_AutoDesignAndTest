"""Clean-room release preparation.

A release attempt must not be able to pass by looking at artifacts that were
already lying in the tree. This module builds a self-contained run directory,
copies the design into it, deletes every previously generated output, then
regenerates ERC, DRC, Gerbers, drills, BOM, CPL and the fabrication archive
from that copy.

It finally writes a *derived manifest* whose every authoritative path - sources,
artifacts, reports, archive, fixture inventory - resolves inside the run
directory, and proves mechanically that none of them can reach the original
project. Validation then runs against that derived manifest, so what is
validated is exactly what was generated.

Nothing here is board-specific: every command, field name and policy comes from
the board manifest's `release_generation` block, and a manifest that does not
declare one cannot obtain a release.
"""

from __future__ import annotations

import copy
import fnmatch
import csv
import glob
import hashlib
import json
import os
import shutil
import zipfile

from . import canonical
from .core import sha256_file, utcnow

# Keys whose resolved paths the release must be able to prove are in-run.
AUTHORITATIVE_KEYS = (
    "sources.pcb", "sources.schematic", "sources.project",
    "artifacts.gerber_dir", "artifacts.bom", "artifacts.cpl",
    "archive.zip", "archive.manifest",
    "fixture.hash_file", "fixture.attributes_file",
)


# Archive shapes and the containment rules live in pcbqa.layout, which is the
# only module allowed to turn an identity into a path. Nothing here decides
# where output goes any more; it is handed the directories it may use.
from .layout import ORDERABLE_SUFFIXES, orderable_archives   # noqa: F401


class CleanRoomError(Exception):
    """Raised when the run directory cannot be trusted. Always blocks."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _matches(rel, patterns):
    rel = rel.replace("\\", "/")
    name = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def _find(root, patterns):
    """Files under `root` matching `patterns`, ignoring what is not
    part of the design.

    The same directories `copy_project` refuses to copy are pruned
    here, and for the same reason: they hold no design, so nothing
    inside them can be a finding about one. Without that, a lock
    glob written for KiCad reaches into machinery it was never
    about - `*-lock` is meant for `*.kicad_prl-lock`, and it also
    matches the `.cargo-lock` that Rust leaves in the vendored
    router's build tree, which blocked a release over a file no
    KiCad ever touched. A stale lock in `.git` or a cache would
    have done the same.
    """
    from .core import NEVER_COPY
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in NEVER_COPY]
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _matches(rel, patterns):
                hits.append(rel)
    return sorted(hits)


def _inside(path, root):
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                      # no common root to speak of
        return False


def _digest_map(root, policy, skip=()):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not _matches(d, skip))
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _matches(rel, skip):
                continue
            kind = policy.classify(rel)
            out[rel] = {"kind": kind, "sha256": canonical.digest(full, kind)}
    return out


def closure_digest(entries):
    """One digest over a {path: sha256} map, order-independent."""
    joined = "\n".join(f"{k}:{v}" for k, v in sorted(entries.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def executed_implementation(names):
    """Digests of the code that is running, taken from the loaded modules.

    A result that a program derived rather than read off the board is only
    reproducible if the program is pinned too, and hashing a file by path
    proves nothing about what was imported: the path could be a stale copy,
    or a different one from the copy on sys.path. So each module is resolved
    through the import system and its ``__file__`` is what gets hashed - the
    bytes that actually ran.

    Raw bytes, not canonicalised: this is source that the interpreter read,
    not a design file whose line endings a checkout is allowed to change.
    """
    import importlib
    entries = {}
    for name in names:
        module = importlib.import_module(name)
        path = getattr(module, "__file__", None)
        if not path or not os.path.isfile(path):
            raise CleanRoomError(
                "{} declares no importable file, so the implementation that "
                "ran cannot be recorded".format(name))
        entries["<executed>" + name] = sha256_file(path)
    return entries


#: Exactly the leaves `derive_manifest` below assigns, and therefore the only
#: values that cannot be part of a configuration identity. This lives beside
#: the code that rewrites them rather than in a board file: which keys move
#: when a project is copied is a property of the clean room, not of any
#: particular board, and a board must not be able to widen it.
#:
#: Every entry here is checked against derive_manifest by
#: tests/test_source_closure.py. Whole objects are deliberately absent:
#: `sources` and `tools` are copied through untouched, and only two of
#: `fixture`'s leaves move, so excluding those objects wholesale would let the
#: selected board, the selected schematic, the toolchain and the fixture
#: rejection policy all change without changing the identity of the result.
REWRITTEN_BY_THE_CLEAN_ROOM = (
    "board_id",
    "project_root",
    "fixture.hash_file",
    "fixture.attributes_file",
    "reports.files",
    "artifacts.gerber_dir",
    "artifacts.bom",
    "artifacts.cpl",
    "artifacts.cpl_fields",
    "artifacts.cpl_origin",
    "assembly.bom_fields",
    "archive.zip",
    "archive.manifest",
    "archive.pre_normalization_digests",
)


def configuration_identity(manifest):
    """The manifest's release-affecting content, without its paths.

    A clean room has to rewrite where things are - the project moves, the
    outputs move, the board gets a different id - so the manifest file's own
    digest is different inside the run and outside it, and using it as
    provenance means the reports a run produced can never be checked against
    the repository that produced them.

    What matters is not where the files were but what the configuration said:
    the thresholds, the profiles, the registry, the generation steps, which
    board and schematic were selected, and which tools were used. So exactly
    the leaves the clean room assigns are removed and everything else is
    hashed - including keys nobody has thought of yet, because the exclusion
    list is the short one and it is owned here.

    A board may name a subset of these, to say which of them its own release
    actually moves. It may not name anything else: a manifest that could add
    `release_generation` to the list would be a manifest that could change the
    reviewed offsets without changing the identity of the result.
    """
    declared = manifest.get("reports.configuration_excludes", None)
    if declared is None:
        pointers = list(REWRITTEN_BY_THE_CLEAN_ROOM)
    else:
        widened = [p for p in declared
                   if p not in REWRITTEN_BY_THE_CLEAN_ROOM]
        if widened:
            raise CleanRoomError(
                "reports.configuration_excludes names {} which the clean room "
                "does not rewrite; a board cannot exclude release-affecting "
                "configuration from its own provenance".format(widened))
        pointers = list(declared)

    data = copy.deepcopy(manifest.data)
    for pointer in pointers:
        node, _, leaf = pointer.rpartition(".")
        target = data
        for step in node.split(".") if node else []:
            target = target.get(step) if isinstance(target, dict) else None
            if target is None:
                break
        if isinstance(target, dict):
            target.pop(leaf, None)
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def source_closure(manifest, policy):
    """Canonical digests of every input a check result depends on.

    The board, its schematic hierarchy, project settings, the frozen
    orientation evidence and the script that reads it - plus the modules that
    derive rather than read, addressed by import name because they are not
    part of the project being copied, and the manifest's configuration
    identity. A report produced before any of these changed is not describing
    the design that is about to be manufactured.

    Two things keep this stable across machines and across the clean room.
    Digests are canonical, so a checkout with either line ending gives the
    same identity for a text file. And the globs are matched against an
    explicit exclusion list, so directories that exist only on some machines -
    the validator's own output tree, routing scratch, other boards' fixtures -
    cannot wander into a board's provenance and change it.

    The declared sources are then added by name rather than left to whichever
    glob happens to reach them. A glob that matches the selected board today
    is a coincidence, not a guarantee: point `sources.pcb` at a board under a
    directory the globs exclude and the closure would otherwise cover a file
    the release never opened while ignoring the one it did.
    """
    root = manifest.resolve(".")
    excluded = manifest.get("reports.source_closure_exclude", [])
    entries = {}
    for pattern in manifest.get("reports.source_closure"):
        for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            if _matches(rel, excluded):
                continue
            entries[rel] = canonical.digest(path, policy.classify(rel))

    for role, declared in sorted((manifest.get("sources") or {}).items()):
        path = manifest.resolve(declared)
        if not os.path.isfile(path):
            raise CleanRoomError(
                "sources.{} names {} which does not exist, so the closure "
                "cannot cover the design it selects".format(role, declared))
        rel = os.path.relpath(path, root).replace("\\", "/")
        entries[rel] = canonical.digest(path, policy.classify(rel))

    entries.update(executed_implementation(
        manifest.get("reports.implementation_closure", [])))
    entries["<configuration>"] = configuration_identity(manifest)
    return entries


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

class CleanRun:
    """One isolated release attempt."""

    def __init__(self, ctx, root, build_dir):
        self.source_ctx = ctx
        self.manifest = ctx.manifest
        self.root = os.path.abspath(root)
        self.origin_root = os.path.realpath(self.manifest.resolve("."))
        self.project = os.path.join(self.root, "fixture", "project")
        self.generated = os.path.join(self.root, "generated")
        self.gerbers = os.path.join(self.generated, "gerbers")
        # The assembled package lives in the attempt's own build directory.
        # It is a candidate for exactly as long as it sits there: publication
        # is the attempt renaming this directory into `published/`, and until
        # that happens nothing here has been released.
        self.release = build_dir
        os.makedirs(self.release, exist_ok=True)
        self.reports = os.path.join(self.root, "reports")
        self.log = []
        self.removed = []
        self.blockers = []
        self.assertions = []
        self.cfg = self.manifest.get("release_generation")

    # -- stage 1: an isolated, output-free copy ---------------------------
    def isolate(self):
        if os.path.isdir(self.root):
            shutil.rmtree(self.root)
        locks = _find(self.origin_root, self.cfg["lock_file_globs"])
        if locks:
            # A lock means KiCad may have the project open: the bytes on disk
            # are not necessarily the design. Refuse rather than copy it.
            self.blockers.append(
                ("release:lock_files", "ERROR",
                 f"project tree contains {len(locks)} lock file(s) "
                 f"({', '.join(locks[:4])}); close KiCad before releasing"))
            raise CleanRoomError("lock files present in the release source tree")

        os.makedirs(os.path.dirname(self.project), exist_ok=True)
        # A project root can contain the validator's own output tree; the
        # shared helper refuses to copy the destination into itself.
        from .core import copy_project
        copy_project(self.origin_root, self.project)
        residual_locks = _find(self.project, self.cfg["lock_file_globs"])
        for rel in residual_locks:
            os.unlink(os.path.join(self.project, rel))
        for pattern in self.cfg["purge_globs"]:
            for path in sorted(glob.glob(os.path.join(self.project, pattern),
                                         recursive=True)):
                rel = os.path.relpath(path, self.project).replace("\\", "/")
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                elif os.path.exists(path):
                    os.unlink(path)
                self.removed.append(rel)
        leftover = _find(self.project, self.cfg["purge_globs"])
        if leftover:
            raise CleanRoomError(
                "previously generated output survived the purge: "
                + ", ".join(leftover[:6]))
        residual = _find(self.project, self.cfg["lock_file_globs"])
        if residual:
            raise CleanRoomError("lock file present in the clean copy: "
                                 + ", ".join(residual))
        for path in (self.generated, self.gerbers, self.release, self.reports):
            os.makedirs(path, exist_ok=True)
        self.log.append({"step": "isolate", "copied_from": self.origin_root,
                         "purged": len(self.removed)})

    def _reject_locks(self, dirpath, names):
        skip = set()
        for name in names:
            if _matches(name, self.cfg["lock_file_globs"]):
                skip.add(name)
        return skip

    # -- stage 2: freeze the inputs ---------------------------------------
    def freeze(self):
        """Snapshot the copied design before anything is run against it.

        The inventory written here is what PROV.FIXTURE_INTEGRITY checks after
        generation, so a tool that rewrites the board while checking it - a
        zone refill or a save-on-DRC - cannot go unnoticed.
        """
        files = _digest_map(self.project, self.policy)
        inventory = {
            "digest_policy": "text hashed over LF bytes; production output over "
                             "raw bytes; classification from .gitattributes",
            "normalization_commit": None,
            "recorded": utcnow(),
            "recorded_by": "clean-room release run, after purge, before generation",
            "files": files,
        }
        path = os.path.join(self.root, "fixture", "HASHES.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(inventory, fh, indent=2, sort_keys=True)
        self.inventory_path = path
        self.log.append({"step": "freeze", "files": len(files)})
        return inventory

    def load_policy(self):
        """Install the line-ending policy the canonical digests depend on."""
        attrs = self.manifest.resolve(self.manifest.get("fixture.attributes_file"))
        target_attrs = os.path.join(self.root, "fixture", ".gitattributes")
        shutil.copy2(attrs, target_attrs)
        self.policy = canonical.AttributePolicy.load(target_attrs)
        return self.policy

    # -- stage 3: generate everything the release ships --------------------
    def generate(self):
        cli = self.source_ctx.kicad_cli
        board = os.path.join(self.project, self.manifest.get("sources.pcb"))
        sch = os.path.join(self.project, self.manifest.get("sources.schematic"))
        cfg = self.cfg
        bom, cpl = cfg["bom"], cfg["cpl"]

        from .gates.g_checks import VIOLATIONS_EXIT_CODE, required_options
        commands = [
            ("erc", [cli, "sch", "erc", "--output",
                     os.path.join(self.reports, cfg["erc"]["output"]),
                     "--format", "json"]
             + list(required_options("erc")) + [sch]),
            ("drc", [cli, "pcb", "drc", "--output",
                     os.path.join(self.reports, cfg["drc"]["output"]),
                     "--format", "json"]
             + list(required_options("drc")) + [board]),
            ("gerbers", [cli, "pcb", "export", "gerbers", "--output", self.gerbers]
             + list(self.manifest.get("artifacts.gerber_export_flags")) + [board]),
            ("drill", [cli, "pcb", "export", "drill", "--output", self.gerbers]
             + list(cfg["drill"]["flags"]) + [board]),
            ("cpl", [cli, "pcb", "export", "pos", "--output",
                     os.path.join(self.release, cpl["output"])]
             + list(cpl["flags"]) + [board]),
            ("bom", [cli, "sch", "export", "bom", "--output",
                     os.path.join(self.release, bom["output"]),
                     "--fields", ",".join(bom["fields"]),
                     "--labels", ",".join(bom["labels"]),
                     "--group-by", ",".join(bom["group_by"])]
             + list(bom["flags"]) + [sch]),
        ]
        # `--exit-code-violations` makes a finding an exit code; that is a
        # successful invocation reporting something, not a tool failure. Only
        # the gates decide what the findings mean.
        violations_exit = VIOLATIONS_EXIT_CODE
        for name, args in commands:
            proc = self.source_ctx.run_tool(args)
            ok = proc.returncode == 0 or (
                name in ("erc", "drc") and proc.returncode == violations_exit)
            self.log.append({
                "step": name, "exit": proc.returncode, "ok": ok,
                "command": [os.path.basename(args[0])] + args[1:],
                "stderr": (proc.stderr or "").strip()[:400],
            })
            if not ok:
                self.blockers.append(
                    (f"generate:{name}", "ERROR",
                     f"exit {proc.returncode}: {(proc.stderr or '').strip()[:120]}"))

        missing = [n for n, p in (
            ("bom", os.path.join(self.release, bom["output"])),
            ("cpl", os.path.join(self.release, cpl["output"])),
            ("erc", os.path.join(self.reports, cfg["erc"]["output"])),
            ("drc", os.path.join(self.reports, cfg["drc"]["output"])),
        ) if not os.path.isfile(p)]
        if not glob.glob(os.path.join(self.gerbers, "*")):
            missing.append("gerbers")
        for name in missing:
            self.blockers.append((f"generate:{name}", "ERROR",
                                  "mandatory release artifact was not generated"))

    # -- stage 3a: give every exported file the name the fab reads --------
    def name_for_fab(self):
        """Rename the export to fab-identifiable filenames. Contents untouched.

        The fab decides what it is looking at from the filename. KiCad names
        an inner layer after whatever the board calls it, which says nothing
        about where it sits in the stack, and the layer's real role travels in
        a Gerber X2 attribute that a board declaring this block does not write
        and whose fabricator does not reliably read. A multilayer board can be
        quoted and built with layers missing on the strength of that.

        The mapping is keyed on KiCad's own extension, which is fixed by the
        layer being plotted: `.g1` is the first inner copper layer and `.g2`
        the second, whatever they are named. Every declared file must appear
        exactly once, and anything left over blocks: an unrecognised file in
        the fabrication directory is either something new that nobody has
        approved or a rename that silently did not happen.
        """
        spec = self.manifest.get("fabrication_naming", None)
        if not spec:
            return
        wanted, by_suffix = {}, []
        for row in spec["files"]:
            if row.get("kicad_suffix"):
                # Both drill files end in .drl, so the extension cannot tell
                # them apart; the plated one is matched by its own suffix.
                by_suffix.append(row)
            else:
                wanted[row["kicad_ext"].lower()] = row

        excluded, renamed, unknown = [], {}, []
        for path in sorted(glob.glob(os.path.join(self.gerbers, "*"))):
            if not os.path.isfile(path):
                continue
            name = os.path.basename(path)
            if _matches(name, [e["glob"] for e in spec.get("exclude", [])]):
                os.unlink(path)
                excluded.append(name)
                continue
            row = next((r for r in by_suffix
                        if name.lower().endswith(r["kicad_suffix"].lower())),
                       None)
            if row is None:
                ext = os.path.splitext(name)[1].lstrip(".").lower()
                row = wanted.get(ext)
            if row is None:
                unknown.append(name)
                continue
            if row["ship_as"] in renamed:
                self.blockers.append((
                    "release:fab_naming", "ERROR",
                    "two exported files claim to be {}: {} and {}".format(
                        row["ship_as"], renamed[row["ship_as"]], name)))
                continue
            target = os.path.join(self.gerbers, row["ship_as"])
            # Two steps, because a rename that only changes case is a no-op on
            # a case-insensitive filesystem and the archive would ship the
            # name KiCad chose rather than the one declared here.
            staging = target + ".renaming"
            os.rename(path, staging)
            os.rename(staging, target)
            renamed[row["ship_as"]] = name

        for row in spec["files"]:
            if row["ship_as"] in renamed:
                continue
            self.blockers.append((
                "release:fab_naming", "ERROR",
                "the export produced no {} file, so {} cannot be shipped"
                .format(row["kicad_layer"], row["ship_as"])))
        for name in unknown:
            self.blockers.append((
                "release:fab_naming", "ERROR",
                "{} is not a file this board knows how to name for the fab"
                .format(name)))
        self.log.append({
            "step": "fab_naming", "exit": 0, "ok": not unknown,
            "command": ["rename", "{} file(s)".format(len(renamed))],
            "renamed": renamed, "excluded": excluded,
        })

    # -- stage 3b: put the assembly data in the fab's own columns ----------
    def format_for_fab(self):
        """Rewrite the BOM and CPL into the column names the fab expects.

        kicad-cli names its columns for KiCad: `Ref`, `PosX`, `Side`. The
        assembly house reads `Designator`, `Mid X`, `Layer`, and rejects the
        file outright when they are absent - "Failed processing CPL file",
        with nothing to say which column it wanted. The numbers are not
        touched: every value here comes from the native export, and this only
        relabels the columns and normalises `top` to `Top`.

        A missing source column blocks the release. Shipping a placement file
        the fab cannot read is not something to discover at the order desk.
        """
        spec = self.cfg.get("fab_format")
        if not spec:
            return
        for kind in ("cpl", "bom"):
            rules = spec.get(kind)
            if not rules:
                continue
            path = os.path.join(self.release, self.cfg[kind]["output"])
            if not os.path.isfile(path):
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            columns = rules["columns"]
            missing = sorted({c["from"] for c in columns}
                             - set(rows[0].keys() if rows else ()))
            if missing:
                self.blockers.append((
                    "release:fab_format", "ERROR",
                    "{} export has no {} column(s); the fab format cannot be "
                    "built from it".format(kind, ", ".join(missing))))
                continue
            out = []
            for row in rows:
                entry = {}
                for column in columns:
                    value = (row.get(column["from"]) or "").strip()
                    values = column.get("values")
                    if values:
                        if value not in values:
                            self.blockers.append((
                                "release:fab_format", "ERROR",
                                "{} column {!r} holds {!r}, which the fab "
                                "format does not know how to say".format(
                                    kind, column["from"], value)))
                            value = ""
                        else:
                            value = values[value]
                    entry[column["label"]] = value
                out.append(entry)
            if kind == "cpl":
                self.orient_cpl(out, rules)
            labels = [c["label"] for c in columns]
            absent = [name for name in rules.get("required_columns", [])
                      if name not in labels]
            if absent:
                self.blockers.append((
                    "release:fab_format", "ERROR",
                    "the {} format would ship without {}, which the fab "
                    "requires".format(kind, ", ".join(absent))))
                continue
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=labels)
                writer.writeheader()
                writer.writerows(out)
            self.log.append({"step": "fab_format:" + kind, "exit": 0, "ok": True,
                             "command": ["relabel", os.path.basename(path)],
                             "rows": len(out)})

    def orient_cpl(self, rows, rules):
        """Put every placement angle in the frame the assembly machine uses.

        Normalisation into the range the fab reads, and the reviewed
        library-zero offset for the part. Both live in pcbqa.orientation, which
        is also what the validating gate uses, so the generator and the check
        cannot drift apart on what "correct" means - what makes the check
        independent is that it re-derives the offsets from the frozen library
        evidence rather than trusting the registry.

        A placement whose part has no reviewed entry stops the release. There
        is deliberately no default: assuming zero for an unreviewed part is
        indistinguishable, in the output, from having reviewed it and found
        zero, and only one of those is safe to put on a machine.
        """
        from .orientation import Registry, apply_to_rows

        spec = self.cfg.get("cpl_orientation")
        if not spec:
            return
        registry = Registry(spec)
        defects = registry.defects()
        for defect in defects:
            self.blockers.append((
                "release:cpl_orientation", "ERROR",
                "{}: {}".format(defect.get("lcsc", "registry"),
                                defect["issue"])))

        part_numbers = self.part_numbers_by_designator(
            registry.part_number_field)
        applied, problems = apply_to_rows(
            rows, registry, part_numbers,
            rules["field_map"]["designator"], rules["field_map"]["rotation"],
            int(spec.get("angle_decimals", 4)))
        for problem in problems:
            self.blockers.append((
                "release:cpl_orientation", "ERROR",
                "{}: {}".format(problem.get("reference") or "a placement",
                                problem["issue"])))

        turned = {ref: row["offset_deg"] for ref, row in applied.items()
                  if row["offset_deg"]}
        self.log.append({
            "step": "cpl_orientation",
            "exit": 0,
            "ok": not problems and not defects,
            "command": ["orient", "{} placement(s)".format(len(applied))],
            "reviewed_parts": len(registry.entries),
            "offsets_applied": dict(sorted(turned.items())),
            "unreviewed": sorted(p.get("reference") for p in problems),
        })

    def part_numbers_by_designator(self, field_name):
        """Each reference's distributor part number, from the board released."""
        import pcbnew
        board = pcbnew.LoadBoard(
            os.path.join(self.project, self.manifest.get("sources.pcb")))
        out = {}
        for footprint in board.Footprints():
            for field in footprint.GetFields():
                if field.GetName() == field_name and field.GetText().strip():
                    out[footprint.GetReference()] = field.GetText().strip()
        return out

    def bind_reports(self, manifest):
        """Bind the source closure into every report this run produced.

        The closure is taken from the *derived* manifest - the one validation
        will load - because the manifest is itself an input a check result
        depends on. Binding the origin manifest's digest here would guarantee a
        mismatch at validation time and make the freshness gate meaningless.
        """
        entries = source_closure(manifest, self.policy)
        digest = closure_digest(entries)

        # The reports this run writes will later be checked from the origin
        # manifest, against the installed package. If the two manifests do not
        # produce the same closure the reports are stale the moment they are
        # installed, and the failure would surface as an unexplained hash
        # mismatch weeks later. Compare them here, while the difference can
        # still be named.
        origin = source_closure(self.manifest, self.policy)
        if closure_digest(origin) != digest:
            only_here = sorted(set(entries) - set(origin))
            only_there = sorted(set(origin) - set(entries))
            changed = sorted(k for k in set(entries) & set(origin)
                             if entries[k] != origin[k])
            self.blockers.append((
                "release:closure_identity", "ERROR",
                "the clean room and the origin do not agree on the source "
                "closure: only in the run {}; only in the origin {}; "
                "differing {}".format(only_here[:6], only_there[:6],
                                      changed[:6])))
        board = os.path.join(self.project, self.manifest.get("sources.pcb"))
        sch = os.path.join(self.project, self.manifest.get("sources.schematic"))
        for name, source in (("erc", sch), ("drc", board)):
            path = os.path.join(self.reports, self.cfg[name]["output"])
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            # Canonical, like every other identity this project records: a
            # checkout is allowed to have either line ending, and a report
            # bound to the raw bytes of one of them is stale on the other
            # machine before anyone has changed anything.
            doc["source_sha256"] = canonical.digest(
                source, self.policy.classify(
                    os.path.relpath(source, self.project).replace("\\", "/")))
            doc["source_closure_sha256"] = digest
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
        self.closure = entries
        self.closure_sha256 = digest

    # -- stage 4: package only approved fabrication data -------------------
    def package(self):
        # A board says what its fabrication data is either by Gerber X2 file
        # function or, where the fabricator does not read X2, by filename. The
        # shared helper honours whichever this board declared.
        from .gates.g_contracts import _classify, archive_rule
        allow = self.manifest.get("archive.allow")
        deny = self.manifest.get("archive.deny", [])
        chosen, rejected = [], []
        for path in sorted(glob.glob(os.path.join(self.gerbers, "*"))):
            if not os.path.isfile(path):
                continue
            name = os.path.basename(path)
            with open(path, "rb") as fh:
                _kind, function, _empty = _classify(name, fh.read())
            if archive_rule(deny, name, function) is not None or \
                    archive_rule(allow, name, function) is None:
                # Courtyard, fabrication, adhesive, margin and user layers all
                # land here. They are kept out of the archive *and* they block:
                # a release that quietly dropped them would hide the fact that
                # the export step produced something nobody approved.
                rejected.append({"file": name,
                                 "issue": "not approved fabrication data"})
                self.blockers.append(
                    ("release:fabrication_allowlist", "ERROR",
                     f"{name} is not on the archive allowlist"))
                continue
            chosen.append(path)
        self.rejected_layers = rejected
        zpath = os.path.join(self.release, self.cfg["archive"]["zip"])
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in chosen:
                zf.write(path, os.path.basename(path))
        self.archive_zip = zpath

        # The approved layers also go into the package unzipped. They are what
        # the gates read, and a release directory that cannot be validated
        # without reaching back into the run that produced it is not a package.
        loose = os.path.join(self.release, "gerbers")
        os.makedirs(loose, exist_ok=True)
        for path in chosen:
            shutil.copy2(path, os.path.join(loose, os.path.basename(path)))
        self.package_gerbers = loose
        self.log.append({"step": "package", "entries": len(chosen),
                         "excluded": len(rejected)})

        listed = [zpath,
                  os.path.join(self.release, self.cfg["bom"]["output"]),
                  os.path.join(self.release, self.cfg["cpl"]["output"])]
        lines = [
            "# Fabrication release manifest",
            "",
            f"- generated: {utcnow()}",
            f"- kicad: {self.source_ctx.kicad_version()}",
            f"- constraint profile: {self.manifest.get('release_profile.id')}",
            f"- source closure sha256: `{self.closure_sha256}`",
            "",
            "## command",
            "",
        ]
        for entry in self.log:
            if "command" in entry:
                lines.append("    " + " ".join(
                    f'"{a}"' if " " in a else a for a in entry["command"]))
        lines += ["", "## artifacts", ""]
        for path in listed:
            if os.path.isfile(path):
                lines.append(f"- `{os.path.basename(path)}` sha256 "
                             f"`{sha256_file(path)}`")
        lines += ["", "## excluded from the archive", ""]
        for entry in rejected or [{"file": "(none)",
                                   "issue": "every exported layer was approved"}]:
            lines.append(f"- `{entry['file']}`: "
                         f"{entry['issue']}")
        lines.append("")
        mpath = os.path.join(self.release, self.cfg["archive"]["manifest"])
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        self.archive_manifest = mpath

    # -- stage 5: a manifest that can only see this run --------------------
    def derive_manifest(self):
        cfg = self.cfg
        data = copy.deepcopy(self.manifest.data)
        data["board_id"] = str(data.get("board_id")) + "-cleanroom"
        data["project_root"] = "fixture/project"
        data["fixture"] = dict(data.get("fixture", {}))
        data["fixture"]["hash_file"] = "../HASHES.json"
        data["fixture"]["attributes_file"] = "../.gitattributes"
        up = "../.."                       # fixture/project -> run root
        # The package's own copy, not the export directory it was taken from:
        # what gets validated has to be what gets installed.
        data["artifacts"]["gerber_dir"] = os.path.join(self.release, "gerbers")
        data["artifacts"]["bom"] = os.path.join(self.release,
                                                cfg["bom"]["output"])
        data["artifacts"]["cpl"] = os.path.join(self.release,
                                                cfg["cpl"]["output"])
        fab = cfg.get("fab_format") or {}
        data["artifacts"]["cpl_fields"] = (
            fab["cpl"]["field_map"] if fab.get("cpl")
            else cfg["cpl"]["field_map"])
        data["artifacts"]["cpl_origin"] = cfg["cpl"]["origin"]
        data["assembly"]["bom_fields"] = (
            fab["bom"]["field_map"] if fab.get("bom")
            else cfg["bom"]["field_map"])
        data["archive"]["zip"] = os.path.join(self.release,
                                              cfg["archive"]["zip"])
        data["archive"]["manifest"] = os.path.join(self.release,
                                                   cfg["archive"]["manifest"])
        data["archive"].pop("pre_normalization_digests", None)
        data["reports"] = dict(data["reports"])
        data["reports"]["files"] = [f"{up}/reports/*.json"]
        path = os.path.join(self.root, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        self.manifest_path = path
        return path

    # -- stage 6: prove the isolation --------------------------------------
    def reaches_back(self, path):
        """True if this path belongs to the original project rather than here.

        Ownership decides it, not nesting. A board whose project root is the
        repository itself keeps its attempts under that same root, so every
        correctly isolated path in the run is also, trivially, inside the
        origin tree; asking only "is it under the origin" called all twelve of
        them a leak. What matters is whether the run produced it.
        """
        return _inside(path, self.origin_root) and not self.owns(path)

    def assert_isolated(self, derived):
        """Every authoritative path must be in-run, and none may reach back."""
        problems = []
        checked = {}
        for key in AUTHORITATIVE_KEYS:
            if not derived.has(key):
                continue
            resolved = derived.resolve(derived.get(key))
            checked[key] = resolved
            if self.reaches_back(resolved):
                problems.append({"key": key, "path": resolved,
                                 "issue": "resolves into the original project"})
            elif not self.owns(resolved):
                problems.append({"key": key, "path": resolved,
                                 "issue": "resolves outside the clean run"})
        report_root = derived.resolve(".")
        for pattern in derived.get("reports.files"):
            for path in glob.glob(os.path.join(report_root, pattern), recursive=True):
                checked[f"reports:{os.path.basename(path)}"] = path
                if not self.owns(path):
                    problems.append({"key": "reports.files", "path": path,
                                     "issue": "report is not from this run"})
        # And nothing may resolve into the outputs the origin already carries.
        for key in ("artifacts.gerber_dir", "artifacts.bom", "artifacts.cpl",
                    "archive.zip", "archive.manifest"):
            if not self.manifest.has(key):
                continue
            stale = os.path.realpath(self.manifest.resolve(self.manifest.get(key)))
            for name, path in checked.items():
                if os.path.realpath(path) == stale or _inside(path, stale):
                    problems.append({"key": name, "path": path,
                                     "issue": f"is the pre-existing {key}"})
        self.assertions = [{"key": k, "path": v} for k, v in sorted(checked.items())]
        for p in problems:
            self.blockers.append(("release:isolation", "ERROR",
                                  f"{p['key']} {p['issue']}: {p['path']}"))
        return problems

    # -- orchestration -----------------------------------------------------
    def build(self):
        from .core import load_manifest
        self.isolate()
        # The authoritative DRC refills zones and saves the board, so the
        # inventory is taken after generation: it records the design that was
        # actually exported and packaged, which is the one that would ship.
        self.load_policy()
        self.generate()
        self.name_for_fab()
        self.format_for_fab()
        self.freeze()
        derived = load_manifest(self.derive_manifest())
        self.bind_reports(derived)
        self.package()
        self.assert_isolated(derived)
        return derived

    # -- stage 7: what this run owns --------------------------------------
    def owns(self, path):
        """The two roots this run legitimately writes to."""
        return _inside(path, self.root) or _inside(path, self.release)

    def summary(self):
        return {
            "run_root": self.root,
            "build_root": self.release,
            "origin": self.origin_root,
            "purged": self.removed,
            "steps": self.log,
            "authoritative_paths": self.assertions,
            "excluded_layers": getattr(self, "rejected_layers", []),
            "source_closure_sha256": getattr(self, "closure_sha256", None),
        }
