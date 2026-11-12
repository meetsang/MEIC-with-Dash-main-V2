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

    def test_api_perma_bull_endpoints_params_and_times(self):
        from dashboard.server import app
        client = app.test_client()

        # 1. Test GET /api/perma_bull/today returns entry/exit times without slow history scan
        res = client.get('/api/perma_bull/today')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn('trade', data)
        if data.get('trade'):
            self.assertIn('entry_time', data['trade'])
            self.assertIn('exit_time', data['trade'])
            self.assertIn('profit_target_debit', data['trade'])
            self.assertIn('quantity', data['trade'])

        # 2. Test POST /api/perma_bull/params updates all 4 parameters:
        # quantity, spread_width, target_credit, profit_target_debit
        res_params = client.post('/api/perma_bull/params', json={
            'quantity': 3,
            'spread_width': 10,
            'target_credit': 1.25,
            'profit_target_debit': 0.25,
        })
        self.assertEqual(res_params.status_code, 200)
        pdata = res_params.get_json()
        self.assertEqual(pdata['status'], 'ok')
        self.assertEqual(pdata['updates']['quantity'], 3)
        self.assertEqual(pdata['updates']['spread_width'], 10)
        self.assertEqual(pdata['updates']['target_credit'], 1.25)
        self.assertEqual(pdata['updates']['profit_target_debit'], 0.25)

        # 3. Test history is accessible through main history endpoint /api/history
        res_hist = client.get('/api/history?strategy=PERMA_BULL_PCS')
        self.assertEqual(res_hist.status_code, 200)
        hist_trades = res_hist.get_json()
        self.assertIsInstance(hist_trades, list)

        # Restore default values
        client.post('/api/perma_bull/params', json={
            'quantity': 1,
            'spread_width': 5,
            'target_credit': 1.0,
            'profit_target_debit': 0.15,
        })

    def test_db_upsert_stores_and_reads_time_closed(self):
        from dashboard.db import init_db, upsert_trade, get_trades
        init_db()
        test_trade = {
            'date_opened': '2026-11-04',
            'time_opened': '2026-11-04T08:45:06-06:00',
            'time_closed': '2026-11-04T12:02:06-06:00',
            'strategy': 'PERMA_BULL_PCS',
            'lot': 'perma-test-2026-11-04',
            'side': 'P',
            'quantity': 1,
            'filled_price': 1.0,
            'short_close_price': 1.15,
            'long_close_price': 1.0,
            'close_debit': 0.15,
            'pnl': 85.0,
            'status': 'CLOSED',
            'paper': 1,
        }
        upsert_trade(test_trade)
        trades = get_trades(date='2026-11-04', strategy='PERMA_BULL_PCS', include_paper=True)
        matching = [t for t in trades if t.get('lot') == 'perma-test-2026-11-04']
        self.assertTrue(len(matching) >= 1)
        self.assertEqual(matching[0].get('time_closed'), '2026-11-04T12:02:06-06:00')


    def test_perma_bull_places_oco_short_stop(self):
        """Perma Bull PCS uses OCO short stop order with 0.15 profit target."""
        state = {
            'strategy': 'PERMA_BULL_PCS',
            'stop_profile': 'perma_bull_pcs',
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'short_leg': {'symbol': '.SPXW261110P7485', 'strike': 7485, 'fill_price': 13.92},
            'long_leg': {'symbol': '.SPXW261110P7480', 'strike': 7480, 'fill_price': 12.67},
            'exit_config': {'profit_target_debit': 0.15},
            'phases': {},
            'entry': {'net_credit': 1.25},
        }
        broker = MagicMock()
        broker.get_single_leg_position_qty.return_value = -1
        broker.place_oco_order.return_value = OrderResult(True, 'oco_999', 'working')
        prices = MagicMock()
        prices.get_spx.return_value = 5900.0
        prices.get_quote.return_value = None

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            f.write('{}')
            tpath = f.name
        try:
            mon = StopMonitor(tpath, broker, prices, phases=None)
            mon.state = state
            mon.stop_profile = perma_bull_pcs_profile()
            mon.setup_initial_stop()

            # Verify place_oco_order was called instead of place_stop_order!
            broker.place_oco_order.assert_called_once()
            call_args = broker.place_oco_order.call_args
            self.assertEqual(call_args[0][0], '.SPXW261110P7485')
            self.assertEqual(call_args[0][1], 1)
            self.assertEqual(call_args[1]['profit_price'], 0.15)
            self.assertEqual(call_args[1]['stop_price'], 27.6)
            self.assertEqual(mon.state.get('active_stop', {}).get('type'), 'OCO')
            self.assertEqual(mon.state.get('active_stop', {}).get('order_id'), 'oco_999')
        finally:
            try:
                os.unlink(tpath)
            except OSError:
                pass

    def test_manage_manually_disarms_stop_and_suppresses_auto_exits(self):
        """Manage Manually command cancels resting stop/OCO and freezes automated exits."""
        state = {
            'strategy': 'PERMA_BULL_PCS',
            'stop_profile': 'perma_bull_pcs',
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'short_leg': {'symbol': '.SPXW261110P7485', 'strike': 7485, 'fill_price': 13.92},
            'long_leg': {'symbol': '.SPXW261110P7480', 'strike': 7480, 'fill_price': 12.67},
            'active_stop': {'order_id': '524331437', 'type': 'STOP_LIMIT', 'stop_price': 27.6, 'status': 'working'},
            'exit_config': {'profit_target_debit': 0.15},
            'phases': {},
            'entry': {'net_credit': 1.25},
        }
        broker = MagicMock()
        broker.cancel_order.return_value = True
        prices = MagicMock()
        # Mark reaches 0.10 debit (below 0.15 PT)
        prices.get_option_mid.side_effect = lambda sym: 0.20 if sym == '.SPXW261110P7485' else 0.10

        prices.get_spx.return_value = 5900.0
        prices.get_quote.return_value = None
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            trades_dir = os.path.join(tmp_dir.name, 'trades')
            active_dir = os.path.join(trades_dir, 'active')
            cmd_dir = os.path.join(trades_dir, 'commands')
            os.makedirs(active_dir, exist_ok=True)
            os.makedirs(cmd_dir, exist_ok=True)

            trade_file = os.path.join(active_dir, 'perma_test.json')
            import json
            with open(trade_file, 'w', encoding='utf-8') as f:
                json.dump(state, f)

            cmd_file = os.path.join(cmd_dir, 'perma_test.json.manage_manually.json')
            with open(cmd_file, 'w', encoding='utf-8') as f:
                json.dump({'action': 'manage_manually', 'enabled': True}, f)

            mon = StopMonitor(trade_file, broker, prices, phases=None)
            mon.stop_profile = perma_bull_pcs_profile()
            mon.phases = sorted(mon.stop_profile.phases, key=lambda p: p.priority)

            # Check dashboard commands picks up manage_manually
            handled = mon._check_dashboard_commands()
            self.assertTrue(handled)
            broker.cancel_order.assert_called_once_with('524331437')
            self.assertIsNone(mon.state.get('active_stop'))
            self.assertTrue(mon.state.get('manual_management'))
            self.assertFalse(os.path.exists(cmd_file))

            # Subsequent poll should NEVER place stop or auto-close on PT
            mon.replace_with_spread_close = MagicMock()
            mon._poll_once()
            self.assertIsNone(mon.state.get('active_stop'))
            mon.replace_with_spread_close.assert_not_called()
        finally:
            tmp_dir.cleanup()


    def test_api_perma_bull_manage_manually_endpoint(self):
        """Verify POST /api/perma_bull/manage_manually creates command and toggles manual management."""
        from dashboard.server import app
        client = app.test_client()

        # Call endpoint (if trade exists today, it disarms; if no trade, 404 is valid response)
        res = client.post('/api/perma_bull/manage_manually', json={'enabled': True})
        self.assertIn(res.status_code, (200, 404))
        if res.status_code == 200:
            data = res.get_json()
            self.assertEqual(data.get('status'), 'ok')
            self.assertTrue(data.get('manual_management'))


if __name__ == '__main__':
    unittest.main()
