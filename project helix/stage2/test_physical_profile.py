import json
from pathlib import Path
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage2.physical_profile import build_physical_profile


class PhysicalProfileTests(unittest.TestCase):
    def test_saved_broadway_investigation_offline(self):
        path = Path(__file__).resolve().parents[1] / ".stage2-handoffs.json"
        if not path.exists():
            self.skipTest("Local saved investigation is unavailable")
        records = json.loads(path.read_text())
        choices = [r for r in records.values() if (r.get("dossier") or {}).get("selected_property", {}).get("property_id") == "4809147" and (r.get("dossier") or {}).get("visual_analysis", {}).get("observations")]
        if not choices:
            self.skipTest("Saved 1250 S Broadway investigation is unavailable")
        dossier = max(choices, key=lambda r: r.get("created_at", ""))["dossier"]
        with patch("socket.socket", side_effect=AssertionError("Network access is forbidden")):
            profile = build_physical_profile(dossier)
        self.assertEqual(profile["property"]["address"], "1250 S Broadway")
        self.assertEqual(profile["property"]["usable_area_sqft"]["min"], 7205)
        self.assertEqual(profile["property"]["rent"]["monthly_low_usd"], 10087)
        self.assertEqual(profile["physical_features"]["parking"]["status"], "observed")
        self.assertIsNone(profile["physical_features"]["parking"]["verified_capacity"])
        self.assertLess(len(json.dumps(profile).encode()), 10000)
        self.assertFalse(profile["compliance"]["legal_approval"])

    def test_missing_feature_never_means_absent(self):
        profile = build_physical_profile({"visual_analysis": {"matterport": {"status": "complete"}}})
        self.assertEqual(profile["physical_features"]["entrances"]["status"], "not_observed")
        self.assertEqual(profile["physical_features"]["parking"]["status"], "unknown")
        self.assertIn("does not mean absent", profile["status_semantics"]["not_observed"])

    def test_residential_obstructions_and_unverified_clearance_are_not_lost(self):
        dossier = {"selected_property": {"property_id": "1", "space_id": "2"},
            "reported_building_facts": {"costar": {"property_type": "Residential"}},
            "property_compliance_preflight": {"report": {"records": [{"fact": "clear_height_ft", "value": 20, "status": "unknown"}]}},
            "physical_observations": [{"kind": "direct_visual_observation", "provider": "Matterport", "status": "visible", "feature": "Uneven perimeter wall", "evidence": "Wall projects into room.", "evidence_ref": "scan:1", "image_id": "image:1"},
                                      {"kind": "direct_visual_observation", "provider": "Matterport", "status": "visible", "feature": "Office chair", "evidence": "Chair on floor.", "evidence_ref": "scan:2"}]}
        profile = build_physical_profile(dossier)
        self.assertEqual(profile["building"]["property_type"], "Residential")
        self.assertEqual(profile["physical_features"]["obstructions"]["evidence"][0]["evidence_ref"], "scan:1")
        self.assertEqual(profile["physical_features"]["furniture_and_fixtures"]["status"], "observed")
        self.assertEqual(profile["matching_constraints"]["clear_height_ft"], {"value": None, "status": "unknown"})
        self.assertEqual(profile["matching_constraints"]["clear_floor_dimensions_ft"]["status"], "unknown")
        self.assertEqual(profile["matching_constraints"]["visual_findings_suite_scope"], "not_established")
