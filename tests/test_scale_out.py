"""Unit tests for scale-out / partial position close."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from common.scale_out import scale_out_spread
from dashboard.server import _trade_pnl
from blocks.stop.fill_sync import stop_is_current, stop_qty_for_state


class MockBroker:
    def __init__(self):
        self.cancelled_orders = []
        self.placed_spread_close = []
        self.placed_spread_oco = []
        self.placed_stops = []
        self.placed_oco = []
        self.spread_close_fail = False
        self.spread_oco_fail = False

    def cancel_order(self, order_id: str):
        self.cancelled_orders.append(str(order_id))
        return OrderResult(success=True, order_id=order_id, status='cancelled')

    def place_spread_oco_order(
        self,
        short_sym: str,
        long_sym: str,
        qty: int,
        *,
        profit_debit: float,
        stop_trigger_debit: float,
        stop_limit_debit: float,
    ):
        if self.spread_close_fail or self.spread_oco_fail:
            return OrderResult(success=False, order_id=None, status='rejected', message='Spread close rejected')
        oid = f'oco-spread-{len(self.placed_spread_oco) + 1}'
        self.placed_spread_oco.append({
            'order_id': oid,
            'short_sym': short_sym,
            'long_sym': long_sym,
            'qty': qty,
            'profit_debit': profit_debit,
            'stop_trigger_debit': stop_trigger_debit,
            'stop_limit_debit': stop_limit_debit,
        })
        return OrderResult(
            success=True,
            order_id=oid,
            status='working',
            filled_price=None,
        )

    def place_spread_close_order(self, short_sym: str, long_sym: str, qty: int, debit: float):
        if self.spread_close_fail:
            return OrderResult(success=False, order_id=None, status='rejected', message='Spread close rejected')
        self.placed_spread_close.append({
            'short_sym': short_sym,
            'long_sym': long_sym,
            'qty': qty,
            'debit': debit,
        })
        return OrderResult(
            success=True,
            order_id='close-ord-123',
            status='filled',
            filled_price=debit,
            short_fill_price=debit + 0.10,
            long_fill_price=0.10,
        )

    def place_stop_order(self, symbol: str, qty: int, stop_price: float, limit_price: float):
        oid = f'stop-ord-{len(self.placed_stops) + 1}'
        self.placed_stops.append({
            'order_id': oid,
            'symbol': symbol,
            'qty': qty,
            'stop_price': stop_price,
            'limit_price': limit_price,
        })
        return OrderResult(success=True, order_id=oid, status='working')

    def place_oco_order(self, symbol: str, qty: int, profit_price: float, stop_price: float, limit_price: float):
        oid = f'oco-ord-{len(self.placed_oco) + 1}'
        self.placed_oco.append({
            'order_id': oid,
            'symbol': symbol,
            'qty': qty,
            'profit_price': profit_price,
            'stop_price': stop_price,
            'limit_price': limit_price,
        })
        return OrderResult(success=True, order_id=oid, status='working')

    def get_order_status(self, order_id: str, **kwargs):
        return OrderResult(success=True, order_id=order_id, status='filled')


class TestScaleOut(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.active_dir = os.path.join(self.tmp_dir, 'trades', 'active', 'MANUAL_SPREAD')
        os.makedirs(self.active_dir, exist_ok=True)

        self.trade_state = {
            'lot': 'ms-999',
            'status': 'open',
            'quantity': 3,
            'filled_quantity': 3,
            'entry': {
                'net_credit': 2.00,
                'limit_credit': 2.00,
            },
            'short_leg': {
                'symbol': 'SPXW261218P07600000',
                'strike': 7600,
                'fill_price': 2.50,
            },
            'long_leg': {
                'symbol': 'SPXW261218P07580000',
                'strike': 7580,
                'fill_price': 0.50,
            },
            'active_stop': {
                'order_id': '510999000',
                'type': 'STOP_LIMIT',
                'stop_price': 3.50,
                'limit_price': 3.70,
                'quantity': 3,
                'status': 'working',
                'phase': 1,
            },
            'stop_quantity': 3,
            'designated_stop_price': 3.50,
        }
        self.trade_file = 'ms-999_P_20261218T100000.json'
        self.trade_path = os.path.join(self.active_dir, self.trade_file)
        with open(self.trade_path, 'w', encoding='utf-8') as f:
            json.dump(self.trade_state, f)

        self.broker = MockBroker()
        self.prices = {
            'SPXW261218P07600000': 1.60,
            'SPXW261218P07580000': 0.20,
            'spx': 7610.0,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_validation_holding_one(self):
        # Trade with only 1 contract cannot scale out
        with open(self.trade_path, 'w', encoding='utf-8') as f:
            st = dict(self.trade_state)
            st['filled_quantity'] = 1
            st['quantity'] = 1
            json.dump(st, f)

        res = scale_out_spread(self.broker, self.prices, self.trade_file, close_qty=1, root=self.tmp_dir)
        self.assertEqual(res['status'], 'error')
        self.assertIn('Cannot scale out: current holding is 1', res['error'])

    def test_validation_quantity_bounds(self):
        # close_qty == holding (3) must fail (user should use full close)
        res1 = scale_out_spread(self.broker, self.prices, self.trade_file, close_qty=3, root=self.tmp_dir)
        self.assertEqual(res1['status'], 'error')
        self.assertIn('must be between 1 and 2', res1['error'])

        # close_qty <= 0 must fail
        res2 = scale_out_spread(self.broker, self.prices, self.trade_file, close_qty=0, root=self.tmp_dir)
        self.assertEqual(res2['status'], 'error')

    def test_successful_scale_out(self):
        # Scale out 1 of 3 contracts at debit limit 1.40
        res = scale_out_spread(
            self.broker,
            self.prices,
            self.trade_file,
            close_qty=1,
            debit_limit=1.40,
            root=self.tmp_dir,
        )
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['closed_quantity'], 1)
        self.assertEqual(res['remaining_quantity'], 2)
        # Entry net credit was 2.00, close debit was 1.40 -> PnL = (2.00 - 1.40) * 100 * 1 = +$60.00
        self.assertEqual(res['pnl'], 60.00)

        # Check broker interactions:
        # 1. Old stop cancelled
        self.assertIn('510999000', self.broker.cancelled_orders)
        # 2. Spread OCO order placed for 1 contract
        self.assertEqual(len(self.broker.placed_spread_oco), 1)
        self.assertEqual(self.broker.placed_spread_oco[0]['qty'], 1)
        # 3. New stop placed for remaining 2 contracts
        self.assertEqual(len(self.broker.placed_stops), 1)
        self.assertEqual(self.broker.placed_stops[0]['qty'], 2)
        self.assertEqual(self.broker.placed_stops[0]['stop_price'], 3.50)

        # Check state saved on disk
        with open(self.trade_path, 'r', encoding='utf-8') as f:
            updated = json.load(f)

        self.assertEqual(updated['filled_quantity'], 2)
        self.assertEqual(updated['quantity'], 2)
        self.assertEqual(updated['stop_quantity'], 2)
        self.assertEqual(updated['initial_quantity'], 3)
        self.assertEqual(updated['active_stop']['quantity'], 2)
        self.assertEqual(len(updated['scale_outs']), 1)
        self.assertEqual(updated['scale_outs'][0]['quantity'], 1)
        self.assertEqual(updated['scale_outs'][0]['pnl'], 60.00)

        # Stop monitor functions agree that remaining position is 2 and current stop matches
        self.assertEqual(stop_qty_for_state(updated), 2)
        self.assertTrue(stop_is_current(updated))

    def test_pnl_with_scaled_portions(self):
        # Initial 3 contracts entered at 2.00 net credit
        # Scale out 1 @ 1.40 -> realized +$60.00
        # 2 contracts remaining. Live spread mid is (1.60 - 0.20) = 1.40
        trade = dict(self.trade_state)
        trade['filled_quantity'] = 2
        trade['quantity'] = 2
        trade['scale_outs'] = [{'quantity': 1, 'pnl': 60.00}]

        # Live PnL: (2.00 - 1.40) * 100 * 2 = $120.00 + $60.00 (scaled) = $180.00
        pnl, _, _, _ = _trade_pnl(
            net_credit=2.00,
            quantity=2,
            short_fill=2.50,
            long_fill=0.50,
            short_close=None,
            long_close=None,
            cur_short=1.60,
            cur_long=0.20,
            status='open',
            trade=trade,
        )
        self.assertEqual(pnl, 180.00)

        # When remaining 2 contracts fully close at 1.00 debit:
        # Exit debit = 1.00. Remaining PnL = (2.00 - 1.00) * 100 * 2 = $200.00
        # Total PnL = $200.00 + $60.00 = $260.00
        pnl_closed, exit_cr, _, frozen = _trade_pnl(
            net_credit=2.00,
            quantity=2,
            short_fill=2.50,
            long_fill=0.50,
            short_close=1.20,
            long_close=0.20,
            cur_short=1.20,
            cur_long=0.20,
            status='closed',
            trade=trade,
        )
        self.assertTrue(frozen)
        self.assertEqual(exit_cr, 1.00)
        self.assertEqual(pnl_closed, 260.00)

    def test_spread_close_failure_restores_stop(self):
        # If broker rejects the spread close, old stop for original 3 contracts must be restored
        self.broker.spread_close_fail = True
        res = scale_out_spread(
            self.broker,
            self.prices,
            self.trade_file,
            close_qty=1,
            root=self.tmp_dir,
        )
        self.assertEqual(res['status'], 'error')
        self.assertIn('Spread close rejected', res['error'])

        # Check that stop was restored with full 3 contracts
        self.assertEqual(len(self.broker.placed_stops), 1)
        self.assertEqual(self.broker.placed_stops[0]['qty'], 3)

    def test_reconcile_does_not_clobber_new_stop_when_old_stop_cancelled(self):
        """When an old stop is cancelled at broker but disk already adopted a new stop, reconcile must NOT clear it."""
        from blocks.stop.monitor import StopMonitor

        monitor = StopMonitor(self.trade_path, self.broker, MagicMock())
        # In monitor memory, active_stop is the OLD stop
        monitor.state['active_stop'] = {
            'order_id': 'old-stop-111',
            'status': 'working',
            'quantity': 3,
        }

        # On disk, scale-out has already updated the trade file to NEW stop 222
        with open(self.trade_path, 'r', encoding='utf-8') as f:
            disk_data = json.load(f)
        disk_data['active_stop'] = {
            'order_id': 'new-stop-222',
            'status': 'working',
            'quantity': 2,
        }
        disk_data['quantity'] = 2
        disk_data['filled_quantity'] = 2
        with open(self.trade_path, 'w', encoding='utf-8') as f:
            json.dump(disk_data, f)

        # Broker returns status for old-stop-111: cancelled
        self.broker.get_order_status = MagicMock(return_value=OrderResult(
            success=True, order_id='old-stop-111', status='cancelled',
        ))

        monitor._reconcile_active_stop_with_broker()

        # Monitor state and disk must retain the new stop 222!
        self.assertIsNotNone(monitor.state.get('active_stop'))
        self.assertEqual(monitor.state['active_stop']['order_id'], 'new-stop-222')
        with open(self.trade_path, 'r', encoding='utf-8') as f:
            rechecked = json.load(f)
        self.assertEqual(rechecked['active_stop']['order_id'], 'new-stop-222')
        self.assertEqual(rechecked['quantity'], 2)

    def test_place_short_stop_adopts_existing_working_closing_order(self):
        """When broker rejects stop placement because a closing order is already working, adopt it."""
        from blocks.stop.monitor import StopMonitor

        prices_mock = MagicMock()
        prices_mock.get_spx.return_value = 7610.0
        monitor = StopMonitor(self.trade_path, self.broker, prices_mock)
        # Broker fails place_stop_order / place_oco_order with 'already have a closing order'
        rej_res = OrderResult(
            success=False,
            order_id=None,
            status='rejected',
            message='cannot_close_against_more_than_existing_position: You already have a closing order for SPXW 261218P07600000',
        )
        self.broker.place_stop_order = MagicMock(return_value=rej_res)
        self.broker.place_oco_order = MagicMock(return_value=rej_res)

        # Broker has find_working_close_order which returns the live order
        working_order_result = OrderResult(
            success=True,
            order_id='511076986',
            status='working',
            order_limit_price=3.70,
            order_quantity=2,
        )
        self.broker.find_working_close_order = MagicMock(return_value=working_order_result)

        with patch('blocks.stop.monitor.broker_actions_allowed_for_trade', return_value=(True, 'allowed')):
            placed = monitor._place_short_stop(3.50, 3.70, phase=1, reason='test_rearm', qty=2)
            self.assertTrue(placed)
            self.assertEqual(monitor.state['active_stop']['order_id'], '511076986')
            self.assertEqual(monitor.state['active_stop']['quantity'], 2)
            self.assertEqual(monitor.state['stop_quantity'], 2)

    def test_check_scale_out_command_execution(self):
        """Test end-to-end command file scale-out execution in StopMonitor."""
        from blocks.stop.monitor import StopMonitor

        commands_dir = os.path.join(self.tmp_dir, 'trades', 'commands')
        os.makedirs(commands_dir, exist_ok=True)
        cmd_path = os.path.join(commands_dir, f'{self.trade_file}.scale_out.json')
        done_path = os.path.join(commands_dir, f'{self.trade_file}.scale_out.done.json')

        with open(cmd_path, 'w', encoding='utf-8') as f:
            json.dump({
                'action': 'scale_out',
                'close_qty': 1,
                'target_debit': 1.40,
            }, f)

        prices_mock = MagicMock()
        prices_mock.get_spx.return_value = 7610.0
        monitor = StopMonitor(self.trade_path, self.broker, prices_mock)
        took_action = monitor._check_scale_out_command()
        self.assertTrue(took_action)

        # Command file should be unlinked
        self.assertFalse(os.path.exists(cmd_path))
        # Done file should exist with status ok
        self.assertTrue(os.path.exists(done_path))
        with open(done_path, 'r', encoding='utf-8') as f:
            done_data = json.load(f)
        self.assertEqual(done_data['status'], 'ok')
        self.assertEqual(done_data['closed_quantity'], 1)
        self.assertEqual(done_data['remaining_quantity'], 2)

        # Monitor state and disk updated
        self.assertEqual(monitor.state['quantity'], 2)
        self.assertEqual(monitor.state['filled_quantity'], 2)
        self.assertEqual(monitor.state['active_stop']['quantity'], 2)

    def test_scale_out_places_spread_oco_with_4x_stop(self):
        """Test scale-out places spread OCO (target debit + 4x stop multiple) protecting scaled-out portion."""
        from blocks.stop.monitor import StopMonitor

        state = dict(self.trade_state)
        state['entry'] = {'net_credit': 0.80, 'limit_credit': 0.80}
        state['stop_multiplier'] = 4.0
        state['quantity'] = 3
        state['filled_quantity'] = 3
        state['active_stop'] = {
            'order_id': '510999000',
            'type': 'STOP_LIMIT',
            'stop_price': 3.50,
            'limit_price': 3.70,
            'phase': 1,
            'quantity': 3,
        }
        with open(self.trade_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

        prices_mock = MagicMock()
        prices_mock.get_spx.return_value = 7610.0
        monitor = StopMonitor(self.trade_path, self.broker, prices_mock)

        from common.scale_out import execute_scale_out_in_monitor
        res = execute_scale_out_in_monitor(monitor, close_qty=1, target_debit=0.35)

        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['order_type'], 'SPREAD_OCO')
        self.assertEqual(res['stop_trigger_debit'], 3.20)

        # Verify old 3-contract stop cancelled
        self.assertIn('510999000', self.broker.cancelled_orders)

        # Verify spread OCO order placed for 1 contract with 35c profit and 3.20 stop (4x of 0.80)
        self.assertEqual(len(self.broker.placed_spread_oco), 1)
        oco_call = self.broker.placed_spread_oco[0]
        self.assertEqual(oco_call['qty'], 1)
        self.assertEqual(oco_call['profit_debit'], 0.35)
        self.assertEqual(oco_call['stop_trigger_debit'], 3.20)
        self.assertEqual(oco_call['stop_limit_debit'], 3.30)

        # Verify replacement stop placed for remaining 2 contracts
        self.assertEqual(len(self.broker.placed_stops), 1)
        rem_stop = self.broker.placed_stops[0]
        self.assertEqual(rem_stop['qty'], 2)
        self.assertEqual(rem_stop['stop_price'], 3.50)

        # Verify scale_record in state
        self.assertEqual(len(monitor.state['scale_outs']), 1)
        rec = monitor.state['scale_outs'][0]
        self.assertEqual(rec['quantity'], 1)
        self.assertEqual(rec['order_type'], 'SPREAD_OCO')
        self.assertEqual(rec['debit_limit'], 0.35)
        self.assertEqual(rec['stop_trigger_debit'], 3.20)
        self.assertEqual(rec['stop_limit_debit'], 3.30)
        self.assertEqual(rec['status'], 'working')

        # Verify stop history recorded OCO placement
        actions = [h['action'] for h in monitor.state.get('stop_history', [])]
        self.assertIn('placed_scale_out_oco', actions)

    def test_monitor_sync_working_scale_out_orders(self):
        """Test monitor periodically checks and syncs filled scale-out orders."""
        from blocks.stop.monitor import StopMonitor

        state = dict(self.trade_state)
        state['entry'] = {'net_credit': 0.80}
        state['scale_outs'] = [{
            'quantity': 1,
            'order_id': 'oco-spread-1',
            'order_type': 'SPREAD_OCO',
            'debit_limit': 0.35,
            'status': 'working',
            'close_price': 0.35,
            'pnl': 45.0,
        }]
        with open(self.trade_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)

        self.broker.get_order_status = MagicMock(return_value=OrderResult(
            success=True, order_id='oco-spread-1', status='filled', filled_price=0.35,
        ))

        prices_mock = MagicMock()
        monitor = StopMonitor(self.trade_path, self.broker, prices_mock)
        monitor._sync_working_scale_out_orders()

        self.assertEqual(monitor.state['scale_outs'][0]['status'], 'filled')
        self.assertEqual(monitor.state['scale_outs'][0]['close_price'], 0.35)
        self.assertEqual(monitor.state['scale_outs'][0]['pnl'], 45.00)

    def test_scale_out_splits_row_into_two_trades(self):
        """Scale-out must split trade into remaining contracts row and a child OCO row."""
        res = scale_out_spread(
            self.broker,
            self.prices,
            self.trade_file,
            close_qty=1,
            debit_limit=1.20,
            stop_loss=3.40,
            root=self.tmp_dir,
        )
        self.assertEqual(res['status'], 'ok')
        self.assertEqual(res['closed_quantity'], 1)
        self.assertEqual(res['remaining_quantity'], 2)
        self.assertEqual(res['child_lot'], 'ms-999-s1')
        self.assertEqual(res['profit_target'], 1.20)
        self.assertEqual(res['stop_loss'], 3.40)

        # 1. Verify parent file state
        with open(self.trade_path, 'r', encoding='utf-8') as f:
            parent = json.load(f)
        self.assertEqual(parent['quantity'], 2)
        self.assertEqual(parent['filled_quantity'], 2)
        self.assertEqual(parent['holding_quantity'], 2)
        self.assertEqual(parent['stop_quantity'], 2)
        self.assertTrue(stop_is_current(parent))
        self.assertEqual(parent['scale_outs'][0]['child_lot'], 'ms-999-s1')

        # 2. Verify child file exists in active_dir
        child_path = os.path.join(self.active_dir, res['child_filename'])
        self.assertTrue(os.path.exists(child_path))
        with open(child_path, 'r', encoding='utf-8') as f:
            child = json.load(f)

        self.assertEqual(child['lot'], 'ms-999-s1')
        self.assertEqual(child['parent_lot'], 'ms-999')
        self.assertTrue(child['is_scale_out_split'])
        self.assertEqual(child['quantity'], 1)
        self.assertEqual(child['filled_quantity'], 1)
        self.assertEqual(child['holding_quantity'], 1)
        self.assertEqual(child['stop_quantity'], 1)
        self.assertEqual(child['status'], 'open')
        self.assertEqual(child['active_stop']['type'], 'SPREAD_OCO')
        self.assertEqual(child['active_stop']['profit_debit'], 1.20)
        self.assertEqual(child['active_stop']['stop_trigger_debit'], 3.40)
        self.assertEqual(child['active_stop']['quantity'], 1)
        self.assertTrue(stop_is_current(child))

    def test_second_scale_out_increments_child_lot(self):
        """Subsequent scale-outs must create incremented child lots (s1, s2)."""
        res1 = scale_out_spread(
            self.broker,
            self.prices,
            self.trade_file,
            close_qty=1,
            debit_limit=1.20,
            stop_loss=3.40,
            root=self.tmp_dir,
        )
        self.assertEqual(res1['child_lot'], 'ms-999-s1')

        res2 = scale_out_spread(
            self.broker,
            self.prices,
            self.trade_file,
            close_qty=1,
            debit_limit=1.10,
            stop_loss=3.30,
            root=self.tmp_dir,
        )
        self.assertEqual(res2['child_lot'], 'ms-999-s2')

        # Both child files exist
        self.assertTrue(os.path.exists(os.path.join(self.active_dir, res1['child_filename'])))
        self.assertTrue(os.path.exists(os.path.join(self.active_dir, res2['child_filename'])))

    def test_stop_price_label_pt_and_sl(self):
        """Test _stop_price_label formats PT and SL for OCO orders."""
        from dashboard.server import _stop_price_label

        active_oco = {
            'type': 'SPREAD_OCO',
            'profit_debit': 0.50,
            'stop_trigger_debit': 3.50,
        }
        self.assertEqual(_stop_price_label(active_oco), 'PT 0.50 / SL 3.50')

    def test_pnl_with_split_child_row_does_not_double_count(self):
        """When scaled-out child row is tracked separately, parent row PnL only accounts for remaining contracts."""
        trade = dict(self.trade_state)
        trade['filled_quantity'] = 2
        trade['quantity'] = 2
        trade['scale_outs'] = [{
            'quantity': 1,
            'pnl': 60.00,
            'child_lot': 'ms-999-s1',
            'child_filename': 'ms-999-s1_P_20261218T100000.json',
        }]

        # Parent live PnL: (2.00 - 1.40) * 100 * 2 = $120.00 (scaled $60.00 is tracked by child row!)
        pnl, _, _, _ = _trade_pnl(
            net_credit=2.00,
            quantity=2,
            short_fill=2.50,
            long_fill=0.50,
            short_close=None,
            long_close=None,
            cur_short=1.60,
            cur_long=0.20,
            status='open',
            trade=trade,
        )
        self.assertEqual(pnl, 120.00)


if __name__ == '__main__':
    unittest.main()

