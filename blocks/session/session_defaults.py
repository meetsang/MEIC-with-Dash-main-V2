"""Persistent defaults for daily MEIC session plan rows (cr, times, qty, width, stop)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

from common import trades_layout

log = logging.getLogger(__name__)

SESSION_DEFAULTS_FILENAME = 'meic_session_defaults.json'
PERSISTABLE_KEYS = frozenset({
    'credit_min',
    'credit_max',
    'entry_window_start',
    'entry_window_end',
    'quantity',
    'width',
    'stop_multiplier',
    'fill_wait_sec',
    'chase1_mode',
    'chase1_max',
    'chase2_mode',
    'chase2_max',
})


def session_defaults_path(root: Optional[str] = None) -> str:
    return trades_layout.ops_path(SESSION_DEFAULTS_FILENAME, root=root)


def load_session_defaults(root: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load persistent session row defaults keyed by slot_key (e.g. '10-30_P')."""
    path = session_defaults_path(root)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning('Could not load session defaults from %s: %s', path, exc)
    return {}


def save_session_defaults(
    slot_key: str,
    updates: Dict[str, Any],
    root: Optional[str] = None,
) -> None:
    """Save persistent default overrides for a specific slot key."""
    to_persist = {k: v for k, v in updates.items() if k in PERSISTABLE_KEYS}
    if not to_persist:
        return
    save_bulk_session_defaults({slot_key: to_persist}, root=root)


def save_bulk_session_defaults(
    updates_by_slot: Dict[str, Dict[str, Any]],
    root: Optional[str] = None,
) -> None:
    """Save persistent default overrides for multiple slot keys atomically."""
    path = session_defaults_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = load_session_defaults(root=root)
    changed = False

    for slot_key, fields in updates_by_slot.items():
        filtered = {k: v for k, v in fields.items() if k in PERSISTABLE_KEYS}
        if not filtered:
            continue
        entry = current.setdefault(slot_key, {})
        for k, v in filtered.items():
            if entry.get(k) != v:
                entry[k] = v
                changed = True

    if not changed and os.path.isfile(path):
        return

    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
            f.flush()
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        log.info('Saved session defaults for %d slots to %s', len(updates_by_slot), path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
