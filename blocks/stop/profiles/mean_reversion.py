"""Mean Reversion stop profile — position management owned by strategy run loop."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile

MEAN_REVERSION_PROFILE = 'mean_reversion'


def mean_reversion_profile() -> StopProfile:
    return StopProfile(
        name=MEAN_REVERSION_PROFILE,
        spread_type='credit',
        phases=[],
        long_close_delay_sec=0,
    )


register_stop_profile(MEAN_REVERSION_PROFILE, mean_reversion_profile)
