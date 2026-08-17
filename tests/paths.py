"""Where the toolkit's own test assets live.

One module so a fixture can be added, renamed or relocated without editing a
dozen tests, and so no test has to spell out a board-specific directory name
more than once. Production code never imports this: `pcbqa/` knows nothing
about fixtures, which is what `GenericSourceHygiene` enforces.
"""

from __future__ import annotations

import os

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)

PACKAGE = os.path.join(ROOT, "pcbqa")
MANIFESTS = os.path.join(TESTS, "manifests")
FIXTURES = os.path.join(TESTS, "fixtures")
ATTRIBUTES = os.path.join(ROOT, ".gitattributes")

# --- generic fixtures ------------------------------------------------------
CLEAN_MANIFEST = os.path.join(MANIFESTS, "clean.json")
CLEAN_PROJECT = os.path.join(FIXTURES, "clean", "project")

PORTABILITY_MANIFEST = os.path.join(MANIFESTS, "portability.json")
PORTABILITY_FIXTURE = os.path.join(FIXTURES, "portability")

# --- negative integration fixture ------------------------------------------
# An intentionally defective, curated real project. The suite passes by proving
# the validator rejects it. See its README for what it is and is not.
NEGATIVE = os.path.join(FIXTURES, "negative")
REVA_ROOT = os.path.join(NEGATIVE, "microphone_array_reva")
REVA_MANIFEST = os.path.join(REVA_ROOT, "manifest.json")
REVA_EXPECTED = os.path.join(REVA_ROOT, "expected.json")
REVA_PROJECT = os.path.join(REVA_ROOT, "project")
