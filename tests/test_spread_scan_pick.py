"""Tests for MEIC spread scan candidate selection."""
from __future__ import annotations

import logging
import unittest
from unittest.mock import patch

from common.symbols import build_tastytrade_symbol, to_tastytrade
from blocks.entry.spread_scan import (
    SpreadCandidate,
    _dedupe_spread_candidates,
    _overlap_shift_credit_bounds,
    pick_meic_candidate,
    scan_credit_spreads,
)
from tests.mock_broker import MockBroker


class TestPickMeicCandidate(unittest.TestCase):
    def test_prefers_first_non_overlap(self):
        dirty = SpreadCandidate(
            short_symbol='a', long_symbol='b',
            short_strike=7300, long_strike=7275,
            market_credit=0.90, short_mid=1.0, long_mid=0.1,
            overlap_warning='leg flip',
        )
        clean = SpreadCandidate(
            short_symbol='c', long_symbol='d',
            short_strike=7320, long_strike=7295,
            market_credit=0.85, short_mid=1.0, long_mid=0.15,
        )
        pick = pick_meic_candidate([dirty, clean])
        self.assertIs(pick, clean)

    def test_returns_none_when_all_overlap(self):
        dirty = SpreadCandidate(
            short_symbol='a', long_symbol='b',
            short_strike=7300, long_strike=7275,
            market_credit=0.90, short_mid=1.0, long_mid=0.1,
            overlap_warning='leg flip',
        )
        self.assertIsNone(pick_meic_candidate([dirty]))


class TestScanCreditSpreadsPick(unittest.TestCase):
    def test_scan_returns_clean_candidate_not_first_dirty(self):
        broker = MockBroker()
        broker.prices['SPX'] = 7350.0
        expiry = '260625'
        log = logging.getLogger('test')

        # First OTM pair: in credit band but will get overlap flag
        short1 = build_tastytrade_symbol(expiry, 'P', 7320)
        long1 = build_tastytrade_symbol(expiry, 'P', 7295)
        broker.prices[to_tastytrade(short1)] = 1.00
        broker.prices[to_tastytrade(long1)] = 0.10

        # Second pair further OTM — also in band, no overlap
        short2 = build_tastytrade_symbol(expiry, 'P', 7280)
        long2 = build_tastytrade_symbol(expiry, 'P', 7255)
        broker.prices[to_tastytrade(short2)] = 0.95
        broker.prices[to_tastytrade(long2)] = 0.10

        def fake_overlap(short_symbol, long_symbol, opt_type):
            if '7320' in short_symbol:
                return 'mock overlap on 7320'
            return None

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', side_effect=fake_overlap):
            with patch('blocks.entry.spread_scan._resolve_overlap_candidate', side_effect=lambda _b, c, *_a, **_k: c):
                results = scan_credit_spreads(
                    broker, 'P', expiry, '01-15', log,
                    spread_width=25,
                    otm_min=5,
                    otm_max=100,
                    credit_min=0.80,
                    credit_max=1.20,
                    max_results=1,
                    quote_source='api',
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_strike, 7280)
        self.assertIsNone(results[0].overlap_warning)


class TestOverlapShiftCreditExpansion(unittest.TestCase):
    def test_expanded_band_widens_min_and_max(self):
        cmin, cmax = _overlap_shift_credit_bounds(0.60, 1.00)
        self.assertAlmostEqual(cmin, 0.54)
        self.assertAlmostEqual(cmax, 1.10)

    def test_overlap_shift_accepts_credit_just_above_session_max(self):
        broker = MockBroker()
        broker.prices['SPX'] = 7435.0
        expiry = '260629'
        log = logging.getLogger('test')

        short1 = build_tastytrade_symbol(expiry, 'P', 7415)
        long1 = build_tastytrade_symbol(expiry, 'P', 7390)
        short2 = build_tastytrade_symbol(expiry, 'P', 7420)
        long2 = build_tastytrade_symbol(expiry, 'P', 7395)
        broker.prices[to_tastytrade(short1)] = 0.97
        broker.prices[to_tastytrade(long1)] = 0.28
        broker.prices[to_tastytrade(short2)] = 1.35
        broker.prices[to_tastytrade(long2)] = 0.28

        def fake_overlap(short_symbol, long_symbol, opt_type):
            if '7415' in short_symbol:
                return 'long 7390 flip conflict'
            return None

        def fake_resolve(expiry, side, short_strike, long_strike, **kwargs):
            if short_strike == 7415:
                return 7420, 7395, short2, long2, 1
            return short_strike, long_strike, short_symbol, long_symbol, 0

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', side_effect=fake_overlap):
            with patch('blocks.entry.spread_scan.resolve_leg_overlap', side_effect=fake_resolve):
                results = scan_credit_spreads(
                    broker, 'P', expiry, '02-00', log,
                    spread_width=25,
                    otm_min=20,
                    otm_max=20,
                    credit_min=0.60,
                    credit_max=1.00,
                    max_results=1,
                    quote_source='api',
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_strike, 7420)
        self.assertEqual(results[0].long_strike, 7395)
        self.assertAlmostEqual(results[0].market_credit, 1.05)
        self.assertIsNone(results[0].overlap_warning)


class TestDedupeSpreadCandidates(unittest.TestCase):
    def test_dedupe_keeps_better_distance(self):
        dup_a = SpreadCandidate(
            short_symbol='a', long_symbol='b',
            short_strike=7420, long_strike=7395,
            market_credit=1.05, short_mid=1.35, long_mid=0.30,
            distance_from_target=0.45,
        )
        dup_b = SpreadCandidate(
            short_symbol='c', long_symbol='d',
            short_strike=7420, long_strike=7395,
            market_credit=0.80, short_mid=1.00, long_mid=0.20,
            distance_from_target=0.20,
        )
        out = _dedupe_spread_candidates([dup_a, dup_b])
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0].market_credit, 0.80)

    def test_scan_target_mode_returns_unique_strike_pairs(self):
        broker = MockBroker()
        broker.prices['SPX'] = 7435.0
        expiry = '260629'
        log = logging.getLogger('test')

        pairs = [(7420, 7395), (7410, 7385), (7400, 7375)]
        for ss, ls in pairs:
            short = build_tastytrade_symbol(expiry, 'P', ss)
            long = build_tastytrade_symbol(expiry, 'P', ls)
            broker.prices[to_tastytrade(short)] = 1.0
            broker.prices[to_tastytrade(long)] = 0.2

        def fake_overlap(short_symbol, long_symbol, opt_type):
            if '7415' in short_symbol:
                return 'flip'
            return None

        def fake_resolve(expiry, side, short_strike, long_strike, **kwargs):
            if short_strike == 7415:
                ss, ls = 7420, 7395
                return (
                    ss,
                    ls,
                    build_tastytrade_symbol(expiry, 'P', ss),
                    build_tastytrade_symbol(expiry, 'P', ls),
                    1,
                )
            short = build_tastytrade_symbol(expiry, side, short_strike)
            long = build_tastytrade_symbol(expiry, side, long_strike)
            return short_strike, long_strike, short, long, 0

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', side_effect=fake_overlap):
            with patch('blocks.entry.spread_scan.resolve_leg_overlap', side_effect=fake_resolve):
                results = scan_credit_spreads(
                    broker, 'P', expiry, 'manual-scan', log,
                    spread_width=25,
                    otm_min=15,
                    otm_max=25,
                    target_credit=0.60,
                    max_results=10,
                    quote_source='api',
                )

        keys = [(c.short_strike, c.long_strike) for c in results]
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == '__main__':
    unittest.main()
