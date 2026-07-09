"""Strategy/runtime session shutdown profiles — not a global platform rule.

MEIC SPX 0DTE daytime trading stops at regular cash close and does not run
overnight. Futures or other overnight-capable runtimes use a different profile
and rely on their own session calendars.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

from common.market_hours import MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT


@dataclass(frozen=True)
class RuntimeSessionProfile:
    name: str
    allow_overnight: bool
    close_hour: int = MARKET_CLOSE_HOUR_CT
    close_minute: int = MARKET_CLOSE_MINUTE_CT


MEIC_SPX_0DTE = RuntimeSessionProfile(
    name='MEIC_SPX_0DTE',
    allow_overnight=False,
    close_hour=MARKET_CLOSE_HOUR_CT,
    close_minute=MARKET_CLOSE_MINUTE_CT,
)

FUTURES_OVERNIGHT = RuntimeSessionProfile(
    name='FUTURES_OVERNIGHT',
    allow_overnight=True,
)


def runtime_should_stop_for_session(
    session_start: datetime,
    now: Optional[datetime] = None,
    *,
    profile: RuntimeSessionProfile = MEIC_SPX_0DTE,
) -> bool:
    """True when this runtime profile should end its trading session.

    Overnight-capable profiles never stop merely because clock is past cash close.
    MEIC SPX 0DTE stops when:
      - session started at/after regular close (no daytime trading left), or
      - session started before close and clock reached close same day, or
      - calendar date rolled after a pre-close session start.
    """
    if profile.allow_overnight:
        return False

    if now is None:
        from meic0dte.app.utilities import central_now
        now = central_now()

    close = time(profile.close_hour, profile.close_minute)

    if session_start.time() >= close:
        return True

    if now.date() > session_start.date():
        return True

    return now.time() >= close
