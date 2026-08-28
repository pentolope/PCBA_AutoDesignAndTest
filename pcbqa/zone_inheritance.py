"""Declarative zone-inheritance policy for derived candidate boards.

A candidate derived from an authoritative board must decide, zone by
zone, what it inherits: some copper structures are shared product
architecture (a continuous ground plane), some are functions of the
placement the candidate just abandoned (keepouts guarding pad mask
openings). This module makes that decision EXECUTABLE: the policy
document drives behavior, so changing an accepted policy changes
what happens to the board - or fails validation - and never merely
changes a provenance hash.

Match semantics, deliberately small:

  * ``{"kind": "fill", "net": N, "layers": [...]}`` matches a filled
    copper zone on net N whose copper layers are a subset of the
    listed layers;
  * ``{"kind": "rule_area", "name": S}`` matches a rule area with
    that zone name.

Decisions:

  * ``inherited-architecture`` - the zone stays;
  * ``derived-from-placement`` - the zone stays only where its
    extent still intersects requirement-fixed geometry (the caller
    supplies those boxes); otherwise it is deleted as stale.

A zone no rule matches REFUSES the whole application: an
unclassified copper structure silently inherited is exactly the
fiction this module exists to prevent.
"""

from __future__ import annotations


class ZonePolicyError(Exception):
    """The policy cannot be accepted or applied as declared."""


_DECISIONS = ("inherited-architecture", "derived-from-placement")


def validate_policy(policy):
    """Strict validation of one policy document."""
    if not isinstance(policy, dict):
        raise ZonePolicyError("a zone policy must be a dict")
    if policy.get("kind") != "candidate-zone-inheritance-policy":
        raise ZonePolicyError(
            "policy kind {!r} is not "
            "candidate-zone-inheritance-policy".format(
                policy.get("kind")))
    if not isinstance(policy.get("policy_version"), str) \
            or not policy["policy_version"]:
        raise ZonePolicyError("the policy needs a policy_version")
    unmatched = policy.get("unmatched_zone_policy")
    if not isinstance(unmatched, str) \
            or not unmatched.startswith("refuse"):
        raise ZonePolicyError(
            "unmatched_zone_policy must state refusal; a policy "
            "that silently inherits unclassified zones is not "
            "accepted")
    rules = policy.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ZonePolicyError("the policy needs a nonempty rules "
                              "list")
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ZonePolicyError("rule #{} must be a dict".format(
                index))
        match = rule.get("match")
        if not isinstance(match, dict):
            raise ZonePolicyError(
                "rule #{} needs a match dict".format(index))
        kind = match.get("kind")
        if kind == "fill":
            if not match.get("net") or \
                    not isinstance(match.get("layers"), list) \
                    or not match["layers"]:
                raise ZonePolicyError(
                    "rule #{}: a fill match needs net and "
                    "layers".format(index))
        elif kind == "rule_area":
            if not match.get("name"):
                raise ZonePolicyError(
                    "rule #{}: a rule_area match needs a "
                    "name".format(index))
        else:
            raise ZonePolicyError(
                "rule #{}: match kind {!r} is not fill or "
                "rule_area".format(index, kind))
        if rule.get("decision") not in _DECISIONS:
            raise ZonePolicyError(
                "rule #{}: decision {!r} is not one of {}".format(
                    index, rule.get("decision"), list(_DECISIONS)))
    return policy


def _zone_layers(board, zone):
    import pcbnew
    return sorted(board.GetLayerName(layer)
                  for layer in zone.GetLayerSet().Seq()
                  if pcbnew.IsCopperLayer(layer))


def _intersects_any(box, boxes):
    for other in boxes:
        if box[0] < other[2] and other[0] < box[2] \
                and box[1] < other[3] and other[1] < box[3]:
            return True
    return False


def apply_policy(board, policy, fixed_boxes_mm):
    """Apply one validated policy to one board's zones.

    ``fixed_boxes_mm`` is the list of [minx, miny, maxx, maxy] boxes
    of requirement-fixed geometry, used by derived-from-placement
    decisions. Returns a machine-readable outcome; refuses on the
    first zone no rule matches.
    """
    import pcbnew
    validate_policy(policy)
    outcome = {"policy_version": policy["policy_version"],
               "kept": 0, "deleted": 0,
               "per_rule": [{"decision": rule["decision"],
                             "matched": 0, "kept": 0, "deleted": 0}
                            for rule in policy["rules"]]}
    for zone in list(board.Zones()):
        matched_rule = None
        for index, rule in enumerate(policy["rules"]):
            match = rule["match"]
            if zone.GetIsRuleArea():
                if match["kind"] == "rule_area" and \
                        zone.GetZoneName() == match["name"]:
                    matched_rule = index
                    break
            else:
                if match["kind"] == "fill" and \
                        zone.GetNetname() == match["net"] and \
                        set(_zone_layers(board, zone)) <= \
                        set(match["layers"]):
                    matched_rule = index
                    break
        if matched_rule is None:
            raise ZonePolicyError(
                "zone {!r} (net {!r}, rule_area={}) matches no "
                "policy rule; an unclassified zone refuses instead "
                "of being inherited by silence".format(
                    zone.GetZoneName(), zone.GetNetname(),
                    zone.GetIsRuleArea()))
        rule = policy["rules"][matched_rule]
        record = outcome["per_rule"][matched_rule]
        record["matched"] += 1
        if rule["decision"] == "inherited-architecture":
            record["kept"] += 1
            outcome["kept"] += 1
            continue
        box = zone.GetBoundingBox()
        zone_box = [pcbnew.ToMM(box.GetLeft()),
                    pcbnew.ToMM(box.GetTop()),
                    pcbnew.ToMM(box.GetRight()),
                    pcbnew.ToMM(box.GetBottom())]
        if _intersects_any(zone_box, fixed_boxes_mm):
            record["kept"] += 1
            outcome["kept"] += 1
        else:
            board.Delete(zone)
            record["deleted"] += 1
            outcome["deleted"] += 1
    return outcome
