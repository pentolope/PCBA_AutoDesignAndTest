"""Source closure: the identity of every input a check result depends on."""

from __future__ import annotations

import copy
import fnmatch
import glob
import hashlib
import json
import os

from . import canonical
from .core import NEVER_COPY, sha256_file


class ClosureError(Exception):
    """A closure cannot be built, so nothing derived from it can be trusted."""


def matches(rel, patterns):
    rel = rel.replace("\\", "/")
    name = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
               for p in patterns)


def find(root, patterns):
    """Files under `root` matching `patterns`, skipping non-design trees.

    NEVER_COPY is pruned for the same reason `copy_project` refuses it: a lock
    glob written for KiCad (`*-lock`) otherwise matches Rust's `.cargo-lock`
    in the vendored router's build tree.
    """
    hits = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in NEVER_COPY]
        for name in files:
            rel = os.path.relpath(os.path.join(dirpath, name),
                                  root).replace("\\", "/")
            if matches(rel, patterns):
                hits.append(rel)
    return sorted(hits)


def closure_digest(entries):
    """One digest over a {path: sha256} map, order-independent."""
    joined = "\n".join(f"{k}:{v}" for k, v in sorted(entries.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def executed_implementation(names):
    """Digests of the code that is running, taken from the loaded modules.

    Resolved through the import system and hashed at ``__file__``: hashing a
    path proves nothing about what was imported.
    """
    import importlib
    entries = {}
    for name in names:
        module = importlib.import_module(name)
        path = getattr(module, "__file__", None)
        if not path or not os.path.isfile(path):
            raise ClosureError(
                "{} declares no importable file, so the implementation that "
                "ran cannot be recorded".format(name))
        entries["<executed>" + name] = sha256_file(path)
    return entries


#: The one leaf that is location rather than content. Every other path in a
#: manifest selects something; this one only says where the tree is, and every
#: file the closure records is already keyed relative to it. Owned here, not
#: declarable by a board: a manifest that could name its own exclusions could
#: change what a release does without changing what the closure says about it.
LOCATION_ONLY = ("project_root",)


def configuration_identity(manifest):
    """The manifest's content, hashed independently of how it is formatted."""
    data = copy.deepcopy(manifest.data)
    for key in LOCATION_ONLY:
        data.pop(key, None)
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def source_closure(manifest, policy):
    """Canonical digests of every input a check result depends on.

    Glob-matched project files minus an explicit exclusion list, the declared
    `sources` by name, the modules that derive rather than read (by import
    name), and the manifest's configuration identity.
    """
    root = manifest.resolve(".")
    excluded = manifest.get("reports.source_closure_exclude", [])
    entries = {}
    for pattern in manifest.get("reports.source_closure"):
        for path in sorted(glob.glob(os.path.join(root, pattern),
                                     recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            if matches(rel, excluded):
                continue
            entries[rel] = canonical.digest(path, policy.classify(rel))

    for role, declared in sorted((manifest.get("sources") or {}).items()):
        path = manifest.resolve(declared)
        if not os.path.isfile(path):
            raise ClosureError(
                "sources.{} names {} which does not exist, so the closure "
                "cannot cover the design it selects".format(role, declared))
        rel = os.path.relpath(path, root).replace("\\", "/")
        entries[rel] = canonical.digest(path, policy.classify(rel))

    entries.update(executed_implementation(
        manifest.get("reports.implementation_closure", [])))
    entries["<configuration>"] = configuration_identity(manifest)
    return entries


def policy_for(manifest):
    return canonical.AttributePolicy.load(
        manifest.resolve(manifest.get("fixture.attributes_file")))


def current(manifest):
    """(entries, digest) for a manifest, loading its own line-ending policy."""
    entries = source_closure(manifest, policy_for(manifest))
    return entries, closure_digest(entries)
