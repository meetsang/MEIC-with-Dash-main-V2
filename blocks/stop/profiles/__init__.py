"""Registered stop profiles."""
from blocks.stop.profiles import hold_to_expiry  # noqa: F401 — registers hold_to_expiry + skyline
from blocks.stop.profiles import meic  # noqa: F401 — registers meic_credit_spread + legacy alias
from blocks.stop.profiles import meic_profit_target  # noqa: F401 — opt-in profit target profile
