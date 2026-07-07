"""Process singleton locks under runtime/locks/."""
from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LOCKS_DIR = os.path.join(ROOT, 'runtime', 'locks')


@dataclass
class LockInfo:
    name: str
    pid: int
    alive: bool
    path: str
    meta: Dict[str, Any]


def _lock_path(name: str) -> str:
    safe = name.replace(os.sep, '_')
    return os.path.join(LOCKS_DIR, f'{safe}.lock')


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def active_lock_pid(name: str) -> Optional[int]:
    """Return PID holding lock if alive, else None."""
    meta = read_lock(name)
    if not meta:
        return None
    pid = int(meta.get('pid') or 0)
    if pid and _pid_alive(pid):
        return pid
    return None


def read_lock(name: str) -> Optional[Dict[str, Any]]:
    path = _lock_path(name)
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_locks() -> List[LockInfo]:
    out: List[LockInfo] = []
    if not os.path.isdir(LOCKS_DIR):
        return out
    for fname in os.listdir(LOCKS_DIR):
        if not fname.endswith('.lock'):
            continue
        name = fname[:-5]
        path = os.path.join(LOCKS_DIR, fname)
        meta = read_lock(name) or {}
        pid = int(meta.get('pid') or 0)
        out.append(LockInfo(name=name, pid=pid, alive=_pid_alive(pid), path=path, meta=meta))
    return out


def acquire_lock(
    name: str,
    *,
    command: str = '',
    paper: Optional[bool] = None,
    extra: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> bool:
    """
    Acquire singleton lock. Returns True if acquired.
    If another live PID holds the lock, returns False unless force=True.
    """
    os.makedirs(LOCKS_DIR, exist_ok=True)
    existing = read_lock(name)
    if existing and not force:
        pid = int(existing.get('pid') or 0)
        if pid == os.getpid():
            log.warning('Process lock %s already held by this PID — already_running', name)
            return False
        if _pid_alive(pid):
            log.warning(
                'Process lock %s held by live PID %s — already_running',
                name,
                pid,
            )
            return False

    payload = {
        'pid': os.getpid(),
        'name': name,
        'command': command or ' '.join(sys.argv),
        'paper': paper,
        'ts': time.time(),
    }
    if extra:
        payload.update(extra)
    path = _lock_path(name)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    log.info('Process lock acquired %s pid=%s', name, os.getpid())
    return True


def release_lock(name: str) -> None:
    path = _lock_path(name)
    data = read_lock(name)
    if data and int(data.get('pid') or 0) not in (0, os.getpid()):
        return
    try:
        os.remove(path)
        log.info('Process lock released %s', name)
    except OSError:
        pass


@contextmanager
def process_lock(
    name: str,
    *,
    command: str = '',
    paper: Optional[bool] = None,
    extra: Optional[Dict[str, Any]] = None,
    exit_on_conflict: bool = True,
) -> Iterator[bool]:
    """
    Context manager for singleton lock.
    Yields True if lock acquired; if exit_on_conflict and not acquired, raises SystemExit.
    """
    acquired = acquire_lock(name, command=command, paper=paper, extra=extra)
    if not acquired and exit_on_conflict:
        raise SystemExit(f'{name} already running — exiting to avoid duplicate broker traffic')
    try:
        if acquired:
            atexit.register(release_lock, name)
        yield acquired
    finally:
        if acquired:
            release_lock(name)
            try:
                atexit.unregister(release_lock, name)
            except Exception:
                pass
