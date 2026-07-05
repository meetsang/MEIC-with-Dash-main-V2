"""V3 SoftwareBreachHandler — phase execution via exit pool."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.handlers.software_breach import SoftwareBreachHandler
from blocks.stop.v3.trade_slot import TradeSlot
from tests.mock_broker import MockBroker


def _open_state(**overrides):
    st = state_mod.create_new_state(
        strategy='MANUAL_SPREAD',
        lot='ms-v3',
        side='C',
        short_symbol='.SPXW260706C7600',
        long_symbol='.SPXW260706C7625',
        short_strike=7600,
        long_strike=7625,
        short_fill=0.82,
        long_fill=0.27,
        net_credit=0.55,
        quantity=3,
        open_order_id='open-v3',
    )
    st['active_stop'] = {
        'order_id': '9001',
        'type': 'STOP_LIMIT',
        'stop_price': 1.7,
        'limit_price': 1.8,
        'phase': 1,
        'status': 'working',
        'quantity': 3,
    }
    st['stop_quantity'] = 3
    st.update(overrides)
    return st


class TestV3SoftwareBreach(unittest.TestCase):
    def test_phase_execute_runs_in_handler(self):
        broker = MockBroker()
        prices = MagicMock()
        prices.get_spx.return_value = 7483.0
        prices.get_market_mid = MagicMock(return_value=0.20)
        prices.get = prices.get_market_mid
        prices.kill_switch = False

        phase = MagicMock()
        phase.name = 'phase2_net_credit'
        phase.execute = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms-v3_C_test.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)
            lane = BrokerLane(max_concurrent=4)

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                SoftwareBreachHandler(
                    slot, broker, prices, lane, phase,
                ).run()

            phase.execute.assert_called_once()
            self.assertEqual(slot.state.get('exit_handler'), 'breach_phase2_net_credit')
            self.assertTrue(slot.state.get('exit_started_at'))
            self.assertIn('phase_done', slot.state.get('exit_last_step', ''))
