"""Tests for manual close primitive."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date

from common.manual_close import close_structure
from tests.mock_broker import MockBroker


class TestManualClose(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        active = os.path.join(self.tmp, 'trades', 'active', 'SKYLINE_SPX')
        os.makedirs(active, exist_ok=True)
        today = date.today().isoformat()
        trade = {
            'strategy': 'SKYLINE_SPX',
            'date': today,
            'status': 'open',
            'structures': [{
                'name': 'BF1',
                'status': 'filled',
                'net_debit': 2.0,
                'legs': [
                    {'symbol': '.SPXW260730C6400', 'action': 'BUY_TO_OPEN', 'qty': 1},
                    {'symbol': '.SPXW260730C6425', 'action': 'SELL_TO_OPEN', 'qty': 2},
                    {'symbol': '.SPXW260730C6450', 'action': 'BUY_TO_OPEN', 'qty': 1},
                ],
            }],
        }
        with open(os.path.join(active, f'skyline_{today}.json'), 'w', encoding='utf-8') as f:
            json.dump(trade, f)

    def test_close_structure_reverses_legs(self):
        from unittest.mock import patch
        broker = MockBroker()
        with patch('common.manual_close.get_shared_broker_for_strategy', return_value=broker):
            result = close_structure('SKYLINE_SPX', 'BF1', root=self.tmp, paper=True)
        self.assertTrue(result.success)
        self.assertEqual(len(broker.placed), 1)
        self.assertEqual(broker.placed[0][0], 'multi_leg')


if __name__ == '__main__':
    unittest.main()
