"""Tests for stable trade filename and order_history on handshake retry."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from blocks.entry.handshake import write_credit_spread_handshake
from blocks.stop import state as state_mod


class TestOrderHistory(unittest.TestCase):
    def test_stable_filename_and_history_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path1 = write_credit_spread_handshake(
                lot='01-15',
                side='P',
                short_symbol='.SPXW260625P7335',
                long_symbol='.SPXW260625P7310',
                short_strike=7335,
                long_strike=7310,
                quantity=3,
                open_order_id='111111',
                limit_credit=0.90,
                active_directory=tmp,
                entry_ts='20260625T131403',
            )
            self.assertTrue(path1.endswith('01-15_P_20260625T131403.json'))
            st1 = state_mod.load_state(path1)
            self.assertEqual(len(st1['order_history']), 1)
            self.assertEqual(st1['order_history'][0]['order_id'], '111111')
            self.assertEqual(st1['order_history'][0]['reason'], 'placed')

            path2 = write_credit_spread_handshake(
                lot='01-15',
                side='P',
                short_symbol='.SPXW260625P7320',
                long_symbol='.SPXW260625P7295',
                short_strike=7320,
                long_strike=7295,
                quantity=3,
                open_order_id='222222',
                limit_credit=0.85,
                active_directory=tmp,
                existing_path=path1,
                reason='cancelled_for_chase',
            )
            self.assertEqual(path1, path2)
            st2 = state_mod.load_state(path2)
            self.assertEqual(st2['open_order_id'], '222222')
            self.assertEqual(st2['short_leg']['strike'], 7320)
            self.assertEqual(len(st2['order_history']), 2)
            self.assertEqual(st2['order_history'][1]['reason'], 'cancelled_for_chase')
            self.assertEqual(st2['order_history'][1]['short_strike'], 7320)

    def test_create_pending_includes_order_history(self):
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='02-00',
            side='P',
            short_symbol='.SPXW260625P7000',
            long_symbol='.SPXW260625P6975',
            short_strike=7000,
            long_strike=6975,
            target_quantity=1,
            open_order_id='999',
            limit_credit=1.0,
        )
        self.assertEqual(st['order_history'], [])


if __name__ == '__main__':
    unittest.main()
