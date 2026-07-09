"""Unified trades directory layout (V2).

All live state, ops files, and per-strategy archives live under ``trades/``.
"""
from __future__ import annotations

import os
from typing import List, Optional

STRATEGY_MEIC = 'MEIC_IC'
STRATEGY_MANUAL = 'MANUAL_SPREAD'

MEIC_ACTIVE = 'trades/active/MEIC_IC'
MEIC_HISTORY = 'trades/history/MEIC_IC'
MANUAL_ACTIVE = 'trades/active/MANUAL_SPREAD'
MANUAL_HISTORY = 'trades/history/MANUAL_SPREAD'

# Test fixtures — never scanned for dashboard History / PnL totals.
TEST_ROOT = 'trades/test'
TEST_MEIC_ACTIVE = 'trades/test/active/MEIC_IC'
TEST_MANUAL_ACTIVE = 'trades/test/active/MANUAL_SPREAD'
TEST_MEIC_HISTORY = 'trades/test/history/MEIC_IC'
TEST_MANUAL_HISTORY = 'trades/test/history/MANUAL_SPREAD'


def project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def trades_root(root: Optional[str] = None) -> str:
    return os.path.join(root or project_root(), 'trades')


def active_dir_for_strategy(strategy: str, root: Optional[str] = None) -> str:
    if strategy == STRATEGY_MANUAL:
        return os.path.join(root or project_root(), MANUAL_ACTIVE)
    return os.path.join(root or project_root(), MEIC_ACTIVE)


def history_dir_for_strategy(strategy: str, root: Optional[str] = None) -> str:
    if strategy == STRATEGY_MANUAL:
        return os.path.join(root or project_root(), MANUAL_HISTORY)
    return os.path.join(root or project_root(), MEIC_HISTORY)


def all_active_dirs(root: Optional[str] = None) -> List[str]:
    base = root or project_root()
    return [
        os.path.join(base, MEIC_ACTIVE),
        os.path.join(base, MANUAL_ACTIVE),
    ]


def commands_dir(root: Optional[str] = None) -> str:
    return os.path.join(trades_root(root), 'commands')


def ops_path(name: str, root: Optional[str] = None) -> str:
    """pause_tranches.json, killswitch.json, heartbeat.json, etc."""
    return os.path.join(trades_root(root), name)


def ensure_all_trade_dirs(root: Optional[str] = None) -> None:
    base = root or project_root()
    for path in (
        os.path.join(base, MEIC_ACTIVE),
        os.path.join(base, MEIC_HISTORY),
        os.path.join(base, MANUAL_ACTIVE),
        os.path.join(base, MANUAL_HISTORY),
        os.path.join(base, TEST_MEIC_ACTIVE),
        os.path.join(base, TEST_MEIC_HISTORY),
        os.path.join(base, TEST_MANUAL_ACTIVE),
        os.path.join(base, TEST_MANUAL_HISTORY),
        commands_dir(base),
    ):
        os.makedirs(path, exist_ok=True)
