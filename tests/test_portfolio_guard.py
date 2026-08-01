"""Tests for portfolio guard."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date

from common.portfolio_guard import PortfolioGuard, PortfolioGuardConfig, is_strategy_halted, write_halt_file


class TestPortfolioGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        active = os.path.join(self.tmp, 'trades', 'active', 'SKYLINE_SPX')
        os.makedirs(active, exist_ok=True)
        today = date.today().isoformat()
        with open(os.path.join(active, f'skyline_{today}.json'), 'w', encoding='utf-8') as f:
            json.dump({'date': today, 'realized_pnl': -2500, 'status': 'open'}, f)

    def test_detects_breach(self):
        guard = PortfolioGuard(
            PortfolioGuardConfig('SKYLINE_SPX', max_daily_loss=2000),
            root=self.tmp,
        )
        status = guard.check()
        self.assertTrue(status.breached)

    def test_halt_file_written(self):
        write_halt_file('SKYLINE_SPX', self.tmp, reason='test', action='halt_only')
        self.assertTrue(is_strategy_halted('SKYLINE_SPX', self.tmp))


if __name__ == '__main__':
    unittest.main()
