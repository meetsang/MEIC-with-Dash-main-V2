"""Tests for StandaloneStrategyRunner."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from common.standalone_runner import StandaloneStrategyRunner
from strategies.skyline.strategy import SkylineConfig, SkylineStrategy


class TestStandaloneRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = SkylineConfig(
            name='SKYLINE_SPX',
            enabled=True,
            confirmed=True,
            paper=True,
            root=self.tmp,
        )
        self.strategy = SkylineStrategy(self.cfg)

    def test_skips_reentry_when_session_entered(self):
        active = os.path.join(self.tmp, 'trades', 'active', 'SKYLINE_SPX')
        os.makedirs(active, exist_ok=True)
        today = date.today().isoformat()
        with open(os.path.join(active, f'session_{today}.json'), 'w', encoding='utf-8') as f:
            json.dump({'entered': True, 'skip_reason': None, 'overrides': {}}, f)

        runner = StandaloneStrategyRunner(self.strategy, root=self.tmp)
        with patch.object(runner, '_register_open_trade_streamer_legs') as reg:
            runner.start()
            reg.assert_called_once()
        self.assertFalse(runner._running)

    def test_saves_session_state(self):
        runner = StandaloneStrategyRunner(self.strategy, root=self.tmp)
        runner.save_session_state(entered=True, skip_reason=None)
        state = runner._load_session_state()
        self.assertTrue(state.get('entered'))


if __name__ == '__main__':
    unittest.main()
