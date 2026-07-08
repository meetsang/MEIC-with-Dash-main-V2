"""Tests for per-tranche stop placement and repair-only adoption."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.broker_sync import (
    expected_exchange_stop_prices,
    repair_orphan_stop,
)
from blocks.stop.fill_sync import stop_is_current
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.stop_ownership import (
    scan_duplicate_stop_ownership,
    ownership_conflict_paths,
)
from blocks.stop.stop_math import exchange_stop_limit_prices


def _make_open_state(
    *,
    lot: str,
    short_symbol: str = '.SPXW260707P7485',
    long_symbol: str = '.SPXW260707P7460',
    short_strike: int = 7485,
    long_strike: int = 7460,
    short_fill: float = 0.72,
    long_fill: float = 0.27,
    net_credit: float = 0.45,
    quantity: int = 1,
    open_order_id: str = '481576412',
    active_stop: dict | None = None,
    stop_quantity: int | None = None,
) -> dict:
    st = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot=lot,
        side='P',
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        short_strike=short_strike,
        long_strike=long_strike,
        short_fill=short_fill,
        long_fill=long_fill,
        net_credit=net_credit,
        quantity=quantity,
        open_order_id=open_order_id,
    )
    if active_stop is not None:
        st['active_stop'] = active_stop
    if stop_quantity is not None:
        st['stop_quantity'] = stop_quantity
    return st


def _broker_order_raw(stop_trigger: float, limit_price: float, qty: int = 1):
    raw = MagicMock()
    raw.order_type = 'Stop Limit'
    raw.stop_trigger = stop_trigger
    raw.price = -limit_price
    raw.status = 'Live'
    raw.size = qty
    return raw


class TestSharedStopPerTranche(unittest.TestCase):
    def test_two_same_strike_tranches_place_separate_stops(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, 'x', 'filled')
        broker.place_stop_order.side_effect = [
            OrderResult(True, '481561791', 'working'),
            OrderResult(True, '481562001', 'working'),
        ]

        st1 = _make_open_state(lot='12-00', open_order_id='481561770', active_stop=None, stop_quantity=0)
        st2 = _make_open_state(lot='12-30', open_order_id='481576412', active_stop=None, stop_quantity=0)

        with tempfile.TemporaryDirectory() as tmp:
            p1 = os.path.join(tmp, '12-00_P.json')
            p2 = os.path.join(tmp, '12-30_P.json')
            state_mod.save_state(p1, st1)
            state_mod.save_state(p2, st2)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7510.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                m1 = StopMonitor(p1, broker, prices)
                m1._ensure_stop_for_filled_qty()
                m2 = StopMonitor(p2, broker, prices)
                m2._ensure_stop_for_filled_qty()

            self.assertEqual(broker.place_stop_order.call_count, 2)
            self.assertEqual(m1.state['active_stop']['order_id'], '481561791')
            self.assertEqual(m2.state['active_stop']['order_id'], '481562001')
            broker.find_working_close_order.assert_not_called()

    def test_different_long_strike_still_one_stop_per_json(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, 'x', 'filled')
        broker.place_stop_order.return_value = OrderResult(True, '481599999', 'working')

        st = _make_open_state(
            lot='01-45',
            long_symbol='.SPXW260707P7455',
            long_strike=7455,
            active_stop=None,
            stop_quantity=0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '01-45_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7510.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._ensure_stop_for_filled_qty()

            broker.place_stop_order.assert_called_once()
            self.assertEqual(mon.state['active_stop']['order_id'], '481599999')

    def test_slow_reconcile_does_not_adopt_other_tranche_stop(self):
        st = _make_open_state(
            lot='12-30',
            active_stop=None,
            stop_quantity=0,
        )
        exp_stop, exp_limit = expected_exchange_stop_prices(st)

        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(False, '', 'unknown')
        broker.find_working_close_order.return_value = OrderResult(
            True,
            '481561791',
            'live',
            order_quantity=1,
            raw=_broker_order_raw(exp_stop, exp_limit),
        )
        broker.place_stop_order.return_value = OrderResult(True, '481562002', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '12-30_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7510.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._reconcile_active_stop_with_broker()
                mon._ensure_stop_for_filled_qty()

            broker.find_working_close_order.assert_not_called()
            broker.place_stop_order.assert_called_once()
            self.assertEqual(mon.state['active_stop']['order_id'], '481562002')

    def test_stop_is_current_false_when_ownership_conflict(self):
        st = _make_open_state(
            lot='12-30',
            active_stop={
                'order_id': '481561791',
                'type': 'STOP_LIMIT',
                'status': 'working',
                'quantity': 1,
            },
            stop_quantity=1,
        )
        st['lifecycle'] = {'stop_ownership_conflict': True}
        self.assertFalse(stop_is_current(st, ownership_conflict=True))

    def test_repair_dry_run_does_not_write_json(self):
        st = _make_open_state(lot='12-30', active_stop=None, stop_quantity=0)
        exp_stop, exp_limit = expected_exchange_stop_prices(st)

        broker = MagicMock()
        broker.find_working_close_orders.return_value = [
            OrderResult(
                True,
                '481561791',
                'live',
                order_quantity=1,
                raw=_broker_order_raw(exp_stop, exp_limit),
            )
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '12-30_P.json')
            state_mod.save_state(path, st)
            outcome = repair_orphan_stop(st, broker, apply=False)
            disk = state_mod.load_state(path)

        self.assertEqual(outcome.status, 'dry_run')
        self.assertIsNone(disk.get('active_stop'))

    def test_repair_apply_adopts_unique_orphan(self):
        st = _make_open_state(lot='12-30', active_stop=None, stop_quantity=0)
        exp_stop, exp_limit = expected_exchange_stop_prices(st)

        broker = MagicMock()
        broker.find_working_close_orders.return_value = [
            OrderResult(
                True,
                '481561791',
                'live',
                order_quantity=1,
                raw=_broker_order_raw(exp_stop, exp_limit),
            )
        ]

        outcome = repair_orphan_stop(st, broker, apply=True, repair_reason='test_repair')
        self.assertEqual(outcome.status, 'adopted')
        self.assertEqual(st['active_stop']['order_id'], '481561791')
        self.assertTrue(st['active_stop']['repair_mode'])
        self.assertTrue(st['active_stop']['adopted_from_broker'])

    def test_repair_ambiguous_refuses_adopt(self):
        st = _make_open_state(lot='12-30', active_stop=None, stop_quantity=0)
        exp_stop, exp_limit = expected_exchange_stop_prices(st)

        broker = MagicMock()
        broker.find_working_close_orders.return_value = [
            OrderResult(True, '481561791', 'live', order_quantity=1, raw=_broker_order_raw(exp_stop, exp_limit)),
            OrderResult(True, '481561792', 'live', order_quantity=1, raw=_broker_order_raw(exp_stop, exp_limit)),
        ]

        outcome = repair_orphan_stop(st, broker, apply=True)
        self.assertEqual(outcome.status, 'ambiguous')
        self.assertIsNone(st.get('active_stop'))

    def test_duplicate_active_stop_detected(self):
        st1 = _make_open_state(
            lot='12-00',
            active_stop={'order_id': '481561791', 'type': 'STOP_LIMIT', 'status': 'working'},
            stop_quantity=1,
        )
        st2 = _make_open_state(
            lot='12-30',
            open_order_id='481576412',
            active_stop={'order_id': '481561791', 'type': 'STOP_LIMIT', 'status': 'working'},
            stop_quantity=1,
        )

        with tempfile.TemporaryDirectory() as tmp:
            p1 = os.path.join(tmp, '12-00_P.json')
            p2 = os.path.join(tmp, '12-30_P.json')
            state_mod.save_state(p1, st1)
            state_mod.save_state(p2, st2)
            dups = scan_duplicate_stop_ownership([p1, p2])

        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0].order_id, '481561791')
        self.assertEqual(set(ownership_conflict_paths(dups)), {p1, p2})

    def test_breach_cancels_only_own_stop(self):
        st = _make_open_state(
            lot='01-45',
            active_stop={
                'order_id': '481561791',
                'type': 'STOP_LIMIT',
                'stop_price': 2.05,
                'limit_price': 2.15,
                'phase': 1,
                'status': 'working',
                'quantity': 1,
            },
            stop_quantity=1,
        )

        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, '481561791', 'cancelled')
        broker.cancel_order.return_value = OrderResult(True, '481561791', 'cancelled')
        broker.place_limit_order.return_value = OrderResult(True, '481611728', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '01-45_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get.return_value = 1.75
            prices.get_market_mid.return_value = 1.75
            prices.get_spx.return_value = 7505.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon.replace_with_limit_close(reason='spread_stop_breach')

            broker.cancel_order.assert_called_once_with('481561791')
            broker.find_working_close_orders.assert_not_called()
            broker.find_working_close_order.assert_not_called()

    def test_v3_slow_sync_does_not_adopt(self):
        from blocks.stop.v3.supervisor import StopSupervisor
        from blocks.stop.v3.trade_slot import TradeSlot

        st = _make_open_state(lot='12-30', active_stop=None, stop_quantity=0)
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(False, '', 'unknown')
        broker.find_working_close_order.return_value = OrderResult(True, '481561791', 'live', order_quantity=1)
        broker.place_stop_order.return_value = OrderResult(True, '481562003', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '12-30_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7510.0
            sup = StopSupervisor(broker, prices, phases=[])
            slot = TradeSlot.from_loaded(path, st)
            slot.legacy_monitor = StopMonitor(path, broker, prices, phases=[])
            slot.legacy_monitor.state = slot.state
            sup._slow_broker_sync(slot)
            slot.legacy_monitor._ensure_stop_for_filled_qty()

        broker.find_working_close_order.assert_not_called()


class TestManualKillPerTranche(unittest.TestCase):
    def test_spread_close_cancels_only_own_stop_not_all_btc(self):
        st = _make_open_state(
            lot='01-45',
            active_stop={
                'order_id': '481561791',
                'type': 'STOP_LIMIT',
                'stop_price': 2.05,
                'limit_price': 2.15,
                'phase': 1,
                'status': 'working',
                'quantity': 1,
            },
            stop_quantity=1,
        )

        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, '481561791', 'cancelled')
        broker.cancel_order.return_value = OrderResult(True, '481561791', 'cancelled')
        broker.place_spread_close_order.return_value = OrderResult(True, '481700001', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '01-45_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.side_effect = lambda sym: 1.5 if '7485' in sym else 0.1
            prices.get.side_effect = prices.get_market_mid
            prices.get_spx.return_value = 7500.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon.replace_with_spread_close(reason='manual_close')

            broker.cancel_order.assert_called_once_with('481561791')
            broker.find_working_close_orders.assert_not_called()
            broker.find_working_close_order.assert_not_called()


class TestCloseDedupWithinTradeOnly(unittest.TestCase):
    def test_preflight_blocks_only_own_spread_close_order(self):
        from blocks.stop.v3.recovery import spread_close_preflight_blocked

        st_a = _make_open_state(lot='12-00', open_order_id='481561770')
        st_b = _make_open_state(lot='12-30', open_order_id='481576412')
        st_a['spread_close_order_id'] = '481700001'

        broker = MagicMock()
        broker.inspect_spread_position.return_value = 'ok'

        self.assertEqual(
            spread_close_preflight_blocked(
                broker, st_a,
                short_sym=st_a['short_leg']['symbol'],
                long_sym=st_a['long_leg']['symbol'],
                qty=1,
            ),
            'existing_close_order',
        )
        self.assertIsNone(
            spread_close_preflight_blocked(
                broker, st_b,
                short_sym=st_b['short_leg']['symbol'],
                long_sym=st_b['long_leg']['symbol'],
                qty=1,
            ),
        )


if __name__ == '__main__':
    unittest.main()
