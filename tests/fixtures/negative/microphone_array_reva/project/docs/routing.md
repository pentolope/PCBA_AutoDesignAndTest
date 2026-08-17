# Routing methodology

> Curated for use as a validation fixture. The sections describing the obsolete
> external router this revision was originally routed with, its invocation, its
> workarounds and the merge step they required have been removed along with the
> scripts that implemented them. What remains is the board's own pre-routed
> geometry and the post-route gates, which is what the fixture exercises.

## Authority

KiCad owns the board. An autorouter may add or change tracks and vias and
nothing else. Every candidate is routed into a *copy*, compared against a
semantic snapshot of the pre-route board, and only then promoted.

## What is pre-routed, and why

Three classes of copper are placed deterministically by `tools/gen_pcb.py`
rather than by the autorouter:

**Ground stitching.** Ground is a plane net that the autorouter is told to
ignore, and an inner plane cannot reach a surface-mount pad by itself. Every
ground pad therefore gets its own via plus a short connecting track. Via
direction is chosen along the pad's own axes, never diagonally: a diagonal
crosses the neighbouring pin on a fine-pitch package, and on the microphone it
lands in the corner channel the signal pads need.

**Microphone L/R straps.** Pad 2 sits inside the ground ring with a 0.40 mm gap
around it, so it cannot reach a via of its own, and it must not consume one of
the four diagonal corners. It is always on the same net as a neighbour it can
be joined to directly: ground (pad 6) on even channels, the supply pad on odd
channels.

**Microphone escapes.** This is the critical one. The `MSM261DHP006` land
pattern encloses its four signal pads in a ring of ground pads. The straight
gaps are 0.40 mm, which fits no track at all once clearance is counted. The
only way out is the diagonal corner between a side bar and an end bar, which
measures 0.566 mm - enough for a 0.15 mm track with 0.15 mm clearance and
nothing more.

A corner that tight is not something to hand to a general-purpose autorouter.
Routers that approximate pad outlines with bounding polygons close the diagonal
gap entirely and then report that no connection could be found. The escapes are
therefore generated rather than searched for, and the corridor they occupy is
reserved with a keepout rule area so nothing else is routed through it.

The geometry is identical for all sixteen channels and is generated in
footprint-local coordinates:

| Pad | Path |
|---|---|
| 1 (VDD) | inward through the pad 5 / pad 6 corner |
| 4 (DATA) | inward through the pad 5 / pad 8 corner |
| 3 (CLK) | outward through the pad 7 / pad 8 corner, then up the outside of the left ground bar |

The clock pad needs the detour because it sits on the outward half of the
package while its resistor is inboard. Every corner waypoint is the exact
midpoint of the 0.566 mm gap, giving 0.208 mm to each ground pad, and every
segment is at 45 degrees or orthogonal.

The per-channel 100 nF capacitor is rotated 180 degrees so that its ground pad
- and therefore its stitching via - faces radially inward instead of sitting in
the clock escape corridor.

## Placement is never owned by a routing import

A routed candidate contributes tracks and vias and nothing else. The pre-route
board stays the authority for footprints, outline, zones and nets, and the
semantic snapshot hash of the promoted board must match it exactly. An import
format that carries component placement - and rounds it - is the reason this
rule exists: this board is built on 22.5 degree steps, and a format storing
rotation in whole degrees silently moves most of the footprints.

Re-running `gen_pcb.py` regenerates the board from the netlist and discards all
routing, so it must not be run after a candidate has been promoted.

## Post-route gates

`tools/check_routes.py` enforces the constraints KiCad's DRC cannot express:

- tracks only on `F.Cu` and `B.Cu`;
- `AUDIO_MCLK` and `MCLK_OSC` on `F.Cu` with zero vias;
- via budgets on the PDM clock nets;
- length spread across the eight clock branches within 6 mm;
- no segment shorter than 0.05 mm and no corner sharper than 45 degrees;
- the ground stitching still intact.

KiCad's own DRC is then the authority for clearance, shorts, holes, edge
clearance and schematic parity, run with `--all-track-errors`,
`--schematic-parity`, `--severity-all` and zero tolerated violations.
