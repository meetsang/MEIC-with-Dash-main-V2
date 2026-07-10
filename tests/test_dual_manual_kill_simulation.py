"""Simulate dual manual kill on two CCS (different exp) — Jul 6 / Jul 7 fixture."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from typing import List, Tuple
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from scripts.seed_dual_manual_kill_fixture import FIXTURES, _build_state, _filename
from tests.mock_broker import MockBroker


class SerializingBroker(MockBroker):
    """Mock broker with global lock — mimics single TT asyncio lane (Jul 2 pattern)."""

    def __init__(self, delay_sec: float = 0.05):
        super().__init__()
        self._lock = threading.Lock()
        self._delay = delay_sec
        self.op_log: List[Tuple[float, str]] = []

    def _record(self, name: str) -> None:
        with self._lock:
            self.op_log.append((time.monotonic(), name))
            if self._delay:
                time.sleep(self._delay)

    def cancel_order(self, order_id) -> OrderResult:
        self._record(f'cancel:{order_id}')
        return super().cancel_order(order_id)

    def get_order_status(self, order_id, **kwargs) -> OrderResult:
        self._record(f'status:{order_id}')
        return super().get_order_status(order_id)

    def place_spread_close_order(self, short_symbol, long_symbol, qty, debit_limit) -> OrderResult:
        self._record(f'spread_close:{short_symbol}')
        return super().place_spread_close_order(short_symbol, long_symbol, qty, debit_limit)


def _price_cache_for_specs():
    prices = MagicMock()
    mids = {}
    for spec in FIXTURES:
        short_sym = f'.SPXW{spec["expiry_yymmdd"]}C{spec["short_strike"]}'
        long_sym = f'.SPXW{spec["expiry_yymmdd"]}C{spec["long_strike"]}'
        mids[short_sym] = spec['short_mid']
        mids[long_sym] = spec['long_mid']
    prices.get_market_mid = lambda sym: mids.get(sym)
    prices.get = prices.get_market_mid
    prices.get_spx.return_value = 7483.24
    return prices


class TestDualManualKillSimulation(unittest.TestCase):
    def _seed_pair(self, tmp: str, broker: MockBroker) -> Tuple[StopMonitor, StopMonitor, str, str]:
        paths = []
        for spec, lot in zip(FIXTURES, ('ms-99', 'ms-100')):
            state = _build_state(spec, lot=lot)
            fname = _filename(lot)
            path = os.path.join(tmp, fname)
            state_mod.save_state(path, state)
            broker.orders[spec['stop_order_id']] = OrderResult(
                True, spec['stop_order_id'], 'working',
            )
            paths.append(path)

        prices = _price_cache_for_specs()
        with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
             patch('common.streamer_symbols.register_spread_symbols'):
            mon_a = StopMonitor(paths[0], broker, prices)
            mon_b = StopMonitor(paths[1], broker, prices)
        return mon_a, mon_b, paths[0], paths[1]

    def test_dual_kill_both_spreads_close_fast_with_mock_broker(self):
        """Two threads, shared broker — both manual kills should finish quickly offline."""
        broker = SerializingBroker(delay_sec=0.02)
        with tempfile.TemporaryDirectory() as tmp:
            mon_a, mon_b, _, _ = self._seed_pair(tmp, broker)
            results = {}

            def run_kill(mon: StopMonitor, key: str) -> None:
                t0 = time.monotonic()
                mon.replace_with_spread_close(reason='manual_close')
                results[key] = time.monotonic() - t0

            t0 = time.monotonic()
            ta = threading.Thread(target=run_kill, args=(mon_a, 'a'))
            tb = threading.Thread(target=run_kill, args=(mon_b, 'b'))
            ta.start()
            tb.start()
            ta.join(timeout=30)
            tb.join(timeout=30)
            wall = time.monotonic() - t0

            self.assertEqual(mon_a.state.get('status'), 'closed')
            self.assertEqual(mon_b.state.get('status'), 'closed')
            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertEqual(len(spread_closes), 2)
            # Offline mock: entire dual kill should be well under Jul 2 ~120s baseline
            self.assertLess(wall, 5.0, f'dual kill wall={wall:.2f}s ops={len(broker.op_log)}')

    def test_dashboard_close_commands_both_trades(self):
        """Write .close.json for both files; each monitor picks up its command."""
        broker = MockBroker()
        with tempfile.TemporaryDirectory() as tmp:
            mon_a, mon_b, path_a, path_b = self._seed_pair(tmp, broker)
            cmd_dir = os.path.join(tmp, 'commands')
            os.makedirs(cmd_dir, exist_ok=True)

            for path in (path_a, path_b):
                fname = os.path.basename(path)
                cmd_path = os.path.join(cmd_dir, f'{fname}.close.json')
                with open(cmd_path, 'w', encoding='utf-8') as f:
                    f.write('{"close_mechanism":"manual_close"}')

            with patch('blocks.stop.monitor._trades_dir', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                self.assertTrue(mon_a._check_dashboard_commands())
                self.assertTrue(mon_b._check_dashboard_commands())

            self.assertEqual(mon_a.state.get('status'), 'closed')
            self.assertEqual(mon_b.state.get('status'), 'closed')


if __name__ == '__main__':
    unittest.main()
