# Shared configuration parameters for trading holidays and FOMC days.
# Both MARKET_CLOSED and FOMC_DAY are auto-populated at startup.
# FOMC dates are scraped from the Federal Reserve website and cached locally.
# Format: date strings in 'yymmdd' format, e.g. '260613' = June 13 2026

import os, re, json
import pandas_market_calendars as mcal
import pandas as _pd
import requests as _requests
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


def _central_today_year():
    utc = _dt.now(timezone.utc)
    dst_s, dst_e = _central_dst_bounds(utc.year)
    dst_s_utc = (dst_s - timedelta(hours=_CENTRAL_STD)).replace(tzinfo=timezone.utc)
    dst_e_utc = (dst_e - timedelta(hours=_CENTRAL_DST)).replace(tzinfo=timezone.utc)
    off = _CENTRAL_DST if dst_s_utc <= utc < dst_e_utc else _CENTRAL_STD
    return (utc + timedelta(hours=off)).year

_CACHE_FILE = os.path.join(os.path.dirname(__file__), 'fomc_cache.json')
_FED_URL = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'

def _get_nyse_holidays(year: int):
    nyse = mcal.get_calendar('NYSE')
    start = f"{year}-01-01"
    end   = f"{year}-12-31"
    holidays = nyse.holidays().holidays
    return [_pd.Timestamp(d).strftime('%y%m%d') for d in holidays
            if start <= _pd.Timestamp(d).strftime('%Y-%m-%d') <= end]


def _fetch_fomc_dates(year: int):
    """Scrape FOMC decision dates from the Fed website for a given year.
    The page uses panel divs; each meeting row has the month and day range.
    Decision date = last day of the two-day meeting.
    Falls back to local cache if the site is unreachable.
    """
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
            return result

    except Exception:
        pass

    # Fallback: use cached dates
    try:
        if os.path.exists(_CACHE_FILE):
            with open(_CACHE_FILE) as f:
                cache = json.load(f)
            if str(year) in cache:
                return cache[str(year)]
    except Exception:
        pass

    return []


_year = _central_today_year()
MARKET_CLOSED = _get_nyse_holidays(_year)
FOMC_DAY = _fetch_fomc_dates(_year)

