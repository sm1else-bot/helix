"""Final investment memorandum: one read-only view over the saved Stage 1-4 records.

Nothing here recalculates screening, costs or cash. Values are selected from the saved
Stage 2 dossier, the deterministic Stage 3 result, the opportunity report and the latest
Stage 4 Finance Lab run. The verdict is a stated rule over those saved values.
"""
from __future__ import annotations

from datetime import datetime, timezone

from stage2.physical_profile import build_physical_profile

VERSION = 'helix.final-report.v1'

# Physical feature groups from the Stage 2 profile, in reading order, with what each
# observation means in practice. Implications are conditional on the feature being observed.
FEATURES = [
    ('loading_access', 'Loading and vehicle access', 'Roll-up doors give a direct path for vehicles and equipment; door count, width and height are not measured.'),
    ('interior_layout', 'Interior layout', 'Columns and existing partitions shape any open floor plan; partition removal belongs in the fit-out scope.'),
    ('entrances', 'Entrances', 'Several entry points are visible, which helps separate customer and service circulation.'),
    ('possible_emergency_exits', 'Exits', 'Exit signage is present; the required exit count depends on the new occupant load.'),
    ('parking', 'Parking and exterior', 'Marked parking sits next to the building; spaces allocated to this lease are not recorded.'),
    ('utilities', 'Visible utilities', 'Visible services suggest existing distribution; capacity for the new use is not established.'),
    ('ceiling', 'Ceiling and volume', 'The ceiling finish is visible; usable clear height for the new use still needs a measurement.'),
    ('furniture_and_fixtures', 'Existing fit-out', 'Shelving, desks and fixtures from the prior use will need to be cleared.'),
]

# Business-specific reading of the same observations. Only emitted when the feature is observed.
USE_NOTES = {
    'auto_repair': {'loading_access': 'Roll-up doors support vehicle bays; confirm grade-level access and lift clearance.',
                    'interior_layout': 'Columns limit where lifts can go; plan bays around them.'},
    'fitness_gym': {'interior_layout': 'Columns and partitions break up the training floor; equipment layout must work around them.',
                    'parking': 'Peak-hour member parking needs to be confirmed against the marked lot.'},
    'martial_arts': {'interior_layout': 'A visible column interrupts the open floor; the mat area must be laid out around it.',
                     'entrances': 'Separate entries could serve a front desk and changing rooms.',
                     'loading_access': 'Roll-up doors are not required for this use.'},
}

MEASUREMENTS = [('clear_height_ft', 'Usable clear height'), ('unobstructed_area_sqft', 'Unobstructed floor area'),
                ('clear_floor_dimensions_ft', 'Clear floor dimensions'), ('furniture_and_partition_removability', 'Partition removability')]


import re

_SIZE = re.compile(r'^(?:[^:]+: )?Smallest option is ([\d,]+) sq ft \([^)]*\), above the typical ([\d,]+)-([\d,]+) sq ft: only works if part of it can be leased\.?$')


def tidy(text):
    """Plain-English rewording of engine strings; meaning unchanged."""
    text = str(text or '').strip()
    m = _SIZE.match(text)
    if m: return f'At {m.group(1)} sq ft the space is larger than the {m.group(2)}–{m.group(3)} sq ft typical for this use; it works only if part of it can be leased.'
    return text.replace(' (CoStar-recorded, propertyIndustrial)', ' (CoStar record)').replace('a martial arts / boxing', 'a martial arts / boxing studio')


def _num(value):
    try: return float(value)
    except (TypeError, ValueError): return None


def _first(items):
    return items[0] if items else None


def _property(handoff, stage3):
    listing = handoff.get('selected_listing') or {}
    dossier = handoff.get('dossier') or {}
    costar = (dossier.get('reported_building_facts') or {}).get('costar') or {}
    facts = ((stage3 or {}).get('property') or {}).get('facts') or {}
    s3prop = (stage3 or {}).get('property') or {}
    rent = listing.get('rent_normalization') or {}
    views = (((dossier.get('source_status') or {}).get('vexcel') or {}).get('imagery') or {}).get('views') or []
    models = (dossier.get('identity_resolution') or {}).get('matterport_models') or []
    model = next((m.get('model_id') for m in models if m.get('status') == 'matched' and m.get('showcase_url')), None)
    tour = f'https://my.matterport.com/show/?m={model}' if model else None  # plain Showcase link, no SDK key
    return {
        'address': listing.get('address'), 'city': listing.get('city'), 'state': listing.get('state'),
        'property_id': listing.get('property_id'), 'listing_id': listing.get('listing_id'), 'space_id': listing.get('space_id'),
        'property_type': costar.get('property_type') or listing.get('property_type'), 'building_class': costar.get('building_class'),
        'building_sqft': costar.get('gross_building_area_sqft'), 'space_sqft': listing.get('area_max_sqft'),
        'stories': costar.get('stories'), 'zoning': costar.get('zoning_description'),
        'rent_monthly': rent.get('monthly_low_usd'), 'rent_psf_year': rent.get('raw_low'),
        'lease_basis': (s3prop.get('rent') or {}).get('source'), 'occupancy_costs': rent.get('total_occupancy_cost_status'),
        'available_on': listing.get('available_at'), 'ceiling_ft': facts.get('ceiling_ft'), 'drive_in_bays': facts.get('drive_in_bays'),
        'jurisdiction': ((s3prop.get('jurisdiction') or {}).get('place')), 'authority': s3prop.get('authority'),
        'latitude': costar.get('latitude'), 'longitude': costar.get('longitude'),
        'images': [{'url': v.get('image_url'), 'view': v.get('view'), 'captured': v.get('capture_date')} for v in views if v.get('image_url')],
        'tour_url': tour,
    }


def physical_summary(dossier, business=None):
    profile = build_physical_profile(dossier)
    features = profile.get('physical_features') or {}
    findings = []
    for key, title, implication in FEATURES:
        item = features.get(key) or {}
        if item.get('status') != 'observed': continue
        evidence = item.get('evidence') or []
        names = []
        for name in item.get('features') or []:
            clean = str(name).strip().rstrip('.').capitalize()
            if clean.lower() not in {n.lower() for n in names}: names.append(clean)
        first = _first(evidence) or {}
        findings.append({'key': key, 'title': title, 'features': names[:5], 'detail': first.get('description'),
                         'provider': first.get('provider'), 'scope': first.get('scope'), 'image_id': first.get('image_id'),
                         'implication': implication, 'for_selected_use': (USE_NOTES.get(business) or {}).get(key),
                         'evidence': [{'provider': e.get('provider'), 'description': e.get('description'), 'ref': e.get('evidence_ref')} for e in evidence]})
    constraints = profile.get('matching_constraints') or {}
    unmeasured = [label for key, label in MEASUREMENTS if (constraints.get(key) or {}).get('status') == 'unknown']
    visual = dossier.get('visual_analysis') or {}
    mp, vx = visual.get('matterport') or {}, visual.get('vexcel') or {}
    return {'findings': findings, 'unmeasured': unmeasured,
            'coverage': {'panoramas_analyzed': mp.get('panoramas_analyzed'), 'panoramas_total': mp.get('panoramas_total'),
                         'aerial_views': vx.get('views_analyzed'), 'observations': len(dossier.get('physical_observations') or []),
                         'model': mp.get('model') or vx.get('model')},
            'comparisons': [{'fact': c.get('fact'), 'status': c.get('status'), 'reason': c.get('reason')} for c in dossier.get('technical_comparisons') or []],
            'contradictions': dossier.get('contradictions') or [],
            'scope_note': 'Scan observations are building-level. The scan has not been matched to the advertised suite, so they describe the building, not a surveyed suite.'}


def _opportunities(stage3, report, business):
    shortlist = stage3.get('shortlist') or []
    gates = {g.get('business'): g for g in stage3.get('gates') or []}
    lists = [c.get('verification_questions') or [] for c in shortlist]
    common = [q for q in (lists[0] if lists else []) if all(q in other for other in lists[1:])] if len(lists) > 1 else []
    ai = {c.get('business'): c.get('claims') or [] for c in ((report or {}).get('narrative') or {}).get('candidates') or []}
    budget = _num((stage3.get('inputs') or {}).get('budget'))
    candidates = []
    for c in shortlist:
        gate = gates.get(c.get('business')) or {}
        s4 = c.get('stage4_inputs') or {}
        building = gate.get('building') or {}
        candidates.append({
            'rank': c.get('rank'), 'business': c.get('business'), 'label': c.get('label'), 'group': c.get('group'),
            'selected': c.get('business') == business,
            'cash_to_open': s4.get('cash_to_open') or {}, 'max_reserve_base_case': s4.get('max_reserve_base_case'),
            'building_status': building.get('status'), 'building_reasons': building.get('reasons') or [],
            'building_checks': [tidy(x) for x in building.get('unknowns') or []],
            'checks': [q for q in c.get('verification_questions') or [] if q not in common],
            'ai_claims': ai.get(c.get('business'), []),
        })
    return {'evaluated': (stage3.get('summary') or {}).get('businesses'), 'passed': (stage3.get('summary') or {}).get('pass_all_hard_gates'),
            'stopped_at': (stage3.get('summary') or {}).get('stopped_at') or {}, 'budget': budget,
            'candidates': candidates, 'common_checks': common, 'tie_breaks': stage3.get('tie_breaks') or [],
            'not_evaluated': [w for w in stage3.get('warnings') or [] if w.startswith(('Neighborhood', 'Licences', 'No Stage 2'))]}


def _financial(selection):
    run = (selection or {}).get('latest_run') or {}
    result, request = run.get('result') or {}, run.get('request') or {}
    out = {'stage3_cash_to_open': ((selection or {}).get('stage3') or {}).get('cash_to_open'), 'run': None}
    if not result.get('capital') or not result.get('simulation'): return out
    scenario = request.get('cost_scenario') or 'base'; mode = request.get('financing') or 'cash'
    chosen = (result['simulation'].get(scenario) or {}).get(mode) or {}
    on_time, delayed = chosen.get('on_time') or {}, chosen.get('delayed') or {}
    other = {k: ((result['simulation'].get(k) or {}).get(mode) or {}).get('on_time') or {} for k in ('low', 'base', 'high')}
    loan = result.get('loan') or {}
    out['run'] = {
        'at': run.get('at'), 'scenario': scenario, 'financing': mode,
        'inputs': {k: request.get(k) for k in ('budget', 'reserve', 'monthly_rent', 'build_months', 'delay_months', 'loan_principal', 'apr', 'loan_months')},
        'capital': result['capital'],
        'months': [{'month': r.get('month'), 'cash': r.get('cash'), 'outflow': r.get('outflow')} for r in on_time.get('rows') or []],
        'delayed_months': [{'month': r.get('month'), 'cash': r.get('cash')} for r in delayed.get('rows') or []],
        'opening_month': on_time.get('opening_month'), 'delayed_opening_month': delayed.get('opening_month'),
        'lowest_cash': on_time.get('lowest_cash'), 'first_month_below_reserve': on_time.get('first_month_below_reserve'),
        'first_month_below_zero': on_time.get('first_month_below_zero'),
        'scenarios': {k: {'month12': (v.get('rows') or [{}])[-1].get('cash'), 'below_reserve': v.get('first_month_below_reserve'),
                          'below_zero': v.get('first_month_below_zero')} for k, v in other.items() if v.get('rows')},
        'monthly_bills': chosen.get('monthly_bills') or [],
        'loan': {k: loan.get(k) for k in ('amount', 'apr', 'term_months', 'monthly_payment', 'first_year_interest', 'status')} if mode == 'loan' else None,
        'ledger': [{'label': l.get('label'), 'how': l.get('how'), 'low': l.get('low'), 'base': l.get('base'), 'high': l.get('high'), 'source': l.get('source')}
                   for l in (result.get('ledger') or {}).get('lines') or []],
        'sources': [{'name': s.get('publisher_or_brand'), 'document': s.get('document'), 'url': s.get('url')} for s in (result.get('ledger') or {}).get('sources') or []],
        'deviations': (result.get('handoff') or {}).get('deviations') or [],
        'consistency': (result.get('handoff') or {}).get('stage3_cash_to_open_consistency') or {},
    }
    return out


def _verdict(selection, opportunities, financial, next_steps):
    label = (selection or {}).get('label')
    if not selection: return {'code': 'pending', 'headline': 'Choose a business to complete the memorandum', 'reasons': [], 'rule': ''}
    run = financial.get('run')
    if not run: return {'code': 'pending', 'headline': f'Run the Finance Lab for {label} to complete the verdict', 'reasons': [], 'rule': ''}
    cap = run['capital']; gap = cap.get('financed_gap') if run['financing'] == 'loan' else cap.get('cash_gap')
    cand = next((c for c in opportunities['candidates'] if c['selected']), {})
    reasons = []
    if gap and gap > 0:
        code, headline = 'not_funded', f'Do not proceed as modeled: {label} needs about ${gap:,.0f} more than the capital available'
    elif run.get('first_month_below_zero') or run.get('first_month_below_reserve'):
        month = run.get('first_month_below_zero') or run.get('first_month_below_reserve')
        code, headline = 'caution', f'Proceed with caution: cash falls below the reserve in month {month} of a no-revenue year'
    else:
        code, headline = 'proceed', f'Proceed to diligence on {label}'
    if not gap:
        reasons.append(f"Fully funded in the {run['scenario']} case: ${cap.get('cash_to_open', 0):,.0f} to open plus a ${cap.get('reserve', 0):,.0f} reserve leaves ${cap.get('cash_headroom', 0):,.0f} of headroom.")
    lowest = run.get('lowest_cash')
    if lowest is not None and not run.get('first_month_below_reserve'):
        reasons.append(f'Even with no revenue for 12 months, cash never drops below the reserve (lowest ${lowest:,.0f}).')
    high = (run.get('scenarios') or {}).get('high') or {}
    if run['scenario'] != 'high' and (high.get('below_zero') or high.get('below_reserve')):
        month = high.get('below_zero') or high.get('below_reserve')
        if high.get('below_zero') == 1: reasons.append('Risk: at the high end of the cost range, opening costs alone exceed the budget. Contractor bids decide which case applies.')
        else: reasons.append(f"Risk: at the high end of the cost range, cash falls {'below zero' if high.get('below_zero') else 'below the reserve'} by month {month}. Contractor bids decide which case applies.")
    if cand:
        reasons.append(f"One of {opportunities.get('passed')} businesses (of {opportunities.get('evaluated')} screened) that cleared every building, budget and compliance gate; ranked #{cand.get('rank')}.")
        if cand.get('building_checks'): reasons.append('Condition: ' + cand['building_checks'][0])
    return {'code': code, 'headline': headline, 'reasons': reasons, 'open_checks': len(next_steps),
            'rule': 'Do not proceed if the selected financing leaves a funding gap; proceed with caution if 12-month no-revenue cash falls below the reserve; otherwise proceed to diligence. Always conditional on the checks listed.'}


def _next_steps(stage3, business):
    steps = []
    for item in stage3.get('missing_evidence') or []:
        affects = item.get('affects')
        if affects != 'all' and business and business not in (affects or []): continue
        steps.append({'question': tidy(item.get('question')), 'source': item.get('needed_source'), 'jurisdiction': item.get('jurisdiction'),
                      'status': item.get('status'), 'applies': 'all options' if affects == 'all' or len(affects or []) > 1 else 'this business'})
    order = ['zoning', 'licen', 'height', 'fire', 'parking', 'smallest', 'demand', 'reserve']
    def rank(step):
        text = (step['question'] or '').lower()
        return next((i for i, word in enumerate(order) if word in text), len(order))
    return sorted(steps, key=rank)


def build(investigation_id, *, handoff, stage3, report, selection):
    if not handoff or not handoff.get('dossier'): raise ValueError('This investigation has no completed building investigation yet.')
    if not stage3: raise ValueError('Run business screening (Stage 3) before opening the report.')
    business = (selection or {}).get('business')
    opportunities = _opportunities(stage3, report, business)
    financial = _financial(selection)
    steps = _next_steps(stage3, business)
    claims = (report or {}).get('narrative') or {}
    return {
        'schema_version': VERSION, 'investigation_id': investigation_id, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'intake': {k: (handoff.get('intake') or {}).get(k) for k in ('city', 'total_budget_usd', 'financing')},
        'property': _property(handoff, stage3),
        'physical': physical_summary(handoff['dossier'], business),
        'opportunities': opportunities,
        'analysis': {'report_id': (report or {}).get('report_id'), 'model': ((report or {}).get('model') or {}).get('model'),
                     'created_at': (report or {}).get('created_at'),
                     'sections': {k: claims.get(k) or [] for k in ('executive_thesis', 'physical_intelligence', 'evidence_comparisons', 'risks_and_unknowns')},
                     'sources': (report or {}).get('evidence') or {}} if report else None,
        'selection': {k: (selection or {}).get(k) for k in ('business', 'label', 'rank', 'selected_at', 'report_claims')} if selection else None,
        'financial': financial,
        'next_steps': steps,
        'verdict': _verdict(selection, opportunities, financial, steps),
        'stage3_hash': ((stage3.get('reproduce') or {}).get('output_hash')),
        'scope': ['Startup costs are scaled published franchise benchmarks, not contractor bids.',
                  'The 12-month view is a no-revenue cash runway. Revenue, payroll, utilities and profit are not modeled.',
                  'Business screening uses CoStar records and published size ranges; it does not read the scan findings above.'],
    }
