"""A stand-in for `kicad-cli pcb drc` that needs no KiCad installed.

Two mutation tests are about what the *gate* does with a report - that an
unsupported schema is an ERROR, and that a rule the project has set to
`ignore` fails the authoritative gate. Neither is about KiCad. They were
nevertheless invoking the absolute Windows path out of the board manifest, so
on any other machine they failed for the wrong reason: no tool, no report, and
an ERROR that says "invocation failed" long before the assertion under test.

This writes the report instead. It is a fake, not a mock: it reads the same
project file KiCad would read and reports the same `ignored_checks` KiCad
would report, which is the only part of KiCad's behaviour these tests depend
on. Everything else in the report is a valid, empty, clean run.

    fake_kicad_cli.py pcb drc --output REPORT [...] BOARD
"""

from __future__ import annotations

import json
import os
import sys

SCHEMA = "https://schemas.kicad.org/drc.v1.json"
VERSION = "10.0.5"


def ignored_checks(board):
    """What KiCad would list as ignored, read from the project beside the board.

    A severity of "ignore" means the check did not run. KiCad records those in
    the report, and that record is what the gate reads; deriving it from the
    project here keeps the test's cause and effect intact - edit the project,
    and the gate sees an ignored check.
    """
    project = os.path.splitext(board)[0] + ".kicad_pro"
    if not os.path.isfile(project):
        return []
    with open(project, encoding="utf-8") as fh:
        document = json.load(fh)
    severities = (document.get("board", {})
                  .get("design_settings", {})
                  .get("rule_severities", {}))
    return [{"key": key, "description": key.replace("_", " ")}
            for key, value in sorted(severities.items())
            if value == "ignore"]


def main(argv):
    # kicad-cli accepts the output as -o or --output, joined or separate.
    output, board = None, None
    for index, item in enumerate(argv):
        if item in ("-o", "--output") and index + 1 < len(argv):
            output = argv[index + 1]
        elif item.startswith("--output="):
            output = item.split("=", 1)[1]
        elif item.startswith("-o=") :
            output = item.split("=", 1)[1]
        elif item.endswith((".kicad_pcb", ".kicad_sch")):
            board = item
    if output is None or board is None:
        sys.stderr.write("fake kicad-cli: no --output or no board\n")
        return 2

    report = {
        "$schema": SCHEMA,
        "coordinate_units": "mm",
        "date": "2026-01-01T00:00:00+00:00",
        "kicad_version": VERSION,
        "source": os.path.basename(board),
        "ignored_checks": ignored_checks(board),
        "included_severities": ["error", "warning", "exclusion"],
        "violations": [],
        "unconnected_items": [],
        "schematic_parity": [],
    }
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    with open(output, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    return 0                       # clean run, no violations


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
