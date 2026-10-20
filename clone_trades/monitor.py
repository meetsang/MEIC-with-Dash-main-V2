"""Background monitor thread updating quotes and ratcheting unbalanced OCO ladders."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from clone_trades.engine import CloneTradeEngine, get_engine

log = logging.getLogger("clone_trades.monitor")


class CloneTradeMonitor:
    """Runs a background loop checking quotes against stop loss and profit targets."""

    def __init__(self, engine: Optional[CloneTradeEngine] = None, interval_sec: float = 1.5):
        self.engine = engine or get_engine()
        self.interval_sec = interval_sec
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running and self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="CloneTradeMonitor", daemon=True)
        self._thread.start()
        log.info("CloneTradeMonitor started with interval=%.1fs", self.interval_sec)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        log.info("CloneTradeMonitor stopped")

    def _loop(self) -> None:
        while self._running:
            try:
                self.engine.update_quotes_and_check_stops()
            except Exception as exc:
                log.error("Error in CloneTradeMonitor loop: %s", exc, exc_info=True)
            time.sleep(self.interval_sec)


_GLOBAL_MONITOR: Optional[CloneTradeMonitor] = None
_MONITOR_LOCK = threading.Lock()


def get_monitor(engine: Optional[CloneTradeEngine] = None) -> CloneTradeMonitor:
    global _GLOBAL_MONITOR
    with _MONITOR_LOCK:
        if _GLOBAL_MONITOR is None:
            _GLOBAL_MONITOR = CloneTradeMonitor(engine=engine)
        return _GLOBAL_MONITOR
