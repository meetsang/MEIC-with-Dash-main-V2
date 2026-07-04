"""CreditSpreadEntry block tests."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import unittest

from blocks.entry.config import CreditEntryConfig
from blocks.entry.credit_spread import CreditSpreadEntry
from common.symbols import build_tastytrade_symbol, to_tastytrade
from blocks.entry.spread_scan import OTM_MAX_DEEP, OTM_MAX_MODERATE, resolve_scan_otm_max
from tests.mock_broker import MockBroker


class TestCreditSpreadEntry(unittest.TestCase):
    def test_scan_for_target_uses_api(self):
        broker = MockBroker()
        broker.prices['SPX'] = 6000.0
        expiry = '260625'
        short = build_tastytrade_symbol(expiry, 'P', 5970)
        long = build_tastytrade_symbol(expiry, 'P', 5945)
        broker.prices[to_tastytrade(short)] = 0.70
        broker.prices[to_tastytrade(long)] = 0.10

        entry = CreditSpreadEntry(
            broker,
            CreditEntryConfig(otm_min=5, otm_max=50, quote_source='api'),
            log=logging.getLogger('test'),
        )
        results = entry.scan_for_target(
            'P', expiry, 'test-lot', spread_width=25, target_credit=0.60, max_results=1,
        )
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0].market_credit, 0.60, delta=0.05)

    def test_meic_scan_low_credit_min_reaches_far_otm(self):
        """MEIC band at $0.60 needs deep OTM — same fix as manual target scan."""
        broker = MockBroker()
        broker.prices['SPX'] = 7225.0
        expiry = '260626'
        log = logging.getLogger('test')

        for otm in (10, 15, 20):
            short = build_tastytrade_symbol(expiry, 'P', 7225 - otm)
            long = build_tastytrade_symbol(expiry, 'P', 7225 - otm - 25)
            credit = 1.50 - (otm - 10) * 0.05
            broker.prices[to_tastytrade(short)] = credit + 0.80
            broker.prices[to_tastytrade(long)] = 0.80

        for otm in (75, 80):
            short = build_tastytrade_symbol(expiry, 'P', 7225 - otm)
            long = build_tastytrade_symbol(expiry, 'P', 7225 - otm - 25)
            credit = 0.62 if otm == 75 else 0.58
            broker.prices[to_tastytrade(short)] = credit + 0.15
            broker.prices[to_tastytrade(long)] = 0.15

        entry = CreditSpreadEntry(
            broker,
            CreditEntryConfig(
                spread_width_min=25,
                spread_width_max=25,
                credit_min=0.55,
                credit_max_put=0.70,
                credit_max_call=0.70,
                otm_min=5,
                quote_source='api',
            ),
            log=log,
        )
        results = entry.scan_for_meic('P', expiry, 'test-lot')
        self.assertEqual(len(results), 1)
        self.assertGreaterEqual(results[0].market_credit, 0.55)
        self.assertLessEqual(results[0].market_credit, 0.70)

    def test_resolve_scan_otm_max(self):
        self.assertEqual(resolve_scan_otm_max(target_credit=0.60), OTM_MAX_DEEP)
        self.assertEqual(resolve_scan_otm_max(credit_min=0.85), OTM_MAX_MODERATE)
        self.assertEqual(resolve_scan_otm_max(credit_min=1.50), 150)

    def test_write_handshake_creates_v2_json(self):
        broker = MockBroker()
        entry = CreditSpreadEntry(broker)
        with tempfile.TemporaryDirectory() as tmp:
            path = entry.write_handshake(
                lot='test-lot',
                side='P',
                short_symbol='.SPXW260625P5970',
                long_symbol='.SPXW260625P5945',
                short_strike=5970,
                long_strike=5945,
                quantity=1,
                open_order_id='477000001',
                limit_credit=0.60,
                strategy='MEIC_IC',
                active_directory=tmp,
            )
            self.assertTrue(os.path.isfile(path))
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
            self.assertEqual(state['strategy_version'], '2.0')
            self.assertEqual(state['stop_profile'], 'meic_credit_spread')
            self.assertEqual(state['status'], 'pending_fill')


if __name__ == '__main__':
    unittest.main()
