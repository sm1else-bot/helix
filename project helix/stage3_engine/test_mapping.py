import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

WORKER = Path(__file__).resolve().parent / 'worker.py'


class Stage3MappingTests(unittest.TestCase):
    def run_fixture(self, spaces, listing=123, space=45):
        with tempfile.TemporaryDirectory() as directory:
            kit=Path(directory)/'kit.zip'
            data={'id':123,'listingStatus':1,'forLeaseSummary':{'spaces':spaces}}
            with ZipFile(kit,'w') as z:
                z.writestr('data/commercial-listing-summary.jsonl',json.dumps(data)+'\n')
            payload={'property_id':1,'budget':900000,'reserve':None,'kit':str(kit),'listing_id':listing,'space_id':space}
            env={**os.environ,'COSTAR_KIT_PASSWORD':'','JURISDICTION_OFFLINE':'1','NEIGHBORHOOD_OFFLINE':'1'}
            result=subprocess.run([sys.executable,str(WORKER)],input=json.dumps(payload),text=True,capture_output=True,env=env,timeout=20)
            return result.returncode,json.loads(result.stdout)

    def test_rejects_unknown_space_instead_of_falling_back(self):
        status,body=self.run_fixture([{'propertyId':1,'id':46,'squareFeetMax':7205}])
        self.assertNotEqual(status,0)
        self.assertIn('not uniquely available',body['error'])

    def test_rejects_two_spaces_with_same_original_key(self):
        status,body=self.run_fixture([{'propertyId':1,'id':45,'squareFeetMax':7205},{'propertyId':1,'id':46,'squareFeetMax':7205}])
        self.assertNotEqual(status,0)
        self.assertIn('share this Stage 3 suite key',body['error'])
