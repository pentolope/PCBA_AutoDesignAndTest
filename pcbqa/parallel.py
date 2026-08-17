"""Process-based parallel test runner.

The self-test suite spends almost all of its time inside KiCad and Shapely, so
threads would not help. This distributes whole test IDs across OS processes
started with spawn semantics, which is what Windows supports and what keeps
each worker's pcbnew state independent.

Design constraints that shaped this:

  * only serialisable job descriptions cross a process boundary - test IDs and
    directory paths, never a loaded BOARD or a Shapely geometry;
  * every worker gets its own output root, so no two workers can collide on a
    KiCad lock file or overwrite each other's reports;
  * a worker that crashes, hangs or returns nothing is an error, not a silent
    pass;
  * results arrive out of order and are re-sorted into discovery order, so the
    printed report is deterministic.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue
import shutil
import sys
import tempfile
import time
import traceback
import unittest

ENV_OUTPUT_ROOT = "PCBQA_TEST_OUTPUT_ROOT"
ENV_WORKER = "PCBQA_WORKER_ID"


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def discover(tests_dir, top_level):
    """Every test ID, in a stable order that does not depend on scheduling."""
    suite = unittest.TestLoader().discover(tests_dir, top_level_dir=top_level)
    ids = []

    def walk(node):
        if isinstance(node, unittest.TestSuite):
            for child in node:
                walk(child)
        else:
            ids.append(node.id())
    walk(suite)
    return sorted(ids)


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------

class _Collector(unittest.TestResult):
    """Captures a structured outcome per test instead of printing."""

    def __init__(self):
        super().__init__()
        self.records = {}
        self._started = {}

    def startTest(self, test):
        super().startTest(test)
        self._started[test.id()] = time.time()

    def _finish(self, test, outcome, detail=""):
        started = self._started.get(test.id(), time.time())
        self.records[test.id()] = {
            "id": test.id(), "outcome": outcome,
            "duration_s": round(time.time() - started, 3),
            "detail": detail,
        }

    def addSuccess(self, test):
        super().addSuccess(test)
        self._finish(test, "ok")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._finish(test, "fail", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._finish(test, "error", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._finish(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._finish(test, "expected_failure", "")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._finish(test, "unexpected_success", "")

    def addSubTest(self, test, subtest, err):
        """Record subtest outcomes against the parent test.

        Without this, a test using `subTest` whose subtest fails calls neither
        addSuccess nor addFailure for the test itself, so the worker returns
        nothing for it and the parent reports "test produced no result
        record" - technically true and completely useless, because the actual
        assertion failure is discarded. A runner that turns a real failure
        into a shrug is the failure mode this whole file exists to prevent.
        """
        super().addSubTest(test, subtest, err)
        if err is None:
            return
        detail = self._exc_info_to_string(err, test)
        outcome = ("fail" if issubclass(err[0], test.failureException)
                   else "error")
        existing = self.records.get(test.id())
        if existing and existing["outcome"] in ("fail", "error"):
            existing["detail"] += "\n" + str(subtest) + "\n" + detail
            return
        self._finish(test, outcome, str(subtest) + "\n" + detail)


def _run_one(test_id, top_level):
    sys.path.insert(0, top_level)
    loader = unittest.TestLoader()
    collector = _Collector()
    try:
        suite = loader.loadTestsFromName(test_id)
    except Exception:                                   # noqa: BLE001
        return {"id": test_id, "outcome": "error", "duration_s": 0.0,
                "detail": "could not load test:\n" + traceback.format_exc()}
    started = time.time()
    suite.run(collector)
    record = collector.records.get(test_id)
    if record is None:
        # unittest answers an unimportable module with a synthetic
        # `_FailedTest` under a different id. Report why it could not be
        # loaded rather than the useless fact that nothing came back.
        detail = "test produced no result record"
        for other in collector.records.values():
            if other["outcome"] in ("fail", "error"):
                detail = other["detail"] or detail
                break
        return {"id": test_id, "outcome": "error",
                "duration_s": round(time.time() - started, 3),
                "detail": detail}
    return record


def worker_main(jobs, results, top_level, output_root, worker_id):
    """Child entry point. Pulls test IDs until the queue is exhausted."""
    os.environ[ENV_WORKER] = str(worker_id)
    own_root = os.path.join(output_root, f"worker{worker_id}")
    os.makedirs(own_root, exist_ok=True)
    os.environ[ENV_OUTPUT_ROOT] = own_root
    tempfile.tempdir = os.path.join(own_root, "tmp")
    os.makedirs(tempfile.tempdir, exist_ok=True)
    while True:
        try:
            test_id = jobs.get_nowait()
        except queue.Empty:
            break
        try:
            record = _run_one(test_id, top_level)
        except Exception:                               # noqa: BLE001
            record = {"id": test_id, "outcome": "error", "duration_s": 0.0,
                      "detail": "worker exception:\n" + traceback.format_exc()}
        record["worker"] = worker_id
        results.put(record)
    results.put({"id": None, "outcome": "worker_done", "worker": worker_id})


# ---------------------------------------------------------------------------
# parent
# ---------------------------------------------------------------------------

def resolve_jobs(spec, test_count):
    if spec in (None, "auto"):
        cpu = os.cpu_count() or 1
        return max(1, min(cpu - 1 if cpu > 2 else cpu, test_count, 8))
    jobs = int(spec)
    if jobs < 1:
        raise ValueError("--jobs must be >= 1")
    return max(1, min(jobs, test_count))


def run(tests_dir, top_level, jobs="auto", timeout_s=1800, fail_fast=False,
        output_root=None, stream=sys.stdout):
    """Run the suite and return (exit_code, summary dict)."""
    test_ids = discover(tests_dir, top_level)
    if not test_ids:
        print("no tests discovered", file=stream)
        return 1, {"tests": [], "workers": 0}

    worker_count = resolve_jobs(jobs, len(test_ids))
    owned_root = output_root is None
    root = output_root or tempfile.mkdtemp(prefix="pcbqa_selftest_")
    started = time.time()
    print(f"pcbqa self-test: {len(test_ids)} tests, {worker_count} worker "
          f"process{'es' if worker_count != 1 else ''}", file=stream)

    try:
        if worker_count == 1:
            records = _run_serial(test_ids, top_level, root, stream)
        else:
            records = _run_parallel(test_ids, top_level, root, worker_count,
                                    timeout_s, fail_fast, stream)
    finally:
        if owned_root:
            shutil.rmtree(root, ignore_errors=True)

    elapsed = time.time() - started
    return _summarise(test_ids, records, worker_count, elapsed, stream)


def _run_serial(test_ids, top_level, root, stream):
    """`--jobs 1` in this process, with the environment put back afterwards.

    A serial run happens inside whatever process called it - which may itself
    be a worker with an output root of its own. Clobbering that permanently
    would silently un-isolate every later test in the same worker, so the
    previous values are restored.
    """
    own = os.path.join(root, "worker0")
    os.makedirs(own, exist_ok=True)
    saved = {k: os.environ.get(k) for k in (ENV_OUTPUT_ROOT, ENV_WORKER)}
    saved_tempdir = tempfile.tempdir
    os.environ[ENV_OUTPUT_ROOT] = own
    os.environ[ENV_WORKER] = "0"
    tempfile.tempdir = os.path.join(own, "tmp")
    os.makedirs(tempfile.tempdir, exist_ok=True)
    records = {}
    try:
        for test_id in test_ids:
            record = _run_one(test_id, top_level)
            record["worker"] = 0
            records[test_id] = record
            mark = {"ok": ".", "skip": "s"}.get(record["outcome"], "F")
            print(mark, end="", flush=True, file=stream)
        print("", file=stream)
    finally:
        tempfile.tempdir = saved_tempdir
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return records


def _run_parallel(test_ids, top_level, root, worker_count, timeout_s,
                  fail_fast, stream):
    ctx = mp.get_context("spawn")            # Windows-safe; never fork
    jobs = ctx.Queue()
    results = ctx.Queue()
    for test_id in test_ids:
        jobs.put(test_id)

    procs = []
    for index in range(worker_count):
        proc = ctx.Process(target=worker_main,
                           args=(jobs, results, top_level, root, index),
                           daemon=False)
        proc.start()
        procs.append(proc)

    records = {}
    finished = set()
    deadline = time.time() + timeout_s
    try:
        while len(finished) < worker_count:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                record = results.get(timeout=min(5.0, max(0.1, remaining)))
            except queue.Empty:
                if all(not p.is_alive() for p in procs) and results.empty():
                    break
                continue
            if record.get("outcome") == "worker_done":
                finished.add(record["worker"])
                continue
            records[record["id"]] = record
            mark = {"ok": ".", "skip": "s"}.get(record["outcome"], "F")
            print(mark, end="", flush=True, file=stream)
            if fail_fast and record["outcome"] in ("fail", "error"):
                break
        print("", file=stream)
    finally:
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
        for proc in procs:
            proc.join(timeout=30)

    for index, proc in enumerate(procs):
        if index in finished:
            continue
        records.setdefault(f"<worker{index}>", {
            "id": f"<worker{index}>", "outcome": "error", "duration_s": 0.0,
            "worker": index,
            "detail": (f"worker exited without finishing its queue "
                       f"(exitcode={proc.exitcode})"),
        })
    return records


def _summarise(test_ids, records, worker_count, elapsed, stream):
    rows = []
    missing = []
    for test_id in test_ids:                 # discovery order, deterministic
        record = records.get(test_id)
        if record is None:
            missing.append(test_id)
            rows.append({"id": test_id, "outcome": "missing", "duration_s": 0.0,
                         "detail": "no result was returned for this test",
                         "worker": None})
        else:
            rows.append(record)
    for key, record in sorted(records.items()):
        if isinstance(key, str) and key.startswith("<worker"):
            rows.append(record)

    counts = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1

    per_worker = {}
    for row in rows:
        worker = row.get("worker")
        if worker is None:
            continue
        entry = per_worker.setdefault(worker, {"tests": 0, "seconds": 0.0})
        entry["tests"] += 1
        entry["seconds"] = round(entry["seconds"] + row.get("duration_s", 0.0), 3)

    bad = [r for r in rows if r["outcome"] not in ("ok", "skip", "expected_failure")]
    for row in bad:
        header = f"{row['outcome'].upper()}: {row['id']}"
        rule = "=" * len(header)
        print(f"\n{rule}\n{header}\n{rule}", file=stream)
        print(row.get("detail", "").rstrip(), file=stream)

    print(f"\nRan {len(test_ids)} tests across {worker_count} worker "
          f"process{'es' if worker_count != 1 else ''} in {elapsed:.1f}s",
          file=stream)
    for worker in sorted(per_worker):
        stats = per_worker[worker]
        print(f"  worker {worker}: {stats['tests']} tests, "
              f"{stats['seconds']:.1f}s of test time", file=stream)
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
          file=stream)
    ok = not bad
    print("OK" if ok else "FAILED", file=stream)

    summary = {
        "tests": rows, "workers": worker_count, "elapsed_s": round(elapsed, 3),
        "counts": counts, "missing": missing, "ok": ok,
    }
    return (0 if ok else 1), summary
