"""Tests for Perma Bull PCS profit-target stop profile."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from blocks.stop.monitor import StopMonitor
from blocks.stop.phases import PhaseAction
from blocks.stop.phases_profit_target import PhaseSpreadDebitProfitTarget
from blocks.stop.profiles.perma_bull_pcs import perma_bull_pcs_profile
from brokers.base import OrderResult
from brokers.journal_broker import JournalBroker


class TestPermaBullProfitTarget(unittest.TestCase):
    def test_profile_has_no_exchange_stop_phase(self):
        phases = perma_bull_pcs_profile().phases
        names = {p.name for p in phases}
        self.assertIn('phase_spread_debit_profit_target', names)
        self.assertNotIn('phase1_initial_stop', names)

    def test_monitor_uses_trade_profile_phases_not_global_meic(self):
        state = {
            'stop_profile': 'perma_bull_pcs',
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'short_leg': {'symbol': 'S', 'strike': 5500, 'fill_price': 2.0},
            'long_leg': {'symbol': 'L', 'strike': 5495, 'fill_price': 0.95},
            'exit_config': {'profit_target_debit': 0.15},
            'phases': {},
            'entry': {'net_credit': 1.05},
        }
        broker = MagicMock()
        prices = MagicMock()
        prices.get_option_mid.side_effect = lambda sym: 0.12 if sym == 'S' else 0.05

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            f.write('{}')
            path = f.name
        try:
            mon = StopMonitor(path, broker, prices, phases=None)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        mon.state = state
        mon.stop_profile = perma_bull_pcs_profile()
        mon.phases = sorted(mon.stop_profile.phases, key=lambda p: p.priority)

        phase = mon.phases[0]
        self.assertTrue(phase.should_activate(mon))
        mon.replace_with_spread_close = MagicMock()
        phase.execute(mon)
        mon.replace_with_spread_close.assert_called_once_with(
            reason='profit_target_hit',
            debit_limit=0.15,
        )

    def test_profit_target_phase_evaluate_none_when_mark_above_target(self):
        phase = PhaseSpreadDebitProfitTarget()
        state = {
            'status': 'open',
            'exit_config': {'profit_target_debit': 0.15},
            'short_leg': {'symbol': 'S'},
            'long_leg': {'symbol': 'L'},
        }
        prices = MagicMock()
        # short mid 0.50, long mid 0.10 -> mark = 0.40 > 0.15 debit target
        prices.get_option_mid.side_effect = lambda sym: 0.50 if sym == 'S' else 0.10
        mon = MagicMock()
        mon.state = state
        mon.prices = prices
        mon.stop_profile = perma_bull_pcs_profile()

        self.assertTrue(phase.should_activate(mon))
        self.assertEqual(phase.evaluate(mon), PhaseAction.NONE)

    def test_profit_target_phase_evaluate_exit_required_when_mark_hit(self):
        phase = PhaseSpreadDebitProfitTarget()
        state = {
            'status': 'open',
            'exit_config': {'profit_target_debit': 0.15},
            'short_leg': {'symbol': 'S'},
            'long_leg': {'symbol': 'L'},
        }
        prices = MagicMock()
        # short mid 0.18, long mid 0.05 -> mark = 0.13 <= 0.15 debit target
        prices.get_option_mid.side_effect = lambda sym: 0.18 if sym == 'S' else 0.05
        mon = MagicMock()
        mon.state = state
        mon.prices = prices
        mon.stop_profile = perma_bull_pcs_profile()

        self.assertTrue(phase.should_activate(mon))
        self.assertEqual(phase.evaluate(mon), PhaseAction.EXIT_REQUIRED)

    def test_paper_spread_close_fill_records_timestamp_and_stop_history(self):
        state = {
            'paper': True,
            'status': 'closing',
            'spread_close_order_id': 'paper-ord-123',
            'close_mechanism': 'profit_target_hit',
            'short_leg': {'symbol': 'S', 'strike': 5500, 'fill_price': 2.0},
            'long_leg': {'symbol': 'L', 'strike': 5495, 'fill_price': 0.95},
            'entry': {'net_credit': 1.05},
            'stop_history': [],
            'close': None,
        }
        broker = MagicMock()
        prices = MagicMock()
        prices.get_spx.return_value = 5510.0

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            f.write('{}')
            path = f.name
        try:
            mon = StopMonitor(path, broker, prices, phases=None)
            mon.state = state
            fill_result = OrderResult(
                success=True,
                order_id='paper-ord-123',
                status='filled',
                filled_price=0.15,
                filled_quantity=1,
            )
            mon._apply_spread_close_fill(fill_result)

            # Verify state was updated with exit timestamp
            self.assertEqual(mon.state.get('status'), 'closed')
            close_info = mon.state.get('close') or {}
            self.assertIsNotNone(close_info.get('timestamp'))
            self.assertEqual(close_info.get('reason'), 'profit_target_hit')
            self.assertEqual(close_info.get('short_close_price'), 0.15)

            # Verify stop_history recorded the fill
            history = mon.state.get('stop_history', [])
            filled_events = [e for e in history if e.get('action') == 'filled']
            self.assertTrue(len(filled_events) >= 1)
            fill_ev = filled_events[-1]
            self.assertEqual(fill_ev.get('reason'), 'profit_target_hit')
            self.assertEqual(fill_ev.get('price'), 0.15)
            self.assertIn('timestamp', fill_ev)

            # Ensure broker live order methods were never invoked
            broker.place_order.assert_not_called()
            broker.place_spread_order.assert_not_called()
            broker.place_oco_order.assert_not_called()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_journal_broker_blocks_live_order_and_simulates_spread_close(self):
        live_broker = MagicMock()
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            f.write('{}')
            orders_file = f.name
        try:
            jb = JournalBroker(live_broker, strategy_name='PERMA_BULL_PCS', orders_file=orders_file)

            # 1. Verify calling an unhandled live order mutation method raises RuntimeError
            with self.assertRaises(RuntimeError):
                jb.execute_order()

            # 2. Mock option prices: short 0.18, long 0.05 -> debit = 0.13 <= 0.15 debit limit
            jb.get_option_price = MagicMock(side_effect=lambda sym: 0.18 if sym == 'S' else 0.05)
            res = jb.place_spread_close_order('S', 'L', qty=1, debit_limit=0.15)
            self.assertTrue(res.success)
            self.assertEqual(res.status, 'filled')
            self.assertEqual(res.filled_price, 0.15)

            # 3. Verify no live orders ever touched the broker
            live_broker.place_spread_close_order.assert_not_called()
            live_broker.place_order.assert_not_called()
            live_broker.execute_order.assert_not_called()
        finally:
            try:
                os.unlink(orders_file)
            except OSError:
                pass


if __name__ == '__main__':
    unittest.main()
