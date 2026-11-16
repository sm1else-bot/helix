import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2.parent))

from stage2.contracts import validate_handoff
from stage2.reconciliation import compare_claims, reconcile_claims
from stage2.orchestrator import investigate
from stage2.providers import MatterportAdapter, VexcelAdapter
from stage2.vision import validate_visual_observation, analyze_one_image, analyze_matterport_panorama
from stage2.property_preflight import run_property_preflight


def handoff(**changes):
    data={"handoff_id":"h1","intake":{"total_budget_usd":500000},
      "selected_listing":{"property_id":236138,"listing_id":22141592,"space_id":20324034,
        "stable_id":"lease:22141592:20324034","address":"15603 W Hardy Rd","suite":"350","area_min_sqft":5941},
      "property_facts":{"property_id":236138,"address":"15603 W Hardy Rd","city":"Houston","state":"TX",
        "latitude":29.944369,"longitude":-95.385271,"property_type":"Flex","gross_building_area_sqft":46981,
        "rentable_building_area_sqft":46981,"stories":1,"matterport_model_ids":["NDMWnz1H9uV"]}}
    data.update(changes); return data


class HandoffTests(unittest.TestCase):
    def test_selected_suite_and_financial_context_are_preserved(self):
        x=validate_handoff(handoff())
        self.assertEqual(x["selected_listing"]["property_id"],"236138")
        self.assertEqual(x["selected_listing"]["listing_id"],"22141592")
        self.assertEqual(x["selected_listing"]["space_id"],"20324034")
        self.assertEqual(x["intake"]["total_budget_usd"],500000)

    def test_optional_identifiers_are_allowed(self):
        x=handoff(); del x["selected_listing"]["listing_id"]; del x["selected_listing"]["space_id"]
        self.assertIsNone(validate_handoff(x)["selected_listing"]["space_id"])

    def test_malformed_id_and_cross_property_facts_rejected(self):
        x=handoff(); x["selected_listing"]["property_id"]="bad id"
        with self.assertRaises(ValueError): validate_handoff(x)
        x=handoff(); x["property_facts"]["property_id"]=999
        with self.assertRaises(ValueError): validate_handoff(x)

    def test_bad_coordinates_rejected(self):
        x=handoff(); x["property_facts"]["latitude"]=float("nan")
        with self.assertRaises(ValueError): validate_handoff(x)


class ReconciliationTests(unittest.TestCase):
    def claim(self, **overrides):
        base=dict(claim_id="a",fact="area",value=3000,unit="sqft",source="costar",source_record_id="p1",
          scope_type="building",scope_id="b1",measurement_semantics="gross_floor_area",identity_key="prop:1",
          observed_at="2026-01-01",retrieved_at="2026-11-15")
        base.update(overrides); return base

    def test_agrees_and_genuine_conflict_same_scope_unit_time(self):
        a=self.claim(); b=self.claim(claim_id="b",source="matterport",source_record_id="m1",value=3000)
        self.assertEqual(compare_claims(a,b)["status"],"agrees")
        b["value"]=4100
        self.assertEqual(compare_claims(a,b)["status"],"conflict")

    def test_floor_area_vs_footprint_is_not_comparable(self):
        a=self.claim(); b=self.claim(claim_id="v",source="vexcel",value=3984,scope_type="structure",scope_id="v1",measurement_semantics="ground_footprint")
        self.assertEqual(compare_claims(a,b)["status"],"not_comparable")

    def test_different_buildings_and_suite_scopes_are_not_compared(self):
        a=self.claim(); b=self.claim(claim_id="m",identity_key="prop:other")
        self.assertTrue(compare_claims(a,b)["identity_conflict"])
        c=self.claim(claim_id="s",scope_type="suite",scope_id="20324034")
        self.assertEqual(compare_claims(a,c)["status"],"not_comparable")

    def test_same_source_duplicates_are_not_independent_comparisons(self):
        claims=[self.claim(claim_id=f"a{i}",source="costar",value=3000+i) for i in range(20)]
        self.assertEqual(reconcile_claims(claims),[])

    def test_missing_source_is_unknown_not_conflict(self):
        a=self.claim(value=None); b=self.claim(claim_id="b",source="vexcel",value=3000)
        self.assertEqual(compare_claims(a,b)["status"],"unknown")
        self.assertEqual(reconcile_claims([a]),[])

    def test_york_timeline_resolves_empty_scan_before_tenant_move_in(self):
        a=self.claim(claim_id="scan",fact="suite_occupancy",value="vacant",unit=None,scope_type="suite",scope_id="260",measurement_semantics="observed_occupancy",observed_at="2020-01-15")
        b=self.claim(claim_id="lease",fact="suite_occupancy",value="occupied",unit=None,source="costar",scope_type="suite",scope_id="260",measurement_semantics="observed_occupancy",observed_at="2020-07-01",extra={"move_in_date":"2020-07-01"})
        self.assertEqual(compare_claims(a,b)["status"],"resolved_by_timeline")

    def test_attachment_date_does_not_prove_scan_capture_for_suite_155(self):
        a=self.claim(claim_id="scan",fact="suite_occupancy",value="vacant",unit=None,scope_type="suite",scope_id="155",measurement_semantics="observed_occupancy",observed_at="2026-03-10",extra={"date_semantics":"costar_attachment_date"})
        b=self.claim(claim_id="lease",fact="suite_occupancy",value="occupied",unit=None,scope_type="suite",scope_id="155",measurement_semantics="observed_occupancy",extra={"move_out_date":"2026-07-23"})
        self.assertEqual(compare_claims(a,b)["status"],"possible_conflict")

    def test_grant_case_quarantines_identity_and_measurement_scope(self):
        fixture=json.loads((STAGE2/"fixtures/contradiction_cases.json").read_text())[0]
        self.assertEqual(compare_claims(fixture["costar_claim"],fixture["vexcel_claim"])["status"],"not_comparable")
        scan=self.claim(claim_id="scan",fact="scan_address",value=fixture["matterport_address"],identity_key="costar-property:grant-10601")
        current=self.claim(claim_id="building",fact="scan_address",value=fixture["address"],identity_key="costar-property:grant-10607")
        self.assertTrue(compare_claims(scan,current)["identity_conflict"])


class ProviderTests(unittest.TestCase):
    def test_matterport_exact_match_keeps_rooms_and_dates_scoped(self):
        model={"id":"NDMWnz1H9uV","name":"15603 W Hardy Rd","state":"active","visibility":"public",
          "created":"2026-11-10T00:00:00Z","modified":"2026-11-12T00:00:00Z",
          "floors":[{"id":"f1","label":"Floor 1"}],"rooms":[{"id":"r1","label":"Warehouse","dimensions":{"areaFloor":5002,"height":0,"units":"imperial"}}],
          "locations":[{"id":"loc1","position":{"x":1,"y":2,"z":0},"floor":{"id":"f1"},"room":{"id":"r1"},"panos":[{"id":"p1","skybox":{"id":"s1","status":"available","children":[]}}]}]}
        adapter=MatterportAdapter(); adapter._query=lambda *args:(200,{"data":{"model":model}})
        with patch.dict(os.environ,{"MATTERPORT_API_KEY":"private","MATTERPORT_API_TOKEN":"private","MATTERPORT_SDK_KEY":"public-sdk-key"}):
            result=adapter.collect(validate_handoff(handoff()))
        self.assertEqual(result["models"][0]["identity_status"],"matched")
        self.assertEqual(result["models"][0]["room_count"],1)
        self.assertIn("not camera capture dates",result["models"][0]["timestamps_interpretation"])
        self.assertEqual(result["claims"][0]["scope_type"],"room")
        self.assertEqual(result["models"][0]["showcase_url"],"https://my.matterport.com/show/?m=NDMWnz1H9uV")
        self.assertEqual(result["visual_analysis"]["status"],"unavailable")

    def test_string_address_lines_and_panorama_inventory_are_handled(self):
        model={"id":"NDMWnz1H9uV","name":"different model label","address":{"streetAddressLines":"15603 W Hardy Rd","locality":"Houston","administrativeArea":"TX"},
          "floors":[],"rooms":[],"locations":[{"id":f"loc{i}","room":{"id":f"r{i}"},"panos":[{"id":f"p{i}","skybox":{"status":"available"}}]} for i in range(10)]}
        adapter=MatterportAdapter(); adapter._query=lambda *args:(200,{"data":{"model":model}})
        with patch.dict(os.environ,{"MATTERPORT_API_KEY":"private","MATTERPORT_API_TOKEN":"private","MATTERPORT_SDK_KEY":"public-sdk-key"}):
            result=adapter.collect(validate_handoff(handoff()))
        self.assertEqual(result["models"][0]["identity_status"],"matched")
        self.assertEqual(result["models"][0]["panorama_count"],10)
        self.assertEqual(len(result["models"][0]["images"]),10)

    def test_wrong_address_model_is_quarantined(self):
        model={"id":"x","name":"10601 Grant Rd","visibility":"public","state":"active","floors":[],"rooms":[],"locations":[]}
        adapter=MatterportAdapter(); adapter._query=lambda *args:(200,{"data":{"model":model}})
        with patch.dict(os.environ,{"MATTERPORT_API_KEY":"private","MATTERPORT_API_TOKEN":"private"}):
            result=adapter.collect(validate_handoff(handoff(property_facts={**handoff()["property_facts"],"matterport_model_ids":["x"]})))
        self.assertEqual(result["models"][0]["identity_status"],"quarantined")
        self.assertIsNone(result["models"][0]["showcase_url"])
        self.assertEqual(result["claims"],[])

    def test_missing_matterport_is_unknown(self):
        x=handoff(); x["property_facts"]["matterport_model_ids"]=[]
        self.assertEqual(MatterportAdapter().collect(validate_handoff(x))["status"],"unknown")

    def test_vexcel_uses_key_header_and_categorizes_unauthorized(self):
        seen=[]
        def forbidden(req,timeout=0):
            seen.append(req.get_header("X-api-key"))
            raise HTTPError(req.full_url,401,"unauthorized",{},None)
        with patch.dict(os.environ,{"VEXCEL_API_KEY":"secret-value"}):
            result=VexcelAdapter(opener=forbidden).collect(validate_handoff(handoff()))
        self.assertEqual(result["status"],"unauthorized")
        self.assertTrue(all(v=="secret-value" for v in seen))
        self.assertNotIn("secret-value",json.dumps(result))

    def test_vexcel_retrieves_aerial_and_all_available_obliques(self):
        import tempfile
        from PIL import Image
        from io import BytesIO
        buf=BytesIO(); Image.new("RGB",(12,8),(90,120,80)).save(buf,format="JPEG"); image=buf.getvalue()
        features=[{"type":"Feature","geometry":{"type":"Polygon","coordinates":[[[-118.25,34.04],[-118.24,34.04],[-118.24,34.05],[-118.25,34.05],[-118.25,34.04]]]},"properties":{"metadata.collection":"la-2026","metadata.ortho-date-estimate":"2026-08-01","structure.count":1,"structures":[]}}]
        coverage=[{"properties":{"product-type":f"oblique-{d}","collection":"la-2026","last-capture-date":"2026-08-01"}} for d in ("north","south","east","west")]
        sequence=iter([(200,{"features":features}),(200,{"features":coverage}),(200,{"features":[]}),(200,{"features":[{"properties":{"collection":"la-2026","last-capture-date":"2026-08-01"}}]}),(200,{"features":[]}),(200,{"collection":"la-2026"})])
        adapter=VexcelAdapter(asset_dir=tempfile.mkdtemp()); adapter._get=lambda *args:next(sequence)
        calls=[]; adapter._post_image=lambda key,path,body:(calls.append((path,body)) or (200,"image/jpeg",image))
        with patch.dict(os.environ,{"VEXCEL_API_KEY":"private-vexcel"}),patch.dict(os.environ,{},clear=False):
            os.environ.pop("OPENROUTER_API_KEY",None)
            result=adapter.collect(validate_handoff(handoff()))
        gallery=result["operations"]["imagery"]
        self.assertEqual(len(calls),5)
        self.assertEqual([x[0] for x in calls].count("ortho/extract"),1)
        self.assertEqual([x[0] for x in calls].count("oriented/extract"),4)
        self.assertTrue(all(x[1]["srid"]==4326 and x[1]["wkt"].startswith("MULTIPOLYGON") is False for x in calls))
        self.assertEqual(len(gallery["views"]),5)
        self.assertEqual(gallery["views"][0]["width"],12)
        self.assertNotIn("private-vexcel",json.dumps(result))


class PreflightAndVisionTests(unittest.TestCase):
    def test_jayant_preflight_uses_coordinates_and_never_calls_business_checks(self):
        fake=SimpleNamespace(resolve_jurisdiction=lambda lat,lon:{"code":"harris_unincorporated","verified":True,"evidence_url":"https://census.test/point","county":"Harris County","place":None,"note":"Point is outside incorporated place."})
        with patch.dict(sys.modules,{"jurisdiction":fake}):
            result=run_property_preflight(validate_handoff(handoff()),handoff()["property_facts"])
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["report"]["jurisdiction"]["code"],"harris_unincorporated")
        self.assertFalse(result["business_specific_checks_invoked"])
        self.assertNotIn("check_business",dir(__import__("_jayant_compliance")))
        self.assertFalse(result["report"]["legal_approval"])

    def test_visual_claim_needs_image_provenance_and_rejects_compliance(self):
        item={"feature":"electrical_panel","evidence":"Panel visible on wall","status":"visible","bbox_xyxy_normalized":[.1,.2,.4,.6]}
        ref={"image_id":"model:location:pano:face0","model_id":"model","location_id":"location","evidence_ref":"pano face","captured_at":None}
        self.assertIsNotNone(validate_visual_observation(item,ref))
        self.assertIsNone(validate_visual_observation(item,None))
        self.assertIsNone(validate_visual_observation({**item,"evidence":"This building is code compliant."},ref))

    def test_visual_bbox_from_0_to_1000_grid_is_normalized(self):
        item={"feature":"flat roof","evidence":"Visible roof surface","status":"visible","bbox_xyxy_normalized":[180,230,850,760]}
        result=validate_visual_observation(item,{"image_id":"img"})
        self.assertEqual(result["bbox_xyxy_normalized"],[0.18,0.23,0.85,0.76])
        self.assertIsNone(validate_visual_observation({**item,"bbox_xyxy_normalized":[0,0,1001,900]},{"image_id":"img"}))

    def test_vision_runs_by_default_without_enable_flag(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":json.dumps({"observations":[{"feature":"entrance door","evidence":"A door is visible.","status":"visible","bbox_xyxy_normalized":[.1,.2,.3,.8]}]})}}],"usage":{"total_tokens":12}}).encode()
        calls=[]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"private"}):
            os.environ.pop("HELIX_STAGE2_VISION_ENABLED",None)
            result=analyze_one_image(b"jpeg","image/jpeg",{"image_id":"img"},opener=lambda req,**kw:(calls.append(req) or Response()))
        self.assertEqual(result["status"],"available")
        self.assertEqual(len(calls),1)
        self.assertEqual(result["observations"][0]["image_id"],"img")

    def test_vision_retries_malformed_json_once(self):
        class Response:
            def __init__(self,content): self.content=content
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":self.content}}]}).encode()
        calls=[]
        def opener(req,**kw):
            calls.append(req)
            content="not json" if len(calls)<3 else json.dumps({"observations":[{"feature":"door","evidence":"Door is visible.","status":"visible"}]})
            return Response(content)
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"private"}):
            result=analyze_one_image(b"jpeg","image/jpeg",{"image_id":"img"},opener=opener)
        self.assertEqual(len(calls),3)
        self.assertEqual(json.loads(calls[1].data).get("response_format"),{"type":"json_object"})
        self.assertEqual(result["status"],"available")
        self.assertEqual(len(result["observations"]),1)

    def test_matterport_panorama_analyzes_six_faces_in_one_grounded_call(self):
        from PIL import Image
        from io import BytesIO
        buf=BytesIO(); Image.new("RGB",(12,12),(20,40,60)).save(buf,format="JPEG"); jpeg=buf.getvalue()
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({"choices":[{"message":{"content":json.dumps({"observations":[{"feature":"door","evidence":"Door visible in tile.","face_index":3,"status":"visible","bbox_xyxy_normalized":[.1,.1,.4,.8]}]})}}],"usage":{"total_tokens":18}}).encode()
        calls=[]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"private"}):
            result=analyze_matterport_panorama([(jpeg,"image/jpeg") for _ in range(6)],{"image_id":"m:l:p","model_id":"m","location_id":"l","evidence_ref":"scan"},opener=lambda req,**kw:(calls.append(req) or Response()))
        self.assertEqual(len(calls),1)
        self.assertEqual(result["faces_analyzed"],6)
        self.assertEqual(result["observations"][0]["image_id"],"m:l:p:face4")


class OrchestrationTests(unittest.TestCase):
    def test_236138_fixture_builds_stage3_dossier_and_preserves_partial_sources(self):
        class C:
            def collect(self,h,service=None): return {"status":"available","facts":h["property_facts"],"claims":[],"listing_context":{"budget":500000}}
        class M:
            def collect(self,h): return {"status":"unknown","models":[],"claims":[],"reason":"fixture has no scan"}
        class V:
            def collect(self,h): return {"status":"not_covered","operations":{},"claims":[]}
        calls=[]
        def preflight(*args): calls.append(args); return {"status":"available","business_specific_checks_invoked":False,"report":{"status":"needs_verification","gates":["jurisdiction_unverified"],"legal_approval":False}}
        result=investigate(handoff(),costar_adapter=C(),matterport_adapter=M(),vexcel_adapter=V(),preflight_runner=preflight)
        d=result["dossier"]
        self.assertEqual(result["status"],"partial")
        self.assertEqual(d["selected_property"]["property_id"],"236138")
        self.assertEqual(d["stage3_handoff"]["stage1_context"]["intake"]["total_budget_usd"],500000)
        self.assertEqual(d["source_status"]["vexcel"]["status"],"not_covered")
        self.assertEqual(d["stage3_handoff"]["business_recommendations"],[])
        self.assertIsNone(d["stage3_handoff"]["financial_feasibility"])
        self.assertEqual(len(calls),1)

    def test_provider_failure_returns_partial_not_crash(self):
        class C:
            def collect(self,*args): return {"status":"temporarily_unavailable","facts":{},"claims":[]}
        class Broken:
            def collect(self,*args): raise TimeoutError()
        result=investigate(handoff(),costar_adapter=C(),matterport_adapter=Broken(),vexcel_adapter=Broken(),preflight_runner=lambda *a:{"status":"temporarily_unavailable","report":None})
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["dossier"]["source_status"]["matterport"]["status"],"temporarily_unavailable")


if __name__=="__main__": unittest.main()
