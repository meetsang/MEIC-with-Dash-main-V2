"""Persistent strategy execution modes (paper vs live) across sessions and restarts.

Ensures strategy and sub-strategy live/paper choices persist indefinitely
until explicitly changed back by the operator.
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

STRATEGY_MODES_FILENAME = 'strategy_modes.json'

# Recognized mode keys that control execution environment
MODE_KEYS = frozenset({
    'paper',
    'dir_paper',
    'non_dir_paper',
    'futures_paper',
    'mode',
})

# Sub-strategy to parent strategy and field mapping
SUB_STRATEGY_MAP = {
    'DRMA_HM_DIR': ('DRMA_HM_SPX', 'dir_paper'),
    'DRMA_HM_NONDIR': ('DRMA_HM_SPX', 'non_dir_paper'),
    'DRMA_HM_FUT': ('DRMA_HM_SPX', 'futures_paper'),
}


def strategy_modes_path(root: Optional[str] = None) -> str:
    """Return canonical path to trades/strategy_modes.json."""
    return trades_layout.ops_path(STRATEGY_MODES_FILENAME, root=root)


def load_strategy_modes(root: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Load persistent strategy modes dict from disk."""
    path = strategy_modes_path(root)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning('Could not load strategy modes from %s: %s', path, exc)
    return {}


def save_strategy_modes(modes: Dict[str, Dict[str, Any]], root: Optional[str] = None) -> None:
    """Save persistent strategy modes dict atomically with Windows-safe retries."""
    path = strategy_modes_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(modes, f, indent=2)
            f.flush()
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
        log.info('Saved persistent strategy modes to %s', path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def get_strategy_paper_mode(strategy_name: str, root: Optional[str] = None) -> Optional[bool]:
    """Return whether a strategy or sub-strategy is configured for paper mode in persistent storage.

    Returns None if no persistent setting exists.
    """
    modes = load_strategy_modes(root=root)
    clean_name = strategy_name.strip()

    # 1. Sub-strategy mapping (e.g. DRMA_HM_DIR -> DRMA_HM_SPX.dir_paper)
    if clean_name in SUB_STRATEGY_MAP:
        parent_name, field_name = SUB_STRATEGY_MAP[clean_name]
        parent_cfg = modes.get(parent_name) or {}
        if field_name in parent_cfg and parent_cfg[field_name] is not None:
            return bool(parent_cfg[field_name])
        if any(k in parent_cfg for k in ('dir_paper', 'non_dir_paper', 'futures_paper')):
            return True
        if 'paper' in parent_cfg and parent_cfg['paper'] is not None:
            return bool(parent_cfg['paper'])

    # 2. Direct strategy check
    strat_cfg = modes.get(clean_name) or {}
    if 'paper' in strat_cfg and strat_cfg['paper'] is not None:
        return bool(strat_cfg['paper'])

    # DRMA_HM_SPX sub-strategy aggregation fallback
    if clean_name == 'DRMA_HM_SPX':
        sub_modes = [strat_cfg[k] for k in ('dir_paper', 'non_dir_paper', 'futures_paper') if k in strat_cfg and strat_cfg[k] is not None]
        if sub_modes:
            return all(bool(v) for v in sub_modes)

    return None


def get_persistent_modes_for_strategy(strategy_name: str, root: Optional[str] = None) -> Dict[str, Any]:
    """Return all persistent mode fields stored for a strategy (e.g. paper, dir_paper, etc.)."""
    modes = load_strategy_modes(root=root)
    clean_name = strategy_name.strip()
    data = dict(modes.get(clean_name) or {})
    if clean_name == 'DRMA_HM_SPX' and 'paper' in data and data['paper'] is not None:
        parent_val = bool(data['paper'])
        for k in ('dir_paper', 'non_dir_paper', 'futures_paper'):
            if k not in data or data[k] is None:
                data[k] = parent_val
    return data


def set_strategy_paper_mode(
    strategy_name: str,
    paper: bool,
    root: Optional[str] = None,
    *,
    sub_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Set and persist strategy paper mode indefinitely until changed back."""
    modes = load_strategy_modes(root=root)
    clean_name = strategy_name.strip()

    if clean_name in SUB_STRATEGY_MAP:
        parent_name, field_name = SUB_STRATEGY_MAP[clean_name]
        entry = modes.setdefault(parent_name, {})
        entry[field_name] = bool(paper)
        target_name = parent_name
    elif clean_name == 'DRMA_HM_SPX' and sub_key in ('dir_paper', 'non_dir_paper', 'futures_paper'):
        entry = modes.setdefault('DRMA_HM_SPX', {})
        entry[sub_key] = bool(paper)
        target_name = 'DRMA_HM_SPX'
    elif clean_name == 'DRMA_HM_SPX' and sub_key is None:
        entry = modes.setdefault('DRMA_HM_SPX', {})
        b_val = bool(paper)
        entry['paper'] = b_val
        entry['dir_paper'] = b_val
        entry['non_dir_paper'] = b_val
        entry['futures_paper'] = b_val
        target_name = 'DRMA_HM_SPX'
    else:
        entry = modes.setdefault(clean_name, {})
        entry['paper'] = bool(paper)
        if clean_name in ('IRON_FLY_SPX', 'IRON_FLY_XSP'):
            entry['mode'] = 'PAPER' if paper else 'LIVE_AUTO'
        target_name = clean_name

    save_strategy_modes(modes, root=root)

    # Sync strategy-specific files where applicable
    if clean_name in ('MEIC_IC', 'MEIC'):
        try:
            from common.meic_settings import get_meic_settings, save_meic_settings
            st = get_meic_settings(root=root)
            st.paper_mode = bool(paper)
            save_meic_settings(st, root=root)
        except Exception as exc:
            log.warning('Could not sync MEIC settings paper mode: %s', exc)

    if clean_name == 'CLONE_TRADES':
        try:
            cfg_p = os.path.join(root or trades_layout.project_root(), 'trades', 'clone_trades', 'config.json')
            if os.path.exists(cfg_p):
                with open(cfg_p, 'r', encoding='utf-8') as f:
                    cfg_data = json.load(f)
                cfg_data['paper_mode'] = bool(paper)
                with open(cfg_p, 'w', encoding='utf-8') as f:
                    json.dump(cfg_data, f, indent=2)
        except Exception as exc:
            log.warning('Could not sync Clone Trades config paper mode: %s', exc)

    # Also sync today's active session state so running loops observe it immediately
    try:
        from common.daily_overrides import load_session_state, save_session_state
        state = load_session_state(target_name, root or trades_layout.project_root())
        session_ov = dict(state.get('overrides') or {})
        session_ov.update(modes.get(target_name, {}))
        state['overrides'] = session_ov
        save_session_state(target_name, root or trades_layout.project_root(), state)
    except Exception as exc:
        log.warning('Could not sync session state for %s: %s', target_name, exc)

    return dict(modes.get(target_name) or {})


def delete_strategy_paper_mode(
    strategy_name: str,
    key: str = 'paper',
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete a persistent mode override, reverting to strategies.yaml defaults."""
    modes = load_strategy_modes(root=root)
    clean_name = strategy_name.strip()

    if clean_name in SUB_STRATEGY_MAP:
        parent_name, field_name = SUB_STRATEGY_MAP[clean_name]
        target_name = parent_name
        target_key = field_name
    else:
        target_name = clean_name
        target_key = key

    if target_name in modes:
        modes[target_name].pop(target_key, None)
        if target_name == 'DRMA_HM_SPX' and target_key == 'paper':
            modes[target_name].pop('dir_paper', None)
            modes[target_name].pop('non_dir_paper', None)
            modes[target_name].pop('futures_paper', None)
        if target_name == 'DRMA_HM_SPX' and not any(k in modes[target_name] for k in ('dir_paper', 'non_dir_paper', 'futures_paper')):
            modes[target_name].pop('paper', None)
        if not modes[target_name]:
            modes.pop(target_name, None)
        save_strategy_modes(modes, root=root)

    return dict(modes.get(target_name) or {})
