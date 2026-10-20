"""Unified trades directory layout (V2).

All live state, ops files, and per-strategy archives live under ``trades/``.
"""
from __future__ import annotations

import os
from typing import List, Optional

STRATEGY_MEIC = 'MEIC_IC'
STRATEGY_MANUAL = 'MANUAL_SPREAD'
STRATEGY_SKYLINE = 'SKYLINE_SPX'
STRATEGY_PERMA_BULL = 'PERMA_BULL_PCS'
STRATEGY_MEAN_REVERSION = 'MEAN_REVERSION_SPX'
STRATEGY_IRON_FLY = 'IRON_FLY_SPX'
STRATEGY_IRON_FLY_XSP = 'IRON_FLY_XSP'
STRATEGY_DRMA_HM = 'DRMA_HM_SPX'
STRATEGY_DRMA_HM_DIR = 'DRMA_HM_DIR'
STRATEGY_DRMA_HM_NONDIR = 'DRMA_HM_NONDIR'
STRATEGY_DRMA_HM_FUT = 'DRMA_HM_FUT'

MEIC_ACTIVE = 'trades/active/MEIC_IC'
MEIC_HISTORY = 'trades/history/MEIC_IC'
MANUAL_ACTIVE = 'trades/active/MANUAL_SPREAD'
MANUAL_HISTORY = 'trades/history/MANUAL_SPREAD'
SKYLINE_ACTIVE = 'trades/active/SKYLINE_SPX'
SKYLINE_HISTORY = 'trades/history/SKYLINE_SPX'
PERMA_BULL_ACTIVE = 'trades/active/PERMA_BULL_PCS'
PERMA_BULL_HISTORY = 'trades/history/PERMA_BULL_PCS'
MEAN_REVERSION_ACTIVE = 'trades/active/MEAN_REVERSION_SPX'
MEAN_REVERSION_HISTORY = 'trades/history/MEAN_REVERSION_SPX'
IRON_FLY_ACTIVE = 'trades/active/IRON_FLY_SPX'
IRON_FLY_HISTORY = 'trades/history/IRON_FLY_SPX'
IRON_FLY_XSP_ACTIVE = 'trades/active/IRON_FLY_XSP'
IRON_FLY_XSP_HISTORY = 'trades/history/IRON_FLY_XSP'
DRMA_HM_ACTIVE = 'trades/active/DRMA_HM_SPX'
DRMA_HM_HISTORY = 'trades/history/DRMA_HM_SPX'

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
    return os.path.join(root or project_root(), 'trades', 'active', strategy)


def history_dir_for_strategy(strategy: str, root: Optional[str] = None) -> str:
    return os.path.join(root or project_root(), 'trades', 'history', strategy)


def all_active_dirs(root: Optional[str] = None) -> List[str]:
    base = root or project_root()
    active_root = os.path.join(base, 'trades', 'active')
    if not os.path.isdir(active_root):
        return [
            os.path.join(base, MEIC_ACTIVE),
            os.path.join(base, MANUAL_ACTIVE),
        ]
    dirs = []
    for name in sorted(os.listdir(active_root)):
        path = os.path.join(active_root, name)
        if os.path.isdir(path):
            dirs.append(path)
    return dirs or [
        os.path.join(base, MEIC_ACTIVE),
        os.path.join(base, MANUAL_ACTIVE),
    ]


def commands_dir(root: Optional[str] = None) -> str:
    return os.path.join(trades_root(root), 'commands')


def ops_path(name: str, root: Optional[str] = None) -> str:
    """pause_tranches.json, killswitch.json, heartbeat.json, etc."""
    return os.path.join(trades_root(root), name)


def registered_strategies() -> List[str]:
    return [
        STRATEGY_MEIC,
        STRATEGY_MANUAL,
        STRATEGY_SKYLINE,
        STRATEGY_PERMA_BULL,
        STRATEGY_MEAN_REVERSION,
        STRATEGY_IRON_FLY,
        STRATEGY_IRON_FLY_XSP,
        STRATEGY_DRMA_HM,
    ]


def ensure_all_trade_dirs(root: Optional[str] = None) -> None:
    base = root or project_root()
    for path in (
        os.path.join(base, MEIC_ACTIVE),
        os.path.join(base, MEIC_HISTORY),
        os.path.join(base, MANUAL_ACTIVE),
        os.path.join(base, MANUAL_HISTORY),
        os.path.join(base, SKYLINE_ACTIVE),
        os.path.join(base, SKYLINE_HISTORY),
        os.path.join(base, PERMA_BULL_ACTIVE),
        os.path.join(base, PERMA_BULL_HISTORY),
        os.path.join(base, MEAN_REVERSION_ACTIVE),
        os.path.join(base, MEAN_REVERSION_HISTORY),
        os.path.join(base, IRON_FLY_ACTIVE),
        os.path.join(base, IRON_FLY_HISTORY),
        os.path.join(base, IRON_FLY_XSP_ACTIVE),
        os.path.join(base, IRON_FLY_XSP_HISTORY),
        os.path.join(base, DRMA_HM_ACTIVE),
        os.path.join(base, DRMA_HM_HISTORY),
        os.path.join(base, TEST_MEIC_ACTIVE),
        os.path.join(base, TEST_MEIC_HISTORY),
        os.path.join(base, TEST_MANUAL_ACTIVE),
        os.path.join(base, TEST_MANUAL_HISTORY),
        commands_dir(base),
    ):
        os.makedirs(path, exist_ok=True)
