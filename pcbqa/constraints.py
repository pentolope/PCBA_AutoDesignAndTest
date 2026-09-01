"""Typed constraints.

Every policy comparison a gate makes must go through a `Constraint` obtained by
stable ID. The constraint carries its value, units and manifest provenance, and
the result records all three, so "which number did this gate actually apply and
where did it come from" is answerable from the JSON report alone.

Computational tolerances - the numbers that exist because arithmetic is finite,
not because a fabricator said so - live in a separate versioned geometry
profile and are reported as such. They are never mixed in with process limits.
"""

from __future__ import annotations

import math


class ConstraintError(Exception):
    pass


class Constraint:
    __slots__ = ("id", "key", "value", "units", "manifest", "sha256", "kind")

    def __init__(self, cid, key, value, units, manifest_name, sha256, kind="policy"):
        # Units are structural, not advisory: a limit reported without them
        # cannot be compared to a measurement by anyone reading the result.
        # Refusing here is why no later gate has to prove gates were written
        # correctly.
        if not units:
            raise ConstraintError(
                "constraint {!r} declares no units; a limit without units is "
                "not a typed constraint".format(cid or key))
        self.id = cid
        self.key = key
        self.value = value
        self.units = units
        self.manifest = manifest_name
        self.sha256 = sha256
        self.kind = kind

    @property
    def provenance(self):
        return f"{self.manifest}#{self.key}@{self.sha256[:12]}"

    def to_dict(self):
        return {
            "id": self.id,
            "value": self.value,
            "units": self.units,
            "kind": self.kind,
            "manifest_key": self.key,
            "provenance": self.provenance,
        }

    # Comparisons are expressed on the constraint so a gate never extracts a
    # policy number and writes its own inequality beside a measurement.
    def violated_maximum(self, measured):
        return measured > self.value

    def violated_minimum(self, measured):
        return measured < self.value

    def differs(self, measured):
        return measured != self.value

    def differs_by_more_than(self, measured, expected):
        return abs(measured - expected) > self.value

    def within(self, measured, expected):
        return abs(measured - expected) <= self.value

    def contains(self, measured, upper_inclusive=False):
        try:
            low, high = self.value
        except (TypeError, ValueError) as exc:
            raise ConstraintError(
                "constraint {!r} is not a two-ended range".format(
                    self.id)) from exc
        return low <= measured <= high if upper_inclusive \
            else low <= measured < high

    def __repr__(self):
        return f"<Constraint {self.id}={self.value}{self.units or ''}>"


def implementation_constant(value, rationale):
    """Mark a numeric algorithm constant as non-policy for source audit.

    The returned object is deliberately just the original number: this is a
    development-time declaration, not another runtime quantity system.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(float(value)):
        raise ConstraintError("an implementation constant is finite numeric")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ConstraintError("an implementation constant needs a rationale")
    return value


class GeometryProfile:
    """Versioned computational tolerances, separate from process policy."""

    def __init__(self, data, manifest_name, sha256):
        self.data = data
        self.manifest = manifest_name
        self.sha256 = sha256

    @property
    def version(self):
        return self.data.get("version", "unversioned")

    def tolerance(self, name):
        if name not in self.data.get("tolerances", {}):
            raise ConstraintError(
                f"geometry profile {self.version!r} declares no tolerance {name!r}")
        entry = self.data["tolerances"][name]
        return Constraint(f"geometry.{name}", f"geometry_profile.tolerances.{name}.value",
                          entry["value"], entry.get("units"),
                          self.manifest, self.sha256, kind="tolerance")

    def to_dict(self):
        return {"version": self.version,
                "tolerances": self.data.get("tolerances", {})}
