"""Test / sandbox trade paths — excluded from dashboard totals and history sync."""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from common.trades_layout import project_root

# Lots used by pytest fixtures and seed scripts (never in production PnL).
KNOWN_TEST_LOTS = frozenset({
    'ms-99',
    'ms-100',
    'ms-v3',
    'test-lot',
    'test',
})


def is_test_trade_path(path: str, root: Optional[str] = None) -> bool:
    """True when JSON lives under trades/test/ or trades/sandbox/."""
    if not path:
        return False
    base = os.path.abspath(root or project_root())
    norm = os.path.normpath(os.path.abspath(path))
    for rel in ('trades/test', 'trades/sandbox'):
        prefix = os.path.normpath(os.path.join(base, rel))
        if norm == prefix or norm.startswith(prefix + os.sep):
            return True
    return False


def is_test_trade_lot(lot: Optional[str]) -> bool:
    text = (lot or '').strip().lower()
    if not text:
        return False
    if text in KNOWN_TEST_LOTS:
        return True
    if text.startswith('test') or text.endswith('-test'):
        return True
    return False


def is_test_trade(trade: Dict[str, Any], path: str = '') -> bool:
    """Whether a trade dict/path should be excluded from operator totals."""
    if path and is_test_trade_path(path):
        return True
    entry = trade.get('entry') or {}
    lot = trade.get('lot') or entry.get('lot')
    if is_test_trade_lot(str(lot) if lot is not None else ''):
        return True
    base = os.path.basename(path).lower()
    if base in ('t.json', 'trade.json', 'ms1_p_test.json'):
        return True
    if '_test.json' in base or base.endswith('_test.json'):
        return True
    return False
