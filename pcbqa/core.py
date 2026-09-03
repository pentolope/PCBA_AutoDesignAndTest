"""Board-agnostic core: results, constraint manifest, provenance, gate registry.

Nothing in this package may name a specific board, net, reference designator,
component, coordinate or threshold. Every value a gate compares against is
fetched from the project's constraint manifest through `Manifest.get`, which
records where the value came from so the constraint-parity gate can prove that
no checker invented its own limit.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

from .constraints import Constraint, ConstraintError, GeometryProfile

SCHEMA_VERSION = 2
_MISSING = object()


# ---------------------------------------------------------------------------
# results
# ---------------------------------------------------------------------------

class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"          # fail-closed: could not evaluate
    # A gate the board declares advisory: it ran, it found something, and the
    # board has decided that finding does not stop a release. The measurement
    # and the reason are both kept in the report - an advisory gate is quieter
    # than a blocking one, never silent - and only a FAIL can be demoted.
    # ERROR never is: a gate that could not be evaluated is still fail-closed.
    ADVISORY = "ADVISORY"

    BLOCKING = (FAIL, ERROR)


class GateResult:
    """One gate's outcome, machine readable."""

    def __init__(self, gate_id, title):
        self.gate_id = gate_id
        self.title = title
        self.status = None
        self.reason = ""
        self.findings = []       # list of dict
        self.measurements = {}   # name -> value
        self.limits = {}         # name -> {"value":..., "source":...}
        self.evidence = []       # list of file paths / hashes

    # -- outcome helpers ---------------------------------------------------
    def passed(self, reason="", **measurements):
        self.status = Status.PASS
        self.reason = reason
        self.measurements.update(measurements)
        return self

    def failed(self, reason, **measurements):
        self.status = Status.FAIL
        self.reason = reason
        self.measurements.update(measurements)
        return self

    def not_applicable(self, reason):
        self.status = Status.NOT_APPLICABLE
        self.reason = reason
        return self

    def advisory(self, why):
        """Demote a FAIL the board has declared advisory, keeping the finding."""
        if self.status != Status.FAIL:
            return self
        self.status = Status.ADVISORY
        self.measurements["advisory_blocking_reason"] = self.reason
        self.reason = "{} [advisory: {}]".format(self.reason, why)
        return self

    def errored(self, reason):
        self.status = Status.ERROR
        self.reason = reason
        return self

    # -- detail helpers ----------------------------------------------------
    def finding(self, **fields):
        self.findings.append(fields)
        return self

    def limit(self, constraint):
        """Record a typed constraint this gate applied."""
        if not isinstance(constraint, Constraint):
            raise TypeError("gates must apply typed Constraint objects, not raw "
                            f"values (got {type(constraint).__name__})")
        self.limits[constraint.id] = constraint.to_dict()
        return constraint

    def evidence_file(self, path, digest=None, name=None):
        """Record a file this gate examined.

        `path` is absolute and therefore host-shaped: it carries the separator
        and the drive letter of whichever machine ran the gate, which makes it
        useless for finding the same file again somewhere else. `name` is the
        stable identity - a package-relative, forward-slash name - and is what
        a later reader should match on. It defaults to the basename, split on
        both separators so a report written on one platform can be read on the
        other.
        """
        self.evidence.append({
            "path": path,
            "name": name or re.split(r"[\\/]", str(path))[-1],
            "sha256": digest if digest else (sha256_file(path) if os.path.isfile(path) else None),
        })
        return self

    def to_dict(self):
        return {
            "gate": self.gate_id,
            "title": self.title,
            "status": self.status,
            "reason": self.reason,
            "measurements": self.measurements,
            "limits": self.limits,
            "finding_count": len(self.findings),
            "findings": self.findings,
            "evidence": self.evidence,
        }


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


#: The file kinds a KiCad tool must see to open a project. Fixed by KiCad,
#: not by any board: the board and schematic carry their own footprints and
#: symbols, but ERC resolves library assignments through the tables and the
#: project-local libraries they name, so those travel too.
DESIGN_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_prl",
                   ".kicad_dru", ".kicad_sym", ".kicad_mod", ".kicad_wks")
DESIGN_FILES = ("fp-lib-table", "sym-lib-table")

_KIPRJMOD = re.compile(r"\$\{KIPRJMOD\}/([^\"')\s]+)")
_SHEETFILE = re.compile(r'"Sheetfile"\s+"([^"]+)"')


def design_inputs(manifest):
    """Every file a KiCad tool needs, reached from the declared sources.

    Reachability, not a directory listing: the sources the manifest selects,
    the sibling settings KiCad opens beside them, the library tables, whatever
    those tables name inside the project, and the sheets the schematic
    references. A project root that also holds outputs, candidates or another
    board's fixtures contributes none of it.

    Returns paths relative to the project root, sorted.
    """
    root = os.path.realpath(manifest.resolve("."))
    found, pending = set(), []

    def contained(candidate, authority, label):
        resolved = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath((authority, resolved)) == authority
        except ValueError:
            inside = False
        if not inside:
            raise ManifestError(
                "{} resolves outside the declared project root: {} -> {}"
                .format(label, candidate, resolved))
        return resolved

    def relative_path(value):
        if not isinstance(value, str) or not value.strip():
            raise ManifestError("a design input path must be a non-empty string")
        value = value.replace("\\", "/")
        if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
            raise ManifestError(
                "design input paths are project-relative, not absolute: {}"
                .format(value))
        return os.path.normpath(value).replace("\\", "/")

    def remember(rel):
        if rel not in found:
            found.add(rel)
            pending.append(rel)

    def take(rel):
        rel = relative_path(rel)
        lexical = os.path.join(root, rel)
        full = contained(lexical, root, "design input {!r}".format(rel))
        if os.path.isdir(full):
            visited = set()
            # Walk the lexical spelling so an allowed in-project symlink keeps
            # the relative path KiCad's table names. Every directory and file
            # is still authorized by its resolved spelling before use.
            for base, dirs, names in os.walk(lexical, followlinks=True):
                base_real = contained(
                    base, root, "design directory {!r}".format(rel))
                if base_real in visited:
                    dirs[:] = []
                    continue
                visited.add(base_real)
                safe_dirs = []
                for name in dirs:
                    directory = os.path.join(base, name)
                    directory_real = contained(
                        directory, root,
                        "design directory {!r}".format(
                            os.path.relpath(directory, root)))
                    if directory_real not in visited:
                        safe_dirs.append(name)
                dirs[:] = safe_dirs
                for name in names:
                    if name.endswith(DESIGN_SUFFIXES):
                        source = os.path.join(base, name)
                        contained(
                            source, root,
                            "design file {!r}".format(
                                os.path.relpath(source, root)))
                        remember(os.path.relpath(source, root)
                                 .replace("\\", "/"))
            return
        if os.path.isfile(full):
            remember(rel)

    for declared in sorted((manifest.get("sources") or {}).values()):
        take(declared)
    # KiCad opens these beside a project whether or not a manifest names them.
    for rel in sorted(found):
        stem = os.path.splitext(rel)[0]
        for suffix in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
            take(stem + suffix)
    for name in DESIGN_FILES:
        take(name)

    while pending:
        rel = pending.pop()
        if not rel.endswith((".kicad_sch", "-lib-table")) and \
                os.path.basename(rel) not in DESIGN_FILES:
            continue
        try:
            with open(os.path.join(root, rel), encoding="utf-8",
                      errors="ignore") as handle:
                text = handle.read()
        except OSError:
            continue
        base = os.path.dirname(rel)
        for hit in _KIPRJMOD.findall(text):
            take(hit)
        for hit in _SHEETFILE.findall(text):
            take(os.path.join(base, hit) if base else hit)
    return sorted(found)


def stage_design(manifest, destination):
    """Copy the design into `destination`, preserving relative paths.

    KiCad's `pcb drc --refill-zones --save-board` - which this validator requires,
    because an export ships whatever fill is stored and a stale one ships an
    empty plane - rewrites the board outright. So tools run against a staged
    design and never against the authoritative one. What is staged is the
    design, not the repository that holds it.
    """
    import shutil

    root = os.path.realpath(manifest.resolve("."))
    destination = os.path.abspath(destination)
    os.makedirs(destination, exist_ok=True)
    staging_root = os.path.realpath(destination)

    def require_inside(candidate, authority, label):
        resolved = os.path.realpath(candidate)
        try:
            inside = os.path.commonpath((authority, resolved)) == authority
        except ValueError:
            inside = False
        if not inside:
            raise ManifestError(
                "{} resolves outside its authority root: {} -> {}".format(
                    label, candidate, resolved))
        return resolved

    staged = []
    for rel in design_inputs(manifest):
        source = os.path.join(root, rel)
        require_inside(source, root, "staged source {!r}".format(rel))
        target = os.path.join(destination, rel)
        require_inside(target, staging_root,
                       "staging destination {!r}".format(rel))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        require_inside(os.path.dirname(target), staging_root,
                       "staging parent {!r}".format(rel))
        shutil.copy2(source, target)
        staged.append(rel)
    return staged


class ManifestError(Exception):
    pass


class Manifest:
    """The single canonical source of every threshold and policy.

    `get("a.b.c")` walks the manifest and records the access, so a report can
    say which manifest values a run actually read. A miss is an error, never a
    default.
    """

    def __init__(self, path):
        """Parse and validate. A constructed Manifest is a trusted one.

        Everything a caller needs to check about a manifest is checked here,
        exactly once: that it is JSON, that it is an object, that it declares a
        schema version this validator implements, and that its board id is a
        name safe to use as a single path component. There is deliberately no
        second, looser way to read a manifest - the reason this class exists is
        so that no command has to decide for itself how much of a file to
        believe before touching the filesystem.
        """
        from .layout import valid_board_id

        self.path = os.path.abspath(path)
        try:
            with open(self.path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise ManifestError(f"{path}: cannot be read: {exc}") from exc
        self.sha256 = sha256_bytes(raw)
        try:
            self.data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{self.path}: not valid JSON: {exc}") from exc
        if not isinstance(self.data, dict):
            raise ManifestError(
                f"{self.path}: top level is a "
                f"{type(self.data).__name__}, not a JSON object")
        if self.data.get("schema_version") != SCHEMA_VERSION:
            raise ManifestError(
                f"{self.path}: schema_version {self.data.get('schema_version')!r}, "
                f"this validator implements {SCHEMA_VERSION}")
        board_id = self.data.get("board_id")
        if not valid_board_id(board_id):
            raise ManifestError(
                f"{self.path}: board_id {board_id!r} is not a usable board "
                f"identity; it must be a single conservative slug, because it "
                f"names a directory this tool creates and removes")
        # Every declared key must be one this toolkit implements: a key the
        # toolkit would ignore is a declaration that silently does nothing,
        # and a misspelled block is four gate failures instead of one named
        # refusal. Board-local data goes under x_-prefixed keys.
        from . import manifest_schema
        try:
            manifest_schema.check(self.data, self.path)
        except manifest_schema.ManifestSchemaError as exc:
            raise ManifestError(str(exc)) from exc
        self.board_id = board_id
        self.root = os.path.dirname(self.path)
        self.accesses = []       # [(key, value)] for the parity gate

    # -- access ------------------------------------------------------------
    @staticmethod
    def _walk(node, key):
        """Follow a dotted path through dicts and list indices."""
        for part in key.split("."):
            if isinstance(node, dict):
                if part not in node:
                    return _MISSING
                node = node[part]
            elif isinstance(node, (list, tuple)):
                if not part.lstrip("-").isdigit():
                    return _MISSING
                index = int(part)
                if not -len(node) <= index < len(node):
                    return _MISSING
                node = node[index]
            else:
                return _MISSING
        return node

    def get(self, key, default=_MISSING):
        node = self._walk(self.data, key)
        if node is _MISSING:
            if default is _MISSING:
                raise ManifestError(
                    f"manifest {self.path}: missing required key {key!r}")
            self.accesses.append((key, default))
            return default
        self.accesses.append((key, node))
        return node

    def has(self, key):
        return self._walk(self.data, key) is not _MISSING

    def source_of(self, key):
        return f"{os.path.basename(self.path)}#{key}@{self.sha256[:12]}"

    def constraint(self, key, units=None, cid=None):
        """A typed constraint by stable ID. Missing keys and missing units
        both raise; there are no defaults and no untyped limits."""
        value = self.get(key)
        return Constraint(cid or key, key, value, units,
                          os.path.basename(self.path), self.sha256)

    def geometry_profile(self):
        return GeometryProfile(self.get("geometry_profile"),
                               os.path.basename(self.path), self.sha256)

    def resolve(self, *parts):
        """A path relative to the manifest's project_root."""
        base = self.get("project_root")
        return os.path.abspath(os.path.join(self.root, base, *parts))


def load_manifest(path):
    """The authoritative manifest entry point for every command.

    Raises ManifestError, and does so before any caller has been given
    anything it could build a path out of.
    """
    return Manifest(path)


# ---------------------------------------------------------------------------
# gate registry
# ---------------------------------------------------------------------------

_REGISTRY = []

#: What a gate judges, and therefore when running it is meaningful.
#:
#:   design           the design sources and the native board; answerable
#:                    mid-search about a candidate
#:   release-artifact committed or generated release artifacts and the records
#:                    binding them to the design
#:   fixture          the toolkit's own frozen test material
GATE_CLASSES = ("design", "release-artifact", "fixture")


def gate(gate_id, title, gate_class, requires=(), order=100):
    """Register a gate.

    `gate_class` is one of GATE_CLASSES and is mandatory: selection by class
    replaces board-maintained gate lists, so an unclassified gate would be a
    gate no class-driven run could reach deliberately.

    `requires` lists manifest keys the gate needs; when any is absent the gate
    reports NOT_APPLICABLE with the reason instead of silently passing. An
    entry may itself be a tuple, meaning any one of those keys satisfies it -
    which is how a key can be renamed without breaking a pinned consumer.
    """
    if gate_class not in GATE_CLASSES:
        raise ValueError(
            "gate {} declares class {!r}; a gate is one of {}".format(
                gate_id, gate_class, GATE_CLASSES))

    def wrap(fn):
        _REGISTRY.append({
            "id": gate_id, "title": title, "requires": tuple(requires),
            "fn": fn, "order": order, "class": gate_class,
        })
        return fn
    return wrap


def select_gates(tokens):
    """Expand gate-selection tokens into registered gate IDs.

    A token is an exact gate ID, an fnmatch pattern over gate IDs (`ROUTE.*`),
    or a gate class from GATE_CLASSES (`design`). Returns (ids, unknown):
    `ids` in registry order without duplicates, `unknown` the tokens that
    selected nothing - a selector that names nothing is an error for the
    caller to refuse, never a silently empty run.
    """
    import fnmatch

    entries = registered()
    ids = [entry["id"] for entry in entries]
    chosen, unknown = [], []
    for token in tokens:
        if token in GATE_CLASSES:
            hits = [e["id"] for e in entries if e["class"] == token]
        elif any(ch in token for ch in "*?["):
            hits = [i for i in ids if fnmatch.fnmatch(i, token)]
        elif token in ids:
            hits = [token]
        else:
            hits = []
        if not hits:
            unknown.append(token)
            continue
        chosen.extend(h for h in hits if h not in chosen)
    return [i for i in ids if i in chosen], sorted(unknown)


def _satisfied(manifest, requirement):
    if isinstance(requirement, tuple):
        return any(manifest.has(key) for key in requirement)
    return manifest.has(requirement)


def _missing_label(requirement):
    if isinstance(requirement, tuple):
        return " or ".join(requirement)
    return requirement


def registered():
    return sorted(_REGISTRY, key=lambda e: (e["order"], e["id"]))


def run_all(context, only=None):
    results = []
    for entry in registered():
        if only and entry["id"] not in only:
            continue
        result = GateResult(entry["id"], entry["title"])
        missing = [_missing_label(k) for k in entry["requires"]
                   if not _satisfied(context.manifest, k)]
        if missing:
            results.append(result.not_applicable(
                "manifest does not declare " + ", ".join(sorted(missing))
                + "; this board does not opt in to this gate"))
            continue
        try:
            entry["fn"](context, result)
            if result.status is None:
                result.errored("gate returned without setting a status")
            # Every limit a gate applied is pooled so the constraint-parity gate,
            # which runs last, can prove each one came from the manifest.
            context.cache("applied_limits", dict).update(
                {f"{entry['id']}.{k}": v for k, v in result.limits.items()})
        except Exception as exc:                       # fail closed
            import traceback
            result.errored(f"{type(exc).__name__}: {exc}")
            result.finding(traceback=traceback.format_exc().splitlines()[-6:])
        why = advisory_reason(context.manifest, entry["id"])
        if why:
            result.advisory(why)
        results.append(result)
    return results


def advisory_reason(manifest, gate_id):
    """Why this board declares a gate advisory, or None if it blocks."""
    for entry in manifest.get("advisory_gates", []) or []:
        if entry.get("gate") == gate_id:
            reason = entry.get("reason")
            if not reason:
                raise ValueError(
                    "advisory_gates entry for {} states no reason; a gate is "
                    "demoted on the record or not at all".format(gate_id))
            return reason
    return None


# ---------------------------------------------------------------------------
# execution context
# ---------------------------------------------------------------------------

class Context:
    """Everything a gate may look at. Created once per validation run."""

    def __init__(self, manifest, workdir, kicad_cli=None, board_path=None):
        self.manifest = manifest
        self.workdir = workdir
        # A candidate board judged in place of the declared one - the library
        # entry point for a search that must ask the release's own gates about
        # copper it has not promoted yet. Only what reads the board follows it;
        # the source closure keeps describing the authoritative design, which
        # is why a run with an override is a diagnostic and never a verdict.
        self._board_override = (os.path.abspath(board_path)
                                if board_path else None)
        os.makedirs(workdir, exist_ok=True)
        # Resolved here, once, so every gate shells out to the same absolute
        # binary and the run's provenance records which one it was - a bare
        # `kicad-cli` in a manifest is portable, but "whatever PATH meant at
        # the time" is not evidence.
        from . import preflight
        self.kicad_cli = preflight.resolve_tool(
            kicad_cli or manifest.get("tools.kicad_cli"))
        self._cache = {}
        self.tool_versions = {}

    # -- lazily loaded, shared across gates --------------------------------
    def cache(self, key, factory):
        if key not in self._cache:
            self._cache[key] = factory()
        return self._cache[key]

    def board_path(self):
        if self._board_override:
            return self._board_override
        return self.manifest.resolve(self.manifest.get("sources.pcb"))

    def schematic_path(self):
        return self.manifest.resolve(self.manifest.get("sources.schematic"))

    def project_path(self):
        return self.manifest.resolve(self.manifest.get("sources.project"))

    def board(self):
        def load():
            import pcbnew
            path = self.board_path()
            if not os.path.isfile(path):
                raise ManifestError(f"board not found: {path}")
            return pcbnew.LoadBoard(path)
        return self.cache("board", load)

    def staged_design(self):
        """A staged copy of the design that tools may open.

        KiCad can rewrite the board under `drc --save-board`, so nothing is
        pointed at the authoritative files. Built once per context and shared
        by every gate that shells out.
        """
        def build():
            import shutil
            destination = os.path.join(self.workdir, "design")
            stage_design(self.manifest, destination)
            if self._board_override:
                # The candidate takes the declared board's place in the staged
                # design, so a tool judges it with the project's own rules and
                # library tables rather than KiCad's defaults.
                shutil.copy2(self._board_override,
                             os.path.join(destination,
                                          self.manifest.get("sources.pcb")))
            return destination
        return self.cache("staged_design", build)

    def check_path(self, relative):
        """`relative` inside the staged design rather than the design."""
        return os.path.join(self.staged_design(), relative)

    def run_tool(self, args, timeout=1800):
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc

    def kicad_version(self):
        def probe():
            # Recording which tool ran must never be the thing that breaks a
            # gate: a tool too broken to state its version is a finding for
            # the gate to report, not an exception to crash on.
            try:
                proc = self.run_tool([self.kicad_cli, "--version"], timeout=120)
            except Exception as exc:                       # noqa: BLE001
                return f"UNAVAILABLE: {type(exc).__name__}: {exc}"
            lines = (proc.stdout or proc.stderr or "").strip().splitlines()
            if not lines:
                return f"UNREPORTED: {self.kicad_cli} exited {proc.returncode} " \
                       f"without printing a version"
            return lines[0]
        return self.cache("kicad_version", probe)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def summarise(results):
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    blocking = [r for r in results if r.status in Status.BLOCKING]
    return counts, blocking


def toolkit_identity():
    """The validation implementation's own identity.

    A verdict is a function of the board AND of the code that
    judged it: the same board under a different toolkit commit is a
    different claim. This records the toolkit checkout's git HEAD
    (and whether its working tree was dirty) so a consumer can bind
    a validation document to the exact implementation that produced
    it. Never fabricated - when git cannot answer, the record says
    so instead of guessing.
    """
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    record = {"toolkit_root": root, "commit": None,
              "working_tree_dirty": None,
              "detail": "git HEAD of the toolkit checkout that "
                        "executed this validation"}
    try:
        head = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30)
        if head.returncode != 0:
            record["detail"] = ("unrecorded: git rev-parse failed: "
                                + head.stderr.strip())
            return record
        record["commit"] = head.stdout.strip()
        status = subprocess.run(
            ["git", "-C", root, "status", "--porcelain"],
            capture_output=True, text=True, timeout=30)
        if status.returncode == 0:
            record["working_tree_dirty"] = bool(
                status.stdout.strip())
        else:
            # `working_tree_dirty` stays None: git being unable to answer is
            # a refusal, and rendering it as clean would fabricate an identity.
            record["detail"] = ("unrecorded: git status failed: "
                                + status.stderr.strip())
    except Exception as error:                         # noqa: BLE001
        record["detail"] = "unrecorded: {}".format(error)
    return record


def _tooling(context):
    from . import preflight
    ok, rows = preflight.environment(context.kicad_cli)
    return {
        "environment_ok": ok,
        "kicad_cli": context.kicad_cli,
        "validation_implementation": toolkit_identity(),
        "kicad_version": context.tool_versions.get("kicad", "unrecorded"),
        "components": [
            {"name": r["name"], "version": r["version"], "path": r.get("path"),
             "ok": r["ok"], "detail": r["detail"],
             **({"ownership": r["ownership"]} if r.get("ownership") else {})}
            for r in rows
        ],
    }


def _source_closure_sha256(context):
    """The design identity this verdict is about, or why it is unknown."""
    from . import closure
    try:
        return closure.current(context.manifest)[1]
    except Exception as exc:                                   # noqa: BLE001
        return "UNAVAILABLE: {}: {}".format(type(exc).__name__, exc)


def to_json(results, context, extra=None):
    counts, blocking = summarise(results)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_utc": utcnow(),
        # A verdict is about one design state. Recorded at the top level so a
        # committed validation report can be bound to the sources beside it
        # without reading a gate's measurements.
        "source_closure_sha256": _source_closure_sha256(context),
        "manifest": {
            "path": context.manifest.path,
            "sha256": context.manifest.sha256,
            "board_id": context.manifest.get("board_id"),
            "constraint_version": context.manifest.get("constraint_version"),
        },
        # Every report records not just which versions were used but where
        # each module was loaded from. Two KiCad installs, or a Shapely
        # supplied by an add-on rather than the one you expected, produce
        # different geometry; a version string alone cannot tell you which
        # environment produced a result.
        "tooling": _tooling(context),
        "summary": {
            "counts": counts,
            "blocking": [r.gate_id for r in blocking],
            "verdict": "REJECTED" if blocking else "ACCEPTED",
        },
        "gates": [r.to_dict() for r in results],
    }
    if extra:
        doc.update(extra)
    return doc


def to_markdown(doc):
    lines = [f"# Verification report - {doc['manifest']['board_id']}", ""]
    lines.append(f"- Manifest: `{os.path.basename(doc['manifest']['path'])}` "
                 f"sha256 `{doc['manifest']['sha256'][:16]}`")
    lines.append(f"- Constraint version: `{doc['manifest']['constraint_version']}`")
    lines.append(f"- KiCad: `{doc['tooling']['kicad_version']}`")
    lines.append(f"- Generated: {doc['generated_utc']}")
    lines.append("")
    lines.append(f"## Verdict: **{doc['summary']['verdict']}**")
    lines.append("")
    counts = doc["summary"]["counts"]
    lines.append("| Status | Gates |")
    lines.append("|---|---:|")
    for key in (Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_APPLICABLE):
        if key in counts:
            lines.append(f"| {key} | {counts[key]} |")
    lines.append("")
    lines.append("## Gate matrix")
    lines.append("")
    lines.append("| Gate | Status | Detail |")
    lines.append("|---|---|---|")
    for g in doc["gates"]:
        detail = g["reason"].replace("|", "\\|")
        if len(detail) > 160:
            detail = detail[:157] + "..."
        lines.append(f"| `{g['gate']}` | {g['status']} | {detail} |")
    lines.append("")
    for g in doc["gates"]:
        if g["status"] not in Status.BLOCKING or not g["findings"]:
            continue
        lines.append(f"### `{g['gate']}` - {g['title']}")
        lines.append("")
        lines.append(f"{g['reason']}")
        lines.append("")
        if g["limits"]:
            lines.append("Limits applied:")
            for name, lim in g["limits"].items():
                units = f" {lim['units']}" if lim.get("units") else ""
                lines.append(f"- `{name}` = {lim['value']}{units} "
                             f"[{lim.get('kind', 'policy')}] "
                             f"(from {lim.get('provenance', 'unrecorded')})")
            lines.append("")
        shown = g["findings"][:25]
        for f in shown:
            bits = ", ".join(f"{k}={v}" for k, v in f.items())
            lines.append(f"- {bits}")
        if len(g["findings"]) > len(shown):
            lines.append(f"- ... {len(g['findings']) - len(shown)} more")
        lines.append("")
    return "\n".join(lines)
