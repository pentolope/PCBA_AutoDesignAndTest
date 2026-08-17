"""Render one copper layer of the board to a PNG, for looking at.

KiCad's own plotters emit PDF and SVG, neither of which can be inspected
without extra tooling, and the point of this script is to be able to see what
the generator actually laid down - which track went where, and which pad it
came too close to. Nets are coloured by name so families stand out.

    python tools/plot_layer.py F.Cu out.png [--zoom x y half_span]
"""

import colorsys
import hashlib
import os
import sys

import pcbnew
from PIL import Image, ImageDraw

import design as d

SIZE = 2000


def net_colour(name):
    if not name:
        return (110, 110, 110)
    digest = hashlib.md5(name.encode()).digest()
    hue = digest[0] / 255.0
    light = 0.45 + (digest[1] / 255.0) * 0.2
    return tuple(int(255 * c) for c in colorsys.hls_to_rgb(hue, light, 0.95))


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    layer_name = sys.argv[1] if len(sys.argv) > 1 else "F.Cu"
    out = sys.argv[2] if len(sys.argv) > 2 else "layer.png"
    if "--zoom" in sys.argv:
        index = sys.argv.index("--zoom")
        cx, cy, span = (float(v) for v in sys.argv[index + 1:index + 4])
    else:
        cx, cy, span = 0.0, 0.0, d.BOARD_RADIUS + 2.0

    board = pcbnew.LoadBoard(os.path.join(root, "microphone_array_v2.kicad_pcb"))
    layer = board.GetLayerID(layer_name)
    scale = SIZE / (2.0 * span)

    image = Image.new("RGB", (SIZE, SIZE), (16, 16, 20))
    draw = ImageDraw.Draw(image)

    def point(x, y):
        return ((x - cx) * scale + SIZE / 2.0, (cy - y) * scale + SIZE / 2.0)

    def board_point(vector):
        return point(vector.x / 1e6 - d.PAGE_CX, -(vector.y / 1e6 - d.PAGE_CY))

    for footprint in board.Footprints():
        for pad in footprint.Pads():
            if not pad.IsOnLayer(layer):
                continue
            px, py = board_point(pad.GetPosition())
            size = pad.GetSize()
            half_x = size.x / 2e6 * scale
            half_y = size.y / 2e6 * scale
            if round(pad.GetOrientation().AsDegrees()) % 180 == 90:
                half_x, half_y = half_y, half_x
            colour = net_colour(pad.GetNetname())
            draw.rectangle([px - half_x, py - half_y, px + half_x, py + half_y],
                           fill=tuple(c // 2 for c in colour), outline=colour)
            label = f"{footprint.GetReference()}.{pad.GetNumber()}"
            draw.text((px + half_x + 2, py - 6), label, fill=(150, 150, 150))

    for track in board.Tracks():
        colour = net_colour(track.GetNetname())
        if isinstance(track, pcbnew.PCB_VIA):
            vx, vy = board_point(track.GetPosition())
            radius = track.GetWidth(pcbnew.F_Cu) / 2e6 * scale
            draw.ellipse([vx - radius, vy - radius, vx + radius, vy + radius],
                         outline=(255, 255, 255), fill=colour)
            continue
        if track.GetLayer() != layer:
            continue
        draw.line([board_point(track.GetStart()), board_point(track.GetEnd())],
                  fill=colour, width=max(1, int(track.GetWidth() / 1e6 * scale)))

    image.save(out)
    print(f"wrote {out} ({layer_name}, centre {cx},{cy} span {span})")


if __name__ == "__main__":
    main()
