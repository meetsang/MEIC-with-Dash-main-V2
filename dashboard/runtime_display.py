"""Dashboard runtime status labels (Phase 6) — read-only display helpers."""
from __future__ import annotations

from typing import Any, Dict, Optional


def is_protective_estimate(trade: Dict[str, Any]) -> bool:
    entry = trade.get('entry') or {}
    if entry.get('fill_confidence') == 'protective_estimate':
        return True
    fs = (trade.get('open_order') or {}).get('fill_sync') or {}
    return fs.get('phase') == 'resolved_estimated'


def is_expired_trade(
    trade: Optional[Dict[str, Any]],
    close_mechanism: Optional[str] = None,
) -> bool:
    if not trade:
        return close_mechanism == 'expiry_settlement'
    if trade.get('settled_at_expiry'):
        return True
    mech = close_mechanism if close_mechanism is not None else trade.get('close_mechanism')
    return mech == 'expiry_settlement'


def decorate_entry_label(trade: Dict[str, Any], base_label: str) -> str:
    """Prefix protective-estimate trades with Estimated Fill."""
    if not base_label:
        return base_label
    if is_protective_estimate(trade):
        return f'Estimated Fill · {base_label}'
    return base_label


def quote_source_label(trade: Dict[str, Any]) -> str:
    """REST vs MQTT fallback entry quote source when recorded on trade JSON."""
    entry = trade.get('entry') or {}
    lifecycle = trade.get('lifecycle') or {}
    src = (
        trade.get('entry_quote_source')
        or entry.get('quote_source')
        or entry.get('candidate_source')
        or lifecycle.get('entry_scan_source')
    )
    if not src:
        return ''
    normalized = str(src).lower()
    if normalized in ('mqtt_fallback', 'mqtt', 'mqtt-fallback'):
        return 'MQTT fallback'
    if normalized == 'rest':
        return 'REST'
    return str(src)


def breach_readiness_label(trade: Dict[str, Any]) -> str:
    """Software-breach readiness / freeze reason from persisted breach_watch."""
    watch = trade.get('breach_watch') or {}
    if watch.get('software_breach_confirmed'):
        return 'SW breach confirmed'
    if watch.get('software_breach_ready'):
        return 'SW breach ready'
    reason = watch.get('quote_pair_reason')
    if reason and reason not in ('ready', ''):
        return f'SW frozen ({reason})'
    if watch.get('fill_grace_remaining_sec'):
        return f'SW grace {watch["fill_grace_remaining_sec"]}s'
    return ''
