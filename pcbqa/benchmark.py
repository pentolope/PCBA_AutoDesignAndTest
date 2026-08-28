"""The typed A/B benchmark contract: metrics that cannot lie by shape.

Board A and every Board B candidate are evaluated by this one
contract, and the shape itself enforces the honesty rules:

  * a MEASURED metric carries value, units, evidence class,
    provenance and applicability;
  * an UNMEASURED metric carries blocked_on and why_unmeasured and
    may NOT carry a value - absence of measurement can never become a
    numeric zero or a default;
  * every metric declares its SCOPE (net, electrical-path, board,
    process), so similarly named quantities of different scopes are
    never comparable by accident;
  * a report binds the exact board file SHA-256, the toolkit commit,
    the physical-evidence identity its measurements consumed, and
    this schema version.

``compare_reports`` is the only sanctioned way to place two reports
side by side: it verifies schema compatibility, structured
physical-evidence identity and metric-shape agreement before any
number meets another, and it fails closed on anything unknown. Two
reports that merely LOOK similar never compare by accident.
"""

from __future__ import annotations

SCHEMA_VERSION = "ab-metrics-4"

#: Schema versions this comparator understands. A version outside
#: this set - older, newer, or foreign - fails closed; an explicit
#: migration rule would extend this map, never a guess.
_COMPARABLE_SCHEMAS = {SCHEMA_VERSION}

_EVIDENCE_KEYS = {"kind", "digest", "detail"}

_SCOPES = ("net", "electrical-path", "board", "process")

_MEASURED_KEYS = {"name", "scope", "status", "definition", "value",
                  "units", "evidence_class", "provenance",
                  "applicability"}
_UNMEASURED_KEYS = {"name", "scope", "status", "definition",
                    "blocked_on", "why_unmeasured"}

_REQUIRED_BINDING_KEYS = {"board_file_sha256", "toolkit_commit",
                          "physical_evidence", "schema_version"}


class BenchmarkError(Exception):
    """The benchmark record cannot be accepted as declared."""


def measured(name, scope, definition, value, units, evidence_class,
             provenance, applicability):
    """One measured metric. ``definition`` is the metric's stable
    semantic identity (producer/quantity@semantic-version): two
    metrics with one name but different definitions are different
    quantities, and the comparator refuses to pair them."""
    return validate_metric({
        "name": name, "scope": scope, "status": "measured",
        "definition": definition, "value": value, "units": units,
        "evidence_class": evidence_class, "provenance": provenance,
        "applicability": applicability,
    })


def unmeasured(name, scope, definition, blocked_on, why_unmeasured):
    """One explicitly unmeasured metric - no value, ever. It still
    names the DEFINITION of the quantity it fails to measure."""
    return validate_metric({
        "name": name, "scope": scope, "status": "unmeasured",
        "definition": definition, "blocked_on": blocked_on,
        "why_unmeasured": why_unmeasured,
    })


def validate_metric(metric):
    if not isinstance(metric, dict):
        raise BenchmarkError("a metric must be a dict")
    scope = metric.get("scope")
    if scope not in _SCOPES:
        raise BenchmarkError(
            "metric scope {!r} is not one of {}".format(
                scope, list(_SCOPES)))
    name = metric.get("name")
    if not isinstance(name, str) or not name:
        raise BenchmarkError("a metric needs a nonempty name")
    definition = metric.get("definition")
    if not isinstance(definition, str) or not definition:
        raise BenchmarkError(
            "metric {!r} needs a nonempty semantic definition "
            "identity".format(name))
    status = metric.get("status")
    if status == "measured":
        if set(metric) != _MEASURED_KEYS:
            raise BenchmarkError(
                "a measured metric carries exactly {}".format(
                    sorted(_MEASURED_KEYS)))
        value = metric["value"]
        if isinstance(value, bool) or \
                not isinstance(value, (int, float)) or \
                value != value or \
                value in (float("inf"), float("-inf")):
            raise BenchmarkError(
                "measured value must be a finite number, "
                "not {!r}".format(value))
        for key in ("units", "evidence_class", "applicability"):
            if not isinstance(metric[key], str) or not metric[key]:
                raise BenchmarkError(
                    "measured metric {!r} needs a nonempty "
                    "{}".format(name, key))
        if not isinstance(metric["provenance"], dict) or \
                not metric["provenance"]:
            raise BenchmarkError(
                "measured metric {!r} needs provenance".format(name))
    elif status == "unmeasured":
        if set(metric) != _UNMEASURED_KEYS:
            raise BenchmarkError(
                "an unmeasured metric carries exactly {} - in "
                "particular NO value: absence can never read as "
                "zero".format(sorted(_UNMEASURED_KEYS)))
        for key in ("blocked_on", "why_unmeasured"):
            if not isinstance(metric[key], str) or not metric[key]:
                raise BenchmarkError(
                    "unmeasured metric {!r} needs a nonempty "
                    "{}".format(name, key))
    else:
        raise BenchmarkError(
            "metric status {!r} is neither measured nor "
            "unmeasured".format(status))
    return metric


def report(binding, metrics):
    """A complete report: identity binding plus validated metrics."""
    if not isinstance(binding, dict) or \
            set(binding) != _REQUIRED_BINDING_KEYS:
        raise BenchmarkError(
            "a report binding carries exactly {}".format(
                sorted(_REQUIRED_BINDING_KEYS)))
    if binding["schema_version"] != SCHEMA_VERSION:
        raise BenchmarkError(
            "schema_version {!r} is not this contract's "
            "{!r}; reports from different schema versions are not "
            "comparable".format(binding["schema_version"],
                                SCHEMA_VERSION))
    sha = binding["board_file_sha256"]
    if not isinstance(sha, str) or len(sha) != 64 or \
            not set(sha) <= set("0123456789abcdef"):
        raise BenchmarkError(
            "board_file_sha256 must be a 64-hex-character digest")
    if not binding["toolkit_commit"]:
        raise BenchmarkError(
            "report binding needs a nonempty toolkit_commit")
    _validate_evidence(binding["physical_evidence"])
    names = set()
    for metric in metrics:
        validate_metric(metric)
        key = (metric["scope"], metric["name"])
        if key in names:
            raise BenchmarkError(
                "metric {} appears twice in one scope".format(key))
        names.add(key)
    return {"kind": "ab-benchmark-report", "binding": dict(binding),
            "metrics": list(metrics)}


def _validate_evidence(evidence):
    """Structured physical-evidence identity: kind, digest, detail."""
    if not isinstance(evidence, dict) or \
            set(evidence) != _EVIDENCE_KEYS:
        raise BenchmarkError(
            "physical_evidence must be a structured identity with "
            "exactly keys {} - free text cannot be compared, so it "
            "cannot bind a report".format(sorted(_EVIDENCE_KEYS)))
    digest = evidence["digest"]
    if not (isinstance(digest, str) and len(digest) == 64
            and set(digest) <= set("0123456789abcdef")):
        raise BenchmarkError(
            "physical_evidence digest must be a 64-hex-character "
            "SHA-256, not {!r}".format(digest))
    for key in ("kind", "detail"):
        if not isinstance(evidence[key], str) or not evidence[key]:
            raise BenchmarkError(
                "physical_evidence needs a nonempty {}".format(key))
    return evidence


def compare_reports(report_a, report_b):
    """Compare two reports, refusing every incompatibility.

    Checks, in order: both are reports of this module's kind; both
    schema versions are known to this comparator and equal (an
    unknown version fails closed - never assume a foreign contract
    matches); the structured physical evidence is identical in kind
    and digest (numbers measured under different physics never meet);
    and every metric name shared by both carries the same scope and
    units. Board SHAs are expected to differ - that is the point of
    A/B. Toolkit commits may differ: the versioned metric schema is
    the semantic contract, and both commits are recorded in the
    result for the reviewer.

    The result pairs measured metrics (with delta = b - a), lists
    blocked pairs where one side is unmeasured (with blocked_on),
    and lists metrics only one report carries. Nothing numeric is
    synthesized for an unmeasured side.
    """
    for label, report in (("a", report_a), ("b", report_b)):
        if not isinstance(report, dict) or \
                report.get("kind") != "ab-benchmark-report":
            raise BenchmarkError(
                "report {} is not an ab-benchmark-report".format(
                    label))
    binding_a = report_a["binding"]
    binding_b = report_b["binding"]
    for label, binding in (("a", binding_a), ("b", binding_b)):
        if binding["schema_version"] not in _COMPARABLE_SCHEMAS:
            raise BenchmarkError(
                "report {} carries schema_version {!r}, which this "
                "comparator does not know; unknown compatibility "
                "fails closed".format(label,
                                      binding["schema_version"]))
    if binding_a["schema_version"] != binding_b["schema_version"]:
        raise BenchmarkError(
            "the reports carry different schema versions ({!r} vs "
            "{!r}) and no migration rule exists".format(
                binding_a["schema_version"],
                binding_b["schema_version"]))
    evidence_a = _validate_evidence(binding_a["physical_evidence"])
    evidence_b = _validate_evidence(binding_b["physical_evidence"])
    if evidence_a["kind"] != evidence_b["kind"] or \
            evidence_a["digest"] != evidence_b["digest"]:
        raise BenchmarkError(
            "the reports consumed different physical evidence "
            "({}:{}... vs {}:{}...); comparing them would mix "
            "physics".format(
                evidence_a["kind"], evidence_a["digest"][:12],
                evidence_b["kind"], evidence_b["digest"][:12]))
    metrics_a = {(m["scope"], m["name"]): m
                 for m in report_a["metrics"]}
    metrics_b = {(m["scope"], m["name"]): m
                 for m in report_b["metrics"]}
    compared = []
    blocked = []
    for key in sorted(set(metrics_a) & set(metrics_b)):
        metric_a, metric_b = metrics_a[key], metrics_b[key]
        scope, name = key
        if metric_a["definition"] != metric_b["definition"]:
            raise BenchmarkError(
                "metric {} carries definition {!r} vs {!r}: one "
                "name, two semantics - produced by different "
                "extractor semantics, these are different "
                "quantities and never compare".format(
                    key, metric_a["definition"],
                    metric_b["definition"]))
        if metric_a["status"] == "measured" and \
                metric_b["status"] == "measured":
            if metric_a["units"] != metric_b["units"]:
                raise BenchmarkError(
                    "metric {} carries units {!r} vs {!r}; one name "
                    "with two units is a contract violation, not a "
                    "conversion opportunity".format(
                        key, metric_a["units"], metric_b["units"]))
            compared.append({
                "scope": scope, "name": name,
                "units": metric_a["units"],
                "a": metric_a["value"], "b": metric_b["value"],
                "delta_b_minus_a": metric_b["value"]
                - metric_a["value"],
                "evidence_classes": [metric_a["evidence_class"],
                                     metric_b["evidence_class"]],
            })
        else:
            entry = {"scope": scope, "name": name}
            for side, metric in (("a", metric_a), ("b", metric_b)):
                if metric["status"] == "unmeasured":
                    entry["{}_blocked_on".format(side)] = \
                        metric["blocked_on"]
                else:
                    entry["{}_value".format(side)] = metric["value"]
            blocked.append(entry)
    return {
        "kind": "ab-comparison",
        "binding": {
            "schema_version": binding_a["schema_version"],
            "physical_evidence": evidence_a,
            "board_file_sha256_a": binding_a["board_file_sha256"],
            "board_file_sha256_b": binding_b["board_file_sha256"],
            "toolkit_commit_a": binding_a["toolkit_commit"],
            "toolkit_commit_b": binding_b["toolkit_commit"],
        },
        "compared": compared,
        "blocked": blocked,
        "only_a": sorted("{}:{}".format(*key) for key in
                         set(metrics_a) - set(metrics_b)),
        "only_b": sorted("{}:{}".format(*key) for key in
                         set(metrics_b) - set(metrics_a)),
    }


def comparable(metric_a, metric_b):
    """Whether two metrics may be numerically compared at all."""
    return (metric_a["scope"] == metric_b["scope"]
            and metric_a["name"] == metric_b["name"]
            and metric_a["status"] == "measured"
            and metric_b["status"] == "measured"
            and metric_a["units"] == metric_b["units"])
