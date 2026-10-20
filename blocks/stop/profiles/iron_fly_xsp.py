"""Stop profile for XSP Iron Fly strategy — break-even spot cut."""
from __future__ import annotations

from blocks.stop.stop_profile import StopProfile, register_stop_profile

IRON_FLY_XSP_BREAK_EVEN_PROFILE = 'iron_fly_xsp_break_even_cut'


def iron_fly_xsp_stop_profile() -> StopProfile:
    return StopProfile(
        name=IRON_FLY_XSP_BREAK_EVEN_PROFILE,
        spread_type='combo',
        phases=[],
        long_close_delay_sec=0,
        profit_target_short=None,
        use_oco_short_stop=False,
        adjust_to_breakeven_on_companion_breach=False,
    )


register_stop_profile(IRON_FLY_XSP_BREAK_EVEN_PROFILE, iron_fly_xsp_stop_profile)
