"""G8 — orphan closing recovery on stop_monitor load."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import LONG_CLOSE_DELAY_SEC, StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache


class TestClosingOrphanRecovery(unittest.TestCase):
    def test_on_load_resumes_long_close_when_delay_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='test-lot',
                side='P',
                short_symbol='.SPXW260625P7300',
                long_symbol='.SPXW260625P7275',
                short_strike=7300,
                long_strike=7275,
                short_fill=1.0,
                long_fill=0.4,
                net_credit=0.6,
                quantity=1,
                open_order_id='1',
            )
            st['status'] = 'closing'
            st['short_closed_at'] = time.time() - LONG_CLOSE_DELAY_SEC - 5
            st['close_mechanism'] = 'exchange_stop'
            path = os.path.join(tmp, 'trade.json')
            state_mod.save_state(path, st)

            broker = MagicMock()
            broker.fetch_option_mids_api = None
            broker.get_order_status.return_value = OrderResult(False, '', 'unknown')
            broker.place_limit_order.return_value = OrderResult(True, '478000001', 'working')
            prices = MqttPriceCache()
            prices._client = MagicMock()
            prices._connected = True
            now = time.time()
            with prices._lock:
                prices._prices['.SPXW260625P7275'] = 0.15
                prices._last_msg_at = now

            class _SyncThread:
                def __init__(self, target=None, **kwargs):
                    self._target = target

                def start(self):
                    if self._target:
                        self._target()

            with patch('blocks.stop.monitor.threading.Thread', _SyncThread):
                mon = StopMonitor(path, broker, prices)
                mon._on_load()
            broker.place_limit_order.assert_called()

    def test_on_load_finalizes_when_long_already_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='test-lot',
                side='P',
                short_symbol='.SPXW260625P7300',
                long_symbol='.SPXW260625P7275',
                short_strike=7300,
                long_strike=7275,
                short_fill=1.0,
                long_fill=0.4,
                net_credit=0.6,
                quantity=1,
                open_order_id='1',
            )
            st['status'] = 'closing'
            st['short_closed_at'] = time.time() - 120
            st['long_close_order_id'] = '478000002'
            path = os.path.join(tmp, 'trade.json')
            state_mod.save_state(path, st)

            broker = MagicMock()

            def _order_status(oid, **kwargs):
                if str(oid) == '478000002':
                    return OrderResult(True, '478000002', 'filled', filled_price=0.12)
                return OrderResult(False, '', 'unknown')

            broker.get_order_status.side_effect = _order_status
            prices = MqttPriceCache()

            mon = StopMonitor(path, broker, prices)
            mon._on_load()
            self.assertEqual(mon.state.get('status'), 'closed')


if __name__ == '__main__':
    unittest.main()
