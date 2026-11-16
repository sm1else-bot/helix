"""Isolated adapter for the team's existing venture and Nessie calculations."""
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for directory in (ROOT / 'vendor', ROOT.parents[1] / 'playground'):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
import venture_costs
from neighborhood_industries import INDUSTRIES
import nessie_loan
import nessie_pilot
import venture_review
import neighborhood
from reconcile import costar_ceiling_ft
from space_fit import building_numbers


def property_facts(service, property_id):
    raw = service.get_stage2_raw_record(int(property_id))
    if not raw: return None
    height, source = costar_ceiling_ft(raw)
    numbers = building_numbers(raw)
    return {'type': str(raw.get('propertyType') or ''), 'subtype': str(raw.get('propertySubtype') or ''),
            'gba': raw.get('grossBuildingAreaSquareFeet'), 'stories': raw.get('numStoriesAboveGrade'),
            'ceiling_ft': height, 'ceiling_source': source, 'drive_in_bays': numbers.get('drive_in_bays'), 'docks': numbers.get('docks')}


def create_loan(data):
    import re
    if not isinstance(data,dict) or data.get('confirm') is not True: raise ValueError('Explicit confirmation is required to create a sandbox loan.')
    account=data.get('account_id')
    if not isinstance(account,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',account): raise ValueError('Enter a valid Nessie sandbox account ID.')
    def num(key, low, high):
        v=data.get(key)
        if isinstance(v,bool): raise ValueError(f'{key} is invalid.')
        try: v=float(v)
        except (TypeError,ValueError): raise ValueError(f'{key} is invalid.')
        if not math.isfinite(v) or not low<=v<=high: raise ValueError(f'{key} is invalid.')
        return v
    amount=num('amount',1,50000000); apr=num('apr',0,100); term=num('term_months',1,600)
    if not term.is_integer() or not amount.is_integer(): raise ValueError('Loan amount and term must be whole numbers.')
    return nessie_loan.create(account,int(amount),apr,int(term))


def businesses():
    return [{"id": key, "name": value['label'], "benchmark": venture_costs.costs().get(key)} for key, value in INDUSTRIES.items()]


def simulate(data, property_data=None):
    key = data.get('business')
    if key not in INDUSTRIES:
        raise ValueError('Select a supported business.')
    def number(name, default=None, minimum=0, maximum=1000000000):
        val = data.get(name, default)
        if val in (None, ''): return None
        try: value = float(val)
        except (TypeError, ValueError): raise ValueError(f'{name} must be numeric.')
        if not math.isfinite(value) or not minimum <= value <= maximum: raise ValueError(f'{name} is outside the allowed range.')
        return value
    budget=number('budget'); reserve=number('reserve',0); rent=number('monthly_rent'); area=number('area_sqft',None,1); months=number('build_months',3,1,120); delay=number('delay_months',3,0,120)
    apr=number('apr',9,0,100); term=number('loan_months',60,1,600); principal=number('loan_principal',0)
    if budget is None or reserve is None or months is None or delay is None or apr is None or term is None or principal is None: raise ValueError('Complete the capital, duration and loan inputs.')
    if not months.is_integer() or not delay.is_integer() or not term.is_integer(): raise ValueError('Durations must be whole months.')
    if area is None: raise ValueError('Select a suite with recorded square footage.')
    mode=data.get('financing','cash'); case=data.get('cost_scenario','base')
    if mode not in ('cash','loan') or case not in ('low','base','high'): raise ValueError('Invalid financing or startup scenario.')
    cost=venture_costs.cash_to_open(key, area, rent, budget=budget, reserve=reserve, build_months=int(months), building_type=(property_data or {}).get('type') or data.get('building_type'))
    result={'inputs':data,'business':INDUSTRIES[key]['label'],'ledger':cost,'rent_source':data.get('rent_source','user entry'), 'warnings':['Physical fit is preliminary; use-specific compliance has not been assessed.', 'Revenue, payroll, utilities, profit and break-even are NOT MODELED.'], 'simulation':None,'loan':None}
    type_codes={'Hospitality':'1','Industrial':'2','Land':'3','Office':'5','GeneralRetail':'6','Flex':'7','SportsAndEntertainment':'8','MixedUse':'9','Specialty':'10','Multifamily':'11','Healthcare':'13','Residential':'15','Homes':'16'}
    code=(property_data or {}).get('type') or type_codes.get(data.get('building_type'))
    if code:
        fit_facts={'type':code,'subtype':'','gba':None,'stories':None,'ceiling_ft':None,'ceiling_source':None,'drive_in_bays':None,'docks':None,**(property_data or {}),'listed':[{'value':'selected','suite':'selected','sf':area,'floor':None}]}
        fit=neighborhood.building_fit(key,fit_facts,suite='selected')
        result['physical_fit']=fit
        if fit.get('status')!='fits': result['warnings'].append('Building fit: '+ '; '.join((fit.get('reasons') or [])+(fit.get('unknowns') or [])))
        if fit_facts.get('ceiling_ft') is not None: result['warnings'].append('CoStar ceiling height is building-level; verify selected-suite clear height before relying on this screening.')
    if str(data.get('building_type') or '').lower() in ('residential','homes','multifamily'):
        result['warnings'].append('Residential property type: commercial use and physical suitability need confirmation before proceeding.')
    totals=cost.get('cash_to_open') or {}; required=cost.get('required') or {}; complete=rent is not None and rent>0 and totals.get(case) is not None and not cost.get('unpriced')
    if not complete:
        result['warnings'].append('Complete capital requirement and runway are unavailable: rent or benchmark costs are missing.')
        return result
    cost_to_open=totals[case]; gap=max(0, cost_to_open+reserve-budget); active_loan=principal if mode=='loan' else 0
    result['capital']={'cash_to_open':cost_to_open,'reserve':reserve,'required':required[case],'budget':budget,'cash_gap':gap,'cash_headroom':max(0,budget-cost_to_open-reserve),'financed_gap':max(0,cost_to_open+reserve-budget-active_loan),'proposed_loan_gap':max(0,cost_to_open+reserve-budget-principal),'financing_mode':mode,'selected_loan_amount':active_loan,'formula':'max(0, cash to open + reserve − starting cash − selected loan proceeds)'}
    suggested=nessie_loan.plan(cost_to_open,budget,reserve,apr=apr,months=int(term)); pay=nessie_loan.payment(principal,apr,int(term)); schedule=nessie_loan.schedule(principal,apr,int(term))
    loan={'amount':principal,'apr':apr,'term_months':int(term),'monthly_payment':round(pay,2),'first_year_interest':round(sum(x['interest'] for x in schedule),2),'principal_remaining':schedule[-1]['balance_left'] if schedule else 0,'schedule':schedule,'suggested_gap_loan':suggested,'status':'proposed only'}
    result['loan']=loan
    extra_bills=[]
    for item in data.get('nessie_bills') or []:
        try: amount=float(item['monthly'])
        except (ValueError, TypeError, KeyError): raise ValueError('Sandbox bill amount is invalid.')
        if not math.isfinite(amount) or not 0 <= amount <= 1000000000: raise ValueError('Sandbox bill amount is invalid.')
        extra_bills.append({'payee':str(item.get('payee','Sandbox bill'))[:120],'monthly':amount})
    if any('rent' in item['payee'].casefold() for item in extra_bills):
        result['warnings'].append('A selected sandbox bill is labelled rent and is added to the selected property rent. Confirm it is a separate obligation to avoid double-counting.')
    result['venture_review_cash_months']=venture_review.cash_months(cost,{'monthly':rent},budget,reserve,delay=int(delay),extra_bills=extra_bills)
    build_line=next((line for line in cost['lines'] if line.get('id')=='rent_during_buildout'),None)
    result['simulation']={}
    for kind in ('low','base','high'):
        upfront=totals[kind]-(build_line[kind] if build_line else 0)
        scenario={'name':kind,'upfront_cost':upfront,'permit_months':int(months),'build_months':0,'build_cost':0,'monthly_income':0}
        buyer={'starting_cash':budget,'recurring_bills':[{'payee':'Base rent','monthly':rent}]+extra_bills}
        financed=nessie_loan.with_loan(buyer,loan)
        result['simulation'][kind]={}
        for variant,b in [('cash',buyer),('loan',financed)]:
            run=nessie_pilot.simulate(scenario,b,12,reserve); late=nessie_pilot.simulate(scenario,b,12,reserve,permit_delay=int(delay))
            for x in (run,late): x['first_month_below_zero']=next((row['month'] for row in x['rows'] if row['cash']<0),None)
            result['simulation'][kind][variant]={'on_time':run,'delayed':late,'monthly_bills':b['recurring_bills'],'upfront_cost':upfront}
    result['warnings'].append('The delay moves opening but does not change the 12-month balance in this zero-revenue model: rent and selected bills are already charged every month. No extra delay-specific costs are modeled.')
    return result
