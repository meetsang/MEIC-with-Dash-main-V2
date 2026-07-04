"""Pick the best active trade JSON when multiple files match one lot+side slot."""
from __future__ import annotations

from typing import Any, Dict, List, Optional


_STATUS_RANK = {
    'open': 4,
    'closing': 4,
    'closed': 3,
    'pending_fill': 1,
}


def pick_best_trade(matches: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Prefer filled trades, then live status, then newest entry timestamp."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def _score(trade: Dict[str, Any]) -> tuple:
        filled = int(trade.get('filled_quantity') or 0)
        status = trade.get('status', '')
        rank = _STATUS_RANK.get(status, 2)
        ts = (trade.get('entry') or {}).get('timestamp') or ''
        return (filled > 0, rank, ts)

    return max(matches, key=_score)
