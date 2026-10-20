"""Manual spread credit-spread stop profile — standard stop losses without OCO or IC companion breach."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile
from blocks.stop.phases import (
    Phase1InitialStop,
    Phase2NetCreditUpgrade,
    Phase3SpxProximityClose,
)
from blocks.stop.phases_profit_target import PhaseProfitTarget

MANUAL_CREDIT_SPREAD_PROFILE = 'manual_credit_spread'


def manual_stop_profile() -> StopProfile:
    return StopProfile(
        name=MANUAL_CREDIT_SPREAD_PROFILE,
        spread_type='credit',
        phases=[
            PhaseProfitTarget(),
            Phase1InitialStop(),
            Phase2NetCreditUpgrade(),
            Phase3SpxProximityClose(),
        ],
        long_close_delay_sec=30,
        profit_target_short=None,
        use_oco_short_stop=False,
        adjust_to_breakeven_on_companion_breach=False,
    )


register_stop_profile(MANUAL_CREDIT_SPREAD_PROFILE, manual_stop_profile)
