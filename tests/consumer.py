"""An optional consumer board, for integration tests that need a live design.

Most of the suite runs against fixtures this repository ships. A few tests need
something a fixture cannot be: a board with a *complete, installed* release -
committed fabrication outputs, cross-checkable reports and frozen
part-orientation evidence - because what they exercise is the toolkit's
behaviour on a real consumer state.

Rather than keep a consumer's board here, or drop the coverage, those tests
take the board from outside:

    set PCBQA_CONSUMER_MANIFEST=<path to a board manifest>
    python run.py selftest

With nothing set they skip, with a reason that says what to set. The toolkit
therefore stays reusable by any board without modification, and the board that
happens to be driving it stays in its own repository.

`require()` is the whole interface. Call it in `setUpClass` and let it raise
`unittest.SkipTest` when no consumer is registered.
"""

from __future__ import annotations

import json
import os
import unittest

ENV = "PCBQA_CONSUMER_MANIFEST"

_WHY = (
    "no consumer board registered: set {} to a board manifest to run the "
    "integration tests that need an installed release".format(ENV)
)


def manifest_path():
    """The registered consumer manifest, or None."""
    path = os.environ.get(ENV)
    if not path:
        return None
    path = os.path.abspath(path)
    return path if os.path.isfile(path) else None


def needed(cls):
    """Class decorator: skip the whole class when no consumer is registered.

    Use this rather than letting `require()` raise out of `setUpClass`. A
    `SkipTest` from `setUpClass` is recorded by unittest against a synthetic
    holder rather than against each test, so a runner that expects one result
    per test id - like this one - reports "test produced no result record"
    instead of a skip. A class decorator skips each test individually and
    records it properly.
    """
    return unittest.skipUnless(manifest_path(), _WHY)(cls)


def require():
    """The consumer manifest path, or skip the test.

    A registered-but-missing manifest is an error, not a skip: someone meant
    to run these and the path is wrong, and silently skipping would hide it.
    """
    raw = os.environ.get(ENV)
    if not raw:
        raise unittest.SkipTest(_WHY)
    path = os.path.abspath(raw)
    if not os.path.isfile(path):
        raise AssertionError(
            "{} is set to {!r}, which is not a file".format(ENV, raw))
    return path


def project_root():
    """The consumer project directory, resolved from its manifest."""
    path = require()
    with open(path, encoding="utf-8") as fh:
        declared = json.load(fh).get("project_root", ".")
    return os.path.abspath(os.path.join(os.path.dirname(path), declared))


def document():
    with open(require(), encoding="utf-8") as fh:
        return json.load(fh)
