"""Compact, deterministic Stage 3 profile derived from an existing dossier."""
import re


FEATURES = {
    "parking": (r"\bparking\b|\bcar park\b", "vexcel"),
    "interior_layout": (r"\bpartition|\bcorridor|\bhallway|\bopen (?:area|space)|\bcolumn", "matterport"),
    "furniture_and_fixtures": (r"\bchair|\bdesk|\bshelv|\bworkbench|\bfixture|\btable|\bcabinet", "matterport"),
    "entrances": (r"\bentrance|\bentry|\bdoorway|\bdoor\b", "matterport"),
    "loading_access": (r"\bloading|\bdock\b|\broll[ -]?up|\bdrive[ -]?in", "either"),
    "possible_emergency_exits": (r"\bemergency exit|\bexit sign|\bfire exit", "matterport"),
    "utilities": (r"\bhvac|\bduct|\belectrical|\bconduit|\bpipe|\bplumbing|\bsprinkler", "matterport"),
    "ceiling": (r"\bceiling|\bbeam", "matterport"),
    "building_form": (r"\broof|\bbuilding configuration", "vexcel"),
    "obstructions": (r"\bcolumn|\bpartition|\bwall|\bshelv|\bstair|\bobstruction|\buneven", "matterport"),
}


def build_physical_profile(dossier):
    """Build a small profile without I/O; the dossier remains the evidence source."""
    selected = dossier.get("selected_property") or {}
    reported = dossier.get("reported_building_facts") or {}
    facts, listing = reported.get("costar") or {}, reported.get("listing_context") or {}
    rent = listing.get("rent_normalization") or {}
    preflight = dossier.get("property_compliance_preflight") or {}
    report = preflight.get("report") or {}
    jurisdiction = report.get("jurisdiction") or {}
    visual = dossier.get("visual_analysis") or {}
    observations = dossier.get("physical_observations") or []
    categories = {}
    for name, (pattern, provider) in FEATURES.items():
        matches = []
        evidence = []
        for item in observations:
            if item.get("kind") != "direct_visual_observation" or item.get("status") != "visible":
                continue
            source = (item.get("provider") or "").lower()
            if provider != "either" and source != provider:
                continue
            feature = item.get("feature") or ""
            if re.search(pattern, feature, re.I) and feature.casefold() not in {x.casefold() for x in matches}:
                matches.append(feature[:100])
                if len(evidence) < 2:
                    evidence.append({"feature": feature[:100], "description": str(item.get("evidence") or "")[:140],
                        "provider": item.get("provider") or "unknown", "scope": item.get("scope_type") or "not_established",
                        "evidence_ref": item.get("evidence_ref"), "image_id": item.get("image_id")})
        coverage = (visual.get(provider) or {}).get("status") if provider != "either" else None
        if provider == "either":
            coverage = "complete" if all((visual.get(p) or {}).get("status") == "complete" for p in ("matterport", "vexcel")) else None
        categories[name] = {"status": "observed" if matches else "not_observed" if coverage == "complete" else "unknown", "features": matches[:8], "evidence": evidence}
    records = report.get("records") or []
    verified_parking = next((r.get("value") for r in records if r.get("fact") == "parking_spaces" and r.get("status") == "verified" and isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool)), None)
    verified_height = next((r.get("value") for r in records if r.get("fact") == "clear_height_ft" and r.get("status") == "verified" and isinstance(r.get("value"), (int, float)) and not isinstance(r.get("value"), bool)), None)
    categories["parking"]["verified_capacity"] = verified_parking
    unknowns = []
    for label, missing in (("Verified usable area", True), ("Clear floor dimensions and unobstructed area", True), ("Wall geometry", True), ("Furniture and partition removability", True), ("Selected suite attribution of visual findings", True), ("Rent", rent.get("monthly_low_usd") is None), ("Parking capacity", verified_parking is None), ("Ceiling height", verified_height is None), ("Utility capacity", True), ("Emergency exit adequacy", True), ("Use-specific compliance", True)):
        if missing: unknowns.append(label)
    for item in dossier.get("unknowns") or []:
        topic = item.get("topic")
        if topic and topic not in unknowns: unknowns.append(str(topic)[:120])
    return {
        "schema_version": "helix.stage3-physical-profile.v1",
        "property": {"property_id": selected.get("property_id"), "address": selected.get("address"), "city": selected.get("city"), "state": selected.get("state"), "space_id": selected.get("space_id"),
            "usable_area_sqft": {"min": listing.get("area_min_sqft"), "max": listing.get("area_max_sqft"), "status": "listed" if listing.get("area_min_sqft") is not None else "unknown"},
            "rent": {"monthly_low_usd": rent.get("monthly_low_usd"), "monthly_high_usd": rent.get("monthly_high_usd"), "status": rent.get("status", "unknown"), "total_occupancy_cost_status": rent.get("total_occupancy_cost_status", "unknown")}},
        "building": {"property_type": facts.get("property_type"), "property_subtype": facts.get("property_subtype"), "gross_area_sqft": facts.get("gross_building_area_sqft"), "rentable_area_sqft": facts.get("rentable_building_area_sqft"), "stories": facts.get("stories")},
        "matching_constraints": {
            "clear_height_ft": {"value": verified_height, "status": "verified" if verified_height is not None else "unknown"},
            "unobstructed_area_sqft": {"value": None, "status": "unknown"},
            "clear_floor_dimensions_ft": {"length": None, "width": None, "status": "unknown"},
            "perimeter_wall_geometry": {"status": "unknown"},
            "furniture_and_partition_removability": {"status": "unknown"},
            "visual_findings_suite_scope": "not_established",
            "guidance": "Screen using reported facts and observed obstacles; confirm required clear dimensions, use permissions and suite scope before declaring a business feasible."
        },
        "physical_features": categories,
        "compliance": {"jurisdiction": jurisdiction.get("place") or jurisdiction.get("code"), "county": jurisdiction.get("county"), "jurisdiction_verified": jurisdiction.get("verified"), "preflight_status": report.get("status") or preflight.get("status", "unknown"), "legal_approval": report.get("legal_approval"), "use_specific_status": "not_assessed"},
        "unknowns": unknowns,
        "status_semantics": {"not_observed": "Not seen in analyzed views; does not mean absent.", "unknown": "Not assessed or insufficient coverage."},
    }
