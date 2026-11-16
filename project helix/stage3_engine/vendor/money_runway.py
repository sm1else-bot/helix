"""Versioned, editable underwriting assumptions over Jayant's Nessie simulator."""
import math

from nessie_pilot import simulate, buyer_from_nessie

DEFAULTS = dict(starting_cash=320000, reserve=50000, build_cost=200000,
                permit_months=1, build_months=2, delay=0, months=24,
                monthly_rent=8500, monthly_carry=2000, monthly_operating_cost=5000,
                bookings=594, booking_price=35, monthly_income=20790,
                baseline_income=18000, baseline_build_cost=25000,
                baseline_operating_cost=5000, upfront_cost=0,
                loan_amount=0, loan_apr=8, loan_term=60,
                monthly_nnn=0, monthly_payroll=0, monthly_utilities=0,
                monthly_insurance=0, monthly_maintenance=0, monthly_taxes=0)
LIMITS = {key: (0, 10000000) for key in DEFAULTS}
LIMITS.update(permit_months=(0, 36), build_months=(0, 36), delay=(0, 24),
              months=(1, 120), loan_term=(1, 360), loan_apr=(0, 50), bookings=(0, 100000))
INTEGERS = {'permit_months', 'build_months', 'delay', 'months', 'loan_term'}


def assumptions(inputs=None):
    values = DEFAULTS.copy()
    for key, raw in (inputs or {}).items():
        if key not in LIMITS or isinstance(raw, bool):
            raise ValueError(f'Invalid assumption: {key}')
        try:
            number = float(raw)
        except (ValueError, TypeError):
            raise ValueError(f'Invalid assumption: {key}') from None
        low, high = LIMITS[key]
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f'{key} must be between {low} and {high}')
        if key in INTEGERS and not number.is_integer():
            raise ValueError(f'{key} must be a whole number')
        values[key] = int(number) if key in INTEGERS else number
    if values['build_cost'] and not values['build_months']:
        raise ValueError('Build duration must be at least one month when build-out cost is positive.')
    return values


def payment(amount, apr, term):
    rate = apr / 1200
    return amount / term if not rate else amount * rate / (1 - (1 + rate) ** -term)


def analyze_money(inputs=None, *, proposed_use='indoor pickleball', ownership='lease', buyer=None):
    """Compare current-use and conversion scenarios; amounts are assumptions, not quotes."""
    if ownership not in {'lease', 'buy'}:
        raise ValueError('Choose lease or buy')
    v = assumptions(inputs)
    actual_buyer = buyer or {'starting_cash': v['starting_cash'], 'recurring_bills': [],
                             'source': 'Editable scenario assumptions (not a bank balance)'}
    base_cash = float(actual_buyer['starting_cash'])
    fixed = sum(float(b['monthly']) for b in actual_buyer['recurring_bills'])
    if not math.isfinite(base_cash) or not math.isfinite(fixed) or base_cash < 0 or fixed < 0:
        raise ValueError('Bank inputs must be finite, nonnegative amounts')
    rent = v['monthly_rent'] if ownership == 'lease' else 0
    monthly_payment = payment(v['loan_amount'], v['loan_apr'], v['loan_term'])
    income = v['bookings'] * v['booking_price'] if proposed_use in {'indoor pickleball', 'fitness studio'} else v['monthly_income']
    carry = v['monthly_carry'] + v['monthly_nnn'] + v['monthly_insurance'] + v['monthly_taxes']
    operating = v['monthly_operating_cost'] + v['monthly_payroll'] + v['monthly_utilities'] + v['monthly_maintenance']
    scenario = dict(name=proposed_use, upfront_cost=v['upfront_cost'],
                    permit_months=v['permit_months'], build_months=v['build_months'],
                    build_cost=v['build_cost'], monthly_carry=carry + rent,
                    monthly_operating_cost=operating, monthly_income=income)
    baseline = dict(name='Current-use business baseline', upfront_cost=v['upfront_cost'],
                    build_months=1, build_cost=v['baseline_build_cost'],
                    monthly_carry=carry + rent,
                    monthly_operating_cost=v['baseline_operating_cost'], monthly_income=v['baseline_income'])

    def project(s, delay=0, financing=False):
        bank = {**actual_buyer, 'starting_cash': base_cash + (v['loan_amount'] if financing else 0)}
        result = simulate(s, bank, v['months'], v['reserve'], delay)
        # Loan proceeds at month zero; payments begin month one and stop at term.
        for row in result['rows']:
            paid = monthly_payment * min(row['month'], v['loan_term']) if financing else 0
            if financing and row['month'] <= v['loan_term']:
                row['outflow'] = round(row['outflow'] + monthly_payment, 2)
            row['cash'] = round(row['cash'] - paid, 2)
        initial = bank['starting_cash'] - s.get('upfront_cost', 0)
        lowest = min([{'month': 0, 'cash': initial}] + result['rows'], key=lambda r: r['cash'])
        result.update(initial_cash=initial, lowest_cash=lowest['cash'], lowest_month=lowest['month'],
                      first_month_below_reserve=next((r['month'] for r in [{'month':0,'cash':initial}]+result['rows'] if r['cash'] < v['reserve']), None),
                      extra_funding_needed=max(0, round(v['reserve']-lowest['cash'], 2)))
        return result

    conversion = project(scenario, v['delay'])
    stress = project(scenario, v['delay'] + 3)
    # Search boundaries against the exact same deterministic projection, not LLM math.
    def feasible(cost, delay):
        return project({**scenario, 'build_cost': cost}, delay)['extra_funding_needed'] == 0
    max_cost = None
    if feasible(0, v['delay']) and v['build_months'] and v['permit_months'] + v['delay'] + v['build_months'] <= v['months']:
        lo, hi = 0., max(10000000., base_cash)
        for _ in range(40):
            mid = (lo + hi) / 2
            if feasible(mid, v['delay']): lo = mid
            else: hi = mid
        max_cost = max(0, math.floor(lo))
    possible = [d for d in range(25) if v['permit_months']+v['build_months']+d+1 <= v['months'] and feasible(v['build_cost'], d)]
    operating_break_even = fixed + rent + carry + operating
    boundaries = {'break_even_monthly_revenue': round(operating_break_even, 2),
                  'break_even_monthly_bookings': math.ceil(operating_break_even / v['booking_price']) if v['booking_price'] else None,
                  'max_build_cost': max_cost, 'max_delay_months': max(possible) if possible else None,
                  'delay_search_limit': 24, 'basis': 'Unfinanced, within selected horizon; break-even covers monthly operations, not recovery of build-out capital.'}
    gap = conversion['extra_funding_needed']
    summary = (f"Conversion needs ${gap:,.0f} more cash to maintain your ${v['reserve']:,.0f} reserve."
               if gap else f"Conversion stays above your ${v['reserve']:,.0f} reserve under these assumptions.")
    actions = [{'title':'Validate the operating assumptions', 'why':'Bookings, price and costs determine the cash result.',
                'owner':'Operator and contractor', 'request':'A priced build-out scope, operating budget and evidence of bookings at the target price.', 'source_ids':[]}]
    if ownership == 'buy':
        actions.append({'title':'Include acquisition and financing costs', 'why':'Purchase price, closing costs and debt service are not inferred from the property.',
                        'owner':'Buyer and lender', 'request':'Enter acquisition cash in upfront cost; include ongoing ownership costs in carry and financing terms separately.', 'source_ids':[]})
    return {'status':'assumption', 'summary':summary, 'findings':[], 'actions':actions,
            'inputs':v, 'ownership':ownership, 'proposed_use':proposed_use,
            'buyer_source':'Nessie sandbox account' if buyer else actual_buyer['source'], 'starting_cash':base_cash, 'bank_monthly_bills':fixed,
            'monthly_revenue':income, 'baseline':project(baseline), 'conversion':conversion,
            'stress':stress, 'boundaries':boundaries,
            'financing': {'label':'Simulated loan assumptions; no credit offer or loan created',
                          'monthly_payment':round(monthly_payment,2),
                          'result':project(scenario,v['delay'],True) if v['loan_amount'] else None},
            'assumptions_label':'All costs, income, capacity and dates are editable assumptions. Nessie, when connected, supplies sandbox cash and recurring bills; Python projects the balances.'}


def read_nessie_buyer(account_id):
    if not isinstance(account_id, str) or not account_id.isalnum() or len(account_id) > 64:
        raise ValueError('Invalid Nessie account ID')
    return buyer_from_nessie(account_id)
