"""MEIC credit-spread stop profile — phases shared; stop× lives on trade JSON."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile
from blocks.stop.phases import (
    PhaseCompanionBreachAdjustment,
    Phase1InitialStop,
    Phase2NetCreditUpgrade,
    Phase3SpxProximityClose,
)

MEIC_CREDIT_SPREAD_PROFILE = 'meic_credit_spread'
LEGACY_MEIC_PROFILE = 'meic_2x_short'  # alias for pre-Jun-26 JSON / yaml


def meic_stop_profile() -> StopProfile:
    from common.meic_settings import is_15c_oco_enabled

    oco_enabled = is_15c_oco_enabled()
    return StopProfile(
        name=MEIC_CREDIT_SPREAD_PROFILE,
        spread_type='credit',
        phases=[
            PhaseCompanionBreachAdjustment(),
            Phase1InitialStop(),
            Phase2NetCreditUpgrade(),
            Phase3SpxProximityClose(),
        ],
        long_close_delay_sec=30,
        profit_target_short=0.15 if oco_enabled else None,
        use_oco_short_stop=oco_enabled,
        adjust_to_breakeven_on_companion_breach=True,
    )


register_stop_profile(MEIC_CREDIT_SPREAD_PROFILE, meic_stop_profile)
register_stop_profile(LEGACY_MEIC_PROFILE, meic_stop_profile)
