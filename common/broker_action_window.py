"""Strategy-specific broker-action windows — separate from runtime session lifecycle.

runtime_session answers: should this process/trading loop keep running?
broker_action_window answers: may this strategy send broker orders right now?
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Dict, Optional, Tuple

from common.market_hours import trade_past_0dte_close


@dataclass(frozen=True)
class BrokerActionProfile:
    name: str
    allow_broker_actions_overnight: bool
    start_hour_ct: int = 8
    start_minute_ct: int = 30
    end_hour_ct: int = 15
    end_minute_ct: int = 0


MEIC_SPX_OPTIONS_RTH_ACTIONS = BrokerActionProfile(
    name='MEIC_SPX_OPTIONS_RTH_ACTIONS',
    allow_broker_actions_overnight=False,
    start_hour_ct=8,
    start_minute_ct=30,
    end_hour_ct=15,
    end_minute_ct=0,
)

FUTURES_OVERNIGHT_ACTIONS = BrokerActionProfile(
    name='FUTURES_OVERNIGHT_ACTIONS',
    allow_broker_actions_overnight=True,
)


def _within_rth_window(now: datetime, profile: BrokerActionProfile) -> bool:
    window_start = time(profile.start_hour_ct, profile.start_minute_ct)
    window_end = time(profile.end_hour_ct, profile.end_minute_ct)
    t = now.time()
    return window_start <= t < window_end


def broker_actions_allowed_for_trade(
    state: Dict[str, Any],
    now: Optional[datetime] = None,
    *,
    filename: str = '',
    profile: BrokerActionProfile = MEIC_SPX_OPTIONS_RTH_ACTIONS,
) -> Tuple[bool, str]:
    """Return whether broker actions are allowed for this trade right now.

    Priority:
    1. Expired option cutoff → (False, "expired_option")
    2. Overnight-capable profile → (True, "allowed")
    3. Inside RTH window → (True, "allowed")
    4. Outside RTH window → (False, "outside_meic_spx_broker_action_window")
    """
    from meic0dte.app.utilities import central_now

    now = now or central_now()

    if trade_past_0dte_close(state, filename, now=now):
        return False, 'expired_option'

    if profile.allow_broker_actions_overnight:
        return True, 'allowed'

    if _within_rth_window(now, profile):
        return True, 'allowed'

    return False, 'outside_meic_spx_broker_action_window'
