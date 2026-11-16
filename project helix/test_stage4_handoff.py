import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import opportunity_report as report
import stage4_handoff as stage4
from test_opportunity_report import fixture, model_response


def rehash(stage3):
    body = {k: v for k, v in stage3.items() if k not in ('generated_at', 'reproduce')}
    stage3['reproduce'] = {'output_hash': hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]}


def chain():
    iid, stage1, handoff, stage3 = fixture()
    handoff['intake']['financing'] = 'cash'
    stage3['inputs'].update({'market': 'los_angeles', 'reserve': 'unavailable'})
    stage3['shortlist'][0].update({'rank': 1, 'conditions': ['check height'], 'unknowns': [], 'verification_questions': ['zoning?']})
    stage3['shortlist'][0]['stage4_inputs'].update({'address': '1250 S Broadway', 'space_sf': 7205.0, 'monthly_rent': 10087,
        'rent_source': 'CoStar asking rent', 'build_months': 3, 'cash_to_open': {'low': 270001, 'base': 530432, 'high': 790862}})
    rehash(stage3)
    return iid, stage1, handoff, stage3


class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [patch.object(report, 'REPORT_DIR', tmp / 'reports'), patch.object(report, 'USAGE_PATH', tmp / 'usage.jsonl'),
                        patch.object(stage4, 'SELECTIONS_PATH', tmp / 'selections.json')]
        for p in self.patches: p.start()
        self.iid, self.stage1, self.handoff, self.stage3 = chain()
        self.report = report.generate(self.iid, self.stage1, self.handoff, self.stage3, 'key',
                                      opener=lambda req, timeout: Response(json.dumps(model_response()).encode()))

    def tearDown(self):
        for p in self.patches: p.stop()
        self.tmp.cleanup()

    def sources(self, **override):
        values = dict(stage1=self.stage1, handoff=self.handoff, stage3=self.stage3, report=report.read_report(self.iid),
                      report_stage3=report.read_source(self.iid, self.report['report_id'], 3))
        values.update(override)
        return values

    def test_selection_carries_identity_budget_reserve_and_stage3_costs(self):
        record = stage4.select(self.iid, 'b0', self.report['report_id'], **self.sources())
        self.assertEqual(record['identity']['space_id'], '19822761')
        self.assertEqual(record['identity']['stage3_suite'], 'listing:38268171:7205')
        self.assertEqual(record['market'], 'Los Angeles')
        self.assertEqual(record['budget'], 900000.0)
        self.assertIsNone(record['reserve'])
        self.assertEqual(record['reserve_status'], 'unavailable')
        self.assertEqual(record['stage3']['cash_to_open']['base'], 530432)
        self.assertEqual(record['stage3']['output_hash'], self.stage3['reproduce']['output_hash'])
        self.assertTrue(record['report_claims'])
        self.assertEqual(stage4.read(self.iid), record)

    def test_rejects_unlisted_business_missing_report_and_stale_screening(self):
        with self.assertRaisesRegex(ValueError, 'shortlisted'):
            stage4.select(self.iid, 'b5', self.report['report_id'], **self.sources())
        with self.assertRaisesRegex(ValueError, 'opportunity report'):
            stage4.select(self.iid, 'b0', 'f' * 32, **self.sources())
        rerun = json.loads(json.dumps(self.stage3)); rerun['inputs']['reserve'] = 50000; rerun['shortlist'][0]['stage4_inputs']['reserve'] = 50000; rehash(rerun)
        with self.assertRaisesRegex(ValueError, 'Regenerate'):
            stage4.select(self.iid, 'b0', self.report['report_id'], **self.sources(stage3=rerun))
        self.assertIsNone(stage4.read(self.iid))

    def test_finance_run_must_keep_identity_and_records_deviations(self):
        stage4.select(self.iid, 'b0', self.report['report_id'], **self.sources())
        payload = {'investigation_id': self.iid, 'property_id': 4809147, 'space_id': '19822761', 'business': 'b0', 'area_sqft': 7205,
                   'budget': 900000, 'reserve': 50000, 'monthly_rent': 10087, 'build_months': 3}
        stage4.check_run(self.iid, payload)
        for field, value in (('space_id', '1'), ('business', 'b1'), ('area_sqft', 5000)):
            with self.assertRaises(ValueError): stage4.check_run(self.iid, {**payload, field: value})
        result = stage4.record_run(self.iid, payload, {'ledger': {'cash_to_open': {'low': 270001, 'base': 530432, 'high': 790862}}})
        self.assertEqual([d['field'] for d in result['handoff']['deviations']], ['reserve'])
        self.assertTrue(all(x['matches'] for x in result['handoff']['stage3_cash_to_open_consistency'].values()))
        self.assertEqual(stage4.read(self.iid)['latest_run']['request'], payload)


if __name__ == '__main__':
    unittest.main()
