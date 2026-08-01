"""MEIC credit-spread profile with optional profit-target phase (opt-in via yaml)."""
from __future__ import annotations

from blocks.stop.phases import (
    Phase1InitialStop,
    Phase2NetCreditUpgrade,
    Phase3SpxProximityClose,
)
from blocks.stop.phases_profit_target import PhaseProfitTarget
from blocks.stop.stop_profile import StopProfile, register_stop_profile

MEIC_PROFIT_TARGET_PROFILE = 'meic_credit_spread_profit_target'


def meic_profit_target_profile() -> StopProfile:
    """Same as meic_credit_spread plus PhaseProfitTarget when exit_config.profit_target_pct is set."""
    return StopProfile(
        name=MEIC_PROFIT_TARGET_PROFILE,
        spread_type='credit',
        phases=[
            PhaseProfitTarget(),
            Phase1InitialStop(),
            Phase2NetCreditUpgrade(),
            Phase3SpxProximityClose(),
        ],
        long_close_delay_sec=30,
    )


register_stop_profile(MEIC_PROFIT_TARGET_PROFILE, meic_profit_target_profile)
