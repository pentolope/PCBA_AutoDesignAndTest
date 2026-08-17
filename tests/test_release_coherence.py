"""A release package must agree with itself, and be caught when it does not.

The defect these tests exist for is not a broken file. Every file was well
formed; they came from two different clean-room runs, and the set was a lie -
a validation report describing an archive that was not there, beside an archive
no report described, with a prose note explaining the discrepancy.

So every test here damages the package the way that actually happens: it puts
a file from another, equally valid release beside files from this one. Nothing
is corrupted, nothing is truncated, and a checker that only validates files one
at a time passes every one of them.

The second release is built rather than found. Looking for one in
`verification/out` meant these tests skipped entirely in a fresh checkout,
which is the one place they most need to run.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import coherence                                   # noqa: E402
from pcbqa import core                                        # noqa: E402
from pcbqa.core import Context, Manifest, Status              # noqa: E402
from pcbqa.gates import g_provenance                          # noqa: E402,F401

from tests import consumer                                               # noqa: E402

GATE = "PROV.RELEASE_COHERENCE"


def _live():
    """The registered consumer board, or skip. See tests/consumer.py."""
    return consumer.require()


def _project():
    return consumer.project_root()


def _installed():
    return os.path.join(consumer.project_root(), "generated", "release")


_SCRATCH = []


def _scratch(prefix):
    path = tempfile.mkdtemp(prefix=prefix)
    _SCRATCH.append(path)
    return path


def tearDownModule():
    for path in _SCRATCH:
        shutil.rmtree(path, ignore_errors=True)
    del _SCRATCH[:]


def _names():
    return coherence.member_names(Manifest(_live()))


def _rewrite(path, mutate):
    with open(path, encoding="utf-8") as fh:
        document = json.load(fh)
    mutate(document)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(document, fh, indent=2)


def a_second_coherent_release(names, marker):
    """Another package, internally coherent, from a different notional run.

    Built rather than found. Depending on whatever happened to be left in
    `verification/out` meant these tests skipped entirely in a fresh checkout,
    which is the one place they most need to run - and a test that skips when
    the history is missing is a test that never fails on a clean machine.

    So the installed release is copied and then made *coherent again* around a
    different archive and a different source closure: the release manifest, the
    check reports, the clean-room record, the validation report and the receipt
    are all brought into agreement with each other. The result is a package
    that passes every check on its own, and every one of whose files is wrong
    beside the installed one.
    """
    root = os.path.join(_scratch("pcbqa_donor_"), "release")
    shutil.copytree(_installed(), root)
    closure = hashlib.sha256(marker.encode("utf-8")).hexdigest()

    # A different archive: the same members, repacked, so it is a valid zip
    # and a different file.
    archive = os.path.join(root, names["archive"])
    with zipfile.ZipFile(archive) as source:
        members = [(item.filename, source.read(item.filename))
                   for item in source.infolist()]
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as out:
        for name, payload in members:
            out.writestr(name, payload)

    digests = {role: coherence.sha256_file(os.path.join(root, names[role]))
               for role in ("archive", "bom", "cpl")}

    manifest_path = os.path.join(root, names["manifest"])
    with open(manifest_path, encoding="utf-8") as fh:
        text = fh.read()
    by_name = {names[role]: digest for role, digest in digests.items()}
    lines = []
    for line in text.splitlines():
        if "source closure sha256" in line:
            line = "- source closure sha256: `{}`".format(closure)
        else:
            named = re.match(r"- `([^`]+)` sha256 `[0-9a-f]{64}`", line.strip())
            if named and named.group(1) in by_name:
                line = "- `{}` sha256 `{}`".format(named.group(1),
                                                   by_name[named.group(1)])
        lines.append(line)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    manifest_digest = coherence.sha256_file(manifest_path)

    for rel in sorted(coherence.inventory(root)):
        if rel.startswith("reports/") and rel.endswith(".json"):
            _rewrite(os.path.join(root, rel),
                     lambda d: d.update(source_closure_sha256=closure))

    _rewrite(os.path.join(root, names["clean_room"]),
             lambda d: d.update(source_closure_sha256=closure,
                                run_root=d.get("run_root", "") + marker))

    def fix_validation(document):
        document["clean_room"]["source_closure_sha256"] = closure
        document["clean_room"]["run_root"] = \
            document["clean_room"].get("run_root", "") + marker
        for entry in document.get("gates", []):
            measurements = entry.get("measurements") or {}
            if "source_closure_sha256" in measurements:
                measurements["source_closure_sha256"] = closure
            for item in entry.get("evidence", []):
                if coherence.leaf(item.get("name") or item.get("path")) == \
                        names["archive"]:
                    item["sha256"] = digests["archive"]
                elif coherence.leaf(item.get("name") or item.get("path")) == \
                        names["manifest"]:
                    item["sha256"] = manifest_digest
    _rewrite(os.path.join(root, names["validation"]), fix_validation)

    coherence.write_receipt(root, {"board_id": "donor" + marker,
                                   "attempt_id": "donor" + marker,
                                   "verdict": "ACCEPTED",
                                   "source_closure_sha256": closure,
                                   "members": names})
    return root


@consumer.needed
class TheInstalledReleaseIsOneRun(unittest.TestCase):

    def test_it_is_coherent(self):
        problems, facts = coherence.check(_installed(), _names())
        self.assertEqual(problems, [], "the committed release is not one run")
        self.assertEqual(facts["stage"], "installed")
        self.assertEqual(facts["verdict"], "ACCEPTED")
        self.assertIsInstance(facts["source_closure_sha256"], str,
                              "more than one source closure is named")

    def test_the_gate_agrees(self):
        ctx = Context(Manifest(_live()), _scratch("pcbqa_coh_"))
        result = {r.gate_id: r.to_dict()
                  for r in core.run_all(ctx, only={GATE})}[GATE]
        self.assertEqual(result["status"], Status.PASS, result["findings"])

    def test_the_receipt_accounts_for_every_file_but_itself(self):
        receipt = json.load(open(os.path.join(_installed(),
                                              coherence.RECEIPT_NAME),
                                 encoding="utf-8"))
        self.assertNotIn(coherence.RECEIPT_NAME, receipt["files"])
        listed = set(receipt["files"])
        actual = set(coherence.inventory(_installed()))
        self.assertEqual(listed, actual)
        self.assertTrue(receipt["what_is_hashed"])


@consumer.needed
class AFileFromAnotherRunIsCaught(unittest.TestCase):
    """Each test swaps in one file from another, equally coherent, release."""

    @classmethod
    def setUpClass(cls):
        cls.names = _names()
        cls.donor = a_second_coherent_release(cls.names, "-donor")

    def test_the_donor_is_itself_coherent(self):
        """Otherwise the swaps below would prove nothing about the swap."""
        problems, facts = coherence.check(self.donor, self.names)
        self.assertEqual(problems, [], problems)
        self.assertNotEqual(facts["archive_sha256"],
                            coherence.sha256_file(
                                os.path.join(_installed(), self.names["archive"])),
                            "the donor is the same release, so nothing is "
                            "being swapped")

    def _package_with(self, member):
        """The installed release with one file taken from the donor."""
        root = os.path.join(_scratch("pcbqa_mutate_"), "release")
        shutil.copytree(_installed(), root)
        source = os.path.join(self.donor, member)
        self.assertTrue(os.path.isfile(source), member)
        self.assertNotEqual(coherence.sha256_file(source),
                            coherence.sha256_file(os.path.join(root, member)),
                            "{} is identical in both packages".format(member))
        shutil.copy2(source, os.path.join(root, member))
        return root

    def _problems(self, member):
        root = self._package_with(member)
        problems, _facts = coherence.check(root, self.names)
        self.assertTrue(problems,
                        "swapping {} for one from another run was not "
                        "caught".format(member))
        return problems, root

    def test_an_older_archive_is_caught(self):
        problems, _root = self._problems(self.names["archive"])
        blamed = " ".join(json.dumps(p) for p in problems)
        self.assertIn(self.names["archive"], blamed)
        self.assertIn("different", blamed)

    def test_an_older_release_manifest_is_caught(self):
        problems, _root = self._problems(self.names["manifest"])
        self.assertTrue(any("digest" in p["issue"] or "closure" in p["issue"]
                            or "receipt" in p["issue"] for p in problems),
                        problems)

    def test_an_older_check_report_is_caught(self):
        problems, _root = self._problems("reports/drc.json")
        blamed = " ".join(json.dumps(p) for p in problems)
        self.assertIn("drc.json", blamed)

    def test_an_older_clean_room_record_is_caught(self):
        problems, _root = self._problems(self.names["clean_room"])
        blamed = " ".join(json.dumps(p) for p in problems)
        self.assertIn(self.names["clean_room"], blamed)

    def test_an_older_validation_report_is_caught(self):
        problems, _root = self._problems(self.names["validation"])
        blamed = " ".join(json.dumps(p) for p in problems)
        self.assertIn(self.names["validation"], blamed)

    def test_a_prose_note_does_not_rescue_any_of_them(self):
        """The exact thing that was tried before: explain it and move on."""
        root = self._package_with(self.names["archive"])
        with open(os.path.join(root, "PROVENANCE_NOTE.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# Why the archive is older\n\nThe Gerbers differ only "
                     "in KiCad timestamp comments, so this is fine.\n")
        problems, _facts = coherence.check(root, self.names)
        self.assertTrue(problems, "a note made the mismatch pass")


class AnIncompletePackageIsCaught(unittest.TestCase):

    def setUp(self):
        self.names = _names()
        self.work = _scratch("pcbqa_incomplete_")
        self.root = os.path.join(self.work, "release")
        shutil.copytree(_installed(), self.root)

    def test_a_missing_receipt_is_caught(self):
        os.remove(os.path.join(self.root, coherence.RECEIPT_NAME))
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any("receipt" in p["issue"] for p in problems),
                        problems)

    def test_an_extra_file_is_caught(self):
        with open(os.path.join(self.root, "left_over.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("from some other run\n")
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any(p.get("file") == "left_over.txt"
                            for p in problems), problems)

    def test_a_deleted_member_is_caught(self):
        os.remove(os.path.join(self.root, "reports", "erc.json"))
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any("reports/erc.json" == p.get("file")
                            for p in problems), problems)

    def _reissue_receipt(self):
        """Make the receipt honest about the package as it now stands.

        This is the interesting case: an inventory that accurately describes an
        incomplete release. Nothing is stale and nothing disagrees - the
        package is simply missing a report, and saying so accurately is not the
        same as being complete.
        """
        os.remove(os.path.join(self.root, coherence.RECEIPT_NAME))
        coherence.write_receipt(self.root, {"attempt_id": "reissued",
                                            "verdict": "ACCEPTED"})

    def test_a_missing_erc_report_fails_even_with_an_accurate_receipt(self):
        os.remove(os.path.join(self.root, "reports", "erc.json"))
        self._reissue_receipt()
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any(p.get("file") == "reports/erc.json"
                            for p in problems), problems)

    def test_a_missing_drc_report_fails_even_with_an_accurate_receipt(self):
        os.remove(os.path.join(self.root, "reports", "drc.json"))
        self._reissue_receipt()
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any(p.get("file") == "reports/drc.json"
                            for p in problems), problems)

    def test_an_empty_reports_directory_fails_even_with_an_accurate_receipt(self):
        shutil.rmtree(os.path.join(self.root, "reports"))
        os.makedirs(os.path.join(self.root, "reports"))
        self._reissue_receipt()
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any(p.get("file") == "reports/" for p in problems),
                        problems)
        self.assertEqual(len(self.names["reports"]), 2,
                         "this board generates two check reports")

    def test_every_report_present_still_has_its_closure_checked(self):
        path = os.path.join(self.root, "reports", "drc.json")
        _rewrite(path, lambda d: d.update(source_closure_sha256="f" * 64))
        self._reissue_receipt()
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any("more than one clean-room run" in p["issue"]
                            for p in problems), problems)

    def test_a_rejected_verdict_cannot_ship(self):
        path = os.path.join(self.root, self.names["validation"])
        doc = json.load(open(path, encoding="utf-8"))
        doc["summary"]["verdict"] = "REJECTED"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        problems, _facts = coherence.check(self.root, self.names)
        self.assertTrue(any("did not accept" in p["issue"] for p in problems),
                        problems)

    def test_a_candidate_package_is_recognised_rather_than_failed(self):
        """Mid-build there are no reports yet, and that is not incoherence."""
        for name in (self.names["validation"], self.names["clean_room"],
                     self.names["unsealed"], coherence.RECEIPT_NAME):
            os.remove(os.path.join(self.root, name))
        shutil.rmtree(os.path.join(self.root, "reports"))
        problems, facts = coherence.check(self.root, self.names,
                                          require_receipt=False)
        self.assertEqual(problems, [], problems)
        self.assertEqual(facts["stage"], "candidate")


class AnIncoherentPackageIsNotPublished(unittest.TestCase):
    """The check has to run before the rename, not after it.

    Everything else here tests the checker. This tests that the release is
    actually wired to it: a package that becomes incoherent after the gates
    have passed and the files have been assembled must not become a published
    release, and must not move `latest.json`.

    The incoherence is injected at the last possible moment - after the receipt
    is written, which is after validation - so nothing earlier can catch it and
    only the post-assembly check can.
    """

    def test_a_post_assembly_incoherence_blocks_publication(self):
        import run
        from pcbqa import coherence as module
        from pcbqa.core import copy_project
        from pcbqa.parallel import ENV_OUTPUT_ROOT

        work = _scratch("pcbqa_publish_")
        project = os.path.join(work, "project")
        copy_project(_project(), project)
        for unwanted in ("verification", "build", "candidates"):
            shutil.rmtree(os.path.join(project, unwanted), ignore_errors=True)

        document = json.load(open(_live(), encoding="utf-8"))
        document["board_id"] = "coherence-publication-block"
        document["project_root"] = project
        manifest_path = os.path.join(work, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2)

        names = coherence.member_names(Manifest(manifest_path))
        original = module.write_receipt

        def receipt_then_damage(root, meta):
            """Write a true receipt, then make one file untrue."""
            path = original(root, meta)
            with open(os.path.join(root, names["archive"]), "ab") as fh:
                fh.write(b"\x00")          # a byte nobody accounted for
            return path

        module.write_receipt = receipt_then_damage
        self.addCleanup(setattr, module, "write_receipt", original)

        saved = os.environ.get(ENV_OUTPUT_ROOT)
        os.environ[ENV_OUTPUT_ROOT] = work
        self.addCleanup(os.environ.__setitem__, ENV_OUTPUT_ROOT, saved) \
            if saved else self.addCleanup(os.environ.pop, ENV_OUTPUT_ROOT, None)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = run.cmd_release(manifest_path)
        printed = captured.getvalue()

        self.assertNotEqual(code, 0,
                            "an incoherent package was published:\n"
                            + printed[-3000:])
        self.assertIn("RELEASE BLOCKED", printed)
        self.assertIn("release:coherence", printed)
        board_out = os.path.join(work, "out", document["board_id"])
        published = os.path.join(board_out, "published")
        self.assertFalse(os.path.isdir(published) and os.listdir(published),
                         "a release directory was published anyway")
        self.assertFalse(os.path.isfile(os.path.join(board_out, "latest.json")),
                         "latest.json was moved for a blocked release")


class EvidenceIsFoundWhicheverPlatformWroteIt(unittest.TestCase):
    """A recorded path is data, not a path on this machine.

    validation.json carries absolute paths from the machine that produced it.
    Matching them with os.path.basename asks the *reading* host how paths are
    spelled, so a report written on Windows reads on Linux as one long
    filename and the archive it validated can never be found - the package
    then looks incoherent on one platform and coherent on the other, which is
    worse than either.

    These run the real lookup, on this host, over strings in all three shapes.
    Nothing is monkeypatched and no host is pretended to be another.
    """

    WINDOWS = ("C:\\Users\\someone\\PCB\\verification\\out\\board\\attempts"
               "\\20260812T163729Z-491cf08b\\build\\{}")
    POSIX = "/home/someone/pcb/verification/out/board/attempts/x/build/{}"
    MIXED = "C:/Users/someone\\PCB/verification\\out/board\\build/{}"

    def test_the_leaf_is_the_same_whichever_separator_wrote_it(self):
        for shape in (self.WINDOWS, self.POSIX, self.MIXED):
            self.assertEqual(coherence.leaf(shape.format("archive.zip")),
                             "archive.zip", shape)
        self.assertEqual(coherence.leaf("archive.zip"), "archive.zip")
        self.assertEqual(coherence.leaf(""), "")
        self.assertEqual(coherence.leaf(None), "")

    def _package_with_evidence_paths(self, shape, use_name):
        """The installed release, with its evidence paths rewritten."""
        root = os.path.join(_scratch("pcbqa_paths_"), "release")
        shutil.copytree(_installed(), root)
        names = _names()

        def rewrite(document):
            for entry in document.get("gates", []):
                for item in entry.get("evidence", []):
                    tail = coherence.leaf(item.get("name") or item.get("path"))
                    item["path"] = shape.format(tail)
                    if use_name:
                        item["name"] = tail
                    else:
                        item.pop("name", None)
        _rewrite(os.path.join(root, names["validation"]), rewrite)
        os.remove(os.path.join(root, coherence.RECEIPT_NAME))
        coherence.write_receipt(root, {"attempt_id": "paths",
                                       "verdict": "ACCEPTED"})
        return root, names

    def test_evidence_written_on_any_platform_is_found(self):
        for shape in (self.WINDOWS, self.POSIX, self.MIXED):
            for use_name in (True, False):
                root, names = self._package_with_evidence_paths(shape,
                                                                use_name)
                problems, _facts = coherence.check(root, names)
                self.assertEqual(
                    problems, [],
                    "evidence recorded as {!r} (name field: {}) could not be "
                    "matched: {}".format(shape, use_name, problems))

    def test_a_wrong_digest_still_fails_whatever_the_separator(self):
        """Finding the record must not become accepting whatever it says."""
        for shape in (self.WINDOWS, self.POSIX, self.MIXED):
            root, names = self._package_with_evidence_paths(shape, True)

            def spoil(document):
                for entry in document.get("gates", []):
                    if entry.get("gate") != "ARCH.CONTENTS":
                        continue
                    for item in entry.get("evidence", []):
                        item["sha256"] = "0" * 64
            _rewrite(os.path.join(root, names["validation"]), spoil)
            os.remove(os.path.join(root, coherence.RECEIPT_NAME))
            coherence.write_receipt(root, {"attempt_id": "spoiled",
                                           "verdict": "ACCEPTED"})
            problems, _facts = coherence.check(root, names)
            self.assertTrue(
                any("ARCH.CONTENTS validated a different archive" in p["issue"]
                    for p in problems),
                "a wrong digest passed for {!r}: {}".format(shape, problems))

    def test_the_installed_report_records_stable_names(self):
        """New evidence should not need path archaeology to be read at all."""
        document = json.load(open(os.path.join(_installed(), "validation.json"),
                                  encoding="utf-8"))
        named = 0
        for entry in document.get("gates", []):
            for item in entry.get("evidence", []):
                self.assertIn("name", item,
                              "{} recorded evidence with no stable "
                              "name".format(entry.get("gate")))
                self.assertNotIn("\\", item["name"])
                named += 1
        self.assertGreater(named, 0)


if __name__ == "__main__":
    unittest.main()
