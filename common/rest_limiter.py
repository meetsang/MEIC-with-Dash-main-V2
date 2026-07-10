"""Token-bucket REST rate limiter for Tasty broker calls."""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque, Optional

REST_MAX_PER_SEC = float(os.environ.get('TT_REST_MAX_PER_SEC', '1'))
REST_BURST = int(os.environ.get('TT_REST_BURST', '3'))


class RestLimiter:
  """Serialize and cap broker REST call rate (per process)."""

  def __init__(self, max_per_sec: float = REST_MAX_PER_SEC, burst: int = REST_BURST):
    self._max_per_sec = max(0.1, max_per_sec)
    self._burst = max(1, burst)
    self._lock = threading.Lock()
    self._timestamps: Deque[float] = deque()
    self._calls_1m: Deque[float] = deque()
    self._calls_5m: Deque[float] = deque()
    self._total = 0

  def acquire(self, *, priority: str = 'NORMAL', name: str = '') -> bool:
    """Block until a token is available. Returns False if skipped by caller."""
    from common.rest_metrics import record_call

    with self._lock:
      now = time.time()
      self._evict_old(now)
      while len(self._timestamps) >= self._burst:
        oldest = self._timestamps[0]
        wait = (1.0 / self._max_per_sec) - (now - oldest)
        if wait > 0:
          self._lock.release()
          try:
            time.sleep(wait)
          finally:
            self._lock.acquire()
          now = time.time()
          self._evict_old(now)
        else:
          self._timestamps.popleft()
      self._timestamps.append(now)
      self._calls_1m.append(now)
      self._calls_5m.append(now)
      self._total += 1
    try:
      record_call(name or 'broker', priority)
    except Exception:
      pass
    return True

  def _evict_old(self, now: float) -> None:
    while self._timestamps and now - self._timestamps[0] > 1.0:
      self._timestamps.popleft()
    while self._calls_1m and now - self._calls_1m[0] > 60.0:
      self._calls_1m.popleft()
    while self._calls_5m and now - self._calls_5m[0] > 300.0:
      self._calls_5m.popleft()

  def stats(self) -> dict:
    from common.rest_metrics import metrics_snapshot

    with self._lock:
      now = time.time()
      self._evict_old(now)
      base = {
        'total_calls': self._total,
        'calls_last_1m': len(self._calls_1m),
        'calls_last_5m': len(self._calls_5m),
        'max_per_sec': self._max_per_sec,
        'burst': self._burst,
      }
    try:
      base['metrics'] = metrics_snapshot()
    except Exception:
      pass
    return base


_global_limiter: Optional[RestLimiter] = None
_global_lock = threading.Lock()


def get_rest_limiter() -> RestLimiter:
  global _global_limiter
  with _global_lock:
    if _global_limiter is None:
      _global_limiter = RestLimiter()
    return _global_limiter


def reset_rest_limiter() -> None:
  global _global_limiter
  with _global_lock:
    _global_limiter = None
