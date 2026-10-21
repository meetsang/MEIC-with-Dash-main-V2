"""Daily dashboard overrides for standalone strategies."""
from __future__ import annotations

import json
import os
import time
from dataclasses import fields
from datetime import date
from typing import Any, Dict, Optional, Type

from common.trades_layout import active_dir_for_strategy
from strategies.base import StrategyConfig, apply_config_overrides

# Permanent-only fields — never overridable from dashboard for a day.
NON_OVERRIDABLE = frozenset({
    'name', 'confirmed', 'requires_confirmation', 'broker', 'root',
})


def session_state_path(strategy_name: str, root: str, for_date: Optional[date] = None) -> str:
    d = (for_date or date.today()).isoformat()
    active = active_dir_for_strategy(strategy_name, root)
    os.makedirs(active, exist_ok=True)
    return os.path.join(active, f'session_{d}.json')


def load_session_state(strategy_name: str, root: str) -> Dict[str, Any]:
    path = session_state_path(strategy_name, root)
    if not os.path.isfile(path):
        persisted = {}
        try:
            from common.strategy_modes import get_persistent_modes_for_strategy
            persisted = get_persistent_modes_for_strategy(strategy_name, root)
        except Exception:
            pass
        return {'entered': False, 'skip_reason': None, 'overrides': dict(persisted)}
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        persisted = {}
        try:
            from common.strategy_modes import get_persistent_modes_for_strategy
            persisted = get_persistent_modes_for_strategy(strategy_name, root)
        except Exception:
            pass
        return {'entered': False, 'skip_reason': None, 'overrides': dict(persisted)}
    if 'overrides' not in state:
        state['overrides'] = {}
    return state


def save_session_state(strategy_name: str, root: str, state: Dict[str, Any]) -> None:
    path = session_state_path(strategy_name, root)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
        f.flush()
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def get_overrides(strategy_name: str, root: str) -> Dict[str, Any]:
    return dict(load_session_state(strategy_name, root).get('overrides') or {})


def set_overrides(strategy_name: str, root: str, updates: Dict[str, Any], cfg_cls: Type[StrategyConfig]) -> Dict[str, Any]:
    allowed = {f.name for f in fields(cfg_cls)} - NON_OVERRIDABLE
    unknown = set(updates) - allowed
    if unknown:
        try:
            import importlib, sys
            mod = sys.modules.get(cfg_cls.__module__)
            if mod:
                importlib.reload(mod)
                refreshed_cls = getattr(mod, cfg_cls.__name__, None)
                if refreshed_cls:
                    allowed = {f.name for f in fields(refreshed_cls)} - NON_OVERRIDABLE
                    unknown = set(updates) - allowed
                    if not unknown:
                        cfg_cls = refreshed_cls
        except Exception:
            pass

    if strategy_name == 'DRMA_HM_SPX':
        drma_sub_keys = {'dir_paper', 'non_dir_paper', 'futures_paper'}
        unknown = unknown - drma_sub_keys

    if unknown:
        raise ValueError(f'unknown override keys: {sorted(unknown)}')
    state = load_session_state(strategy_name, root)
    overrides = dict(state.get('overrides') or {})
    overrides.update(updates)
    state['overrides'] = overrides
    save_session_state(strategy_name, root, state)

    # Persist live/paper execution modes so they stick across sessions and restarts
    mode_updates = {k: v for k, v in updates.items() if k in ('paper', 'dir_paper', 'non_dir_paper', 'futures_paper', 'mode')}
    if mode_updates:
        try:
            from common.strategy_modes import set_strategy_paper_mode
            for mk, mv in mode_updates.items():
                if mk in ('dir_paper', 'non_dir_paper', 'futures_paper'):
                    set_strategy_paper_mode(strategy_name, mv, root=root, sub_key=mk)
                elif mk == 'paper':
                    set_strategy_paper_mode(strategy_name, mv, root=root)
        except Exception:
            pass

    return overrides


def delete_override(strategy_name: str, root: str, key: str) -> Dict[str, Any]:
    state = load_session_state(strategy_name, root)
    overrides = dict(state.get('overrides') or {})
    overrides.pop(key, None)
    if strategy_name == 'DRMA_HM_SPX' and key == 'paper':
        overrides.pop('dir_paper', None)
        overrides.pop('non_dir_paper', None)
        overrides.pop('futures_paper', None)
    if strategy_name == 'DRMA_HM_SPX' and not any(k in overrides for k in ('dir_paper', 'non_dir_paper', 'futures_paper')):
        overrides.pop('paper', None)
    state['overrides'] = overrides
    save_session_state(strategy_name, root, state)

    if key in ('paper', 'dir_paper', 'non_dir_paper', 'futures_paper', 'mode'):
        try:
            from common.strategy_modes import delete_strategy_paper_mode
            delete_strategy_paper_mode(strategy_name, key, root=root)
        except Exception:
            pass

    return overrides


def effective_config(cfg: StrategyConfig, strategy_name: str, root: str) -> StrategyConfig:
    """Return a copy of cfg with persistent modes and today's overrides applied."""
    import copy
    merged = copy.copy(cfg)
    try:
        from common.strategy_modes import get_persistent_modes_for_strategy
        persisted = get_persistent_modes_for_strategy(strategy_name, root=root)
        if persisted:
            apply_config_overrides(merged, persisted)
            if strategy_name == 'DRMA_HM_SPX' and 'paper' in persisted and persisted['paper'] is not None:
                parent_p = bool(persisted['paper'])
                if 'dir_paper' not in persisted:
                    merged.dir_paper = parent_p
                if 'non_dir_paper' not in persisted:
                    merged.non_dir_paper = parent_p
                if 'futures_paper' not in persisted:
                    merged.futures_paper = parent_p
    except Exception:
        pass
    overrides = get_overrides(strategy_name, root)
    if overrides:
        apply_config_overrides(merged, overrides)
    return merged


def is_disabled_for_today(cfg: StrategyConfig, strategy_name: str, root: str) -> bool:
    overrides = get_overrides(strategy_name, root)
    if 'enabled' in overrides:
        return not bool(overrides['enabled'])
    return not cfg.enabled
