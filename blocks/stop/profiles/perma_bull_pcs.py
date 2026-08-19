"""Perma Bull PCS — profit target at fixed spread debit, no exchange stop."""
from __future__ import annotations

from blocks.stop.phases_profit_target import PhaseSpreadDebitProfitTarget
from blocks.stop.stop_profile import StopProfile, register_stop_profile

PERMA_BULL_PCS_PROFILE = 'perma_bull_pcs'


def perma_bull_pcs_profile() -> StopProfile:
    return StopProfile(
        name=PERMA_BULL_PCS_PROFILE,
        spread_type='credit',
        phases=[PhaseSpreadDebitProfitTarget()],
        long_close_delay_sec=0,
    )


register_stop_profile(PERMA_BULL_PCS_PROFILE, perma_bull_pcs_profile)
