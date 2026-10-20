"""Stop profile for Iron Fly break-even cut strategy."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile

IRON_FLY_PROFILE = 'iron_fly_break_even_cut'


def iron_fly_profile() -> StopProfile:
    return StopProfile(
        name=IRON_FLY_PROFILE,
        spread_type='credit',
        phases=[],
        long_close_delay_sec=0,
    )


register_stop_profile(IRON_FLY_PROFILE, iron_fly_profile)
