"""Conservative shortlist: eligibility first, explicit criteria, no synthetic scores."""
CATALOG = {
    'warehouse / storage': {'types': {'2','7'}, 'fit': 'custom', 'check': 'Verify loading access, storage fire classification and floor loading.'},
    'flex workspace': {'types': {'2','7','5'}, 'fit': 'custom', 'check': 'Confirm separation of workshop and office occupancies, ventilation and electrical load.'},
    'indoor pickleball': {'types': {'2','7','8','9','6'}, 'fit': 'pickleball', 'check': 'Measure court layout, usable clear height and overhead obstructions before estimating capacity.'},
    'fitness studio': {'types': {'2','7','8','9','6','5'}, 'fit': 'gym', 'check': 'Confirm floor loading, HVAC, showers, exits and peak parking rights.'},
    'office / service': {'types': {'5','7','9','6'}, 'fit': 'custom', 'check': 'Verify accessible entry, restrooms, HVAC, daylight and permitted professional use.'},
    'retail / showroom': {'types': {'6','9'}, 'fit': 'custom', 'check': 'Confirm customer access, signage rights, parking allocation and retail permission.'},
}


def rank_alternatives(report, prop, fits, listing, budget, reserve):
    rows = []
    eligible = report['eligibility']['status'] == 'conditional'
    kind = str(prop.get('propertyType', ''))
    identity_conflict = any(r['fact'] in {'scan_address','scan_suite'} and r['status']=='conflict'
                            for r in report['identity']['records'])
    rent = (report.get('money') or {}).get('inputs',{}).get('monthly_rent')
    for use, spec in CATALOG.items():
        fit = fits.get(use, {})
        state, blockers = 'conditional', []
        if not eligible:
            state, blockers = 'blocked', [report['eligibility']['reason']]
        elif kind not in spec['types']:
            state, blockers = 'excluded', ['Property type is outside the supported conversion catalog for this use; bespoke design review required.']
        elif fit.get('verdict') == 'does_not_fit':
            state, blockers = 'excluded', ['Available physical measurements conflict with this use.']
        else:
            if not listing or not listing.get('square_feet'):
                state = 'blocked'
                blockers.append('Advertised premises and usable dimensions are unverified.')
            if identity_conflict:
                blockers.append('Tour does not match the building or selected suite; do not use it as measured premises.')
            blockers += [spec['check'], 'Parcel/suite permission, allocated parking and utilities remain unverified.']
        screening_state=state
        # Only a lower bound until Stage 2 ledger is available. Never claim it is total cost.
        lower = reserve + (rent or 0)
        budget_fit = 'insufficient even for reserve and one month base rent' if budget < lower else 'unknown until itemized costs'
        if eligible and state != 'excluded' and budget < lower:
            state = 'unaffordable'
            blockers.insert(0, 'Cash does not cover reserve plus one month of base rent, before construction.')
        rows.append(dict(use=use,status=state,screen_status=screening_state,physical_fit="investigate" if eligible and (spec["fit"]=="custom" or identity_conflict) else fit.get('verdict','not_run'),
                         cash_required=None,known_cash_floor=lower,budget_fit=budget_fit,
                         permission='unverified',demand_confidence='unverified',opening_delay=None,runway=None,
                         evidence_quality='scope conflict' if identity_conflict else 'partial sponsor evidence',
                         blockers=blockers,key_blocker=blockers[0],next_check=spec['check'],
                         records=fit.get('records',[])))
    order={'conditional':0,'blocked':1,'unaffordable':2,'excluded':3}
    rows.sort(key=lambda r:(order[r['status']], r['use'] != report['proposed_use'], list(CATALOG).index(r['use'])))
    shortlisted=[r for r in rows if r['status']=='conditional'][:3]
    return dict(options=shortlisted,all_options=rows,
                recommendation=(f"Investigate {shortlisted[0]['use']} first; suitability and total cost remain conditional."
                                if shortlisted else 'Do not commit: no use has cleared this preliminary screen within the available evidence and cash.'),
                ranking_basis='Eligibility and physical exclusions first; requested use breaks ties. Costs, permissions and demand are not yet established. No numeric score.',
                reversal='A measured plan, permission decision, priced work scope or paid-demand test can change this order.')
