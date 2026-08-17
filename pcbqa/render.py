"""Draw the four shipped copper layers from the archive, by filename alone.

The point of the fabrication naming is that a reader who knows nothing about
KiCad, and who cannot see a Gerber X2 attribute because there are none, can
still tell this is a four-layer board and see copper on every layer. This does
exactly that: it opens the archive, picks the four files out by extension, and
renders what it parses out of them into one SVG.

It shares no code path with the exporter - it goes through the validator's own
RS-274X parser - so agreement between the picture and the board is evidence
rather than a restatement.

    "C:/Program Files/KiCad/10.0/bin/python.exe" tools/render_copper_layers.py [OUT.svg]
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "verification"))

from pcbqa import gerber                                # noqa: E402

ARCHIVE = os.path.join(HERE, "generated", "release",
                       "microphone_array_v2-revA-fabrication.zip")
# Protel copper extensions, in stack order. Nothing else identifies them.
STACK = [(".GTL", "L1 - F.Cu", "#c62828"),
         (".G2L", "L2 - inner 1", "#1565c0"),
         (".G3L", "L3 - inner 2", "#2e7d32"),
         (".GBL", "L4 - B.Cu", "#6a1b9a")]
SCALE = 1.6
PANEL = 130.0


def render(archive=ARCHIVE, out_path=None):
    out_path = out_path or os.path.join(HERE, "generated", "release",
                                        "renders", "copper_layers.svg")
    work = tempfile.mkdtemp(prefix="copper_render_")
    try:
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(work)
        layers, _drills, _extra = gerber.load_layers(work)
        picked = []
        for ext, label, colour in STACK:
            name = next((n for n in sorted(layers)
                         if n.upper().endswith(ext)), None)
            picked.append((ext, label, colour, name,
                           layers[name].union() if name else None))

        width = int(PANEL * SCALE * len(picked))
        height = int(PANEL * SCALE + 40)
        parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='{}' "
                 "height='{}' viewBox='0 0 {} {}'>".format(
                     width, height, width, height),
                 "<rect width='100%' height='100%' fill='#fbfbfb'/>"]
        for index, (ext, label, colour, name, shape) in enumerate(picked):
            ox = index * PANEL * SCALE
            parts.append("<rect x='{:.0f}' y='24' width='{:.0f}' "
                         "height='{:.0f}' fill='none' stroke='#ddd'/>".format(
                             ox + 4, PANEL * SCALE - 8, PANEL * SCALE - 8))
            area = 0.0
            if shape is not None and not shape.is_empty:
                area = shape.area
                geoms = shape.geoms if hasattr(shape, "geoms") else [shape]
                for poly in geoms:
                    rings = [poly.exterior] + list(poly.interiors)
                    path = []
                    for ring in rings:
                        pts = ["{:.2f},{:.2f}".format(
                            ox + (x - 90.0) * SCALE, 24 + (y + 210.0) * SCALE)
                            for x, y in ring.coords]
                        path.append("M " + " L ".join(pts) + " Z")
                    parts.append("<path d='{}' fill='{}' fill-opacity='0.75' "
                                 "fill-rule='evenodd' stroke='none'/>".format(
                                     " ".join(path), colour))
            parts.append("<text x='{:.0f}' y='16' font-family='sans-serif' "
                         "font-size='13' fill='#222'>{}  {}</text>".format(
                             ox + 8, label, ext))
            parts.append("<text x='{:.0f}' y='{:.0f}' font-family='sans-serif' "
                         "font-size='12' fill='#555'>{}  -  {:.0f} mm2 "
                         "copper</text>".format(
                             ox + 8, PANEL * SCALE + 32,
                             name or "MISSING", area))
        parts.append("</svg>")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(parts))
        return out_path, [(label, name, 0.0 if s is None else s.area)
                          for _e, label, _c, name, s in picked]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv):
    out, summary = render(out_path=argv[1] if len(argv) > 1 else None)
    for label, name, area in summary:
        print("  {:<14} {:<34} {:9.1f} mm2".format(label, name or "MISSING",
                                                   area))
    print("wrote " + out)
    return 0 if all(a > 0 for _l, _n, a in summary) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
