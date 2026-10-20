"""Tests for partial fill stop placement and stop resize/replace lifecycle."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from blocks.entry.handoff import apply_stop_snapshot
from blocks.session.plan import SessionRow
from blocks.stop import state as state_mod
from blocks.stop.fill_provenance import is_fill_sync_terminal
from blocks.stop.fill_sync import apply_order_result_to_state, stop_is_current, stop_qty_for_state, sync_open_order
from blocks.stop.pending_fill_sync import needs_open_order_sync
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import merge_policy
from brokers.base import OrderResult


class TestPartialFillStops(unittest.TestCase):
    def test_v3_supervisor_slot_eligible_on_partial_fill(self):
        """Partial fill (e.g. 1 of 2 contracts) must be eligible for StopSupervisor."""
        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-215',
            side='P',
            short_symbol='.SPXW260930P7680',
            long_symbol='.SPXW260930P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.17,
            long_fill=0.42,
            net_credit=0.75,
            quantity=2,
            open_order_id='510663729',
        )
        st['status'] = 'open'
        st['filled_quantity'] = 1  # 1 filled of 2 target

        self.assertTrue(
            StopSupervisor._slot_eligible(st),
            'Partial fill with filled_quantity > 0 must be eligible for StopSupervisor',
        )

        # But if filled_quantity is 0, it should not be eligible
        st['filled_quantity'] = 0
        self.assertFalse(
            StopSupervisor._slot_eligible(st),
            'Trade with 0 filled contracts should not be eligible for StopSupervisor',
        )

    def test_is_fill_sync_terminal_keeps_working_partial_fills_active(self):
        """When an order has resolved prices but is partially filled and working, fill sync is not terminal."""
        st = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-215',
            side='P',
            short_symbol='.SPXW260930P7680',
            long_symbol='.SPXW260930P7655',
            short_strike=7680,
            long_strike=7655,
            target_quantity=2,
            open_order_id='510663729',
            limit_credit=0.75,
        )
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True,
            '510663729',
            'working',
            filled_quantity=1,
            order_quantity=2,
            filled_price=0.75,
            short_fill_price=1.17,
            long_fill_price=0.42,
        )

        sync_open_order(st, broker, force=True)
        self.assertEqual(st['filled_quantity'], 1)
        self.assertEqual(st['quantity'], 2)
        # Leg prices are resolved
        self.assertEqual(st['open_order']['fill_sync']['phase'], 'resolved_exact')
        # BUT order is still working to fill the 2nd contract, so it must NOT be terminal!
        self.assertFalse(is_fill_sync_terminal(st))
        self.assertTrue(needs_open_order_sync(st))

        # Once the remaining contract fills:
        broker.get_order_status.return_value = OrderResult(
            True,
            '510663729',
            'filled',
            filled_quantity=2,
            order_quantity=2,
            filled_price=0.75,
            short_fill_price=1.17,
            long_fill_price=0.42,
        )
        sync_open_order(st, broker, force=True)
        self.assertEqual(st['filled_quantity'], 2)
        self.assertTrue(st['open_order']['fully_filled'])
        self.assertTrue(is_fill_sync_terminal(st))
        self.assertFalse(needs_open_order_sync(st))

    def test_apply_stop_snapshot_does_not_set_fully_filled_on_partial(self):
        """apply_stop_snapshot must only set fully_filled=True if filled >= quantity."""
        st = {
            'filled_quantity': 1,
            'quantity': 2,
            'entry': {'net_credit': 0.5},
            'short_leg': {'fill_price': 1.0},
            'long_leg': {'fill_price': 0.5},
        }
        row = SessionRow(
            slot_key='ms-215_P',
            lot='ms-215',
            side='P',
            entry_window_start='00:00',
            entry_window_end='23:59',
            entry_condition='manual',
            quantity=2,
            stop_multiplier=2.0,
        )
        apply_stop_snapshot(st, row)
        self.assertEqual(st['status'], 'open')
        self.assertFalse(st.get('open_order', {}).get('fully_filled', False))

        # When filled is 2 of 2:
        st['filled_quantity'] = 2
        apply_stop_snapshot(st, row)
        self.assertTrue(st.get('open_order', {}).get('fully_filled', False))

    def test_stop_quantity_and_resize_on_partial_fill(self):
        """Stop quantity must match filled quantity, and detect when resize is needed."""
        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-215',
            side='P',
            short_symbol='.SPXW260930P7680',
            long_symbol='.SPXW260930P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.17,
            long_fill=0.42,
            net_credit=0.75,
            quantity=2,
            open_order_id='510663729',
        )
        st['filled_quantity'] = 1
        self.assertEqual(stop_qty_for_state(st), 1)

        # No stop placed yet
        self.assertFalse(stop_is_current(st))

        # Stop placed for 1 contract
        st['active_stop'] = {'order_id': 'stop_1', 'quantity': 1, 'status': 'working'}
        st['stop_quantity'] = 1
        self.assertTrue(stop_is_current(st))

        # 2nd contract fills: filled_quantity becomes 2
        st['filled_quantity'] = 2
        self.assertEqual(stop_qty_for_state(st), 2)
        # stop_is_current must now be False because stop covers only 1 of 2 filled contracts
        self.assertFalse(stop_is_current(st))

        # After stop is replaced for 2 contracts
        st['active_stop'] = {'order_id': 'stop_2', 'quantity': 2, 'status': 'working'}
        st['stop_quantity'] = 2
        self.assertTrue(stop_is_current(st))

    def test_merge_policy_adopts_filled_quantity_and_open_order(self):
        """merge_policy must adopt filled_quantity and open_order from disk."""
        mem = {'status': 'open', 'filled_quantity': 1, 'quantity': 2, 'open_order': {'status': 'working'}}
        disk = {'status': 'open', 'filled_quantity': 2, 'quantity': 2, 'open_order': {'status': 'filled', 'fully_filled': True}}
        merged = merge_policy(mem, disk)
        self.assertEqual(merged['filled_quantity'], 2)
        self.assertEqual(merged['open_order']['status'], 'filled')
        self.assertTrue(merged['open_order']['fully_filled'])

    def test_stop_qty_for_state_accounts_for_partial_fills(self):
        """stop_qty_for_state must deduce closed quantities when short_partial_fills exists."""
        st = {
            'filled_quantity': 3,
            'quantity': 3,
            'short_partial_fills': [{'qty': 2, 'price': 2.75}],
        }
        self.assertEqual(stop_qty_for_state(st), 1)

        # Explicit short_remaining_qty takes priority
        st['short_remaining_qty'] = 1
        self.assertEqual(stop_qty_for_state(st), 1)

        st['short_remaining_qty'] = 0
        self.assertEqual(stop_qty_for_state(st), 0)

    def test_handle_partial_stop_fill_cancels_and_replaces_with_mid_limit(self):
        """When a stop partially fills, cancel remainder and place BUY_TO_CLOSE limit @ market mid."""
        import tempfile
        import os
        from blocks.stop.monitor import StopMonitor
        from blocks.stop.mqtt_prices import MqttPriceCache

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-453',
            side='P',
            short_symbol='.SPXW261020P7680',
            long_symbol='.SPXW261020P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.45,
            long_fill=0.75,
            net_credit=0.70,
            quantity=3,
            open_order_id='516297448',
        )
        st['filled_quantity'] = 3
        st['status'] = 'open'
        st['active_stop'] = {
            'order_id': '516304265',
            'type': 'STOP_LIMIT',
            'stop_price': 2.70,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'quantity': 3,
        }
        st['stop_quantity'] = 3

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ms-453_P.json')
        state_mod.save_state(path, st)

        broker = MagicMock()
        # Mock cancel confirmation
        broker.cancel_order.return_value = OrderResult(True, '516304265', 'cancelled')
        # Mock status check after cancel showing 2 filled, 1 remaining
        broker.get_order_status.return_value = OrderResult(
            True,
            '516304265',
            'cancelled',
            filled_quantity=2,
            order_quantity=3,
            remaining_quantity=1,
            filled_price=2.75,
            short_fill_price=2.75,
        )
        # Broker position check returns -1 (1 short contract held)
        broker.get_option_position_quantity.return_value = -1
        # Place limit order succeeds
        broker.place_limit_order.return_value = OrderResult(
            True,
            '516320999',
            'working',
            order_quantity=1,
        )

        prices = MagicMock(spec=MqttPriceCache)
        prices.get_market_mid.return_value = 2.85
        prices.get.return_value = 2.85
        prices.get_spx.return_value = 7720.0
        prices.kill_switch = False

        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        mon.json_path = path

        partial_result = OrderResult(
            True,
            '516304265',
            'partial',
            filled_quantity=2,
            order_quantity=3,
            filled_price=2.75,
        )

        mon.handle_partial_stop_fill(st['active_stop'], broker_result=partial_result)

        # 1. Stop cancel was attempted
        broker.cancel_order.assert_called_with('516304265')
        # 2. Limit order placed for 1 contract at 2.85 mid
        broker.place_limit_order.assert_called_with(
            'BUY_TO_CLOSE',
            '.SPXW261020P7680',
            1,
            2.85,
        )
        # 3. State updated properly
        self.assertEqual(mon.state['active_stop']['order_id'], '516320999')
        self.assertEqual(mon.state['active_stop']['quantity'], 1)
        self.assertEqual(mon.state['active_stop']['type'], 'LIMIT')
        self.assertEqual(mon.state['stop_quantity'], 1)
        self.assertEqual(mon.state['short_remaining_qty'], 1)
        self.assertEqual(len(mon.state['short_partial_fills']), 1)
        self.assertEqual(mon.state['short_partial_fills'][0]['qty'], 2)
        self.assertEqual(mon.state['short_partial_fills'][0]['price'], 2.75)

    def test_handle_partial_stop_fill_all_filled_during_cancel(self):
        """If remaining contracts filled before cancel finished, finalize short leg immediately."""
        import tempfile
        import os
        from blocks.stop.monitor import StopMonitor
        from blocks.stop.mqtt_prices import MqttPriceCache

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-453',
            side='P',
            short_symbol='.SPXW261020P7680',
            long_symbol='.SPXW261020P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.45,
            long_fill=0.75,
            net_credit=0.70,
            quantity=3,
            open_order_id='516297448',
        )
        st['filled_quantity'] = 3
        st['status'] = 'open'
        st['active_stop'] = {
            'order_id': '516304265',
            'type': 'STOP_LIMIT',
            'stop_price': 2.70,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'quantity': 3,
        }
        st['stop_quantity'] = 3

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ms-453_P.json')
        state_mod.save_state(path, st)

        broker = MagicMock()
        broker.cancel_order.return_value = OrderResult(False, '516304265', 'filled')
        broker.get_order_status.return_value = OrderResult(
            True,
            '516304265',
            'filled',
            filled_quantity=3,
            order_quantity=3,
            remaining_quantity=0,
            filled_price=2.75,
        )
        broker.get_option_position_quantity.return_value = 0

        prices = MagicMock(spec=MqttPriceCache)
        prices.get_spx.return_value = 7720.0
        prices.kill_switch = False

        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        mon.json_path = path

        partial_result = OrderResult(
            True,
            '516304265',
            'partial',
            filled_quantity=2,
            order_quantity=3,
            filled_price=2.75,
        )

        mon.handle_partial_stop_fill(st['active_stop'], broker_result=partial_result)

        self.assertEqual(mon.state['status'], 'closing')
        self.assertIsNotNone(mon.state.get('short_closed_at'))
        self.assertEqual(mon.state['short_remaining_qty'], 0)
        self.assertEqual(mon.state['short_close_price'], 2.75)

    def test_blended_short_close_price(self):
        """Verify VWAP blended calculation across partial fills and final fill."""
        from blocks.stop.monitor import StopMonitor

        mon = StopMonitor.__new__(StopMonitor)
        mon.state = {
            'short_partial_fills': [
                {'qty': 2, 'price': 2.75},
            ]
        }
        # Final 1 contract fills at 2.80: (2 * 2.75 + 1 * 2.80) / 3 = 8.30 / 3 = 2.7667
        blended = mon._calculate_blended_short_close_price(2.80, final_qty=1)
        self.assertAlmostEqual(blended, 2.7667, places=4)

    def test_replace_with_limit_close_clamps_to_remaining_broker_position(self):
        """replace_with_limit_close must clamp quantity to remaining short position rather than initial entry size."""
        import tempfile
        import os
        from blocks.stop.monitor import StopMonitor
        from blocks.stop.mqtt_prices import MqttPriceCache

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-453',
            side='P',
            short_symbol='.SPXW261020P7680',
            long_symbol='.SPXW261020P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.45,
            long_fill=0.75,
            net_credit=0.70,
            quantity=3,
            open_order_id='516297448',
        )
        st['filled_quantity'] = 3
        st['status'] = 'open'
        st['active_stop'] = {
            'order_id': '516304265',
            'type': 'STOP_LIMIT',
            'stop_price': 2.70,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'quantity': 3,
        }
        st['stop_quantity'] = 3

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ms-453_P.json')
        state_mod.save_state(path, st)

        broker = MagicMock()
        broker.cancel_order.return_value = OrderResult(True, '516304265', 'cancelled')
        # Order status shows 2 filled
        broker.get_order_status.return_value = OrderResult(
            True,
            '516304265',
            'cancelled',
            filled_quantity=2,
            order_quantity=3,
            filled_price=2.75,
        )
        # Broker position quantity confirms only 1 contract held short
        broker.get_option_position_quantity.return_value = -1
        broker.place_limit_order.return_value = OrderResult(
            True,
            '516399999',
            'working',
            order_quantity=1,
        )

        prices = MagicMock(spec=MqttPriceCache)
        prices.get_market_mid.return_value = 2.90
        prices.get.return_value = 2.90
        prices.get_spx.return_value = 7720.0
        prices.kill_switch = False

        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        mon.json_path = path

        mon.replace_with_limit_close(reason='spread_stop_breach')

        # Must place limit order for 1 contract (clamped by broker position), NOT 3 contracts!
        broker.place_limit_order.assert_called_with(
            'BUY_TO_CLOSE',
            '.SPXW261020P7680',
            1,
            2.90,
        )
        self.assertEqual(mon.state['active_stop']['quantity'], 1)
        self.assertEqual(mon.state['stop_quantity'], 1)

    def test_sync_active_close_order_triggers_partial_fill_handling(self):
        """_sync_active_close_order must call handle_partial_stop_fill when partial fill is detected."""
        import tempfile
        import os
        from blocks.stop.monitor import StopMonitor

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-453',
            side='P',
            short_symbol='.SPXW261020P7680',
            long_symbol='.SPXW261020P7655',
            short_strike=7680,
            long_strike=7655,
            short_fill=1.45,
            long_fill=0.75,
            net_credit=0.70,
            quantity=3,
            open_order_id='516297448',
        )
        st['filled_quantity'] = 3
        st['status'] = 'open'
        st['active_stop'] = {
            'order_id': '516304265',
            'type': 'STOP_LIMIT',
            'stop_price': 2.70,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'quantity': 3,
        }
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'ms-453_P.json')
        state_mod.save_state(path, st)

        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True,
            '516304265',
            'working',
            filled_quantity=2,
            order_quantity=3,
            filled_price=2.70,
        )

        prices = MagicMock()
        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        mon.handle_partial_stop_fill = MagicMock()

        mon._sync_active_close_order()

        mon.handle_partial_stop_fill.assert_called_once()
        args, kwargs = mon.handle_partial_stop_fill.call_args
        self.assertEqual(kwargs['broker_result'].filled_quantity, 2)


if __name__ == '__main__':
    unittest.main()

