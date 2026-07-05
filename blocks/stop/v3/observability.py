"""Structured V3 exit logging (design §12.4)."""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)


def _trade_label(path: str) -> str:
    return os.path.basename(path)


def log_exit_event(
    *,
    path: str,
    handler: str,
    step: str,
    wait_ms: Optional[float] = None,
    queue_depth: Optional[int] = None,
    **extra: Any,
) -> None:
    payload = {
        'trade': _trade_label(path),
        'handler': handler,
        'step': step,
    }
    if wait_ms is not None:
        payload['wait_ms'] = round(wait_ms, 1)
    if queue_depth is not None:
        payload['queue_depth'] = queue_depth
    if extra:
        payload.update(extra)
    log.info('v3_exit %s', json.dumps(payload, default=str))


@contextmanager
def timed_exit_step(
    *,
    path: str,
    handler: str,
    step: str,
    queue_depth: Optional[int] = None,
    **extra: Any,
) -> Iterator[None]:
    t0 = time.perf_counter()
    try:
        yield
    finally:
        wait_ms = (time.perf_counter() - t0) * 1000.0
        log_exit_event(
            path=path,
            handler=handler,
            step=step,
            wait_ms=wait_ms,
            queue_depth=queue_depth,
            **extra,
        )
