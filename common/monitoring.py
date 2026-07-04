"""Health and performance monitoring (ported from spx-bot)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List


@dataclass
class AlertConfig:
    enabled: bool = False


class HealthChecker:
    def __init__(self, name: str):
        self.name = name
        self._checks: Dict[str, tuple] = {}
        self._last_heartbeat = time.time()

    def register_check(self, name: str, fn: Callable[[], bool], description: str = '') -> None:
        self._checks[name] = (fn, description)

    def heartbeat(self) -> None:
        self._last_heartbeat = time.time()

    def run_health_checks(self) -> Dict[str, bool]:
        return {name: fn() for name, (fn, _) in self._checks.items()}


class PerformanceMonitor:
    def __init__(self, name: str, alert_config: AlertConfig = None):
        self.name = name
        self.alert_config = alert_config or AlertConfig()
        self._pnl_history: List[float] = []

    def update_pnl(self, pnl: float) -> None:
        self._pnl_history.append(pnl)

    def get_performance_summary(self) -> Dict:
        if not self._pnl_history:
            return {'total_pnl': 0.0, 'samples': 0}
        return {
            'total_pnl': self._pnl_history[-1],
            'samples': len(self._pnl_history),
        }
