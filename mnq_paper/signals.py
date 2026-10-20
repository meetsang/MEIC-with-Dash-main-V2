"""Parse inbound webhook payloads (TradingView / Ninja-style alerts)."""
from __future__ import annotations

from typing import Any, Optional

from mnq_paper.models import utc_now_iso


def _str(val: Any) -> str:
    if val is None:
        return ''
    return str(val).strip()


def normalize_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, str):
        text = payload.strip().lower()
        if text in ('buy', 'sell', 'long', 'short', 'bull', 'bear', 'bullish', 'bearish', 'b', 's'):
            return {'action': text}
        return {}
    if not isinstance(payload, dict):
        return {}
    return dict(payload)


def extract_action(payload: dict[str, Any]) -> str:
    for key in ('action', 'side', 'signal', 'direction'):
        val = _str(payload.get(key))
        if val:
            return val
    strategy = payload.get('strategy')
    if isinstance(strategy, dict):
        order = strategy.get('order') or {}
        if isinstance(order, dict):
            val = _str(order.get('action') or order.get('side'))
            if val:
                return val
    return ''


def extract_quantity(payload: dict[str, Any]) -> Optional[int]:
    raw = payload.get('qty') or payload.get('quantity')
    if raw is None or raw == '':
        return None
    try:
        qty = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def extract_strategy(payload: dict[str, Any]) -> str:
    strat = payload.get('strategy')
    if isinstance(strat, dict):
        return _str(strat.get('name') or strat.get('id') or '')
    return _str(strat)


def is_placeholder_price(raw: str) -> bool:
    text = raw.strip()
    if not text:
        return True
    if '{{' in text and '}}' in text:
        return True
    return False


def extract_signal_price(payload: dict[str, Any]) -> Optional[float]:
    """Use alert price only when it is a real number (not {{close}})."""
    raw = _str(payload.get('price'))
    if is_placeholder_price(raw):
        return None
    try:
        px = float(raw.replace(',', ''))
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def build_signal_record(payload: dict[str, Any], received_at: Optional[str] = None) -> dict[str, Any]:
    ts = received_at or utc_now_iso()
    base: dict[str, Any] = {
        'received_at': ts,
        'aname': _str(payload.get('aname')),
        'account': _str(payload.get('account')),
        'ticker': _str(payload.get('ticker')),
        'action': extract_action(payload),
        'quantity': _str(payload.get('quantity') or payload.get('qty')),
        'price': _str(payload.get('price')),
        'strategy': extract_strategy(payload),
    }
    # Preserve any additional custom fields passed in payload
    for k, v in payload.items():
        if k not in base:
            base[k] = v
    return base


def strategy_allowed(payload: dict[str, Any], allowed: str) -> bool:
    if not allowed or not allowed.strip():
        return True
    got = extract_strategy(payload).lower()
    # If incoming signal didn't specify strategy, allow it
    if not got:
        return True
    return got == allowed.strip().lower()

