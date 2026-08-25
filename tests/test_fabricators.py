"""Fabricator knowledge: acquisition, comparison, promotion, selection.

Everything here runs offline against fixtures. The fixtures are minimized
excerpts of the real official JLCPCB pages - the structure and every value
are verbatim - so the parser is exercised against what the fabricator
actually publishes, without any test depending on the fabricator's website
being up, unchanged, or reachable.

The properties under test are the trust rules, and each is checked from the
outside rather than by mirroring the implementation: a restyled page must
not demand review, one changed thickness must, a failed fetch must leave
the approved state untouched while superseding any older observation as
"latest", raw evidence must be load-bearing, unknown capability must never
read as supported, a selected stackup must describe the same process
configuration as the selected options, and promotion must be the only door
through which observation becomes trust.
"""

from __future__ import annotations

import copy
import datetime
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
            "copper-weight": _fixture_bytes("copper-weight.fixture.html")}


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
# the store: trust moves only through promotion
# ---------------------------------------------------------------------------

class TrustMovesOnlyThroughPromotion(unittest.TestCase):

    def test_a_failed_fetch_preserves_the_approved_state(self):
        store = _approved_store("keep")
        before = store.approved()["normalized_sha256"]
        snapshot, problem = acquire.acquire(
            "jlcpcb", store.root, fetcher=_fetcher(fail=("impedance",)))
        self.assertIn("incomplete", problem)
        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertEqual(store.approved()["normalized_sha256"], before)

    def test_an_incomplete_observation_cannot_be_promoted(self):
        store = _approved_store("incomplete")
        acquire.acquire(
            "jlcpcb", store.root, fetcher=_fetcher(fail=("capabilities",)))
        with self.assertRaises(StoreError) as caught:
            store.promote("0" * 12, [], allow_older=True)
        self.assertIn("incomplete", str(caught.exception))

    def test_a_newer_parse_failure_supersedes_an_older_success(self):
        """approved A, successful refresh B, then a newer fetch C that the
        parser cannot understand: C is now the latest known source state.
        B survives as history but can no longer be promoted as current -
        the newest established fact is 'the live source is unreadable'."""
        store = _approved_store("chronology")
        good, problem = acquire.acquire("jlcpcb", store.root,
                                        fetcher=_fetcher())
        self.assertIsNone(problem)
        raw = _raw_sources()
        raw["capabilities"] = b"<html>redesigned beyond recognition</html>"
        failed, problem = acquire.acquire("jlcpcb", store.root,
                                          fetcher=_fetcher(raw=raw))
        self.assertIn("parse failed", problem)
        latest = store.observed()
        self.assertEqual(latest["outcome"], "parse-failed")
        self.assertIsNone(latest["normalized"])
        # B is not promotable: promotion sees only the newest attempt.
        with self.assertRaises(StoreError) as caught:
            store.promote(good["normalized_sha256"][:12], [],
                          allow_older=True)
        self.assertIn("parse-failed", str(caught.exception))
        # B is retained - explicitly as displaced history, not as latest.
        previous = store.previous_observed()
        self.assertEqual(previous["normalized_sha256"],
                         good["normalized_sha256"])
        # The failure's own evidence is preserved for reproduction.
        names = os.listdir(store.observed_evidence)
        self.assertTrue(any("capabilities" in name for name in names))
        # And the failure is visible wherever freshness is consulted.
        attention = store.freshness()["attention"]
        self.assertTrue(any("could not be parsed" in item
                            for item in attention), attention)

    def test_deleted_observed_evidence_blocks_promotion(self):
        store = _approved_store("delev")
        snapshot, _problem = acquire.acquire("jlcpcb", store.root,
                                             fetcher=_fetcher())
        target = [source for source in snapshot["sources"]
                  if source["id"] == "impedance"][0]
        os.unlink(os.path.join(
            store.observed_evidence,
            "impedance-{}.raw".format(target["sha256_raw"][:12])))
        with self.assertRaises(StoreError) as caught:
            store.promote(snapshot["normalized_sha256"][:12], [])
        self.assertIn("evidence", str(caught.exception))

    def test_altered_observed_evidence_blocks_promotion(self):
        store = _approved_store("altev")
        snapshot, _problem = acquire.acquire("jlcpcb", store.root,
                                             fetcher=_fetcher())
        target = [source for source in snapshot["sources"]
                  if source["id"] == "capabilities"][0]
        path = os.path.join(
            store.observed_evidence,
            "capabilities-{}.raw".format(target["sha256_raw"][:12]))
        with open(path, "ab") as handle:
            handle.write(b"<!-- tampered -->")
        with self.assertRaises(StoreError) as caught:
            store.promote(snapshot["normalized_sha256"][:12], [])
        self.assertIn("not the bytes", str(caught.exception).replace(
            "are not the bytes", "not the bytes"))

    def test_wrong_evidence_under_the_right_name_is_detected(self):
        store = _approved_store("wrongev")
        snapshot, _problem = acquire.acquire("jlcpcb", store.root,
                                             fetcher=_fetcher())
        sources = {source["id"]: source for source in snapshot["sources"]}
        name = "impedance-{}.raw".format(
            sources["impedance"]["sha256_raw"][:12])
        other = "capabilities-{}.raw".format(
            sources["capabilities"]["sha256_raw"][:12])
        shutil.copyfile(os.path.join(store.observed_evidence, other),
                        os.path.join(store.observed_evidence, name))
        with self.assertRaises(StoreError):
            store.observed()

    def test_missing_approved_evidence_is_detected(self):
        store = _approved_store("apprev")
        victim = os.listdir(store.approved_evidence)[0]
        os.unlink(os.path.join(store.approved_evidence, victim))
        with self.assertRaises(StoreError) as caught:
            store.approved()
        self.assertIn("proof does not", str(caught.exception))
        self.assertEqual(store.freshness()["state"], "unusable")

    def test_a_corrupt_observation_cannot_become_approved(self):
        store = _approved_store("corrupt")
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())
        snapshot = _rewrite_json(
            store.observed_path,
            lambda d: d["normalized"]["materials"].__setitem__(
                "core", dict(d["normalized"]["materials"]["core"], dk=99.0)))
        with self.assertRaises(StoreError) as caught:
            store.observed()
        self.assertIn("altered or corrupted", str(caught.exception))
        with self.assertRaises(StoreError):
            store.promote(snapshot["normalized_sha256"][:12], [])

    def test_an_unsupported_snapshot_schema_refuses(self):
        store = _approved_store("schema")
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())
        _rewrite_json(store.observed_path,
                      lambda d: d.__setitem__("schema_version", 99))
        with self.assertRaises(StoreError) as caught:
            store.observed()
        self.assertIn("schema_version", str(caught.exception))

    def test_a_schema_1_snapshot_is_still_understood(self):
        """The previous release wrote schema 1 with a `complete` boolean;
        history written under the old rules must stay readable."""
        store = _approved_store("compat")
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())

        def downgrade(document):
            document["schema_version"] = 1
            document["complete"] = True
            del document["outcome"]
        _rewrite_json(store.observed_path, downgrade)
        observed = store.observed()
        self.assertEqual(observed["outcome"], "complete")

    def test_an_unsupported_catalog_schema_refuses(self):
        catalog = jlcpcb.parse(_raw_sources())
        catalog["schema_version"] = 99
        with self.assertRaises(model.CatalogError):
            model.validate_catalog(catalog)

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
        snapshot = _rewrite_json(
            store.observed_path,
            lambda d: d.__setitem__("retrieved_utc",
                                    "2020-01-01T00:00:00+00:00"))
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

    def test_the_parser_identity_travels_with_every_snapshot(self):
        store = _approved_store("parserid")
        approved = store.approved()
        self.assertEqual(approved["parser"]["version"],
                         jlcpcb.PARSER_VERSION)
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher())
        _rewrite_json(
            store.observed_path,
            lambda d: d["parser"].__setitem__("version",
                                              "999-experimental"))
        attention = store.freshness()["attention"]
        self.assertTrue(any("parser identity differs" in item
                            for item in attention), attention)


# ---------------------------------------------------------------------------
# freshness: verified-current versus approved-version
# ---------------------------------------------------------------------------

class FreshnessIsVerificationAge(unittest.TestCase):

    @staticmethod
    def _age_approved(store, days):
        when = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=days)).isoformat()
        _rewrite_json(store.approved_path,
                      lambda d: d.__setitem__("retrieved_utc", when))

    def test_an_identical_refresh_renews_freshness_without_promotion(self):
        store = _approved_store("renew")
        digest_before = store.approved()["normalized_sha256"]
        self._age_approved(store, 40)
        self.assertEqual(store.freshness()["state"], "stale")
        snapshot, problem = acquire.acquire("jlcpcb", store.root,
                                            fetcher=_fetcher())
        self.assertIsNone(problem)
        store.record_verification(snapshot)
        after = store.freshness()
        self.assertEqual(after["state"], "current")
        self.assertIn("renewed_by_verification_utc", after)
        # The approved semantic version did not move one bit.
        self.assertEqual(store.approved()["normalized_sha256"],
                         digest_before)

    def test_a_differing_observation_cannot_be_recorded_as_verification(
            self):
        store = _approved_store("noverify")
        snapshot, _problem = acquire.acquire("jlcpcb", store.root,
                                             fetcher=_fetcher())
        changed = copy.deepcopy(snapshot["normalized"])
        changed["materials"]["core"]["dk"] = 5.0
        forged = dict(snapshot, normalized=changed,
                      normalized_sha256=model.normalized_digest(changed))
        with self.assertRaises(StoreError) as caught:
            store.record_verification(forged)
        self.assertIn("reviewed and promoted", str(caught.exception))

    def test_a_verification_of_a_previous_approval_does_not_carry_over(
            self):
        """Promote a new baseline: verifications recorded against the old
        digest no longer vouch for anything."""
        store = _approved_store("carry")
        snapshot, _problem = acquire.acquire("jlcpcb", store.root,
                                             fetcher=_fetcher())
        store.record_verification(snapshot)
        _rewrite_json(
            store.verification_path,
            lambda d: d.__setitem__("approved_normalized_sha256",
                                    "not-the-current-approved-digest"))
        freshness = store.freshness()
        self.assertNotIn("renewed_by_verification_utc", freshness)

    def test_stale_plus_failed_refresh_stays_stale_and_says_why(self):
        store = _approved_store("stalefail")
        self._age_approved(store, 45)
        acquire.acquire("jlcpcb", store.root,
                        fetcher=_fetcher(fail=("impedance",)))
        freshness = store.freshness()
        self.assertEqual(freshness["state"], "stale")
        self.assertTrue(any("could not fetch" in item
                            for item in freshness["attention"]))

    def test_a_pending_differing_observation_is_visible(self):
        store = _approved_store("pending")
        raw = _raw_sources()
        raw["impedance"] = raw["impedance"].replace(
            b">1.065mm<", b">1.075mm<", 1)
        acquire.acquire("jlcpcb", store.root, fetcher=_fetcher(raw=raw))
        attention = store.freshness()["attention"]
        self.assertTrue(any("awaits review" in item for item in attention),
                        attention)

    def test_a_future_dated_verification_is_anomalous(self):
        store = _approved_store("clock")
        now = datetime.datetime.now(datetime.timezone.utc)
        earlier = now - datetime.timedelta(days=1)
        self.assertEqual(store.freshness(now=earlier)["state"], "anomalous")

    def test_fresh_and_unchanged_reads_current(self):
        store = _approved_store("fresh")
        freshness = store.freshness()
        self.assertEqual(freshness["state"], "current")
        self.assertEqual(freshness["attention"], [])


# ---------------------------------------------------------------------------
# concurrency: the store under simultaneous writers
# ---------------------------------------------------------------------------

class TheStoreSurvivesConcurrency(unittest.TestCase):

    def test_concurrent_refreshes_cannot_corrupt_the_observed_store(self):
        root = _root("simref")
        failures = []

        def worker():
            try:
                acquire.acquire("jlcpcb", root, fetcher=_fetcher())
            except Exception as exc:                  # noqa: BLE001 - record
                failures.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        store = CatalogStore(root, "jlcpcb")
        observed = store.observed()
        self.assertEqual(observed["outcome"], "complete")
        # No stray partial temp files survive the stampede.
        stray = [name for name in os.listdir(
            os.path.dirname(store.observed_path))
            if name.startswith(".partial-")]
        self.assertEqual(stray, [])

    def test_concurrent_promotions_cannot_double_approve(self):
        root = _root("simprom")
        snapshot, _problem = acquire.acquire("jlcpcb", root,
                                             fetcher=_fetcher())
        store = CatalogStore(root, "jlcpcb")
        outcomes = []

        def worker():
            local = CatalogStore(root, "jlcpcb")
            try:
                local.promote(snapshot["normalized_sha256"][:12], [],
                              initial=True)
                outcomes.append("promoted")
            except StoreError as exc:
                outcomes.append(str(exc))
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(o[:8] for o in outcomes),
                         ["an appro", "promoted"], outcomes)
        with open(store.promotions_path, encoding="utf-8") as handle:
            self.assertEqual(len(json.load(handle)), 1)
        self.assertEqual(store.approved()["normalized_sha256"],
                         snapshot["normalized_sha256"])

    def test_a_promotion_promotes_what_was_reviewed_or_refuses(self):
        """A refresh landing between review and promotion must not swap in
        different content under the reviewed digest prefix."""
        store = _approved_store("swap")
        raw = _raw_sources()
        raw["impedance"] = raw["impedance"].replace(
            b">1.065mm<", b">1.075mm<", 1)
        changed, _problem = acquire.acquire("jlcpcb", store.root,
                                            fetcher=_fetcher(raw=raw))
        # The reviewer reviewed the ORIGINAL digest; a newer differing
        # observation has since replaced latest. Promotion refuses.
        with self.assertRaises(StoreError) as caught:
            store.promote(store.approved()["normalized_sha256"][:12], [],
                          allow_older=True)
        self.assertIn("promote exactly what was reviewed",
                      str(caught.exception))
        # Promoting the digest that IS the newest observation works.
        store.promote(changed["normalized_sha256"][:12], [],
                      allow_older=True)


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

    def test_required_one_oz_inner_cannot_select_a_half_oz_construction(
            self):
        """The published constructions all describe the 0.5 oz-inner
        build. A profile at 1 oz inner is orderable - but no published
        construction describes it, so no stackup may be claimed."""
        result = selection.select(self.catalog,
                                  _requirements(inner_copper_oz=1.0))
        self.assertTrue(result["feasible"], result["rejections"])
        self.assertIsNone(result["stackup"])
        self.assertEqual(result["stackup_candidates"], [])
        self.assertTrue(any("different copper build" in e
                            for e in result["explanations"]),
                        result["explanations"])

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
        del catalog["capabilities"]["drill_diameter_multilayer_mm"]
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
            self.approved, "JLC-4L-no-requirement",
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
            self.approved, "JLC-4L-no-requirement",
            ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"])
        sources = {s["id"]: s for s in
                   document["generated_from"]["sources"]}
        self.assertTrue(sources["impedance"]["sha256_raw"])
        self.assertTrue(sources["copper-weight"]["sha256_raw"])
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
        same way the store verifies any snapshot - digests, evidence
        bytes and all."""
        store = CatalogStore(os.path.join(HERE, "profiles", "jlcpcb"),
                             "jlcpcb")
        approved = store.approved()
        self.assertIsNotNone(approved, "no committed approved baseline")
        self.assertTrue(approved["normalized"]["stackups"])
        problems = store_module.verify_evidence(approved,
                                                store.approved_evidence)
        self.assertEqual(problems, [])


if __name__ == "__main__":                                # pragma: no cover
    unittest.main()
