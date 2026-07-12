"""Direct REST call-count tests for probe and entry order status."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from brokers.tastytrade_broker import BrokerCooldownActive, TastyTradeBroker


class TestBrokerRestDirectCalls(unittest.TestCase):
    def _make_broker(self):
        broker = object.__new__(TastyTradeBroker)
        broker.session = MagicMock()
        broker.account = MagicMock()
        broker._loop = MagicMock()
        broker._live_orders_cache = None
        broker._live_orders_ts = 0.0
        broker._live_orders_ttl = 2.0
        broker._last_broker_error = None
        return broker

    def test_probe_orders_rest_one_live_orders_call(self):
        broker = self._make_broker()
        per_order_calls = []

        async def _get_live_orders(session):
            o1 = MagicMock()
            o1.id = '111'
            o2 = MagicMock()
            o2.id = '222'
            return [o1, o2]

        async def _get_order(session, oid):
            per_order_calls.append(oid)
            return MagicMock()

        broker.account.get_live_orders = _get_live_orders
        broker.account.get_order = _get_order

        def _sync_run(coro, loop):
            import asyncio
            inner = asyncio.new_event_loop()
            try:
                value = inner.run_until_complete(coro)
            finally:
                inner.close()
            fut = MagicMock()
            fut.result = lambda timeout=None: value
            return fut

        with patch('brokers.tastytrade_broker.should_skip_priority', return_value=False), \
             patch('brokers.tastytrade_broker.get_rest_limiter') as mock_lim, \
             patch('brokers.tastytrade_broker.asyncio.run_coroutine_threadsafe', side_effect=_sync_run) as mock_rcts:
            mock_lim.return_value.acquire = MagicMock()
            result = broker.probe_orders_rest(timeout=5.0)

        self.assertTrue(result.ok, msg=result.detail)
        self.assertEqual(mock_rcts.call_count, 1)
        self.assertEqual(per_order_calls, [])

    def test_get_order_status_direct_one_get_order_call(self):
        broker = self._make_broker()
        get_order_calls = []
        live_cache_calls = {'n': 0}

        async def _get_order(session, oid):
            get_order_calls.append(oid)
            order = MagicMock()
            order.id = oid
            order.status = 'Filled'
            order.legs = []
            return order

        broker.account.get_order = _get_order
        broker.get_live_orders_cached = lambda ttl_sec=None: live_cache_calls.__setitem__('n', live_cache_calls['n'] + 1) or []

        def _run(coro, timeout=120, *, priority='NORMAL', op=''):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        broker._run = _run

        with patch('brokers.tastytrade_broker.should_skip_priority', return_value=False), \
             patch('brokers.tastytrade_broker.get_rest_limiter') as mock_lim, \
             patch('brokers.tastytrade_broker._order_result_from_placed_order') as mock_parse:
            mock_lim.return_value.acquire = MagicMock()
            mock_parse.return_value = OrderResult(
                True, '482624006', 'filled',
                filled_quantity=1,
                short_fill_price=1.2,
                long_fill_price=0.4,
                filled_price_source='broker_aggregate',
            )
            out = broker.get_order_status_direct(
                '482624006',
                priority='HIGH',
                op='entry_open_order_status',
            )

        self.assertEqual(get_order_calls, [482624006])
        self.assertEqual(live_cache_calls['n'], 0)
        self.assertTrue(out.success)
        self.assertEqual(out.filled_price_source, 'broker_aggregate')

    def test_get_order_status_direct_propagates_cooldown(self):
        broker = self._make_broker()

        def _run(*args, **kwargs):
            raise BrokerCooldownActive('cooldown')

        broker._run = _run
        with patch('brokers.tastytrade_broker.should_skip_priority', return_value=True):
            with self.assertRaises(BrokerCooldownActive):
                broker.get_order_status_direct('1')


if __name__ == '__main__':
    unittest.main()
