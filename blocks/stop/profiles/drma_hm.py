"""DRMA + Hilega-Milega stop profile — position management owned by strategy run loop."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile

DRMA_HM_PROFILE = 'drma_hm_strategy'


def drma_hm_profile() -> StopProfile:
    return StopProfile(
        name=DRMA_HM_PROFILE,
        spread_type='both',
        phases=[],
        long_close_delay_sec=0,
    )


register_stop_profile(DRMA_HM_PROFILE, drma_hm_profile)
