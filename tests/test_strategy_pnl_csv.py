"""Test normalized strategy daily PnL CSV export and aggregation."""
import csv
import os
import tempfile
import unittest

from dashboard.strategy_pnl_csv import (
    build_daily_strategy_summary,
    export_strategy_daily_pnl_csv,
    get_sub_strategy,
)


class TestStrategyPnlCsv(unittest.TestCase):
    def test_get_sub_strategy(self):
        self.assertEqual(get_sub_strategy('DRMA_HM_SPX', 'fut_bull_123'), 'FUTURES')
        self.assertEqual(get_sub_strategy('DRMA_HM_SPX', 'dir_bear_456'), 'DIRECTIONAL')
        self.assertEqual(get_sub_strategy('DRMA_HM_SPX', 'credit_bull_789'), 'NON_DIRECTIONAL')
        self.assertEqual(get_sub_strategy('MEIC_IC', '10-30'), 'IRON_CONDOR')
        self.assertEqual(get_sub_strategy('MANUAL_SPREAD', 'ms-88'), 'MANUAL_SPREAD')
        self.assertEqual(get_sub_strategy('PERMA_BULL_PCS', 'perma-2026'), 'BULL_PCS')
        self.assertEqual(get_sub_strategy('SKYLINE_SPX', 'skyline-1'), 'BUTTERFLY')
        self.assertEqual(get_sub_strategy('CLONE_TRADES', 'trade_1'), 'DISCRETIONARY')

    def test_export_strategy_daily_pnl_csv_creates_normalized_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_csv = os.path.join(tmp, 'test_summary.csv')
            res = export_strategy_daily_pnl_csv(out_path=out_csv)
            self.assertTrue(os.path.isfile(res))
            with open(res, encoding='utf-8') as f:
                reader = csv.DictReader(f)
                expected_fields = [
                    'Date',
                    'Strategy',
                    'Sub_Strategy',
                    'Mode',
                    'Daily_PnL',
                    'Trades',
                    'Wins',
                    'Win_Rate_Pct',
                ]
                self.assertEqual(reader.fieldnames, expected_fields)
                rows = list(reader)
                # Verify rows exist and have valid values
                self.assertGreater(len(rows), 0)
                first = rows[0]
                self.assertIn(first['Mode'], ('live', 'paper'))
                self.assertTrue(len(first['Date']) == 10)
