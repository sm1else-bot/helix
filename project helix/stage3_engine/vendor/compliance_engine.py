"""Two-stage compliance triage. No output constitutes a permit or inspection.

Caller supplies normalized facts and confirmed jurisdiction; postal city is insufficient.
Optional search callback: search(query, domains) -> list of {url, title, content}.
Only official-domain search results survive; retrieved text never clears a gate.
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

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


def official(url, domains):
    parsed = urlsplit(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme == 'https' and not parsed.username and not parsed.password and any(host == d or host.endswith('.' + d) for d in domains)


def tavily_search(query, domains):
    """Opt-in live search. API key is read only when called; never returned."""
    key = os.environ.get('TAVILY_API_KEY')
    if not key:
        raise RuntimeError('TAVILY_API_KEY unavailable')
    data = json.dumps(dict(query=query, include_domains=domains, max_results=3)).encode()
    req = Request('https://api.tavily.com/search', data=data, headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    with urlopen(req, timeout=20) as response:
        return json.load(response).get('results', [])


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


PIN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'compliance', 'pins.json')


def kit_pin(property_id, kit=None):
    """(lat, lon) for a CoStar property id from the kit, cached on disk (the encrypted zip read is slow)."""
    pins = {}
    if os.path.exists(PIN_CACHE):
        with open(PIN_CACHE) as stream:
            pins = json.load(stream)
    if str(property_id) in pins:
        return tuple(pins[str(property_id)])
    from reconcile import kit_password, load_property
    kit = kit or os.environ.get('COSTAR_KIT') or os.path.expanduser('~/Downloads/costar-hackathon-kit.zip')
    a = load_property(kit, int(property_id), kit_password()).get('primaryAddress') or {}
    pin = (a.get('latitude'), a.get('longitude'))
    if None not in pin:
        pins[str(property_id)] = pin
        os.makedirs(os.path.dirname(PIN_CACHE), exist_ok=True)
        with open(PIN_CACHE, 'w') as stream:
            json.dump(pins, stream)
    return pin


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


def cached_search(search, cache_dir, offline=False):
    """Cache public research only; a missing offline entry raises into unknown.

    Cache contains timestamps, never credentials. No cached source clears a gate.
    """
    import hashlib
    from pathlib import Path
    directory = Path(cache_dir)
    def run(query, domains):
        key = hashlib.sha256(json.dumps([query, sorted(domains)]).encode()).hexdigest()
        path = directory / (key + '.json')
        if path.exists():
            payload = json.loads(path.read_text())
            return payload['results']
        if offline:
            raise RuntimeError('Research not cached')
        results = search(query, domains)
        directory.mkdir(parents=True, exist_ok=True)
        payload = dict(retrieved_at=datetime.now(timezone.utc).isoformat(), results=results)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload))
        temporary.replace(path)
        return results
    return run


def context_from_kit(property_id, suite=None, kit=None):
    """Normalized context for one CoStar property (optionally one listed suite) from the sponsor kit.

    Building-level CoStar facts are not suite measurements, so measurements_match_scope stays False and the
    business screen never treats them as the selected space's usable size or height. A scan captioned for
    another address is reported as a record, not as a building identity conflict: the CoStar record itself is
    still the right building, only the tour may not be.
    """
    from eligibility import PROPERTY_TYPES
    from reconcile import check_identity, costar_address, costar_ceiling_ft, kit_password, load_property
    from space_fit import listed_spaces
    kit = kit or os.environ.get('COSTAR_KIT') or os.path.expanduser('~/Downloads/costar-hackathon-kit.zip')
    password = kit_password()
    prop = load_property(kit, int(property_id), password)
    address = costar_address(prop)
    a = prop.get('primaryAddress') or {}
    space = None
    if suite is not None:
        space = next((s for s in listed_spaces(kit, int(property_id), password)
                      if str(s.get('suite') or '').strip().lower() == str(suite).strip().lower()), None)
        if space is None:
            raise ValueError(f'Suite {suite!r} is not an active listed space at property {property_id}.')
    kind = str(prop.get('propertyType', '')).strip()
    parking = sum((p.get('totalSpaces') or 0) for p in prop.get('parking') or []) or None
    scan_flags = [r for r in check_identity(prop) if r['status'] == 'conflict']
    return {
        'property_id': int(property_id), 'address': address['display'], 'lat': a.get('latitude'), 'lon': a.get('longitude'),
        'scope': f'suite:{space["suite"]}' if space else 'building',
        'facts': {'property_type': PROPERTY_TYPES.get(kind, kind or 'unknown'),
                  'area_sqft': (space or {}).get('square_feet') or prop.get('grossBuildingAreaSquareFeet'),
                  'clear_height_ft': costar_ceiling_ft(prop)[0], 'floors': prop.get('numStoriesAboveGrade'),
                  'parking_spaces': parking},
        'measurements_match_scope': False,
        'evidence_records': [record('scan_address', r['value'], source='costar', url=r.get('evidence_url'),
                                    note=r['note'] + ' Do not apply this tour\'s measurements to the property.',
                                    conflict=True) for r in scan_flags],
    }


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


def demo_requirements(business, jurisdiction_code):
    """Reuse team thresholds; distinguish sport guidance from legal requirements.

    California childcare must NOT inherit Texas area ratios.
    """
    from space_fit import USES, SOURCES
    aliases = {'indoor_pickleball':'pickleball', 'indoor pickleball':'pickleball', 'childcare':'daycare', 'childcare/daycare':'daycare', 'restaurant':'restaurant'}
    key = aliases.get(business.lower())
    if key is None:
        return None
    spec = USES[key]
    result = {'category':key, 'checks':list(spec.get('checks', [])), 'thresholds':{}, 'limitations':[]}
    if key == 'pickleball':
        result['thresholds'] = {'area_per_court_sqft':spec['area_per_unit_sf'], 'min_clear_height_ft':spec['min_clear_height_ft']}
        result['source_url'] = SOURCES[spec['area_source']]
        result['limitations'].append('Area is sport layout guidance, not proof of court geometry; ceiling threshold is unofficial guidance, not code.')
    elif key == 'daycare':
        if jurisdiction_code in ('houston','harris_unincorporated'):
            result['thresholds'] = {'indoor_activity_sqft_per_child':spec['indoor_sf_per_child'], 'outdoor_activity_sqft_per_child':spec['outdoor_sf_per_child']}
            result['source_url'] = SOURCES[spec['indoor_source']]
        else:
            result['checks'] = ['Confirm California childcare licensing, indoor/outdoor activity areas, fire clearance and age/capacity rules']
            result['limitations'].append('Texas thresholds intentionally excluded; California requirements need official verification.')
    return result


def check_business(context, business, search=None, requirements=None):
    """Business stage accepts ANY registry key, avoiding a divergent 26-use catalog.

    requirements may contain numeric min_area_sqft/min_clear_height_ft plus source_url.
    These are explicit screening thresholds, never assumed code requirements.
    """
    if not isinstance(business, str) or not business.strip():
        raise ValueError('business must be a nonempty registry key or label')
    context = resolve_context_jurisdiction(context)
    report = preflight(context)
    report.update(stage='business', business=business, research=[], errors=[], trace=[])
    if not report['business_handoff_allowed']:
        report['trace'].append('Stopped before business research: unresolved building gate')
        return report
    category = demo_requirements(business, (context.get('jurisdiction') or {}).get('code'))
    report['category_requirements'] = category
    fit = space_screen(context, business)
    report['registry'] = fit and {k: fit[k] for k in ('key', 'label', 'how')}
    if fit and fit.get('fit'):
        f = fit['fit']
        report['building_fit'] = f
        report['records'].append(record(
            'business_space_fit', f['status'], source='costar', url=None, conflict=f['status'] == 'no',
            note=(f"{fit['label']}: " + '; '.join(f.get('reasons') or []) +
                  ('. Unknown: ' + '; '.join(f['unknowns']) if f.get('unknowns') else '') +
                  '. Screening against the team registry (planning ranges), not a code requirement.')))
        if f['status'] == 'no':
            report['gates'].append('business_space_screen_failed')
            report['status'] = 'blocked'
    elif fit:
        report['records'].append(record('business_space_fit', None, note=fit['note']))
    facts = context.get('facts') or {}
    for key, fact in [('min_area_sqft', 'area_sqft'), ('min_clear_height_ft', 'clear_height_ft')]:
        threshold = (requirements or {}).get(key)
        actual = facts.get(fact)
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1e9:
                raise ValueError('invalid screening threshold')
            comparable = isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > 0 and context.get('measurements_match_scope') is True
            failed = comparable and actual < threshold
            report['records'].append(record(key, threshold, url=(requirements or {}).get('source_url'), conflict=failed,
                note='User/registry screening threshold; scope-matched measurement required; not legal capacity.'))
            if failed:
                report['gates'].append(fact + '_below_requested_threshold')
                report['status'] = 'blocked'
    code = (context.get('jurisdiction') or {}).get('code')
    if 'jurisdiction_unverified' in report['gates'] or report['status'] == 'blocked':
        report['trace'].append('Research deferred until jurisdiction and screening gates are resolved')
        return report
    authority, url, domains = AUTHORITIES[code]
    report['authority'] = authority
    report['records'].append(record('official_process', url, url=url, note='General process reference, not parcel approval'))
    # Two bounded calls, not 26 independent loops. Evidence cannot execute instructions.
    term = search_term(business)
    seen = set()
    for topic in ('change of use certificate of occupancy permit', 'fire accessibility parking business license requirements'):
        # Process-level query: official sites never mention a CoStar id or this street address.
        query = f'{authority} {term} {topic}'
        if search is None:
            report['trace'].append(dict(query=query, status='not_run'))
            continue
        try:
            results = search(query, domains)
            accepted = []
            for r in results:
                url = r.get('url', '')
                same = url.lower().replace('://www.', '://')
                text = f"{r.get('title', '')} {r.get('content', '')}".lower()
                if (not official(url, domains) or same in seen or any(x in url.lower() for x in EXCLUDE_PATHS)
                        or not any(t in text for t in PERMIT_TERMS)):
                    continue
                seen.add(same)
                accepted.append(dict(url=url, title=str(r.get('title', ''))[:300], excerpt=str(r.get('content', ''))[:2000],
                                     retrieved_at=datetime.now(timezone.utc).isoformat(), applicability='unverified',
                                     mentions_business=any(w in text for w in business_words(term))))
            report['research'].extend(accepted)
            report['trace'].append(dict(query=query, status='retrieved', accepted=len(accepted)))
        except Exception:
            report['errors'].append('Official-source research unavailable; retry or contact authority')
            report['trace'].append(dict(query=query, status='unavailable'))
    # Pages that name the business first; then general permit-process pages.
    report['research'].sort(key=lambda r: not r['mentions_business'])
    return report


# How permitting offices describe each demo business. Raw keys like "indoor_pickleball" match public park
# pages and unrelated documents; the occupancy wording matches commercial permit guidance.
SEARCH_TERMS = {
    'pickleball': 'commercial indoor sports recreation facility assembly occupancy',
    'daycare': 'child care center',
    'restaurant': 'restaurant food service establishment',
}
# Pages about a different business or about public facilities, not a private tenant's permits.
EXCLUDE_PATHS = ('/parks', 'cannabis', 'byob', 'auto-tire', 'apt%20mgr', 'apartment', 'redline', 'contractor_performance')
# A result must talk about permitting at all to be kept.
PERMIT_TERMS = ('permit', 'certificate of occupancy', 'occupancy', 'license', 'licensing', 'inspection', 'plan review',
                'building code', 'fire code', 'zoning')
GENERIC_WORDS = {'commercial', 'indoor', 'facility', 'center', 'establishment', 'service', 'services', 'and', 'or',
                 'the', 'a', 'of', 'office', 'store', 'shop', 'occupancy', 'assembly', 'sports', 'recreation', 'food'}


def business_words(term):
    """Distinctive words of a business description ("child care center" -> child, care)."""
    return [w for w in re.findall(r'[a-z]+', term.lower()) if w not in GENERIC_WORDS and len(w) > 2]


def registry_key(business):
    """Asmit's 26-category registry key for a business label or free text (code only), or None."""
    try:
        from neighborhood_industries import INDUSTRIES, resolve
    except Exception:
        return None, None, 'registry unavailable'
    key, how, _ = resolve(business.replace('_', ' ') if business not in INDUSTRIES else business)
    return key, (INDUSTRIES[key]['label'] if key else None), how


def space_screen(context, business):
    """Run the team's building_fit (neighborhood.py) for this business at this property; never raises."""
    key, label, how = registry_key(business)
    if not key:
        return None
    result = {'key': key, 'label': label, 'how': how, 'fit': None, 'note': ''}
    if context.get('property_id') is None:
        result['note'] = f'{label}: no CoStar property id, building fit not checked.'
        return result
    suite = context.get('suite')
    if suite is None and str(context.get('scope') or '').startswith('suite:'):
        suite = context['scope'].split(':', 1)[1]
    try:
        from neighborhood import building_facts, building_fit
        facts = building_facts(int(context['property_id']), str(context.get('kit') or ''))
        result['fit'] = building_fit(key, facts, suite)
    except Exception as error:
        result['note'] = f'{label}: building fit unavailable ({type(error).__name__}); verify size and layout manually.'
    return result


def search_term(business):
    category = demo_requirements(business, None)
    if (category or {}).get('category') in SEARCH_TERMS:
        return SEARCH_TERMS[category['category']]
    key, label, _ = registry_key(business)
    return (label or business.replace('_', ' ')).lower()


RESEARCH_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'output', 'compliance', 'research')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('context', nargs='?', help='Normalized JSON context file (or use --property-id)')
    parser.add_argument('--property-id', type=int, help='Build the context from the CoStar kit')
    parser.add_argument('--suite', help='Listed suite at that property')
    parser.add_argument('--kit')
    parser.add_argument('--business')
    parser.add_argument('--live', action='store_true', help='Opt-in Tavily official-source retrieval (cached)')
    parser.add_argument('--offline', action='store_true', help='Use only cached research and jurisdiction')
    args = parser.parse_args()
    if args.property_id:
        context = context_from_kit(args.property_id, args.suite, args.kit)
    elif args.context:
        with open(args.context) as stream:
            context = json.load(stream)
    else:
        parser.error('give a context file or --property-id')
    if args.offline:
        os.environ['JURISDICTION_OFFLINE'] = '1'
    search = cached_search(tavily_search, RESEARCH_CACHE, offline=args.offline) if (args.live or args.offline) else None
    result = check_business(context, args.business, search) if args.business else preflight(context)
    print(json.dumps(result, indent=2, default=str))


if __name__ == '__main__':
    main()
