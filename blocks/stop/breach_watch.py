"""Software spread breach watch — snapshot, logging, dashboard display."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

from blocks.stop.breach import spread_mark_price
from blocks.stop.stop_math import spread_breach_threshold

if TYPE_CHECKING:
    from blocks.stop.monitor import StopMonitor

log = logging.getLogger(__name__)

NEAR_BREACH_GAP = 0.50
NEAR_LOG_INTERVAL_SEC = 30.0
SPREAD_LOG_DELTA = 0.05


def resolve_breach_status(
    *,
    streamer_stale: bool,
    short_mqtt: bool,
    long_mqtt: bool,
    gap_to_breach: Optional[float],
) -> str:
    if streamer_stale:
        return 'stale'
    if not short_mqtt or not long_mqtt:
        return 'no_prices'
    if gap_to_breach is not None and gap_to_breach <= 0:
        return 'breached'
    if gap_to_breach is not None and gap_to_breach <= NEAR_BREACH_GAP:
        return 'near'
    return 'ok'


def build_breach_watch_snapshot(
    state: Dict[str, Any],
    *,
    short_p: Optional[float],
    long_p: Optional[float],
    streamer_stale: bool,
    now_iso: str,
) -> Dict[str, Any]:
    threshold = spread_breach_threshold(state)
    active = state.get('active_stop') or {}
    exchange_stop = active.get('stop_price') or state.get('designated_stop_price')

    short_mqtt = short_p is not None
    long_mqtt = long_p is not None
    spread_mid = None
    gap = None
    if short_mqtt and long_mqtt:
        spread_mid = spread_mark_price(float(short_p), float(long_p))
        gap = round(threshold - spread_mid, 2)

    status = resolve_breach_status(
        streamer_stale=streamer_stale,
        short_mqtt=short_mqtt,
        long_mqtt=long_mqtt,
        gap_to_breach=gap,
    )

    return {
        'threshold': threshold,
        'spread_mid': spread_mid,
        'gap_to_breach': gap,
        'short_mqtt': short_mqtt,
        'long_mqtt': long_mqtt,
        'streamer_stale': streamer_stale,
        'exchange_stop': exchange_stop,
        'status': status,
        'updated_at': now_iso,
    }


def breach_label_from_watch(watch: Dict[str, Any]) -> str:
    threshold = watch.get('threshold')
    if threshold is None:
        return ''
    status = watch.get('status') or 'ok'
    if status == 'no_prices':
        missing = []
        if not watch.get('short_mqtt'):
            missing.append('short')
        if not watch.get('long_mqtt'):
            missing.append('long')
        return f'{float(threshold):.2f} ⚠ no MQTT ({",".join(missing)})'
    if status == 'stale':
        return f'{float(threshold):.2f} ⚠ streamer stale'
    gap = watch.get('gap_to_breach')
    if gap is None:
        return f'{float(threshold):.2f}'
    return f'{float(threshold):.2f} / {float(gap):+.2f}'


def breach_display_fields(
    trade: Dict[str, Any],
    *,
    live_short: Optional[float],
    live_long: Optional[float],
    trade_status: str,
) -> Dict[str, Any]:
    """Dashboard overlay — prefer live MQTT mids, fall back to monitor snapshot."""
    empty = {
        'breach_threshold': None,
        'breach_gap': None,
        'breach_status': '',
        'breach_label': '',
        'breach_class': 'grid-dim',
    }
    if trade_status != 'open':
        return empty

    watch = trade.get('breach_watch') or {}
    threshold = watch.get('threshold')
    if threshold is None:
        threshold = spread_breach_threshold(trade)

    streamer_stale = bool(watch.get('streamer_stale'))
    if live_short is not None and live_long is not None:
        spread_mid = spread_mark_price(float(live_short), float(live_long))
        short_mqtt = True
        long_mqtt = True
    else:
        spread_mid = watch.get('spread_mid')
        short_mqtt = bool(watch.get('short_mqtt'))
        long_mqtt = bool(watch.get('long_mqtt'))

    gap = (
        round(float(threshold) - float(spread_mid), 2)
        if spread_mid is not None
        else watch.get('gap_to_breach')
    )
    status = resolve_breach_status(
        streamer_stale=streamer_stale,
        short_mqtt=short_mqtt,
        long_mqtt=long_mqtt,
        gap_to_breach=float(gap) if gap is not None else None,
    )

    display_watch = {
        'threshold': threshold,
        'gap_to_breach': gap,
        'status': status,
        'short_mqtt': short_mqtt,
        'long_mqtt': long_mqtt,
        'streamer_stale': streamer_stale,
    }
    label = breach_label_from_watch(display_watch)

    css = 'grid-dim'
    if status in ('no_prices', 'stale'):
        css = 'text-warning'
    elif status == 'near':
        css = 'text-warning fw-semibold'
    elif status == 'breached':
        css = 'text-danger fw-semibold'

    return {
        'breach_threshold': threshold,
        'breach_gap': gap,
        'breach_status': status,
        'breach_label': label,
        'breach_class': css,
    }


def log_breach_watch(monitor: 'StopMonitor', watch: Dict[str, Any]) -> None:
    """Rate-limited breach diagnostics for stop_monitor log."""
    lot = monitor.state.get('lot', '?')
    side = (monitor.state.get('entry') or {}).get('side', '?')
    tag = f'{lot} {side}'

    status = watch.get('status') or 'ok'
    if status == 'stale':
        if not monitor._breach_stale_logged:
            log.critical(
                'Breach watch %s: streamer stale — software breach checks frozen',
                tag,
            )
            monitor._breach_stale_logged = True
        return
    monitor._breach_stale_logged = False

    if status == 'no_prices':
        if not monitor._breach_missing_prices_logged:
            short_sym = monitor.state['short_leg']['symbol']
            long_sym = monitor.state['long_leg']['symbol']
            log.warning(
                'Breach watch %s: missing MQTT — short=%s (%s) long=%s (%s)',
                tag,
                short_sym,
                'ok' if watch.get('short_mqtt') else 'MISSING',
                long_sym,
                'ok' if watch.get('long_mqtt') else 'MISSING',
            )
            monitor._breach_missing_prices_logged = True
        return
    monitor._breach_missing_prices_logged = False

    spread = watch.get('spread_mid')
    threshold = watch.get('threshold')
    gap = watch.get('gap_to_breach')
    if spread is None or threshold is None or gap is None:
        return

    now = time.time()
    spread_changed = (
        monitor._breach_last_near_spread is None
        or abs(float(spread) - float(monitor._breach_last_near_spread)) >= SPREAD_LOG_DELTA
    )
    due = (now - monitor._breach_last_near_log) >= NEAR_LOG_INTERVAL_SEC

    if status in ('near', 'breached') and (due or spread_changed):
        log.info(
            'Breach watch %s: spread %.2f / threshold %.2f (gap %+.2f) exchange_stop=%s',
            tag,
            float(spread),
            float(threshold),
            float(gap),
            watch.get('exchange_stop'),
        )
        monitor._breach_last_near_log = now
        monitor._breach_last_near_spread = float(spread)
