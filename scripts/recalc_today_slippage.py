#!/usr/bin/env python3
"""Recompute operator slippage on today's trade JSON and persist to disk."""
from __future__ import annotations

import glob
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod
from blocks.stop.close_fills import apply_close_slippage_fields, slippage_dollars
from common.session_cleanup import central_today


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def today_trade_paths(root: str | None = None) -> list[str]:
    root = root or _project_root()
    today = central_today().isoformat()
    tag = today.replace('-', '')
    paths: list[str] = []
    for pattern in (
        os.path.join(root, 'trades', 'active', '*', f'*{tag}*.json'),
        os.path.join(root, 'trades', 'history', '*', f'*{tag}*.json'),
        os.path.join(root, 'trades', 'history', '*', today, f'*{tag}*.json'),
    ):
        paths.extend(glob.glob(pattern))
    return sorted(set(paths))


def recalc_today_slippage(root: str | None = None) -> int:
    """Recompute and save operator slippage on closed trades for today. Returns count updated."""
    updated = 0
    for path in today_trade_paths(root):
        try:
            state = state_mod.load_state(path)
        except (OSError, ValueError):
            continue
        if state.get('status') != 'closed':
            continue
        apply_close_slippage_fields(state)
        state_mod.save_state(path, state)
        updated += 1
    return updated


def main() -> None:
    count = recalc_today_slippage()
    print(f'Updated slippage on {count} closed trade file(s) for {central_today().isoformat()}')


if __name__ == '__main__':
    main()
