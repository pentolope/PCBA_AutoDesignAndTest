"""Source closure: the identity of every input a check result depends on."""

from __future__ import annotations

import copy
import fnmatch
import glob
import hashlib
import json
import os

from . import canonical
from .core import design_inputs


class ClosureError(Exception):
    """A closure cannot be built, so nothing derived from it can be trusted."""


def matches(rel, patterns):
    rel = rel.replace("\\", "/")
    name = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p)
               for p in patterns)


def open_design_locks(manifest, patterns):
    """Lock files sitting beside the design, meaning KiCad may have it open.

    Scoped to the directories the design actually occupies rather than to the
    repository that holds it: `*-lock` is a KiCad glob, and a repository-wide
    scan also matches the `.cargo-lock` a Rust build leaves in a vendored
    router - a file no KiCad ever touched.
    """
    root = manifest.resolve(".")
    directories = {os.path.dirname(os.path.join(root, rel))
                   for rel in design_inputs(manifest)}
    hits = []
    for directory in sorted(directories):
        for name in sorted(os.listdir(directory) if os.path.isdir(directory)
                           else []):
            full = os.path.join(directory, name)
            if os.path.isfile(full) and matches(name, patterns):
                hits.append(os.path.relpath(full, root).replace("\\", "/"))
    return sorted(hits)


def closure_digest(entries):
    """One digest over a {path: sha256} map, order-independent."""
    joined = "\n".join(f"{k}:{v}" for k, v in sorted(entries.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def implementation_identity():
    """Which toolkit produced this result, from Git rather than from hashes.

    The toolkit is consumed as a pinned submodule, and a release requires a
    clean tree with submodules exactly at their gitlinks, so at the moment the
    identity has to be right the commit determines the code completely.
    Hashing each imported module said the same thing at greater length, and
    said it about a list a board maintained by hand.

    A dirty tree is recorded as dirty rather than pinned: it is a development
    state, and `release-check` refuses it. Git being unable to answer is a
    refusal, not a default - an unidentifiable implementation is not one a
    result may be bound to.
    """
    from .core import toolkit_identity
    record = toolkit_identity()
    commit = record.get("commit")
    if not commit:
        raise ClosureError(
            "the toolkit's own commit cannot be read ({}), so no result can "
            "be bound to the implementation that produced "
            "it".format(record.get("detail")))
    if record.get("working_tree_dirty") is None:
        raise ClosureError(
            "git cannot say whether the toolkit tree is dirty ({}), and a "
            "tree whose cleanliness is unknown must not be recorded as "
            "clean".format(record.get("detail")))
    return {"<toolkit>": "{}+{}".format(
        commit, "dirty" if record["working_tree_dirty"] else "clean")}


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
    `sources` by name, every design input the toolkit itself stages, the
    toolkit that judged them, and the manifest's configuration identity.
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

    # Everything `stage_design` copies for ERC and DRC, unconditionally:
    # symbol libraries, lib tables and sibling settings change what a check
    # sees, so a closure without them keeps reporting fresh after an
    # ERC-visible edit. Same precedent as the declared sources above. An
    # exclusion pattern that matches one is refused rather than silently
    # overridden - a declaration that reads as evaluated but is not would
    # be the manifest lying to its author.
    for rel in design_inputs(manifest):
        rel = rel.replace("\\", "/")
        if matches(rel, excluded):
            raise ClosureError(
                "reports.source_closure_exclude matches design input {}; a "
                "design input can never leave the closure, so remove the "
                "pattern or the declaration is a no-op wearing an effect's "
                "name".format(rel))
        path = os.path.join(root, rel)
        if os.path.isfile(path):
            entries[rel] = canonical.digest(path, policy.classify(rel))

    entries.update(implementation_identity())
    entries["<configuration>"] = configuration_identity(manifest)
    return entries


#: Where the line-ending policy a canonical digest depends on is declared.
#: The first name is the general one; the second is what it was called when
#: only fixtures used it, and a pinned consumer still names it that way.
ATTRIBUTES_KEYS = ("closure.attributes_file", "fixture.attributes_file")


def attributes_file(manifest):
    for key in ATTRIBUTES_KEYS:
        if manifest.has(key):
            return manifest.resolve(manifest.get(key))
    raise ClosureError(
        "no line-ending policy is declared ({}), and a canonical digest "
        "cannot be computed without one".format(" or ".join(ATTRIBUTES_KEYS)))


def policy_for(manifest):
    return canonical.AttributePolicy.load(attributes_file(manifest))


def current(manifest):
    """(entries, digest) for a manifest, loading its own line-ending policy."""
    entries = source_closure(manifest, policy_for(manifest))
    return entries, closure_digest(entries)
