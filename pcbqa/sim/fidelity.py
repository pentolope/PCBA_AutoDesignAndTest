"""Model registry: every model carries provenance and a fidelity class.

The fidelity vocabulary is shared across the simulation subsystem so
results can be compared and gated on one axis. The classes are a TRUST
DESCRIPTION, not a quality score: they say where a model's numbers
come from, and the registry refuses anything that does not declare
itself. ``FIDELITY_CLASSES`` is ordered from the strongest evidence to
the weakest; ``weakest_of`` uses that order to summarize a set of
models by its least-established member, mirroring the fail-closed
philosophy used elsewhere in the toolkit.
"""

from __future__ import annotations


class SimulationError(Exception):
    """The simulation subsystem cannot proceed as asked. Always blocks."""


#: Strongest evidence first. The order is part of the contract: a
#: scenario's required_fidelity names the weakest class it accepts.
FIDELITY_CLASSES = (
    "measured",
    "vendor-spice",
    "vendor-ibis",
    "rtl",
    "full-wave-extracted",
    "quasi-static-extracted",
    "analytic-interconnect",
    "datasheet-behavioral",
    "assumed-behavioral",
    "unsupported",
)

_REQUIRED_MODEL_KEYS = {"identity", "kind", "fidelity", "provenance"}
_KNOWN_MODEL_KEYS = _REQUIRED_MODEL_KEYS | {"ports", "spice", "notes"}


def rank(fidelity_class):
    """Position of a class in the trust order (0 is strongest)."""
    if fidelity_class not in FIDELITY_CLASSES:
        raise SimulationError(
            "fidelity {!r} is not one of the declared classes "
            "{}".format(fidelity_class, list(FIDELITY_CLASSES)))
    return FIDELITY_CLASSES.index(fidelity_class)


def weakest_of(fidelity_classes):
    """The least-established class among those given."""
    members = list(fidelity_classes)
    if not members:
        raise SimulationError(
            "no fidelity classes were given; an empty model set has "
            "no coverage summary and is not silently strong")
    return max(members, key=rank)


def meets(fidelity_class, required):
    """Whether a class is at least as established as `required`."""
    return rank(fidelity_class) <= rank(required)


def validate_model(record):
    """One model record, strictly.

    Required: identity (unique name), kind (free-form device class),
    fidelity (from FIDELITY_CLASSES), provenance (dict stating where
    the model came from - a source name at minimum). Optional: ports
    (list of port names), spice (the subcircuit text a SPICE backend
    may instantiate), notes.
    """
    if not isinstance(record, dict):
        raise SimulationError(
            "a model record must be a dict, not {!r}".format(
                type(record).__name__))
    unknown = sorted(set(record) - _KNOWN_MODEL_KEYS)
    if unknown:
        raise SimulationError(
            "model record carries unknown key(s) {}; unknown keys "
            "refuse rather than being ignored".format(unknown))
    missing = sorted(_REQUIRED_MODEL_KEYS - set(record))
    if missing:
        raise SimulationError(
            "model record is missing required key(s) {}".format(missing))
    if not isinstance(record["identity"], str) or not record["identity"]:
        raise SimulationError("model identity must be a nonempty string")
    rank(record["fidelity"])
    provenance = record["provenance"]
    if not isinstance(provenance, dict) or not provenance.get("source"):
        raise SimulationError(
            "model {!r} declares no provenance source; a model whose "
            "origin is unstated is not usable evidence".format(
                record["identity"]))
    if "spice" in record and not isinstance(record["spice"], str):
        raise SimulationError(
            "model {!r} spice text must be a string".format(
                record["identity"]))
    return record


class ModelRegistry:
    """The set of models a scenario may reference. Fail-closed lookup."""

    def __init__(self, records=()):
        self._models = {}
        for record in records:
            self.add(record)

    def add(self, record):
        validate_model(record)
        identity = record["identity"]
        if identity in self._models:
            raise SimulationError(
                "model {!r} is already registered; a silent overwrite "
                "could swap evidence classes unnoticed".format(identity))
        self._models[identity] = record
        return record

    def get(self, identity):
        try:
            return self._models[identity]
        except KeyError:
            raise SimulationError(
                "model {!r} is not registered; the simulation refuses "
                "before any simulator runs, because a missing model "
                "cannot be defaulted".format(identity)) from None

    def identities(self):
        return sorted(self._models)

    def coverage(self, identities):
        """Machine-readable coverage summary for the referenced set."""
        models = {name: self.get(name) for name in identities}
        summary = {
            name: {"kind": model["kind"],
                   "fidelity": model["fidelity"],
                   "provenance": model["provenance"]}
            for name, model in sorted(models.items())
        }
        return {
            "models": summary,
            "weakest_fidelity":
                weakest_of(m["fidelity"] for m in models.values())
                if models else None,
        }
