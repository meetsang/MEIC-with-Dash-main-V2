import sys
from datetime import datetime, time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# Modules that call is_after_market_close_ct() with no `now` and expect wall clock.
_MARKET_HOURS_TEST_MODULES = frozenset({
    'test_market_hours',
})

# StopMonitor tests that must exercise real expiry settlement behavior.
_EXPIRY_GATE_TEST_MODULES = frozenset({
    'test_expiry_gate',
    'test_stop_runner_expiry_gate',
    'test_stop_monitor_0dte_freeze',
})


def _is_after_market_close_for_tests(now: datetime | None = None) -> bool:
    """Default tests to before 3 PM CT; honor explicit `now=` (test_market_hours)."""
    from common.market_hours import MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT

    if now is None:
        return False
    close = time(MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT)
    return now.time() >= close


@pytest.fixture(autouse=True)
def _session_before_market_close(request):
    """Stop-monitor broker tests assume regular session; avoid 3 PM CT flake."""
    mod = getattr(request.node.module, '__name__', '')
    if mod in _MARKET_HOURS_TEST_MODULES:
        yield
        return
    with patch(
        'common.market_hours.is_after_market_close_ct',
        side_effect=_is_after_market_close_for_tests,
    ):
        yield


@pytest.fixture(autouse=True)
def _neutralize_stop_monitor_expiry_gate(request):
    """Avoid calendar drift closing StopMonitor fixtures with stale SPXW expiries."""
    mod = getattr(request.node.module, '__name__', '')
    if mod in _EXPIRY_GATE_TEST_MODULES:
        yield
        return
    with patch(
        'blocks.stop.monitor.try_settle_or_freeze_trade',
        side_effect=lambda state, **kwargs: ('ok', state),
    ), patch(
        'blocks.stop.monitor.trade_past_0dte_close',
        return_value=False,
    ), patch(
        'blocks.stop.monitor.broker_actions_allowed_for_trade',
        return_value=(True, 'allowed'),
    ):
        yield
