"""The netlist contract: parity against authoritative intent, and a
denominator the candidate can never shrink."""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pcbnew                                       # noqa: E402
from pcbqa import netlist_contract                  # noqa: E402
from pcbqa.netlist_contract import ContractError    # noqa: E402
from tests import synth                             # noqa: E402


def _intent_board():
    """Two two-pad parts and one single-pad part over three nets."""
    board = synth.new_board(layers=2, size_mm=30.0)
    alpha = synth.add_net(board, "ALPHA")
    beta = synth.add_net(board, "BETA")
    gamma = synth.add_net(board, "GAMMA")
    synth.add_two_pad_footprint(board, "R1", 8.0, 10.0, 2.0,
                                (alpha, beta))
    synth.add_two_pad_footprint(board, "R2", 14.0, 10.0, 2.0,
                                (beta, gamma))
    synth.add_pad_footprint(board, "TP1", 11.0, 14.0,
                            pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                            net=alpha)
    return board


class TheContractComesFromTheIntent(unittest.TestCase):

    def test_extraction_records_every_assignment(self):
        contract = netlist_contract.contract_from_board(
            _intent_board())
        self.assertEqual(contract["kind"], "netlist-contract")
        self.assertEqual(sorted(contract["footprints"]),
                         ["R1", "R2", "TP1"])
        self.assertEqual(contract["footprints"]["R1"],
                         {"1": ["ALPHA"], "2": ["BETA"]})
        self.assertEqual(contract["nets"],
                         ["ALPHA", "BETA", "GAMMA"])

    def test_duplicate_references_refuse(self):
        board = _intent_board()
        alpha = None
        for name, net in board.GetNetsByName().items():
            if str(name) == "ALPHA":
                alpha = net
        synth.add_pad_footprint(board, "R1", 20.0, 20.0,
                                pcbnew.PAD_SHAPE_RECT, (1.0, 1.0),
                                net=alpha)
        with self.assertRaises(ContractError):
            netlist_contract.contract_from_board(board)

    def test_the_denominator_is_the_contracts_alone(self):
        """required_nets reads the CONTRACT: a candidate that
        dropped a footprint would compute a smaller set from its
        own remains, which is exactly why it never gets to."""
        intent = netlist_contract.contract_from_board(
            _intent_board())
        # ALPHA has 2 pads (R1.1, TP1.1), BETA has 2 (R1.2, R2.1),
        # GAMMA has 1 (R2.2): the default two-pad minimum keeps the
        # nets that need copper between pads.
        self.assertEqual(netlist_contract.required_nets(intent),
                         ["ALPHA", "BETA"])
        self.assertEqual(
            netlist_contract.required_nets(intent, minimum_pads=1),
            ["ALPHA", "BETA", "GAMMA"])
        # A candidate missing TP1 would - judged by its own netlist
        # - demote ALPHA to a single-pad net and shrink its own
        # denominator to BETA alone.
        candidate_board = _intent_board()
        for footprint in list(candidate_board.GetFootprints()):
            if footprint.GetReference() == "TP1":
                candidate_board.Delete(footprint)
        shrunken = netlist_contract.contract_from_board(
            candidate_board)
        self.assertEqual(netlist_contract.required_nets(shrunken),
                         ["BETA"])


class ParityFindsEveryDifference(unittest.TestCase):

    def _contracts(self, mutate):
        intent_board = _intent_board()
        candidate_board = _intent_board()
        mutate(candidate_board)
        return (netlist_contract.contract_from_board(intent_board),
                netlist_contract.contract_from_board(
                    candidate_board))

    def test_identical_boards_pass(self):
        intent, candidate = self._contracts(lambda board: None)
        verdict = netlist_contract.compare(intent, candidate)
        self.assertIs(verdict["ok"], True)

    def test_a_missing_footprint_is_named(self):
        def drop_tp1(board):
            for footprint in list(board.GetFootprints()):
                if footprint.GetReference() == "TP1":
                    board.Delete(footprint)
        intent, candidate = self._contracts(drop_tp1)
        verdict = netlist_contract.compare(intent, candidate)
        self.assertIs(verdict["ok"], False)
        self.assertEqual(verdict["missing_footprints"], ["TP1"])

    def test_a_changed_assignment_is_named(self):
        def swap_r2_pad2(board):
            beta = None
            for name, net in board.GetNetsByName().items():
                if str(name) == "BETA":
                    beta = net
            for footprint in board.GetFootprints():
                if footprint.GetReference() != "R2":
                    continue
                for pad in footprint.Pads():
                    if pad.GetNumber() == "2":
                        pad.SetNet(beta)
        intent, candidate = self._contracts(swap_r2_pad2)
        verdict = netlist_contract.compare(intent, candidate)
        self.assertIs(verdict["ok"], False)
        self.assertEqual(
            verdict["changed_assignments"],
            {"R2.2": {"intent": ["GAMMA"],
                      "candidate": ["BETA"]}})
        # GAMMA vanished from the candidate entirely - named too.
        self.assertEqual(verdict["missing_nets"], ["GAMMA"])

    def test_an_unexpected_net_is_named(self):
        def invent_net(board):
            rogue = synth.add_net(board, "ROGUE")
            for footprint in board.GetFootprints():
                if footprint.GetReference() != "TP1":
                    continue
                for pad in footprint.Pads():
                    pad.SetNet(rogue)
        intent, candidate = self._contracts(invent_net)
        verdict = netlist_contract.compare(intent, candidate)
        self.assertIs(verdict["ok"], False)
        self.assertEqual(verdict["unexpected_nets"], ["ROGUE"])
        self.assertIn("TP1.1", verdict["changed_assignments"])

    def test_a_missing_pad_is_named(self):
        def drop_pad(board):
            for footprint in board.GetFootprints():
                if footprint.GetReference() != "R1":
                    continue
                for pad in list(footprint.Pads()):
                    if pad.GetNumber() == "2":
                        footprint.Delete(pad)
        intent, candidate = self._contracts(drop_pad)
        verdict = netlist_contract.compare(intent, candidate)
        self.assertIs(verdict["ok"], False)
        self.assertEqual(verdict["missing_pads"], ["R1.2"])

    def test_non_contract_inputs_refuse(self):
        intent = netlist_contract.contract_from_board(
            _intent_board())
        with self.assertRaises(ContractError):
            netlist_contract.compare(intent, {"kind": "other"})
        with self.assertRaises(ContractError):
            netlist_contract.required_nets({"nets": []})


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
