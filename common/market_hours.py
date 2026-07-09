"""Central-time market close gates — 0DTE trades only after 3:00 PM CT."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Dict, Optional

from common import trades_layout

# Match run.py STREAM_STOP_HOUR / streamer 3 PM CT shutdown.
MARKET_CLOSE_HOUR_CT = 15
MARKET_CLOSE_MINUTE_CT = 0


def is_after_market_close_ct(now: Optional[datetime] = None) -> bool:
    """True when Central wall clock is at or past the regular session close."""
    from meic0dte.app.utilities import central_now

    now = now or central_now()
    close = time(MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT)
    return now.time() >= close


def _parse_expiry_yymmdd(raw: str) -> Optional[date]:
    text = (raw or '').strip()
    if not text:
        return None
    for fmt, width in (('%y%m%d', 6), ('%Y-%m-%d', 10), ('%Y%m%d', 8)):
        try:
            return datetime.strptime(text[:width], fmt).date()
        except ValueError:
            continue
    return None


def trade_expiry_on_or_before_today(
    state: Dict[str, Any],
    filename: str = '',
    *,
    today: Optional[date] = None,
) -> bool:
    """True when option expiry is today or earlier (0DTE on session day)."""
    from common.session_cleanup import trade_expiry_date
    from meic0dte.app.utilities import central_date

    expiry = trade_expiry_date(state, filename)
    if expiry is None:
        return False
    today = today or central_date()
    return expiry <= today


def session_row_is_0dte(row, *, strategy: str) -> bool:
    """True when a session CSV row targets same-day (or earlier) expiry."""
    from meic0dte.app.utilities import central_date

    if strategy == trades_layout.STRATEGY_MEIC:
        return True
    expiry = _parse_expiry_yymmdd(getattr(row, 'expiry', '') or '')
    if expiry is None:
        return False
    return expiry <= central_date()


def trade_past_0dte_close(
    state: Dict[str, Any],
    filename: str = '',
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Block broker placement: expired prior-day options or same-day after close."""
    from common.session_cleanup import trade_expiry_date
    from meic0dte.app.utilities import central_now

    now = now or central_now()
    expiry = trade_expiry_date(state, filename)
    if expiry is None:
        return False

    today = now.date()
    if expiry > today:
        return False
    if expiry < today:
        return True

    close = time(MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT)
    return now.time() >= close


def session_row_past_0dte_close(row, *, strategy: str, now: Optional[datetime] = None) -> bool:
    return session_row_is_0dte(row, strategy=strategy) and is_after_market_close_ct(now)
