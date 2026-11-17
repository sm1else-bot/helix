import json
from pathlib import Path
import tempfile
import unittest

from offline_archive import atomic_json, index_scenario
from verify_offline_demos import verify_scenario


class ArchiveTests(unittest.TestCase):
    def test_incomplete_and_tampered_scenario_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / 'scenarios' / 'example'
            case.mkdir(parents=True)
            atomic_json(case / 'manifest.json', {
                'scenario': 'example', 'status': 'PARTIAL',
                'property_id': 1, 'listing_id': 2, 'space_id': 3,
            })
            atomic_json(case / 'stage.json', {'result': 'original'})
            index_scenario(case)
            self.assertTrue(any('not verified complete' in x for x in verify_scenario(root, 'example')))
            (case / 'stage.json').write_text('{"result":"changed"}')
            self.assertTrue(any('checksum mismatch' in x for x in verify_scenario(root, 'example')))

    def test_inventory_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = root / 'scenarios' / 'example'
            case.mkdir(parents=True)
            atomic_json(case / 'manifest.json', {
                'scenario': 'example', 'status': 'VERIFIED',
                'property_id': 1, 'listing_id': 2, 'space_id': 3,
                'assets': [{'path': '../../../elsewhere', 'sha256': 'a' * 64}],
            })
            self.assertTrue(any('outside scenario' in x for x in verify_scenario(root, 'example')))

    def test_failed_write_preserves_existing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'checkpoint.json'
            atomic_json(path, {'ok': True})
            with self.assertRaises(ValueError):
                atomic_json(path, {'bad': float('nan')})
            self.assertEqual(json.loads(path.read_text()), {'ok': True})
            self.assertEqual([p.name for p in path.parent.iterdir()], ['checkpoint.json'])


if __name__ == '__main__':
    unittest.main()
