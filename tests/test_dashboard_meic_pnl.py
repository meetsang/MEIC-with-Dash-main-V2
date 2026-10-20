"""MEIC dashboard PnL must match tranche grid totals."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from dashboard.server import (
    MEIC_GRID_PNL_STATES,
    _counts_toward_meic_day_pnl,
    _resolve_trade_for_row,
    _trade_has_position,
    build_summary,
)


def _row(*, slot_key='11-00_P', lot='11-00', side='P', state='entered', trade_path=''):
    return SimpleNamespace(
        slot_key=slot_key,
        lot=lot,
        side=side,
        state=state,
        trade_path=trade_path,
        paused=False,
        skip=False,
        quantity=1,
        width='25-35',
        credit_min=0.6,
        credit_max=1.2,
        entry_window_start='10:59',
        entry_window_end='11:05',
        chase1_mode='chase_same_trade',
        chase1_max=3,
        chase2_mode='build_new_strikes',
        chase2_max=7,
        stop_multiplier=2,
    )


def _trade(*, status='open', filled_quantity=1, net_credit=0.9, lot='11-00', side='P'):
    short_fill = 1.0 if filled_quantity > 0 else 0.0
    long_fill = 0.1 if filled_quantity > 0 else 0.0
    return {
        'status': status,
        'filled_quantity': filled_quantity,
        'entry': {
            'side': side,
            'lot': lot,
            'net_credit': net_credit,
            'timestamp': '2026-07-23T11:00:00-05:00',
        },
        'short_leg': {'symbol': '.SPXW260723P7345', 'strike': 7345, 'fill_price': short_fill},
        'long_leg': {'symbol': '.SPXW260723P7320', 'strike': 7320, 'fill_price': long_fill},
        '_filename': f'{lot}_{side}_test.json',
    }


class TestMeicPnlHelpers(unittest.TestCase):
    def test_trade_has_position_requires_fill(self):
        self.assertFalse(_trade_has_position(_trade(status='pending_fill', filled_quantity=0)))
        self.assertTrue(_trade_has_position(_trade(status='open', filled_quantity=1)))

    def test_failed_row_ignores_orphan_trade(self):
        ghost = _trade(status='pending_fill', filled_quantity=0, lot='01-15', side='P')
        row = _row(slot_key='01-15_P', lot='01-15', side='P', state='failed')
        self.assertIsNone(_resolve_trade_for_row(row, [ghost]))

    def test_failed_row_with_trade_path_can_count(self):
        row = _row(state='failed', trade_path='/tmp/filled.json')
        self.assertTrue(
            _counts_toward_meic_day_pnl(
                row,
                _trade(status='open', filled_quantity=1),
                'open',
            )
        )

    def test_counts_toward_meic_day_pnl_matches_grid_states(self):
        row = _row()
        trade = _trade()
        for state in MEIC_GRID_PNL_STATES:
            self.assertTrue(_counts_toward_meic_day_pnl(row, trade, state))
        for state in ('failed', 'pending', 'entering'):
            self.assertFalse(_counts_toward_meic_day_pnl(row, trade, state))
        self.assertIn('expired', MEIC_GRID_PNL_STATES)


class TestBuildSummaryMeicPnl(unittest.TestCase):
    def _summary(self, rows, active_trades):
        plan = SimpleNamespace(rows=rows)
        with patch('dashboard.server._meic_session_rows', return_value=plan), \
             patch('dashboard.server._read_active_trades', return_value=active_trades), \
             patch('dashboard.server.read_bot_status', return_value={}), \
             patch('dashboard.server.build_manual_trades', return_value=([], 0.0, 0, 0.0)), \
             patch('dashboard.server._live_price', return_value=0.05), \
             patch('dashboard.server.live_prices', {}):
            return build_summary()

    def _grid_pnl_total(self, summary):
        total = 0.0
        for slot in summary['grid']:
            if slot.get('state') in MEIC_GRID_PNL_STATES and slot.get('live_pnl') is not None:
                if _trade_has_position_from_slot(slot):
                    total += float(slot['live_pnl'])
        return round(total, 2)

    def test_meic_pnl_excludes_failed_orphan_ghosts(self):
        rows = [
            _row(slot_key='11-00_P', lot='11-00', side='P', state='entered'),
            _row(slot_key='01-15_P', lot='01-15', side='P', state='failed'),
        ]
        trades = [
            _trade(status='open', filled_quantity=1, net_credit=0.9, lot='11-00', side='P'),
            _trade(status='pending_fill', filled_quantity=0, net_credit=0.75, lot='01-15', side='P'),
        ]
        summary = self._summary(rows, trades)
        self.assertEqual(summary['meic_pnl'], 90.0)
        failed_slot = next(s for s in summary['grid'] if s['slot_key'] == '01-15_P')
        self.assertEqual(failed_slot['state'], 'failed')
        self.assertNotIn('live_pnl', failed_slot)

    def test_meic_pnl_matches_grid_eligible_slots(self):
        rows = [
            _row(slot_key='11-00_P', state='entered'),
            _row(slot_key='11-00_C', lot='11-00', side='C', state='entered'),
            _row(slot_key='01-15_P', lot='01-15', side='P', state='failed'),
        ]
        trades = [
            _trade(status='open', filled_quantity=1, net_credit=0.9, lot='11-00', side='P'),
            {
                **_trade(status='closed', filled_quantity=1, net_credit=1.1, lot='11-00', side='C'),
                'short_close_price': 2.2,
                'long_close_price': 0.8,
                'close_mechanism': 'stop_breach',
            },
            _trade(status='pending_fill', filled_quantity=0, net_credit=0.75, lot='01-15', side='P'),
        ]
        summary = self._summary(rows, trades)
        grid_total = self._grid_pnl_total(summary)
        self.assertEqual(summary['meic_pnl'], grid_total)


def _trade_has_position_from_slot(slot):
    """Grid only shows live_pnl when spreadRowHasData is true."""
    return slot.get('state') in MEIC_GRID_PNL_STATES


if __name__ == '__main__':
    unittest.main()
