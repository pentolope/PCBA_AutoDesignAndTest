"""Fabricator knowledge: acquisition, comparison, promotion, selection.

Everything here runs offline against fixtures. The fixtures are minimized
excerpts of the real official JLCPCB pages - the structure and every value
are verbatim - so the parser is exercised against what the fabricator
actually publishes, without any test depending on the fabricator's website
being up, unchanged, or reachable.

The properties under test are the trust rules, and each is checked from the
outside rather than by mirroring the implementation: a restyled page must
not demand review, one changed thickness must, a failed fetch must leave the
approved state untouched, unknown capability must never read as supported,
and promotion must be the only door through which observation becomes trust.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from pcbqa.fabricators import acquire, diff, jlcpcb, model, selection  # noqa: E402
from pcbqa.fabricators.store import CatalogStore, StoreError            # noqa: E402
from tests import paths                                                 # noqa: E402

FIXTURES = os.path.join(paths.FIXTURES, "fabricators", "jlcpcb")


def _fixture_bytes(name):
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def _raw_sources():
    return {"impedance": _fixture_bytes("impedance.fixture.html"),
            "capabilities": _fixture_bytes("capabilities.fixture.html")}


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
    """A store with the fixture catalog observed and promoted as baseline."""
    root = _root(tag)
    snapshot, problem = acquire.acquire("jlcpcb", root, fetcher=_fetcher())
    assert problem is None, problem
    store = CatalogStore(root, "jlcpcb")
    store.promote(snapshot["normalized_sha256"][:12], [], initial=True)
    return store


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

    def test_the_default_stackup_is_evidence_not_invention(self):
        stackup = self.catalog["stackups"]["JLC-4L-no-requirement"]
        self.assertTrue(stackup["default_when_no_impedance_requirement"])
        roles = [(l["role"], l.get("form")) for l in stackup["layers"]]
        self.assertEqual(roles, [("copper", None), ("dielectric", "prepreg"),
                                 ("copper", None), ("dielectric", "core"),
                                 ("copper", None), ("dielectric", "prepreg"),
                                 ("copper", None)])
        self.assertEqual(model.stackup_total_mm(stackup), 1.5862)

    def test_named_impedance_stackups_stay_distinct_from_the_default(self):
        """The 7628 construction and the default are byte-identical in their
        layers on the page. They are still two published options, and the
        catalog must not collapse them."""
        default = self.catalog["stackups"]["JLC-4L-no-requirement"]
        named = self.catalog["stackups"]["JLC04161H-7628"]
        self.assertEqual(
            [l for l in default["layers"]], [l for l in named["layers"]])
        self.assertFalse(named["default_when_no_impedance_requirement"])

    def test_capability_records_carry_their_evidence(self):
        for identity, record in self.catalog["capabilities"].items():
            self.assertTrue(record.get("source"), identity)
            self.assertTrue(record.get("excerpt"), identity)

    def test_missing_source_refuses(self):
        with self.assertRaises(model.CatalogError):
            jlcpcb.parse({"impedance":
                          _fixture_bytes("impedance.fixture.html")})

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
        self.assertEqual(diff.semantic_diff(original, observed), [])


# ---------------------------------------------------------------------------
# the store: trust moves only through promotion
# ---------------------------------------------------------------------------

class TrustMovesOnlyThroughPromotion(unittest.TestCase):

    def test_a_failed_fetch_preserves_the_approved_state(self):
        store = _approved_store("keep")
        before = store.approved()["normalized_sha256"]
        snapshot, problem = acquire.acquire(
            "jlcpcb", store.root, fetcher=_fetcher(fail=("impedance",)))
        self.assertIn("incomplete", problem)
        self.assertFalse(snapshot["complete"])
        self.assertEqual(store.approved()["normalized_sha256"], before)

    def test_an_incomplete_observation_cannot_be_promoted(self):
        store = _approved_store("incomplete")
        snapshot, _problem = acquire.acquire(
            "jlcpcb", store.root, fetcher=_fetcher(fail=("capabilities",)))
        with self.assertRaises(StoreError) as caught:
            store.promote(snapshot["normalized_sha256"][:12], [],
                          allow_older=True)
        self.assertIn("incomplete", str(caught.exception))

    def test_a_parse_failure_writes_no_observation_but_keeps_evidence(self):
        root = _root("parsefail")
        raw = _raw_sources()
        raw["capabilities"] = b"<html>redesigned beyond recognition</html>"
        snapshot, problem = acquire.acquire("jlcpcb", root,
                                            fetcher=_fetcher(raw=raw))
        self.assertIsNone(snapshot)
        self.assertIn("parse failed", problem)
        store = CatalogStore(root, "jlcpcb")
        self.assertIsNone(store.observed())
        evidence = os.listdir(store.observed_evidence)
        self.assertTrue(any("capabilities" in name for name in evidence),
                        "the bytes that defeated the parser must survive "
                        "for reproduction")

    def test_a_corrupt_observation_cannot_become_approved(self):
        store = _approved_store("corrupt")
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())
        with open(store.observed_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        snapshot["normalized"]["materials"]["core"]["dk"] = 99.0
        with open(store.observed_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        with self.assertRaises(StoreError) as caught:
            store.observed()
        self.assertIn("altered or corrupted", str(caught.exception))
        with self.assertRaises(StoreError):
            store.promote(snapshot["normalized_sha256"][:12], [])

    def test_promotion_must_name_what_was_reviewed(self):
        store = _approved_store("named")
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())
        with self.assertRaises(StoreError):
            store.promote("abc", [])
        with self.assertRaises(StoreError):
            store.promote("0" * 12, [])

    def test_the_first_baseline_is_a_distinct_deliberate_act(self):
        root = _root("initial")
        snapshot, _problem = acquire.acquire("jlcpcb", root,
                                             fetcher=_fetcher())
        store = CatalogStore(root, "jlcpcb")
        with self.assertRaises(StoreError) as caught:
            store.promote(snapshot["normalized_sha256"][:12], [])
        self.assertIn("initial", str(caught.exception))
        store.promote(snapshot["normalized_sha256"][:12], [], initial=True)
        with self.assertRaises(StoreError):
            store.promote(snapshot["normalized_sha256"][:12], [],
                          initial=True, allow_older=True)

    def test_promoting_an_observation_older_than_approved_needs_saying_so(
            self):
        store = _approved_store("older")
        with open(store.observed_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        snapshot["retrieved_utc"] = "2020-01-01T00:00:00+00:00"
        with open(store.observed_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        with self.assertRaises(StoreError) as caught:
            store.promote(snapshot["normalized_sha256"][:12], [])
        self.assertIn("backwards in time", str(caught.exception))
        store.promote(snapshot["normalized_sha256"][:12], [],
                      allow_older=True)

    def test_promotion_is_audited(self):
        store = _approved_store("audit")
        with open(store.promotions_path, encoding="utf-8") as handle:
            log = json.load(handle)
        self.assertEqual(len(log), 1)
        record = log[0]
        self.assertTrue(record["initial"])
        self.assertIsNone(record["from_normalized_sha256"])
        self.assertEqual(record["to_normalized_sha256"],
                         store.approved()["normalized_sha256"])
        self.assertTrue(record["sources"][0].get("sha256_raw"))

    def test_stale_is_distinguishable_from_changed(self):
        import datetime
        store = _approved_store("stale")
        now = datetime.datetime.now(datetime.timezone.utc)
        fresh = store.freshness(now=now)
        self.assertEqual(fresh["state"], "fresh")
        later = now + datetime.timedelta(days=45)
        stale = store.freshness(now=later)
        self.assertEqual(stale["state"], "stale")
        self.assertIn("remains usable", stale["detail"])
        # And a clock running behind the evidence is anomalous, not fresh.
        earlier = now - datetime.timedelta(days=1)
        self.assertEqual(store.freshness(now=earlier)["state"], "anomalous")

    def test_the_parser_identity_travels_with_every_snapshot(self):
        store = _approved_store("parserid")
        approved = store.approved()
        self.assertEqual(approved["parser"]["version"],
                         jlcpcb.PARSER_VERSION)
        with open(store.observed_path, encoding="utf-8") as handle:
            snapshot = json.load(handle)
        snapshot["parser"]["version"] = "999-experimental"
        with open(store.observed_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        observed = store.observed()
        self.assertNotEqual(observed["parser"], approved["parser"],
                            "a parser change and a fabricator change must "
                            "remain distinguishable histories")


# ---------------------------------------------------------------------------
# selection: feasibility first, standardness second, complexity last
# ---------------------------------------------------------------------------

def _requirements(**overrides):
    base = {"copper_layers": 4, "board_thickness_mm": 1.6,
            "min_track_mm": 0.15, "min_space_mm": 0.15,
            "min_drill_mm": 0.3, "min_via_diameter_mm": 0.45,
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
        self.assertTrue(any("stated default" in e
                            for e in result["explanations"]))

    def test_an_incapable_profile_is_rejected_with_the_stated_limit(self):
        result = selection.select(self.catalog,
                                  _requirements(min_track_mm=0.1))
        self.assertFalse(result["feasible"])
        self.assertTrue(any("0.15" in r["issue"]
                            for r in result["rejections"]))

    def test_unknown_capability_never_becomes_supported(self):
        catalog = copy.deepcopy(self.catalog)
        del catalog["capabilities"]["drill_diameter_multilayer_mm"]
        result = selection.select(catalog, _requirements())
        self.assertFalse(result["feasible"])
        self.assertTrue(any("unknown is not supported" in r["issue"]
                            for r in result["rejections"]))

    def test_cheapness_never_overrides_an_unverifiable_requirement(self):
        """2 oz outer copper is offered - but the published trace/space
        limit is conditioned on 1 oz, so a fine-pitch 2 oz board is
        unverifiable, not cheaper-at-any-cost feasible."""
        result = selection.select(self.catalog,
                                  _requirements(outer_copper_oz=2.0))
        self.assertFalse(result["feasible"])
        self.assertTrue(any("1 oz outer copper" in r["issue"]
                            for r in result["rejections"]))

    def test_a_non_standard_thickness_is_rejected_on_the_stated_options(self):
        result = selection.select(self.catalog,
                                  _requirements(board_thickness_mm=1.4))
        self.assertFalse(result["feasible"])
        self.assertTrue(any("1.4" in r["issue"] for r in
                            result["rejections"]))

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

    def test_an_unpublished_thickness_yields_no_stackup_claim(self):
        result = selection.select(self.catalog,
                                  _requirements(board_thickness_mm=0.8))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertTrue(any("nothing is scaled or invented" in e
                            for e in result["explanations"]))


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
            self.approved, "JLC-4L-no-requirement",
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertIn(self.approved["normalized_sha256"][:12],
                      document["provenance"])
        declared = stackup_physical.from_declaration(document)
        self.assertEqual(declared.copper_layer_names,
                         ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        self.assertEqual(declared.layer("dielectric 1").epsilon_r, 4.4)
        self.assertEqual(declared.layer("dielectric 2").epsilon_r, 4.6)
        self.assertEqual(declared.layer("dielectric 1").thickness_mm, 0.2104)

    def test_the_evidence_chain_reaches_the_raw_bytes(self):
        document = selection.export_physical_stackup(
            self.approved, "JLC-4L-no-requirement",
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        sources = {s["id"]: s for s in
                   document["generated_from"]["sources"]}
        self.assertTrue(sources["impedance"]["sha256_raw"])
        self.assertEqual(
            document["generated_from"]["approved_normalized_sha256"],
            self.approved["normalized_sha256"])

    def test_a_layer_count_mismatch_refuses(self):
        with self.assertRaises(selection.SelectionError):
            selection.export_physical_stackup(
                self.approved, "JLC-4L-no-requirement", ["F.Cu", "B.Cu"])

    def test_no_dk_is_borrowed_for_an_unlisted_material(self):
        snapshot = copy.deepcopy(self.approved)
        del snapshot["normalized"]["materials"]["prepreg 7628"]
        document = selection.export_physical_stackup(
            snapshot, "JLC-4L-no-requirement",
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        prepreg = document["layers"][1]
        self.assertIsNone(prepreg["epsilon_r"])
        self.assertIn("states no dielectric constant",
                      prepreg["epsilon_r_note"])


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
        same way the store verifies any snapshot - digest and all."""
        store = CatalogStore(os.path.join(HERE, "profiles", "jlcpcb"),
                             "jlcpcb")
        approved = store.approved()
        self.assertIsNotNone(approved, "no committed approved baseline")
        self.assertTrue(approved["normalized"]["stackups"])
        for source in approved["sources"]:
            name = "{}-{}.raw".format(source["id"],
                                      source["sha256_raw"][:12])
            self.assertTrue(
                os.path.isfile(os.path.join(store.approved_evidence, name)),
                "approved evidence file missing: " + name)


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
