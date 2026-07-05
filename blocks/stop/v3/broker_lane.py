"""Cross-trade broker concurrency with per-trade ordering (V3 §7.2)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Callable, Iterator, TypeVar

from blocks.stop.v3 import config as v3_config

T = TypeVar('T')


class BrokerLane:
    """Global semaphore + per-trade lock — serializes steps within one trade."""

    def __init__(self, max_concurrent: int | None = None):
        n = max_concurrent if max_concurrent is not None else v3_config.STOP_BROKER_LANE_SIZE
        self._max = max(1, n)
        self._global = threading.Semaphore(self._max)
        self._trade_locks: dict[str, threading.Lock] = {}
        self._meta = threading.Lock()
        self._in_flight = 0

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def in_flight(self) -> int:
        with self._meta:
            return self._in_flight

    def _trade_lock(self, trade_id: str) -> threading.Lock:
        with self._meta:
            if trade_id not in self._trade_locks:
                self._trade_locks[trade_id] = threading.Lock()
            return self._trade_locks[trade_id]

    @contextmanager
    def trade_pipeline(self, trade_id: str) -> Iterator[None]:
        lock = self._trade_lock(trade_id)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def run(self, trade_id: str, fn: Callable[[], T]) -> T:
        with self.trade_pipeline(trade_id):
            self._global.acquire()
            with self._meta:
                self._in_flight += 1
            try:
                return fn()
            finally:
                with self._meta:
                    self._in_flight = max(0, self._in_flight - 1)
                self._global.release()
