"""Tests for multi-strategy history adapter."""
from __future__ import annotations

import unittest

from common.history_adapter import (
    is_skyline_trade,
    is_syncable_trade_path,
    skyline_trade_to_history_row,
)


class TestHistoryAdapter(unittest.TestCase):
    def test_skip_session_and_halt_files(self):
        self.assertFalse(is_syncable_trade_path('/trades/active/SKYLINE_SPX/session_2026-07-30.json'))
        self.assertFalse(is_syncable_trade_path('/trades/active/SKYLINE_SPX/halted_2026-07-30.json'))
        self.assertTrue(is_syncable_trade_path('/trades/active/SKYLINE_SPX/skyline_2026-07-30.json'))

    def test_is_skyline_trade(self):
        trade = {'strategy': 'SKYLINE_SPX', 'structures': [{'name': 'BF1'}]}
        self.assertTrue(is_skyline_trade(trade))
        self.assertFalse(is_skyline_trade({'strategy': 'MEIC_IC', 'entry': {}}))

    def test_skyline_closed_row_includes_paper_flag(self):
        trade = {
            'strategy': 'SKYLINE_SPX',
            'date': '2026-07-30',
            'status': 'closed',
            'paper': True,
            'quantity': 1,
            'net_debit_total': 4.5,
            'realized_pnl_total': 1.25,
            'atm': 6400,
            'structures': [
                {'name': 'BF1', 'status': 'closed', 'legs': [{'symbol': '.SPXW', 'action': 'BUY_TO_OPEN', 'qty': 1}]},
            ],
        }
        row = skyline_trade_to_history_row(trade)
        self.assertIsNotNone(row)
        self.assertEqual(row['strategy'], 'SKYLINE_SPX')
        self.assertEqual(row['side'], 'X')
        self.assertEqual(row['paper'], 1)
        self.assertEqual(row['pnl'], 125.0)

    def test_skyline_open_without_settlement_not_synced(self):
        trade = {
            'strategy': 'SKYLINE_SPX',
            'date': '2026-07-30',
            'status': 'open',
            'structures': [{'name': 'BF1', 'status': 'filled', 'legs': []}],
        }
        self.assertIsNone(skyline_trade_to_history_row(trade))


if __name__ == '__main__':
    unittest.main()
