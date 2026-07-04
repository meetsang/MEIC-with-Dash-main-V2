"""Sanity checks for option MQTT mids (reject SPX index noise)."""
from __future__ import annotations

import re
from typing import Optional

_OPTION_SYMBOL_RE = re.compile(r'SPXW?\d{6}[CP]\d+', re.IGNORECASE)

# SPX option premiums rarely exceed this; index prints are ~4000–8000.
MAX_OPTION_MID = 20.0


def is_option_symbol(symbol: str) -> bool:
    if not symbol:
        return False
    s = symbol.strip().lstrip('.')
    return bool(_OPTION_SYMBOL_RE.search(s))


def sanitize_option_mid(
    symbol: str,
    price: Optional[float],
    *,
    max_mid: float = MAX_OPTION_MID,
) -> Optional[float]:
    """Return price if it looks like an option mid; None if missing or index noise."""
    if price is None:
        return None
    try:
        val = float(price)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    if is_option_symbol(symbol) and val > max_mid:
        return None
    return val
