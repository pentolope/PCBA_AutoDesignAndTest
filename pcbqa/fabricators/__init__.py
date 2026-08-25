"""Fabricator knowledge: what a manufacturer can build, held to evidence.

A design tool that selects a fabrication process needs to know what the
fabricator offers - layer counts, thicknesses, copper weights, stackups,
materials, process limits. That knowledge has two properties that pull in
opposite directions: it comes from the fabricator's own published sources,
which change without notice, and it feeds design and validation decisions,
which must be reproducible and must never change because a webpage did.

The resolution is two distinct states with an explicit door between them:

    fabricator's official sources
              |
              |  acquisition (network, on request, never during validation)
              v
       OBSERVED snapshot          <- latest fetch; trusted for nothing
              |
              |  semantic comparison (normalized records, not raw bytes)
              v
       APPROVED snapshot          <- the only state design work may read
              ^
              |
       explicit, audited promotion

A refresh may discover that the fabricator changed something. It may never
decide that the change is trustworthy: the approved snapshot is untouched
until a person promotes a reviewed observation, and the promotion itself is
recorded - what replaced what, when, on what evidence, with what semantic
differences.

Layout of the package:

    model.py      the normalized, fabricator-neutral catalog schema
    store.py      approved/observed storage, freshness, promotion
    diff.py       semantic comparison between normalized catalogs
    acquire.py    fetching and snapshot assembly (the only networking code)
    selection.py  requirement-driven fabrication profile selection
    jlcpcb.py     the JLCPCB adapter: sources and parsers
    cli.py        the `fab` command group

Nothing in `pcbqa.fabricators` is imported by validation gates' modules at
import time, and nothing here runs during `validate` or `release`: ordinary
verification is deterministic and offline by construction. Networking exists
only behind `acquire`, which only the `fab refresh` command reaches.

Adapters register here by name. An adapter owns its sources and its parsing;
everything downstream of the normalized catalog is fabricator-neutral.
"""

from __future__ import annotations


def adapter(name):
    """The adapter module for a fabricator, by registry name.

    Imported lazily so that loading the package costs nothing and so that a
    broken adapter cannot take down commands that never touch it.
    """
    if name == "jlcpcb":
        from . import jlcpcb
        return jlcpcb
    raise KeyError(
        "no fabricator adapter is registered under {!r}; this release has: "
        "jlcpcb".format(name))


FABRICATORS = ("jlcpcb",)
