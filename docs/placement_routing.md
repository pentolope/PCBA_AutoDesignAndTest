# Placement and routing: next-generation workflow groundwork

Status: investigation and contract groundwork. Nothing here changes
production validation.

## Target architecture

    AI semantic floorplan
        -> placement constraints        (pcbqa.placement, this toolkit)
        -> numerical placement optimizer
        -> trial routing
        -> route/electrical score
        -> perturb and retry

The AI states intent; deterministic tools own coordinates. The
constraint vocabulary lives in `pcbqa/placement.py` (strict,
machine-readable, board-agnostic). Score plumbing belongs to the
optimizer integration, not to the constraint schema.

## KiCadRoutingTools evaluation (installed plugin, surveyed at
com_github_drandyhaas_kicadroutingtools, 128 modules)

Capabilities confirmed present in the installed distribution:

- Rust-accelerated A* routing, multi-layer, automatic vias
  (`route.py`, `rust_router/`).
- Rip-up and reroute with progressive blocker analysis
  (`rip_up_reroute.py`, `reroute_loop.py`, `leg_rip.py`,
  `rip_defer.py`, `rip_restore.py`).
- Placement optimization for routability BEFORE routing
  (`place_optimize.py`) and a placement/trial-route iteration loop
  (`place_route_loop.py`) - structurally the exact loop the target
  architecture wants.
- Length matching with trombone meanders and TIME matching using
  propagation delay with microstrip-vs-stripline awareness
  (`length_matching.py`).
- Differential pairs including multi-point and polarity resolution
  (`diff_pair_*.py`), bus corridors (`bus_corridor.py`,
  `bus_detection.py`), BGA/QFN fanout (`bga_fanout.py`,
  `qfn_fanout.py`).
- Plane routing with resistance / max-current reporting
  (`route_planes.py`, `analyze_power_paths.py`).
- DRC and verification helpers (`check_drc.py`, `check_connected.py`,
  `check_impedance.py`).

## Where this toolkit remains the higher-level authority

These are semantic gaps, not criticisms; the router works at net /
xnet level and does its job there.

1. **ElectricalPath semantics.** Our timing gates measure declared
   source-to-destination paths across component traversals with
   recorded uncertainty and path-integrity counting. The router's
   length/time matching operates on nets and grouped xnets; it does
   not know our declared interfaces, expected path counts, or
   fail-closed integrity rules. Route acceptance must therefore be
   judged by OUR gates after routing, never by the router's own
   matching reports alone.
2. **Impedance evidence.** `check_impedance.py`-style widths are
   calculator-grade. Width policy comes from our evidence-pinned
   impedance contract (design_guidance, provisional corridors); the
   router consumes widths, it does not decide them.
3. **Fabrication limits.** Clearances/track minima come from the
   approved fabricator catalog through our selector, as constraints
   handed to the router - not from router defaults.
4. **Release claims.** Trial-route success is a routability SCORE for
   the optimization loop. Only the toolkit's gates turn copper into
   accepted evidence.

## Discovered limitations (verified by invocation, not assumed)

- `place_optimize.py` boundary detection (`placement/quench.py` via
  `kicad_parser.extract_board_bounds`) reads gr_rect/gr_line/gr_arc/
  gr_poly on Edge.Cuts but NOT `gr_circle`: a circular board outline
  raises "No board boundary (Edge.Cuts) found". A generator can work
  around it by converting the CANDIDATE's circle to two semicircular
  arcs (geometrically identical; the authoritative board is
  untouched). Upstreamable.
- `--lock REF ...` works as advertised for requirement-fixed parts.

## Routability signal

Use trial-route outcome quality (completion rate, via count, ripped
nets, corridor violations) as the primary signal, with ratline length
only as a cheap pre-filter - `place_route_loop.py` already embodies
this shape.

## Next steps

- Prototype: constraint set -> place_optimize -> route -> score, on a
  disposable candidate board copy (never the authoritative board).
- Define the score vector alongside whatever metric schema the
  consuming repository uses to compare candidates.
- Decide the invocation boundary (CLI vs python API) after a trial
  run; the plugin exposes both.
