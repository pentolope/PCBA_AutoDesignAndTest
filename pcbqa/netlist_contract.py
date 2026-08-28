"""The netlist contract: required connectivity comes from the
authoritative product intent, never from the candidate.

A candidate board that dropped a footprint, lost a pad, swapped a
net assignment or invented a net would - if judged against its own
netlist - shrink or reshape its own denominator and read as more
complete than it is. This module extracts a CONTRACT (every
footprint, every pad, every pad-to-net assignment) from the
authoritative board, compares a candidate against it with
machine-readable differences, and derives the required net set from
the CONTRACT alone.

This is deliberately separate from neighbouring truths:

- netlist PARITY (here): does the candidate implement the intended
  product at all;
- required-connectivity DENOMINATOR (here, from the contract): which
  nets must eventually be electrically complete;
- copper CONNECTIVITY (``pcbqa.connectivity``): whether the copper
  actually joins each net's pads;
- critical paths and TOPOLOGY (``pcbqa.critical_topology`` and the
  gates): whether specific structures meet their declared policy.

None of these stands in for another.
"""

from __future__ import annotations


class ContractError(Exception):
    """The contract cannot be extracted or compared as declared."""


CONTRACT_KIND = "netlist-contract"
PARITY_KIND = "netlist-parity"


def contract_from_board(board):
    """Extract the pad-to-net contract from a loaded pcbnew board.

    Pads that share a number on one footprint (thermal pads, split
    paddles) are kept as a multiset: the sorted list of their net
    assignments. An unassigned pad records the empty string - its
    absence of a net is part of the intent too.
    """
    footprints = {}
    for footprint in board.GetFootprints():
        reference = footprint.GetReference()
        if not reference:
            raise ContractError(
                "a footprint with an empty reference cannot enter "
                "a contract")
        if reference in footprints:
            raise ContractError(
                "duplicate reference {!r}; parity needs unique "
                "references".format(reference))
        pads = {}
        for pad in footprint.Pads():
            number = pad.GetNumber()
            pads.setdefault(number, []).append(pad.GetNetname())
        footprints[reference] = {
            number: sorted(nets) for number, nets in pads.items()}
    nets = sorted({net
                   for pads in footprints.values()
                   for assigned in pads.values()
                   for net in assigned if net})
    return {
        "kind": CONTRACT_KIND,
        "footprints": footprints,
        "nets": nets,
    }


def _validate(label, contract):
    if not isinstance(contract, dict) or (
            contract.get("kind") != CONTRACT_KIND) or (
            set(contract) != {"kind", "footprints", "nets"}):
        raise ContractError(
            "{} is not a netlist-contract record".format(label))


def required_nets(contract, minimum_pads=2):
    """The required net set, from the CONTRACT alone.

    A net the intent connects to at least ``minimum_pads`` pads
    must eventually be electrically complete; a candidate that
    dropped the net's footprints does not shrink this set.
    """
    _validate("contract", contract)
    counts = {}
    for pads in contract["footprints"].values():
        for assigned in pads.values():
            for net in assigned:
                if net:
                    counts[net] = counts.get(net, 0) + 1
    return sorted(net for net, count in counts.items()
                  if count >= minimum_pads)


def compare(intent, candidate):
    """Judge a candidate's netlist against the authoritative intent.

    Every difference is named machine-readably; ``ok`` is True only
    when there is none. The candidate never contributes to the
    denominator - a missing footprint is a finding, not a smaller
    contract.
    """
    _validate("intent", intent)
    _validate("candidate", candidate)
    intent_fps = intent["footprints"]
    candidate_fps = candidate["footprints"]
    missing_footprints = sorted(set(intent_fps) - set(candidate_fps))
    added_footprints = sorted(set(candidate_fps) - set(intent_fps))
    missing_pads = []
    added_pads = []
    changed = {}
    for reference in sorted(set(intent_fps) & set(candidate_fps)):
        intent_pads = intent_fps[reference]
        candidate_pads = candidate_fps[reference]
        for number in sorted(set(intent_pads)
                             - set(candidate_pads)):
            missing_pads.append("{}.{}".format(reference, number))
        for number in sorted(set(candidate_pads)
                             - set(intent_pads)):
            added_pads.append("{}.{}".format(reference, number))
        for number in sorted(set(intent_pads)
                             & set(candidate_pads)):
            if intent_pads[number] != candidate_pads[number]:
                changed["{}.{}".format(reference, number)] = {
                    "intent": list(intent_pads[number]),
                    "candidate": list(candidate_pads[number]),
                }
    missing_nets = sorted(set(intent["nets"])
                          - set(candidate["nets"]))
    unexpected_nets = sorted(set(candidate["nets"])
                             - set(intent["nets"]))
    ok = not (missing_footprints or added_footprints or missing_pads
              or added_pads or changed or missing_nets
              or unexpected_nets)
    return {
        "kind": PARITY_KIND,
        "ok": ok,
        "missing_footprints": missing_footprints,
        "added_footprints": added_footprints,
        "missing_pads": missing_pads,
        "added_pads": added_pads,
        "changed_assignments": changed,
        "missing_nets": missing_nets,
        "unexpected_nets": unexpected_nets,
        "detail": "candidate pad-to-net assignments judged against "
                  "the authoritative product intent; the candidate "
                  "never defines its own denominator",
    }
