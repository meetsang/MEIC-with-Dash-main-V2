"""Journal broker records fills locally without broker transmission."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.fill_sync import apply_order_result_to_state
from brokers.base import ComboLeg, OrderResult
from brokers.journal_broker import JournalBroker
from tests.mock_broker import MockBroker


class TestJournalBroker(unittest.TestCase):
    def test_multi_leg_recorded_at_limit_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            journal = JournalBroker(inner, strategy_name='SKYLINE_SPX', orders_file=orders_file)
            legs = [
                ComboLeg('.SPXW260731C6400', 'BUY_TO_OPEN', 1),
                ComboLeg('.SPXW260731C6425', 'SELL_TO_OPEN', 2),
                ComboLeg('.SPXW260731C6450', 'BUY_TO_OPEN', 1),
            ]
            result = journal.place_multi_leg_order(legs, price=-3.15)
            self.assertTrue(result.success)
            self.assertEqual(result.status, 'filled')
            self.assertEqual(result.filled_price, 3.15)
            self.assertFalse(result.transmitted)
            self.assertTrue(result.order_id.startswith('journal-'))
            self.assertEqual(len(inner.placed), 0)

    def test_market_data_delegates_to_inner(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            inner.prices['SPX'] = 6412.5
            journal = JournalBroker(inner, orders_file=orders_file)
            self.assertEqual(journal.get_spx_price(), 6412.5)

    def test_spread_order_leg_fills_and_trade_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            inner.prices['.SPXW261014P7700'] = 1.25
            inner.prices['.SPXW261014P7675'] = 0.55
            journal = JournalBroker(inner, strategy_name='MEIC_IC', orders_file=orders_file)

            result = journal.place_spread_order(
                '.SPXW261014P7700', '.SPXW261014P7675', 1, 0.70
            )
            self.assertTrue(result.success)
            self.assertEqual(result.status, 'filled')
            self.assertEqual(result.filled_price, 0.70)
            self.assertEqual(result.short_fill_price, 1.25)
            self.assertEqual(result.long_fill_price, 0.55)
            self.assertEqual(result.order_quantity, 1)
            self.assertEqual(result.filled_quantity, 1)

            # Test that apply_order_result_to_state promotes a pending_fill state to open
            st = state_mod.create_pending_state(
                strategy='MEIC_IC',
                lot='10-30',
                side='P',
                short_symbol='.SPXW261014P7700',
                long_symbol='.SPXW261014P7675',
                short_strike=7700,
                long_strike=7675,
                target_quantity=1,
                open_order_id=result.order_id,
                limit_credit=0.70,
            )
            changed = apply_order_result_to_state(st, result)
            self.assertTrue(changed)
            self.assertEqual(st['status'], 'open')
            self.assertEqual(st['filled_quantity'], 1)
            self.assertEqual(st['short_leg']['fill_price'], 1.25)
            self.assertEqual(st['long_leg']['fill_price'], 0.55)
            self.assertGreater(st['entry']['two_x_net_credit'], 0.0)
            self.assertGreater(st['short_leg']['two_x_short'], 0.0)

    def test_stop_order_lifecycle_and_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            inner.prices['.SPXW261014P7700'] = 1.25
            journal = JournalBroker(inner, orders_file=orders_file)

            # 1. Place initial 2x short stop at 2.50
            res = journal.place_stop_order('.SPXW261014P7700', 1, 2.50, 2.80)
            self.assertTrue(res.success)
            self.assertEqual(res.status, 'working')
            self.assertEqual(res.filled_quantity, 0)
            self.assertEqual(res.order_quantity, 1)

            # 2. Check status while market price is below stop price
            st = journal.get_order_status(res.order_id)
            self.assertEqual(st.status, 'working')

            # 3. Market price rises to 2.55 (stop breached)
            inner.prices['.SPXW261014P7700'] = 2.55
            st_triggered = journal.get_order_status(res.order_id)
            self.assertEqual(st_triggered.status, 'filled')
            self.assertEqual(st_triggered.filled_quantity, 1)
            self.assertIn('stop_triggered', st_triggered.message)

    def test_oco_order_profit_target_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            inner.prices['.SPXW261014P7700'] = 0.50
            journal = JournalBroker(inner, orders_file=orders_file)

            res = journal.place_oco_order(
                '.SPXW261014P7700', 1, profit_price=0.15, stop_price=2.50, limit_price=2.80
            )
            self.assertEqual(res.status, 'working')

            # Price drops to 0.10 (profit target reached)
            inner.prices['.SPXW261014P7700'] = 0.10
            st_triggered = journal.get_order_status(res.order_id)
            self.assertEqual(st_triggered.status, 'filled')
            self.assertEqual(st_triggered.filled_price, 0.15)
            self.assertIn('oco_profit_triggered', st_triggered.message)

    def test_cancel_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            journal = JournalBroker(inner, orders_file=orders_file)

            res = journal.place_stop_order('.SPXW261014P7700', 1, 2.50, 2.80)
            self.assertEqual(res.status, 'working')

            cancel_res = journal.cancel_order(res.order_id)
            self.assertTrue(cancel_res.success)
            self.assertEqual(cancel_res.status, 'cancelled')

            st = journal.get_order_status(res.order_id)
            self.assertEqual(st.status, 'cancelled')

    def test_cross_process_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            proc1 = JournalBroker(inner, orders_file=orders_file)
            res = proc1.place_spread_order('.SPXW261014P7700', '.SPXW261014P7675', 1, 0.70)

            # proc2 represents another process (e.g. stop_monitor)
            proc2 = JournalBroker(inner, orders_file=orders_file)
            st = proc2.get_order_status(res.order_id)
            self.assertEqual(st.status, 'filled')
            self.assertEqual(st.filled_price, 0.70)
            self.assertEqual(st.short_fill_price, res.short_fill_price)

    def test_get_single_leg_position_qty(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            trades_dir = os.path.join(tmp, 'trades_active')
            os.makedirs(trades_dir, exist_ok=True)
            trade_path = os.path.join(trades_dir, 'trade1.json')

            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='10-30',
                side='P',
                short_symbol='.SPXW261014P7700',
                long_symbol='.SPXW261014P7675',
                short_strike=7700,
                long_strike=7675,
                short_fill=1.25,
                long_fill=0.55,
                net_credit=0.70,
                quantity=1,
                open_order_id='open-123',
            )
            state_mod.save_state(trade_path, st)

            inner = MockBroker()
            journal = JournalBroker(inner, orders_file=orders_file)

            with patch('blocks.stop.state.iter_active_trade_paths', return_value=[trade_path]):
                short_qty = journal.get_single_leg_position_qty('.SPXW261014P7700')
                long_qty = journal.get_single_leg_position_qty('.SPXW261014P7675')
                other_qty = journal.get_single_leg_position_qty('.SPXW261014C7800')

                # Short leg is -1, long leg is +1, other is 0
                self.assertEqual(short_qty, -1)
                self.assertEqual(long_qty, 1)
                self.assertEqual(other_qty, 0)

                # Now simulate short leg closed
                st['short_closed_at'] = 1791999999.0
                state_mod.save_state(trade_path, st)
                short_qty_after = journal.get_single_leg_position_qty('.SPXW261014P7700')
                self.assertEqual(short_qty_after, 0)

    def test_find_working_close_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            journal = JournalBroker(inner, orders_file=orders_file)

            stop_res = journal.place_stop_order('.SPXW261014P7700', 1, 2.50, 2.80)
            found = journal.find_working_close_order('.SPXW261014P7700')
            self.assertIsNotNone(found)
            self.assertEqual(found.order_id, stop_res.order_id)
            self.assertEqual(found.status, 'working')

            cached = journal.get_live_orders_cached()
            self.assertEqual(len(cached), 1)
            self.assertEqual(cached[0].order_id, stop_res.order_id)

    def test_live_mutation_methods_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            journal = JournalBroker(inner, orders_file=orders_file)

            with self.assertRaises(RuntimeError):
                journal.execute_order('test')

            with self.assertRaises(RuntimeError):
                journal.send_order('test')

    def test_place_spread_close_order_resting_and_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            orders_file = os.path.join(tmp, 'orders.json')
            inner = MockBroker()
            # Spread is currently 1.05 in market (short=1.10, long=0.05)
            inner.prices['.SPXW261014P7765'] = 1.10
            inner.prices['.SPXW261014P7760'] = 0.05
            journal = JournalBroker(inner, orders_file=orders_file)

            # Place resting limit close at 0.15 debit (e.g. Perma Bull PCS profit target)
            res = journal.place_spread_close_order('.SPXW261014P7765', '.SPXW261014P7760', 1, 0.15)
            self.assertEqual(res.status, 'working')
            self.assertEqual(res.order_limit_price, 0.15)

            # Market still 1.05 -> still working
            st = journal.get_order_status(res.order_id)
            self.assertEqual(st.status, 'working')

            # Market drops to 0.15 (short=0.20, long=0.05 -> debit=0.15)
            inner.prices['.SPXW261014P7765'] = 0.20
            inner.prices['.SPXW261014P7760'] = 0.05
            st_filled = journal.get_order_status(res.order_id)
            self.assertEqual(st_filled.status, 'filled')
            self.assertEqual(st_filled.filled_price, 0.15)
            self.assertIn('spread_close_triggered', st_filled.message)


if __name__ == '__main__':
    unittest.main()
