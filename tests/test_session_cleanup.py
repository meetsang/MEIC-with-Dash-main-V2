"""Session cleanup expiry rules and archiving."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from common.session_cleanup import (
    archive_active_trades,
    should_archive_expiry,
    trade_expiry_date,
)


def test_should_archive_morning_only_prior_expiry():
    today = date(2026, 6, 24)
    assert should_archive_expiry(date(2026, 6, 23), today, 'morning') is True
    assert should_archive_expiry(date(2026, 6, 24), today, 'morning') is False
    assert should_archive_expiry(date(2026, 6, 25), today, 'morning') is False


def test_should_archive_eod_never_archives():
    """EOD sync does not move active/ files — archiving is morning-only."""
    today = date(2026, 6, 24)
    assert should_archive_expiry(date(2026, 6, 23), today, 'eod') is False
    assert should_archive_expiry(date(2026, 6, 24), today, 'eod') is False
    assert should_archive_expiry(date(2026, 6, 25), today, 'eod') is False
    assert should_archive_expiry(None, today, 'eod') is False


def test_trade_expiry_from_symbol():
    state = {
        'short_leg': {'symbol': '.SPXW260624P7230'},
        'long_leg': {'symbol': '.SPXW260624P7205'},
    }
    assert trade_expiry_date(state) == date(2026, 6, 24)


def test_archive_active_respects_future_expiry(tmp_path):
    active = tmp_path / 'active'
    history = tmp_path / 'history'
    active.mkdir()
    today = date(2026, 6, 24)
    past = {
        'short_leg': {'symbol': '.SPXW260623P7200'},
        'long_leg': {'symbol': '.SPXW260623P7175'},
    }
    future = {
        'short_leg': {'symbol': '.SPXW260625P7200'},
        'long_leg': {'symbol': '.SPXW260625P7175'},
    }
    (active / 'past.json').write_text(json.dumps(past), encoding='utf-8')
    (active / 'future.json').write_text(json.dumps(future), encoding='utf-8')

    kept, archived, _ = archive_active_trades(str(active), str(history), today, 'morning')
    assert archived == 1
    assert kept == 1
    assert not (active / 'past.json').exists()
    assert (active / 'future.json').exists()
    assert (history / '2026-06-24' / 'past.json').exists()

    kept2, archived2, _ = archive_active_trades(str(active), str(history), today, 'eod')
    assert archived2 == 0
    assert kept2 == 1
    assert (active / 'future.json').exists()

    today_trade = {
        'short_leg': {'symbol': '.SPXW260624P7230'},
        'long_leg': {'symbol': '.SPXW260624P7205'},
    }
    (active / 'today.json').write_text(json.dumps(today_trade), encoding='utf-8')
    kept3, archived3, _ = archive_active_trades(str(active), str(history), today, 'eod')
    assert archived3 == 0
    assert kept3 == 2
    assert (active / 'today.json').exists()
    assert (active / 'future.json').exists()
