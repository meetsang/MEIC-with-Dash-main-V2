"""End-to-end integration tests for MEIC in paper mode with JournalBroker."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.phases import Phase1InitialStop
from blocks.stop.profiles.meic import meic_stop_profile
from blocks.stop.v3.supervisor import StopSupervisor
from brokers.journal_broker import JournalBroker
from meic0dte.open.open_spread_tt import sync_entry_trade_state_from_broker
from tests.mock_broker import MockBroker


class TestJournalPaperMeic(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = self.tmp_dir.name
        self.orders_file = os.path.join(self.root, 'journal_orders.json')
        self.active_dir = os.path.join(self.root, 'trades', 'active', 'MEIC_IC')
        os.makedirs(self.active_dir, exist_ok=True)

        self.mock_inner = MockBroker()
        self.mock_inner.prices['SPX'] = 5800.0
        self.mock_inner.prices['.SPXW261014P5750'] = 1.25
        self.mock_inner.prices['.SPXW261014P5725'] = 0.55
        self.journal = JournalBroker(
            self.mock_inner,
            strategy_name='MEIC_IC',
            orders_file=self.orders_file,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_paper_entry_handshake_promotes_to_open(self):
        """Simulate MEIC worker placing spread order and syncing handshake."""
        # 1. Place spread order via JournalBroker
        open_res = self.journal.place_spread_order(
            '.SPXW261014P5750', '.SPXW261014P5725', 1, 0.70
        )
        self.assertTrue(open_res.success)
        self.assertEqual(open_res.status, 'filled')
        self.assertEqual(open_res.short_fill_price, 1.25)
        self.assertEqual(open_res.long_fill_price, 0.55)

        # 2. Handshake writes pending state to disk
        trade_path = os.path.join(self.active_dir, '10-30_P.json')
        st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='10-30',
            side='P',
            short_symbol='.SPXW261014P5750',
            long_symbol='.SPXW261014P5725',
            short_strike=5750,
            long_strike=5725,
            target_quantity=1,
            open_order_id=open_res.order_id,
            limit_credit=0.70,
        )
        state_mod.save_state(trade_path, st)

        # 3. sync_entry_trade_state_from_broker syncs fills from broker
        log_mock = MagicMock()
        state, ok, detail = sync_entry_trade_state_from_broker(self.journal, trade_path, log_mock)
        self.assertTrue(ok)
        self.assertEqual(state['status'], 'open')
        self.assertEqual(state['filled_quantity'], 1)
        self.assertEqual(state['short_leg']['fill_price'], 1.25)
        self.assertEqual(state['long_leg']['fill_price'], 0.55)
        self.assertAlmostEqual(state['entry']['net_credit'], 0.70, places=2)
        # Verify 2x stop thresholds are computed
        self.assertAlmostEqual(state['entry']['two_x_net_credit'], 1.40, places=2)
        self.assertGreater(state['short_leg']['two_x_short'], 0.0)

        # Verify inner broker never received any order
        self.assertEqual(len(self.mock_inner.placed), 0)

    def test_paper_stop_placement_and_exchange_stop_fill(self):
        """StopMonitor places initial 2x stop and detects exchange stop fill in paper mode."""
        # Setup open trade on disk
        trade_path = os.path.join(self.active_dir, '10-30_P.json')
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-30',
            side='P',
            short_symbol='.SPXW261014P5750',
            long_symbol='.SPXW261014P5725',
            short_strike=5750,
            long_strike=5725,
            short_fill=1.25,
            long_fill=0.55,
            net_credit=0.70,
            quantity=1,
            open_order_id='journal-test-open',
        )
        state_mod.save_state(trade_path, st)

        # Create mock price cache
        prices = MagicMock()
        prices.get_spx.return_value = 5800.0
        prices.get_market_mid = MagicMock(side_effect=lambda sym: self.mock_inner.prices.get(sym, 0.50))
        prices.get = prices.get_market_mid
        prices.is_stale.return_value = False
        prices.kill_switch = False

        with patch('blocks.stop.state.iter_active_trade_paths', return_value=[trade_path]):
            mon = StopMonitor(trade_path, self.journal, prices, stop_profile=meic_stop_profile())
            # Ensure stop placement
            mon._ensure_stop_for_filled_qty()

            # Active stop should now be placed as a working paper order
            active = mon.state.get('active_stop')
            self.assertIsNotNone(active)
            self.assertEqual(active.get('status'), 'working')
            stop_oid = active.get('order_id')
            self.assertTrue(stop_oid.startswith('journal-'))

            # Check that JournalBroker holds this working stop
            order_res = self.journal.get_order_status(stop_oid)
            self.assertEqual(order_res.status, 'working')

            # Now short option price surges to 2.80 (2x short loss hit)
            self.mock_inner.prices['.SPXW261014P5750'] = 2.80
            # Next reconcile tick checks order status
            mon._reconcile_active_stop_with_broker()

            # The stop order has now triggered and filled locally in JournalBroker
            active_after = mon.state.get('active_stop')
            self.assertEqual(mon.state.get('status'), 'closing')
            self.assertEqual(mon.state.get('close_mechanism'), 'exchange_stop')
            self.assertIsNotNone(mon.state.get('short_closed_at'))

            # Verify no orders hit the mock inner broker
            self.assertEqual(len(self.mock_inner.placed), 0)

    def test_software_breach_exit_in_paper_mode(self):
        """Phase1InitialStop detects software breach on spread mid and cancels stop to place limit close."""
        trade_path = os.path.join(self.active_dir, '10-30_P.json')
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-30',
            side='P',
            short_symbol='.SPXW261014P5750',
            long_symbol='.SPXW261014P5725',
            short_strike=5750,
            long_strike=5725,
            short_fill=1.25,
            long_fill=0.55,
            net_credit=0.70,
            quantity=1,
            open_order_id='journal-test-open',
        )
        state_mod.save_state(trade_path, st)

        # Place initial working stop
        stop_res = self.journal.place_stop_order('.SPXW261014P5750', 1, 2.50, 2.80)
        st['active_stop'] = {
            'order_id': stop_res.order_id,
            'type': 'STOP_LIMIT',
            'stop_price': 2.50,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        st['stop_quantity'] = 1
        state_mod.save_state(trade_path, st)

        # Market prices: short option surges so spread mid = 2.00 - 0.20 = 1.80 >= 2x credit (1.40)
        self.mock_inner.prices['.SPXW261014P5750'] = 2.00
        self.mock_inner.prices['.SPXW261014P5725'] = 0.20

        prices = MagicMock()
        prices.get_spx.return_value = 5800.0
        prices.get_market_mid = MagicMock(side_effect=lambda sym: self.mock_inner.prices.get(sym))
        prices.get = prices.get_market_mid
        prices.get_quote = MagicMock(return_value=None)
        prices.is_stale.return_value = False
        prices.kill_switch = False
        prices.current_stream_session_id.return_value = 'test-session'
        prices.last_event_kind.return_value = 'quote'

        with patch('blocks.stop.state.iter_active_trade_paths', return_value=[trade_path]):
            mon = StopMonitor(trade_path, self.journal, prices, stop_profile=meic_stop_profile())
            # Execute software breach exit
            mon.replace_with_limit_close(reason='spread_stop_breach')

            # Working stop should be cancelled
            prior_stop_st = self.journal.get_order_status(stop_res.order_id)
            self.assertEqual(prior_stop_st.status, 'cancelled')

            # New active stop should be limit order to buy back short leg
            active = mon.state.get('active_stop')
            self.assertIsNotNone(active)
            self.assertEqual(active.get('type'), 'LIMIT')
            self.assertEqual(mon.state.get('close_mechanism'), 'software_breach')

            # Verify no live orders hit inner broker
            self.assertEqual(len(self.mock_inner.placed), 0)


if __name__ == '__main__':
    unittest.main()
