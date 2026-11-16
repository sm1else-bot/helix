import json
import unittest

import final_report
from test_stage4_handoff import chain


class FinalReportTests(unittest.TestCase):
    def setUp(self):
        self.iid, self.stage1, self.handoff, self.stage3 = chain()
        self.handoff['dossier'].update({'reported_building_facts': {'costar': {'property_type': 'Industrial'}}, 'source_status': {}, 'visual_analysis': {}})

    def test_without_selection_the_verdict_waits_and_nothing_is_invented(self):
        report = final_report.build(self.iid, handoff=self.handoff, stage3=self.stage3, report=None, selection=None)
        self.assertEqual(report['verdict']['code'], 'pending')
        self.assertIsNone(report['financial']['run'])
        self.assertEqual([c['business'] for c in report['opportunities']['candidates']], ['b0'])

    def test_funding_gap_means_do_not_proceed(self):
        rows = [{'month': m, 'cash': 1000 - m, 'outflow': 1} for m in range(1, 13)]
        sim = {k: {'cash': {'on_time': {'rows': rows, 'opening_month': 4}, 'delayed': {'rows': rows}, 'monthly_bills': []}} for k in ('low', 'base', 'high')}
        selection = {'business': 'b0', 'label': 'Business 0', 'rank': 1, 'stage3': {}, 'latest_run': {'at': 'now', 'request': {'cost_scenario': 'base', 'financing': 'cash'},
                     'result': {'capital': {'cash_gap': 25000, 'cash_to_open': 900000, 'reserve': 50000, 'cash_headroom': 0, 'budget': 925000}, 'simulation': sim}}}
        report = final_report.build(self.iid, handoff=self.handoff, stage3=self.stage3, report=None, selection=selection)
        self.assertEqual(report['verdict']['code'], 'not_funded')
        self.assertIn('25,000', report['verdict']['headline'])
        json.dumps(report)

    def test_engine_size_condition_is_reworded_without_changing_numbers(self):
        text = final_report.tidy('Martial arts / boxing: Smallest option is 7,205 sq ft (the chosen listed space), above the typical 1,200-6,000 sq ft: only works if part of it can be leased')
        self.assertIn('7,205', text); self.assertIn('1,200–6,000', text); self.assertIn('part of it can be leased', text)


if __name__ == '__main__':
    unittest.main()
