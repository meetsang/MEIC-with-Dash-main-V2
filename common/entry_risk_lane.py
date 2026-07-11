"""Serialize place → persist → first status confirm for opening orders."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_LOCK = threading.Lock()


@contextmanager
def opening_risk_critical_section(*, timeout_sec: float = 120.0) -> Iterator[None]:
    acquired = _LOCK.acquire(timeout=timeout_sec)
    if not acquired:
        raise TimeoutError('opening risk lane busy')
    try:
        yield
    finally:
        _LOCK.release()
