import sys
import unittest
import json
import tempfile
from zipfile import ZipFile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from discovery import (  # noqa: E402
    apply_allocation_and_horizon, classify_sale_price, make_discovery_response,
    normalize_rent, policy_for_intake,
)
from intake import build_intake  # noqa: E402
from costar_service import CoStarService  # noqa: E402
from discovery import nemotron_explanations  # noqa: E402
from discovery import reconcile_verification  # noqa: E402
from discovery import confidence_assessment  # noqa: E402


def intake(budget=350_000, **updates):
    payload = {
        "city": "Houston, TX", "total_budget_usd": budget,
        "financing": "cash", "business_preferences": "",
        "open_to_ideas": True,
    }
    payload.update(updates)
    return build_intake(payload)


def listing(stable_id="lease:1:1", transaction_type="lease", **overrides):
    raw = {
        "stable_id": stable_id, "transaction_type": transaction_type,
        "property_id": 11, "listing_id": 22, "space_id": 33,
        "property": {"address": "10 Main St", "city": "Houston", "state": "TX"},
        "property_type_code": 2, "listing_status": 1,
        "area_min_sqft": 5000, "area_max_sqft": 5000,
        "monetary_unit": 1, "rate_low": 2, "rate_high": 2,
        "rate_basis_code": 1, "asking_rate_has_value": True,
        "certified_fresh_date": "2026-11-01", "source_reference": "feed#22",
    }
    raw.update(overrides)
    return raw


class PolicyTests(unittest.TestCase):
    def test_costar_suite_name_is_carried_to_the_listing(self):
        named = make_discovery_response(intake(500_000), [listing(suite="350")])["results"][0]
        unnamed = make_discovery_response(intake(500_000), [listing()])["results"][0]
        self.assertEqual(named["suite"], "350")
        self.assertIsNone(unnamed["suite"])

    def test_sale_gate_threshold(self):
        low_la = policy_for_intake(intake(899_999, city="Los Angeles, CA"))
        self.assertEqual(low_la["transaction_types"], ["lease"])
        self.assertIsNone(low_la["maximum_sale_price_usd"])
        policy = policy_for_intake(intake(900_000, city="Los Angeles, CA"))
        self.assertEqual(policy["transaction_types"], ["sale", "lease"])
        self.assertEqual(policy["maximum_sale_price_usd"], 990_000)
        self.assertEqual(policy_for_intake(intake(350_000))["transaction_types"], ["sale", "lease"])

    def test_sale_classes_and_unknown(self):
        self.assertEqual(classify_sale_price(1_000_000, 1_000_000, 1_100_000)["classification"], "within_budget")
        self.assertEqual(classify_sale_price(1_050_000, 1_000_000, 1_100_000)["classification"], "stretch")
        self.assertEqual(classify_sale_price(1_100_001, 1_000_000, 1_100_000)["classification"], "over_budget")
        self.assertEqual(classify_sale_price(None, 1_000_000, 1_100_000)["classification"], "unknown")

    def test_lease_caps_and_reclassification(self):
        caps = policy_for_intake(intake())["monthly_rent_caps_usd"]
        self.assertEqual(caps, {"3": 7777.777777777777, "5": 4666.666666666667, "10": 2333.3333333333335})
        normalized = normalize_rent(listing())
        self.assertEqual(normalized.monthly_low_usd, 10_000)
        annual = normalize_rent(listing(rate_low=24, rate_high=24, rate_basis_code=2))
        self.assertEqual(annual.monthly_high_usd, 10_000)
        self.assertEqual(normalize_rent(listing(rate_basis_code=99)).status, "unknown")
        self.assertEqual(normalize_rent(listing(monetary_unit=2)).status, "unknown")
        self.assertEqual(normalize_rent(listing(area_min_sqft=None, area_max_sqft=None)).status, "unknown")
        self.assertEqual(normalized.total_occupancy_cost_status, "unknown")
        self.assertEqual(normalized.known_additional_charges, [])

    def test_invalid_rent_ranges_remain_unknown(self):
        for changes in ({"area_min_sqft": -5000, "area_max_sqft": -5000},
                        {"area_min_sqft": 6000, "area_max_sqft": 5000},
                        {"rate_low": 20, "rate_high": 2}):
            with self.subTest(changes=changes):
                normalized = normalize_rent(listing(**changes))
                self.assertEqual(normalized.status, "unknown")
                self.assertIsNone(normalized.monthly_low_usd)
                self.assertIsNone(normalized.monthly_high_usd)

    def test_confidence_uses_four_equal_factors_and_horizon(self):
        raw = listing(source_updated_at="2026-11-01")
        record = make_discovery_response(intake(500_000), [raw])["results"][0]
        score = record["confidence"]
        self.assertEqual(set(score["factor_weights_percent"].values()), {25})
        values = [score[k]["score"] for k in ("source_reliability", "data_completeness_consistency", "budget_fit", "property_search_fit")]
        self.assertEqual(score["overall_confidence"], round(sum(values) / 4))
        revised = apply_allocation_and_horizon([record], intake(500_000), 100, 10)[0]
        self.assertAlmostEqual(revised["confidence"]["budget_fit"]["score"], 100 * revised["monthly_rent_caps_usd"]["10"] / revised["rent_normalization"]["monthly_high_usd"], places=1)

    def test_duplicates_and_allocation(self):
        sale = listing("sale:22", "sale", space_id=None, rate_low=None, rate_high=None,
                       asking_price_low=1_050_000, asking_price_high=1_050_000)
        lease_a = listing("lease:22:33")
        lease_b = listing("lease:22:34", space_id=34)
        data = make_discovery_response(intake(1_000_000), [sale, lease_a, lease_a, lease_b])
        self.assertEqual(data["trace"]["deduplicated_count"], 3)
        self.assertEqual(data["trace"]["tavily_calls"], 0)
        sale_record = next(r for r in data["results"] if r["transaction_type"] == "sale")
        revised = apply_allocation_and_horizon([sale_record], intake(1_000_000), 80)
        self.assertEqual(revised[0]["sale_classification"], "over_budget")
        factors = revised[0]["confidence"]
        values = [factors[k]["score"] for k in ("source_reliability", "data_completeness_consistency", "budget_fit", "property_search_fit")]
        self.assertEqual(factors["overall_confidence"], round(sum(values) / 4))

    def test_sale_is_gated_and_preferences_optional(self):
        low = make_discovery_response(intake(899_999, city="Los Angeles, CA"), [listing("sale:22", "sale")])
        self.assertEqual(low["results"], [])
        self.assertEqual(policy_for_intake(intake(350_000, business_preferences="", open_to_ideas=False))["transaction_types"], ["sale", "lease"])

    def test_business_preferences_do_not_change_discovery_or_confidence(self):
        rows = [listing("lease:22:33"), listing("lease:22:34", space_id=34)]
        responses = [make_discovery_response(intake(500_000, business_preferences=pref, open_to_ideas=open_ideas), rows)
                     for pref, open_ideas in (("", False), ("movie theater", False), ("pickleball", False), ("", True))]
        signature = lambda response: [
            (r["stable_id"], r["budget_classification"], r["confidence"]["property_search_fit"], r["confidence"]["overall_confidence"])
            for r in response["results"]
        ]
        self.assertTrue(all(signature(responses[0]) == signature(response) for response in responses[1:]))

    def test_ranking_ties_are_reproducible(self):
        rows = [listing("lease:22:33"), listing("lease:23:34", property_id=12, listing_id=23, space_id=34)]
        first = make_discovery_response(intake(500_000), rows)
        second = make_discovery_response(intake(500_000), list(reversed(rows)))
        self.assertEqual([r["stable_id"] for r in first["results"]], [r["stable_id"] for r in second["results"]])

    def test_real_costar_shape_spaces_malformed_and_inactive_rows(self):
        props = "PropertyId,Address,City,State,MatterportModelId\n100,1 Main St,Houston,TX,\n"
        active = {
            "id": 200, "propertyId": 100, "listingStatus": 1, "propertyType": 2,
            "monetaryUnit": 1, "certifiedFreshDate": "2026-11-01",
            "forLeaseSummary": {"spaces": [
                {"id": 301, "propertyId": 100, "rateLow": 2, "rateHigh": 2,
                 "rateBasis": 1, "askingRateHasValue": True, "squareFeetMin": 5000, "squareFeetMax": 5000},
                {"id": 302, "propertyId": 100, "rateLow": 2, "rateHigh": 2,
                 "rateBasis": 1, "askingRateHasValue": True, "squareFeetMin": 6000, "squareFeetMax": 6000},
            ]},
            "forSaleSummary": {"saleStatus": 1, "askingPriceLow": 1_000_000,
                               "askingPriceHigh": 1_000_000, "totalAvailableSquareFeet": 5000},
        }
        inactive = {**active, "id": 201, "listingStatus": 2}
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "kit.zip"
            with ZipFile(archive, "w") as z:
                z.writestr("commercial-properties.csv", props)
                z.writestr("data/commercial-listing-summary.jsonl", "\n".join([
                    json.dumps(active), "{malformed", json.dumps(inactive), "",
                ]))
            service = CoStarService(str(archive), "unused")
            found = service.discover_active_listings("Houston", ["sale", "lease"], 20)
        self.assertEqual(len(found["listings"]), 3)
        self.assertEqual(len({x["stable_id"] for x in found["listings"]}), 3)
        self.assertEqual(found["sale_listing_records_seen"], 1)
        self.assertEqual(found["skipped_malformed_records"], 1)

    def test_nemotron_discards_unrecognized_ids_and_is_bounded(self):
        calls = []
        content = json.dumps({"explanations": [
            {"stable_id": "fake-id", "text": "Invented listing."},
            {"stable_id": "lease:22:33", "text": "CoStar records 5,000 SF."},
        ]})
        envelope = {"choices": [{"message": {"content": content}}]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(envelope).encode()
        sent = []
        def fake_urlopen(req, **kwargs):
            calls.append(1)
            sent.append(json.loads(req.data))
            return Response()
        with patch("discovery.urllib.request.urlopen", fake_urlopen):
            notes, trace = nemotron_explanations(
                [{"stable_id": "lease:22:33", "property_id": 11}],
                intake(), "test-key", "https://openrouter.ai/api/v1", "test-model")
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(notes), ["lease:22:33"])
        self.assertEqual(trace["model_calls"], 1)
        serialized = json.dumps(sent[0])
        self.assertNotIn("business_preferences", serialized)
        self.assertNotIn("open_to_ideas", serialized)

    def test_nemotron_empty_malformed_timeout_and_truncated_responses_fallback(self):
        cases = [
            (b"not-json", None),
            (json.dumps({"choices": [{"finish_reason": "length", "message": {"content": None}}]}).encode(), None),
            (json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}).encode(), None),
            (None, TimeoutError("timed out")),
        ]
        for body, error in cases:
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return body
            def fake_urlopen(*args, **kwargs):
                if error:
                    raise error
                return Response()
            with self.subTest(body=body, error=error), patch("discovery.urllib.request.urlopen", fake_urlopen):
                notes, trace = nemotron_explanations(
                    [{"stable_id": "lease:22:33", "property_id": 11}], intake(),
                    "test-key", "https://openrouter.ai/api/v1", "test-model")
                self.assertEqual(notes, {})
                self.assertEqual(trace["status"], "deterministic_fallback")
                self.assertEqual(trace["model_calls"], 1)

    def test_verification_preserves_candidate_conflicting_values(self):
        record = {
            "stable_id": "sale:22", "listing_id": "22", "property_id": 11,
            "address": "10 Main St", "transaction_type": "sale",
            "asking_price_usd": 1_000_000, "area_max_sqft": 5000,
        }
        result = reconcile_verification(record, [{
            "title": "10 Main Street property listing", "url": "https://example.test/listing",
            "snippet": "Asking price $1,250,000 for 10 Main Street, 5,000 SF.",
            "source_date": "2026-11-01",
        }])
        claim = next(c for c in result["claims"] if c["claim_type"] == "asking_price")
        self.assertEqual(claim["verdict"], "conflict")
        self.assertEqual(claim["costar_value"], 1_000_000)
        self.assertEqual(claim["sources"][0]["external_value"], 1_250_000.0)
        self.assertEqual(result["independent_exact_claims"], 0)
        self.assertEqual(result["overall_status"], "conflict")


if __name__ == "__main__":
    unittest.main()
