"""Atomic JSON/CSV helpers and cross-process file locks."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, Iterator, Optional


@contextmanager
def interprocess_file_lock(path: str, *, timeout_sec: float = 10.0) -> Iterator[None]:
    """Exclusive cross-process lock (msvcrt on Windows, fcntl elsewhere)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fh = open(path, 'a+b')
    deadline = time.time() + max(0.5, timeout_sec)
    locked = False
    try:
        while True:
            try:
                if sys.platform == 'win32':
                    import msvcrt

                    fh.seek(0)
                    if fh.read(1) == b'':
                        fh.write(b'\0')
                        fh.flush()
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f'interprocess file lock timeout: {path}')
                time.sleep(0.02)
        yield
    finally:
        if locked:
            try:
                if sys.platform == 'win32':
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def save_json_safe(path: str, data: Dict[str, Any], *, retries: int = 8, delay: float = 0.05) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == retries - 1:
                    raise
                time.sleep(delay * (attempt + 1))
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def load_json_safe(path: str, *, retries: int = 8, delay: float = 0.05) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    for attempt in range(retries):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (PermissionError, json.JSONDecodeError):
            if attempt == retries - 1:
                return None
            time.sleep(delay * (attempt + 1))
    return None
