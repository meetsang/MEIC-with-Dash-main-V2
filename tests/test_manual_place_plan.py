"""Manual place plan field normalization."""
from __future__ import annotations

import unittest

from blocks.session.manual_place import apply_plan_metadata, manual_chase_enabled, plan_fields_from_request
from blocks.session.plan import SessionRow


class TestManualPlacePlan(unittest.TestCase):
    def test_plan_fields_leave_working(self):
        out = plan_fields_from_request({
            'stop_multiplier': 2,
            'on_unfilled': 'none',
            'fill_wait_sec': 5,
        })
        self.assertEqual(out['on_unfilled'], 'none')
        self.assertEqual(out['chase1_max'], 0)
        self.assertEqual(out['credit_min'], 0.0)

    def test_plan_fields_chase_same_spread(self):
        out = plan_fields_from_request({
            'stop_multiplier': 3,
            'on_unfilled': 'chase_same_trade',
            'fill_wait_sec': 8,
            'chase_floor': 0.45,
            'chase_max_attempts': 4,
        })
        self.assertEqual(out['chase1_mode'], 'chase_same_trade')
        self.assertEqual(out['chase1_max'], 4)
        self.assertEqual(out['credit_min'], 0.45)
        self.assertEqual(out['max_attempts'], 5)

    def test_manual_chase_enabled(self):
        row = SessionRow(
            slot_key='ms-1_P', lot='ms-1', side='P',
            entry_window_start='00:00', entry_window_end='23:59',
            on_unfilled='chase_same_trade', chase1_max=3,
        )
        self.assertTrue(manual_chase_enabled(row))

    def test_apply_plan_metadata(self):
        row = SessionRow(
            slot_key='ms-1_P', lot='ms-1', side='P',
            entry_window_start='00:00', entry_window_end='23:59',
            stop_multiplier=3, on_unfilled='chase_same_trade',
            credit_min=0.45, chase1_max=3, fill_wait_sec=8,
        )
        state = {'entry': {}, 'short_leg': {}}
        apply_plan_metadata(state, row)
        self.assertEqual(state['plan']['chase_floor'], 0.45)
        self.assertEqual(state['plan']['chase_max_attempts'], 3)


if __name__ == '__main__':
    unittest.main()
