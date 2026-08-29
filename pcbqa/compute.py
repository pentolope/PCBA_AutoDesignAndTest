"""Compute spend as evidence: disjoint categories, honest sums.

A search that cannot say where its seconds went cannot say what a
filter saved. This module gives compute accounting the same
discipline every other artifact here gets:

- every entry belongs to exactly ONE category from a declared,
  disjoint set (a probe second is never also a full-routing
  second);
- the categorised sum must equal the measured total within a
  stated tolerance, or the summary refuses - a ledger that doesn't
  add up is not evidence of anything;
- classification (was the spend a useful finalist, a diagnostic,
  or avoidable in hindsight?) is separate from category, optional,
  and never invented.

The consumer declares its own category set; the default names the
stages a placement-search pipeline actually has. Nothing here
measures time - callers bring measured entries, this module keeps
them honest.
"""

from __future__ import annotations


class ComputeError(Exception):
    """The compute ledger cannot be summarised as asked."""


#: The default disjoint category set for a placement-search
#: pipeline. A consumer with different stages declares its own.
DEFAULT_CATEGORIES = (
    "placement",
    "critical-planning",
    "proxy-scoring",
    "probe-routing",
    "repair",
    "full-routing",
    "validation",
    "parasitics",
)

#: How a spend looks in hindsight. Optional per entry, never
#: guessed: "useful-finalist" fed the artifact that won or shipped;
#: "diagnostic" bought information that changed a decision;
#: "avoidable" was knowable-wasted at the time it was spent.
CLASSIFICATIONS = ("useful-finalist", "diagnostic", "avoidable")


def validate_entries(entries, categories=DEFAULT_CATEGORIES):
    """Every entry: a label, one known category, a non-negative
    seconds figure, and (optionally) one known classification."""
    if not isinstance(entries, (list, tuple)):
        raise ComputeError("a compute ledger is a list of entries")
    known = set(categories)
    if len(known) != len(tuple(categories)):
        raise ComputeError("the category set repeats a name; "
                           "categories must be disjoint by name")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ComputeError(
                "entry {} is a {}, not an object".format(
                    index, type(entry).__name__))
        for field in ("label", "category", "seconds"):
            if field not in entry:
                raise ComputeError(
                    "entry {} ({!r}) states no {!r}".format(
                        index, entry.get("label"), field))
        if entry["category"] not in known:
            raise ComputeError(
                "entry {} ({!r}) claims category {!r}, which is "
                "not in the declared set {}".format(
                    index, entry["label"], entry["category"],
                    sorted(known)))
        seconds = entry["seconds"]
        if not isinstance(seconds, (int, float)) \
                or isinstance(seconds, bool) or seconds < 0:
            raise ComputeError(
                "entry {} ({!r}) seconds must be a non-negative "
                "number, got {!r}".format(index, entry["label"],
                                          seconds))
        classification = entry.get("classification")
        if classification is not None \
                and classification not in CLASSIFICATIONS:
            raise ComputeError(
                "entry {} ({!r}) classification {!r} is not one "
                "of {}".format(index, entry["label"],
                               classification,
                               list(CLASSIFICATIONS)))
    return list(entries)


def summarize(entries, measured_total_seconds=None,
              tolerance_seconds=1.0, categories=DEFAULT_CATEGORIES):
    """The ledger's honest summary, or a refusal.

    When ``measured_total_seconds`` is given (a wall measurement
    taken OUTSIDE the entries), the categorised sum must match it
    within ``tolerance_seconds``: a shortfall is unaccounted time
    and an excess is double-counting, and both make every derived
    "compute avoided" claim unusable, so both refuse.
    """
    validate_entries(entries, categories)
    by_category = {}
    by_classification = {}
    for entry in entries:
        by_category[entry["category"]] = round(
            by_category.get(entry["category"], 0.0)
            + float(entry["seconds"]), 3)
        classification = entry.get("classification")
        if classification is not None:
            by_classification[classification] = round(
                by_classification.get(classification, 0.0)
                + float(entry["seconds"]), 3)
    total = round(sum(by_category.values()), 3)
    record = {
        "kind": "compute-summary",
        "entries": len(entries),
        "by_category": dict(sorted(by_category.items())),
        "by_classification": dict(sorted(
            by_classification.items())),
        "categorized_total_seconds": total,
        "measured_total_seconds": measured_total_seconds,
        "meaning": (
            "every second sits in exactly one category; the "
            "categorised sum equals the measured total or this "
            "summary does not exist"
            if measured_total_seconds is not None else
            "every second sits in exactly one category; NO "
            "measured total was supplied, so this summary asserts "
            "only the categorisation, never completeness - spends "
            "outside these entries are unaccounted here"),
    }
    if measured_total_seconds is not None:
        difference = round(total - float(measured_total_seconds), 3)
        record["difference_seconds"] = difference
        if abs(difference) > tolerance_seconds:
            raise ComputeError(
                "categorised seconds ({}) and the measured total "
                "({}) differ by {}s, beyond the {}s tolerance: "
                "{} - a ledger that does not add up supports no "
                "compute claim".format(
                    total, measured_total_seconds, difference,
                    tolerance_seconds,
                    "double-counting" if difference > 0
                    else "unaccounted time"))
    return record
