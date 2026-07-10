"""Shared fixtures for fill-sync / stop-monitor tests (non-expired SPXW symbols)."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from meic0dte.app.utilities import central_date


def today_expiry_yymmdd() -> str:
    """Central Time same-day SPXW expiration code (yymmdd)."""
    return central_date().strftime('%y%m%d')


def spxw_option_symbol(strike: int, side: str, *, expiry_yymmdd: str | None = None) -> str:
    """Build a Tastytrade-style .SPXW option symbol for tests."""
    expiry = expiry_yymmdd or today_expiry_yymmdd()
    letter = 'P' if side.upper() == 'P' else 'C'
    return f'.SPXW{expiry}{letter}0{int(strike):04d}000'


@contextmanager
def same_day_trade_env() -> Iterator[str]:
    """
    Use today's Central expiry for symbols and skip expiry settlement in StopMonitor.

    Restores any prior MEIC_EXPIRY override after the test.
    """
    expiry_iso = central_date().strftime('%Y-%m-%d')
    prior = os.environ.get('MEIC_EXPIRY')
    os.environ['MEIC_EXPIRY'] = expiry_iso
    gate = patch(
        'blocks.stop.monitor.try_settle_or_freeze_trade',
        side_effect=lambda state, **kwargs: ('ok', state),
    )
    window = patch(
        'blocks.stop.monitor.broker_actions_allowed_for_trade',
        return_value=(True, 'allowed'),
    )
    try:
        with gate, window:
            yield today_expiry_yymmdd()
    finally:
        if prior is None:
            os.environ.pop('MEIC_EXPIRY', None)
        else:
            os.environ['MEIC_EXPIRY'] = prior
