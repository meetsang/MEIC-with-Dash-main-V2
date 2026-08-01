"""Tests for per-strategy broker factory."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

from brokers.journal_broker import JournalBroker
from common import broker_factory


class TestBrokerFactoryMultiStrategy(unittest.TestCase):
    def setUp(self):
        broker_factory.reset_shared_broker()

    def tearDown(self):
        broker_factory.reset_shared_broker()

    @patch('common.broker_factory.get_shared_broker')
    @patch('common.broker_factory.get_broker')
    def test_distinct_strategy_brokers(self, mock_get_broker, mock_get_shared):
        live = MagicMock(name='live')
        mock_get_broker.return_value = live
        mock_get_shared.return_value = live

        b1 = broker_factory.get_shared_broker_for_strategy('MEIC_IC', paper=False)
        b2 = broker_factory.get_shared_broker_for_strategy('SKYLINE_SPX', paper=True)

        self.assertIs(b1, live)
        self.assertIsInstance(b2, JournalBroker)
        self.assertIs(b2._inner, live)
        self.assertIsNot(b1, b2)

    @patch('common.broker_factory.get_shared_broker')
    def test_journal_broker_does_not_deadlock_on_shared_lock(self, mock_get_shared):
        mock_get_shared.return_value = MagicMock(name='live')

        result = {}
        error = {}

        def _create():
            try:
                result['broker'] = broker_factory.get_shared_broker_for_strategy(
                    'SKYLINE_SPX', paper=True,
                )
            except Exception as exc:
                error['exc'] = exc

        thread = threading.Thread(target=_create)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive(), 'get_shared_broker_for_strategy deadlocked')
        self.assertNotIn('exc', error)
        self.assertIsInstance(result['broker'], JournalBroker)


if __name__ == '__main__':
    unittest.main()
