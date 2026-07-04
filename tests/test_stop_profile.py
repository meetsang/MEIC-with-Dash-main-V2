"""StopProfile and MEIC profile tests."""
from __future__ import annotations

import unittest

import blocks.stop.profiles  # noqa: F401
from blocks.stop.profiles.meic import (
    LEGACY_MEIC_PROFILE,
    MEIC_CREDIT_SPREAD_PROFILE,
    meic_stop_profile,
)
from blocks.stop.stop_profile import resolve_stop_profile
from blocks.stop.phases import Phase1InitialStop, Phase2NetCreditUpgrade, Phase3SpxProximityClose


class TestStopProfile(unittest.TestCase):
    def test_meic_profile_has_three_phases(self):
        profile = meic_stop_profile()
        self.assertEqual(profile.name, MEIC_CREDIT_SPREAD_PROFILE)
        self.assertEqual(profile.spread_type, 'credit')
        self.assertEqual(len(profile.phases), 3)
        self.assertEqual(profile.long_close_delay_sec, 30)

    def test_meic_phase_types(self):
        profile = meic_stop_profile()
        self.assertIsInstance(profile.phases[0], Phase1InitialStop)
        self.assertIsInstance(profile.phases[1], Phase2NetCreditUpgrade)
        self.assertIsInstance(profile.phases[2], Phase3SpxProximityClose)

    def test_resolve_from_new_trade_state(self):
        state = {'stop_profile': MEIC_CREDIT_SPREAD_PROFILE}
        profile = resolve_stop_profile(state)
        self.assertEqual(profile.name, MEIC_CREDIT_SPREAD_PROFILE)

    def test_resolve_legacy_alias(self):
        state = {'stop_profile': LEGACY_MEIC_PROFILE}
        profile = resolve_stop_profile(state)
        self.assertEqual(profile.name, MEIC_CREDIT_SPREAD_PROFILE)

    def test_breach_defaults_credit_spread(self):
        profile = meic_stop_profile()
        self.assertEqual(profile.breach_calc(1.0, 0.3), 0.7)
        self.assertTrue(profile.breach_condition(2.0, 1.5))
        self.assertFalse(profile.breach_condition(1.0, 1.5))


if __name__ == '__main__':
    unittest.main()
