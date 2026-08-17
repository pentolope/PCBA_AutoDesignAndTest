"""Checkout-independent file digests.

A frozen-fixture digest is only meaningful if it does not change when Git
checks the file out with different line endings. Design sources are therefore
hashed over their canonical (LF) bytes; production artifacts, whose exact bytes
are what a fabricator receives and what the release manifest records, are
hashed raw and never normalised.

The classification comes from the repository's .gitattributes, so the digest
policy and the checkout policy cannot drift apart.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os

TEXT = "text"
BINARY = "binary"


class AttributePolicy:
    """The subset of .gitattributes this needs: is a path text or binary."""

    def __init__(self, rules, source):
        self.rules = rules            # [(pattern, kind)] in file order
        self.source = source

    @classmethod
    def load(cls, path):
        rules = []
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                pattern, attrs = parts[0], parts[1:]
                kind = None
                for attr in attrs:
                    if attr == "binary" or attr == "-text":
                        kind = BINARY
                    elif attr.startswith("text"):
                        kind = TEXT
                if kind:
                    rules.append((pattern, kind))
        return cls(rules, os.path.abspath(path))

    def classify(self, relpath):
        """Last matching rule wins, as Git does."""
        rel = relpath.replace("\\", "/")
        kind = TEXT
        for pattern, this_kind in self.rules:
            if _matches(pattern, rel):
                kind = this_kind
        return kind


def _matches(pattern, rel):
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if "/" in pattern:
        if pattern.endswith("/**"):
            return rel.startswith(pattern[:-3] + "/") or rel == pattern[:-3]
        return fnmatch.fnmatch(rel, pattern)
    # no slash: match against the basename, like Git
    return fnmatch.fnmatch(os.path.basename(rel), pattern) or fnmatch.fnmatch(rel, pattern)


def canonical_bytes(path, kind):
    raw = open(path, "rb").read()
    if kind == BINARY:
        return raw
    # Canonical text form: LF only. A lone CR is left alone; Git does the same
    # for CRLF normalisation.
    return raw.replace(b"\r\n", b"\n")


def digest(path, kind):
    return hashlib.sha256(canonical_bytes(path, kind)).hexdigest()


def digest_tree(root, policy, skip_dirs=frozenset()):
    """{relpath: {"sha256":..., "kind":...}} for every file under root."""
    out = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for name in files:
            path = os.path.join(base, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            kind = policy.classify(rel)
            out[rel] = {"sha256": digest(path, kind), "kind": kind}
    return dict(sorted(out.items()))
