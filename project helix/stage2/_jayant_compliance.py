"""Selected building-only functions from Jayant's PR #17, commit cb68f3d.

Business checks are intentionally excluded. Adapted module retains the existing preflight rule/output contract.
"""
from pathlib import Path
import sys
AUTHORITIES = {
    'houston': ('Houston Permitting Center', 'https://www.houstontx.gov/business/plan/permits-inspections.html', ['houstontx.gov']),
    'harris_unincorporated': ('Harris County Office of the County Engineer', 'https://oce.harriscountytx.gov/Services/Permits/Permits-A-to-Z/Commercial-Building', ['harriscountytx.gov', 'hctx.net']),
    'los_angeles': ('City of Los Angeles Building and Safety / Planning', 'https://dbs.lacity.gov/services/search-online-building-records', ['lacity.gov', 'ladbs.org']),
    'la_unincorporated': ('LA County Public Works / Planning', 'https://permits.lacounty.gov/permits/building-and-safety/', ['lacounty.gov']),
}

TOPICS = {
    'identity': 'Confirm parcel, building and selected suite against official records',
    'occupancy': 'Retrieve approved occupancy/use and proposed change-of-use requirements',
    'fire': 'Obtain fire review: exits, occupant load, alarms and suppression',
    'accessibility': 'Verify accessible route, entrances and restrooms with qualified review',
    'parking': 'Confirm allocated parking, access and applicable local requirements',
    'mep': 'Verify electrical, HVAC, plumbing capacity and required trade permits',
}

def record(fact, value=None, source='public:compliance-triage', url=None, note='', conflict=False):
    return dict(fact=fact, value=value, source=source, source_id=fact, captured_at=None,
                confidence=None, evidence_url=url, status='conflict' if conflict else 'unknown',
                needs_human=True, note=note)

def normalize_context(context):
    """Accept Helix investigate handoff or its selected_listing; retain provenance.

    Listing area ranges are not measured usable area. Never collapse them to a
    single area for physical/legal capacity checks.
    """
    selected = context.get('selected_listing')
    if selected is None and 'stable_id' in context:
        selected = context
    if selected is None:
        return dict(context)
    if not isinstance(selected, dict):
        raise ValueError('selected_listing must be an object')
    result = dict(context)
    result.update(property_id=selected.get('property_id'), listing_id=selected.get('listing_id'),
                  space_id=selected.get('space_id'), address=selected.get('address'),
                  scope=('space:' + str(selected['space_id'])) if selected.get('space_id') else 'building',
                  listing_evidence=selected)
    # Helix's normalized listing (discovery.normalize_listing) carries no coordinates; its property detail
    # (costar_service) does, as latitude/longitude, possibly nested under "property".
    # Helix's investigate handoff is {selected_listing, property_facts} (app.py); accept older nestings too.
    detail = next((d for d in (context.get('property_facts'), selected.get('property'), context.get('property'))
                   if isinstance(d, dict) and d), {})
    result['facts'] = dict(context.get('facts') or {})
    result['facts'].setdefault('property_type', selected.get('property_type') or detail.get('property_type') or 'unknown')
    if detail.get('stories') is not None:
        result['facts'].setdefault('floors', detail.get('stories'))
    for key, names in (('lat', ('lat', 'latitude')), ('lon', ('lon', 'longitude'))):
        if result.get(key) is None:
            result[key] = next((src[n] for src in (selected, detail) for n in names if src.get(n) is not None), None)
    if 'jurisdiction' not in result and 'jurisdiction' in selected:
        result['jurisdiction'] = selected['jurisdiction']
    result['listed_area_range_sqft'] = [selected.get('area_min_sqft'), selected.get('area_max_sqft')]
    return result

def resolve_context_jurisdiction(context):
    context = normalize_context(context)
    jurisdiction = context.get('jurisdiction') or {}
    if jurisdiction.get('verified') is True and jurisdiction.get('evidence_url') and jurisdiction.get('code') in AUTHORITIES:
        return context
    if (context.get('lat') is None or context.get('lon') is None) and context.get('property_id') is not None:
        try:  # Helix listings have a CoStar id but no pin: take the pin from the kit, never from the postal city
            context['lat'], context['lon'] = kit_pin(context['property_id'], context.get('kit'))
        except Exception:
            pass
    if context.get('lat') is not None and context.get('lon') is not None:
        try:
            from jurisdiction import resolve_jurisdiction
            context['jurisdiction'] = resolve_jurisdiction(context['lat'], context['lon'])
        except Exception:
            context['jurisdiction'] = dict(code=None, verified=False, evidence_url=None,
                                          note='Jurisdiction resolver unavailable; no postal-city inference')
    return context

def preflight(context):
    """Building stage; returns normalized scope, deterministic gates and handoff."""
    if not isinstance(context, dict):
        raise ValueError('context must be an object')
    context = resolve_context_jurisdiction(context)
    facts = context.get('facts') or {}
    records = []
    gates = []
    jurisdiction = context.get('jurisdiction') or {}
    code = jurisdiction.get('code')
    verified = jurisdiction.get('verified') is True and bool(jurisdiction.get('evidence_url'))
    if code not in AUTHORITIES or not verified:
        gates.append('jurisdiction_unverified')
    if not context.get('property_id') or not context.get('scope'):
        gates.append('identity_incomplete')
    if context.get('identity_conflict') is True:
        gates.append('identity_conflict')
    if str(facts.get('property_type') or '').lower() in {'residential', 'multifamily', 'apartment', 'homes'}:
        gates.append('commercial_conversion_unestablished')
    for name in ('area_sqft', 'clear_height_ft', 'floors', 'parking_spaces', 'hvac_capacity', 'fire_inspection'):
        value = facts.get(name)
        # Zero height/area and booleans cannot establish usable geometry.
        if name in ('area_sqft', 'clear_height_ft', 'floors') and (isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0):
            value = None
        records.append(record(name, value, source='costar' if name in ('area_sqft', 'floors') else 'public:caller-evidence', note='Caller-provided; verify source, suite scope and date. Not an inspection.'))
    records.append(record('jurisdiction', code, source='public:census-geocoder', url=jurisdiction.get('evidence_url'),
                          note=jurisdiction.get('note') or 'Jurisdiction supplied by caller.'))
    records.extend(context.get('evidence_records') or [])
    for topic, action in TOPICS.items():
        records.append(record(topic, note=action))
    return dict(schema_version='compliance.v1', stage='building', property_id=context.get('property_id'), scope=context.get('scope'),
                jurisdiction=jurisdiction, gates=gates, status='blocked' if any(g in gates for g in ('identity_conflict', 'commercial_conversion_unestablished')) else 'needs_verification',
                records=records, business_handoff_allowed=not any(g in gates for g in ('identity_conflict', 'commercial_conversion_unestablished')), legal_approval=False)

def kit_pin(property_id, kit=None):
    """Stage 2 requires explicit CoStar coordinates; never infer from postal city or alternate kit lookup."""
    return (None, None)
