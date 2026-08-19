"""Tests for Perma Bull PCS strategy."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from blocks.entry.spread_scan import SpreadCandidate
from strategies.perma_bull_pcs.strategy import PermaBullPCSConfig, PermaBullPCSStrategy


class TestPermaBullPCS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = PermaBullPCSConfig(
            name='PERMA_BULL_PCS',
            enabled=True,
            confirmed=True,
            paper=True,
            root=self.tmp,
            entry_time_et='08:45',
            spread_width=5,
            target_credit=1.0,
            quantity=1,
        )
        self.broker = MagicMock()
        self.strategy = PermaBullPCSStrategy(self.cfg, broker=self.broker)

    def test_schedule_empty(self):
        self.assertEqual(self.strategy.schedule(), [])

    def test_waits_until_central_entry_time(self):
        self.strategy._running = True
        slept = []

        def fake_sleep(sec):
            slept.append(sec)
            self.strategy._running = False

        with patch('strategies.perma_bull_pcs.strategy.util.central_now') as mock_now, \
             patch('strategies.perma_bull_pcs.strategy.time.sleep', side_effect=fake_sleep), \
             patch.object(self.strategy, '_is_disabled_today', return_value=False):
            mock_now.return_value = datetime(2026, 8, 11, 8, 30, 0)
            self.strategy._wait_for_entry_window(self.tmp)

        self.assertTrue(slept)
        self.assertGreater(slept[0], 0)

    def test_skips_when_paused_before_entry(self):
        with patch.object(self.strategy, '_wait_for_entry_window'), \
             patch('common.daily_overrides.is_disabled_for_today', return_value=True):
            self.strategy.run()
        session_path = os.path.join(
            self.tmp, 'trades', 'active', 'PERMA_BULL_PCS', f'session_{date.today().isoformat()}.json',
        )
        self.assertTrue(os.path.isfile(session_path))
        import json
        with open(session_path, encoding='utf-8') as f:
            state = json.load(f)
        self.assertFalse(state.get('entered'))
        self.assertEqual(state.get('skip_reason'), 'disabled_for_today')

    def test_places_pcs_and_writes_handshake(self):
        today = date.today()
        lot = f'perma-{today.isoformat()}'
        candidate = SpreadCandidate(
            short_symbol='SPXW  260808P05500000',
            long_symbol='SPXW  260808P05495000',
            short_strike=5500,
            long_strike=5495,
            market_credit=1.05,
            short_mid=2.0,
            long_mid=0.95,
            distance_from_target=0.05,
        )
        mock_entry = MagicMock()
        mock_entry.scan_for_target.return_value = [candidate]
        mock_entry.open_candidate.return_value = (
            candidate.short_symbol,
            candidate.long_symbol,
            'order-123',
            candidate.market_credit,
            candidate.short_strike,
            candidate.long_strike,
        )
        mock_entry.write_handshake.return_value = os.path.join(
            self.tmp, 'trades', 'active', 'PERMA_BULL_PCS', f'{lot}_P_test.json',
        )

        with patch.object(self.strategy, '_wait_for_entry_window'), \
             patch.object(self.strategy, '_is_disabled_today', return_value=False), \
             patch.object(self.strategy, '_check_trading_day', return_value=(True, 'ok')), \
             patch('blocks.entry.credit_spread.CreditSpreadEntry', return_value=mock_entry), \
             patch('common.streamer_symbols.register_option_symbols') as reg:
            self.strategy.run()

        mock_entry.scan_for_target.assert_called_once()
        args, kwargs = mock_entry.scan_for_target.call_args
        self.assertEqual(args[0], 'P')
        self.assertEqual(kwargs['spread_width'], 5)
        self.assertEqual(kwargs['target_credit'], 1.0)
        mock_entry.open_candidate.assert_called_once_with(candidate, 'P', 1, lot)
        mock_entry.write_handshake.assert_called_once()
        hs_kwargs = mock_entry.write_handshake.call_args.kwargs
        self.assertEqual(hs_kwargs['strategy'], 'PERMA_BULL_PCS')
        self.assertEqual(hs_kwargs['stop_profile'], 'perma_bull_pcs')
        self.assertEqual(hs_kwargs['exit_config'], {'profit_target_debit': 0.15})
        reg.assert_called_once()
        self.assertTrue(self.strategy.get_status().get('entered'))


if __name__ == '__main__':
    unittest.main()
