"""Tests for software breach watch snapshot and dashboard display."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from blocks.stop.breach_watch import (
    NEAR_BREACH_GAP,
    breach_display_fields,
    breach_label_from_watch,
    build_breach_watch_snapshot,
    log_breach_watch,
    resolve_breach_status,
)


class TestResolveBreachStatus(unittest.TestCase):
    def test_stale_wins(self):
        self.assertEqual(
            resolve_breach_status(
                streamer_stale=True,
                short_mqtt=True,
                long_mqtt=True,
                gap_to_breach=0.10,
            ),
            'stale',
        )

    def test_no_prices(self):
        self.assertEqual(
            resolve_breach_status(
                streamer_stale=False,
                short_mqtt=False,
                long_mqtt=True,
                gap_to_breach=None,
            ),
            'no_prices',
        )

    def test_near_and_breached(self):
        self.assertEqual(
            resolve_breach_status(
                streamer_stale=False,
                short_mqtt=True,
                long_mqtt=True,
                gap_to_breach=NEAR_BREACH_GAP,
            ),
            'near',
        )
        self.assertEqual(
            resolve_breach_status(
                streamer_stale=False,
                short_mqtt=True,
                long_mqtt=True,
                gap_to_breach=-0.05,
            ),
            'breached',
        )


class TestBreachWatchSnapshot(unittest.TestCase):
    def test_12_00_pcs_snapshot(self):
        state = {
            'entry': {'net_credit': 1.9, 'two_x_net_credit': 3.8, 'side': 'P'},
            'short_leg': {'fill_price': 3.27, 'two_x_short': 6.55},
            'active_stop': {'stop_price': 6.3},
            'stop_multiplier': 2,
        }
        watch = build_breach_watch_snapshot(
            state,
            short_p=5.2,
            long_p=1.15,
            streamer_stale=False,
            now_iso='2026-06-26T12:00:00-05:00',
        )
        self.assertEqual(watch['threshold'], 4.0)
        self.assertEqual(watch['spread_mid'], 4.05)
        self.assertEqual(watch['gap_to_breach'], -0.05)
        self.assertEqual(watch['status'], 'breached')
        self.assertEqual(watch['exchange_stop'], 6.3)

    def test_missing_leg_prices(self):
        state = {
            'entry': {'net_credit': 1.9, 'two_x_net_credit': 3.8},
            'short_leg': {'fill_price': 3.27},
            'long_leg': {'fill_price': 1.37},
        }
        watch = build_breach_watch_snapshot(
            state,
            short_p=5.0,
            long_p=None,
            streamer_stale=False,
            now_iso='t',
        )
        self.assertEqual(watch['status'], 'no_prices')
        self.assertIn('long', breach_label_from_watch(watch).lower())


class TestBreachDisplayFields(unittest.TestCase):
    def test_closed_trade_empty(self):
        fields = breach_display_fields({}, live_short=1.0, live_long=0.5, trade_status='closed')
        self.assertEqual(fields['breach_label'], '')

    def test_live_prices_override(self):
        trade = {
            'entry': {'net_credit': 1.9, 'two_x_net_credit': 3.8},
            'breach_watch': {
                'threshold': 4.0,
                'spread_mid': 2.0,
                'gap_to_breach': 2.0,
                'short_mqtt': True,
                'long_mqtt': True,
                'streamer_stale': False,
            },
        }
        fields = breach_display_fields(
            trade,
            live_short=5.2,
            live_long=1.15,
            trade_status='open',
        )
        self.assertEqual(fields['breach_gap'], -0.05)
        self.assertEqual(fields['breach_status'], 'breached')
        self.assertIn('-0.05', fields['breach_label'])


class TestBreachWatchLogging(unittest.TestCase):
    def test_missing_prices_logs_once(self):
        monitor = MagicMock()
        monitor.state = {
            'lot': '12-00',
            'entry': {'side': 'P'},
            'short_leg': {'symbol': '.SPXW260626P7340'},
            'long_leg': {'symbol': '.SPXW260626P7315'},
        }
        monitor._breach_missing_prices_logged = False
        monitor._breach_stale_logged = False
        monitor._breach_last_near_log = 0.0
        monitor._breach_last_near_spread = None

        watch = {
            'status': 'no_prices',
            'short_mqtt': True,
            'long_mqtt': False,
            'threshold': 4.0,
        }
        with self.assertLogs('blocks.stop.breach_watch', level='WARNING') as cm:
            log_breach_watch(monitor, watch)
            log_breach_watch(monitor, watch)
        self.assertEqual(len(cm.output), 1)
        self.assertIn('missing MQTT', cm.output[0])


if __name__ == '__main__':
    unittest.main()
