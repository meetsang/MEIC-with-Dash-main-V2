"""Regression: build_manual_trades must emit one row per dashboard trade."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from dashboard.manual_spread_handlers import build_manual_trades


def _noop(*args, **kwargs):
    return None


def _trade_pnl(*args, **kwargs):
    return 0.0, None, None, False


class TestBuildManualTradesRows(unittest.TestCase):
    def test_all_active_trades_become_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            today = date.today().isoformat()
            for i, lot in enumerate(('ms-81', 'ms-82', 'ms-83', 'ms-84'), start=1):
                state = {
                    'status': 'closed',
                    'lot': lot,
                    'filled_quantity': 1,
                    'entry': {
                        'strategy': 'MANUAL_SPREAD',
                        'side': 'P' if i % 2 else 'C',
                        'timestamp': f'{today}T10:0{i}:00-05:00',
                        'net_credit': 0.5,
                    },
                    'short_leg': {'strike': 7500, 'fill_price': 1.0, 'symbol': '.SPXW260702P7500'},
                    'long_leg': {'strike': 7525, 'fill_price': 0.5, 'symbol': '.SPXW260702P7525'},
                    'short_close_price': 0.1,
                    'long_close_price': 0.05,
                    'close_mechanism': 'manual_close',
                }
                side = 'P' if i % 2 else 'C'
                with open(os.path.join(active, f'{lot}_{side}.json'), 'w', encoding='utf-8') as f:
                    json.dump(state, f)

            with patch('manual_spread.entry.state_mod.manual_spread_active_dir', return_value=active), \
                 patch('manual_spread.entry.state_mod.manual_spread_closed_dir', return_value=os.path.join(tmp, 'history')):
                rows, _, _, _ = build_manual_trades(
                    live_price_fn=_noop,
                    phase_display_fn=lambda *a, **k: '',
                    trade_pnl_fn=_trade_pnl,
                    stop_label_fn=lambda *a, **k: '',
                    slot_state_fn=lambda *a, **k: 'closed',
                )
            self.assertEqual(len(rows), 4)
            lots = {r['lot'] for r in rows}
            self.assertEqual(lots, {'ms-81', 'ms-82', 'ms-83', 'ms-84'})

    def test_duplicate_history_archives_dedupe_by_lot_side(self):
        from manual_spread.entry import load_dashboard_manual_trades

        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            hist = os.path.join(tmp, 'history')
            os.makedirs(active)
            os.makedirs(hist)
            today = date.today().isoformat()
            base = {
                'status': 'closed',
                'lot': 'ms-185',
                'entry': {
                    'strategy': 'MANUAL_SPREAD',
                    'side': 'C',
                    'timestamp': f'{today}T10:00:00-05:00',
                    'net_credit': 0.5,
                },
                'short_leg': {'strike': 7600, 'fill_price': 0.8, 'symbol': '.SPXW260706C7600'},
                'long_leg': {'strike': 7625, 'fill_price': 0.3, 'symbol': '.SPXW260706C7625'},
            }
            for name in ('ms-185_C_old.json', 'ms-185_C_new.json'):
                st = dict(base)
                st['entry'] = dict(base['entry'])
                st['entry']['timestamp'] = f'{today}T22:00:00-05:00' if 'new' in name else f'{today}T21:00:00-05:00'
                with open(os.path.join(hist, name), 'w', encoding='utf-8') as f:
                    json.dump(st, f)

            with patch('manual_spread.entry.state_mod.manual_spread_active_dir', return_value=active), \
                 patch('manual_spread.entry.state_mod.manual_spread_closed_dir', return_value=hist), \
                 patch('common.session_cleanup.central_today', return_value=date.today()):
                trades = load_dashboard_manual_trades()
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['_filename'], 'ms-185_C_new.json')

    def test_known_test_lots_excluded_from_dashboard(self):
        from manual_spread.entry import load_dashboard_manual_trades

        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            hist = os.path.join(tmp, 'history')
            os.makedirs(active)
            os.makedirs(hist)
            today = date.today().isoformat()
            fixture = {
                'status': 'closed',
                'lot': 'ms-99',
                'entry': {
                    'strategy': 'MANUAL_SPREAD',
                    'side': 'C',
                    'timestamp': f'{today}T10:00:00-05:00',
                    'net_credit': 0.5,
                },
                'short_leg': {'strike': 7600, 'fill_price': 0.8, 'symbol': '.SPXW260706C7600'},
                'long_leg': {'strike': 7625, 'fill_price': 0.3, 'symbol': '.SPXW260706C7625'},
            }
            with open(os.path.join(hist, 'ms-99_C.json'), 'w', encoding='utf-8') as f:
                json.dump(fixture, f)

            with patch('manual_spread.entry.state_mod.manual_spread_active_dir', return_value=active), \
                 patch('manual_spread.entry.state_mod.manual_spread_closed_dir', return_value=hist), \
                 patch('common.session_cleanup.central_today', return_value=date.today()):
                trades = load_dashboard_manual_trades()
            self.assertEqual(trades, [])

    def test_closed_history_beats_stale_active_open_ghost(self):
        from manual_spread.entry import load_dashboard_manual_trades

        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            hist = os.path.join(tmp, 'history')
            os.makedirs(active)
            os.makedirs(hist)
            today = date.today().isoformat()
            closed = {
                'status': 'closed',
                'lot': 'ms-185',
                'close_mechanism': 'operator_manual',
                'entry': {
                    'strategy': 'MANUAL_SPREAD',
                    'side': 'P',
                    'timestamp': f'{today}T09:16:42-05:00',
                    'net_credit': 0.7,
                },
                'short_leg': {'strike': 7425, 'fill_price': 1.32, 'symbol': '.SPXW260707P7425'},
                'long_leg': {'strike': 7400, 'fill_price': 0.62, 'symbol': '.SPXW260707P7400'},
                'close': {'brokerage_spread_exit_debit': 0.05},
            }
            ghost = {
                'status': 'open',
                'lot': 'ms-185',
                'close_only_mode': True,
                'exit_handler': 'manual_close',
                'entry': dict(closed['entry']),
                'short_leg': dict(closed['short_leg']),
                'long_leg': dict(closed['long_leg']),
            }
            with open(os.path.join(hist, 'ms-185_P_20260707T091639.json'), 'w', encoding='utf-8') as f:
                json.dump(closed, f)
            with open(os.path.join(active, 'ms-185_P_20260707T091639.json'), 'w', encoding='utf-8') as f:
                json.dump(ghost, f)
                # Make active ghost look newer on disk than history.
                os.utime(os.path.join(active, 'ms-185_P_20260707T091639.json'), None)

            with patch('manual_spread.entry.state_mod.manual_spread_active_dir', return_value=active), \
                 patch('manual_spread.entry.state_mod.manual_spread_closed_dir', return_value=hist), \
                 patch('common.session_cleanup.central_today', return_value=date.today()):
                trades = load_dashboard_manual_trades()
            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0]['status'], 'closed')
            self.assertEqual(trades[0]['lot'], 'ms-185')


if __name__ == '__main__':
    unittest.main()
