"""Behavior tests for scoped physical evidence and decision-changing checks."""
import json
import unittest

from space_condition import analyze_space


def record(source, sid, value, fact=None, date=None):
    return {"source": source, "source_id": sid, "value": value,
            "fact": fact or {"costar": "Building record", "matterport": "Linked scan", "vexcel": "Primary structure at CoStar pin"}[source],
            "captured_at": date, "status": "agrees", "evidence_url": None}


class PhysicalEvidence(unittest.TestCase):
    def test_tanner_scopes_remain_distinct_and_no_capacity_is_invented(self):
        records = [
            record("costar", "building", {"grossBuildingAreaSquareFeet": 69500,
                                           "propertyFlex": {"structural": {"ceilingHeightFeet": 18}}}),
            record("costar", "listing", {"lease": {"totalAvailableSquareFeet": 10000},
                                          "spaces": [{"suite": "A"}]}, "commercial-listing-summary", "2026-11-04"),
            record("matterport", "scan", {"rooms": [{"room": "Warehouse", "area_m2": 593, "estimated_height_m": 0},
                                                       {"room": "Office", "area_m2": 20, "estimated_height_m": 2.8}]}),
            record("vexcel", "structure", {"footprint.area": 6913, "roof-characteristics.condition": 2}, date="2025-12-31"),
        ]
        result = analyze_space(records, "indoor pickleball")
        text = " ".join(f["statement"] for f in result["findings"])
        self.assertIn("69,500 sq ft", text)
        self.assertIn("10,000 sq ft in total", text)
        self.assertIn("593 m²", text)
        self.assertIn("6,913 m²", text)
        self.assertIn("18 ft", text)
        self.assertIn("no usable height estimate", text)
        self.assertNotIn("2.8 m", text)
        self.assertIsNone(result["court_count"])
        self.assertFalse(result["fit_verified"])
        self.assertFalse(result["suite_match_verified"])
        self.assertEqual(result["actions"][0]["owner"], "Broker / landlord")
        roof = next(f for f in result["findings"] if f["field"] == "roof-characteristics.condition")
        self.assertIn("vendor classification", roof["statement"])
        self.assertEqual(roof["captured_at"], "2025-12-31")
        self.assertEqual(roof["source_ids"], ["structure"])
        listing = next(f for f in result["findings"] if f["scope"] == "listing total")
        self.assertIn("suite labels: A", listing["statement"])
        self.assertEqual(listing["captured_at"], "2026-11-04")

    def test_identity_agreement_does_not_prove_suite_match(self):
        result = analyze_space([], "fitness studio", {"conflicts": 0, "records": [{"status": "agrees"}]})
        self.assertFalse(result["suite_match_verified"])
        self.assertFalse(result["fit_verified"])

    def test_explicit_join_does_not_prove_clear_geometry(self):
        result = analyze_space([record("matterport", "scan", {"rooms": [{"room": "Hall", "area_m2": 1000, "estimated_height_m": 8}]})],
                               "pickleball", {"suite_match_verified": True})
        self.assertTrue(result["suite_match_verified"])
        self.assertFalse(result["fit_verified"])
        self.assertIsNone(result["court_count"])
        self.assertIn("does not establish unobstructed", result["findings"][1]["statement"])
        self.assertEqual(result["actions"][0]["title"], "Measure the layout before estimating capacity")

    def test_missing_and_malformed_measurements_never_become_facts(self):
        result = analyze_space([record("costar", "building", {"grossBuildingAreaSquareFeet": float("nan"),
                                                               "rentableBuildingAreaSquareFeet": -1,
                                                               "parking": [{"totalSpaces": True}]}),
                                record("matterport", "scan", {"rooms": [None, {"area_m2": "1000"}]}), None], "fitness")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["findings"], [])
        self.assertIn("No usable physical measurements", result["summary"])
        json.dumps(result, allow_nan=False)

    def test_zero_equipment_count_is_retained_but_not_diagnosed(self):
        result = analyze_space([record("vexcel", "roof", {"roof-objects.ac-unit.count": 0})], "restaurant")
        text = result["findings"][0]["statement"]
        self.assertIn("detects 0 rooftop AC units", text)
        self.assertIn("does not establish working condition", text)
        self.assertTrue(any("exhaust" in c for c in result["checklist"]))

    def test_parking_entries_are_not_summed_or_treated_as_suite_capacity(self):
        result = analyze_space([record("costar", "building", {"parking": [{"totalSpaces": 173}, {"totalSpaces": 20}],
                                                               "propertyFlex": {"electric": {"numElectricCurrentPhases": 3}},
                                                               "hasSprinklers": True})], "daycare")
        text = " ".join(f["statement"] for f in result["findings"])
        self.assertIn("173, 20 spaces by inventory entry", text)
        self.assertNotIn("193", text)
        self.assertIn("3-phase", text)
        self.assertIn("building scope", text)
        self.assertTrue(any("drop-off" in c for c in result["checklist"]))

    def test_suite_area_is_labelled_and_all_findings_are_citable(self):
        result = analyze_space([record("costar", "listing", {"spaces": [{"suite": "B", "availableSquareFeet": 2500}]},
                                                       "commercial-listing-summary")], "warehouse")
        self.assertIn("2,500 sq ft available for suite/space B", result["findings"][0]["statement"])
        for item in result["findings"]:
            self.assertEqual(item["source_ids"], ["listing"])
        self.assertTrue(all({"title", "why", "owner", "request", "source_ids"} <= set(a) for a in result["actions"]))


if __name__ == "__main__":
    unittest.main()
