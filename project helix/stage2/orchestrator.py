"""Stage 2 orchestration and dossier construction."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from .contracts import validate_handoff
from .providers import CoStarAdapter, MatterportAdapter, VexcelAdapter
from .property_preflight import run_property_preflight
from .reconciliation import reconcile_claims


def _asmit_reconciliation(costar_result, handoff):
    """Call Asmit/Jayant's shared deterministic reconciliation, without its network collectors."""
    prop=costar_result.get("raw_property_record")
    if not isinstance(prop,dict): return []
    try:
        import sys
        from pathlib import Path
        playground=Path(__file__).resolve().parents[2]/"playground"
        if str(playground) not in sys.path: sys.path.insert(0,str(playground))
        from reconcile import reconcile_evidence
        selected=handoff.get("selected_listing") or {}
        result=reconcile_evidence(prop,selected_suite=selected.get("suite"),tenancies=costar_result.get("tenancies"))
    except Exception:
        return []
    findings=[]
    for record in result.get("records",[]):
        status=record.get("status")
        if record.get("fact")=="suite_occupancy_at_scan":
            if status=="conflict": status="possible_conflict"  # attachment date is not camera capture
            elif status=="agrees" and ("moved in after the scan" in record.get("note","") or "had left before" in record.get("note","")):
                status="resolved_by_timeline"
        elif record.get("fact") in ("scan_address","scan_suite") and status=="conflict":
            status="possible_conflict"  # quarantine a likely wrong-building link; do not transfer its facts
        if status in ("conflict","possible_conflict","not_comparable","resolved_by_timeline"):
            findings.append({"fact":record.get("fact"),"status":status,"evidence_ids":[record.get("source_id")],
                "source":record.get("source"),"reason":record.get("note"),"needs_human":record.get("needs_human",True),
                "source_capture_semantics":"CoStar attachment originationDate is attachment provenance, not verified camera capture" if record.get("fact") in ("scan_attachment_date","suite_occupancy_at_scan") else None})
    return findings


def _status_summary(value):
    result={"status":value.get("status","unknown"),"reason":value.get("reason"),"error":value.get("error")}
    if value.get("operations"):
        result["operations"]={name:{k:op.get(k) for k in ("status","http_status","feature_count") if k in op}
                              for name,op in value["operations"].items() if name not in ("imagery","imagery_visual_analysis")}
        dates=value["operations"].get("ortho_dates",{}).get("dates")
        if dates is not None: result["ortho_date_metadata"]=dates
        images=value["operations"].get("imagery",{})
        result["imagery"]={"status":images.get("status","unknown"),"views":images.get("views",[]),"errors":images.get("errors",[])}
        result["visual_analysis"]=value["operations"].get("imagery_visual_analysis",{"status":"unavailable","observations":[]})
        structures=value["operations"].get("property_extract",{}).get("structures_at_pin") or []
        result["selected_structure_candidates"]=[{"capture_estimate":s.get("capture_estimate"),
            "identity_status":s.get("identity_status"),"footprint_area_sqft":s.get("footprint_area_sqft"),
            "roof_signals":s.get("roof_signals"),"rooftop_objects":s.get("rooftop_objects"),"roof_defect_signals":s.get("roof_defect_signals")}
            for s in structures]
    return result


def _unknowns(costar, matterport, vexcel, compliance):
    result=[]
    for source, item in (("CoStar",costar),("Matterport",matterport),("Vexcel",vexcel)):
        if item.get("status")!="available": result.append({"topic":source,"status":item.get("status","unknown"),"note":item.get("reason") or item.get("error") or "Evidence unavailable."})
    if not any(m.get("identity_status")=="matched" for m in matterport.get("models",[])):
        result.append({"topic":"Matterport scan","status":"unknown","note":"No exact-address Matterport scan was available; interior layout and access have not been visually assessed."})
    imagery=(vexcel.get("operations") or {}).get("imagery") or {}
    if imagery.get("status") not in ("complete","partially_complete"):
        result.append({"topic":"Vexcel imagery","status":imagery.get("status","unknown"),"note":"Exterior imagery was not retrieved, so roof, parking and vehicle access were not visually assessed."})
    structures=(vexcel.get("operations") or {}).get("property_extract",{}).get("structures_at_pin") or []
    if structures and not any(s.get("pin_within_footprint") for s in structures):
        result.append({"topic":"Vexcel building match","status":"needs_verification","note":"The selected map point did not fall inside a returned Vexcel structure footprint; the nearest structure was not treated as this building."})
    report=compliance.get("report") or {}
    for gate in report.get("gates",[]):
        if gate in ("jurisdiction_unverified","identity_conflict","zoning_unknown","occupancy_unknown"):
            result.append({"topic":"Property preflight","status":"needs_verification","note":gate.replace("_"," ").capitalize()+"; check the linked authority record before relying on it."})
    return result


def investigate(payload, *, costar_service=None, costar_adapter=None, matterport_adapter=None, vexcel_adapter=None,
                preflight_runner=run_property_preflight, visual_result=None, progress=None):
    report=progress or (lambda *a, **k: None)
    handoff=validate_handoff(payload)
    # Resolve source identity and collect independently; an adapter outage never aborts the dossier.
    report("costar","Confirming selected CoStar property and listing evidence",source="CoStar")
    costar=(costar_adapter or CoStarAdapter()).collect(handoff,costar_service)
    report("matterport","Resolving the linked interior scan and its spatial coverage",source="Matterport")
    try: matterport=(matterport_adapter or MatterportAdapter(progress=progress)).collect({**handoff,"property_facts":costar.get("facts") or handoff.get("property_facts")})
    except Exception as error: matterport={"status":"temporarily_unavailable","error":type(error).__name__,"models":[],"claims":[]}
    report("vexcel","Checking current Vexcel coverage and retrieving selected-property images",source="Vexcel")
    try: vexcel=(vexcel_adapter or VexcelAdapter(progress=progress)).collect({**handoff,"property_facts":costar.get("facts") or handoff.get("property_facts")})
    except Exception as error: vexcel={"status":"temporarily_unavailable","error":type(error).__name__,"operations":{},"claims":[]}
    claims=list(costar.get("claims") or [])+list(matterport.get("claims") or [])+list(vexcel.get("claims") or [])
    asmit_findings=_asmit_reconciliation(costar,handoff)
    contradictions=reconcile_claims(claims)+asmit_findings
    # Building floor area and a ground footprint are different measurements.
    for area in [c for c in claims if c.get("fact")=="gross_building_area_sqft"]:
        for footprint in [c for c in claims if c.get("fact")=="vexcel_structure_footprint_area"]:
            contradictions.append({"fact":"gross_building_area_sqft_vs_vexcel_structure_footprint_area","status":"not_comparable",
                "evidence_ids":[area["claim_id"],footprint["claim_id"]],
                "reason":"CoStar gross floor area and Vexcel ground footprint area differ in measurement semantics. A parcel may also contain multiple structures; do not interpret the numeric difference as an area contradiction."})
    for model in matterport.get("models",[]):
        if model.get("identity_status")=="quarantined":
            contradictions.append({"fact":"matterport_model_identity","status":"possible_conflict",
                "evidence_ids":[model.get("model_id")],"identity_conflict":True,
                "reason":f"Matterport model label {model.get('name')!r} does not exactly match the selected CoStar address; its measurements are quarantined."})
    identity_conflict=any(v.get("identity_conflict") for v in contradictions)
    report("preflight","Running coordinate-based property compliance preflight",source="Jurisdiction")
    try: compliance=preflight_runner(handoff,costar.get("facts") or handoff.get("property_facts"),identity_conflict)
    except Exception as error: compliance={"status":"temporarily_unavailable","report":None,"business_specific_checks_invoked":False,"error":type(error).__name__}
    facts=costar.get("facts") or handoff.get("property_facts") or {}
    physical=[]
    for claim in claims:
        if claim.get("source")=="vexcel" and claim.get("value") is not None and claim.get("fact") not in ("structure_count",):
            physical.append({"kind":"provider_reported_physical_fact","fact":claim["fact"],"value":claim["value"],"unit":claim["unit"],"scope":claim["scope_type"],
                "evidence_ids":[claim["claim_id"]],"source":claim["source"],"status":"reported_by_provider","evidence_ref":claim.get("evidence_ref")})
    mp_visual=matterport.get("visual_analysis") or {"status":"unavailable","observations":[]}
    vx_visual=(vexcel.get("operations") or {}).get("imagery_visual_analysis") or {"status":"unavailable","observations":[]}
    visual_result=visual_result or {"status":"complete" if mp_visual.get("status")=="complete" and vx_visual.get("status")=="complete" else "partially_complete",
        "model":"qwen/qwen3.8-flash","matterport":mp_visual,"vexcel":vx_visual,
        "observations":(mp_visual.get("observations") or [])+(vx_visual.get("observations") or [])}
    image_by_id={v.get("source_record_id"):v for v in ((vexcel.get("operations") or {}).get("imagery") or {}).get("views",[])}
    for item in visual_result.get("observations",[]):
        visual_item={**item}
        if item.get("provider")=="Vexcel":
            visual_item["image_url"]=(image_by_id.get(item.get("image_id")) or {}).get("image_url")
            visual_item["source_record_id"]=item.get("image_id")
        else:
            visual_item.setdefault("provider","Matterport")
        physical.append({"kind":"direct_visual_observation",**visual_item,"evidence_ids":[item.get("image_id") or item.get("evidence_ref")]})
    report("synthesis","Building the evidence-backed physical profile",source="Helix")
    report("complete","Investigation dossier is ready",source="Helix")
    retrieved=datetime.now(timezone.utc).isoformat()
    selected={"property_id":handoff["selected_listing"]["property_id"],"listing_id":handoff["selected_listing"].get("listing_id"),
              "space_id":handoff["selected_listing"].get("space_id"),"stable_id":handoff["selected_listing"].get("stable_id"),
              "address":handoff["selected_listing"]["address"],"city":facts.get("city"),"state":facts.get("state"),
              "latitude":facts.get("latitude"),"longitude":facts.get("longitude")}
    dossier={"schema_version":"helix.investigation.v1","investigation_id":uuid.uuid4().hex,
      "created_at":retrieved,"selected_property":selected,
      "source_status":{"costar":_status_summary(costar),"matterport":_status_summary(matterport),"vexcel":_status_summary(vexcel)},
      "identity_resolution":{"status":"conflict" if identity_conflict else "resolved_to_selected_costar_property","canonical_identity":f"costar-property:{selected['property_id']}",
          "matterport_models":[{"model_id":m.get("model_id"),"status":m.get("identity_status"),"name":m.get("name"),
              "floor_count":m.get("floor_count"),"room_count":m.get("room_count"),"panorama_count":m.get("panorama_count"),
              "available_panorama_count":m.get("available_panorama_count"),"showcase_url":m.get("showcase_url"),"embed_status":m.get("embed_status"),
              "images":m.get("images",[])} for m in matterport.get("models",[])],
          "vexcel_geometry_scope":"parcel candidate; no building-level identity established"},
      "reported_building_facts":{"costar":facts,"listing_context":costar.get("listing_context"),"evidence_claims":claims,
          "asmit_reconciliation_records":asmit_findings},
      "physical_observations":physical,"physical_capabilities":_physical_capabilities(physical),"visual_analysis":visual_result,
      "contradictions":[v for v in contradictions if v.get("status") in ("conflict","possible_conflict")],
      "technical_comparisons":[v for v in contradictions if v.get("status")=="not_comparable"],
      "resolved_discrepancies":[v for v in contradictions if v.get("status")=="resolved_by_timeline"],
      "property_compliance_preflight":compliance,
      "unknowns":_unknowns(costar,matterport,vexcel,compliance),
      "verification_tasks":[{"task":item["note"],"evidence_ids":[]} for item in _unknowns(costar,matterport,vexcel,compliance) if item.get("status")=="needs_verification"],
      "provenance":claims,"stage3_handoff":{"schema_version":"helix.stage3-input.v1","selected_property":selected,
          "stage1_context":{"intake":handoff.get("intake"),"selected_listing":handoff.get("selected_listing")},
          "physical_facts":physical,"physical_capabilities":_physical_capabilities(physical),"visual_evidence":visual_result.get("observations",[]),
          "physical_unknowns":_unknowns(costar,matterport,vexcel,compliance),
          "source_coverage":{"costar":costar.get("status"),"matterport":matterport.get("status"),"vexcel":vexcel.get("status")},
          "contradictions":contradictions,"unknowns":_unknowns(costar,matterport,vexcel,compliance),
          "property_compliance_preflight":compliance,"source_status":{"costar":costar.get("status"),"matterport":matterport.get("status"),"vexcel":vexcel.get("status")},
          "business_recommendations":[],"financial_feasibility":None}}
    matched=any(m.get("identity_status")=="matched" and m.get("embed_status")=="configured" for m in matterport.get("models",[]))
    complete=(costar.get("status")=="available" and matched and mp_visual.get("status")=="complete"
              and vx_visual.get("status")=="complete" and compliance.get("status")=="available")
    return {"status":"complete" if complete else "partial","dossier":dossier}


def _physical_capabilities(items):
    groups={"interior_layout":[],"entrances_and_access":[],"circulation":[],"loading_and_vehicle_access":[],
      "visible_utilities":[],"ceiling_and_space_configuration":[],"parking_and_exterior":[],"building_geometry":[]}
    for item in items:
        label=" ".join(str(item.get(k) or "") for k in ("feature","fact","evidence")).casefold()
        if any(w in label for w in ("loading","dock","vehicle access","driveway")): group="loading_and_vehicle_access"
        elif any(w in label for w in ("parking","car park","lot area")): group="parking_and_exterior"
        elif any(w in label for w in ("entrance","entry door","access door","door")): group="entrances_and_access"
        elif any(w in label for w in ("hallway","corridor","circulation","stair")): group="circulation"
        elif any(w in label for w in ("electrical","conduit","panel","hvac","duct","plumbing","fixture")): group="visible_utilities"
        elif any(w in label for w in ("ceiling","beam","open area","partition","interior layout")): group="ceiling_and_space_configuration"
        elif any(w in label for w in ("footprint","roof","stories","geometry","structure")): group="building_geometry"
        else: group="interior_layout" if item.get("source")=="matterport" else "parking_and_exterior"
        groups[group].append({**item,"status":item.get("status") or ("observed_present" if item.get("kind")=="direct_visual_observation" else "reported_by_provider")})
    return groups
