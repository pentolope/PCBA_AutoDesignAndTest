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

Comparison logic lives with the consumers; this module owns only the
record shapes and their fail-closed validation.
"""

from __future__ import annotations

SCHEMA_VERSION = "ab-metrics-2"

_SCOPES = ("net", "electrical-path", "board", "process")

_MEASURED_KEYS = {"name", "scope", "status", "value", "units",
                  "evidence_class", "provenance", "applicability"}
_UNMEASURED_KEYS = {"name", "scope", "status", "blocked_on",
                    "why_unmeasured"}

_REQUIRED_BINDING_KEYS = {"board_file_sha256", "toolkit_commit",
                          "physical_evidence", "schema_version"}


class BenchmarkError(Exception):
    """The benchmark record cannot be accepted as declared."""


def measured(name, scope, value, units, evidence_class, provenance,
             applicability):
    """One measured metric."""
    return validate_metric({
        "name": name, "scope": scope, "status": "measured",
        "value": value, "units": units,
        "evidence_class": evidence_class, "provenance": provenance,
        "applicability": applicability,
    })


def unmeasured(name, scope, blocked_on, why_unmeasured):
    """One explicitly unmeasured metric - no value, ever."""
    return validate_metric({
        "name": name, "scope": scope, "status": "unmeasured",
        "blocked_on": blocked_on, "why_unmeasured": why_unmeasured,
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
    for key in ("toolkit_commit", "physical_evidence"):
        if not binding[key]:
            raise BenchmarkError(
                "report binding needs a nonempty {}".format(key))
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


def comparable(metric_a, metric_b):
    """Whether two metrics may be numerically compared at all."""
    return (metric_a["scope"] == metric_b["scope"]
            and metric_a["name"] == metric_b["name"]
            and metric_a["status"] == "measured"
            and metric_b["status"] == "measured"
            and metric_a["units"] == metric_b["units"])
