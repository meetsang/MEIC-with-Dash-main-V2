"""Close slippage — theoretical stop vs brokerage, plus execution efficiency."""
from __future__ import annotations

import unittest

from blocks.stop.close_fills import (
    apply_close_slippage_fields,
    brokerage_spread_exit_debit,
    exit_slippage_per_spread,
    leg_slippage,
    slippage_dollars,
    slippage_label,
    slippage_per_spread,
    stop_out_slippage_per_spread,
    stop_slippage_short,
)


class TestCloseFills(unittest.TestCase):
    def test_execution_efficiency_operator_fills(self):
        """7330/7305 breach — fills better than stop/limit (Jun 26)."""
        state = {
            'short_close_limit_price': 6.3,
            'long_close_limit_price': 3.1,
            'short_close_price': 5.7,
            'long_close_price': 3.3,
        }
        short_slip, long_slip = leg_slippage(state)
        self.assertEqual(short_slip, 0.6)
        self.assertEqual(long_slip, 0.2)
        self.assertEqual(exit_slippage_per_spread(state), 0.8)

    def test_jul02_11_00_pcs_operator_slippage(self):
        """Theoretical 2× credit ($2.00) vs brokerage spread exit ($2.15)."""
        state = {
            'entry': {'net_credit': 1.0, 'two_x_net_credit': 2.0},
            'short_close_price': 3.4,
            'long_close_price': 1.25,
            'close_mechanism': 'exchange_stop',
        }
        self.assertEqual(brokerage_spread_exit_debit(state), 2.15)
        self.assertEqual(stop_out_slippage_per_spread(state), -0.15)
        self.assertEqual(slippage_per_spread(state), -0.15)
        self.assertEqual(slippage_dollars(state), -15.0)

    def test_apply_close_slippage_fields(self):
        state = {
            'entry': {'net_credit': 1.0, 'two_x_net_credit': 2.0},
            'short_close_limit_price': 3.6,
            'long_close_limit_price': 1.25,
            'short_close_price': 3.4,
            'long_close_price': 1.25,
            'close_mechanism': 'exchange_stop',
            'close': {},
        }
        apply_close_slippage_fields(state)
        self.assertEqual(state['exit_slippage'], 0.2)
        self.assertEqual(state['slippage'], -0.15)
        self.assertEqual(state['close']['slippage'], -0.15)
        self.assertEqual(state['theoretical_stop_spread_debit'], 2.0)
        self.assertEqual(state['brokerage_spread_exit_debit'], 2.15)

    def test_stop_slippage_exchange_stop(self):
        state = {
            'designated_stop_price': 6.2,
            'short_close_price': 5.7,
            'close_mechanism': 'exchange_stop',
        }
        self.assertEqual(stop_slippage_short(state), 0.5)

    def test_stop_slippage_software_breach_uses_policy_uplift(self):
        state = {
            'designated_stop_price': 6.2,
            'short_close_price': 5.7,
            'short_close_limit_price': 7.3,
            'close_mechanism': 'software_breach',
        }
        # Execution metric — policy $1 above designated, not fill vs broker limit.
        self.assertEqual(stop_slippage_short(state), -1.0)

    def test_manual_close_slippage_is_zero(self):
        """Dashboard kill/close — not a stop exit; operator slippage is None."""
        state = {
            'status': 'closed',
            'entry': {'net_credit': 0.65, 'two_x_net_credit': 2.6},
            'short_leg': {'fill_price': 0.95},
            'long_leg': {'fill_price': 0.30},
            'short_close_price': 0.15,
            'close_mechanism': 'manual_close',
            'filled_quantity': 1,
        }
        self.assertIsNone(stop_out_slippage_per_spread(state))
        self.assertIsNone(slippage_dollars(state))
        self.assertEqual(slippage_label(None), '')

    def test_manual_close_null_long_defaults_to_zero_not_open_fill(self):
        """Jul 2 spread kill — missing long STC must not infer open long fill (0.37)."""
        state = {
            'status': 'closed',
            'short_close_price': 0.20,
            'long_close_price': None,
            'long_leg': {'fill_price': 0.37},
            'close_mechanism': 'manual_close',
        }
        self.assertEqual(brokerage_spread_exit_debit(state), 0.20)

    def test_admin_killswitch_null_long_defaults_to_zero(self):
        state = {
            'short_close_price': 1.05,
            'long_leg': {'fill_price': 0.40},
            'close_mechanism': 'admin_killswitch',
        }
        self.assertEqual(brokerage_spread_exit_debit(state), 1.05)

    def test_exchange_stop_null_long_no_open_fill_inference(self):
        """Stop/breach exits — no inference; brokerage exit unknown until long leg recorded."""
        state = {
            'short_close_price': 3.4,
            'long_leg': {'fill_price': 1.25},
            'close_mechanism': 'exchange_stop',
        }
        self.assertIsNone(brokerage_spread_exit_debit(state))
        self.assertIsNone(stop_out_slippage_per_spread(state))

    def test_slippage_dollars_scales_qty(self):
        state = {
            'entry': {'net_credit': 1.0, 'two_x_net_credit': 2.0},
            'short_close_price': 3.4,
            'long_close_price': 1.25,
            'filled_quantity': 2,
            'close_mechanism': 'exchange_stop',
        }
        self.assertEqual(slippage_dollars(state), -30.0)

    def test_slippage_label_dollars(self):
        self.assertEqual(slippage_label(80.0), '+$80.00')
        self.assertEqual(slippage_label(-12.5), '-$12.50')
        self.assertEqual(slippage_label(None), '')


if __name__ == '__main__':
    unittest.main()
