"""JLCPCB knowledge: acquisition, comparison, selection.

The committed catalog is the only thing design work may trust, and a Git
commit is what makes it committed. A refresh fetches into scratch and shows a
semantic diff; review replaces the catalog/evidence set exactly before commit.

So the load path is where the refusals live. A catalog whose evidence is
missing, altered, or describes an acquisition that never completed must refuse
to load rather than reach a caller with values it cannot prove.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa.fabricators import acquire, diff, jlcpcb, model, selection  # noqa: E402
from pcbqa.fabricators import store as store_module                     # noqa: E402
from pcbqa.fabricators.store import CatalogStore, StoreError            # noqa: E402
from tests import paths                                                 # noqa: E402

FIXTURES = os.path.join(paths.FIXTURES, "fabricators", "jlcpcb")


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def _raw_sources():
    return {"impedance": _fixture_bytes("impedance.fixture.html"),
            "capabilities": _fixture_bytes("capabilities.fixture.html"),
            "copper-weight": _fixture_bytes("copper-weight.fixture.html"),
            "impedance-calculator":
                _fixture_bytes("impedance-calculator.fixture.html"),
            "thickness-options":
                _fixture_bytes("thickness-options.fixture.html")}


def _fetcher(raw=None, fail=()):
    """A fake network: serves fixture bytes, fails where told to."""
    raw = raw if raw is not None else _raw_sources()
    by_url = {spec["url"]: raw.get(spec["id"]) for spec in jlcpcb.SOURCES}

    def fetch(url):
        for spec in jlcpcb.SOURCES:
            if spec["url"] == url and spec["id"] in fail:
                raise OSError("synthetic network failure")
        data = by_url.get(url)
        if data is None:
            raise OSError("no fixture for {}".format(url))
        return data
    return fetch


_SCRATCH = []


def _root(tag):
    path = tempfile.mkdtemp(prefix="pcbqa_fab_" + tag + "_")
    _SCRATCH.append(path)
    return path


def tearDownModule():
    for path in _SCRATCH:
        shutil.rmtree(path, ignore_errors=True)
    del _SCRATCH[:]


def _approved_store(tag):
    """A store whose catalog is committed, the way a repository holds one."""
    root = _root(tag)
    result, problem = acquire.acquire(fetcher=_fetcher())
    assert problem is None, problem
    store_module.write_catalog(root, result, result["raw"])
    return CatalogStore(root)


def _two_layer_requirements():
    """A board the fabricator publishes no construction for."""
    return {"copper_layers": 2, "board_thickness_mm": 1.6,
            "outer_copper_oz": 1, "material": "FR4",
            "impedance_control": False,
            "min_track_mm": 0.2, "min_space_mm": 0.15,
            "min_drill_mm": 0.3, "min_via_diameter_mm": 0.6}


def _rewrite_json(path, mutate):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    mutate(document)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    return document


# ---------------------------------------------------------------------------
# the parser, against real published content
# ---------------------------------------------------------------------------

class TheParserReadsWhatJlcpcbPublishes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    def test_the_stated_dielectric_constants_are_read(self):
        materials = self.catalog["materials"]
        self.assertEqual(materials["prepreg 7628"]["dk"], 4.4)
        self.assertEqual(materials["prepreg 3313"]["dk"], 4.1)
        self.assertEqual(materials["prepreg 1080"]["dk"], 3.91)
        self.assertEqual(materials["core"]["dk"], 4.6)

    def test_the_two_layer_dielectric_constant_is_read_with_its_scope(self):
        """The impedance page states dielectric constants for the builds it
        publishes, which start at four layers. The capabilities page is the
        only place a two-layer board's is stated, and it is kept scoped to
        two layers so nothing else can consume it."""
        materials = self.catalog["materials"]
        two_layer = materials["core 2-layer (capabilities)"]
        self.assertEqual(two_layer["dk"], 4.5)
        self.assertEqual(two_layer["applies"],
                         {"min_layers": 2, "max_layers": 2})
        self.assertEqual(two_layer["excerpt"], "4.5 (2-Layer PCB)")
        self.assertEqual(materials["core"]["dk"], 4.6)

    def test_the_two_pages_dielectric_constants_stay_distinct(self):
        """Both pages state a value for 7628. They agree today; keeping them
        as separate records is what makes a future disagreement visible
        instead of letting one overwrite the other."""
        materials = self.catalog["materials"]
        self.assertEqual(materials["prepreg 7628"]["source"], "impedance")
        self.assertEqual(
            materials["prepreg 7628 (capabilities)"]["source"],
            "capabilities")
        self.assertEqual(materials["prepreg 7628"]["dk"],
                         materials["prepreg 7628 (capabilities)"]["dk"])

    def test_a_missing_dielectric_block_is_recorded_not_guessed(self):
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"FR-4 Dielectric Constants", b"FR-4 Dielectric Values")
        catalog = jlcpcb.parse(raw)
        self.assertNotIn("core 2-layer (capabilities)",
                         catalog["materials"])
        self.assertTrue(any(
            record["field"] == "FR-4 dielectric constants"
            for record in catalog["not_extracted"]))

    def test_losing_the_board_class_value_is_recorded_not_silent(self):
        """The block can lose its board-class line while still yielding
        prepregs. Reading three of four and reporting nothing would leave a
        composed construction refusing with no record of what went
        missing."""
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"<div>4.5 (2-Layer PCB)</div>\n", b"")
        catalog = jlcpcb.parse(raw)
        self.assertNotIn("core 2-layer (capabilities)", catalog["materials"])
        self.assertTrue(any(
            "no per-board-class" in record["reason"]
            for record in catalog["not_extracted"]))

    def test_a_dk_shaped_line_that_will_not_parse_is_recorded(self):
        """Stopping at the next feature row is how the block ends. Stopping
        at a line still written as a dielectric constant is not an ending,
        it is a reading this parser can no longer perform."""
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"7628 Prepreg 4.4", b"7628 Prepreg 4.4.1")
        catalog = jlcpcb.parse(raw)
        self.assertTrue(any(
            "matches neither published form" in record["reason"]
            for record in catalog["not_extracted"]))

    def test_the_block_scan_stops_at_the_block(self):
        """The parse is anchored to one table row. A scan that ran past it
        would collect anything page-shaped from the rest of the document -
        a value stated about some other product, read as this board's."""
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"<div>Track width tolerance</div>",
            b"<div>9.9 (2-Layer PCB)</div>\n"
            b"<div>1080 Prepreg 9.8</div>\n"
            b"<div>Track width tolerance</div>")
        catalog = jlcpcb.parse(raw)
        self.assertEqual(
            catalog["materials"]["core 2-layer (capabilities)"]["dk"], 4.5)
        self.assertNotIn("prepreg 1080 (capabilities)",
                         catalog["materials"])

    def test_a_malformed_number_refuses_through_the_error_contract(self):
        """float() on a loose numeric pattern raises ValueError, which
        acquisition does not catch, so a restyled page would traceback
        instead of reporting a failed parse."""
        for bad in (b"4.5.1 (2-Layer PCB)", b". (2-Layer PCB)"):
            raw = _raw_sources()
            raw["capabilities"] = raw["capabilities"].replace(
                b"4.5 (2-Layer PCB)", bad)
            catalog = jlcpcb.parse(raw)
            self.assertNotIn("core 2-layer (capabilities)",
                             catalog["materials"], bad)

    def test_a_permittivity_at_or_below_vacuum_is_not_a_material(self):
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"4.5 (2-Layer PCB)", b"0 (2-Layer PCB)")
        catalog = jlcpcb.parse(raw)
        self.assertNotIn("core 2-layer (capabilities)", catalog["materials"])
        self.assertTrue(any("not above vacuum" in record["reason"]
                            for record in catalog["not_extracted"]))

    def test_the_default_stackup_is_evidence_not_invention(self):
        stackup = self.catalog["stackups"]["JLC-4L-no-requirement"]
        self.assertTrue(stackup["default_when_no_impedance_requirement"])
        roles = [(l["role"], l.get("form")) for l in stackup["layers"]]
        self.assertEqual(roles, [("copper", None), ("dielectric", "prepreg"),
                                 ("copper", None), ("dielectric", "core"),
                                 ("copper", None), ("dielectric", "prepreg"),
                                 ("copper", None)])
        self.assertEqual(model.stackup_total_mm(stackup), 1.5862)

    def test_applicability_states_the_build_a_construction_describes(self):
        """Layer count, nominal thickness, outer and inner copper: tied to
        the construction from its own table, the fabricator's stated
        equivalence, and its own cladding notation - each with the basis
        recorded so a reviewer can weigh interpretation against verbatim."""
        for identity in ("JLC-4L-no-requirement", "JLC04161H-7628",
                         "JLC04161H-3313", "JLC04161H-1080"):
            applicability = self.catalog["stackups"][identity][
                "applicability"]
            self.assertEqual(applicability["outer_copper_thickness_mm"],
                             0.035, identity)
            self.assertEqual(applicability["outer_copper_weight_oz"], 1.0)
            self.assertEqual(applicability["inner_copper_weight_oz"], 0.5)
            self.assertEqual(applicability["nominal_thickness_mm"], 1.6)
            self.assertIn("basis", applicability["outer_basis"]
                          .replace("equivalence", "basis"))
        named = self.catalog["stackups"]["JLC04161H-7628"]["applicability"]
        self.assertIn("name-encoded", named["thickness_basis"])
        default = self.catalog["stackups"]["JLC-4L-no-requirement"][
            "applicability"]
        self.assertIn("layer sum", default["thickness_basis"])

    def test_a_name_contradicting_its_own_table_refuses(self):
        """The name-encoding is an interpretation; it survives only while
        the construction's own numbers corroborate it."""
        raw = _raw_sources()
        marker = raw["impedance"].find(b"2) JLC04161H-7628")
        head, tail = raw["impedance"][:marker], raw["impedance"][marker:]
        raw["impedance"] = head + tail.replace(b">1.065mm<",
                                               b">0.465mm<", 1)
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("naming interpretation", str(caught.exception))

    def test_the_oz_bridge_is_the_fabricators_own_statement(self):
        record = self.catalog["capabilities"][
            "copper_weight_equivalence_um_per_oz"]
        self.assertEqual(record["value"], 35.0)
        self.assertEqual(record["source"], "copper-weight")
        self.assertIn("35", record["excerpt"])

    def test_without_the_bridge_no_weight_is_derived(self):
        raw = _raw_sources()
        raw["copper-weight"] = raw["copper-weight"].replace(
            b"1oz copper = copper thickness of 35", b"redacted", 1)
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("equivalence is gone", str(caught.exception))

    def test_named_impedance_stackups_stay_distinct_from_the_default(self):
        """The 7628 construction and the default are byte-identical in their
        layers on the page. They are still two published options, and the
        catalog must not collapse them."""
        default = self.catalog["stackups"]["JLC-4L-no-requirement"]
        named = self.catalog["stackups"]["JLC04161H-7628"]
        self.assertEqual(
            [l for l in default["layers"]], [l for l in named["layers"]])
        self.assertFalse(named["default_when_no_impedance_requirement"])

    def test_conditioned_trace_limits_are_read_with_their_scope(self):
        """Both sources' trace/space tables, one record per (weight, layer
        class) clause, machine-readable scope attached."""
        capabilities = self.catalog["capabilities"]
        one_oz_multi = capabilities[
            "trace_space 1.0oz multilayer (capabilities)"]
        self.assertEqual(one_oz_multi["value"],
                         {"track": 0.09, "space": 0.09})
        self.assertEqual(one_oz_multi["applies"]["min_layers"], 4)
        half_or_one = capabilities[
            "trace_space 0.5-1.0oz >=4-layers-fr4 (copper-weight)"]
        self.assertEqual(half_or_one["value"]["track"], 0.09)
        self.assertEqual(half_or_one["applies"]["copper_weights_oz"],
                         [0.5, 1.0])
        two_oz = capabilities["trace_space 2.0oz multilayer (capabilities)"]
        self.assertEqual(two_oz["value"]["track"], 0.15)
        heavy = capabilities["trace_space 4.5oz 2-layer (capabilities)"]
        self.assertEqual(heavy["applies"]["max_layers"], 2)

    def test_trace_coils_are_special_not_general(self):
        """The trace-coil limits exist in the catalog - and only under
        their own category, where no general-routing lookup will find
        them."""
        capabilities = self.catalog["capabilities"]
        coils = capabilities["trace_coils_masked_1oz"]
        self.assertEqual(coils["value"], {"track": 0.15, "space": 0.15})
        self.assertEqual(coils["category"], "trace-coils")
        self.assertEqual(selection._trace_limits(
            {"coil": coils}, 1.0, 4), [])

    def test_fpc_rows_are_declared_out_of_scope_not_dropped(self):
        skipped = [entry for entry in self.catalog["not_extracted"]
                   if "FPC" in entry.get("field", "")]
        self.assertEqual(len(skipped), 3)
        for entry in skipped:
            self.assertIn("scope", entry["reason"])

    def test_capability_records_carry_their_evidence(self):
        for identity, record in self.catalog["capabilities"].items():
            self.assertTrue(record.get("source"), identity)
            self.assertTrue(record.get("excerpt"), identity)

    def test_missing_source_refuses(self):
        raw = _raw_sources()
        del raw["copper-weight"]
        with self.assertRaises(model.CatalogError):
            jlcpcb.parse(raw)

    def test_an_unexpected_unit_refuses(self):
        raw = _raw_sources()
        raw["impedance"] = raw["impedance"].replace(b"0.21040mm",
                                                    b"8.28mil", 1)
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("unexpected unit", str(caught.exception))

    def test_a_gutted_page_refuses_rather_than_shrinking_the_offer(self):
        raw = _raw_sources()
        cut = raw["impedance"].find(b"2) JLC04161H-7628")
        raw["impedance"] = raw["impedance"][:cut] + b"</li></ul></div>"
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("implausibly few", str(caught.exception))

    def test_a_restructured_capabilities_page_refuses(self):
        raw = _raw_sources()
        raw["capabilities"] = b"<html><div>totally new layout</div></html>"
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("restructured", str(caught.exception))

    def test_a_gutted_traces_table_refuses(self):
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"Min. track width and spacing", b"redacted feature")
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("trace/space", str(caught.exception))


# ---------------------------------------------------------------------------
# presentation changes versus fabrication changes
# ---------------------------------------------------------------------------

class PresentationIsNotFabrication(unittest.TestCase):

    def test_a_restyled_page_changes_nothing_semantic(self):
        """New class hashes, renamed color classes, extra whitespace: the raw
        hash moves, the normalized digest must not."""
        raw = _raw_sources()
        original = jlcpcb.parse(raw)
        restyled = dict(raw)
        page = raw["impedance"]
        page = page.replace(b"data-v-b8c29c54", b"data-v-ffffffff")
        page = page.replace(b"bg-yellow", b"bg-gold")
        page = page.replace(b"<div class=", b"<div  class=")
        self.assertNotEqual(page, raw["impedance"])
        restyled["impedance"] = page
        reparsed = jlcpcb.parse(restyled)
        self.assertEqual(model.normalized_digest(original),
                         model.normalized_digest(reparsed))
        self.assertEqual(diff.semantic_diff(original, reparsed), [])

    def test_one_dielectric_thickness_change_is_one_reviewable_change(self):
        raw = _raw_sources()
        original = jlcpcb.parse(raw)
        edited = dict(raw)
        edited["impedance"] = raw["impedance"].replace(
            b">1.065mm<", b">1.075mm<", 1)
        changed = jlcpcb.parse(edited)
        changes = diff.semantic_diff(original, changed)
        self.assertEqual(len(changes), 1, changes)
        change = changes[0]
        self.assertEqual(change["kind"], "stackup-changed")
        self.assertIn("thickness_mm", change["field"])
        self.assertEqual(change["approved"], 1.065)
        self.assertEqual(change["observed"], 1.075)
        # And the rendering is the engineer's view, naming both values.
        text = diff.render(changes)
        self.assertIn("approved: 1.065", text)
        self.assertIn("observed: 1.075", text)

    def test_a_changed_dk_requests_review(self):
        original = jlcpcb.parse(_raw_sources())
        observed = copy.deepcopy(original)
        observed["materials"]["prepreg 7628"]["dk"] = 4.35
        changes = diff.semantic_diff(original, observed)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "material-changed")
        self.assertEqual(changes[0]["approved"], 4.4)

    def test_added_and_removed_options_are_reported_as_such(self):
        original = jlcpcb.parse(_raw_sources())
        observed = copy.deepcopy(original)
        removed = observed["stackups"].pop("JLC04161H-1080")
        observed["stackups"]["JLC04-NEW"] = dict(
            copy.deepcopy(removed),
            layers=removed["layers"][:-1] + [model.stackup_layer(
                model.COPPER, 0.07, label="Bottom Layer")])
        kinds = sorted(c["kind"] for c in
                       diff.semantic_diff(original, observed))
        self.assertEqual(kinds, ["stackup-added", "stackup-removed"])

    def test_a_pure_rename_is_reported_as_a_rename(self):
        """Same construction, new identity: maybe a real process change,
        maybe housekeeping - the reviewer decides, the diff must not."""
        original = jlcpcb.parse(_raw_sources())
        observed = copy.deepcopy(original)
        observed["stackups"]["JLC04161H-1080-RENAMED"] = \
            observed["stackups"].pop("JLC04161H-1080")
        changes = diff.semantic_diff(original, observed)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "stackup-renamed")
        self.assertIn("JLC04161H-1080", changes[0]["subject"])

    def test_an_excerpt_rewording_alone_is_not_a_semantic_change(self):
        original = jlcpcb.parse(_raw_sources())
        observed = copy.deepcopy(original)
        observed["capabilities"]["layer_count_range"]["excerpt"] = \
            "reworded presentation"
        observed["stackups"]["JLC04161H-7628"]["applicability"][
            "thickness_basis"] = "reworded provenance prose"
        self.assertEqual(diff.semantic_diff(original, observed), [])


# ---------------------------------------------------------------------------
# selection: one coherent configuration or an honest refusal
# ---------------------------------------------------------------------------

def _requirements(**overrides):
    base = {"copper_layers": 4, "board_thickness_mm": 1.6,
            "min_track_mm": 0.15, "min_space_mm": 0.15,
            "min_drill_mm": 0.3, "min_via_diameter_mm": 0.45,
            "outer_copper_oz": 1.0, "inner_copper_oz": 0.5,
            "impedance_control": False}
    base.update(overrides)
    return base


class SelectionIsRequirementDriven(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    def test_a_standard_board_selects_the_fabricators_own_default(self):
        result = selection.select(self.catalog, _requirements())
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertEqual(result["stackup"], "JLC-4L-no-requirement")
        self.assertEqual(result["profile"]["inner_copper_oz"], 0.5)
        self.assertEqual(result["profile"]["outer_copper_oz"], 1.0)

    def test_an_incapable_geometry_is_rejected_on_conditioned_evidence(
            self):
        result = selection.select(self.catalog,
                                  _requirements(min_track_mm=0.08))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "min_track_mm"][0]
        self.assertIn("0.09", rejection["issue"])
        self.assertIn("1 oz outer", rejection["issue"])

    def test_general_limits_are_not_the_trace_coil_limits(self):
        """0.12 mm sits below the 0.15 trace-coil figure and above the
        0.09/0.10 general 1 oz limits. Only the general limits may
        decide."""
        result = selection.select(self.catalog,
                                  _requirements(min_track_mm=0.12,
                                                min_space_mm=0.12))
        self.assertTrue(result["feasible"], result["rejections"])
        cited = " ".join(result["explanations"])
        self.assertNotIn("coil", cited)

    def test_two_oz_geometry_is_judged_on_two_oz_evidence(self):
        result = selection.select(
            self.catalog, _requirements(outer_copper_oz=2.0))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "min_track_mm"][0]
        # The strictest published 2 oz figure (0.16, copper-weight guide)
        # governs - not the 1 oz figure and not the looser of the two
        # sources.
        self.assertIn("0.16", rejection["issue"])
        self.assertIn("2 oz outer", rejection["issue"])

    def test_required_one_oz_inner_is_unknown_not_orderable(self):
        """The fabricator lists 1 oz inner copper - and states in the same
        breath that inner availability depends on unpublished factors. A
        list the fabricator itself qualifies proves nothing about this
        build, so the profile is unknown and unknown is not feasible."""
        result = selection.select(self.catalog,
                                  _requirements(inner_copper_oz=1.0))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "inner_copper_oz"][0]
        self.assertIn("conditional", rejection["issue"])

    def test_required_two_oz_outer_cannot_select_a_one_oz_construction(
            self):
        result = selection.select(
            self.catalog, _requirements(outer_copper_oz=2.0,
                                        min_track_mm=0.2, min_space_mm=0.2))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertEqual(result["stackup_candidates"], [])

    def test_thickness_applicability_is_evidence_not_layer_sum(self):
        """A construction whose layers happen to sum near a requested
        thickness is still not a candidate unless its applicability says
        that is the build it describes."""
        catalog = copy.deepcopy(self.catalog)
        stackup = catalog["stackups"]["JLC04161H-7628"]
        # Forge a physically-thinner variant whose sum is ~1.0 mm but whose
        # reviewed applicability still names 1.6 mm.
        for layer in stackup["layers"]:
            if layer.get("form") == model.CORE:
                layer["thickness_mm"] = 0.5
        result = selection.select(catalog,
                                  _requirements(board_thickness_mm=1.0,
                                                impedance_control=True))
        self.assertNotIn("JLC04161H-7628", result["stackup_candidates"])

    def test_unknown_capability_never_becomes_supported(self):
        catalog = copy.deepcopy(self.catalog)
        del catalog["capabilities"]["drill_diameter multilayer "
                                    "(capabilities)"]
        result = selection.select(catalog, _requirements())
        self.assertFalse(result["feasible"])
        self.assertTrue(any("unknown is not supported" in r["issue"]
                            for r in result["rejections"]))

    def test_unknown_copper_conditioned_geometry_stays_unknown(self):
        """2.5 oz outer copper on a 4-layer board: the weight exists in the
        catalog (2-layer rows), but no published limit covers it at 4
        layers - and no nearby rule is borrowed."""
        catalog = copy.deepcopy(self.catalog)
        options = catalog["capabilities"]["4L outer_copper_options"]
        options["value"] = options["value"] + [2.5]
        result = selection.select(catalog,
                                  _requirements(outer_copper_oz=2.5))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if "trace/space" in r["issue"]
                     or "min_track" in r["requirement"]][0]
        self.assertIn("no published trace/space limit covers 2.5 oz",
                      rejection["issue"])

    def test_a_non_standard_thickness_is_rejected_on_the_stated_options(
            self):
        result = selection.select(self.catalog,
                                  _requirements(board_thickness_mm=1.4))
        self.assertFalse(result["feasible"])
        self.assertTrue(any("1.4" in r["issue"]
                            for r in result["rejections"]))

    def test_impedance_requirement_preserves_the_candidate_ambiguity(self):
        result = selection.select(self.catalog,
                                  _requirements(impedance_control=True))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertEqual(result["stackup_candidates"],
                         sorted(result["stackup_candidates"]))
        self.assertGreater(len(result["stackup_candidates"]), 1)

    def test_selection_ordering_is_deterministic(self):
        first = selection.select(self.catalog,
                                 _requirements(impedance_control=True))
        second = selection.select(
            copy.deepcopy(self.catalog),
            _requirements(impedance_control=True))
        self.assertEqual(first["stackup_candidates"],
                         second["stackup_candidates"])

    def test_an_unrecognised_requirement_key_refuses(self):
        with self.assertRaises(selection.SelectionError):
            selection.select(self.catalog,
                             _requirements(minimum_trace_mm=0.2))

    def test_the_vocabulary_boundary_is_stated_not_implied(self):
        result = selection.select(self.catalog, _requirements())
        self.assertIn("surface finish", result["not_in_vocabulary"])
        self.assertIn("castellated edges", result["not_in_vocabulary"])
        self.assertIn("board_thickness_mm", result["requirements_checked"])
        self.assertIn("NOT inspected", result["vocabulary_note"])

    def test_an_unpublished_thickness_yields_no_stackup_claim(self):
        result = selection.select(self.catalog,
                                  _requirements(board_thickness_mm=0.8))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertTrue(any("nothing is scaled or invented" in e
                            for e in result["explanations"]))

    def test_a_synthetic_same_shape_different_copper_pair_cannot_merge(
            self):
        """Two stackups, same layer count and thickness, different copper:
        the requirement satisfied by neither exact build gets neither."""
        catalog = copy.deepcopy(self.catalog)
        variant = copy.deepcopy(catalog["stackups"]["JLC-4L-no-requirement"])
        variant["name"] = "SYN-2oz-outer"
        variant["applicability"]["outer_copper_weight_oz"] = 2.0
        catalog["stackups"]["SYN-2oz-outer"] = variant
        # Requirements: 1 oz outer but 1 oz inner - the first stackup
        # matches outer only, the synthetic matches neither.
        result = selection.select(catalog,
                                  _requirements(inner_copper_oz=1.0))
        self.assertEqual(result["stackup_candidates"], [])


# ---------------------------------------------------------------------------
# feeding the board: provenance survives into the supplement
# ---------------------------------------------------------------------------

class ExportedStackupsCarryTheirProvenance(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.store = _approved_store("export")
        cls.approved = cls.store.approved()

    def test_the_export_is_a_valid_supplement_with_provenance(self):
        from pcbqa import stackup_physical
        document = selection.export_physical_stackup(
            self.approved, _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertIn(self.approved["normalized_sha256"][:12],
                      document["provenance"])
        declared = stackup_physical.from_declaration(document)
        self.assertEqual(declared.copper_layer_names,
                         ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(declared.layer("dielectric 1").epsilon_r, 4.4)
        self.assertEqual(declared.layer("dielectric 2").epsilon_r, 4.6)
        self.assertEqual(declared.layer("dielectric 1").thickness_mm,
                         0.2104)

    def test_the_evidence_chain_reaches_the_raw_bytes(self):
        document = selection.export_physical_stackup(
            self.approved, _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        sources = {s["id"]: s for s in
                   document["generated_from"]["sources"]}
        self.assertTrue(sources["impedance"]["sha256_raw"])
        self.assertTrue(sources["impedance-calculator"]["sha256_raw"])
        self.assertEqual(
            document["generated_from"]["approved_normalized_sha256"],
            self.approved["normalized_sha256"])

    def test_a_layer_count_mismatch_refuses(self):
        with self.assertRaises(selection.SelectionError):
            selection.export_physical_stackup(
                self.approved, _requirements(), ["F.Cu", "B.Cu"])

    def test_export_cannot_bypass_profile_compatibility(self):
        """Two bypass routes, both closed: an infeasible profile refuses
        outright however plausible the named construction, and a feasible
        profile with no matching construction refuses a construction id
        published for a different build."""
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                self.approved, _requirements(inner_copper_oz=1.0),
                ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                stackup_id="JLC-4L-no-requirement")
        self.assertIn("feasible", str(caught.exception))
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                self.approved, _requirements(board_thickness_mm=0.8),
                ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                stackup_id="JLC-4L-no-requirement")
        self.assertIn("did not publish", str(caught.exception))

    def test_export_refuses_an_infeasible_profile_outright(self):
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                self.approved, _requirements(copper_layers=3),
                ["F.Cu", "In1.Cu", "B.Cu"])
        self.assertIn("feasible", str(caught.exception))

    def test_export_resolves_ambiguity_only_to_a_real_candidate(self):
        requirements = _requirements(impedance_control=True)
        result = selection.select(self.approved["normalized"], requirements)
        self.assertGreater(len(result["stackup_candidates"]), 1)
        with self.assertRaises(selection.SelectionError):
            selection.export_physical_stackup(
                self.approved, requirements,
                ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        document = selection.export_physical_stackup(
            self.approved, requirements,
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
            stackup_id=result["stackup_candidates"][0])
        self.assertEqual(document["generated_from"]["stackup"],
                         result["stackup_candidates"][0])

    def test_a_two_layer_board_gets_the_construction_it_can_only_have(self):
        """The fabricator publishes no two-layer stackup, so one is composed
        from what it does state: the finished thickness, the outer copper
        weight, the ounce equivalence, and the dielectric constant stated
        for a two-layer board. Nothing about the composition is optional -
        two copper layers bound one laminate - so the result is the only
        construction the profile can have."""
        from pcbqa import stackup_physical
        document = selection.export_physical_stackup(
            self.approved, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        declared = stackup_physical.from_declaration(document)
        self.assertEqual(declared.copper_layer_names, ["F.Cu", "B.Cu"])
        self.assertEqual(declared.layer("F.Cu").thickness_mm, 0.035)
        self.assertEqual(declared.layer("B.Cu").thickness_mm, 0.035)
        self.assertEqual(declared.layer("dielectric 1").thickness_mm, 1.53)
        self.assertEqual(declared.layer("dielectric 1").epsilon_r, 4.5)

    def test_a_composed_construction_says_that_it_is_composed(self):
        """A composed stack must never read as a published one: a reviewer
        who cannot tell them apart cannot audit either."""
        document = selection.export_physical_stackup(
            self.approved, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("composed", document["title"])
        self.assertIn("not a published construction", document["provenance"])
        self.assertTrue(any("composed, not published" in note
                            for note in document["notes"]))
        published = selection.export_physical_stackup(
            self.approved, _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertNotIn("composed", published["title"])
        self.assertNotIn("composed", published["provenance"])

    def test_the_multilayer_core_is_not_borrowed_for_two_layers(self):
        """The catalog states 4.6 for the cores of the constructions it
        publishes and 4.5 for a two-layer board. They are different
        laminates; without the two-layer statement the composition refuses
        rather than reaching for the nearer number."""
        snapshot = copy.deepcopy(self.approved)
        materials = snapshot["normalized"]["materials"]
        self.assertEqual(materials["core"]["dk"], 4.6)
        del materials["core 2-layer (capabilities)"]
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("not borrowed", str(caught.exception))

    def test_composition_stops_where_the_evidence_stops(self):
        """The ounce equivalence is what turns a stated weight into a stated
        thickness. Without it there is no construction to compose - and the
        refusal must say so, not fall through to the generic "no unique
        construction" that a disabled feature would also produce."""
        snapshot = copy.deepcopy(self.approved)
        del snapshot["normalized"]["capabilities"][
            "copper_weight_equivalence_um_per_oz"]
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("ounce-to-micrometre", str(caught.exception))

    def test_an_unestablished_copper_weight_refuses_rather_than_raising(self):
        """A catalog that stops publishing two-layer outer copper leaves the
        profile feasible with no weight resolved. That must refuse as a
        selection problem, not arrive at the arithmetic as None."""
        snapshot = copy.deepcopy(self.approved)
        capabilities = snapshot["normalized"]["capabilities"]
        for identity in ("outer_copper_2layer_oz",
                         "outer_copper_fr4_2layer_heavy_oz",
                         "outer_copper_fr4_standard_oz"):
            capabilities.pop(identity, None)
        requirements = _two_layer_requirements()
        del requirements["outer_copper_oz"]
        del requirements["min_track_mm"]
        del requirements["min_space_mm"]
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, requirements, ["F.Cu", "B.Cu"])
        self.assertIn("no outer copper weight", str(caught.exception))

    def test_contradictory_permittivity_is_refused_not_sorted(self):
        """Two records scoped to the same build that disagree are a real
        disagreement. Returning whichever sorts first would settle it by
        string ordering."""
        snapshot = copy.deepcopy(self.approved)
        materials = snapshot["normalized"]["materials"]
        materials["core 2-layer (impedance)"] = dict(
            materials["core 2-layer (capabilities)"],
            source="impedance", dk=3.6)
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("contradictory evidence", str(caught.exception))

    def test_two_pages_agreeing_is_not_a_contradiction(self):
        """Agreement is corroboration. Refusing it would make a second
        witness a fault."""
        snapshot = copy.deepcopy(self.approved)
        materials = snapshot["normalized"]["materials"]
        materials["core 2-layer (impedance)"] = dict(
            materials["core 2-layer (capabilities)"], source="impedance")
        document = selection.export_physical_stackup(
            snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertEqual(document["layers"][1]["epsilon_r"], 4.5)

    def test_a_conditioned_record_is_not_an_answer_about_a_build(self):
        """A record carrying its own conditions describes a narrower thing
        than the build. It must not be consumed as the build's value."""
        snapshot = copy.deepcopy(self.approved)
        materials = snapshot["normalized"]["materials"]
        materials["core 2-layer (capabilities)"]["properties"] = {
            "core_thickness_mm": 0.1}
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("not borrowed", str(caught.exception))

    def test_a_record_scoped_wider_than_the_build_is_not_an_answer(self):
        """min_layers alone is not the scope: a record published for two
        layers upward is a statement about a range, not about this build."""
        snapshot = copy.deepcopy(self.approved)
        materials = snapshot["normalized"]["materials"]
        materials["core 2-layer (capabilities)"]["applies"] = {
            "min_layers": 2, "max_layers": 8}
        with self.assertRaises(selection.SelectionError):
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])

    def test_copper_that_fills_the_board_is_not_a_construction(self):
        """No pair of currently stated options reaches this, which is why
        the arithmetic is guarded rather than trusted: a later catalog is
        free to state a heavier foil, and a stack with no dielectric left
        in it must refuse instead of being emitted."""
        snapshot = copy.deepcopy(self.approved)
        snapshot["normalized"]["capabilities"][
            "copper_weight_equivalence_um_per_oz"]["value"] = 900.0
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("no dielectric", str(caught.exception))

    def test_composition_never_displaces_a_published_construction(self):
        """Composition answers "the fabricator publishes none here", not
        "none matches this profile". A published two-layer construction the
        profile's copper build rules out still means constructions ARE
        published at two layers, and composing beside it would commit the
        flat claim that none is - into a file a board pins."""
        snapshot = copy.deepcopy(self.approved)
        published = copy.deepcopy(
            snapshot["normalized"]["stackups"]["JLC-4L-no-requirement"])
        published["layers"] = [
            {"role": model.COPPER, "thickness_mm": 0.07},
            {"role": model.DIELECTRIC, "form": model.CORE,
             "thickness_mm": 1.46},
            {"role": model.COPPER, "thickness_mm": 0.07}]
        published["applicability"] = {"nominal_thickness_mm": 1.6,
                                      "outer_copper_weight_oz": 2.0}
        snapshot["normalized"]["stackups"]["JLC02161-published"] = published
        with self.assertRaises(selection.SelectionError) as caught:
            selection.export_physical_stackup(
                snapshot, _two_layer_requirements(), ["F.Cu", "B.Cu"])
        self.assertIn("no unique construction", str(caught.exception))

    def test_the_composed_identity_does_not_depend_on_json_spelling(self):
        """1 and 1.0 are the same thickness. Two identities for one
        construction would be two provenance strings for one thing."""
        integer = _two_layer_requirements()
        integer["board_thickness_mm"] = 1
        real = _two_layer_requirements()
        real["board_thickness_mm"] = 1.0
        names = {selection.export_physical_stackup(
            self.approved, requirements,
            ["F.Cu", "B.Cu"])["generated_from"]["stackup"]
            for requirements in (integer, real)}
        self.assertEqual(len(names), 1, names)

    def test_no_dk_is_borrowed_for_an_unlisted_material(self):
        snapshot = copy.deepcopy(self.approved)
        del snapshot["normalized"]["materials"]["prepreg 7628"]
        document = selection.export_physical_stackup(
            snapshot, _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        prepreg = document["layers"][1]
        self.assertIsNone(prepreg["epsilon_r"])
        self.assertIn("states no dielectric constant",
                      prepreg["epsilon_r_note"])


# ---------------------------------------------------------------------------
# combinations, not menus
# ---------------------------------------------------------------------------

class FeasibilityIsAboutCombinations(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    def test_odd_layer_counts_do_not_pass_on_range_membership(self):
        """3 and 5 sit inside the stated 1-32 range and outside every
        stated discrete count; the range must not carry them."""
        for layers in (3, 5, 7, 31):
            result = selection.select(
                self.catalog, _requirements(copper_layers=layers))
            self.assertFalse(result["feasible"], layers)
            rejection = [r for r in result["rejections"]
                         if r["requirement"] == "copper_layers"][0]
            self.assertIn("discrete", rejection["issue"])

    def test_offered_even_counts_pass_the_layer_check(self):
        """8 layers is a stated offered count; it fails later only on
        evidence the catalog genuinely lacks for it, never on the count."""
        result = selection.select(self.catalog,
                                  _requirements(copper_layers=8))
        self.assertFalse(any(r["requirement"] == "copper_layers"
                             for r in result["rejections"]),
                         result["rejections"])

    def test_the_impedance_layer_list_is_read_with_its_ellipsis(self):
        counts = self.catalog["capabilities"][
            "controlled_impedance_layer_counts"]["value"]
        self.assertEqual(counts, list(range(4, 33, 2)))
        self.assertIn("elides", self.catalog["capabilities"][
            "controlled_impedance_layer_counts"]["conditions"])

    def test_a_broken_enumeration_refuses_the_ellipsis_reading(self):
        raw = _raw_sources()
        raw["capabilities"] = raw["capabilities"].replace(
            b"4/6/8/10/12/14/16/18/20/", b"4/6/9/10/12/14/16/18/20/", 1)
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("no longer ascends", str(caught.exception))

    def test_thickness_over_2mm_is_conditioned_not_open(self):
        """3.0 mm exists per the page - for 12-plus-layer boards, values
        unpublished. An 8-layer 3.0 mm request must reject on the stated
        condition, and a 12-layer one on the unpublished values."""
        for layers in (8, 12):
            result = selection.select(
                self.catalog, _requirements(copper_layers=layers,
                                            board_thickness_mm=3.0))
            self.assertFalse(result["feasible"], layers)
            rejection = [r for r in result["rejections"]
                         if r["requirement"] == "board_thickness_mm"][0]
            self.assertIn("12", rejection["issue"])
            self.assertIn("not", rejection["issue"])

    def test_impedance_thickness_comes_from_the_sections_own_options(self):
        """0.4 mm is a stated global FR-4 thickness, and it is NOT among
        the 4-layer impedance section's options; the pair must reject
        even though each half exists."""
        result = selection.select(
            self.catalog, _requirements(board_thickness_mm=0.4,
                                        impedance_control=True))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "board_thickness_mm"][0]
        self.assertIn("not among the stated thickness options",
                      rejection["issue"])
        # The same 0.4 mm without impedance is an ordinary stated option.
        relaxed = selection.select(
            self.catalog, _requirements(board_thickness_mm=0.4))
        self.assertFalse(any(r["requirement"] == "board_thickness_mm"
                             for r in relaxed["rejections"]))

    def test_impedance_on_a_non_listed_layer_count_is_infeasible(self):
        result = selection.select(
            self.catalog, _requirements(copper_layers=2,
                                        impedance_control=True,
                                        inner_copper_oz=None))
        self.assertFalse(result["feasible"])
        self.assertTrue(any(r["requirement"] == "impedance_control"
                            for r in result["rejections"]))

    def test_impedance_without_a_compatible_construction_is_infeasible(
            self):
        """8-layer impedance is a stated offering, but no 8-layer
        construction is published; controlled impedance IS its
        construction, so the profile fails closed."""
        result = selection.select(
            self.catalog, _requirements(copper_layers=8,
                                        board_thickness_mm=1.6,
                                        impedance_control=True))
        self.assertFalse(result["feasible"])
        issues = " ".join(r["issue"] for r in result["rejections"])
        self.assertIn("requires a published construction", issues)

    def test_impedance_with_incompatible_copper_is_infeasible(self):
        """At 4 layers and 1.6 mm impedance IS offered - but with 1 oz
        inner copper no published construction describes the build, so
        the combination fails while its parts all exist."""
        result = selection.select(
            self.catalog, _requirements(impedance_control=True,
                                        inner_copper_oz=1.0))
        self.assertFalse(result["feasible"])
        issues = " ".join(r["issue"] for r in result["rejections"])
        self.assertIn("requires a published construction", issues)

    def test_ordinary_fabrication_survives_an_unpublished_construction(
            self):
        """4L / 0.8 mm / 1 oz / 0.5 oz: every option is stated, no
        construction is published. Ordinary fabrication is feasible with
        no stackup claim - an unpublished construction is merely
        unpublished, unless the requirement depends on it."""
        result = selection.select(
            self.catalog, _requirements(board_thickness_mm=0.8))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertEqual(result["stackup_candidates"], [])

    def test_a_synthetic_tuple_cannot_be_assembled_from_menus(self):
        """Individually supported values whose combination no record
        supports: thickness options are published per layer count in
        impedance mode, so a synthetic catalog claiming 1.0 mm only for
        6 layers must reject a 4-layer 1.0 mm impedance request."""
        catalog = copy.deepcopy(self.catalog)
        catalog["capabilities"]["4L thickness_options"]["value"] = [
            0.8, 1.2, 1.6, 2.0]
        result = selection.select(
            catalog, _requirements(board_thickness_mm=1.0,
                                   impedance_control=True))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "board_thickness_mm"][0]
        self.assertIn("not for this combination", rejection["issue"])

    def test_each_board_class_is_judged_on_its_own_drill_rules(self):
        """0.2 mm drills pass the multilayer rule (0.15 mm floor) and fail
        the 1-layer rule (0.3 mm floor); 0.45 mm vias pass multilayer
        (0.25) and fail 1-layer (0.5). Neither class may borrow the
        other's rule in either direction."""
        multilayer = selection.select(
            self.catalog, _requirements(min_drill_mm=0.2))
        self.assertFalse(any(r["requirement"] == "min_drill_mm"
                             for r in multilayer["rejections"]))
        single = selection.select(
            self.catalog, _requirements(copper_layers=1,
                                        inner_copper_oz=None,
                                        min_drill_mm=0.2))
        drill_rejections = [r for r in single["rejections"]
                            if r["requirement"] == "min_drill_mm"]
        self.assertTrue(drill_rejections)
        self.assertIn("0.3", drill_rejections[0]["issue"])
        # The 1-layer row the page publishes is "NPTH only": a
        # via-shaped non-plated hole, not an interlayer plated via. It
        # lives in the catalog under its own category for what it
        # actually describes, and a generic plated-via requirement on a
        # 1-layer board refuses for want of any covering rule rather
        # than reading an NPTH as a barrel.
        via_rejections = [r for r in single["rejections"]
                          if r["requirement"] == "min_via_diameter_mm"]
        self.assertTrue(via_rejections)
        self.assertIn("no published via rule covers a 1-layer",
                      via_rejections[0]["issue"])
        npth = self.catalog["capabilities"][
            "npth-via 1-layer (capabilities)"]
        self.assertEqual(npth["category"], "via-npth")
        self.assertEqual(npth["value"], {"hole": 0.3, "diameter": 0.5})

    def test_two_layer_drill_and_via_use_two_layer_evidence(self):
        result = selection.select(
            self.catalog, _requirements(copper_layers=2,
                                        inner_copper_oz=None,
                                        min_drill_mm=0.2))
        self.assertFalse(any(r["requirement"] in ("min_drill_mm",
                                                  "min_via_diameter_mm")
                             for r in result["rejections"]),
                         result["rejections"])
        cited = " ".join(result["explanations"])
        self.assertIn("0.15-6.3 mm", cited)

    def test_unreadable_scoped_rule_terminology_refuses(self):
        """A via record whose value lost the published quantities must
        refuse rather than guess which number means what."""
        catalog = copy.deepcopy(self.catalog)
        catalog["capabilities"]["via multilayer (capabilities)"][
            "value"] = {"size": 0.25}
        result = selection.select(catalog, _requirements())
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "min_via_diameter_mm"][0]
        self.assertIn("terminology no longer maps safely",
                      rejection["issue"])

    def test_unknown_layer_thickness_compatibility_refuses(self):
        catalog = copy.deepcopy(self.catalog)
        del catalog["capabilities"]["4L thickness_options"]
        result = selection.select(
            catalog, _requirements(impedance_control=True))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "board_thickness_mm"][0]
        self.assertIn("unknown is not supported", rejection["issue"])


# ---------------------------------------------------------------------------
# one value, one meaning, one scope
# ---------------------------------------------------------------------------

class ValuesKeepTheirScopes(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    def test_calculator_dk_is_distinct_from_generic_dk(self):
        """The stackup page's generic core Dk (4.6) and the calculator's
        thickness-conditioned NP-155F values coexist under different
        identities with different scopes; neither replaces the other."""
        materials = self.catalog["materials"]
        self.assertEqual(materials["core"]["dk"], 4.6)
        self.assertNotIn("context", materials["core"])
        conditioned = materials[
            "core NP-155F 0.08mm (impedance-calculator)"]
        self.assertEqual(conditioned["context"],
                         "impedance-calculator model")
        self.assertEqual(conditioned["applies"],
                         {"min_layers": 4, "max_layers": 8})

    def test_the_same_prepreg_name_differs_by_family(self):
        """1080 prepreg: 3.91 in the NP-155F family, 3.99 under
        S1000-2M, 3.91 on the generic page - three records, three
        scopes, no collapsing."""
        materials = self.catalog["materials"]
        self.assertEqual(materials["prepreg 1080"]["dk"], 3.91)
        self.assertEqual(
            materials["prepreg 1080 (NP-155F, impedance-calculator)"]["dk"],
            3.91)
        self.assertEqual(
            materials["prepreg 1080 (S1000-2M, impedance-calculator)"]["dk"],
            3.99)

    def test_nominal_weight_and_finished_thickness_stay_distinct(self):
        """0.5 oz nominal inner copper, 17.5 um foil, 15.2 um finished:
        the catalog holds the weight options and the finished thickness
        as separate records, and nothing converts one into the other."""
        capabilities = self.catalog["capabilities"]
        self.assertIn(0.5, capabilities["inner_copper_fr4_oz"]["value"])
        finished = capabilities["finished_inner_half_oz_um"]
        self.assertEqual(finished["value"], 15.2)
        self.assertIn("nominal foil is 17.5", finished["conditions"])
        mil = capabilities["finished_copper_internal_0.5oz_mil"]
        self.assertEqual(mil["value"], 0.6)
        self.assertEqual(mil["units"], "mil")

    def test_the_construction_inner_basis_cites_the_finished_statement(
            self):
        basis = self.catalog["stackups"]["JLC-4L-no-requirement"][
            "applicability"]["inner_basis"]
        self.assertIn("15.2", basis)
        self.assertIn("impedance-calculator", basis)

    def test_export_still_uses_the_generic_stackup_page_values(self):
        """The board supplement restates the stackup page's own numbers;
        the calculator-model records exist for the future impedance
        solver and must not leak into it uninvited."""
        store = _approved_store("scope")
        document = selection.export_physical_stackup(
            store.approved(), _requirements(),
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(document["layers"][3]["epsilon_r"], 4.6)

    def test_the_soldermask_dk_carries_its_context(self):
        record = self.catalog["materials"][
            "soldermask (impedance-calculator)"]
        self.assertEqual(record["dk"], 3.8)
        self.assertEqual(record["context"], "impedance-calculator model")


# ---------------------------------------------------------------------------
# the evidence universe cannot shrink quietly
# ---------------------------------------------------------------------------

class TheCommittedCatalogRefusesWhatItCannotProve(unittest.TestCase):
    """Evidence integrity and fail-closed parsing, on the load path."""

    def _damaged(self, mutate, tag):
        store = _approved_store(tag)
        mutate(store)
        with self.assertRaises(StoreError) as caught:
            store.approved()
        return str(caught.exception)

    def test_missing_evidence_is_detected(self):
        def drop(store):
            name = sorted(os.listdir(store.approved_evidence))[0]
            os.unlink(os.path.join(store.approved_evidence, name))
        self.assertIn("evidence", self._damaged(drop, "ev_missing"))

    def test_altered_evidence_under_the_right_name_is_detected(self):
        def tamper(store):
            name = sorted(os.listdir(store.approved_evidence))[0]
            with open(os.path.join(store.approved_evidence, name),
                      "ab") as handle:
                handle.write(b"one byte too many")
        self.assertIn("evidence", self._damaged(tamper, "ev_altered"))

    def test_unreferenced_evidence_is_detected(self):
        def add_orphan(store):
            with open(os.path.join(store.approved_evidence, "orphan.raw"),
                      "wb") as handle:
                handle.write(b"not named by approved.json")
        detail = self._damaged(add_orphan, "ev_orphan")
        self.assertIn("unreferenced evidence", detail)
        self.assertIn("exactly", detail)

    def test_writing_a_refresh_removes_obsolete_evidence(self):
        root = _root("ev_replace")
        first, problem = acquire.acquire(fetcher=_fetcher())
        self.assertIsNone(problem)
        store_module.write_catalog(root, first, first["raw"])

        second = copy.deepcopy(first)
        second_raw = dict(first["raw"])
        changed = second_raw["capabilities"] + b"\nreviewed refresh\n"
        second_raw["capabilities"] = changed
        source = next(s for s in second["sources"]
                      if s["id"] == "capabilities")
        old_name = "capabilities-{}.raw".format(source["sha256_raw"][:12])
        source["sha256_raw"] = hashlib.sha256(changed).hexdigest()
        new_name = "capabilities-{}.raw".format(source["sha256_raw"][:12])

        store_module.write_catalog(root, second, second_raw)
        evidence = os.path.join(root, "catalog", "evidence")
        self.assertFalse(os.path.exists(os.path.join(evidence, old_name)))
        self.assertTrue(os.path.isfile(os.path.join(evidence, new_name)))
        self.assertEqual(store_module.verify_evidence(second, evidence), [])

    def test_writing_replaces_an_evidence_symlink_without_following_it(self):
        root = _root("ev_symlink")
        result, problem = acquire.acquire(fetcher=_fetcher())
        self.assertIsNone(problem)
        evidence = os.path.join(root, "catalog", "evidence")
        os.makedirs(evidence)
        source = result["sources"][0]
        name = "{}-{}.raw".format(source["id"],
                                  source["sha256_raw"][:12])
        outside = os.path.join(root, "outside.raw")
        with open(outside, "wb") as handle:
            handle.write(b"outside must stay unchanged")
        os.symlink(outside, os.path.join(evidence, name))

        store_module.write_catalog(root, result, result["raw"])
        self.assertFalse(os.path.islink(os.path.join(evidence, name)))
        with open(outside, "rb") as handle:
            self.assertEqual(handle.read(), b"outside must stay unchanged")

    def test_an_unsupported_snapshot_schema_refuses(self):
        def bump(store):
            _rewrite_json(store.approved_path,
                          lambda d: d.__setitem__("schema_version", 99))
        self.assertIn("schema_version", self._damaged(bump, "ev_schema"))

    def test_an_acquisition_that_did_not_complete_is_not_a_catalog(self):
        """A `parse-failed` file committed by accident must not load."""
        def spoil(store):
            def mutate(document):
                document["outcome"] = "parse-failed"
                document["normalized"] = None
            _rewrite_json(store.approved_path, mutate)
        detail = self._damaged(spoil, "ev_incomplete")
        self.assertIn("parse-failed", detail)
        self.assertIn("complete", detail)


class TheSourceSetIsComplete(unittest.TestCase):

    def test_a_record_citing_a_vanished_source_refuses(self):
        store = _approved_store("vanish")

        def drop(document):
            document["sources"] = [s for s in document["sources"]
                                   if s["id"] != "impedance"]
            document["declared_source_ids"] = [
                i for i in document["declared_source_ids"]
                if i != "impedance"]
        _rewrite_json(store.approved_path, drop)
        with self.assertRaises(StoreError) as caught:
            store.approved()
        self.assertIn("vanished", str(caught.exception))

    def test_a_complete_snapshot_missing_a_declared_source_refuses(self):
        store = _approved_store("declared")

        def drop(document):
            document["sources"] = [s for s in document["sources"]
                                   if s["id"] != "copper-weight"]
        _rewrite_json(store.approved_path, drop)
        with self.assertRaises(StoreError) as caught:
            store.approved()
        self.assertIn("evidence universe", str(caught.exception))

    def test_a_duplicated_source_id_refuses(self):
        store = _approved_store("dupe")

        def duplicate(document):
            document["sources"].append(dict(document["sources"][0]))
            document["declared_source_ids"].append(
                document["sources"][0]["id"])
        _rewrite_json(store.approved_path, duplicate)
        with self.assertRaises(StoreError) as caught:
            store.approved()
        self.assertIn("twice", str(caught.exception))


# ---------------------------------------------------------------------------
# stated restrictions beat global lists; scopes never borrow
# ---------------------------------------------------------------------------

class StatedRestrictionsBeatGlobalLists(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    def test_a_globally_listed_thickness_respects_its_restriction(self):
        """0.6 mm is in the global FR-4 list and stated as not available
        for 4-layer boards; the pair rejects on the statement."""
        result = selection.select(
            self.catalog, _requirements(board_thickness_mm=0.6))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "board_thickness_mm"][0]
        self.assertIn("not available for 4-layer", rejection["issue"])

    def test_the_same_thickness_passes_where_no_restriction_names_it(self):
        """0.6 mm at 8 layers: the restriction names 1, 4 and 6 only."""
        result = selection.select(
            self.catalog, _requirements(copper_layers=8,
                                        board_thickness_mm=0.6))
        self.assertFalse(any(r["requirement"] == "board_thickness_mm"
                             for r in result["rejections"]),
                         result["rejections"])

    def test_a_newly_added_restriction_cannot_be_silently_ignored(self):
        """The parser discovers restriction-shaped statements instead of
        enumerating known ones: a new 0.8 mm exclusion lands in the
        catalog, changes the digest, and therefore demands review."""
        raw = _raw_sources()
        raw["thickness-options"] += (
            b"<div>Boards with 0.8mm thickness are special. This "
            b"thickness is not available for 8-layer PCBs.</div>")
        catalog = jlcpcb.parse(raw)
        record = catalog["capabilities"]["thickness_restriction 0.8mm"]
        self.assertEqual(record["value"]["excluded_layer_counts"], [8])
        self.assertNotEqual(model.normalized_digest(catalog),
                            model.normalized_digest(
                                jlcpcb.parse(_raw_sources())))
        result = selection.select(
            catalog, _requirements(copper_layers=8,
                                   board_thickness_mm=0.8))
        self.assertTrue(any(r["requirement"] == "board_thickness_mm"
                            for r in result["rejections"]))

    def test_a_restriction_this_code_cannot_read_fails_the_parse(self):
        """A restriction-shaped sentence that will not normalize stops
        the acquisition; it must never vanish while the page 'parses
        fine'."""
        raw = _raw_sources()
        raw["thickness-options"] += (
            b"<div>Certain finishes are not available for thin-layer "
            b"PCBs.</div>")
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("half-understood", str(caught.exception))

    def test_duplicate_restrictions_for_one_thickness_refuse(self):
        raw = _raw_sources()
        raw["thickness-options"] += (
            b"<div>Also, 0.6mm thickness boards are not available for "
            b"8-layer PCBs.</div>")
        with self.assertRaises(jlcpcb.ParseError) as caught:
            jlcpcb.parse(raw)
        self.assertIn("name 0.6 mm", str(caught.exception))

    def test_overlapping_scoped_ranges_intersect_conservatively(self):
        """A second drill record covering multilayer boards with a
        narrower range: the merged rule takes the highest floor AND the
        lowest ceiling, so a 5 mm drill inside one record's range but
        above the other's ceiling rejects."""
        catalog = copy.deepcopy(self.catalog)
        catalog["capabilities"]["drill_diameter multilayer-b (synthetic)"]             = model.capability(
                "capabilities", "drill_diameter multilayer-b (synthetic)",
                {"min": 0.2, "max": 4.0}, units="mm",
                conditions="synthetic overlapping range",
                category="drill",
                applies={"min_layers": 4, "max_layers": None})
        tight = selection.select(
            catalog, _requirements(min_drill_mm=0.18))
        rejection = [r for r in tight["rejections"]
                     if r["requirement"] == "min_drill_mm"][0]
        self.assertIn("0.2", rejection["issue"])
        passing = selection.select(
            catalog, _requirements(min_drill_mm=0.3))
        cited = [e for e in passing["explanations"] if "drill" in e][0]
        self.assertIn("0.2-4.0", cited)

    def test_contradictory_scoped_ranges_refuse(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["capabilities"]["drill_diameter multilayer-b (synthetic)"]             = model.capability(
                "capabilities", "drill_diameter multilayer-b (synthetic)",
                {"min": 7.0, "max": 8.0}, units="mm",
                conditions="synthetic contradictory range",
                category="drill",
                applies={"min_layers": 4, "max_layers": None})
        result = selection.select(catalog, _requirements())
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "min_drill_mm"][0]
        self.assertIn("contradict", rejection["issue"])

    def test_an_unreadable_restriction_record_refuses_the_pair(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["capabilities"]["thickness_restriction 0.6mm"][
            "value"] = {"mm": 0.6}
        result = selection.select(
            catalog, _requirements(board_thickness_mm=0.6,
                                   copper_layers=8))
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "board_thickness_mm"][0]
        self.assertIn("cannot be read", rejection["issue"])

    def test_impedance_and_ordinary_thickness_rules_stay_distinct(self):
        """0.8 mm: fine for an ordinary 4-layer board (global list, no
        restriction), stated for 4-layer impedance sections too - but
        1.0 mm at 6 layers shows the split: ordinary passes, impedance
        rejects because the 6-layer section states 1.2-2.0 only."""
        ordinary = selection.select(
            self.catalog, _requirements(copper_layers=6,
                                        board_thickness_mm=1.0))
        self.assertFalse(any(r["requirement"] == "board_thickness_mm"
                             for r in ordinary["rejections"]))
        impedance = selection.select(
            self.catalog, _requirements(copper_layers=6,
                                        board_thickness_mm=1.0,
                                        impedance_control=True))
        self.assertTrue(any(r["requirement"] == "board_thickness_mm"
                            for r in impedance["rejections"]))

    def test_two_layer_heavy_copper_is_offered_on_its_own_evidence(self):
        result = selection.select(
            self.catalog, _requirements(copper_layers=2,
                                        inner_copper_oz=None,
                                        outer_copper_oz=3.5,
                                        min_track_mm=0.3,
                                        min_space_mm=0.3))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertEqual(result["profile"]["outer_copper_oz"], 3.5)

    def test_two_layer_does_not_offer_unlisted_weights(self):
        result = selection.select(
            self.catalog, _requirements(copper_layers=2,
                                        inner_copper_oz=None,
                                        outer_copper_oz=5.0,
                                        min_track_mm=0.3,
                                        min_space_mm=0.3))
        self.assertFalse(result["feasible"])
        self.assertTrue(any("not among the options offered for this "
                            "board class" in r["issue"]
                            for r in result["rejections"]))

    def test_multilayer_does_not_borrow_two_layer_heavy_copper(self):
        result = selection.select(
            self.catalog, _requirements(outer_copper_oz=3.5,
                                        min_track_mm=0.3,
                                        min_space_mm=0.3))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "outer_copper_oz"][0]
        self.assertIn("this board class", rejection["issue"])

    def test_one_layer_copper_rests_on_records_that_cover_one_layer(self):
        """The guide's FR-4 outer row covers every layer count; the
        2-layer and multilayer capability rows do not cover 1. A 1-layer
        board must resolve 1 oz from the guide record alone - visible in
        the citation - and never from the class rows."""
        result = selection.select(
            self.catalog, _requirements(copper_layers=1,
                                        inner_copper_oz=None,
                                        min_via_diameter_mm=None))
        self.assertTrue(result["feasible"], result["rejections"])
        cited = [e for e in result["explanations"]
                 if "outer copper is an offered option" in e][0]
        self.assertIn("copper-weight", cited)
        self.assertNotIn("impedance", cited)

    def test_conditional_availability_does_not_prove_nondefault_inner(self):
        """The fabricator itself says inner-copper availability depends on
        unpublished factors; 1 oz inner is listed but cannot be proven
        for any particular build, so it is unknown, not offered."""
        result = selection.select(
            self.catalog, _requirements(inner_copper_oz=1.0))
        self.assertFalse(result["feasible"])
        rejection = [r for r in result["rejections"]
                     if r["requirement"] == "inner_copper_oz"][0]
        self.assertIn("conditional", rejection["issue"])
        self.assertIn("unknown is not supported", rejection["issue"])

    def test_the_stated_default_inner_weight_remains_established(self):
        result = selection.select(self.catalog, _requirements())
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertEqual(result["profile"]["inner_copper_oz"], 0.5)


# ---------------------------------------------------------------------------
# the input boundary fails closed
# ---------------------------------------------------------------------------

class RequirementsFailClosedAtTheBoundary(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.catalog = jlcpcb.parse(_raw_sources())

    NUMERIC_KEYS = ("board_thickness_mm", "min_track_mm", "min_space_mm",
                    "min_drill_mm", "min_via_diameter_mm",
                    "outer_copper_oz", "inner_copper_oz")

    def _refuses(self, **overrides):
        with self.assertRaises(selection.SelectionError):
            selection.select(self.catalog, _requirements(**overrides))

    def test_nan_never_slips_past_both_sides_of_a_comparison(self):
        for key in self.NUMERIC_KEYS:
            self._refuses(**{key: float("nan")})

    def test_infinities_refuse(self):
        for key in self.NUMERIC_KEYS:
            self._refuses(**{key: float("inf")})
            self._refuses(**{key: float("-inf")})

    def test_zero_and_negative_dimensions_refuse(self):
        for key in self.NUMERIC_KEYS:
            self._refuses(**{key: 0})
            self._refuses(**{key: -1.6})

    def test_booleans_are_not_numbers(self):
        for key in self.NUMERIC_KEYS:
            self._refuses(**{key: True})

    def test_numeric_strings_are_not_numbers(self):
        for key in self.NUMERIC_KEYS:
            self._refuses(**{key: "1.6"})

    def test_impedance_control_is_a_boolean_or_absent(self):
        for bad in ("false", "true", 0, 1, "no"):
            self._refuses(impedance_control=bad)
        requirements = _requirements()
        del requirements["impedance_control"]
        result = selection.select(self.catalog, requirements)
        self.assertTrue(result["feasible"], result["rejections"])
        result = selection.select(self.catalog,
                                  _requirements(impedance_control=None))
        self.assertTrue(result["feasible"], result["rejections"])

    def test_material_must_be_a_name(self):
        self._refuses(material=42)
        self._refuses(material="")


# ---------------------------------------------------------------------------
# validation stays offline
# ---------------------------------------------------------------------------

class ValidationPerformsNoNetworkAccess(unittest.TestCase):

    def test_no_gate_or_core_module_reaches_the_fabricator_package(self):
        """Validation must not even be able to fetch by accident."""
        package = paths.PACKAGE
        offenders = []
        for base, dirs, files in os.walk(package):
            if "fabricators" in base:
                continue
            dirs[:] = [d for d in dirs if d not in ("fabricators",
                                                    "__pycache__")]
            for name in files:
                if not name.endswith(".py"):
                    continue
                with open(os.path.join(base, name),
                          encoding="utf-8") as handle:
                    text = handle.read()
                if "fabricators" in text or "urllib" in text:
                    offenders.append(os.path.join(base, name))
        self.assertEqual(offenders, [],
                         "modules outside pcbqa.fabricators must neither "
                         "import it nor open the network")

    def test_the_network_is_only_behind_the_refresh_path(self):
        package_dir = os.path.join(paths.PACKAGE, "fabricators")
        for name in os.listdir(package_dir):
            if not name.endswith(".py") or name == "acquire.py":
                continue
            with open(os.path.join(package_dir, name),
                      encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("urllib", text,
                             name + " must not touch the network")

    def test_offline_selection_needs_only_the_approved_file(self):
        store = _approved_store("offline")
        result = selection.select(store.approved()["normalized"],
                                  _requirements())
        self.assertTrue(result["feasible"])

    def test_the_toolkit_ships_an_approved_jlcpcb_baseline(self):
        """The committed baseline a clean checkout works from, verified the
        same way the store verifies any snapshot - digests, evidence
        bytes and all."""
        store = CatalogStore(os.path.join(HERE, "profiles", "jlcpcb"))
        approved = store.approved()
        self.assertIsNotNone(approved, "no committed approved baseline")
        self.assertTrue(approved["normalized"]["stackups"])
        problems = store_module.verify_evidence(approved,
                                                store.approved_evidence)
        self.assertEqual(problems, [])


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
