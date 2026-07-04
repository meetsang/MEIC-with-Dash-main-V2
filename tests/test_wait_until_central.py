"""Overnight / weekend wait must use wall clock, not one long sleep."""
from datetime import datetime, timedelta
from unittest.mock import patch

import run as launcher


def test_wait_until_central_returns_when_wall_clock_passes_target():
    start = datetime(2026, 7, 1, 6, 0, 0)
    target = datetime(2026, 7, 1, 8, 20, 0)
    clock = {'now': start}

    def fake_central_now():
        return clock['now']

    with patch.object(launcher, '_central_now', side_effect=fake_central_now):
        with patch.object(launcher.time, 'sleep', side_effect=lambda _: clock.update(now=target)):
            launcher.wait_until_central(target)


def test_wait_until_central_resumes_after_simulated_os_suspend():
    """After OS suspend jumps wall clock past target, return on next poll (not hours later)."""
    target = datetime(2026, 7, 1, 8, 20, 0)
    times = [
        datetime(2026, 7, 1, 6, 0, 0),
        datetime(2026, 7, 1, 11, 1, 51),
    ]

    with patch.object(launcher, '_central_now', side_effect=times):
        with patch.object(launcher.time, 'sleep') as mock_sleep:
            launcher.wait_until_central(target)
            assert mock_sleep.call_count <= 1
            if mock_sleep.call_count:
                assert mock_sleep.call_args[0][0] <= 30
