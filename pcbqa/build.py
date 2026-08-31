"""Generate the fabrication outputs a release commit carries.

`build` runs KiCad against a staged copy of the design - the sources the
manifest declares and what they reach, never the whole repository and never
the authoritative files - and installs the result into the paths the manifest
declares, as ordinary files in the working tree. Nothing here decides what a
release is; it only produces the bytes a release commit contains.
"""

from __future__ import annotations

import csv
import glob
import json
import os
import shutil
import zipfile

from . import artifacts, closure
from .core import sha256_file, stage_design, toolkit_identity, utcnow


class BuildError(Exception):
    """The build cannot proceed, and nothing may be installed."""


def canonical_argv(args):
    """argv with absolute paths reduced to basenames, so it is checkout-free."""
    return [os.path.basename(a) if os.sep in a else a for a in args]


class Build:
    """One generation run. Writes only inside `root` until `install`."""

    def __init__(self, ctx, root):
        self.ctx = ctx
        self.manifest = ctx.manifest
        self.cfg = self.manifest.get("release_generation")
        self.origin = os.path.realpath(self.manifest.resolve("."))
        self.root = os.path.abspath(root)
        self.project = os.path.join(self.root, "project")
        self.export = os.path.join(self.root, "export")
        self.staged = os.path.join(self.root, "staged")
        self.gerbers = os.path.join(self.staged, "gerbers")
        self.reports = os.path.join(self.staged, "reports")
        self.log = []
        self.blockers = []
        self.excluded_layers = []

    # -- 1: stage the design; tools never open the authoritative files -----
    def isolate(self):
        locks = closure.open_design_locks(self.manifest,
                                          self.cfg["lock_file_globs"])
        if locks:
            self.blockers.append(
                ("build:lock_files", "ERROR",
                 "the design has {} lock file(s) beside it ({}); close KiCad "
                 "before building".format(len(locks), ", ".join(locks[:4]))))
            raise BuildError("lock files present beside the design")
        staged = stage_design(self.manifest, self.project)
        for path in (self.export, self.staged, self.gerbers, self.reports):
            os.makedirs(path, exist_ok=True)
        self.log.append({"step": "stage", "files": len(staged)})

    # -- 2: run every generation step -------------------------------------
    def generate(self):
        from .gates.g_checks import VIOLATIONS_EXIT_CODE, required_options
        cli = self.ctx.kicad_cli
        board = os.path.join(self.project, self.manifest.get("sources.pcb"))
        sch = os.path.join(self.project, self.manifest.get("sources.schematic"))
        cfg = self.cfg
        bom, cpl = cfg["bom"], cfg["cpl"]
        commands = [
            ("erc", [cli, "sch", "erc", "--output",
                     os.path.join(self.reports, cfg["erc"]["output"]),
                     "--format", "json"]
             + list(required_options("erc")) + [sch]),
            ("drc", [cli, "pcb", "drc", "--output",
                     os.path.join(self.reports, cfg["drc"]["output"]),
                     "--format", "json"]
             + list(required_options("drc")) + [board]),
            ("gerbers", [cli, "pcb", "export", "gerbers",
                         "--output", self.export]
             + list(self.manifest.get("artifacts.gerber_export_flags"))
             + [board]),
            ("drill", [cli, "pcb", "export", "drill", "--output", self.export]
             + list(cfg["drill"]["flags"]) + [board]),
            ("cpl", [cli, "pcb", "export", "pos", "--output",
                     os.path.join(self.staged, cpl["output"])]
             + list(cpl["flags"]) + [board]),
            ("bom", [cli, "sch", "export", "bom", "--output",
                     os.path.join(self.staged, bom["output"]),
                     "--fields", ",".join(bom["fields"]),
                     "--labels", ",".join(bom["labels"]),
                     "--group-by", ",".join(bom["group_by"])]
             + list(bom["flags"]) + [sch]),
        ]
        for name, args in commands:
            proc = self.ctx.run_tool(args)
            ok = proc.returncode == 0 or (
                name in ("erc", "drc") and proc.returncode == VIOLATIONS_EXIT_CODE)
            self.log.append({
                "step": name, "exit": proc.returncode, "ok": ok,
                "command": canonical_argv(args),
                "stderr": (proc.stderr or "").strip()[:400]})
            if not ok:
                self.blockers.append(
                    ("generate:" + name, "ERROR",
                     "exit {}: {}".format(proc.returncode,
                                          (proc.stderr or "").strip()[:120])))

        missing = [n for n, p in (
            ("bom", os.path.join(self.staged, bom["output"])),
            ("cpl", os.path.join(self.staged, cpl["output"])),
            ("erc", os.path.join(self.reports, cfg["erc"]["output"])),
            ("drc", os.path.join(self.reports, cfg["drc"]["output"])),
        ) if not os.path.isfile(p)]
        if not glob.glob(os.path.join(self.export, "*")):
            missing.append("gerbers")
        for name in missing:
            self.blockers.append(("generate:" + name, "ERROR",
                                  "mandatory artifact was not generated"))

    # -- 3: the names the fabricator reads --------------------------------
    def name_for_fab(self):
        """Move the export into the fab's filenames. Contents untouched.

        The fabricator decides what a file is from its name; KiCad names an
        inner layer after whatever the board calls it. Every declared file must
        appear exactly once, and anything left over blocks.
        """
        spec = self.manifest.get("fabrication_naming", None)
        if not spec:
            for path in sorted(glob.glob(os.path.join(self.export, "*"))):
                if os.path.isfile(path):
                    shutil.copy2(path, os.path.join(self.gerbers,
                                                    os.path.basename(path)))
            return
        wanted, by_suffix = {}, []
        for row in spec["files"]:
            if row.get("kicad_suffix"):
                by_suffix.append(row)
            else:
                wanted[row["kicad_ext"].lower()] = row

        excluded, renamed, unknown = [], {}, []
        for path in sorted(glob.glob(os.path.join(self.export, "*"))):
            if not os.path.isfile(path):
                continue
            name = os.path.basename(path)
            if closure.matches(name, [e["glob"] for e in spec.get("exclude", [])]):
                excluded.append(name)
                continue
            row = next((r for r in by_suffix
                        if name.lower().endswith(r["kicad_suffix"].lower())),
                       None)
            if row is None:
                row = wanted.get(os.path.splitext(name)[1].lstrip(".").lower())
            if row is None:
                unknown.append(name)
                continue
            if row["ship_as"] in renamed:
                self.blockers.append((
                    "build:fab_naming", "ERROR",
                    "two exported files claim to be {}: {} and {}".format(
                        row["ship_as"], renamed[row["ship_as"]], name)))
                continue
            shutil.copy2(path, os.path.join(self.gerbers, row["ship_as"]))
            renamed[row["ship_as"]] = name

        for row in spec["files"]:
            if row["ship_as"] not in renamed:
                self.blockers.append((
                    "build:fab_naming", "ERROR",
                    "the export produced no {} file, so {} cannot be "
                    "shipped".format(row["kicad_layer"], row["ship_as"])))
        for name in unknown:
            self.blockers.append((
                "build:fab_naming", "ERROR",
                "{} is not a file this board knows how to name for the "
                "fab".format(name)))
        self.log.append({"step": "fab_naming", "exit": 0, "ok": not unknown,
                         "command": ["rename", "{} file(s)".format(len(renamed))],
                         "renamed": renamed, "excluded": excluded})

    # -- 4: the columns the assembly house reads --------------------------
    def format_for_fab(self):
        spec = self.cfg.get("fab_format")
        if not spec:
            return
        for kind in ("cpl", "bom"):
            rules = spec.get(kind)
            if not rules:
                continue
            path = os.path.join(self.staged, self.cfg[kind]["output"])
            if not os.path.isfile(path):
                continue
            with open(path, newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            columns = rules["columns"]
            absent_source = sorted({c["from"] for c in columns}
                                   - set(rows[0].keys() if rows else ()))
            if absent_source:
                self.blockers.append((
                    "build:fab_format", "ERROR",
                    "{} export has no {} column(s); the fab format cannot be "
                    "built from it".format(kind, ", ".join(absent_source))))
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
                                "build:fab_format", "ERROR",
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
            missing = [name for name in rules.get("required_columns", [])
                       if name not in labels]
            if missing:
                self.blockers.append((
                    "build:fab_format", "ERROR",
                    "the {} format would ship without {}, which the fab "
                    "requires".format(kind, ", ".join(missing))))
                continue
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=labels)
                writer.writeheader()
                writer.writerows(out)
            self.log.append({"step": "fab_format:" + kind, "exit": 0,
                             "ok": True, "rows": len(out),
                             "command": ["relabel", os.path.basename(path)]})

    def orient_cpl(self, rows, rules):
        """The reviewed library-zero offset for every placed part.

        A placement whose part has no reviewed entry stops the build: assuming
        zero for an unreviewed part is indistinguishable, in the output, from
        having reviewed it and found zero.
        """
        from .orientation import Registry, apply_to_rows

        spec = self.cfg.get("cpl_orientation")
        if not spec:
            return
        registry = Registry(spec)
        defects = registry.defects()
        for defect in defects:
            self.blockers.append((
                "build:cpl_orientation", "ERROR",
                "{}: {}".format(defect.get("lcsc", "registry"),
                                defect["issue"])))
        applied, problems = apply_to_rows(
            rows, registry, self.part_numbers_by_designator(
                registry.part_number_field),
            rules["field_map"]["designator"], rules["field_map"]["rotation"],
            int(spec.get("angle_decimals", 4)))
        for problem in problems:
            self.blockers.append((
                "build:cpl_orientation", "ERROR",
                "{}: {}".format(problem.get("reference") or "a placement",
                                problem["issue"])))
        turned = {ref: row["offset_deg"] for ref, row in applied.items()
                  if row["offset_deg"]}
        self.log.append({
            "step": "cpl_orientation", "exit": 0,
            "ok": not problems and not defects,
            "command": ["orient", "{} placement(s)".format(len(applied))],
            "reviewed_parts": len(registry.entries),
            "offsets_applied": dict(sorted(turned.items())),
            "unreviewed": sorted(p.get("reference") for p in problems)})

    def part_numbers_by_designator(self, field_name):
        import pcbnew
        board = pcbnew.LoadBoard(
            os.path.join(self.project, self.manifest.get("sources.pcb")))
        out = {}
        for footprint in board.Footprints():
            for field in footprint.GetFields():
                if field.GetName() == field_name and field.GetText().strip():
                    out[footprint.GetReference()] = field.GetText().strip()
        return out

    # -- 5: only approved fabrication data goes in the archive ------------
    def package(self):
        from .gates.g_contracts import _classify, archive_rule
        allow = self.manifest.get("archive.allow")
        deny = self.manifest.get("archive.deny", [])
        chosen = []
        for path in sorted(glob.glob(os.path.join(self.gerbers, "*"))):
            if not os.path.isfile(path):
                continue
            name = os.path.basename(path)
            with open(path, "rb") as fh:
                _kind, function, _empty = _classify(name, fh.read())
            if archive_rule(deny, name, function) is not None or \
                    archive_rule(allow, name, function) is None:
                self.excluded_layers.append(
                    {"file": name, "issue": "not approved fabrication data"})
                self.blockers.append(
                    ("build:fabrication_allowlist", "ERROR",
                     "{} is not on the archive allowlist".format(name)))
                os.unlink(path)
                continue
            chosen.append(path)
        zpath = os.path.join(self.staged, self.cfg["archive"]["zip"])
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in chosen:
                zf.write(path, os.path.basename(path))
        self.log.append({"step": "package", "exit": 0, "ok": True,
                         "command": ["zip", os.path.basename(zpath)],
                         "entries": len(chosen),
                         "excluded": len(self.excluded_layers)})

    # -- 6: bind the reports to the design they were produced from --------
    def bind_reports(self):
        from . import canonical
        policy = closure.policy_for(self.manifest)
        self.closure_entries = closure.source_closure(self.manifest, policy)
        self.closure_sha256 = closure.closure_digest(self.closure_entries)
        root = self.manifest.resolve(".")
        for name, relative in (("erc", self.manifest.get("sources.schematic")),
                               ("drc", self.manifest.get("sources.pcb"))):
            path = os.path.join(self.reports, self.cfg[name]["output"])
            if not os.path.isfile(path):
                continue
            source = os.path.join(root, relative)
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["source_sha256"] = canonical.digest(
                source, policy.classify(relative.replace("\\", "/")))
            doc["source_closure_sha256"] = self.closure_sha256
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)

    # -- 7: the record that binds the artifacts to the design -------------
    def destinations(self):
        """{staged file: the declared path it installs to}.

        Derived from the manifest rather than from the staging layout, so a
        board whose BOM, Gerbers and reports live in different directories
        installs correctly and is recorded under the names the gate will look
        for. Nothing is inferred from the shape of the staging tree.
        """
        declared = artifacts.paths(self.manifest)
        cfg = self.cfg
        mapping = {}
        for role, name in (("bom", cfg["bom"]["output"]),
                           ("cpl", cfg["cpl"]["output"]),
                           ("archive", cfg["archive"]["zip"])):
            if role in declared:
                mapping[os.path.join(self.staged, name)] = declared[role]
        for role, directory in (("gerber_dir", self.gerbers),
                                ("reports_dir", self.reports)):
            target = declared.get(role)
            if not target:
                continue
            for path in sorted(glob.glob(os.path.join(directory, "*"))):
                if os.path.isfile(path):
                    mapping[path] = os.path.join(target,
                                                 os.path.basename(path))
        for staged in self._staged_files():
            if staged not in mapping:
                raise BuildError(
                    "the build produced {!r}, which the manifest declares no "
                    "destination for".format(
                        os.path.relpath(staged, self.staged)))
        return mapping

    def fabrication_manifest(self):
        self.destination_map = self.destinations()
        recorded = {}
        for staged, target in self.destination_map.items():
            recorded[artifacts.record_key(self.manifest, target)] = \
                sha256_file(staged)
        document = {
            "schema_version": artifacts.FABRICATION_SCHEMA_VERSION,
            "board_id": self.manifest.board_id,
            "constraint_version": self.manifest.get("constraint_version"),
            "release_profile": self.manifest.get("release_profile.id", None),
            "generated_utc": utcnow(),
            "source_closure_sha256": self.closure_sha256,
            "source_closure_files": len(self.closure_entries),
            "tools": {
                "kicad": self.ctx.kicad_version(),
                "kicad_cli": os.path.basename(self.ctx.kicad_cli),
            },
            # The commit and its cleanliness, without the checkout path: a
            # release record that named one machine's directory would differ
            # between two developers building the same design.
            "toolkit": {k: v for k, v in toolkit_identity().items()
                        if k != "toolkit_root"},
            "commands": [" ".join(entry["command"]) for entry in self.log
                         if entry.get("command")],
            "artifacts": dict(sorted(recorded.items())),
            "excluded_layers": self.excluded_layers,
        }
        self.record_staged = os.path.join(self.staged, artifacts.leaf(
            artifacts.paths(self.manifest)["fabrication_manifest"]))
        with open(self.record_staged, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, sort_keys=False)
            fh.write("\n")
        return document

    def _staged_files(self):
        found = []
        for dirpath, _dirs, names in os.walk(self.staged):
            for name in sorted(names):
                found.append(os.path.join(dirpath, name))
        return sorted(found)

    # -- 8: install into the tree -----------------------------------------
    def install(self):
        """Replace the declared release locations with what was staged."""
        declared = artifacts.paths(self.manifest)
        mapping = dict(self.destination_map)
        mapping[self.record_staged] = declared["fabrication_manifest"]
        installed = []
        for staged, target in sorted(mapping.items()):
            target = self._installable(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(staged, target)
            installed.append(target)
        for role in ("gerber_dir", "reports_dir"):
            self._prune(declared.get(role), installed)
        return sorted(installed)

    def _prune(self, directory, keep):
        """Remove files a previous build left that this one did not produce."""
        if not directory or not os.path.isdir(directory):
            return
        self._installable(directory)
        kept = {os.path.realpath(p) for p in keep}
        for path in sorted(glob.glob(os.path.join(directory, "*"))):
            if os.path.isfile(path) and os.path.realpath(path) not in kept:
                os.unlink(path)

    def _installable(self, path):
        target = os.path.realpath(path)
        if not os.path.basename(target):
            raise BuildError("refusing to install to {!r}".format(path))
        try:
            inside = os.path.commonpath([target, self.origin]) == self.origin
        except ValueError:
            inside = False
        if not inside or target == self.origin:
            raise BuildError(
                "{!r} is not a location inside the project this manifest "
                "describes".format(path))
        return target

    # -- orchestration -----------------------------------------------------
    def run(self):
        self.isolate()
        self.generate()
        self.name_for_fab()
        self.format_for_fab()
        self.package()
        self.bind_reports()
        return self.fabrication_manifest()

    def summary(self):
        return {"root": self.root, "origin": self.origin, "steps": self.log,
                "excluded_layers": self.excluded_layers,
                "source_closure_sha256": getattr(self, "closure_sha256", None)}
