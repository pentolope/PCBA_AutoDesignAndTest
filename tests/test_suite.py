"""Rev A expectation, portability, source-hygiene and mutation tests."""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from tests import paths                                                    # noqa: E402
from pcbqa import core                              # noqa: E402
from pcbqa.core import Context, Manifest, Status     # noqa: E402
from pcbqa.gates import (g_provenance, g_checks, g_geometry,  # noqa: F401
                         g_contracts, g_assembly, g_export_parity,   # noqa: E402,F401
                         g_fabrication, g_orientation, g_timing)     # noqa: E402,F401
# The FULL registry, exactly as run.py loads it: the expected-failure matrix
# compares every gate's status against the recorded expectation, and a gate
# module nobody imported is a gate that silently never runs - in a parallel
# worker a sibling test module used to import the missing three first, which
# is why only serial runs ever noticed.
from tests import build_portability                 # noqa: E402

REVA = paths.REVA_MANIFEST
EXPECTED = paths.REVA_EXPECTED
PORTABILITY = paths.PORTABILITY_MANIFEST
PYTHON = sys.executable

#: Serial runs get the same output isolation the parallel runner gives its
#: workers. Without it, `run.py` roots a temporary manifest's output beside
#: that manifest's project - which for these tests is the FROZEN Rev A
#: fixture, so the run's own output directory appears inside the tree
#: PROV.FIXTURE_INTEGRITY holds to an exact inventory, and release tests
#: judge outputs accumulated by every earlier invocation on the machine.
#: The env var is inherited by the subprocesses these tests spawn.
_OUTPUT_ISOLATION = {"owned": None, "saved": None}


def setUpModule():
    from pcbqa.parallel import ENV_OUTPUT_ROOT
    _OUTPUT_ISOLATION["saved"] = os.environ.get(ENV_OUTPUT_ROOT)
    if _OUTPUT_ISOLATION["saved"] is None:
        _OUTPUT_ISOLATION["owned"] = tempfile.mkdtemp(
            prefix="pcbqa_suite_out_")
        os.environ[ENV_OUTPUT_ROOT] = _OUTPUT_ISOLATION["owned"]


def tearDownModule():
    from pcbqa.parallel import ENV_OUTPUT_ROOT
    if _OUTPUT_ISOLATION["owned"] is not None:
        os.environ.pop(ENV_OUTPUT_ROOT, None)
        shutil.rmtree(_OUTPUT_ISOLATION["owned"], ignore_errors=True)
        _OUTPUT_ISOLATION["owned"] = None


def _configure_geometry():
    """Install the chord error the way a gate would: from the profile."""
    from pcbqa import geom
    return geom.configure(Manifest(REVA).geometry_profile()
                          .tolerance("polygon_chord_error_mm").value)


_configure_geometry()


def run_validator(manifest, extra=()):
    proc = subprocess.run([PYTHON, os.path.join(HERE, "run.py"), "validate", manifest,
                           *extra], capture_output=True, text=True, cwd=HERE)
    return proc


def _remove(path):
    """Cleanup that tolerates a file another step already removed."""
    try:
        os.unlink(path)
    except (FileNotFoundError, PermissionError):
        pass


def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return path


def temp_manifest(tag, mutate=None, project=None):
    """A derived manifest in its own temp directory.

    Not in the repository's boards/ directory: the source-hygiene test scans
    that directory, and two workers mutating manifests at the same time would
    otherwise read each other's. `project_root` is absolute so the manifest can
    live anywhere.
    """
    doc = json.load(open(REVA, encoding="utf-8"))
    doc["project_root"] = os.path.abspath(
        project if project else paths.REVA_PROJECT)
    if project:
        # A copy is not the frozen inventory, but canonical hashing still
        # needs the line-ending policy, so keep that and drop only the
        # inventory this copy cannot satisfy.
        doc["fixture"] = {"attributes_file": paths.ATTRIBUTES}
    if mutate:
        mutate(doc)
    work = tempfile.mkdtemp(prefix="pcbqa_mf_" + tag + "_")
    return _write_json(os.path.join(work, "manifest.json"), doc)


def validate(manifest_path):
    """Run gates in-process and return {gate_id: result_dict}."""
    manifest = Manifest(manifest_path)
    workdir = tempfile.mkdtemp(prefix="pcbqa_")
    ctx = Context(manifest, workdir)
    results = core.run_all(ctx)
    return {r.gate_id: r.to_dict() for r in results}, ctx


# ---------------------------------------------------------------------------
# Rev A must be rejected, gate by gate
# ---------------------------------------------------------------------------

class RevAExpectedFailureMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.expected = json.load(open(EXPECTED, encoding="utf-8"))
        cls.results, _ctx = validate(REVA)

    def test_every_gate_has_an_expectation(self):
        missing = sorted(set(self.results) - set(self.expected["gates"]))
        self.assertFalse(missing, f"gates without a recorded expectation: {missing}")

    def test_gate_statuses_match_expectation(self):
        wrong = []
        for gate_id, want in self.expected["gates"].items():
            got = self.results.get(gate_id, {}).get("status")
            if got != want:
                wrong.append(f"{gate_id}: expected {want}, observed {got}")
        self.assertFalse(wrong, "\n".join(wrong))

    def test_verdict_is_rejected(self):
        blocking = [g for g, r in self.results.items()
                    if r["status"] in Status.BLOCKING]
        self.assertTrue(blocking, "Rev A must be rejected by at least one gate")

    def test_anchor_counts_reproduce_independently(self):
        wrong = []
        for path, want in self.expected["anchors"].items():
            gate_id, key = path.rsplit(".", 1)
            got = self.results[gate_id]["measurements"].get(key)
            if got != want:
                wrong.append(f"{path}: anchor {want}, recalculated {got}")
        self.assertFalse(wrong, "\n".join(wrong))

    def test_cli_exit_status_is_nonzero(self):
        proc = run_validator(REVA)
        self.assertNotEqual(proc.returncode, 0,
                            "validator must exit nonzero on Rev A")

    def test_all_gates_run_after_the_first_failure(self):
        registered = {e["id"] for e in core.registered()}
        self.assertEqual(set(self.results), registered,
                         "validation stopped early; every gate must report")


# ---------------------------------------------------------------------------
# release must be blocked and must not seal anything
# ---------------------------------------------------------------------------

class ReleaseBlocked(unittest.TestCase):
    """Rev A must not be taggable as a release, and must say why."""

    def _release_check(self, manifest_path):
        return subprocess.run(
            [PYTHON, os.path.join(HERE, "run.py"), "release-check",
             manifest_path], capture_output=True, text=True, cwd=HERE)

    def test_a_rejected_board_is_not_releasable(self):
        proc = self._release_check(REVA)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELEASE BLOCKED", proc.stdout)
        self.assertIn("No release tag should be created", proc.stdout)
        # The gates it fails, named, not merely a nonzero status.
        for gate_id in ("DRC.AUTHORITATIVE", "ERC.AUTHORITATIVE",
                        "ARCH.PROVENANCE"):
            self.assertIn(gate_id, proc.stdout)

    def test_it_writes_nothing_into_the_frozen_fixture(self):
        before = _digest_tree(paths.REVA_PROJECT)
        self._release_check(REVA)
        self.assertEqual(_digest_tree(paths.REVA_PROJECT), before,
                         "release-check modified the design it was judging")

    def test_missing_mandatory_gate_blocks_release(self):
        """A gate that is NOT_APPLICABLE but mandatory must block."""
        tmp = temp_manifest("mandatory",
                            lambda doc: doc.pop("via_mask"))   # four VIA gates N/A
        proc = self._release_check(tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("NOT_APPLICABLE", proc.stdout)

    def test_release_profile_with_no_mandatory_gates_is_refused(self):
        def _empty(doc):
            doc["release_profile"]["mandatory_gates"] = []
        tmp = temp_manifest("empty_profile", _empty)
        proc = self._release_check(tmp)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no mandatory gates", proc.stdout)


def _digest_tree(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                out[os.path.relpath(full, root)] = hashlib.sha256(
                    fh.read()).hexdigest()
    return out


# ---------------------------------------------------------------------------
# portability
# ---------------------------------------------------------------------------

class Portability(unittest.TestCase):
    """The same validator, unchanged, on a structurally different board.

    The fixture is rebuilt into a private temporary directory, never into
    `fixtures/portability`. The builder assigns fresh UUIDs on every run, so
    building in place rewrote a tracked file every time the suite ran and
    produced a 48-line diff that looked like a design change and was not one.
    A test that modifies a checked-in fixture cannot be run twice and compared.
    """

    TRACKED = os.path.join(paths.PORTABILITY_FIXTURE, "widget_b.kicad_pcb")

    @classmethod
    def setUpClass(cls):
        # Recorded before anything is built, so the comparison below measures
        # what this suite did rather than what git happens to think.
        with open(cls.TRACKED, "rb") as fh:
            cls.tracked_digest = hashlib.sha256(fh.read()).hexdigest()
        cls.work = tempfile.mkdtemp(prefix="pcbqa_portability_")
        project = os.path.join(cls.work, "project")
        os.makedirs(project)
        shutil.copytree(paths.PORTABILITY_FIXTURE, project,
                        dirs_exist_ok=True)
        build_portability.build(os.path.join(project, "widget_b.kicad_pcb"))
        doc = json.load(open(PORTABILITY, encoding="utf-8"))
        doc["project_root"] = project
        cls.manifest_path = _write_json(
            os.path.join(cls.work, "manifest.json"), doc)
        cls.results, _ctx = validate(cls.manifest_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_generic_gates_run_on_a_structurally_different_board(self):
        for gate_id in ("STACK.NATIVE_VS_MANIFEST", "VIA.MASK_CLEARANCE_TARGET",
                        "VIA.MASK_CLEARANCE_PROCESS", "VIA.ANNULUS_MASK_OVERLAP",
                        "VIA.IN_PAD_CONTACT", "ROUTE.ANGLE_STYLE",
                        "ROUTE.TINY_SEGMENTS", "ROUTE.GEOMETRY_HYGIENE"):
            status = self.results[gate_id]["status"]
            self.assertEqual(status, Status.PASS,
                             f"{gate_id} = {status}: "
                             f"{self.results[gate_id]['reason']}")

    def test_board_is_structurally_unlike_reva(self):
        stack = self.results["STACK.NATIVE_VS_MANIFEST"]["measurements"]
        self.assertEqual(stack["copper_layers"], 2)

    def test_absent_policy_is_not_applicable_not_silently_passing(self):
        for gate_id in ("CONTRACT.CONNECTOR", "NET.TOPOLOGY", "ARCH.CONTENTS",
                        "ARCH.PROVENANCE", "PROV.FIXTURE_INTEGRITY",
                        "PROV.REPORT_FRESHNESS", "BOM.NATIVE_PARITY",
                        "CONTRACT.PLACEMENT", "ERC.AUTHORITATIVE",
                        "DRC.AUTHORITATIVE"):
            result = self.results[gate_id]
            self.assertEqual(result["status"], Status.NOT_APPLICABLE, gate_id)
            self.assertTrue(result["reason"], f"{gate_id} gave no reason")

    def test_cli_accepts_the_other_board(self):
        proc = run_validator(self.manifest_path)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:])

    def test_running_this_suite_does_not_touch_the_tracked_fixture(self):
        """The builder writes into a temp directory, never into the fixture."""
        with open(self.TRACKED, "rb") as fh:
            now = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(now, self.tracked_digest,
                         "running the portability suite rewrote the tracked "
                         "fixture " + self.TRACKED)
        self.assertFalse(self.work in build_portability.TARGET)
        self.assertTrue(os.path.isfile(
            os.path.join(self.work, "project", "widget_b.kicad_pcb")),
            "the suite must build its own copy")


# ---------------------------------------------------------------------------
# no board-specific identifiers in generic checker source
# ---------------------------------------------------------------------------

class GenericSourceHygiene(unittest.TestCase):
    """Board identity must live in configuration, never in the framework."""

    PACKAGE = paths.PACKAGE
    # Everything a consumer of this repository executes or reads as policy.
    # Not just pcbqa/: a board name in a shipped schema or manufacturer profile
    # is exactly as wrong as one in a gate, and for the same reason.
    PRODUCTION = (paths.PACKAGE,
                  os.path.join(paths.ROOT, "schemas"),
                  os.path.join(paths.ROOT, "profiles"))
    PRODUCTION_SUFFIXES = (".py", ".json")

    def _sources(self):
        seen = set()
        for root in self.PRODUCTION:
            for base, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                if os.path.basename(os.path.dirname(base)) == "profiles"                         or os.path.basename(base) == "profiles":
                    # A fabricator profile mixes two kinds of file: authored
                    # source (process.json and friends), which these hygiene
                    # rules govern, and the ACQUIRED evidence store - the
                    # the committed catalog, whose numbers are JLCPCB's own
                    # (a 69% resin content is data, not a leaked fixture
                    # answer). The catalog directory is data, not source, and
                    # is not scanned.
                    dirs[:] = [d for d in dirs if d != "catalog"]
                for name in files:
                    if not name.endswith(self.PRODUCTION_SUFFIXES):
                        continue
                    path = os.path.join(base, name)
                    if path in seen:
                        continue
                    seen.add(path)
                    yield path, open(path, encoding="utf-8").read()
        run_py = os.path.join(paths.ROOT, "run.py")
        if run_py not in seen:
            yield run_py, open(run_py, encoding="utf-8").read()

    def _board_identity(self):
        """The names that identify a specific board, from every manifest.

        Distinct from `_identifiers_from_configs`, which pulls every token out
        of a manifest and then filters to things that LOOK like designators -
        an uppercase-only filter that a lowercase project name walks straight
        past. `microphone_array_v2` sat in pcbqa/render.py through exactly that
        gap: it was on no allowlist, it was simply never looked at.

        These are long, distinctive and unambiguous, so they are matched as
        plain substrings rather than filtered by shape.
        """
        names = set()
        for path in self._manifest_paths():
            doc = json.load(open(path, encoding="utf-8"))
            if "schema_version" not in doc:
                continue
            if doc.get("board_id"):
                names.add(doc["board_id"])
            for declared in (doc.get("sources") or {}).values():
                stem = os.path.splitext(os.path.basename(declared))[0]
                if len(stem) > 6:
                    names.add(stem)
        return {n for n in names if len(n) > 6}

    def test_no_board_name_appears_in_production_source(self):
        """A board's own name, in any file a consumer executes or reads."""
        offenders = []
        for name in sorted(self._board_identity()):
            for path, text in self._sources():
                if name.lower() in text.lower():
                    offenders.append("{}: {}".format(
                        os.path.relpath(path, paths.ROOT), name))
        self.assertFalse(offenders,
                         "board names found in production source; they belong "
                         "in a manifest, a fixture or an example:" +
                         chr(10) + chr(10).join(sorted(offenders)))

    def _framework_vocabulary(self):
        """Words the framework itself owns: statuses and gate-ID components."""
        # Framework statuses, gate-ID components, and industry vocabulary that
        # belongs to the domain rather than to any board.
        vocab = {Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_APPLICABLE,
                 "REJECTED", "ACCEPTED",
                 "BOM", "CPL", "ERC", "DRC", "PTH", "NPTH", "SMD", "THT",
                 "PCB", "JSON", "CSV", "UTC", "URL", "API", "ID", "IU", "MM"}
        for entry in core.registered():
            vocab.update(re.split(r"[._]", entry["id"]))
        return vocab

    def _manifest_paths(self):
        """Every real manifest the toolkit ships, wherever it keeps it.

        Fixture manifests count. A fixture is allowed to name a concrete board
        - that is what makes it a fixture - and this test exists to prove none
        of those names leaked out of `tests/` into the framework.
        """
        found = []
        for name in sorted(os.listdir(paths.MANIFESTS)):
            if name.endswith(".json"):
                found.append(os.path.join(paths.MANIFESTS, name))
        for base, _dirs, files in os.walk(paths.FIXTURES):
            for name in sorted(files):
                if name == "manifest.json":
                    found.append(os.path.join(base, name))
        return found

    def _identifiers_from_configs(self):
        """Board-specific tokens taken from real manifests only.

        The expectation file is not a manifest and is excluded: it legitimately
        contains gate IDs and status words, which belong to the framework.
        """
        tokens = set()
        for path in self._manifest_paths():
            doc = json.load(open(path, encoding="utf-8"))
            if "schema_version" not in doc:
                continue

            def walk(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)
                elif isinstance(node, str):
                    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_+.]{2,}", node):
                        tokens.add(word)
            walk(doc)
        generic = {
            "signal", "plane", "front", "back", "female", "male", "socket",
            "header", "true", "false", "null", "SMD", "designator", "Designator",
            "Layer", "Rotation", "json", "csv", "zip", "gerbers", "generated",
            # NOTE: no board name belongs on this list. `microphone_array_v2`
            # used to sit here; it was removed when the framework moved into
            # its own repository, because there is no longer any legitimate
            # reason for a board's name to appear in framework source.
            "widget_b", "kicad_cli", "Program", "Files",
            "KiCad", "bin", "exe", "https", "jlcpcb", "com", "capabilities",
            "pcb", "sha256", "kicad", "command", "constraint", "native_kicad",
            "Copper", "Top", "Bot", "Inr", "Soldermask", "Legend", "Paste",
            # The manufacturer this toolkit is scoped to. JLCPCB-wide
            # capability and process knowledge belongs here by design, so its
            # name is framework vocabulary, not board identity.
            "JLCPCB", "jlcpcb",
            # KiCad API and CLI vocabulary. These appear in manifests because
            # the manifest documents which KiCad behaviour a tolerance depends
            # on; they name the tool, not this board.
            "ERROR_OUTSIDE", "DNP", "EXCLUDE_FROM_BOM", "QUANTITY", "LCSC",
            "MPN", "Reference", "Value", "Footprint", "Quantity", "Comment",
            "Manufacturer", "Description", "PosX", "PosY", "Rot", "Side",
            "Ref", "JobFile", "Drill", "Profile", "NP", "Drillmap",
            "Profile", "Drill", "plated", "nonplated", "JobFile", "Drillmap",
            "annulus_to_opening_mm", "pinsocket", "receptacle", "pinheader",
            "plug", "radial", "README", "docs", "constraints", "tools",
            "check_routes", "netlist", "make_release", "widget", "cpl", "bom",
            "MANIFEST", "HASHES", "project", "fixtures", "portability",
            "manifest", "negative", "expected",
        }
        generic |= self._framework_vocabulary()
        return {t for t in tokens if t not in generic}

    def test_no_board_identifier_appears_in_framework_source(self):
        board_tokens = self._identifiers_from_configs()
        # Only look for tokens that are plausibly board identity, not English.
        suspicious = {t for t in board_tokens
                      if re.fullmatch(r"[A-Z][A-Z0-9_+]{2,}", t)
                      or re.fullmatch(r"[A-Z]{1,3}\d+", t)}
        offenders = []
        for path, text in self._sources():
            for token in suspicious:
                if re.search(rf"\b{re.escape(token)}\b", text):
                    offenders.append(f"{os.path.relpath(path, HERE)}: {token}")
        self.assertFalse(offenders,
                         "board-specific identifiers found in generic source:\n"
                         + "\n".join(sorted(offenders)))

    def test_no_expected_defect_counts_in_framework_source(self):
        """A generic checker must not encode this board's known answers."""
        anchors = json.load(open(EXPECTED, encoding="utf-8"))["anchors"]
        # Only distinctive counts. Small integers (0, 2, 3, 9, 11, 12) occur
        # naturally as slot counts and version numbers; flagging them would be
        # noise, not evidence that a board's answers were hard-coded.
        values = {v for v in anchors.values() if isinstance(v, int) and v >= 20}
        offenders = []
        for path, text in self._sources():
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in values:
                    offenders.append(
                        f"{os.path.relpath(path, HERE)}:{node.lineno}: {node.value}")
        self.assertFalse(offenders,
                         "known Rev A counts embedded in generic source:\n"
                         + "\n".join(offenders))

    def test_framework_declares_no_absolute_project_paths(self):
        offenders = []
        for path, text in self._sources():
            for m in re.finditer(r"[A-Za-z]:\\\\|/Users/|/home/", text):
                offenders.append(f"{os.path.relpath(path, HERE)}: {m.group(0)}")
        self.assertFalse(offenders, "absolute paths in framework source: " + str(offenders))


# ---------------------------------------------------------------------------
# mutation tests - each deliberate defect must be detected
# ---------------------------------------------------------------------------

class Mutations(unittest.TestCase):
    """Each mutation injects a defect; the suite must notice."""

    @classmethod
    def setUpClass(cls):
        results, _ = validate(REVA)
        cls.baseline_contacts = (results["VIA.ANNULUS_MASK_OVERLAP"]
                                 ["measurements"]["annulus_contacts"])

    def _mutated_manifest(self, mutate):
        return temp_manifest("mut", mutate)

    def test_stale_drc_report_substitution_is_detected(self):
        """Swapping in a report that names a different source must be caught."""
        results, _ = validate(REVA)
        fresh = results["PROV.REPORT_FRESHNESS"]
        self.assertEqual(fresh["status"], Status.FAIL)
        issues = {f.get("issue", "") for f in fresh["findings"]}
        self.assertTrue(any("not a current design source" in i or "older than" in i
                            or "no source hash" in i for i in issues), issues)

    def test_altered_output_after_hash_recorded_is_detected(self):
        """Mutate a recorded artifact and the fabrication record must object."""
        from pcbqa import artifacts as artifact_set
        from pcbqa import closure

        work = tempfile.mkdtemp(prefix="pcbqa_alt_")
        project = os.path.join(work, "project")
        shutil.copytree(paths.REVA_PROJECT, project)
        path = temp_manifest("altered", project=project)

        # A fabrication record that is honest about the tree as it stands.
        manifest = Manifest(path)
        _entries, digest = closure.current(manifest)
        record = {
            "schema_version": artifact_set.FABRICATION_SCHEMA_VERSION,
            "board_id": manifest.board_id,
            "constraint_version": manifest.get("constraint_version"),
            "source_closure_sha256": digest,
            "tools": {"kicad_cli": "kicad-cli"},
            "commands": ["kicad-cli pcb export gerbers"],
            "artifacts": {artifact_set.record_key(manifest, p):
                          core.sha256_file(p)
                          for p in artifact_set.generated_files(manifest)},
        }
        record_path = manifest.resolve(
            manifest.get("artifacts.fabrication_manifest"))
        os.makedirs(os.path.dirname(record_path), exist_ok=True)
        _write_json(record_path, record)
        self.assertEqual(validate(path)[0]["ARCH.PROVENANCE"]["status"],
                         Status.PASS,
                         "the honest record must pass before it is broken")

        with open(manifest.resolve(manifest.get("artifacts.bom")), "a",
                  encoding="utf-8") as fh:
            fh.write("MUTATED,,,,\n")
        arch = validate(path)[0]["ARCH.PROVENANCE"]
        self.assertEqual(arch["status"], Status.FAIL)
        self.assertTrue(
            any("changed since its digest was recorded" in f.get("issue", "")
                for f in arch["findings"]), arch["findings"])

    def test_a_stale_artifact_set_is_detected(self):
        """The design moved; the artifacts did not."""
        from pcbqa import artifacts as artifact_set

        work = tempfile.mkdtemp(prefix="pcbqa_stale_")
        project = os.path.join(work, "project")
        shutil.copytree(paths.REVA_PROJECT, project)
        path = temp_manifest("stale", project=project)
        manifest = Manifest(path)
        record_path = manifest.resolve(
            manifest.get("artifacts.fabrication_manifest"))
        os.makedirs(os.path.dirname(record_path), exist_ok=True)
        _write_json(record_path, {
            "schema_version": artifact_set.FABRICATION_SCHEMA_VERSION,
            "board_id": manifest.board_id,
            "constraint_version": manifest.get("constraint_version"),
            "source_closure_sha256": "0" * 64,
            "tools": {"kicad_cli": "kicad-cli"},
            "commands": ["kicad-cli pcb export gerbers"],
            "artifacts": {artifact_set.record_key(manifest, p):
                          core.sha256_file(p)
                          for p in artifact_set.generated_files(manifest)},
        })
        arch = validate(path)[0]["ARCH.PROVENANCE"]
        self.assertEqual(arch["status"], Status.FAIL)
        self.assertTrue(
            any("generated from a different design" in f.get("issue", "")
                for f in arch["findings"]), arch["findings"])

    def test_drill_map_in_the_archive_is_detected(self):
        results, _ = validate(REVA)
        arch = results["ARCH.CONTENTS"]
        self.assertEqual(arch["status"], Status.FAIL)
        # Named by the entry rather than by its X2 file function: a board may
        # identify its fabrication data either way, and this one has to be
        # refused under both.
        self.assertTrue(any("drl_map" in str(f.get("entry", ""))
                            for f in arch["findings"]), arch["findings"])

    def test_adding_a_new_disallowed_file_to_the_archive_is_detected(self):
        src = os.path.join(paths.REVA_PROJECT,
                           Manifest(REVA).get("archive.zip"))
        work = tempfile.mkdtemp(prefix="pcbqa_zip_")
        dst = os.path.join(work, "mutated.zip")
        shutil.copy2(src, dst)
        with zipfile.ZipFile(dst, "a") as zf:
            zf.writestr("stray_notes.txt", "not fabrication data")
        rel = os.path.relpath(dst, paths.REVA_PROJECT)
        path = self._mutated_manifest(
            lambda d: d["archive"].update({"zip": rel.replace("\\", "/")}))
        results, _ = validate(path)
        entries = [f.get("entry") for f in results["ARCH.CONTENTS"]["findings"]]
        self.assertIn("stray_notes.txt", entries)

    def test_bypassing_a_validation_stage_is_detected(self):
        """Removing a gate's policy makes it NOT_APPLICABLE, and the expectation
        matrix then fails - a stage cannot be silently skipped."""
        path = self._mutated_manifest(lambda d: d.pop("via_mask"))
        results, _ = validate(path)
        for gate_id in ("VIA.MASK_CLEARANCE_TARGET", "VIA.IN_PAD_CONTACT"):
            self.assertEqual(results[gate_id]["status"], Status.NOT_APPLICABLE)
        expected = json.load(open(EXPECTED, encoding="utf-8"))["gates"]
        drift = [g for g in ("VIA.MASK_CLEARANCE_TARGET", "VIA.IN_PAD_CONTACT")
                 if results[g]["status"] != expected[g]]
        self.assertTrue(drift, "bypassing a stage was not visible in the matrix")

    def test_rotated_pad_via_overlap_mutation_is_detected(self):
        """Rotating a pad so a previously clear via now overlaps must be caught."""
        import pcbnew
        from tests import synth
        from pcbqa import geom
        board = synth.new_board()
        net = synth.add_net(board, "N1")
        fp, _pad = synth.add_pad_footprint(board, "P1", 100, 100,
                                           pcbnew.PAD_SHAPE_RECT, (2.0, 0.4),
                                           rotation_deg=0.0, net=net)
        d = 0.9 / (2 ** 0.5)
        synth.add_via(board, 100 + d, 100 - d, net=net)
        clear = geom.BoardGeometry(board)
        self.assertFalse(clear.via_mask_report(clear.vias[0], "front")
                         ["annulus_contacts_opening"])
        fp.SetOrientationDegrees(45.0)
        mutated = geom.BoardGeometry(board)
        self.assertTrue(mutated.via_mask_report(mutated.vias[0], "front")
                        ["annulus_contacts_opening"],
                        "rotated-pad overlap mutation was not detected")


    def _fixture_copy(self, tag):
        work = tempfile.mkdtemp(prefix="pcbqa_" + tag + "_")
        project = os.path.join(work, "project")
        # Another worker may be running kicad-cli against the fixture
        # at this moment, and kicad-cli drops a transient ~*.lck
        # beside the project for the duration; racing that file into
        # the copy loses (it vanishes between listing and copying).
        # The fixture's own reject globs already call such files
        # scratch that has no place in a frozen copy, so excluding
        # them here is the inventory policy, not a workaround.
        shutil.copytree(paths.REVA_PROJECT, project,
                        ignore=shutil.ignore_patterns(
                            "~*", "*.lck", "*.bak", "*.tmp", ".#*"))
        return project

    def _manifest_for(self, project, tag):
        """A manifest beside the project copy it describes.

        Never in the repository's boards/ directory: two workers running two
        mutations at once would otherwise write the same file, and the loser
        would validate the winner's board.
        """
        return temp_manifest(tag, project=project)

    @staticmethod
    def _via_centres_inside(mask_path, via_points):
        from shapely.geometry import Point
        from pcbqa import gerber as gbr
        mask = gbr.GerberFile(mask_path)
        count = 0
        for x, y in via_points:
            point = Point(x, y)
            if any(shape.contains(point) for _c, _fx, _fy, shape in mask.flashes):
                count += 1
        return count, mask

    def test_moving_a_defect_between_vias_is_detected(self):
        """Per-object truth moves while every total stays the same.

        Two solder-mask apertures are swapped in the shipped Gerber: a circular
        one sitting on one via and a rounded-rectangle one sitting on another.
        The number of apertures, the aperture list and the number of via
        centres inside an opening are all unchanged - a totals comparison sees
        nothing - but each of those two vias now faces a different opening with
        a different clearance, so the per-object comparison must fail.
        """
        import pcbnew
        from pcbqa import geom, gerber as gbr

        project = self._fixture_copy("swap")
        mask_path = os.path.join(project, "generated", "release", "gerbers",
                                 "microphone_array_v2-F_Mask.gbr")
        board = pcbnew.LoadBoard(os.path.join(project,
                                              "microphone_array_v2.kicad_pcb"))
        survey = geom.BoardGeometry(board, contact_tolerance_mm=1e-6)
        via_points = [(v.x, -v.y) for v in survey.vias]

        before, mask = self._via_centres_inside(mask_path, via_points)
        from shapely.affinity import translate
        from shapely.geometry import Point

        # A via with generous clearance in the shipped export, and a mask
        # opening that contains no via centre at all.
        def nearest_gap(shape_list, x, y):
            annulus = Point(x, y).buffer(0.225, quad_segs=32)
            return min(annulus.distance(sh) for sh in shape_list)

        shapes = [f[3] for f in mask.flashes]
        roomy = None
        for x, y in via_points:
            if nearest_gap(shapes, x, y) > 1.0:
                roomy = (x, y)
                break
        self.assertIsNotNone(roomy, "no via with generous clearance to relocate onto")

        spare = None
        for index, (code, fx, fy, shape) in enumerate(mask.flashes):
            if any(shape.contains(Point(x, y)) for x, y in via_points):
                continue
            half = max(shape.bounds[2] - shape.bounds[0],
                       shape.bounds[3] - shape.bounds[1]) / 2.0
            if half < 0.45:                     # must not swallow the via centre
                spare = (index, fx, fy, shape, half)
                break
        self.assertIsNotNone(spare, "no relocatable opening found")
        index, fx, fy, shape, half = spare

        # Park it 0.5 mm from the via centre: close enough to destroy the
        # clearance, far enough that the via centre stays outside the opening,
        # so the count of centres-inside - the only thing a totals comparison
        # looked at - is untouched.
        tx, ty = roomy[0] + 0.5, roomy[1]
        trial = list(shapes)
        trial[index] = translate(shape, tx - fx, ty - fy)
        self.assertEqual(sum(1 for x, y in via_points
                             if any(sh.contains(Point(x, y)) for sh in trial)),
                         before, "relocation must not change centres-inside")

        text = open(mask_path, encoding="utf-8").read()
        tok_from = "X%dY%dD03*" % (round(fx * 1e6), round(fy * 1e6))
        tok_to = "X%dY%dD03*" % (round(tx * 1e6), round(ty * 1e6))
        self.assertEqual(text.count(tok_from), 1, tok_from)
        open(mask_path, "w", encoding="utf-8").write(
            text.replace(tok_from, tok_to, 1))

        after, _ = self._via_centres_inside(mask_path, via_points)
        self.assertEqual(before, after,
                         "the mutation was supposed to preserve the totals a "
                         "counting comparison would look at")

        results, _ = validate(self._manifest_for(project, "swap"))
        gate = results["VIA.NATIVE_GERBER_AGREEMENT"]
        self.assertEqual(gate["status"], Status.FAIL,
                         "per-object gate missed a defect that moved between vias")
        issues = {f.get("issue", "") for f in gate["findings"]}
        self.assertTrue(
            any("clearance disagrees" in i or "different object" in i
                or "disagrees" in i for i in issues), issues)

    def test_moving_a_via_without_re_exporting_is_detected(self):
        """A via moved in the board but not re-exported must fail object matching."""
        import pcbnew
        project = self._fixture_copy("vmove")
        board_path = os.path.join(project, "microphone_array_v2.kicad_pcb")
        board = pcbnew.LoadBoard(board_path)
        via = next(t for t in board.Tracks() if isinstance(t, pcbnew.PCB_VIA))
        pos = via.GetPosition()
        via.SetPosition(pcbnew.VECTOR2I(pos.x + pcbnew.FromMM(0.5), pos.y))
        board.Save(board_path)

        results, _ = validate(self._manifest_for(project, "vmove"))
        gate = results["VIA.NATIVE_GERBER_AGREEMENT"]
        self.assertEqual(gate["status"], Status.FAIL)
        self.assertTrue(any("no plated drill hit" in f.get("issue", "")
                            for f in gate["findings"]), gate["findings"])

    def test_editing_a_shipped_gerber_is_detected(self):
        """A copper layer changed after export must fail per-layer parity."""
        work = tempfile.mkdtemp(prefix="pcbqa_layer_")
        project = os.path.join(work, "project")
        # Another worker may be running kicad-cli against the fixture
        # at this moment, and kicad-cli drops a transient ~*.lck
        # beside the project for the duration; racing that file into
        # the copy loses (it vanishes between listing and copying).
        # The fixture's own reject globs already call such files
        # scratch that has no place in a frozen copy, so excluding
        # them here is the inventory policy, not a workaround.
        shutil.copytree(paths.REVA_PROJECT, project,
                        ignore=shutil.ignore_patterns(
                            "~*", "*.lck", "*.bak", "*.tmp", ".#*"))
        target = os.path.join(project, "generated", "release", "gerbers",
                              "microphone_array_v2-F_Cu.gbr")
        text = open(target, encoding="utf-8").read()
        cut = text.replace("D03*", "D02*", 1)      # one flash becomes a move
        self.assertNotEqual(text, cut)
        open(target, "w", encoding="utf-8").write(cut)

        tmp = temp_manifest("layer", project=project)
        results, _ = validate(tmp)
        gate = results["STACK.GERBER_PARITY"]
        self.assertEqual(gate["status"], Status.FAIL,
                         "per-layer parity missed an edited copper layer")
        self.assertTrue(any("differs from a fresh export" in f.get("issue", "")
                            for f in gate["findings"]), gate["findings"])

    def test_gerber_parser_fails_closed_on_unknown_aperture(self):
        from pcbqa import gerber
        work = tempfile.mkdtemp(prefix="pcbqa_gbr_")
        path = os.path.join(work, "bad.gbr")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("%FSLAX46Y46*%\n%MOMM*%\n%ADD10WeirdShape,1.0*%\n"
                     "D10*\nX1000000Y1000000D03*\nM02*\n")
        with self.assertRaises(gerber.GerberError):
            gerber.GerberFile(path)


if __name__ == "__main__":
    unittest.main()
