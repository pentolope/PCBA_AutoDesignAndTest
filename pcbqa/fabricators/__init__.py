"""JLCPCB fabrication knowledge: acquisition, catalog, selection.

JLCPCB is the only manufacturer this toolkit targets, and there is no adapter
layer for hypothetical others. What is here is JLCPCB's published process
data, the code that reads it, and the requirement-driven selection built on
top of it.

    model.py      the normalized catalog schema and its record rules
    store.py      the committed catalog, read-only
    diff.py       semantic comparison between normalized catalogs
    acquire.py    fetching and parsing (the only networking code)
    selection.py  requirement-driven fabrication profile selection
    jlcpcb.py     the sources and the parsers
    cli.py        the `fab` command group

Manufacturer-independent physics does not live here: transmission-line closed
forms are `pcbqa.transmission_line`, and the covered-microstrip reference
transcription is `pcbqa.overlay_reference`.

Nothing in this package is imported by a gate, and nothing here runs during
`validate` or `release-check`: ordinary verification is deterministic and
offline by construction. Networking exists only behind `acquire`, which only
`fab refresh` reaches. A refresh decides nothing - it shows what changed, and
a person reviewing that diff and committing is what makes a change approved.
"""

from __future__ import annotations
