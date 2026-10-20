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

    def test_phase_shows_breakeven_and_closed_states(self):
        from dashboard.server import _phase_display

        # Breakeven tightened
        self.assertEqual(
            _phase_display({'phase1_active': True, 'breakeven_tightened': True}, stop_multiplier=1),
            'Breakeven (1×)',
        )

        # Closed terminal outcomes
        self.assertEqual(
            _phase_display({}, status='closed', close_mechanism='profit_target_oco'),
            'Closed (PT 15¢)',
        )
        self.assertEqual(
            _phase_display({}, status='closed', close_mechanism='profit_target'),
            'Closed (PT)',
        )
        self.assertEqual(
            _phase_display({}, status='closed', close_mechanism='profit_target_hit'),
            'Closed (PT)',
        )
        self.assertEqual(
            _phase_display({}, status='closed', close_mechanism='exchange_stop'),
            'Closed (Stop)',
        )
        self.assertEqual(
            _phase_display({}, status='closed', close_mechanism='expiry_settlement'),
            'Settled (Expiry)',
        )

    def test_format_phase_tooltip(self):
        from dashboard.server import _format_phase_tooltip

        history = [
            {'timestamp': '2026-09-03T12:14:21-05:00', 'action': 'placed', 'price': 2.45, 'order_id': '8035597', 'reason': 'initial_short_stop_2x'},
            {'timestamp': '2026-09-03T12:36:31-05:00', 'action': 'tightened_breakeven', 'price': 1.20, 'order_id': '8037782', 'reason': 'companion_breached'},
            {'timestamp': '2026-09-03T14:02:03-05:00', 'action': 'filled', 'price': 0.15, 'order_id': '8037782', 'reason': 'profit_target_oco'},
        ]
        tip = _format_phase_tooltip(history, 'Closed (PT 15¢)')
        self.assertIn('Phase Progression:', tip)
        self.assertIn('12:14:21: Placed @ $2.45 (#8035597) [initial_short_stop_2x]', tip)
        self.assertIn('12:36:31: Tightened Breakeven @ $1.20 (#8037782) [companion_breached]', tip)
        self.assertIn('14:02:03: Filled @ $0.15 (#8037782) [profit_target_oco]', tip)


if __name__ == '__main__':
    unittest.main()
