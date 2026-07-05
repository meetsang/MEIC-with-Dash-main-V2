"""Exit worker pool — one active job per trade path (V3 §6.3)."""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Callable, Dict, Optional, Set

from blocks.stop.v3 import config as v3_config
from blocks.stop.v3.observability import log_exit_event
from blocks.stop.v3.trade_slot import TradeSlot

log = logging.getLogger(__name__)


class ExitWorkerPool:
    """One active exit job per trade path; concurrency capped by semaphore.

    Note: manual-kill jobs start immediately like other jobs. The semaphore
    bounds concurrent broker work; there is no separate priority queue yet
    (see changes/STOP_MONITOR_V3_REVIEW_FIXES.md §T-4).
    """

    def __init__(self, max_jobs: Optional[int] = None):
        self._max = max_jobs if max_jobs is not None else v3_config.STOP_MAX_EXIT_JOBS
        self._lock = threading.Lock()
        self._active: Dict[str, str] = {}  # path -> job_id
        self._sem = threading.Semaphore(self._max)

    @property
    def active_paths(self) -> Set[str]:
        with self._lock:
            return set(self._active.keys())

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._active)

    def has_job(self, path: str) -> bool:
        with self._lock:
            return path in self._active

    def submit_manual_kill(self, slot: TradeSlot, fn: Callable[[], None]) -> bool:
        return self._submit(slot.path, fn, priority=True, job_kind='manual_kill')

    def submit(self, slot: TradeSlot, fn: Callable[[], None], *, job_kind: str = 'exit') -> bool:
        return self._submit(slot.path, fn, priority=False, job_kind=job_kind)

    def _submit(
        self,
        path: str,
        fn: Callable[[], None],
        *,
        priority: bool,
        job_kind: str,
    ) -> bool:
        with self._lock:
            if path in self._active:
                log.info('exit_duplicate_ignored path=%s kind=%s', path, job_kind)
                return False
            job_id = uuid.uuid4().hex[:12]
            self._active[path] = job_id

        def _runner() -> None:
            with self._sem:
                try:
                    fn()
                except Exception:
                    log.exception('Exit worker failed path=%s kind=%s', path, job_kind)
                finally:
                    with self._lock:
                        self._active.pop(path, None)

        t = threading.Thread(
            target=_runner,
            name=f'exit-{job_kind}-{job_id}',
            daemon=True,
        )
        t.start()
        log.info('Exit job started path=%s kind=%s job=%s', path, job_kind, job_id)
        log_exit_event(
            path=path,
            handler=job_kind,
            step='job_started',
            queue_depth=len(self._active),
        )
        return True
