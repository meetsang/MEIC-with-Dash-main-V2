"""Dashboard phase labels — multiplier shown separately from profile name."""
from __future__ import annotations

import unittest


class TestDashboardPhaseDisplay(unittest.TestCase):
    def test_phase_labels_are_multiplier_neutral(self):
        from dashboard.server import PHASE_DISPLAY, _phase_display

        self.assertEqual(PHASE_DISPLAY[1], 'Short Stop')
        self.assertEqual(PHASE_DISPLAY[2], 'Net Credit Stop')
        self.assertNotIn('2×', PHASE_DISPLAY[1])
        self.assertNotIn('2×', PHASE_DISPLAY[2])

        self.assertEqual(
            _phase_display({'phase1_active': True}),
            'Short Stop',
        )
        self.assertEqual(
            _phase_display({'phase2_activated_at': '2026-06-26T12:00:00-05:00'}),
            'Net Credit Stop',
        )

    def test_phase_shows_stop_multiplier_from_trade(self):
        from dashboard.server import _phase_display

        self.assertEqual(
            _phase_display({'phase1_active': True}, stop_multiplier=3),
            'Short Stop (3×)',
        )
        self.assertEqual(
            _phase_display({'phase1_active': True}, stop_multiplier=2),
            'Short Stop (2×)',
        )


if __name__ == '__main__':
    unittest.main()
