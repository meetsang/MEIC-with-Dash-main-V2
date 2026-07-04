"""G6 — breach checks frozen when streamer health is stale."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from common.streamer_health import is_stale, write_health
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache


class TestStreamerStaleGuard(unittest.TestCase):
    def test_is_stale_when_no_health_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(is_stale(root=tmp))

    def test_not_stale_with_recent_spx_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
            write_health(last_spx_price_ts=ts, root=tmp)
            self.assertFalse(is_stale(root=tmp))

    def test_stale_when_spx_ts_old(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (datetime.now(timezone.utc) - timedelta(seconds=60)).astimezone()
            write_health(last_spx_price_ts=old.isoformat(timespec='seconds'), root=tmp)
            self.assertTrue(is_stale(root=tmp))

    def test_monitor_skips_breach_when_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = (datetime.now(timezone.utc) - timedelta(seconds=90)).astimezone()
            write_health(last_spx_price_ts=ts.isoformat(timespec='seconds'), root=tmp)

            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='test',
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
            st['status'] = 'open'
            st['active_stop'] = {'order_id': '99', 'status': 'working', 'quantity': 1}
            st['stop_quantity'] = 1
            path = os.path.join(tmp, 'trade.json')
            state_mod.save_state(path, st)

            broker = MagicMock()
            prices = MqttPriceCache()
            phase = MagicMock()
            phase.should_activate.return_value = True
            mon = StopMonitor(path, broker, prices)
            mon.phases = [phase]

            with patch('blocks.stop.monitor.streamer_prices_stale', return_value=True):
                with patch('blocks.stop.monitor.central_time') as ct:
                    ct.return_value = datetime(2026, 6, 24, 12, 0, 0)
                    mon._poll_once()

            phase.should_activate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
