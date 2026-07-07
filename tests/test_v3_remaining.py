"""V3 remaining — observability, stall reconcile, startup recovery, broker lane."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.recovery import recover_route, reconcile_stalled_exit
from blocks.stop.v3.trade_slot import TradeSlot
from tests.mock_broker import MockBroker


class TestV3RecoverRoute(unittest.TestCase):
    def test_spread_close_poll_route(self):
        st = {
            'status': 'closing',
            'spread_close_order_id': '8800',
            'close_only_mode': True,
            'recovery': {},
        }
        slot = TradeSlot(path='/tmp/t.json', state=st)
        self.assertEqual(recover_route(slot), 'poll_close_order')

    def test_long_chase_route(self):
        st = {
            'status': 'closing',
            'short_closed_at': 1.0,
            'exit_handler': 'exchange_stop',
            'recovery': {},
        }
        slot = TradeSlot(path='/tmp/t.json', state=st)
        self.assertEqual(recover_route(slot), 'resume_long_chase')


class TestV3StallReconcile(unittest.TestCase):
    def test_reconcile_clears_stall_when_spread_close_filled(self):
        broker = MockBroker()
        broker.orders['8800'] = OrderResult(
            True, '8800', 'filled',
            filled_price=0.25,
            filled_quantity=3,
            short_fill_price=0.30,
            long_fill_price=0.05,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = {
                'status': 'closing',
                'exit_handler': 'manual_close',
                'exit_stalled': True,
                'spread_close_order_id': '8800',
                'recovery': {},
            }
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            progress, _ = reconcile_stalled_exit(slot, broker)

            self.assertTrue(progress)
            self.assertEqual(slot.state.get('status'), 'closed')
            self.assertFalse(slot.state.get('exit_stalled'))


class TestV3BrokerLaneMetrics(unittest.TestCase):
    def test_in_flight_tracks_concurrent_ops(self):
        lane = BrokerLane(max_concurrent=2)
        gate = __import__('threading').Event()
        started = __import__('threading').Event()

        def _slow() -> None:
            started.set()
            gate.wait(timeout=5)

        import threading
        t1 = threading.Thread(target=lambda: lane.run('a', _slow), daemon=True)
        t2 = threading.Thread(target=lambda: lane.run('b', _slow), daemon=True)
        t1.start()
        t2.start()
        started.wait(timeout=2)
        self.assertGreaterEqual(lane.in_flight, 1)
        gate.set()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(lane.in_flight, 0)
