"""Persistent strategy parameter overrides across sessions, days, and launcher restarts.

Ensures strategy parameters (limits, caps, targets, PT/SL, trade modes, etc.)
persist indefinitely until explicitly changed or cleared by the operator.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Dict, Optional

from common import trades_layout

log = logging.getLogger(__name__)

STRATEGY_OVERRIDES_FILENAME = 'strategy_overrides.json'

# Ephemeral parameters that should NOT be persisted indefinitely across days
EPHEMERAL_KEYS = frozenset({
    'manual_onh',
    'manual_onl',
    'manual_pdh',
    'manual_pdl',
    'manual_pdc',
    'manual_es_basis',
    'manual_es_offset',
})


def strategy_overrides_path(root: Optional[str] = None) -> str:
    """Return canonical path to trades/strategy_overrides.json."""
    return trades_layout.ops_path(STRATEGY_OVERRIDES_FILENAME, root=root)


def load_strategy_overrides(root: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load persistent strategy overrides dict from disk."""
    path = strategy_overrides_path(root)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning('Could not load strategy overrides from %s: %s', path, exc)
    return {}


def save_strategy_overrides(overrides: Dict[str, Dict[str, Any]], root: Optional[str] = None) -> None:
    """Save persistent strategy overrides dict atomically with Windows-safe retries."""
    path = strategy_overrides_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(overrides, f, indent=2)
            f.flush()
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
        log.info('Saved persistent strategy overrides to %s', path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def get_persistent_overrides_for_strategy(strategy_name: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Return all persistent parameter overrides stored for a strategy."""
    overrides = load_strategy_overrides(root=root)
    clean_name = strategy_name.strip()
    return dict(overrides.get(clean_name) or {})


def set_persistent_override_for_strategy(
    strategy_name: str,
    updates: Dict[str, Any],
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """Save or update persistent overrides for a strategy."""
    overrides = load_strategy_overrides(root=root)
    clean_name = strategy_name.strip()
    strat_ov = overrides.setdefault(clean_name, {})

    filtered = {k: v for k, v in updates.items() if k not in EPHEMERAL_KEYS}
    strat_ov.update(filtered)
    save_strategy_overrides(overrides, root=root)
    return dict(strat_ov)


def delete_persistent_override_for_strategy(
    strategy_name: str,
    key: str,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """Remove a key from persistent strategy overrides."""
    overrides = load_strategy_overrides(root=root)
    clean_name = strategy_name.strip()
    if clean_name in overrides:
        overrides[clean_name].pop(key, None)
        if not overrides[clean_name]:
            overrides.pop(clean_name, None)
        save_strategy_overrides(overrides, root=root)
    return dict(overrides.get(clean_name) or {})
