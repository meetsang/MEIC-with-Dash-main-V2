"""Tests for Perma Bull PCS profit-target stop profile."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from blocks.stop.monitor import StopMonitor
from blocks.stop.phases_profit_target import PhaseSpreadDebitProfitTarget
from blocks.stop.profiles.perma_bull_pcs import perma_bull_pcs_profile


class TestPermaBullProfitTarget(unittest.TestCase):
    def test_profile_has_no_exchange_stop_phase(self):
        phases = perma_bull_pcs_profile().phases
        names = {p.name for p in phases}
        self.assertIn('phase_spread_debit_profit_target', names)
        self.assertNotIn('phase1_initial_stop', names)

    def test_monitor_uses_trade_profile_phases_not_global_meic(self):
        state = {
            'stop_profile': 'perma_bull_pcs',
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'short_leg': {'symbol': 'S', 'strike': 5500, 'fill_price': 2.0},
            'long_leg': {'symbol': 'L', 'strike': 5495, 'fill_price': 0.95},
            'exit_config': {'profit_target_debit': 0.15},
            'phases': {},
            'entry': {'net_credit': 1.05},
        }
        broker = MagicMock()
        prices = MagicMock()
        prices.get_option_mid.side_effect = lambda sym: 0.12 if sym == 'S' else 0.05

        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False, encoding='utf-8') as f:
            f.write('{}')
            path = f.name
        try:
            mon = StopMonitor(path, broker, prices, phases=None)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        mon.state = state
        mon.stop_profile = perma_bull_pcs_profile()
        mon.phases = sorted(mon.stop_profile.phases, key=lambda p: p.priority)

        phase = mon.phases[0]
        self.assertTrue(phase.should_activate(mon))
        mon.replace_with_spread_close = MagicMock()
        phase.execute(mon)
        mon.replace_with_spread_close.assert_called_once_with(
            reason='profit_target_resting',
            debit_limit=0.15,
        )

    def test_resting_order_placed_without_mark_hit(self):
        phase = PhaseSpreadDebitProfitTarget()
        state = {
            'status': 'open',
            'exit_config': {'profit_target_debit': 0.15},
            'short_leg': {'symbol': 'S'},
            'long_leg': {'symbol': 'L'},
        }
        broker = MagicMock()
        prices = MagicMock()
        prices.get_option_mid.side_effect = lambda sym: 0.50 if sym == 'S' else 0.10
        mon = MagicMock()
        mon.state = state
        mon.prices = prices
        mon.stop_profile = perma_bull_pcs_profile()
        self.assertTrue(phase.should_activate(mon))


if __name__ == '__main__':
    unittest.main()
