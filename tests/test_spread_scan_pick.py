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

    def test_scan_evaluates_wider_width_when_narrower_width_has_overlap_and_breaks(self):
        broker = MockBroker()
        broker.prices['SPX'] = 7660.0
        expiry = '260820'
        log = logging.getLogger('test')

        # Width 25: 7680/7705 has overlap. 7685/7710 credit is 0.50 (below credit_min 0.60)
        s25_1 = build_tastytrade_symbol(expiry, 'C', 7680)
        l25_1 = build_tastytrade_symbol(expiry, 'C', 7705)
        broker.prices[to_tastytrade(s25_1)] = 1.10
        broker.prices[to_tastytrade(l25_1)] = 0.15  # credit = 0.95, but has overlap

        s25_2 = build_tastytrade_symbol(expiry, 'C', 7685)
        l25_2 = build_tastytrade_symbol(expiry, 'C', 7710)
        broker.prices[to_tastytrade(s25_2)] = 0.60
        broker.prices[to_tastytrade(l25_2)] = 0.10  # credit = 0.50 (below credit_min 0.60) -> breaks width 25

        # Width 30: 7685/7715 has credit 0.85 (in-band 0.60-1.20) and no overlap
        s30_1 = build_tastytrade_symbol(expiry, 'C', 7685)
        l30_1 = build_tastytrade_symbol(expiry, 'C', 7715)
        broker.prices[to_tastytrade(s30_1)] = 0.95
        broker.prices[to_tastytrade(l30_1)] = 0.10  # credit = 0.85

        def fake_overlap(short_symbol, long_symbol, opt_type):
            if '7705' in long_symbol or '7705' in short_symbol:
                return 'mock overlap on 7705'
            return None

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', side_effect=fake_overlap):
            results = scan_credit_spreads(
                broker, 'C', expiry, '12-45', log,
                spread_width_min=25,
                spread_width_max=35,
                otm_min=20,
                otm_max=40,
                credit_min=0.60,
                credit_max=1.20,
                max_results=1,
                quote_source='api',
            )

        self.assertEqual(len(results), 1)
        # Verify it picked the clean candidate from width 30 or shifted width 25
        self.assertIsNone(results[0].overlap_warning)
        self.assertGreaterEqual(results[0].market_credit, 0.60)
        self.assertLessEqual(results[0].market_credit, 1.32)

    def test_resolve_overlap_candidate_expands_width_on_long_clash(self):
        broker = MockBroker()
        expiry = '260903'
        log = logging.getLogger('test')

        # Candidate with width 25: short 7420, long 7395 (overlap on long)
        cand = SpreadCandidate(
            short_symbol=build_tastytrade_symbol(expiry, 'P', 7420),
            long_symbol=build_tastytrade_symbol(expiry, 'P', 7395),
            short_strike=7420,
            long_strike=7395,
            market_credit=0.90,
            short_mid=1.20,
            long_mid=0.30,
            overlap_warning='Long leg 7395 overlaps with active trade',
        )

        price_map = {
            to_tastytrade(build_tastytrade_symbol(expiry, 'P', 7420)): 1.20,
            to_tastytrade(build_tastytrade_symbol(expiry, 'P', 7395)): 0.30,
            to_tastytrade(build_tastytrade_symbol(expiry, 'P', 7390)): 0.20,  # width 30 -> credit 1.00
        }

        def fake_overlap(short_symbol, long_symbol, opt_type, **kwargs):
            if '7395' in long_symbol:
                return 'overlap on 7395'
            return None

        from blocks.entry.spread_scan import _resolve_overlap_candidate

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', side_effect=fake_overlap):
            resolved = _resolve_overlap_candidate(
                broker,
                cand,
                'P',
                expiry,
                log,
                quote_source='api',
                price_map=price_map,
                target_credit=None,
                credit_min=0.60,
                credit_max=1.20,
                min_market_credit=0.05,
                spread_width_max=35,
            )

        self.assertIsNone(resolved.overlap_warning)
        self.assertEqual(resolved.short_strike, 7420)
        self.assertEqual(resolved.long_strike, 7390)
        self.assertEqual(abs(resolved.short_strike - resolved.long_strike), 30)
        self.assertAlmostEqual(resolved.market_credit, 1.00)

    def test_sub_bracket_credit_expands_debit_leg_into_band(self):
        broker = MockBroker()
        broker.prices['SPX'] = 7600.0
        expiry = '260915'
        log = logging.getLogger('test')

        # Short 7620 mid = 0.65
        # Width 25: Long 7645 mid = 0.25 -> credit = 0.40 (below credit_min = 0.50)
        # Width 30: Long 7650 mid = 0.10 -> credit = 0.55 (in bracket 0.50 - 1.00!)
        s = build_tastytrade_symbol(expiry, 'C', 7620)
        l25 = build_tastytrade_symbol(expiry, 'C', 7645)
        l30 = build_tastytrade_symbol(expiry, 'C', 7650)

        broker.prices[to_tastytrade(s)] = 0.65
        broker.prices[to_tastytrade(l25)] = 0.25
        broker.prices[to_tastytrade(l30)] = 0.10

        with patch('blocks.entry.spread_scan.leg_overlap_conflict', return_value=None):
            results = scan_credit_spreads(
                broker, 'C', expiry, '12-15', log,
                spread_width=25,
                spread_width_max=35,
                otm_min=20,
                otm_max=30,
                credit_min=0.50,
                credit_max=1.00,
                max_results=1,
                quote_source='api',
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].short_strike, 7620)
        self.assertEqual(results[0].long_strike, 7650)
        self.assertEqual(abs(results[0].short_strike - results[0].long_strike), 30)
        self.assertAlmostEqual(results[0].market_credit, 0.55)


if __name__ == '__main__':
    unittest.main()
