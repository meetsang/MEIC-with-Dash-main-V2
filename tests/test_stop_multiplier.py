"""Stop multiplier flows from session JSON into exchange stop placement."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.handoff import apply_stop_snapshot
from blocks.session.plan import SessionRow
from blocks.stop import state as state_mod
from blocks.stop.fill_sync import apply_order_result_to_state
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.stop_math import exchange_stop_price, stop_multiplier_for_state
from brokers.base import OrderResult
from tests.mock_broker import MockBroker


class TestStopMultiplier(unittest.TestCase):
    def test_exchange_stop_price_ms50_example(self):
        # ms-50: short fill 1.32, Stop× 3 → (1.32 - 0.10) × 3 = 3.66 → $0.10 tick → 3.70
        self.assertEqual(exchange_stop_price(1.32, 3), 3.7)
        # Old bug used config 2× → 2.45
        self.assertEqual(exchange_stop_price(1.32, 2), 2.45)

    def test_exchange_stop_limit_aligns_ten_cent_tick(self):
        # 01-15_C: short 1.57, 2× → raw 2.95 / limit 3.1 — Tasty rejects $0.05 stop in $0.10 zone
        from blocks.stop.stop_math import exchange_stop_limit_prices

        stop, limit = exchange_stop_limit_prices(1.57, 2)
        self.assertEqual(stop, 3.0)
        self.assertEqual(limit, 3.1)

    def test_stop_multiplier_fractional(self):
        from blocks.stop.stop_math import exchange_stop_limit_prices

        self.assertEqual(stop_multiplier_for_state({'stop_multiplier': 1.5}), 1.5)
        stop, limit = exchange_stop_limit_prices(1.32, 1.5)
        self.assertEqual(stop, 1.85)
        self.assertGreater(limit, stop)

    def test_stop_multiplier_from_state_prefers_json_field(self):
        st = {'stop_multiplier': 3, 'plan': {'stop_multiplier': 2}, 'entry': {'side': 'P'}}
        self.assertEqual(stop_multiplier_for_state(st), 3.0)

    def test_apply_stop_snapshot_persists_multiplier(self):
        st = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260626P7290',
            long_symbol='.SPXW260626P7265',
            short_strike=7290,
            long_strike=7265,
            target_quantity=3,
            open_order_id='1',
            limit_credit=0.6,
        )
        st['short_leg']['fill_price'] = 1.32
        st['long_leg']['fill_price'] = 0.67
        st['entry']['net_credit'] = 0.65
        row = SessionRow(
            slot_key='ms-1_P', lot='ms-1', side='P',
            entry_window_start='00:00', entry_window_end='23:59',
            stop_multiplier=3,
        )
        apply_stop_snapshot(st, row)
        self.assertEqual(st['stop_multiplier'], 3)
        self.assertEqual(st['short_leg']['two_x_short'], 3.95)
        self.assertEqual(st['entry']['two_x_net_credit'], 1.95)

    def test_fill_sync_recompute_uses_state_multiplier(self):
        st = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260626P7290',
            long_symbol='.SPXW260626P7265',
            short_strike=7290,
            long_strike=7265,
            target_quantity=3,
            open_order_id='1',
            limit_credit=0.6,
        )
        st['stop_multiplier'] = 3
        result = OrderResult(
            True, '1', 'filled',
            filled_quantity=3, order_quantity=3, filled_price=0.65,
            short_fill_price=1.32, long_fill_price=0.67,
        )
        apply_order_result_to_state(st, result)
        self.assertEqual(st['short_leg']['two_x_short'], 3.95)

    def test_setup_initial_stop_uses_state_multiplier(self):
        broker = MockBroker()
        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260626P7290',
            long_symbol='.SPXW260626P7265',
            short_strike=7290,
            long_strike=7265,
            short_fill=1.32,
            long_fill=0.67,
            net_credit=0.65,
            quantity=3,
            open_order_id='open-1',
        )
        st['stop_multiplier'] = 3
        st['short_leg']['two_x_short'] = 3.95
        st['entry']['two_x_net_credit'] = 1.95

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms1_P_test.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7360.0

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                mon = StopMonitor(path, broker, prices)
                mon.setup_initial_stop()

            stops = [p for p in broker.placed if p[0] == 'stop']
            self.assertEqual(len(stops), 1)
            self.assertEqual(stops[0][2], 3.7)
            self.assertEqual(
                mon.state['stop_history'][-1]['reason'],
                'initial_short_stop_3x',
            )


if __name__ == '__main__':
    unittest.main()
