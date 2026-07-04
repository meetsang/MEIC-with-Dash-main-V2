"""Timestamped session logs under project logs/ — one file per process start."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

LOGS_DIR_NAME = "logs"
LAUNCHER_BASE = "launcher"
STREAM_TT_BASE = "stream_pub_tt"
STREAM_SCHWAB_BASE = "stream_pub"

_LOG_LINE_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})"
)


def logs_dir(root: str | os.PathLike) -> Path:
    path = Path(root) / LOGS_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def session_stamp(when: datetime | None = None) -> str:
    """Filename-safe stamp from session start (Central wall time when provided)."""
    when = when or datetime.now()
    return when.strftime("%Y-%m-%d_%H%M%S")


def legacy_log_path(root: str | os.PathLike, base_name: str) -> Path | None:
    """Previous flat log locations before logs/{base}_{stamp}.log layout."""
    root = Path(root)
    if base_name == LAUNCHER_BASE:
        return root / "launcher.log"
    if base_name == STREAM_TT_BASE:
        return root / "stream_pub_tt.log"
    if base_name == STREAM_SCHWAB_BASE:
        return root / "streaming" / "stream_pub.log"
    return None


def new_session_log_path(
    root: str | os.PathLike,
    base_name: str,
    *,
    when: datetime | None = None,
) -> Path:
    """Return logs/{base_name}_{YYYY-MM-DD_HHMMSS}.log and ensure logs/ exists."""
    return logs_dir(root) / f"{base_name}_{session_stamp(when)}.log"


def _stamp_from_log_file(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                match = _LOG_LINE_TS.match(line.strip())
                if match:
                    date_part, hh, mm, ss = match.groups()
                    return f"{date_part}_{hh}{mm}{ss}"
    except OSError:
        pass
    return None


def relocate_legacy_log(
    root: str | os.PathLike,
    base_name: str,
    *,
    legacy_path: str | os.PathLike | None = None,
) -> Path | None:
    """Move a legacy flat log file into logs/ if it still exists at the old path."""
    src = Path(legacy_path) if legacy_path else legacy_log_path(root, base_name)
    if src is None or not src.is_file() or src.stat().st_size == 0:
        return None

    stamp = _stamp_from_log_file(src) or session_stamp(
        datetime.fromtimestamp(src.stat().st_mtime)
    )
    dest = logs_dir(root) / f"{base_name}_{stamp}.log"
    if dest.exists():
        stem = f"{base_name}_{stamp}"
        n = 2
        while dest.exists():
            dest = logs_dir(root) / f"{stem}_legacy{n}.log"
            n += 1

    shutil.move(str(src), str(dest))
    return dest


def relocate_all_legacy_logs(root: str | os.PathLike) -> list[Path]:
    """Move any pre-migration launcher/stream logs from old paths into logs/."""
    moved: list[Path] = []
    for base in (LAUNCHER_BASE, STREAM_TT_BASE, STREAM_SCHWAB_BASE):
        dest = relocate_legacy_log(root, base)
        if dest is not None:
            moved.append(dest)
    return moved


def latest_session_log(
    root: str | os.PathLike,
    base_name: str,
    *,
    legacy_path: str | os.PathLike | None = None,
) -> str | None:
    """Newest logs/{base_name}_*.log by mtime, else legacy root file if present."""
    log_dir = Path(root) / LOGS_DIR_NAME
    if log_dir.is_dir():
        matches = sorted(
            log_dir.glob(f"{base_name}_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if matches:
            return str(matches[0])

    legacy = legacy_path or legacy_log_path(root, base_name)
    if legacy and os.path.isfile(legacy):
        return str(legacy)
    return None
