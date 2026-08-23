"""Regression tests for the parallel test runner itself.

A test runner that loses a failure is worse than no runner at all: every one of
these checks exists because the failure mode it describes would otherwise turn
a broken build green. The runner is exercised against synthetic test modules
written into a temporary directory, so these tests are fast and say nothing
about any board.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa import parallel                              # noqa: E402

ENV_SHARED = "PCBQA_RUNNER_TEST_SHARED"

# Synthetic suites. Each is written to disk, discovered and run by the real
# runner; none of them import pcbnew or Shapely.
MODULES = {
    "test_alpha.py": '''
        import os, time, unittest
        class Alpha(unittest.TestCase):
            def test_one(self): time.sleep(0.3); self.assertTrue(True)
            def test_two(self): time.sleep(0.3); self.assertTrue(True)
        ''',
    "test_beta.py": '''
        import os, time, unittest
        class Beta(unittest.TestCase):
            def test_three(self): time.sleep(0.3); self.assertTrue(True)
            def test_four(self): time.sleep(0.3); self.assertTrue(True)
        ''',
}

FAILING = '''
    import unittest
    class Failing(unittest.TestCase):
        def test_passes(self): self.assertTrue(True)
        def test_asserts(self): self.assertEqual(2, 3, "deliberate assertion")
    '''

RAISING = '''
    import unittest
    class Raising(unittest.TestCase):
        def test_passes(self): self.assertTrue(True)
        def test_raises(self): raise RuntimeError("deliberate worker exception")
    '''

CRASHING = '''
    import os, unittest
    class Crashing(unittest.TestCase):
        def test_passes(self): self.assertTrue(True)
        def test_exits(self):
            os._exit(0)          # leaves the queue unfinished, reports nothing
    '''

HANGING = '''
    import time, unittest
    class Hanging(unittest.TestCase):
        def test_passes(self): self.assertTrue(True)
        def test_hangs(self): time.sleep(600)
    '''

RECORDING = '''
    import os, time, unittest
    class Recording(unittest.TestCase):
        def _record(self):
            root = os.environ["PCBQA_TEST_OUTPUT_ROOT"]
            worker = os.environ.get("PCBQA_WORKER_ID")
            shared = os.environ["PCBQA_RUNNER_TEST_SHARED"]
            os.makedirs(root, exist_ok=True)
            # A fixed name every test writes: if two tests ever shared an
            # output root concurrently, this is where they would collide.
            with open(os.path.join(root, "project.lck"), "w") as fh:
                fh.write(self.id())
            with open(os.path.join(shared,
                                   self.id() + ".txt"), "w") as fh:
                fh.write(root + "\\n" + str(worker))
            # Then hold until all four are in flight. This used to be a
            # 0.3s sleep, which only made concurrency LIKELY: on a loaded
            # machine one worker could finish a test and claim the next
            # before its siblings were scheduled, and the run collapsed
            # onto a single output root. The test then failed for want of
            # CPU rather than for want of isolation.
            #
            # A barrier makes concurrency REQUIRED. Every worker that
            # claims one of these tests must still be holding it when the
            # last one starts, so four can only complete if four workers
            # really ran them at once - which is the condition the
            # assertions are about.
            def arrived():
                return len([n for n in os.listdir(shared)
                            if n.endswith('.txt')])
            deadline = time.time() + 120
            while time.time() < deadline:
                if arrived() >= 4:
                    return
                time.sleep(0.01)
            raise AssertionError(
                'only {} of 4 tests were ever in flight together; the '
                'pool did not run them concurrently'.format(arrived()))
        def test_a(self): self._record()
        def test_b(self): self._record()
        def test_c(self): self._record()
        def test_d(self): self._record()
    '''


class _Sandbox:
    """A throwaway importable package holding synthetic test modules."""

    _serial = 0

    def __init__(self, modules):
        # A unique package name per sandbox: Python caches the first `suite`
        # it imports, so reusing the name would silently run the previous
        # sandbox's modules and make these tests test nothing.
        _Sandbox._serial += 1
        self.package = "pcbqa_suite_{}_{}".format(os.getpid(), _Sandbox._serial)
        self.top = tempfile.mkdtemp(prefix="pcbqa_runner_")
        self.tests = os.path.join(self.top, self.package)
        os.makedirs(self.tests)
        open(os.path.join(self.tests, "__init__.py"), "w").close()
        for name, body in modules.items():
            with open(os.path.join(self.tests, name), "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(body).lstrip())
        self.output = os.path.join(self.top, "out")
        os.makedirs(self.output)

    def run(self, **kwargs):
        kwargs.setdefault("output_root", self.output)
        kwargs.setdefault("stream", open(os.devnull, "w"))
        stream = kwargs["stream"]
        try:
            return parallel.run(self.tests, self.top, **kwargs)
        finally:
            if stream is not sys.stdout:
                stream.close()

    def close(self):
        for name in [m for m in sys.modules if m.split(".")[0] == self.package]:
            sys.modules.pop(name, None)
        while self.top in sys.path:
            sys.path.remove(self.top)
        shutil.rmtree(self.top, ignore_errors=True)


class RunnerContract(unittest.TestCase):
    """What the parent process must never fail to notice."""

    def _sandbox(self, modules):
        box = _Sandbox(modules)
        self.addCleanup(box.close)
        return box

    def test_serial_and_parallel_execute_the_same_tests(self):
        box = self._sandbox(MODULES)
        serial_code, serial = box.run(jobs="1")
        parallel_code, par = box.run(jobs="4")
        self.assertEqual(serial_code, 0)
        self.assertEqual(parallel_code, 0)
        self.assertEqual([t["id"] for t in serial["tests"]],
                         [t["id"] for t in par["tests"]],
                         "the same tests must run, in the same reported order, "
                         "however many workers there are")
        self.assertEqual(len(serial["tests"]), 4)
        self.assertEqual(serial["workers"], 1)
        self.assertGreater(par["workers"], 1)

    def test_reported_order_is_discovery_order_not_completion_order(self):
        box = self._sandbox(MODULES)
        _code, summary = box.run(jobs="4")
        ids = [t["id"] for t in summary["tests"]]
        self.assertEqual(ids, sorted(ids),
                         "results arrive out of order and must be re-sorted")

    def test_assertion_failure_reaches_the_parent(self):
        box = self._sandbox({"test_failing.py": FAILING})
        code, summary = box.run(jobs="2")
        self.assertEqual(code, 1, "a failed assertion in a worker must make the "
                                  "parent exit nonzero")
        outcomes = {t["id"].split(".")[-1]: t["outcome"] for t in summary["tests"]}
        self.assertEqual(outcomes["test_asserts"], "fail")
        self.assertEqual(outcomes["test_passes"], "ok")
        detail = next(t["detail"] for t in summary["tests"]
                      if t["id"].endswith("test_asserts"))
        self.assertIn("deliberate assertion", detail,
                      "the failure text must survive the process boundary")

    def test_worker_exception_reaches_the_parent(self):
        box = self._sandbox({"test_raising.py": RAISING})
        code, summary = box.run(jobs="2")
        self.assertEqual(code, 1)
        row = next(t for t in summary["tests"] if t["id"].endswith("test_raises"))
        self.assertEqual(row["outcome"], "error")
        self.assertIn("deliberate worker exception", row["detail"])

    def test_worker_that_exits_without_a_result_is_an_error(self):
        box = self._sandbox({"test_crashing.py": CRASHING})
        code, summary = box.run(jobs="2", timeout_s=120)
        self.assertEqual(code, 1, "a worker that dies mid-test must not be "
                                  "mistaken for a worker that finished")
        outcomes = {t["id"]: t["outcome"] for t in summary["tests"]}
        vanished = [i for i, o in outcomes.items()
                    if i.endswith("test_exits") and o == "missing"]
        worker_errors = [t for t in summary["tests"]
                         if t["id"].startswith("<worker")]
        self.assertTrue(vanished or worker_errors,
                        "the vanished test, the dead worker, or both must be "
                        "reported: {}".format(outcomes))

    def test_timed_out_worker_is_terminated_and_reported(self):
        box = self._sandbox({"test_hanging.py": HANGING})
        code, summary = box.run(jobs="2", timeout_s=5)
        self.assertEqual(code, 1)
        self.assertTrue(
            any(t["outcome"] in ("missing", "error") for t in summary["tests"]),
            "a stalled worker must surface as a missing or errored result")
        # And it must actually be gone, not left running.
        for proc in getattr(parallel.mp, "active_children", lambda: [])():
            self.assertFalse(proc.is_alive(),
                             "runner left a child process alive after timeout")

    def test_concurrent_tests_never_share_an_output_directory(self):
        box = self._sandbox({"test_recording.py": RECORDING})
        shared = os.path.join(box.top, "shared")
        os.makedirs(shared)
        os.environ[ENV_SHARED] = shared
        self.addCleanup(os.environ.pop, ENV_SHARED, None)
        code, summary = box.run(jobs="4")
        self.assertEqual(code, 0, summary)

        records = {}
        for name in os.listdir(shared):
            with open(os.path.join(shared, name), encoding="utf-8") as fh:
                root, worker = fh.read().splitlines()
            records[name[:-4]] = (os.path.realpath(root), worker)
        self.assertEqual(len(records), 4)
        for root, _worker in records.values():
            self.assertTrue(root.startswith(os.path.realpath(box.output)),
                            "tests must write under the run's output root, "
                            "never into the repository")
        by_worker = {}
        for root, worker in records.values():
            by_worker.setdefault(worker, set()).add(root)
        for worker, roots in by_worker.items():
            self.assertEqual(len(roots), 1,
                             "one worker, one output root")
        roots = {root for root, _ in records.values()}
        self.assertEqual(len(roots), len(by_worker),
                         "two workers must never resolve to the same directory")
        # Guaranteed by the barrier in RECORDING rather than hoped for: the
        # four tests cannot all have completed unless four workers held them
        # at the same moment. Kept as an explicit statement so the assertions
        # above can never pass vacuously on a run that quietly serialised.
        self.assertGreater(len(roots), 1,
                           "four concurrent tests resolved to one output root")

    def test_repeated_runs_are_stable(self):
        box = self._sandbox(MODULES)
        first_code, first = box.run(jobs="4")
        second_code, second = box.run(jobs="4")
        self.assertEqual(first_code, second_code)
        self.assertEqual([t["id"] for t in first["tests"]],
                         [t["id"] for t in second["tests"]])
        self.assertEqual([t["outcome"] for t in first["tests"]],
                         [t["outcome"] for t in second["tests"]])

    def test_job_resolution_never_exceeds_the_work_available(self):
        self.assertEqual(parallel.resolve_jobs("8", 3), 3)
        self.assertEqual(parallel.resolve_jobs("1", 50), 1)
        self.assertGreaterEqual(parallel.resolve_jobs("auto", 50), 1)
        self.assertLessEqual(parallel.resolve_jobs("auto", 2), 2)
        with self.assertRaises(ValueError):
            parallel.resolve_jobs("0", 5)

    def test_empty_discovery_is_a_failure_not_a_pass(self):
        box = self._sandbox({})
        code, summary = box.run(jobs="2")
        self.assertEqual(code, 1, "a run that discovered nothing has proved "
                                  "nothing and must not report success")
        self.assertEqual(summary["tests"], [])


if __name__ == "__main__":
    unittest.main()
