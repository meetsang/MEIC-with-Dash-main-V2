"""Unit tests for stop_monitor JSON state."""
import json
import os
import tempfile
import unittest

from blocks.stop import state as state_mod


class TestState(unittest.TestCase):
    def test_create_new_state_shape(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='11-00',
            side='P',
            short_symbol='.SPXW260619P5550',
            long_symbol='.SPXW260619P5520',
            short_strike=5550,
            long_strike=5520,
            short_fill=4.0,
            long_fill=2.5,
            net_credit=1.5,
            quantity=1,
            open_order_id='12345',
        )
        self.assertEqual(st['status'], 'open')
        self.assertIsNone(st['active_stop'])
        self.assertEqual(st['filled_quantity'], 1)
        self.assertEqual(st['entry']['two_x_net_credit'], 3.0)
        self.assertEqual(st['short_leg']['two_x_short'], 8.0)

    def test_atomic_save_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'test_trade.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='test',
                side='C',
                short_symbol='.SPXW260619C5600',
                long_symbol='.SPXW260619C5630',
                short_strike=5600,
                long_strike=5630,
                short_fill=3.0,
                long_fill=1.5,
                net_credit=1.5,
                quantity=1,
                open_order_id='991234567',
            )
            name = state_mod.stable_trade_filename('11-00', 'C', '20260625T110000')
            self.assertEqual(name, '11-00_C_20260625T110000.json')
            state_mod.save_state(path, st)
            loaded = state_mod.load_state(path)
            self.assertEqual(loaded['entry']['side'], 'C')
            with open(path, encoding='utf-8') as f:
                json.load(f)  # valid JSON

    def test_append_history(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='11-00',
            side='P',
            short_symbol='.SPXW260619P5550',
            long_symbol='.SPXW260619P5520',
            short_strike=5550,
            long_strike=5520,
            short_fill=4.0,
            long_fill=2.5,
            net_credit=1.5,
            quantity=1,
            open_order_id='1',
        )
        state_mod.append_stop_history(
            st,
            action='placed',
            order_id='stop-1',
            price=8.0,
            phase=1,
            reason='initial_2x_short',
            spx_price_at_event=5500.0,
        )
        self.assertEqual(len(st['stop_history']), 1)
        self.assertEqual(st['stop_history'][0]['spx_price_at_event'], 5500.0)


if __name__ == '__main__':
    unittest.main()
