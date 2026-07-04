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
                with open(os.path.join(active, f'{lot}_test.json'), 'w', encoding='utf-8') as f:
                    json.dump(state, f)

            with patch('manual_spread.entry.state_mod.manual_spread_active_dir', return_value=active), \
                 patch('manual_spread.entry.state_mod.manual_spread_closed_dir', return_value=os.path.join(tmp, 'history')), \
                 patch('dashboard.manual_spread_handlers.sync_pending_fills'):
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


if __name__ == '__main__':
    unittest.main()
