import unittest
from unittest.mock import patch
from finance_lab.engine import simulate, create_loan


class FinanceLabTests(unittest.TestCase):
    def setUp(self):
        self.data = {'business':'indoor_pickleball','area_sqft':7205,'monthly_rent':10087,'budget':900000,'reserve':50000,'build_months':3,'cost_scenario':'base','financing':'cash','loan_principal':0,'apr':9,'loan_months':60,'delay_months':3}

    def test_rent_budget_loan_and_delay_change_results(self):
        base=simulate(self.data)
        loan=simulate({**self.data,'financing':'loan','loan_principal':300000})
        rent=simulate({**self.data,'monthly_rent':15000})
        poor=simulate({**self.data,'budget':10000})
        self.assertGreater(loan['loan']['monthly_payment'],0)
        self.assertGreater(loan['simulation']['base']['loan']['on_time']['rows'][11]['cash'],base['simulation']['base']['cash']['on_time']['rows'][11]['cash'])
        self.assertGreater(rent['capital']['cash_to_open'],base['capital']['cash_to_open'])
        self.assertGreater(poor['capital']['cash_gap'],0)
        self.assertEqual(base['simulation']['base']['cash']['delayed']['opening_month']-base['simulation']['base']['cash']['on_time']['opening_month'],3)

    def test_missing_rent_is_not_a_complete_total(self):
        result=simulate({**self.data,'monthly_rent':None})
        self.assertNotIn('capital',result)
        self.assertIsNone(result['simulation'])

    def test_low_high_and_different_business_are_distinct(self):
        low=simulate({**self.data,'cost_scenario':'low'})
        high=simulate({**self.data,'cost_scenario':'high'})
        gym=simulate({**self.data,'business':'fitness_gym','area_sqft':5941,'monthly_rent':3218.04})
        self.assertLess(low['capital']['cash_to_open'],high['capital']['cash_to_open'])
        self.assertNotEqual(gym['capital']['cash_to_open'],low['capital']['cash_to_open'])

    def test_selected_sandbox_bills_affect_outflow(self):
        result=simulate({**self.data,'nessie_bills':[{'payee':'Sandbox bill','monthly':3000}]})
        self.assertEqual(result['simulation']['base']['cash']['on_time']['rows'][0]['outflow'],13087)

    def test_recorded_building_ceiling_flags_pickleball(self):
        facts={'type':'2','subtype':'','gba':7205,'stories':1,'ceiling_ft':14,'ceiling_source':'propertyIndustrial','drive_in_bays':None,'docks':None}
        result=simulate(self.data,property_data=facts)
        self.assertEqual(result['physical_fit']['status'],'no')
        self.assertTrue(any('14 ft' in warning for warning in result['warnings']))
        self.assertTrue(any('building-level' in warning for warning in result['warnings']))

    def test_loan_creation_requires_confirmation(self):
        body={'account_id':'sandbox-123','amount':300000,'apr':9,'term_months':60}
        with patch('finance_lab.engine.nessie_loan.create',return_value={'status':'pending'}) as create:
            with self.assertRaises(ValueError): create_loan(body)
            create.assert_not_called()
            self.assertEqual(create_loan({**body,'confirm':True})['status'],'pending')
            create.assert_called_once_with('sandbox-123',300000,9.0,60)

    def test_provided_scenario_cash_and_loan_are_distinguished(self):
        data={**self.data,'budget':320000,'loan_principal':100000,'nessie_bills':[{'payee':'Suite base rent (NNN)','monthly':8500},{'payee':'NNN charges (taxes/insurance/CAM)','monthly':2000}]}
        cash=simulate(data)
        self.assertEqual(cash['capital']['cash_to_open'],595307)
        self.assertEqual(cash['capital']['cash_gap'],325307)
        self.assertEqual(cash['capital']['financed_gap'],325307)
        self.assertEqual(cash['capital']['proposed_loan_gap'],225307)
        self.assertTrue(any('double-counting' in w for w in cash['warnings']))
        self.assertEqual(cash['simulation']['base']['cash']['on_time']['rows'][0]['cash'],-265633)
        self.assertEqual(cash['simulation']['base']['cash']['on_time']['rows'][11]['cash'],-492090)
        self.assertEqual(cash['simulation']['base']['loan']['on_time']['rows'][11]['cash'],-417000)
        financed=simulate({**data,'financing':'loan'})
        self.assertEqual(financed['capital']['financed_gap'],225307)

    def test_preopening_base_rent_is_not_charged_twice_and_delay_is_explicit(self):
        result=simulate(self.data)
        run=result['simulation']['base']['cash']
        self.assertEqual(run['upfront_cost'],result['ledger']['cash_to_open']['base']-30261)
        self.assertEqual(run['on_time']['rows'][0]['outflow'],10087)
        self.assertEqual(run['delayed']['opening_month'],run['on_time']['opening_month']+3)
        self.assertEqual(run['delayed']['rows'],run['on_time']['rows'])
        self.assertTrue(any('No extra delay-specific costs' in w for w in result['warnings']))
