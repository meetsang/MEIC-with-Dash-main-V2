"""V3 supervisor tuning via environment."""
from __future__ import annotations

import os


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    return float(raw)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    return int(raw)


TARGET_CYCLE_SEC = _float_env('TARGET_CYCLE_SEC', 0.25)
STOP_MAX_EXIT_JOBS = _int_env('STOP_MAX_EXIT_JOBS', 12)
STOP_BROKER_LANE_SIZE = _int_env('STOP_BROKER_LANE_SIZE', 6)
STOP_EXIT_STALL_SEC = _int_env('STOP_EXIT_STALL_SEC', 120)
MANUAL_KILL_EMERGENCY_OFFSET = _float_env('MANUAL_KILL_EMERGENCY_OFFSET', 0.50)
