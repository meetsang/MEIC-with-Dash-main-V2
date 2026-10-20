"""Unit tests for Iron Fly Strategy lifecycle, sweep logic, and execution."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, time as dtime
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from strategies.iron_fly.book import IronFly, IronFlyBook
from strategies.iron_fly.executor import PaperExecutor
from strategies.iron_fly.strategy import IronFlyConfig, IronFlyStrategy


class TestIronFlyStrategy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = IronFlyConfig(
            name='IRON_FLY_SPX',
            mode='PAPER',
            contracts=1,
            max_open_risk=15000.0,
            daily_loss_stop=2500.0,
            fee_per_leg=1.30,
            root=self.tmp,
            paper=True,
        )
        self.mock_broker = MagicMock()
        self.strategy = IronFlyStrategy(self.config, self.mock_broker)
        self.strategy.book = IronFlyBook(day='2026-10-02', mode='PAPER')
        self.strategy.executor = PaperExecutor(fee_per_leg=1.30)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_and_open_initial_fly(self):
        # Synthetic chain around 5800 with 50-wide wings
        chain = {
            ('C', 5800.0): {'mid': 15.5, 'bid': 15.4, 'ask': 15.6, 'symbol': '.SPXW261002C5800'},
            ('P', 5800.0): {'mid': 15.0, 'bid': 14.9, 'ask': 15.1, 'symbol': '.SPXW261002P5800'},
            ('C', 5850.0): {'mid': 0.35, 'bid': 0.30, 'ask': 0.40, 'symbol': '.SPXW261002C5850'},
            ('P', 5750.0): {'mid': 0.35, 'bid': 0.30, 'ask': 0.40, 'symbol': '.SPXW261002P5750'},
        }

        self.strategy._plan_and_open_initial_fly(5800.0, chain)

        self.assertEqual(len(self.strategy.book.flies), 1)
        fly = list(self.strategy.book.flies.values())[0]
        self.assertEqual(fly.center, 5800.0)
        self.assertEqual(fly.call_width, 50)
        self.assertEqual(fly.put_width, 50)
        self.assertEqual(fly.status, 'OPEN')
        # Credit: shorts mid (15.5 + 15.0 = 30.5) - longs mid (0.35 + 0.35 = 0.70) = 29.80
        self.assertEqual(fly.fill_credit, 29.80)
        self.assertEqual(fly.be_low, round(5800.0 - 29.80, 2))
        self.assertEqual(fly.be_high, round(5800.0 + 29.80, 2))
        self.assertEqual(fly.fees, 5.20)  # 4 legs * 1 * 1.30

    def test_cut_fly_trigger_and_pnl(self):
        fly = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=50,
            put_width=50,
            qty=1,
            status='OPEN',
            planned_credit=18.0,
            fill_credit=18.0,
            be_low=5782.0,
            be_high=5818.0,
            max_loss=3200.0,
            fees=5.20,
        )
        self.strategy.book.flies[fly.id] = fly

        # Beyond breakeven high breach: spot at 5818.05 > 5818.0 + 0.01
        chain = {
            ('C', 5800.0): {'mid': 18.0, 'bid': 17.8, 'ask': 18.2},
            ('P', 5800.0): {'mid': 1.5, 'bid': 1.4, 'ask': 1.6},
            ('C', 5850.0): {'mid': 0.1, 'bid': 0.05, 'ask': 0.15},
            ('P', 5750.0): {'mid': 0.05, 'bid': 0.0, 'ask': 0.1},
        }

        self.strategy._evaluate_open_flies(5818.05, chain, dtime(11, 30, 0))

        # Fly must be closed due to beyond breakeven cut
        self.assertEqual(fly.status, 'CLOSED')
        self.assertEqual(fly.close_reason, 'be_high_breach')
        self.assertIsNotNone(fly.realized_pnl)
        self.assertEqual(fly.fees, 10.40)  # open fee 5.20 + cut fee 5.20

    def test_settle_expired_flies(self):
        fly = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=50,
            put_width=50,
            qty=1,
            status='OPEN',
            planned_credit=18.0,
            fill_credit=18.0,
            be_low=5782.0,
            be_high=5818.0,
            max_loss=3200.0,
            fees=5.20,
        )
        self.strategy.book.flies[fly.id] = fly

        # SPX settles exactly at 5805
        # Call spread: max(0, 5) - max(0, -45) = 5
        # Put spread: 0
        # Fly intrinsic = 5.0
        # PnL = (18.0 - 5.0) * 100 = 1300.0
        self.strategy._last_spot = 5805.0
        self.strategy._fetch_spot = MagicMock(return_value=5805.0)

        self.strategy._settle_expired_flies()

        self.assertEqual(fly.status, 'EXPIRED')
        self.assertEqual(fly.exit_debit, 5.0)
        self.assertEqual(fly.realized_pnl, 1300.0)
        self.assertEqual(fly.fees, 5.20)  # No cut fee on expiry

    def test_approve_and_skip_fly(self):
        self.strategy.book.mode = 'LIVE_APPROVE'
        fly = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=50,
            put_width=50,
            qty=1,
            status='PENDING_APPROVAL',
            planned_credit=18.0,
        )
        self.strategy.book.flies[fly.id] = fly

        # Approve
        self.assertTrue(self.strategy.approve_fly('IF_20261002_1'))
        self.assertEqual(fly.status, 'OPEN')

        # Skip
        fly2 = IronFly(
            id='IF_20261002_2',
            seq=2,
            day='2026-10-02',
            center=5820.0,
            call_width=50,
            put_width=50,
            qty=1,
            status='PENDING_APPROVAL',
            planned_credit=18.0,
        )
        self.strategy.book.flies[fly2.id] = fly2
        self.assertTrue(self.strategy.skip_fly('IF_20261002_2'))
        self.assertEqual(fly2.status, 'FAILED')
        self.assertEqual(fly2.close_reason, 'skipped_by_operator')

    def test_close_all_open_flies(self):
        fly = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=50,
            put_width=50,
            qty=1,
            status='OPEN',
            planned_credit=18.0,
            fill_credit=18.0,
        )
        self.strategy.book.flies[fly.id] = fly
        self.strategy.close_all_open_flies(reason='manual_panic')
        self.assertEqual(fly.status, 'CLOSED')
        self.assertEqual(fly.close_reason, 'manual_panic')

    def test_sweep_cycle_uses_atm_rounding(self):
        self.strategy._fetch_spot = MagicMock(return_value=5802.3)
        chain = {
            ('C', 5800.0): {'mid': 7.5, 'bid': 7.4, 'ask': 7.6, 'symbol': '.SPXW261002C5800'},
            ('P', 5800.0): {'mid': 7.0, 'bid': 6.9, 'ask': 7.1, 'symbol': '.SPXW261002P5800'},
            ('C', 5850.0): {'mid': 0.35, 'bid': 0.30, 'ask': 0.40, 'symbol': '.SPXW261002C5850'},
            ('P', 5750.0): {'mid': 0.35, 'bid': 0.30, 'ask': 0.40, 'symbol': '.SPXW261002P5750'},
        }
        self.strategy._fetch_chain = MagicMock(return_value=chain)
        self.assertEqual(self.strategy.config.atm_rounding, 5)
        self.strategy._run_sweep_cycle(dtime(9, 35, 0))
        self.assertEqual(self.strategy.book.em_open, 14.5)


if __name__ == '__main__':
    unittest.main()
