"""Deterministic editable cost ledger. Defaults are allowances, never quotes."""
from datetime import date
import math

# Allocation of the user's construction allowance, not market-derived pricing.
BUILD = [('design_engineering', .08), ('permits', .02), ('demolition', .05), ('interiors', .25),
         ('electrical', .10), ('hvac', .15), ('plumbing', .08), ('fire_ada', .10),
         ('equipment', .14), ('signage', .03)]
EXTRA = ['deposit','acquisition','insurance_legal','preopening_marketing_payroll','contingency']


def number(value):
    if value is None:
        return None
    if isinstance(value,bool):
        raise ValueError('Costs must be finite nonnegative numbers or null.')
    try:
        n=float(value)
    except (TypeError,ValueError):
        raise ValueError('Costs must be finite nonnegative numbers or null.') from None
    if not math.isfinite(n) or not 0 <= n <= 1e9:
        raise ValueError('Costs must be finite nonnegative numbers or null.')
    return n


def estimate_costs(inputs, *, area=None, tenure='lease', geography='Unknown', rent_source='Editable assumption', overrides=None):
    """One source of cost truth. Deposit not prepaid rent; reserve is held cash, not spent."""
    from money_runway import assumptions
    v=assumptions(inputs)
    overrides=overrides or {}
    if not isinstance(overrides,dict) or set(overrides)-({k for k,_ in BUILD}|set(EXTRA)):
        raise ValueError('Unknown cost line.')
    rows=[]
    def line(key,base,phase,kind,source='Unverified editable planning allowance'):
        default={'low':round(base*.75,2),'base':round(base,2),'high':round(base*1.5,2)} if base is not None else dict.fromkeys(('low','base','high'))
        override=overrides.get(key,{})
        if not isinstance(override,dict) or set(override)-{'low','base','high','payer'}:
            raise ValueError('Cost overrides accept low, base, high and payer.')
        payer=override.get('payer','tenant' if tenure=='lease' else 'buyer')
        if payer not in {'tenant','buyer','landlord'}:
            raise ValueError('Choose tenant, buyer or landlord payer.')
        values={k:number(override.get(k,value)) for k,value in default.items()}
        known=[values[k] for k in ('low','base','high') if values[k] is not None]
        if len(known)==3 and not known[0] <= known[1] <= known[2]:
            raise ValueError('Each cost line must satisfy low ≤ base ≤ high.')
        quantity=area if key in dict(BUILD) and area and area>0 else 1
        rows.append(dict(id=key,label=key.replace('_',' ').capitalize(),quantity=quantity,
                         unit='sq ft' if quantity!=1 else 'allowance',
                         rates={k:round(n/quantity,4) if n is not None else None for k,n in values.items()},
                         amounts=values,payer=payer,phase=phase,classification=kind,source=source,
                         source_date=None,assumption_date=date.today().isoformat(),geography=geography,
                         confidence='unverified assumption',editable=True,
                         note='Rates are allocation arithmetic from the entered allowance, not contractor pricing.' if key in dict(BUILD) else 'Verify amount and responsibility; not a sourced quote.'))
    for key,share in BUILD:
        line(key,v['build_cost']*share,'build','CAPEX')
    line('deposit',v['monthly_rent']*2 if tenure=='lease' else 0,'upfront','refundable deposit',
         f'Two months assumed deposit × base rent ({rent_source}); deposit terms unverified')
    line('acquisition',(v['upfront_cost'] or None) if tenure=='buy' else 0,'upfront','acquisition')
    line('insurance_legal',3000,'upfront','OPEX')
    line('preopening_marketing_payroll',5000,'upfront','OPEX')
    construction={k:sum(r['amounts'][k] or 0 for r in rows if r['phase']=='build' and r['payer']!='landlord') for k in ('low','base','high')}
    line('contingency',construction['base']*.15,'build','CAPEX')
    duration=v['permit_months']+v['build_months']+v['delay']
    burn=(v['monthly_rent'] if tenure=='lease' else 0)+v['monthly_carry']+v['monthly_nnn']+v['monthly_insurance']+v['monthly_taxes']
    totals={}
    for case in ('low','base','high'):
        owed=[r for r in rows if r['payer']!='landlord']
        unknown=[r['id'] for r in owed if r['amounts'][case] is None]
        build=sum(r['amounts'][case] or 0 for r in owed if r['phase']=='build')
        upfront=sum(r['amounts'][case] or 0 for r in owed if r['phase']=='upfront')
        spent=round(build+upfront+burn*duration,2)
        required=round(spent+v['reserve'],2)
        totals[case]=dict(build_cost=round(build,2),upfront_cost=round(upfront,2),preopening_carry=round(burn*duration,2),
                          cash_spent_to_open=None if unknown else spent,cash_required=None if unknown else required,
                          known_cash_floor=required,reserve=v['reserve'],cash_at_opening=None if unknown else v['starting_cash']-spent,
                          shortfall=None if unknown else max(0,required-v['starting_cash']),unknown_lines=unknown)
    return dict(lines=rows,totals=totals,monthly_preopening_burn=burn,opening_month=duration+1,
                monthly_operating_burn=burn+v['monthly_operating_cost']+v['monthly_payroll']+v['monthly_utilities']+v['monthly_maintenance'],budget=v['starting_cash'],overrides=overrides,
                basis='Unverified editable planning allowances, not estimates or quotes. Reserve is retained cash. Landlord-paid lines excluded. Deposit is additional refundable cash, not prepaid rent. Lease rent is counted monthly only.',
                missing=['Obtain contractor quantities, rates, geography and dated bids.', 'Confirm deposit, landlord contribution, taxes, NNN/CAM, insurance and acquisition terms.'])
