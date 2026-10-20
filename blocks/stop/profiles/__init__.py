"""Registered stop profiles."""
from blocks.stop.profiles import hold_to_expiry  # noqa: F401 — registers hold_to_expiry + skyline
from blocks.stop.profiles import meic  # noqa: F401 — registers meic_credit_spread + legacy alias
from blocks.stop.profiles import meic_profit_target  # noqa: F401 — opt-in profit target profile
from blocks.stop.profiles import perma_bull_pcs  # noqa: F401 — perma bull PCS profit target
from blocks.stop.profiles import manual  # noqa: F401 — registers manual_credit_spread
from blocks.stop.profiles import mean_reversion  # noqa: F401 — registers mean_reversion profile
from blocks.stop.profiles import iron_fly  # noqa: F401 — registers iron_fly_break_even_cut
from blocks.stop.profiles import iron_fly_xsp  # noqa: F401 — registers iron_fly_xsp_break_even_cut
from blocks.stop.profiles import drma_hm  # noqa: F401 — registers drma_hm_strategy
