"""Unit tests for XSP Iron Fly strategy orchestrator."""
from __future__ import annotations

import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.journal_broker import JournalBroker
from strategies.iron_fly.book import FlyStatus
from strategies.iron_fly_xsp.book import IronFlyXspBook
from strategies.iron_fly_xsp.strategy import IronFlyXspConfig, IronFlyXspStrategy


def _mock_chain_for_xsp(center_a: int, center_b: int) -> dict:
    chain = {}
    for c in set((center_a, center_b)):
        chain[('C', c)] = {'bid': 3.10, 'ask': 3.30, 'mid': 3.20}
        chain[('P', c)] = {'bid': 3.00, 'ask': 3.20, 'mid': 3.10}
        for w in (1, 2, 3, 4, 5):
            chain[('C', c + w)] = {'bid': round(max(0.05, 1.20 - w * 0.20), 2), 'ask': round(max(0.10, 1.40 - w * 0.20), 2), 'mid': round(max(0.08, 1.30 - w * 0.20), 2)}
            chain[('P', c - w)] = {'bid': round(max(0.05, 1.10 - w * 0.20), 2), 'ask': round(max(0.10, 1.30 - w * 0.20), 2), 'mid': round(max(0.08, 1.20 - w * 0.20), 2)}
    return chain


class TestIronFlyXspStrategy(unittest.TestCase):
    def test_plan_and_open_fractional_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IronFlyXspConfig(
                name='IRON_FLY_XSP',
                paper=True,
                mode='PAPER',
            )
            strat = IronFlyXspStrategy(cfg)
            strat.trades_dir = tmp
            strat.start()

            # SPX spot 7755.0 -> scaled XSP 775.5 -> centers 775 and 776
            chain = _mock_chain_for_xsp(775, 776)
            strat._plan_and_open_pair(7755, 775, 776, chain)

            self.assertEqual(len(strat.book.pairs), 1)
            pair = list(strat.book.pairs.values())[0]
            self.assertEqual(pair.center_ideal, 775.5)
            self.assertEqual(pair.fly_a.center, 775)
            self.assertEqual(pair.fly_b.center, 776)
            self.assertEqual(pair.status, FlyStatus.OPEN)
            # Verify 2 contracts total (1 each)
            self.assertEqual(pair.fly_a.quantity, 1)
            self.assertEqual(pair.fly_b.quantity, 1)
            # Total flies in book = 2
            self.assertEqual(len(strat.book.flies), 2)
            strat.stop()

    def test_plan_and_open_integer_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IronFlyXspConfig(
                name='IRON_FLY_XSP',
                paper=True,
                mode='PAPER',
            )
            strat = IronFlyXspStrategy(cfg)
            strat.trades_dir = tmp
            strat.start()

            # SPX spot 7750.0 -> scaled XSP 775.0 -> centers 775 and 775
            chain = _mock_chain_for_xsp(775, 775)
            strat._plan_and_open_pair(7750, 775, 775, chain)

            self.assertEqual(len(strat.book.pairs), 1)
            pair = list(strat.book.pairs.values())[0]
            self.assertEqual(pair.center_ideal, 775.0)
            self.assertEqual(pair.fly_a.center, 775)
            self.assertEqual(pair.fly_b.center, 775)
            self.assertEqual(pair.status, FlyStatus.OPEN)
            strat.stop()

    def test_spot_cut_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IronFlyXspConfig(
                name='IRON_FLY_XSP',
                paper=True,
                mode='PAPER',
                be_buffer_pts=0.20,
            )
            strat = IronFlyXspStrategy(cfg)
            strat.trades_dir = tmp
            strat.start()

            chain = _mock_chain_for_xsp(775, 776)
            strat._plan_and_open_pair(7755, 775, 776, chain)
            pair = list(strat.book.pairs.values())[0]
            self.assertEqual(pair.status, FlyStatus.OPEN)

            # BE Low is ~775.5 - 4.60 = ~770.90
            # Force spot to breach low: 770.0
            mock_prices = MagicMock()
            mock_prices.get_price.return_value = 770.0
            mock_prices.get_spx.return_value = 7700.0
            strat.prices = mock_prices
            strat._fetch_xsp_chain = MagicMock(return_value=chain)

            strat._check_spot_cuts()

            self.assertEqual(pair.status, FlyStatus.CLOSED)
            self.assertEqual(pair.fly_a.status, FlyStatus.CLOSED)
            self.assertEqual(pair.fly_b.status, FlyStatus.CLOSED)
            strat.stop()

    def test_risk_cap_prevents_new_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IronFlyXspConfig(
                name='IRON_FLY_XSP',
                paper=True,
                mode='PAPER',
                max_open_risk=50.0,  # Very small cap
            )
            strat = IronFlyXspStrategy(cfg)
            strat.trades_dir = tmp
            strat.start()

            # Set open risk above max
            strat.book.open_risk = 100.0
            strat._get_live_spot = MagicMock(return_value=775.5)
            strat.last_spot_update_ts = time.time()

            strat.on_tick()
            # No pairs should be opened
            self.assertEqual(len(strat.book.pairs), 0)
            strat.stop()

    def test_config_overrides_contracts_and_restart_when_flat(self):
        from strategies.base import apply_config_overrides
        cfg = IronFlyXspConfig(
            name='IRON_FLY_XSP',
            paper=True,
            mode='PAPER',
        )
        overrides = {
            'mode': 'PAPER',
            'contracts': 2,
            'max_open_risk': 3000.0,
            'daily_loss_stop': 500.0,
            'restart_when_flat': False,
        }
        apply_config_overrides(cfg, overrides)
        self.assertEqual(cfg.contracts, 2)
        self.assertEqual(cfg.restart_when_flat, False)

    def test_restart_when_flat_blocks_when_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = IronFlyXspConfig(
                name='IRON_FLY_XSP',
                paper=True,
                mode='PAPER',
                restart_when_flat=False,
            )
            strat = IronFlyXspStrategy(cfg)
            strat.trades_dir = tmp
            strat.start()

            # Simulate having had a pair that is now CLOSED (book is flat)
            chain = _mock_chain_for_xsp(775, 776)
            strat._plan_and_open_pair(7755, 775, 776, chain)
            pair = list(strat.book.pairs.values())[0]
            pair.status = FlyStatus.CLOSED

            strat._get_live_spot = MagicMock(return_value=780.0)
            strat.last_spot_update_ts = time.time()
            strat.on_tick()

            # Should not open new pairs because book is flat and restart_when_flat=False
            self.assertEqual(len(strat.book.pairs), 1)
            strat.stop()

