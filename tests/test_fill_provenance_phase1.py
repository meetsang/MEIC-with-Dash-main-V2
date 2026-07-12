"""Phase 1 fill provenance, bounded sync, and ownership tests."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.fill_provenance import maybe_run_fill_audit
from blocks.stop.fill_sync import sync_open_order
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.pending_fill_sync import needs_open_order_sync, sync_pending_fills
from dashboard.manual_spread_handlers import build_manual_trades
from dashboard.server import _read_active_trades
from tests.fill_sync_fixtures import same_day_trade_env, spxw_option_symbol


class TestJul9MissingLegScenario(unittest.TestCase):
    """Replay 01-15_P: short broker fill, long API empty, limit 0.75 → estimate 0.40."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.trades_dir = os.path.join(self.root, 'trades', 'active', 'MEIC_IC')
        os.makedirs(self.trades_dir, exist_ok=True)

    def _jul9_state(self):
        with same_day_trade_env() as expiry:
            short_sym = spxw_option_symbol(7530, 'P', expiry_yymmdd=expiry)
            long_sym = spxw_option_symbol(7485, 'P', expiry_yymmdd=expiry)
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='01-15_P',
            side='P',
            short_symbol=short_sym,
            long_symbol=long_sym,
            short_strike=7530,
            long_strike=7485,
            target_quantity=1,
            open_order_id='482348463',
            limit_credit=0.75,
        )
        st['entry']['limit_credit'] = 0.75
        return st

    def _broker_missing_long(self):
        broker = MagicMock()

        def status(_oid, **kwargs):
            return OrderResult(
                True,
                '482348463',
                'filled',
                filled_quantity=1,
                order_quantity=1,
                filled_price=0.75,
                filled_price_source='order_limit_fallback',
                order_limit_price=0.75,
                short_fill_price=1.15,
                long_fill_price=None,
            )

        broker.get_order_status.side_effect = status
        return broker

    def test_protective_estimate_promotes_open(self):
        state = self._jul9_state()
        broker = self._broker_missing_long()

        changed1, _ = sync_open_order(state, broker, force=True)
        self.assertTrue(changed1)
        self.assertEqual(state['open_order']['fill_sync']['phase'], 'confirm_pending')

        changed2, _ = sync_open_order(state, broker, force=True)
        self.assertTrue(changed2)
        self.assertEqual(state['status'], 'open')
        self.assertEqual(state['short_leg']['fill_price'], 1.15)
        self.assertEqual(state['long_leg']['fill_price'], 0.40)
        self.assertEqual(state['entry']['fill_confidence'], 'protective_estimate')
        self.assertEqual(state['open_order']['fill_sync']['phase'], 'resolved_estimated')
        self.assertFalse(needs_open_order_sync(state))
        self.assertEqual(broker.get_order_status.call_count, 2)

    @patch('blocks.stop.state.iter_active_trade_paths')
    def test_no_afternoon_polling_after_resolution(self, mock_iter):
        path = os.path.join(self.trades_dir, '01-15_P_test.json')
        state = self._jul9_state()
        broker = self._broker_missing_long()
        sync_open_order(state, broker, force=True)
        sync_open_order(state, broker, force=True)
        state_mod.save_state(path, state)
        mock_iter.return_value = [path]
        calls_after_resolve = broker.get_order_status.call_count

        for _ in range(20):
            sync_pending_fills(broker, force=False)
            time.sleep(0.01)

        self.assertEqual(broker.get_order_status.call_count, calls_after_resolve)
        self.assertFalse(needs_open_order_sync(state_mod.load_state(path)))

    def test_confirm_survives_restart_at_most_once(self):
        path = os.path.join(self.trades_dir, 'confirm_restart.json')
        state = self._jul9_state()
        broker = self._broker_missing_long()

        sync_open_order(state, broker, force=True)
        state_mod.save_state(path, state)
        self.assertEqual(state['open_order']['fill_sync']['phase'], 'confirm_pending')
        self.assertFalse(state['open_order']['fill_sync']['confirm_attempted'])

        reloaded = state_mod.load_state(path)
        sync_open_order(reloaded, broker, force=True)
        state_mod.save_state(path, reloaded)
        self.assertEqual(broker.get_order_status.call_count, 2)
        self.assertTrue(reloaded['open_order']['fill_sync']['confirm_attempted'])
        self.assertEqual(reloaded['open_order']['fill_sync']['phase'], 'resolved_estimated')

        again = state_mod.load_state(path)
        sync_open_order(again, broker, force=True)
        self.assertEqual(broker.get_order_status.call_count, 2)

    def test_jul9_rest_call_budget(self):
        """Simulated Jul 9 missing-leg: fast + confirm only (≤2 opening-order polls)."""
        state = self._jul9_state()
        broker = self._broker_missing_long()
        with patch('blocks.stop.pending_fill_sync.state_mod.iter_active_trade_paths', return_value=[]):
            sync_open_order(state, broker, force=True)
            sync_open_order(state, broker, force=True)
            for _ in range(50):
                sync_open_order(state, broker, force=False)
                sync_pending_fills(broker, force=False)
        self.assertLessEqual(broker.get_order_status.call_count, 2)


class TestProtectiveEstimateStopPlacement(unittest.TestCase):
    def test_stop_placed_after_protective_estimate(self):
        broker = MagicMock()
        broker.place_stop_order.return_value = OrderResult(True, '481561791', 'working')

        with same_day_trade_env() as expiry:
            short_sym = spxw_option_symbol(7530, 'P', expiry_yymmdd=expiry)
            long_sym = spxw_option_symbol(7485, 'P', expiry_yymmdd=expiry)
            st = state_mod.create_pending_state(
                strategy='MEIC_IC',
                lot='01-15_P',
                side='P',
                short_symbol=short_sym,
                long_symbol=long_sym,
                short_strike=7530,
                long_strike=7485,
                target_quantity=1,
                open_order_id='482348463',
                limit_credit=0.75,
            )

            missing = OrderResult(
                True,
                '482348463',
                'filled',
                filled_quantity=1,
                order_quantity=1,
                filled_price=0.75,
                filled_price_source='order_limit_fallback',
                order_limit_price=0.75,
                short_fill_price=1.15,
                long_fill_price=None,
            )
            broker.get_order_status.side_effect = [missing, missing]
            sync_open_order(st, broker, force=True)
            sync_open_order(st, broker, force=True)
            self.assertEqual(st['status'], 'open')

            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'trade.json')
                state_mod.save_state(path, st)
                prices = MagicMock(spec=MqttPriceCache)
                prices.get_spx.return_value = 7520.0
                prices.kill_switch = False
                with patch('common.streamer_symbols.register_spread_symbols'):
                    mon = StopMonitor(path, broker, prices)
                mon._ensure_stop_for_filled_qty()

            broker.place_stop_order.assert_called_once()


class TestOwnershipPassiveReads(unittest.TestCase):
    def test_read_active_trades_makes_no_broker_calls(self):
        with patch('blocks.stop.state.iter_active_trade_paths', return_value=[]), \
             patch('common.broker_factory.get_shared_broker') as mock_broker, \
             patch('dashboard.server.read_json_safe', return_value=None):
            _read_active_trades()
            mock_broker.assert_not_called()

    def test_manual_dashboard_build_makes_no_broker_calls(self):
        with patch('manual_spread.entry.load_dashboard_manual_trades', return_value=[]), \
             patch('common.broker_factory.get_shared_broker') as mock_broker:
            build_manual_trades(
                live_price_fn=lambda _s: None,
                phase_display_fn=lambda *a, **k: '',
                trade_pnl_fn=lambda *a, **k: (0.0, None, None, False),
                stop_label_fn=lambda *a, **k: '',
                slot_state_fn=lambda *a, **k: 'closed',
            )
            mock_broker.assert_not_called()

    def test_launcher_has_no_fill_sync_loop(self):
        run_path = os.path.join(
            os.path.dirname(__file__), '..', 'run.py',
        )
        with open(run_path, encoding='utf-8') as fh:
            source = fh.read()
        self.assertNotIn('sync_pending_fills', source)


class TestFillProvenance(unittest.TestCase):
    def test_limit_fallback_does_not_set_fill_credit(self):
        from blocks.stop.fill_sync import apply_order_result_to_state

        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='t',
            side='P',
            short_symbol='.SPXW260709P7530',
            long_symbol='.SPXW260709P7485',
            short_strike=7530,
            long_strike=7485,
            target_quantity=1,
            open_order_id='1',
            limit_credit=0.75,
        )
        result = OrderResult(
            True,
            '1',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            filled_price=0.75,
            filled_price_source='order_limit_fallback',
            order_limit_price=0.75,
            short_fill_price=1.15,
        )
        apply_order_result_to_state(st, result)
        self.assertEqual(st['short_leg']['fill_price'], 1.15)
        self.assertEqual(st['long_leg']['fill_price'], 0.0)
        self.assertNotIn('fill_credit', st['entry'])

    def test_exact_resolution_no_audit(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True,
            '1',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            filled_price=0.50,
            filled_price_source='broker_leg_math',
            short_fill_price=0.77,
            long_fill_price=0.27,
        )
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='exact',
            side='P',
            short_symbol='.SPXW260710P7500',
            long_symbol='.SPXW260710P7475',
            short_strike=7500,
            long_strike=7475,
            target_quantity=1,
            open_order_id='1',
            limit_credit=0.50,
        )
        sync_open_order(st, broker, force=True)
        fs = st['open_order']['fill_sync']
        self.assertEqual(fs['phase'], 'resolved_exact')
        self.assertIsNone(fs.get('audit_due_epoch'))
        self.assertEqual(broker.get_order_status.call_count, 1)

    def test_estimated_resolution_at_most_one_audit(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True,
            '482348463',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            filled_price=0.75,
            filled_price_source='order_limit_fallback',
            order_limit_price=0.75,
            short_fill_price=1.15,
            long_fill_price=None,
        )
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='audit',
            side='P',
            short_symbol='.SPXW260710P7530',
            long_symbol='.SPXW260710P7485',
            short_strike=7530,
            long_strike=7485,
            target_quantity=1,
            open_order_id='482348463',
            limit_credit=0.75,
        )
        sync_open_order(st, broker, force=True)
        sync_open_order(st, broker, force=True)
        fs = st['open_order']['fill_sync']
        self.assertEqual(fs['phase'], 'resolved_estimated')
        fs['audit_due_epoch'] = time.time() - 1

        maybe_run_fill_audit(st, broker)
        first_audit_calls = broker.get_order_status.call_count
        maybe_run_fill_audit(st, broker)
        self.assertEqual(broker.get_order_status.call_count, first_audit_calls)
        self.assertTrue(fs['audit_attempted'])
        self.assertEqual(fs['phase'], 'audit_complete')

    def test_resolved_cycles_no_duplicate_fill_history(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True,
            '482348463',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            filled_price=0.75,
            filled_price_source='order_limit_fallback',
            order_limit_price=0.75,
            short_fill_price=1.15,
            long_fill_price=None,
        )
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='hist',
            side='P',
            short_symbol='.SPXW260710P7530',
            long_symbol='.SPXW260710P7485',
            short_strike=7530,
            long_strike=7485,
            target_quantity=1,
            open_order_id='482348463',
            limit_credit=0.75,
        )
        sync_open_order(st, broker, force=True)
        sync_open_order(st, broker, force=True)
        count = len(st.get('fill_history') or [])
        self.assertEqual(count, 1)

        for _ in range(10):
            sync_open_order(st, broker, force=False)
        self.assertEqual(len(st.get('fill_history') or []), count)

    def test_audit_correction_does_not_duplicate_active_stop(self):
        broker = MagicMock()
        audit_result = OrderResult(
            True,
            '482348463',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            short_fill_price=1.15,
            long_fill_price=0.40,
            filled_price_source='broker_leg_math',
            filled_price=0.75,
        )
        broker.place_stop_order.return_value = OrderResult(True, 'stop-1', 'working')

        with same_day_trade_env() as expiry:
            short_sym = spxw_option_symbol(7530, 'P', expiry_yymmdd=expiry)
            long_sym = spxw_option_symbol(7485, 'P', expiry_yymmdd=expiry)
            st = state_mod.create_pending_state(
                strategy='MEIC_IC',
                lot='corr',
                side='P',
                short_symbol=short_sym,
                long_symbol=long_sym,
                short_strike=7530,
                long_strike=7485,
                target_quantity=1,
                open_order_id='482348463',
                limit_credit=0.75,
            )
            miss = OrderResult(
                True,
                '482348463',
                'filled',
                filled_quantity=1,
                order_quantity=1,
                filled_price=0.75,
                filled_price_source='order_limit_fallback',
                order_limit_price=0.75,
                short_fill_price=1.15,
                long_fill_price=None,
            )
            broker.get_order_status.side_effect = [miss, miss, audit_result]
            sync_open_order(st, broker, force=True)
            sync_open_order(st, broker, force=True)
            self.assertEqual(st['status'], 'open')

            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'trade.json')
                state_mod.save_state(path, st)
                prices = MagicMock(spec=MqttPriceCache)
                prices.get_spx.return_value = 7520.0
                prices.kill_switch = False
                with patch('common.streamer_symbols.register_spread_symbols'):
                    mon = StopMonitor(path, broker, prices)
                mon._ensure_stop_for_filled_qty()
                self.assertEqual(broker.place_stop_order.call_count, 1)

                st = mon.state
                fs = st['open_order']['fill_sync']
                fs['audit_due_epoch'] = time.time() - 1
                maybe_run_fill_audit(st, broker)
                mon.state = st
                mon._ensure_stop_for_filled_qty()
                self.assertEqual(broker.place_stop_order.call_count, 1)


if __name__ == '__main__':
    unittest.main()
