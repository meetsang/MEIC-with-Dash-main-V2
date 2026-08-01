"""Monitor-only stop profile for defined-risk hold-to-expiry strategies."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile

HOLD_TO_EXPIRY_PROFILE = 'hold_to_expiry'
SKYLINE_HOLD_TO_EXPIRY_PROFILE = 'skyline_hold_to_expiry'


def hold_to_expiry_profile(name: str = HOLD_TO_EXPIRY_PROFILE, *, spread_type: str = 'credit') -> StopProfile:
    return StopProfile(
        name=name,
        spread_type=spread_type,
        phases=[],
        long_close_delay_sec=0,
    )


def skyline_hold_to_expiry_profile() -> StopProfile:
    return hold_to_expiry_profile(SKYLINE_HOLD_TO_EXPIRY_PROFILE, spread_type='debit')


register_stop_profile(HOLD_TO_EXPIRY_PROFILE, lambda: hold_to_expiry_profile(HOLD_TO_EXPIRY_PROFILE))
register_stop_profile(SKYLINE_HOLD_TO_EXPIRY_PROFILE, skyline_hold_to_expiry_profile)
