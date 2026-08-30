"""KiCadRoutingTools discovery and provenance.

A candidate's routed copper is a function of the router that drew
it, so WHICH KiCadRoutingTools executed - and under WHICH Python -
is evidence, not ambience. This module resolves one KRT source by
an explicit, deterministic order and produces a provenance record
strong enough to reproduce (or honestly refuse to trust) a routing
run:

    explicit override (parameter, else the PCB_KRT_PATH
    environment variable)
        -> configured development checkout (the consumer's own
           toolchain configuration)
        -> the router vendored into this toolkit as the
           ``tooling/KiCadRoutingTools`` submodule
        -> the active KiCad plugin installation (a scan of the
           caller-named plugin directories; MORE than one valid
           installation refuses as ambiguous)
        -> refusal.

The only path searched that no caller named is that submodule, which
this module derives from its own ``__file__``; nothing else on disk
is ever searched, and a path under a ``disabled_pcm_plugins``
directory refuses outright - a disabled installation exists to be
recoverable, never to be executed.

Be plain about what vendoring costs. With the submodule present the
plugin scan is unreachable: the pinned router wins, so an ambient
PCM installation is silently NOT used, and the ambiguity and
not-found refusals below it cannot fire. That is deliberate - a
pinned, reviewable revision should beat whatever happens to be
installed - and it is visible rather than silent, because every
resolution reports the ``origin`` that answered and consumers log
it. Pass ``vendored=None`` to resolve as if no submodule existed;
that is how the scan and its refusals stay reachable and tested.

A dirty checkout is honest evidence of an experiment, not of a
reproducible build: ``provenance`` records the dirty state, and
callers that need clean evidence pass ``require_clean=True``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess


class KRTError(Exception):
    """No usable KiCadRoutingTools source resolves as declared."""


ENV_OVERRIDE = "PCB_KRT_PATH"

#: Files that make a directory a plausible KRT source root.
_MARKERS = ("VERSION", os.path.join("py_router", "route.py"))

#: The router vendored into this toolkit as a submodule, pinned to a
#: commit that exists on its remote. This is the reproducible source:
#: a recursive clone has it, at one known revision, with no sibling
#: checkout to find and no absolute path to agree on.
VENDORED = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tooling", "KiCadRoutingTools")


def vendored_path():
    """The vendored router root, or None when submodules are absent.

    Returns a path only when it actually looks like a KRT source
    root, so an uninitialised submodule - an empty directory, which
    is exactly what a non-recursive clone leaves - reads as absent
    rather than as a broken installation.
    """
    return VENDORED if _is_krt_root(VENDORED) else None


def _is_krt_root(path):
    return all(os.path.isfile(os.path.join(path, marker))
               for marker in _MARKERS)


def _refuse_disabled(path, origin):
    real = os.path.realpath(path)
    if "disabled_pcm_plugins" in real.replace("\\", "/").split("/"):
        raise KRTError(
            "{} resolves to {}, which lives under "
            "disabled_pcm_plugins; a disabled installation is "
            "kept to be recoverable, never to be executed".format(
                origin, real))
    return real


#: Sentinel: use the real vendored submodule. A caller passing
#: ``vendored=None`` is stating that no vendored router exists for
#: this resolution, which is how the plugin-scan and refusal paths
#: stay reachable and testable now that a submodule ships with the
#: toolkit and would otherwise always win first.
_VENDORED_DEFAULT = object()


def resolve(override=None, configured=None, plugin_dirs=None,
            environ=None, vendored=_VENDORED_DEFAULT):
    """One KRT source root, by the declared order, or a refusal.

    ``override`` beats the PCB_KRT_PATH environment variable beats
    ``configured`` beats the vendored submodule beats a scan of
    ``plugin_dirs``. An explicit source (override/env/configured)
    that does not validate is an ERROR, never a silent fall-through
    - a caller who named a path meant it. The vendored submodule is
    the default rather than an override because it is the only
    source a fresh recursive clone is guaranteed to have, and the
    only one pinned to a reviewable revision. The plugin-directory
    scan accepts exactly one valid installation; two is ambiguity
    and refuses.
    """
    env = os.environ if environ is None else environ
    for origin, value in (("override", override),
                          ("environment " + ENV_OVERRIDE,
                           env.get(ENV_OVERRIDE)),
                          ("configured checkout", configured)):
        if not value:
            continue
        real = _refuse_disabled(value, origin)
        if not _is_krt_root(real):
            raise KRTError(
                "{} names {}, which is not a KiCadRoutingTools "
                "source root (missing {})".format(
                    origin, real,
                    [m for m in _MARKERS if not os.path.isfile(
                        os.path.join(real, m))]))
        return {"path": real, "origin": origin}
    found = []
    for plugin_dir in (plugin_dirs or []):
        if not os.path.isdir(plugin_dir):
            continue
        for name in sorted(os.listdir(plugin_dir)):
            candidate = os.path.join(plugin_dir, name)
            if not os.path.isdir(candidate):
                continue
            real = os.path.realpath(candidate)
            if "disabled_pcm_plugins" in \
                    real.replace("\\", "/").split("/"):
                continue
            if _is_krt_root(real):
                found.append(real)
    if vendored is _VENDORED_DEFAULT:
        vendored = vendored_path()
    elif vendored and not _is_krt_root(os.path.realpath(vendored)):
        # Named explicitly and wrong is an error, like every other
        # named source; only the default may be absent silently.
        raise KRTError(
            "vendored submodule names {}, which is not a "
            "KiCadRoutingTools source root".format(vendored))
    if vendored:
        return {"path": _refuse_disabled(vendored, "vendored submodule"),
                "origin": "vendored submodule"}
    unique = sorted(set(found))
    if len(unique) == 1:
        return {"path": unique[0],
                "origin": "active plugin installation"}
    if len(unique) > 1:
        raise KRTError(
            "{} KiCadRoutingTools installations found in the "
            "plugin directories ({}); ambiguous resolution is "
            "refused - name one explicitly via override, {} or "
            "the configured checkout".format(
                len(unique), unique, ENV_OVERRIDE))
    raise KRTError(
        "no KiCadRoutingTools source found: no override, no {} "
        "environment value, no configured checkout, nothing vendored "
        "at {}, and no active plugin installation in {}.\n"
        "The vendored router is this toolkit's own submodule, so the "
        "usual cause is a clone that did not take submodules. Run:  "
        "git submodule update --init --recursive".format(
            ENV_OVERRIDE, VENDORED, list(plugin_dirs or [])))


def _git(path, *arguments):
    try:
        completed = subprocess.run(
            ["git", "-C", path] + list(arguments),
            capture_output=True, text=True, timeout=30)
    except Exception as error:                        # noqa: BLE001
        return None, "git unavailable: {}".format(error)
    if completed.returncode != 0:
        return None, completed.stderr.strip()[:200]
    return completed.stdout.strip(), None


def _probe(python_executable, code, cwd=None):
    try:
        completed = subprocess.run(
            [python_executable, "-c", code],
            capture_output=True, text=True, timeout=120, cwd=cwd)
    except Exception as error:                        # noqa: BLE001
        return None, "probe failed: {}".format(error)
    if completed.returncode != 0:
        return None, (completed.stderr.strip()
                      or completed.stdout.strip())[:300]
    return completed.stdout.strip(), None


def verify_environment(python_executable):
    """The interpreter a routing run will use must BE the KiCad
    environment: pcbnew must import there. A wrong Python refuses
    with the import error named - never a quiet fallback."""
    output, error = _probe(
        python_executable,
        "import pcbnew, sys; print(sys.version.split()[0])")
    if output is None:
        raise KRTError(
            "python {!r} is not a KiCad environment (pcbnew does "
            "not import): {}".format(python_executable, error))
    return {"executable": python_executable, "version": output}


def provenance(krt_path, python_executable, require_clean=False,
               upstream_remote="upstream"):
    """The routing implementation identity, recorded not assumed.

    Everything a future reader needs to reproduce or refuse: the
    source path, VERSION, git commit and dirty state (explicitly
    'unrecorded' for a copy without git metadata - such a copy can
    run experiments but never claims a commit), the upstream base
    when the remote exists, the native grid_router's file hash and
    self-reported version probed under the SAME interpreter the
    run will use, and that interpreter's identity.
    """
    krt_path = os.path.realpath(krt_path)
    with open(os.path.join(krt_path, "VERSION"),
              encoding="utf-8") as handle:
        version = handle.read().strip()
    sha, sha_error = _git(krt_path, "rev-parse", "HEAD")
    if sha is None:
        git_record = {"sha": None, "dirty": None,
                      "upstream_base": None,
                      "detail": "unrecorded: {}".format(sha_error)}
    else:
        status, _err = _git(krt_path, "status", "--porcelain")
        dirty = bool(status) if status is not None else None
        base, _err = _git(krt_path, "merge-base", "HEAD",
                          upstream_remote + "/main")
        git_record = {"sha": sha, "dirty": dirty,
                      "upstream_base": base}
        if dirty:
            # Two different uncommitted edits at the same HEAD are
            # two different routers; a boolean cannot tell them
            # apart. Digest the actual divergence (tracked diff
            # plus the untracked/status inventory) so the identity
            # moves with the edit, not just with its existence.
            diff_text, _diff_err = _git(krt_path, "diff", "HEAD")
            git_record["dirty_divergence_sha256"] = \
                hashlib.sha256(
                    ((diff_text or "") + "\n" + status)
                    .encode("utf-8", "replace")).hexdigest()
        if require_clean and dirty is not False:
            # dirty True refuses; dirty None (git status itself
            # failed) also refuses - an unprovable clean is not a
            # clean, and silence never passes.
            raise KRTError(
                "KRT checkout {} is {}; a dirty or unprovable "
                "checkout may run experiments when explicitly "
                "allowed, but it never masquerades as clean "
                "reproducible evidence".format(
                    krt_path,
                    "dirty" if dirty else
                    "of unprovable cleanliness (git status "
                    "failed)"))
    if require_clean and sha is None:
        raise KRTError(
            "KRT at {} has no git metadata; without a commit it "
            "cannot provide clean reproducible evidence".format(
                krt_path))
    environment = verify_environment(python_executable)
    router_module = None
    # Search order matters: a real installation ships the native
    # binary, which wins; a pure-python grid_router.py is accepted
    # last so test fixtures can exercise this contract without
    # fabricating a platform binary.
    for name in ("grid_router.so", "grid_router.py"):
        candidate = os.path.join(krt_path, "rust_router", name)
        if os.path.isfile(candidate):
            router_module = candidate
            break
    if router_module is None:
        raise KRTError(
            "no native grid_router binary under {}; run the "
            "repository's build_router.py first".format(
                os.path.join(krt_path, "rust_router")))
    router_kind = ("native-binary"
                   if router_module.endswith(".so")
                   else "python-source-stand-in")
    with open(router_module, "rb") as handle:
        router_hash = hashlib.sha256(handle.read()).hexdigest()
    router_version, router_error = _probe(
        python_executable,
        "import sys; sys.path.insert(0, {!r}); import "
        "grid_router; print(getattr(grid_router, '__version__', "
        "'unversioned'))".format(os.path.dirname(router_module)))
    if router_version is None:
        raise KRTError(
            "native grid_router at {} does not import under "
            "{}: {}".format(router_module, python_executable,
                            router_error))
    # The kicad-cli beside the interpreter that will run the router, then
    # PATH. On a distribution KiCad both answer /usr/bin/kicad-cli; the first
    # probe still matters for an interpreter installed outside the system
    # prefix, where the sibling binary is the one that actually matches.
    kicad_cli = os.path.join(
        os.path.dirname(python_executable), "kicad-cli")
    if not os.path.isfile(kicad_cli):
        kicad_cli = shutil.which("kicad-cli") or kicad_cli
    # Which kicad-cli answered, and - when none did - why. A bare None here
    # would make "no kicad-cli on this machine" and "found one that could not
    # state its version" the same record, which is the silence this module
    # exists to refuse.
    if not os.path.isfile(kicad_cli):
        kicad_cli, kicad_version = None, "UNAVAILABLE: no kicad-cli beside " \
            "{} or on PATH".format(python_executable)
    else:
        try:
            completed = subprocess.run(
                [kicad_cli, "version"], capture_output=True,
                text=True, timeout=30)
            kicad_version = completed.stdout.strip() if \
                completed.returncode == 0 else \
                "UNREPORTED: {} exited {}".format(
                    kicad_cli, completed.returncode)
        except Exception as error:                    # noqa: BLE001
            kicad_version = "UNAVAILABLE: {}: {}".format(
                type(error).__name__, error)
    return {
        "kind": "krt-provenance",
        "source_path": krt_path,
        "version": version,
        "git": git_record,
        "grid_router": {"path": router_module,
                        "kind": router_kind,
                        "sha256": router_hash,
                        "version": router_version},
        "python": environment,
        "kicad_cli": kicad_cli,
        "kicad_version": kicad_version,
        "meaning": "the routing implementation identity for this "
                   "run; a different sha, dirty state or native "
                   "hash is a different router, and downstream "
                   "routing-derived artifacts must go stale with "
                   "it",
    }


def identity_digest(record):
    """A stable digest over the load-bearing identity fields, for
    freshness closures: sha, dirty state INCLUDING what the dirt
    actually is, VERSION, the router module's kind and hash, and
    the interpreter. Two different uncommitted edits at one HEAD
    are two different identities."""
    from .freshness import canonical_json_digest
    return canonical_json_digest({
        "sha": record["git"]["sha"],
        "dirty": record["git"]["dirty"],
        "dirty_divergence_sha256":
            record["git"].get("dirty_divergence_sha256"),
        "version": record["version"],
        "grid_router_kind": record["grid_router"].get("kind"),
        "grid_router_sha256": record["grid_router"]["sha256"],
        "python": record["python"],
    })
