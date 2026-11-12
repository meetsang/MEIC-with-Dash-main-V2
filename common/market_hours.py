"""Central-time market close gates — 0DTE trades and NYSE early-close (half-day) schedule."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import os
from typing import Any, Dict, Optional, Union

from common import trades_layout

# Match run.py STREAM_START_HOUR / STREAM_STOP_HOUR (8:30 AM - 3:00 PM CT normal cash RTH).
MARKET_OPEN_HOUR_CT = 8
MARKET_OPEN_MINUTE_CT = 30
MARKET_CLOSE_HOUR_CT = 15
MARKET_CLOSE_MINUTE_CT = 0


def _resolve_date(for_date: Optional[Union[date, datetime]] = None) -> date:
    if isinstance(for_date, datetime):
        return for_date.date()
    if isinstance(for_date, date):
        return for_date
    from meic0dte.app.utilities import central_date

    return central_date()


def is_early_close_day_ct(for_date: Optional[Union[date, datetime]] = None) -> bool:
    """True when NYSE has an early close (e.g. 12:00 PM CT / 1:00 PM ET) on for_date."""
    d = _resolve_date(for_date)
    try:
        from common.config import _get_nyse_early_closes

        early = _get_nyse_early_closes(d.year)
        return d.strftime('%y%m%d') in early
    except Exception:
        return False


def market_close_time_ct(for_date: Optional[Union[date, datetime]] = None) -> time:
    """Return cash RTH market close in Central Time (12:00 on NYSE half-days, else 15:00)."""
    d = _resolve_date(for_date)
    try:
        from common.config import _get_nyse_early_closes

        early = _get_nyse_early_closes(d.year)
        hhmm = early.get(d.strftime('%y%m%d'))
        if hhmm:
            parts = hhmm.split(':')
            return time(int(parts[0]), int(parts[1]))
    except Exception:
        pass
    return time(MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT)


def early_close_shift_minutes(for_date: Optional[Union[date, datetime]] = None) -> int:
    """Minutes earlier than normal 15:00 CT close (e.g. 180 on a 12:00 CT early-close day, else 0)."""
    close_t = market_close_time_ct(for_date)
    normal_mins = MARKET_CLOSE_HOUR_CT * 60 + MARKET_CLOSE_MINUTE_CT
    actual_mins = close_t.hour * 60 + close_t.minute
    return max(0, normal_mins - actual_mins)


def effective_session_time_ct(
    configured: Union[str, time],
    for_date: Optional[Union[date, datetime]] = None,
    *,
    default: time = time(14, 50),
) -> time:
    """Adjust an afternoon cutoff/exit time (e.g. 14:50 EOD flatten) earlier on NYSE half-days.

    Example:
      - Normal day (close 15:00 CT): '14:50' -> 14:50 CT; '14:45' -> 14:45 CT
      - Early-close day (close 12:00 CT, shift=180m): '14:50' -> 11:50 CT; '14:45' -> 11:45 CT
    """
    if isinstance(configured, time):
        base_t = configured
    else:
        raw = str(configured or '').strip()
        try:
            if ':' in raw:
                parts = raw.split(':')
                base_t = time(int(parts[0]), int(parts[1]), int(parts[2]) if len(parts) > 2 else 0)
            else:
                digits = ''.join(ch for ch in raw if ch.isdigit())
                base_t = time(int(digits[:2]), int(digits[2:4])) if len(digits) >= 4 else default
        except Exception:
            base_t = default

    shift_min = early_close_shift_minutes(for_date)
    if shift_min <= 0:
        return base_t

    base_mins = base_t.hour * 60 + base_t.minute
    # Only shift afternoon cutoffs (>= 11:30 CT) that are relative to the afternoon close
    if base_mins >= 11 * 60 + 30:
        shifted_mins = max(9 * 60, base_mins - shift_min)
        return time(shifted_mins // 60, shifted_mins % 60, base_t.second)
    return base_t


def effective_session_time_str_ct(
    configured_hhmm: str,
    for_date: Optional[Union[date, datetime]] = None,
    *,
    default: Union[str, time] = '14:50',
) -> str:
    """String ('HH:MM') wrapper around effective_session_time_ct."""
    if isinstance(default, time):
        def_t = default
    else:
        def_parts = str(default).split(':')
        def_t = time(int(def_parts[0]), int(def_parts[1]))
    eff = effective_session_time_ct(configured_hhmm, for_date, default=def_t)
    return f'{eff.hour:02d}:{eff.minute:02d}'


def is_cash_session_open_ct(now: Optional[datetime] = None) -> bool:
    """True when cash index (SPX/XSP) Regular Trading Hours are open (08:30–close CT).

    Outside this window only futures (/ES, /NQ) trade overnight; cash SPX does not tick.
    Returns True when MEIC_INTEGRATION=1 or MEIC_FORCE_TRADE=1 for off-hours integration tests.
    """
    if os.environ.get('MEIC_INTEGRATION') == '1' or os.environ.get('MEIC_FORCE_TRADE') == '1':
        return True
    from meic0dte.app.utilities import central_now

    now = now or central_now()
    if now.weekday() >= 5:
        return False
    open_t = time(MARKET_OPEN_HOUR_CT, MARKET_OPEN_MINUTE_CT)
    close_t = market_close_time_ct(now.date())
    return open_t <= now.time() < close_t


def is_after_market_close_ct(now: Optional[datetime] = None) -> bool:
    """True when Central wall clock is at or past the regular session close (12:00 on half-days, else 15:00)."""
    from meic0dte.app.utilities import central_now

    now = now or central_now()
    close = market_close_time_ct(now.date())
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

    close = market_close_time_ct(today)
    return now.time() >= close


def session_row_past_0dte_close(row, *, strategy: str, now: Optional[datetime] = None) -> bool:
    return session_row_is_0dte(row, strategy=strategy) and is_after_market_close_ct(now)
