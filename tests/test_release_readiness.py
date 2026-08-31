"""What Git must be able to say before a commit may be tagged as a release.

A release is a Git tag naming one commit whose committed fabrication artifacts
passed the release policy. Git supplies the history; these checks supply the
engineering preconditions Git does not check by itself. Every test here breaks
exactly one of them against a real repository.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run as run_cli                                             # noqa: E402
from tests import paths                                           # noqa: E402
from pcbqa import artifacts, closure, core, release               # noqa: E402
from pcbqa.core import sha256_file                                # noqa: E402
from pcbqa.parallel import ENV_OUTPUT_ROOT                        # noqa: E402

BOM = "generated/bom.csv"
REPORTS = "generated/reports"
FABRICATION = "generated/fabrication.json"
VALIDATION = "generated/validation.json"
EVIDENCE = "generated/visual_review.json"


def git(root, *args):
    proc = subprocess.run(("git", "-C", root) + args, capture_output=True,
                          text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError("git {}: {}".format(" ".join(args), proc.stderr))
    return proc.stdout


class _Board:
    """A tiny KiCad project inside a real Git repository, with a submodule."""

    def __init__(self, required_evidence=()):
        self.work = tempfile.mkdtemp(prefix="pcbqa_rel_")
        self.repo = os.path.join(self.work, "board")
        shutil.copytree(paths.CLEAN_PROJECT, self.repo)
        shutil.copy2(paths.ATTRIBUTES, os.path.join(self.repo, ".gitattributes"))
        os.makedirs(os.path.join(self.repo, "generated"), exist_ok=True)

        self.dependency = os.path.join(self.work, "dependency")
        os.makedirs(self.dependency)
        self._init(self.dependency)
        with open(os.path.join(self.dependency, "tool.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("VERSION = 1\n")
        git(self.dependency, "add", "-A")
        git(self.dependency, "commit", "-m", "dependency")

        self._init(self.repo)
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add",
            self.dependency, "tooling/dependency")

        self.manifest_path = os.path.join(self.repo, "manifest.json")
        self._write_manifest(required_evidence)
        self._write_release()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "board")

    @classmethod
    def _init(cls, root):
        git(root, "init", "-q", "-b", "main")
        cls._init_identity(root)

    @staticmethod
    def _init_identity(root):
        git(root, "config", "user.email", "suite@example.invalid")
        git(root, "config", "user.name", "pcbqa suite")

    def _write_manifest(self, required_evidence):
        with open(paths.CLEAN_MANIFEST, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["board_id"] = "release-readiness"
        doc["project_root"] = "."
        doc["fixture"] = {"attributes_file": ".gitattributes"}
        doc["artifacts"] = {"bom": BOM, "reports_dir": REPORTS,
                            "fabrication_manifest": FABRICATION,
                            "validation_report": VALIDATION}
        doc["reports"] = {
            "files": [REPORTS + "/*.json"],
            "source_closure": ["*.kicad_sch", "*.kicad_pcb", "*.kicad_pro"],
            "source_closure_exclude": ["generated/**", "out/**", ".git/**",
                                       "tooling/**"],
            "required_steps": ["erc"],
        }
        doc["release_generation"] = {"erc": {"output": "erc.json"}}
        doc["release_profile"] = {
            "id": "release-readiness",
            "required_evidence": list(required_evidence),
            "mandatory_gates": ["ARCH.PROVENANCE", "ERC.AUTHORITATIVE",
                                "DRC.AUTHORITATIVE"],
        }
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

    def _write_release(self):
        """Generate what a build installs, then record the verdict beside it."""
        with open(self.path(BOM), "w", encoding="utf-8") as fh:
            fh.write("Designator,Comment\n")
        self._write_report()
        self.rewrite_fabrication_record()
        code, doc, _ctx = self._validate()
        assert code == 0, "the fixture board must validate: {}".format(
            [g["gate"] for g in doc["gates"]
             if g["status"] not in ("PASS", "NOT_APPLICABLE")])

    def _write_report(self):
        """One real ERC report, bound to the design the way a build binds it."""
        from pcbqa import canonical
        from pcbqa.core import Context
        from pcbqa.gates.g_checks import required_options

        manifest = core.load_manifest(self.manifest_path)
        os.makedirs(self.path(REPORTS), exist_ok=True)
        output = os.path.join(self.path(REPORTS), "erc.json")
        ctx = Context(manifest, os.path.join(self.work, "erc"))
        relative = manifest.get("sources.schematic")
        ctx.run_tool([ctx.kicad_cli, "sch", "erc", "--output", output,
                      "--format", "json"] + list(required_options("erc"))
                     + [manifest.resolve(relative)])
        policy = closure.policy_for(manifest)
        _entries, digest = closure.current(manifest)
        with open(output, encoding="utf-8") as fh:
            report = json.load(fh)
        report["source_sha256"] = canonical.digest(
            manifest.resolve(relative), policy.classify(relative))
        report["source_closure_sha256"] = digest
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)

    def rewrite_fabrication_record(self):
        manifest = core.load_manifest(self.manifest_path)
        _entries, digest = closure.current(manifest)
        record = {
            "schema_version": artifacts.FABRICATION_SCHEMA_VERSION,
            "board_id": manifest.board_id,
            "constraint_version": manifest.get("constraint_version"),
            "generated_utc": "2026-01-01T00:00:00+00:00",
            "source_closure_sha256": digest,
            "tools": {"kicad_cli": "kicad-cli"},
            "commands": ["kicad-cli sch export bom"],
            "artifacts": {artifacts.record_key(manifest, path):
                          sha256_file(path)
                          for path in artifacts.generated_files(manifest)},
        }
        with open(self.path(FABRICATION), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        return record

    def _validate(self, write=True):
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = self.work
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return run_cli.cmd_validate(self.manifest_path, write=write,
                                            quiet=True)
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved

    def revalidate_and_commit(self):
        self._write_report()
        self.rewrite_fabrication_record()
        self._validate()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "rebuild")

    def path(self, relative):
        return os.path.join(self.repo, relative)

    def manifest(self):
        return core.load_manifest(self.manifest_path)

    def readiness(self):
        return release.readiness(self.manifest())

    def issues(self):
        problems, _facts = self.readiness()
        return [p["issue"] for p in problems]

    def release_check(self):
        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = self.work
        try:
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = run_cli.cmd_release_check(self.manifest_path)
        finally:
            if saved is None:
                os.environ.pop(ENV_OUTPUT_ROOT, None)
            else:
                os.environ[ENV_OUTPUT_ROOT] = saved
        return code, captured.getvalue()

    def close(self):
        shutil.rmtree(self.work, ignore_errors=True)


class _Base(unittest.TestCase):
    def board(self, required_evidence=()):
        board = _Board(required_evidence)
        self.addCleanup(board.close)
        return board


class ACleanCommittedStateIsReleasable(_Base):

    def test_it_passes_readiness(self):
        board = self.board()
        problems, facts = board.readiness()
        self.assertEqual(problems, [], problems)
        self.assertEqual(facts["worktree_entries"], 0)
        self.assertEqual(facts["submodules"], 1)
        self.assertEqual(facts["submodules_in_sync"], 1)
        self.assertEqual(facts["commit"], git(board.repo, "rev-parse",
                                              "HEAD").strip())

    def test_release_check_accepts_it_and_names_the_commit(self):
        board = self.board()
        code, output = board.release_check()
        self.assertEqual(code, 0, output[-3000:])
        self.assertIn("RELEASE READY", output)
        self.assertIn(git(board.repo, "rev-parse", "HEAD").strip(), output)

    def test_release_check_creates_no_tag_and_no_commit(self):
        """The toolkit proves; the tag is a deliberate act elsewhere."""
        board = self.board()
        before = git(board.repo, "rev-parse", "HEAD")
        code, output = board.release_check()
        self.assertEqual(code, 0, output[-2000:])
        self.assertEqual(git(board.repo, "tag", "-l").strip(), "",
                         "release-check created a tag")
        self.assertEqual(git(board.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(board.repo, "status", "--porcelain").strip(), "",
                         "release-check wrote into the working tree")

    def test_the_committed_artifacts_are_ordinary_tracked_files(self):
        board = self.board()
        tracked = set(git(board.repo, "ls-files").split())
        for relative in (BOM, FABRICATION, VALIDATION):
            self.assertIn(relative, tracked)


class ADirtyTreeIsNotAReleaseState(_Base):

    def test_a_modified_source_refuses(self):
        board = self.board()
        with open(board.path("clean.kicad_pcb"), "a", encoding="utf-8") as fh:
            fh.write("\n")
        self.assertTrue(any("differ from HEAD" in issue
                            for issue in board.issues()), board.issues())
        self.assertNotEqual(board.release_check()[0], 0)

    def test_an_untracked_file_refuses(self):
        board = self.board()
        with open(board.path("scratch.txt"), "w", encoding="utf-8") as fh:
            fh.write("not committed")
        self.assertTrue(any("differ from HEAD" in issue
                            for issue in board.issues()), board.issues())

    def test_an_untracked_release_artifact_refuses_by_name(self):
        board = self.board()
        git(board.repo, "rm", "-q", "--cached", BOM)
        git(board.repo, "commit", "-q", "-m", "stop tracking the bom")
        with open(board.path(".gitignore"), "w", encoding="utf-8") as fh:
            fh.write("generated/bom.csv\n")
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "ignore it")
        self.assertTrue(any("not tracked by Git" in issue
                            for issue in board.issues()), board.issues())

    def test_a_missing_release_artifact_refuses(self):
        board = self.board()
        os.unlink(board.path(BOM))
        self.assertTrue(any("are missing" in issue for issue in board.issues()),
                        board.issues())


class ASubmoduleMustBeExactlyItsGitlink(_Base):

    def test_a_dirty_submodule_refuses(self):
        board = self.board()
        with open(os.path.join(board.repo, "tooling", "dependency", "tool.py"),
                  "a", encoding="utf-8") as fh:
            fh.write("# edited in place\n")
        issues = board.issues()
        self.assertTrue(any("differ from HEAD" in issue
                            for issue in issues), issues)
        self.assertNotEqual(board.release_check()[0], 0)

    def test_a_submodule_at_the_wrong_commit_refuses(self):
        board = self.board()
        sub = os.path.join(board.repo, "tooling", "dependency")
        board._init_identity(sub)
        with open(os.path.join(sub, "tool.py"), "w", encoding="utf-8") as fh:
            fh.write("VERSION = 2\n")
        git(sub, "add", "-A")
        git(sub, "commit", "-q", "-m", "move the submodule on")
        problems, facts = board.readiness()
        self.assertEqual(facts["submodules_in_sync"], 0)
        self.assertTrue(
            any("not the one the superproject records" in p["issue"]
                for p in problems), problems)

    def test_an_uninitialised_submodule_refuses(self):
        board = self.board()
        shutil.rmtree(os.path.join(board.repo, "tooling", "dependency"))
        os.makedirs(os.path.join(board.repo, "tooling", "dependency"))
        problems, _facts = board.readiness()
        self.assertTrue(any("not initialised" in p["issue"] for p in problems),
                        problems)


class RequiredEvidenceIsRequired(_Base):

    def test_absent_evidence_refuses(self):
        board = self.board(required_evidence=[EVIDENCE])
        problems, facts = board.readiness()
        self.assertEqual(facts["required_evidence"], 1)
        self.assertTrue(any("required release evidence is absent" in p["issue"]
                            for p in problems), problems)
        self.assertNotEqual(board.release_check()[0], 0)

    def test_uncommitted_evidence_refuses(self):
        board = self.board(required_evidence=[EVIDENCE])
        with open(board.path(EVIDENCE), "w", encoding="utf-8") as fh:
            json.dump({"reviewed_by": "someone"}, fh)
        problems, _facts = board.readiness()
        self.assertTrue(any("not tracked by Git" in p["issue"] or
                            "differ from HEAD" in p["issue"]
                            for p in problems), problems)

    def test_committed_evidence_satisfies_it(self):
        board = self.board(required_evidence=[EVIDENCE])
        with open(board.path(EVIDENCE), "w", encoding="utf-8") as fh:
            json.dump({"reviewed_by": "someone"}, fh)
        board.revalidate_and_commit()
        problems, _facts = board.readiness()
        self.assertEqual(problems, [], problems)


class StaleArtifactsCannotBeReleased(_Base):
    """The design moved and the artifacts did not, or the reverse."""

    def _commit_a_design_change(self, board):
        """Move the configuration the artifacts were generated against."""
        manifest = board.manifest()
        manifest.data["constraint_version"] = "2"
        with open(board.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest.data, fh, indent=2)
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "change the design")

    def test_a_design_change_without_a_rebuild_refuses(self):
        board = self.board()
        self._commit_a_design_change(board)
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("ARCH.PROVENANCE", output)

    def test_rebuilding_and_recommitting_makes_it_releasable_again(self):
        board = self.board()
        self._commit_a_design_change(board)
        self.assertNotEqual(board.release_check()[0], 0)
        board.revalidate_and_commit()
        code, output = board.release_check()
        self.assertEqual(code, 0, output[-3000:])

    def test_an_edited_artifact_refuses(self):
        board = self.board()
        with open(board.path(BOM), "a", encoding="utf-8") as fh:
            fh.write("MUTATED,\n")
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "edit the bom by hand")
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("ARCH.PROVENANCE", output)

    def test_an_unrecorded_artifact_refuses(self):
        """A file in the release directory that the record does not name."""
        board = self.board()
        record_path = board.path(FABRICATION)
        with open(record_path, encoding="utf-8") as fh:
            record = json.load(fh)
        record["artifacts"].pop("bom.csv")
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "drop it from the record")
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("ARCH.PROVENANCE", output)

    def test_a_committed_verdict_about_another_design_refuses(self):
        board = self.board()
        with open(board.path(VALIDATION), encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["source_closure_sha256"] = "0" * 64
        with open(board.path(VALIDATION), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "swap the verdict")
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("about a different design", output)

    def test_a_rejected_verdict_refuses(self):
        board = self.board()
        with open(board.path(VALIDATION), encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["summary"]["verdict"] = "REJECTED"
        with open(board.path(VALIDATION), "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        git(board.repo, "add", "-A")
        git(board.repo, "commit", "-q", "-m", "reject it")
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("committed verdict is 'REJECTED'", output)


class AFailingGateRefuses(_Base):

    def test_a_mandatory_gate_that_cannot_run_refuses(self):
        board = self.board()
        manifest = board.manifest()
        manifest.data["release_profile"]["mandatory_gates"].append(
            "NET.TOPOLOGY")                       # no policy: NOT_APPLICABLE
        with open(board.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest.data, fh, indent=2)
        board.revalidate_and_commit()
        code, output = board.release_check()
        self.assertNotEqual(code, 0, output[-2000:])
        self.assertIn("NOT_APPLICABLE", output)

    def test_a_profile_with_no_mandatory_gates_refuses(self):
        board = self.board()
        manifest = board.manifest()
        manifest.data["release_profile"]["mandatory_gates"] = []
        with open(board.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest.data, fh, indent=2)
        code, output = board.release_check()
        self.assertNotEqual(code, 0)
        self.assertIn("no mandatory gates", output)

    def test_a_board_with_no_release_profile_refuses(self):
        board = self.board()
        manifest = board.manifest()
        manifest.data.pop("release_profile")
        with open(board.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest.data, fh, indent=2)
        code, output = board.release_check()
        self.assertNotEqual(code, 0)
        self.assertIn("no release_profile", output)


class TheRemovedArchitectureStaysRemoved(unittest.TestCase):
    """Concepts deleted because a repository grew them, not a domain."""

    def test_no_ab_experiment_lives_in_the_toolkit(self):
        """Board A, Board B and compute accounting are research, not a
        generic PCBA toolkit."""
        for name in ("pcbqa.benchmark", "pcbqa.compute", "pcbqa.progression"):
            with self.assertRaises(ImportError):
                __import__(name)

    def test_no_backend_dispatch_for_an_unimplemented_solver(self):
        with self.assertRaises(ImportError):
            __import__("pcbqa.backends")

    def test_the_producer_closure_layer_is_gone(self):
        """Nothing in the toolkit reuses an artifact across runs."""
        with self.assertRaises(ImportError):
            __import__("pcbqa.freshness")

    def test_no_fabricator_adapter_registry(self):
        from pcbqa import fabricators
        for name in ("adapter", "FABRICATORS"):
            self.assertFalse(hasattr(fabricators, name), name)
        from pcbqa.fabricators.store import CatalogStore
        for name in ("promote", "observed", "previous_observed",
                     "verification", "record_observation",
                     "record_verification", "freshness"):
            self.assertFalse(hasattr(CatalogStore, name), name)

    def test_no_gate_polices_toolkit_implementation_style(self):
        from pcbqa import core as pcbqa_core
        registered = {entry["id"] for entry in pcbqa_core.registered()}
        for gate_id in ("CFG.THRESHOLD_PARITY", "CFG.NO_RIVAL_THRESHOLDS",
                        "PROV.SOURCE_AUTHORITY", "PROV.DERIVATION_CLOSURE"):
            self.assertNotIn(gate_id, registered)

    def test_no_manifest_this_repository_ships_configures_them(self):
        for path in (paths.REVA_MANIFEST, paths.PORTABILITY_MANIFEST,
                     paths.CLEAN_MANIFEST):
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
            for key in ("source_authority", "constraint_parity"):
                self.assertNotIn(key, doc, "{} still declares {}".format(
                    os.path.basename(path), key))
            reports = doc.get("reports") or {}
            for key in ("implementation_closure", "configuration_excludes"):
                self.assertNotIn(key, reports, key)


class TheOldSyntheticLifecycleIsGone(unittest.TestCase):
    """No release logic may depend on a directory pretending to be Git."""

    #: Identifiers, not English. `published` and `latest.json` are ordinary
    #: words in the fabricator catalogue, which is why the scan below is
    #: restricted to the modules the release lifecycle actually lives in.
    FORBIDDEN = ("latest.json", "release_id", "UNSEALED", "RECEIPT",
                 "clean_room", "cleanroom", "attempts/", "new_attempt",
                 "publish(", ".publish", "published/", "unsealed")

    def _sources(self):
        found = [os.path.join(HERE, "run.py")]
        for name in ("layout.py", "build.py", "release.py", "artifacts.py",
                     "closure.py", "core.py"):
            found.append(os.path.join(paths.PACKAGE, name))
        gates = os.path.join(paths.PACKAGE, "gates")
        found += [os.path.join(gates, n) for n in sorted(os.listdir(gates))
                  if n.endswith(".py")]
        return found

    def test_no_production_source_names_the_old_lifecycle(self):
        offenders = []
        for path in self._sources():
            with open(path, encoding="utf-8") as fh:
                for number, line in enumerate(fh, 1):
                    lowered = line.lower()
                    for word in self.FORBIDDEN:
                        if word.lower() in lowered:
                            offenders.append("{}:{}: {}".format(
                                os.path.relpath(path, HERE), number,
                                line.strip()[:90]))
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_module_of_the_old_lifecycle_survives(self):
        for name in ("pcbqa.cleanroom", "pcbqa.coherence"):
            with self.assertRaises(ImportError):
                __import__(name)

    def test_the_whole_project_is_never_copied(self):
        from pcbqa import core as pcbqa_core
        for name in ("copy_project", "NEVER_COPY", "ORDERABLE_SUFFIXES"):
            self.assertFalse(hasattr(pcbqa_core, name), name)

    def test_the_release_command_is_a_check_and_not_a_publisher(self):
        self.assertFalse(hasattr(run_cli, "cmd_release"))
        self.assertFalse(hasattr(run_cli, "cmd_coherence"))
        self.assertTrue(hasattr(run_cli, "cmd_release_check"))
        self.assertTrue(hasattr(run_cli, "cmd_build"))


if __name__ == "__main__":
    unittest.main()
