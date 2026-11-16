import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stage3_engine import adapter


class Stage3AdapterTests(unittest.TestCase):
    def test_preserves_engine_result_and_reserve_absence(self):
        original={'inputs':{'suite':'listing:38268171:7205','reserve':'unavailable'},'gates':[{'business':str(i)} for i in range(26)],'shortlist':[{'stage4_inputs':{'reserve':'unavailable'}}],'missing_evidence':[{'question':'reserve?'}]}
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'result':original}))
        with patch.object(adapter.subprocess,'run',return_value=response) as run:
            actual=adapter.run(4809147,900000,kit='kit.zip')
        self.assertEqual(actual,original)
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertIsNone(payload['reserve'])
        self.assertEqual(run.call_args.kwargs['env']['JURISDICTION_OFFLINE'],'1')

    def test_invalid_or_unmatched_suite_is_rejected(self):
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'result':{'inputs':{'suite':'listing:1:100'}}}))
        with patch.object(adapter.subprocess,'run',return_value=response):
            with self.assertRaisesRegex(ValueError,'suite identifier'): adapter.run(1,900000,suite='19822761')
        with self.assertRaises(ValueError): adapter.run(1,900000,reserve=-1)
        with self.assertRaises(ValueError): adapter.run(1,float('nan'))

    def test_explicit_zero_reserve_and_original_suite_are_forwarded(self):
        output={'inputs':{'suite':'listing:38268171:7205','reserve':0}}
        response=SimpleNamespace(returncode=0,stdout=json.dumps({'result':output}))
        with patch.object(adapter.subprocess,'run',return_value=response) as run:
            self.assertEqual(adapter.run(4809147,900000,reserve=0,suite='listing:38268171:7205'),output)
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['reserve'],0)
        self.assertEqual(payload['suite'],'listing:38268171:7205')
