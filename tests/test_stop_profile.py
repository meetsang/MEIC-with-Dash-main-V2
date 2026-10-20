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
    def test_meic_profile_configuration(self):
        # By default, 15c OCO flag is OFF -> normal stop (no profit target)
        profile = meic_stop_profile()
        self.assertEqual(profile.name, MEIC_CREDIT_SPREAD_PROFILE)
        self.assertEqual(profile.spread_type, 'credit')
        self.assertEqual(len(profile.phases), 4)
        self.assertEqual(profile.long_close_delay_sec, 30)
        self.assertIsNone(profile.profit_target_short)
        self.assertFalse(profile.use_oco_short_stop)
        self.assertTrue(profile.adjust_to_breakeven_on_companion_breach)

        # When 15c OCO flag is enabled -> OCO with 0.15 profit target
        from unittest.mock import patch
        with patch.dict('os.environ', {'MEIC_15C_OCO_ENABLED': '1'}):
            profile_oco = meic_stop_profile()
            self.assertEqual(profile_oco.profit_target_short, 0.15)
            self.assertTrue(profile_oco.use_oco_short_stop)

    def test_meic_phase_types(self):
        from blocks.stop.phases import PhaseCompanionBreachAdjustment
        profile = meic_stop_profile()
        self.assertIsInstance(profile.phases[0], PhaseCompanionBreachAdjustment)
        self.assertIsInstance(profile.phases[1], Phase1InitialStop)
        self.assertIsInstance(profile.phases[2], Phase2NetCreditUpgrade)
        self.assertIsInstance(profile.phases[3], Phase3SpxProximityClose)

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
