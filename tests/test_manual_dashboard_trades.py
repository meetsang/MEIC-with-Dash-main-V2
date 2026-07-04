"""Dashboard manual spread visibility after close."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from dashboard.server import _slot_state_from_trade
from dashboard.db import STRATEGY_MANUAL
from manual_spread import entry as ms_entry


class TestManualDashboardTrades(unittest.TestCase):
    def test_manual_close_shows_closed_not_killed(self):
        self.assertEqual(
            _slot_state_from_trade('closed', 'manual_close', strategy=STRATEGY_MANUAL),
            'closed',
        )
        self.assertEqual(
            _slot_state_from_trade('closed', 'manual_close', strategy='MEIC_IC'),
            'killed',
        )

    def test_load_dashboard_includes_todays_closed_from_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            hist = os.path.join(tmp, 'history')
            os.makedirs(active)
            os.makedirs(hist)
            today = date.today().isoformat()

            open_trade = {
                'status': 'open',
                'lot': 'ms-1',
                'entry': {'strategy': 'MANUAL_SPREAD', 'side': 'C', 'timestamp': f'{today}T10:00:00-05:00'},
                'short_leg': {'strike': 7500},
                'long_leg': {'strike': 7525},
            }
            closed_trade = {
                'status': 'closed',
                'lot': 'ms-2',
                'entry': {'strategy': 'MANUAL_SPREAD', 'side': 'P', 'timestamp': f'{today}T11:00:00-05:00'},
                'short_leg': {'strike': 7400},
                'long_leg': {'strike': 7375},
                'close_mechanism': 'manual_close',
            }
            old_closed = dict(closed_trade)
            old_closed['lot'] = 'ms-0'
            old_closed['entry'] = {
                'strategy': 'MANUAL_SPREAD',
                'side': 'P',
                'timestamp': '2020-01-01T11:00:00-05:00',
            }

            with open(os.path.join(active, 'ms-1_C.json'), 'w', encoding='utf-8') as f:
                json.dump(open_trade, f)
            with open(os.path.join(hist, 'ms-2_P.json'), 'w', encoding='utf-8') as f:
                json.dump(closed_trade, f)
            with open(os.path.join(hist, 'ms-0_P.json'), 'w', encoding='utf-8') as f:
                json.dump(old_closed, f)

            with patch.object(ms_entry.state_mod, 'manual_spread_active_dir', return_value=active), patch.object(
                ms_entry.state_mod, 'manual_spread_closed_dir', return_value=hist
            ):
                rows = ms_entry.load_dashboard_manual_trades()

            lots = {r['lot'] for r in rows}
            self.assertEqual(lots, {'ms-1', 'ms-2'})


if __name__ == '__main__':
    unittest.main()
