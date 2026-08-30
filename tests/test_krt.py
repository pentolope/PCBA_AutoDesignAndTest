"""KRT discovery: deterministic order, disabled installations never
execute, ambiguity refuses, and provenance records the identity a
routing run actually used."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import krt                               # noqa: E402
from pcbqa.krt import KRTError                      # noqa: E402
from tests import synth                             # noqa: E402


def _make_root(base, name, native=True):
    root = os.path.join(base, name)
    os.makedirs(os.path.join(root, "py_router"), exist_ok=True)
    os.makedirs(os.path.join(root, "rust_router"), exist_ok=True)
    with open(os.path.join(root, "VERSION"), "w",
              encoding="utf-8") as handle:
        handle.write("9.9.9-test\n")
    with open(os.path.join(root, "py_router", "route.py"), "w",
              encoding="utf-8") as handle:
        handle.write("# fixture router entry\n")
    if native:
        # A pure-python stand-in is enough for the import probe:
        # the contract is "importable under the named
        # interpreter", not "written in Rust". Real installations
        # ship grid_router.so, which wins the search order.
        with open(os.path.join(root, "rust_router",
                               "grid_router.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("__version__ = '9.9.9-native'\n")
    return root


class ResolutionIsDeterministicAndFailClosed(unittest.TestCase):

    def setUp(self):
        self.base = synth.tempdir("krt-resolve")
        import shutil
        for stale in os.listdir(self.base):
            shutil.rmtree(os.path.join(self.base, stale),
                          ignore_errors=True)

    def test_configured_checkout_resolves(self):
        root = _make_root(self.base, "devcheckout")
        resolved = krt.resolve(configured=root, environ={})
        self.assertEqual(resolved["path"],
                         os.path.realpath(root))
        self.assertEqual(resolved["origin"],
                         "configured checkout")

    def test_override_beats_configured(self):
        first = _make_root(self.base, "first")
        second = _make_root(self.base, "second")
        resolved = krt.resolve(override=first, configured=second,
                               environ={})
        self.assertEqual(resolved["path"],
                         os.path.realpath(first))

    def test_disabled_pcm_path_refuses_everywhere(self):
        disabled = _make_root(
            os.path.join(self.base, "disabled_pcm_plugins",
                         "10.0"), "old_pcm")
        with self.assertRaisesRegex(KRTError,
                                    "disabled_pcm_plugins"):
            krt.resolve(configured=disabled, environ={},
                        vendored=None)
        # And a scan never even considers it.
        with self.assertRaisesRegex(KRTError, "no KiCadRouting"):
            krt.resolve(plugin_dirs=[os.path.join(
                self.base, "disabled_pcm_plugins", "10.0")],
                environ={}, vendored=None)

    def test_missing_krt_fails_clearly(self):
        with self.assertRaisesRegex(KRTError,
                                    "no KiCadRoutingTools"):
            krt.resolve(plugin_dirs=[os.path.join(self.base,
                                                  "empty")],
                        environ={}, vendored=None)
        with self.assertRaisesRegex(KRTError, "not a "
                                    "KiCadRoutingTools"):
            krt.resolve(configured=os.path.join(self.base,
                                                "nonsense"),
                        environ={}, vendored=None)

    def test_conflicting_installations_refuse_as_ambiguous(self):
        plugins = os.path.join(self.base, "plugins")
        _make_root(plugins, "KiCadRoutingTools")
        _make_root(plugins, "com_github_other_copy")
        with self.assertRaisesRegex(KRTError, "ambiguous"):
            krt.resolve(plugin_dirs=[plugins], environ={},
                        vendored=None)

    def test_single_plugin_installation_resolves(self):
        plugins = os.path.join(self.base, "plugins-single")
        root = _make_root(plugins, "KiCadRoutingTools")
        resolved = krt.resolve(plugin_dirs=[plugins], environ={},
                               vendored=None)
        self.assertEqual(resolved["path"],
                         os.path.realpath(root))


class TheVendoredSubmoduleIsTheDefaultSource(unittest.TestCase):
    """The router ships with the toolkit, so a recursive clone routes
    without a sibling checkout - but a named source still outranks
    it, and an absent one must not pretend to be present."""

    def setUp(self):
        self.base = synth.tempdir("krt-vendored")
        import shutil
        for stale in os.listdir(self.base):
            shutil.rmtree(os.path.join(self.base, stale),
                          ignore_errors=True)

    def test_the_real_submodule_resolves_and_is_a_krt_root(self):
        path = krt.vendored_path()
        self.assertIsNotNone(
            path, "the toolkit ships KiCadRoutingTools as a submodule; "
                  "run git submodule update --init --recursive")
        resolved = krt.resolve(environ={})
        self.assertEqual(resolved["origin"], "vendored submodule")
        self.assertEqual(resolved["path"], os.path.realpath(path))

    def test_it_beats_a_plugin_installation(self):
        plugins = os.path.join(self.base, "plugins")
        _make_root(plugins, "KiCadRoutingTools")
        vendored = _make_root(self.base, "vendored")
        resolved = krt.resolve(plugin_dirs=[plugins], environ={},
                               vendored=vendored)
        self.assertEqual(resolved["path"], os.path.realpath(vendored))
        self.assertEqual(resolved["origin"], "vendored submodule")

    def test_every_named_source_still_outranks_it(self):
        vendored = _make_root(self.base, "vendored2")
        named = _make_root(self.base, "named")
        for kwargs, origin in (
                ({"override": named}, "override"),
                ({"configured": named}, "configured checkout")):
            resolved = krt.resolve(environ={}, vendored=vendored,
                                   **kwargs)
            self.assertEqual(resolved["path"],
                             os.path.realpath(named), origin)
            self.assertEqual(resolved["origin"], origin)
        resolved = krt.resolve(environ={"PCB_KRT_PATH": named},
                               vendored=vendored)
        self.assertEqual(resolved["path"], os.path.realpath(named))

    def test_a_vendored_path_that_is_not_a_root_is_an_error(self):
        with self.assertRaisesRegex(KRTError, "not a KiCadRoutingTools"):
            krt.resolve(environ={},
                        vendored=os.path.join(self.base, "nothing"))

    def test_an_uninitialised_submodule_reads_as_absent(self):
        """A non-recursive clone leaves an EMPTY directory where the
        submodule goes. That must read as "no vendored router", not
        as a broken one - otherwise the default path would refuse on
        a checkout that simply has not run `submodule update`.

        This exercises the DEFAULT path, by moving VENDORED itself:
        passing `vendored=` explicitly would take the named-source
        branch, which is a different contract (see the test below).
        """
        empty = os.path.join(self.base, "uninitialised")
        os.makedirs(empty, exist_ok=True)
        original = krt.VENDORED
        krt.VENDORED = empty
        try:
            self.assertIsNone(krt.vendored_path())
            # ...so resolution falls through to the scan, and with no
            # installation to find it refuses by name rather than
            # returning an empty directory as a router.
            with self.assertRaisesRegex(KRTError, "no KiCadRoutingTools"):
                krt.resolve(environ={}, plugin_dirs=[])
            # ...and a real one at that path is picked up again.
            _make_root(self.base, "uninitialised")
            self.assertEqual(krt.vendored_path(), empty)
            self.assertEqual(krt.resolve(environ={})["origin"],
                             "vendored submodule")
        finally:
            krt.VENDORED = original

    def test_the_refusal_names_the_submodule_and_the_remedy(self):
        """Vendoring makes "cloned without submodules" the likeliest
        way to have no router at all, so the refusal a user actually
        reads must name that cause and the command that fixes it -
        not just enumerate the sources it did not find."""
        original = krt.VENDORED
        krt.VENDORED = os.path.join(self.base, "absent")
        try:
            with self.assertRaises(KRTError) as caught:
                krt.resolve(environ={}, plugin_dirs=[])
        finally:
            krt.VENDORED = original
        message = str(caught.exception)
        self.assertIn("vendored", message.lower())
        self.assertIn(os.path.join(self.base, "absent"), message)
        self.assertIn("git submodule update --init --recursive", message)

    def test_a_named_vendored_path_is_held_to_the_named_contract(self):
        """Named explicitly and wrong is an ERROR, never a silent
        fall-through - the same rule every other named source obeys.
        Only the DEFAULT may be absent quietly."""
        empty = os.path.join(self.base, "named-but-empty")
        os.makedirs(empty, exist_ok=True)
        with self.assertRaisesRegex(KRTError, "not a KiCadRoutingTools"):
            krt.resolve(environ={}, vendored=empty)


class ProvenanceRecordsTheActualIdentity(unittest.TestCase):

    def setUp(self):
        self.base = synth.tempdir("krt-prov")
        import shutil
        for stale in os.listdir(self.base):
            shutil.rmtree(os.path.join(self.base, stale),
                          ignore_errors=True)

    def _git_root(self):
        root = _make_root(self.base, "gitted")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "fixture"],
            cwd=root, check=True)
        return root

    def test_version_sha_native_and_python_are_recorded(self):
        root = self._git_root()
        record = krt.provenance(root, sys.executable)
        self.assertEqual(record["version"], "9.9.9-test")
        self.assertEqual(len(record["git"]["sha"]), 40)
        self.assertIs(record["git"]["dirty"], False)
        self.assertEqual(record["grid_router"]["version"],
                         "9.9.9-native")
        self.assertEqual(len(record["grid_router"]["sha256"]), 64)
        self.assertEqual(record["python"]["executable"],
                         sys.executable)

    def test_dirty_state_is_recorded_and_can_refuse(self):
        root = self._git_root()
        with open(os.path.join(root, "VERSION"), "a",
                  encoding="utf-8") as handle:
            handle.write("# experiment\n")
        record = krt.provenance(root, sys.executable)
        self.assertIs(record["git"]["dirty"], True)
        with self.assertRaisesRegex(KRTError, "dirty"):
            krt.provenance(root, sys.executable,
                           require_clean=True)

    def test_wrong_python_environment_refuses(self):
        # An interpreter that cannot import pcbnew - here, one
        # that does not exist at all - refuses by name; and the
        # interpreter this suite runs under (KiCad's own) passes.
        with self.assertRaises(KRTError):
            krt.verify_environment(os.path.join(
                self.base, "no-such-python.exe"))
        if _has_pcbnew():
            record = krt.verify_environment(sys.executable)
            self.assertEqual(record["executable"],
                             sys.executable)

    def test_missing_native_router_refuses(self):
        root = _make_root(self.base, "no-native", native=False)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        with self.assertRaisesRegex(KRTError, "grid_router"):
            krt.provenance(root, sys.executable)

    def test_identity_digest_moves_with_the_sha(self):
        root = self._git_root()
        record = krt.provenance(root, sys.executable)
        first = krt.identity_digest(record)
        record["git"]["sha"] = "f" * 40
        self.assertNotEqual(first, krt.identity_digest(record))

    def test_two_different_dirty_edits_are_two_identities(self):
        """A boolean dirty flag cannot tell two uncommitted edits
        apart; the divergence digest must."""
        root = self._git_root()
        with open(os.path.join(root, "py_router", "route.py"),
                  "a", encoding="utf-8") as handle:
            handle.write("# edit one\n")
        first = krt.identity_digest(
            krt.provenance(root, sys.executable))
        with open(os.path.join(root, "py_router", "route.py"),
                  "a", encoding="utf-8") as handle:
            handle.write("# edit two\n")
        second = krt.identity_digest(
            krt.provenance(root, sys.executable))
        self.assertNotEqual(first, second)

    def test_unprovable_cleanliness_refuses_require_clean(self):
        """git status failing is silence, and silence never passes
        a cleanliness requirement."""
        root = self._git_root()
        original = krt._git

        def broken(path, *arguments):
            if arguments and arguments[0] == "status":
                return None, "simulated status failure"
            return original(path, *arguments)

        krt._git = broken
        try:
            record = krt.provenance(root, sys.executable)
            self.assertIsNone(record["git"]["dirty"])
            with self.assertRaisesRegex(KRTError, "unprovable"):
                krt.provenance(root, sys.executable,
                               require_clean=True)
        finally:
            krt._git = original

    def test_a_python_stand_in_router_is_labelled_as_one(self):
        root = self._git_root()
        record = krt.provenance(root, sys.executable)
        self.assertEqual(record["grid_router"]["kind"],
                         "python-source-stand-in")


def _has_pcbnew():
    try:
        import pcbnew                                # noqa: F401
        return True
    except ImportError:
        return False


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
