import io
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import opportunity_report as report


def fixture():
    iid='a'*32
    listing={'property_id':4809147,'listing_id':'38268171','space_id':'19822761','stable_id':'lease:38268171:19822761','area_max_sqft':7205,'explanation':'untrusted old explanation'}
    intake={'total_budget_usd':900000}
    stage1={'discovery_id':'discovery','intake':intake,'report':{'discovery_id':'discovery','results':[listing], 'trace':{}}}
    handoff={'handoff_id':iid,'discovery_id':'discovery','selected_listing':listing,'intake':intake,'dossier':{'selected_property':{k:listing[k] for k in ('property_id','listing_id','space_id')},'physical_observations':[{'kind':'direct_visual_observation','feature':'roof','provider':'Vexcel'}]}}
    candidate={'business':'b0','label':'Business 0','stage4_inputs':{'property_id':4809147,'suite':'listing:38268171:7205'}}
    stage3={'schema_version':'stage3.v1','inputs':{'property_id':4809147,'budget':900000,'suite':'listing:38268171:7205'},'summary':{'shortlist':['Business 0']},'gates':[{'business':f'b{i}'} for i in range(26)],'shortlist':[candidate],'missing_evidence':[]}
    stage3['reproduce']={'output_hash':hashlib.sha256(json.dumps(stage3,sort_keys=True,default=str).encode()).hexdigest()[:16]}
    return iid,stage1,handoff,stage3


def model_response():
    claim={'text':'Evidence needs review.','kind':'unresolved_question','evidence_ids':['s1:selected']}
    result={section:[claim] for section in report.SECTIONS};result['candidates']=[{'business':'b0','claims':[{'text':'Needs review.','kind':'unresolved_question','evidence_ids':['s3:candidate:b0']}]}]
    return {'model':report.MODEL,'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result)}}], 'usage':{'prompt_tokens':100,'completion_tokens':50,'cost':0.0007}}


class ReportTests(unittest.TestCase):
    def test_generation_preserves_all_sources_and_cost(self):
        iid,stage1,handoff,stage3=fixture()
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        requests=[]
        def opener(request,timeout):
            requests.append(json.loads(request.data));return Response(json.dumps(model_response()).encode())
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'REPORT_DIR',Path(tmp)/'reports'),patch.object(report,'USAGE_PATH',Path(tmp)/'usage.jsonl'):
            output=report.generate(iid,stage1,handoff,stage3,'test-key',opener=opener)
            self.assertEqual(output['model']['usage']['cost'],0.0007)
            self.assertEqual(len(output['screening']['all_outcomes']),26)
            self.assertEqual(report.read_report(iid),output)
            for i,source in enumerate((stage1,handoff,stage3),1): self.assertEqual(report.read_source(iid,output['report_id'],i),source)
            self.assertEqual(len(Path(tmp,'usage.jsonl').read_text().splitlines()),1)
        self.assertEqual(requests[0]['model'],'openai/gpt-6.1-sol')
        self.assertNotIn('untrusted old explanation',requests[0]['messages'][1]['content'])
        self.assertIn('s2:observation:0',output['evidence_index'])

    def test_named_suite_matches_stage3_even_when_listing_dropped_it(self):
        iid,stage1,handoff,stage3=fixture()
        for record in (handoff['selected_listing'], stage1['report']['results'][0]):
            record['original_evidence']={'space':{'id':19822761,'suite':'350'}}
        stage3['inputs']['suite']='350'; stage3['shortlist'][0]['stage4_inputs']['suite']='350'
        stage3['reproduce']['output_hash']=hashlib.sha256(json.dumps({k:v for k,v in stage3.items() if k not in ('generated_at','reproduce')},sort_keys=True,default=str).encode()).hexdigest()[:16]
        self.assertEqual(report.validate_sources(iid,stage1,handoff,stage3)['stage3_suite'],'350')
        handoff['selected_listing']['suite']='350'
        self.assertEqual(report.validate_sources(iid,stage1,handoff,stage3)['stage3_suite'],'350')
        stage3['inputs']['suite']='listing:38268171:7205'
        stage3['reproduce']['output_hash']=hashlib.sha256(json.dumps({k:v for k,v in stage3.items() if k not in ('generated_at','reproduce')},sort_keys=True,default=str).encode()).hexdigest()[:16]
        with self.assertRaisesRegex(ValueError,'suite'): report.validate_sources(iid,stage1,handoff,stage3)

    def test_missing_stage1_and_wrong_identity_fail_before_call(self):
        iid,stage1,handoff,stage3=fixture();calls=[]
        with self.assertRaisesRegex(ValueError,'complete Stage 1'):
            report.generate(iid,None,handoff,stage3,'key',opener=lambda *x,**y:calls.append(1))
        stage3['inputs']['suite']='wrong-suite'
        stage3['reproduce']['output_hash']=hashlib.sha256(json.dumps({k:v for k,v in stage3.items() if k not in ('generated_at','reproduce')},sort_keys=True,default=str).encode()).hexdigest()[:16]
        with self.assertRaisesRegex(ValueError,'suite'):
            report.generate(iid,stage1,handoff,stage3,'key',opener=lambda *x,**y:calls.append(1))
        self.assertEqual(calls,[])

    def test_rejects_invalid_evidence_and_changed_ranking(self):
        value=json.loads(model_response()['choices'][0]['message']['content'])
        value['executive_thesis'][0]['evidence_ids']=['not-real']
        with self.assertRaisesRegex(ValueError,'missing evidence'): report.validate_narrative(value,{'s1:selected'},['b0'])
        value=json.loads(model_response()['choices'][0]['message']['content']);value['candidates'][0]['business']='b1'
        with self.assertRaisesRegex(ValueError,'shortlist'): report.validate_narrative(value,{'s1:selected'},['b0'])

    def test_tampered_screening_fails_before_model_call(self):
        iid,stage1,handoff,stage3=fixture()
        stage3['gates'][0]['business']='tampered'
        calls=[]
        with self.assertRaisesRegex(ValueError,'hash'):
            report.generate(iid,stage1,handoff,stage3,'key',opener=lambda *a,**kw:calls.append(1))
        self.assertEqual(calls,[])

    def test_usage_logged_when_model_output_is_invalid(self):
        iid,stage1,handoff,stage3=fixture()
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self,*args): self.close()
        invalid=model_response()
        invalid['choices'][0]['message']['content']='not json'
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'USAGE_PATH',Path(tmp)/'usage.jsonl'):
            with self.assertRaises(ValueError):
                report.generate(iid,stage1,handoff,stage3,'key',opener=lambda *a,**kw:Response(json.dumps(invalid).encode()))
            self.assertEqual(json.loads(Path(tmp,'usage.jsonl').read_text())['usage']['cost'],0.0007)
