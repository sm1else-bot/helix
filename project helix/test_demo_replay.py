import os
import unittest
from unittest.mock import patch

import demo_replay


class DemoReplayTests(unittest.TestCase):
    def test_off_unless_flag_is_set(self):
        with patch.dict(os.environ, {'HELIX_DEMO_REPLAY': ''}):
            self.assertIsNone(demo_replay.discovery_response({'city': 'Los Angeles, CA'}, {}, {}))
            self.assertIsNone(demo_replay.saved_stage3('a' * 32, None, {'a' * 32: {'inputs': {'reserve': 'unavailable'}}}))

    def test_saved_stage3_only_when_reserve_unchanged(self):
        saved = {'a': {'inputs': {'reserve': 'unavailable'}}, 'b': {'inputs': {'reserve': 50000.0}}}
        with patch.dict(os.environ, {'HELIX_DEMO_REPLAY': '1'}):
            self.assertIs(demo_replay.saved_stage3('a', None, saved), saved['a'])
            self.assertIsNone(demo_replay.saved_stage3('a', '50000', saved))
            self.assertIs(demo_replay.saved_stage3('b', '50000', saved), saved['b'])
            self.assertIsNone(demo_replay.saved_stage3('b', None, saved))
            self.assertIsNone(demo_replay.saved_stage3('missing', None, saved))


if __name__ == '__main__':
    unittest.main()
