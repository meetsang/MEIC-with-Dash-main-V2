"""Chase sequence unit tests (phase 4f)."""
from __future__ import annotations

import unittest

from blocks.entry.chase import (
    chase_credit_step,
    chase_kind,
    max_entry_attempts,
    should_chase_on_unfilled,
)
from blocks.session.plan import SessionRow


def _row(**kwargs) -> SessionRow:
    base = dict(
        slot_key='01-15_P',
        lot='01-15',
        side='P',
        entry_window_start='13:14',
        entry_window_end='13:20',
        chase1_max=3,
        chase2_max=7,
        max_attempts=10,
    )
    base.update(kwargs)
    return SessionRow(**base)


class TestChaseSequence(unittest.TestCase):
    def test_chase_kind_sequence(self):
        row = _row()
        self.assertEqual(chase_kind(1, row), 'initial_scan')
        self.assertEqual(chase_kind(2, row), 'chase_same_trade')
        self.assertEqual(chase_kind(4, row), 'chase_same_trade')
        self.assertEqual(chase_kind(5, row), 'build_new_strikes')
        self.assertEqual(chase_kind(11, row), 'build_new_strikes')
        self.assertEqual(chase_kind(12, row), 'exhausted')

    def test_credit_step_is_five_cents(self):
        self.assertEqual(chase_credit_step(0.90), 0.85)
        self.assertEqual(chase_credit_step(1.00), 0.95)

    def test_max_attempts_capped(self):
        row = _row(max_attempts=5, chase1_max=3, chase2_max=7)
        self.assertEqual(max_entry_attempts(row), 5)

    def test_partial_fill_no_chase(self):
        self.assertFalse(should_chase_on_unfilled(2, 'chase_same_trade'))
        self.assertTrue(should_chase_on_unfilled(0, 'chase_same_trade'))
        self.assertFalse(should_chase_on_unfilled(0, 'none'))


if __name__ == '__main__':
    unittest.main()
