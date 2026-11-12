# Shared configuration parameters for trading holidays and FOMC days.
# Both MARKET_CLOSED and FOMC_DAY are auto-populated at startup.
# FOMC dates are scraped from the Federal Reserve website and cached locally.
# Format: date strings in 'yymmdd' format, e.g. '260613' = June 13 2026

import os, re, json
from datetime import datetime as _dt, timedelta, timezone


_CENTRAL_STD = -6
_CENTRAL_DST = -5


def _nth_weekday_of_month(year, month, weekday, n):
    first = _dt(year, month, 1)
    days_until = (weekday - first.weekday()) % 7
    return first + timedelta(days=days_until + 7 * (n - 1))


def _central_dst_bounds(year):
    dst_start = _nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    dst_end = _nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    return dst_start, dst_end


def _central_now_naive() -> _dt:
    utc = _dt.now(timezone.utc)
    dst_s, dst_e = _central_dst_bounds(utc.year)
    dst_s_utc = (dst_s - timedelta(hours=_CENTRAL_STD)).replace(tzinfo=timezone.utc)
    dst_e_utc = (dst_e - timedelta(hours=_CENTRAL_DST)).replace(tzinfo=timezone.utc)
    off = _CENTRAL_DST if dst_s_utc <= utc < dst_e_utc else _CENTRAL_STD
    return (utc + timedelta(hours=off)).replace(tzinfo=None)


def _central_today_year():
    return _central_now_naive().year


_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'fomc_cache.json')
_NYSE_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'nyse_calendar_cache.json')
_FED_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'

_HOLIDAYS_MEM_CACHE: dict[int, list[str]] = {}
_EARLY_CLOSES_MEM_CACHE: dict[int, dict[str, str]] = {}
_FOMC_MEM_CACHE: dict[tuple[int, str], list[str]] = {}
_TRADING_DAY_LOGGED_DATE: str | None = None


def _load_nyse_disk_cache(year: int) -> dict | None:
    if os.path.exists(_NYSE_CACHE_FILE):
        try:
            with open(_NYSE_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if str(year) in data:
                return data[str(year)]
        except Exception:
            pass
    return None


def _save_nyse_disk_cache(year: int, holidays: list[str], early_closes: dict[str, str]) -> None:
    try:
        data = {}
        if os.path.exists(_NYSE_CACHE_FILE):
            try:
                with open(_NYSE_CACHE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[str(year)] = {
            'holidays': holidays,
            'early_closes': early_closes,
        }
        with open(_NYSE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _get_nyse_holidays(year: int):
    if year in _HOLIDAYS_MEM_CACHE:
        return _HOLIDAYS_MEM_CACHE[year]

    cached = _load_nyse_disk_cache(year)
    if cached and 'holidays' in cached:
        _HOLIDAYS_MEM_CACHE[year] = cached['holidays']
        return cached['holidays']

    # Lazy-import pandas_market_calendars only when disk cache misses
    import pandas as _pd
    import pandas_market_calendars as mcal

    nyse = mcal.get_calendar('NYSE')
    start = f"{year}-01-01"
    end   = f"{year}-12-31"
    holidays = nyse.holidays().holidays
    res = [_pd.Timestamp(d).strftime('%y%m%d') for d in holidays
           if start <= _pd.Timestamp(d).strftime('%Y-%m-%d') <= end]
    _HOLIDAYS_MEM_CACHE[year] = res

    # Compute early closes to populate disk cache together
    early_closes = _get_nyse_early_closes(year)
    _save_nyse_disk_cache(year, res, early_closes)
    return res


def _get_nyse_early_closes(year: int) -> dict[str, str]:
    """Return mapping of yymmdd -> 'HH:MM' Central Time close for NYSE early-close (half) days."""
    if year in _EARLY_CLOSES_MEM_CACHE:
        return _EARLY_CLOSES_MEM_CACHE[year]

    cached = _load_nyse_disk_cache(year)
    if cached and 'early_closes' in cached:
        _EARLY_CLOSES_MEM_CACHE[year] = cached['early_closes']
        return cached['early_closes']

    # Lazy-import pandas_market_calendars only when disk cache misses
    import pandas as _pd
    import pandas_market_calendars as mcal

    out: dict[str, str] = {}
    try:
        nyse = mcal.get_calendar('NYSE')
        sched = nyse.schedule(start_date=f"{year}-01-01", end_date=f"{year}-12-31")
        early = nyse.early_closes(sched)
        for idx_ts, row in early.iterrows():
            yymmdd = _pd.Timestamp(idx_ts).strftime('%y%m%d')
            close_utc = _pd.Timestamp(row['market_close']).to_pydatetime()
            if close_utc.tzinfo is None:
                close_utc = close_utc.replace(tzinfo=timezone.utc)
            dst_s, dst_e = _central_dst_bounds(close_utc.year)
            dst_s_utc = (dst_s - timedelta(hours=_CENTRAL_STD)).replace(tzinfo=timezone.utc)
            dst_e_utc = (dst_e - timedelta(hours=_CENTRAL_DST)).replace(tzinfo=timezone.utc)
            off = _CENTRAL_DST if dst_s_utc <= close_utc < dst_e_utc else _CENTRAL_STD
            close_ct = (close_utc + timedelta(hours=off)).replace(tzinfo=None)
            out[yymmdd] = close_ct.strftime('%H:%M')
    except Exception:
        pass
    _EARLY_CLOSES_MEM_CACHE[year] = out
    return out


def _fetch_fomc_dates(year: int):
    """Scrape FOMC decision dates from the Fed website for a given year.
    The page uses panel divs; each meeting row has the month and day range.
    Decision date = last day of the two-day meeting.
    Falls back to local cache if the site is unreachable.
    Cached in memory per (year, central_date) to avoid repeated HTTP scraping.
    """
    today_key = _central_now_naive().strftime('%y%m%d')
    cache_key = (year, today_key)
    if cache_key in _FOMC_MEM_CACHE:
        return _FOMC_MEM_CACHE[cache_key]

    # Fast path: check disk cache before making network calls
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if str(year) in cache and cache[str(year)]:
                _FOMC_MEM_CACHE[cache_key] = cache[str(year)]
                return cache[str(year)]
    except Exception:
        pass

    import requests as _requests
    from bs4 import BeautifulSoup
    MONTHS = {'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
              'july':7,'august':8,'september':9,'october':10,'november':11,'december':12}

    try:
        resp = _requests.get(_FED_URL, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')

        dates = set()

        # Find the panel whose heading contains the target year
        for panel in soup.find_all('div', class_='panel'):
            heading_text = panel.get_text()
            if f'{year} FOMC' not in heading_text:
                continue

            # Method 1: completed meetings — PDF link contains the exact date
            pdf_pattern = rf'monetary({year}\d{{4}})[a-z]'
            for raw in re.findall(pdf_pattern, str(panel)):
                dates.add(_dt.strptime(raw, '%Y%m%d').strftime('%y%m%d'))

            # Method 2: future/scheduled meetings — parse month + day range rows
            # Page structure per meeting: <div class="row"> with month in one col, days in another
            for row in panel.find_all('div', class_='row'):
                row_text = row.get_text(' ', strip=True)
                # Match "March 18-19" or "April 28-29"
                m = re.search(
                    r'(January|February|March|April|May|June|July|'
                    r'August|September|October|November|December)\s+\d{1,2}[-–](\d{1,2})',
                    row_text, re.IGNORECASE
                )
                if m:
                    month = MONTHS[m.group(1).lower()]
                    day   = int(m.group(2))
                    try:
                        dates.add(_dt(year, month, day).strftime('%y%m%d'))
                    except ValueError:
                        pass

        if dates:
            result = sorted(dates)
            # Cache to disk
            try:
                cache = {}
                if os.path.exists(_CACHE_FILE):
                    with open(_CACHE_FILE) as f:
                        cache = json.load(f)
                cache[str(year)] = result
                with open(_CACHE_FILE, 'w') as f:
                    json.dump(cache, f, indent=2)
            except Exception:
                pass
            _FOMC_MEM_CACHE[cache_key] = result
            return result

    except Exception:
        pass

    # Fallback: use cached dates
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                cache = json.load(f)
            if str(year) in cache:
                _FOMC_MEM_CACHE[cache_key] = cache[str(year)]
                return cache[str(year)]
    except Exception:
        pass

    return []


def is_market_closed_today(now: _dt | None = None) -> bool:
    """True only for a real NYSE holiday. FOMC days are open — not included."""
    today = now or _central_now_naive()
    today_str = today.strftime('%y%m%d')
    market_closed = set(_get_nyse_holidays(today.year))
    return today_str in market_closed


def check_trading_day(now: _dt | None = None, logger=None) -> tuple[bool, str]:
    """Returns (should_trade, reason). Cached per day so polling threads do not spam logs or HTTP."""
    global _TRADING_DAY_LOGGED_DATE
    today = now or _central_now_naive()
    yesterday = today - timedelta(days=1)

    today_str = today.strftime('%y%m%d')
    today_fmt = today.strftime('%Y-%m-%d')

    if logger is not None and _TRADING_DAY_LOGGED_DATE != today_fmt:
        logger.info("Checking if %s is a trading day ...", today_fmt)
        _TRADING_DAY_LOGGED_DATE = today_fmt

    fomc_days = set(_fetch_fomc_dates(today.year))
    fomc_days |= set(_fetch_fomc_dates(yesterday.year))
    market_closed = set(_get_nyse_holidays(today.year))

    if today_str in market_closed:
        return False, f"NYSE is CLOSED today ({today_fmt}). No trading."
    if today_str in fomc_days:
        return False, f"Today ({today_fmt}) is a FOMC day. Skipping trading."
    early_closes = _get_nyse_early_closes(today.year)
    if today_str in early_closes:
        return True, f"{today_fmt} is an NYSE EARLY-CLOSE trading day (closes {early_closes[today_str]} CT). Proceeding."
    return True, f"{today_fmt} is a normal trading day. Proceeding."


_year = _central_today_year()
MARKET_CLOSED = _get_nyse_holidays(_year)
EARLY_CLOSES = _get_nyse_early_closes(_year)
FOMC_DAY = _fetch_fomc_dates(_year)
