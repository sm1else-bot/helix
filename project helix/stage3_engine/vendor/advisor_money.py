"""Reconcile itemized costs to the unchanged Nessie monthly simulator."""
from approvals_timeline import build_timeline
from cost_ledger import estimate_costs
from money_runway import analyze_money, assumptions, DEFAULTS


def plan_inputs(inputs,use):
    result=dict(inputs)
    if use=='warehouse / storage':
        result.setdefault('build_cost', result.get('baseline_build_cost',25000))
        result.setdefault('monthly_income', result.get('baseline_income',18000))
    return result


def comparison_inputs(inputs, selected_use, candidate):
    result=dict(inputs)
    if candidate != selected_use:
        if candidate == 'warehouse / storage':
            result['build_cost']=inputs.get('baseline_build_cost',25000)
            result['monthly_income']=inputs.get('baseline_income',18000)
        elif selected_use == 'warehouse / storage':
            # A small warehouse allowance is not a bid for a recreation conversion.
            result['build_cost']=DEFAULTS['build_cost']
            result['monthly_income']=DEFAULTS['monthly_income']
    return result


def advisor_cash(inputs,use,ownership,*,overrides=None,area=None,geography='Unknown',rent_source='Editable assumption',buyer=None):
    values=assumptions(plan_inputs(inputs,use))
    if buyer: values['starting_cash']=buyer['starting_cash']
    ledger=estimate_costs(values,area=area,tenure=ownership,geography=geography,rent_source=rent_source,overrides=overrides)
    bank_fixed=sum(float(r['monthly']) for r in buyer['recurring_bills']) if buyer else 0
    # Ledger and simulator include the same sandbox bills once, in every preopening month.
    for totals in ledger['totals'].values():
        additional=bank_fixed*(ledger['opening_month']-1)
        totals['preopening_carry']+=additional
        totals['known_cash_floor']+=additional
        if totals['cash_required'] is not None:
            totals['cash_required']+=additional
            totals['cash_spent_to_open']+=additional
            totals['cash_at_opening']-=additional
            totals['shortfall']=max(0,totals['cash_required']-values['starting_cash'])
    ledger['monthly_preopening_burn']+=bank_fixed
    ledger['monthly_operating_burn']+=bank_fixed
    if any(t['unknown_lines'] for t in ledger['totals'].values()):
        return {'status':'incomplete_costs','costs':ledger,'money':None,
                'reason':'Fill unknown cost lines before projecting a complete cash curve.'}
    cases={}
    for case,t in ledger['totals'].items():
        projected={**values,'build_cost':t['build_cost'],'upfront_cost':t['upfront_cost']}
        cases[case]=analyze_money(projected,proposed_use=use,ownership=ownership,buyer=buyer)
    cash=cases['base']
    cash['timeline']=build_timeline({'permit_months':values['permit_months'],'build_months':values['build_months'],'delay_months':values['delay']})
    cash['planning_inputs']=values
    cash['rent_source']=rent_source
    cash['cost_cases']={k:v['conversion'] for k,v in cases.items()}
    cash['cash_at_opening']=ledger['totals']['base']['cash_at_opening']
    cash['runway_months']=next((r['month'] for r in cash['conversion']['rows'] if r['cash']<0),None)
    if cash['conversion']['initial_cash'] < 0: cash['runway_months']=0
    cash['runway_note']='First negative month within the modeled horizon; null means no negative month observed, not perpetual runway.'
    # Scenario stress tests, never revenue forecasts. Defaults and multiplier are visible assumptions.
    projected=cash['inputs']
    stresses={
      'build_overrun_25pct':{**projected,'build_cost':projected['build_cost']*1.25},
      'rent_increase_20pct':{**projected,'monthly_rent':projected['monthly_rent']*1.2},
      'demand_shortfall_25pct':{**projected,'bookings':projected['bookings']*.75,'monthly_income':projected['monthly_income']*.75},
    }
    cash['sensitivities']={key:analyze_money(v,proposed_use=use,ownership=ownership,buyer=buyer)['conversion'] for key,v in stresses.items()}
    cash['sensitivities']['opening_delay_3months']=cash['stress']
    cash['expense_components']={key:values[key] for key in values if key.startswith('monthly_') and key!='monthly_income'}
    cash['revenue_basis']='Editable unverified revenue scenario; observed competitors do not verify bookings, utilization or ROI.'
    cash['assumptions_label']='Itemized planning allowances and hypothetical income; no contractor quotes or verified ROI. Nessie supplies deterministic cash mechanics and optional sandbox bills.'
    return {'status':'conditional','money':cash,'costs':ledger}
