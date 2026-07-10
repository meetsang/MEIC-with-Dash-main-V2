"""Quote-pair readiness and consecutive breach confirmation for software breach."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional, TYPE_CHECKING

from blocks.stop.breach import spread_breach_triggered, spread_mark_price
from blocks.stop.breach_config import (
    BREACH_CONFIRM_MAX_WINDOW_SEC,
    BREACH_CONFIRM_OBSERVATIONS,
    BREACH_FILL_GRACE_SEC,
    MAX_MQTT_BREACH_QUOTE_AGE_SEC,
    MAX_MQTT_PAIR_SKEW_SEC,
    MQTT_SPREAD_WIDTH_TOLERANCE,
)
from blocks.stop.fill_provenance import spread_width_points
from blocks.stop.fill_reference import ensure_fill_reference_epoch, resolve_fill_reference_epoch
from blocks.stop.stop_math import spread_breach_threshold
from common.market_quote import (
    REPLAY_EVENT_KIND,
    QuoteSnapshot,
    is_genuine_event_kind,
    quote_is_pre_subscription,
)

if TYPE_CHECKING:
    from blocks.stop.monitor import StopMonitor
    from common.mqtt_prices import MqttPriceCache

log = logging.getLogger(__name__)

REASON_FILL_GRACE = 'fill_grace'
REASON_MISSING_QUOTE = 'missing_quote'
REASON_OLD_SESSION = 'old_session'
REASON_REPLAY_EVENT = 'replay_event'
REASON_PRE_FILL = 'pre_fill'
REASON_PRE_SUBSCRIPTION = 'pre_subscription'
REASON_SOURCE_STALE = 'source_stale'
REASON_PAIR_SKEW = 'pair_skew'
REASON_NONPOSITIVE_PRICE = 'nonpositive_price'
REASON_NEGATIVE_SPREAD = 'negative_spread'
REASON_SPREAD_OVER_WIDTH = 'spread_over_width'
REASON_QUOTE_NOT_ADVANCED = 'quote_not_advanced'
REASON_CONFIRMATION_PENDING = 'confirmation_pending'
REASON_READY = 'ready'
REASON_STREAMER_STALE = 'stale'
REASON_MQTT_CACHE_STALE = 'stale'
REASON_NO_PRICES = 'no_prices'


@dataclass(frozen=True)
class QuotePairReadiness:
    quote_pair_valid: bool
    quote_pair_reason: str
    software_breach_ready: bool
    short_quote: Optional[QuoteSnapshot]
    long_quote: Optional[QuoteSnapshot]
    spread_mid: Optional[float]
    short_source_epoch: Optional[float]
    long_source_epoch: Optional[float]
    short_sequence: Optional[int]
    long_sequence: Optional[int]
    pair_skew_sec: Optional[float]
    stream_session_id: Optional[str]
    fill_reference_epoch: Optional[float]
    fill_grace_remaining_sec: Optional[float]


def _lookup_quote(
    cache: 'MqttPriceCache',
    symbol: str,
    *,
    require_current_session: bool = False,
) -> Optional[QuoteSnapshot]:
    return cache.get_quote(
        symbol,
        require_current_session=require_current_session,
        allow_override=False,
        allow_pre_subscription=True,
    )


def evaluate_quote_pair_readiness(
    state: Dict[str, Any],
    cache: 'MqttPriceCache',
    *,
    streamer_stale: bool = False,
    mqtt_cache_stale: bool = False,
    now: Optional[float] = None,
) -> QuotePairReadiness:
    """Validate short/long QuoteSnapshot pair for software breach decisions."""
    now = now if now is not None else time.time()
    empty = QuotePairReadiness(
        quote_pair_valid=False,
        quote_pair_reason=REASON_MISSING_QUOTE,
        software_breach_ready=False,
        short_quote=None,
        long_quote=None,
        spread_mid=None,
        short_source_epoch=None,
        long_source_epoch=None,
        short_sequence=None,
        long_sequence=None,
        pair_skew_sec=None,
        stream_session_id=None,
        fill_reference_epoch=None,
        fill_grace_remaining_sec=None,
    )

    if streamer_stale:
        return replace(empty, quote_pair_reason=REASON_STREAMER_STALE)
    if mqtt_cache_stale:
        return replace(empty, quote_pair_reason=REASON_MQTT_CACHE_STALE)

    fill_ref, _ = resolve_fill_reference_epoch(state)
    if fill_ref is None:
        fill_ref = ensure_fill_reference_epoch(state)
    grace_remaining = None
    if fill_ref is not None and now < fill_ref + BREACH_FILL_GRACE_SEC:
        grace_remaining = round(fill_ref + BREACH_FILL_GRACE_SEC - now, 3)
        return QuotePairReadiness(
            quote_pair_valid=False,
            quote_pair_reason=REASON_FILL_GRACE,
            software_breach_ready=False,
            short_quote=None,
            long_quote=None,
            spread_mid=None,
            short_source_epoch=None,
            long_source_epoch=None,
            short_sequence=None,
            long_sequence=None,
            pair_skew_sec=None,
            stream_session_id=None,
            fill_reference_epoch=fill_ref,
            fill_grace_remaining_sec=grace_remaining,
        )

    short_sym = state['short_leg']['symbol']
    long_sym = state['long_leg']['symbol']
    current_session = cache.current_stream_session_id()
    short_q = _lookup_quote(cache, short_sym)
    long_q = _lookup_quote(cache, long_sym)

    if short_q is None or long_q is None:
        reason = REASON_MISSING_QUOTE
        if short_q is None and long_q is None:
            reason = REASON_NO_PRICES
        for sym in (short_sym, long_sym):
            if cache.last_event_kind(sym) == REPLAY_EVENT_KIND:
                return _invalid_pair(
                    short_q,
                    long_q,
                    REASON_REPLAY_EVENT,
                    fill_ref,
                )
        if current_session:
            for quote in (short_q, long_q):
                if quote is not None and quote.stream_session_id != current_session:
                    reason = REASON_OLD_SESSION
                    break
        return QuotePairReadiness(
            quote_pair_valid=False,
            quote_pair_reason=reason,
            software_breach_ready=False,
            short_quote=short_q,
            long_quote=long_q,
            spread_mid=None,
            short_source_epoch=short_q.source_event_epoch if short_q else None,
            long_source_epoch=long_q.source_event_epoch if long_q else None,
            short_sequence=short_q.sequence if short_q else None,
            long_sequence=long_q.sequence if long_q else None,
            pair_skew_sec=None,
            stream_session_id=(
                short_q.stream_session_id if short_q else (long_q.stream_session_id if long_q else None)
            ),
            fill_reference_epoch=fill_ref,
            fill_grace_remaining_sec=None,
        )

    if current_session:
        if short_q.stream_session_id != current_session or long_q.stream_session_id != current_session:
            return _invalid_pair(short_q, long_q, REASON_OLD_SESSION, fill_ref)

    for sym, quote in ((short_sym, short_q), (long_sym, long_q)):
        last_kind = cache.last_event_kind(sym)
        if last_kind == REPLAY_EVENT_KIND:
            return _invalid_pair(short_q, long_q, REASON_REPLAY_EVENT, fill_ref)

    for quote, reason in (
        (short_q, REASON_REPLAY_EVENT),
        (long_q, REASON_REPLAY_EVENT),
    ):
        if not is_genuine_event_kind(quote.event_kind):
            if quote.event_kind == REPLAY_EVENT_KIND:
                return _invalid_pair(
                    short_q, long_q, reason, fill_ref,
                )
            return _invalid_pair(short_q, long_q, REASON_MISSING_QUOTE, fill_ref)

    if fill_ref is not None:
        if short_q.source_event_epoch <= fill_ref or long_q.source_event_epoch <= fill_ref:
            return _invalid_pair(short_q, long_q, REASON_PRE_FILL, fill_ref)

    if quote_is_pre_subscription(short_q) or quote_is_pre_subscription(long_q):
        return _invalid_pair(short_q, long_q, REASON_PRE_SUBSCRIPTION, fill_ref)

    short_age = now - short_q.source_event_epoch
    long_age = now - long_q.source_event_epoch
    if short_age > MAX_MQTT_BREACH_QUOTE_AGE_SEC or long_age > MAX_MQTT_BREACH_QUOTE_AGE_SEC:
        return _invalid_pair(short_q, long_q, REASON_SOURCE_STALE, fill_ref)

    pair_skew = abs(short_q.source_event_epoch - long_q.source_event_epoch)
    if pair_skew > MAX_MQTT_PAIR_SKEW_SEC:
        return _invalid_pair(
            short_q, long_q, REASON_PAIR_SKEW, fill_ref, pair_skew_sec=round(pair_skew, 3),
        )

    if short_q.price <= 0 or long_q.price <= 0:
        return _invalid_pair(short_q, long_q, REASON_NONPOSITIVE_PRICE, fill_ref, pair_skew_sec=round(pair_skew, 3))

    spread_mid = spread_mark_price(short_q.price, long_q.price)
    if spread_mid < 0:
        return _invalid_pair(
            short_q, long_q, REASON_NEGATIVE_SPREAD, fill_ref,
            spread_mid=spread_mid, pair_skew_sec=round(pair_skew, 3),
        )

    width = spread_width_points(state)
    if spread_mid > width + MQTT_SPREAD_WIDTH_TOLERANCE:
        return _invalid_pair(
            short_q, long_q, REASON_SPREAD_OVER_WIDTH, fill_ref,
            spread_mid=spread_mid, pair_skew_sec=round(pair_skew, 3),
        )

    return QuotePairReadiness(
        quote_pair_valid=True,
        quote_pair_reason=REASON_READY,
        software_breach_ready=True,
        short_quote=short_q,
        long_quote=long_q,
        spread_mid=spread_mid,
        short_source_epoch=short_q.source_event_epoch,
        long_source_epoch=long_q.source_event_epoch,
        short_sequence=short_q.sequence,
        long_sequence=long_q.sequence,
        pair_skew_sec=round(pair_skew, 3),
        stream_session_id=short_q.stream_session_id,
        fill_reference_epoch=fill_ref,
        fill_grace_remaining_sec=None,
    )


def _invalid_pair(
    short_q: Optional[QuoteSnapshot],
    long_q: Optional[QuoteSnapshot],
    reason: str,
    fill_ref: Optional[float],
    *,
    spread_mid: Optional[float] = None,
    pair_skew_sec: Optional[float] = None,
) -> QuotePairReadiness:
    return QuotePairReadiness(
        quote_pair_valid=False,
        quote_pair_reason=reason,
        software_breach_ready=False,
        short_quote=short_q,
        long_quote=long_q,
        spread_mid=spread_mid,
        short_source_epoch=short_q.source_event_epoch if short_q else None,
        long_source_epoch=long_q.source_event_epoch if long_q else None,
        short_sequence=short_q.sequence if short_q else None,
        long_sequence=long_q.sequence if long_q else None,
        pair_skew_sec=pair_skew_sec,
        stream_session_id=(
            short_q.stream_session_id if short_q else (long_q.stream_session_id if long_q else None)
        ),
        fill_reference_epoch=fill_ref,
        fill_grace_remaining_sec=None,
    )


def _confirmation_section(state: Dict[str, Any]) -> Dict[str, Any]:
    section = state.setdefault('breach_confirmation', {})
    if not isinstance(section, dict):
        section = {}
        state['breach_confirmation'] = section
    return section


def reset_breach_confirmation(state: Dict[str, Any]) -> None:
    state['breach_confirmation'] = {
        'count': 0,
        'window_started_at': None,
        'last_short_sequence': None,
        'last_long_sequence': None,
        'last_short_source_epoch': None,
        'last_long_source_epoch': None,
        'last_stream_session_id': None,
    }


def _quote_pair_advanced(
    conf: Dict[str, Any],
    short_q: QuoteSnapshot,
    long_q: QuoteSnapshot,
) -> bool:
    if conf.get('count', 0) <= 0:
        return True
    prev_short_seq = conf.get('last_short_sequence')
    prev_long_seq = conf.get('last_long_sequence')
    prev_short_epoch = conf.get('last_short_source_epoch')
    prev_long_epoch = conf.get('last_long_source_epoch')
    if prev_short_seq is None and prev_long_seq is None:
        return True
    return (
        short_q.sequence > int(prev_short_seq or 0)
        or long_q.sequence > int(prev_long_seq or 0)
        or short_q.source_event_epoch > float(prev_short_epoch or 0)
        or long_q.source_event_epoch > float(prev_long_epoch or 0)
    )


def update_breach_confirmation(
    state: Dict[str, Any],
    readiness: QuotePairReadiness,
    *,
    spread_breached: bool,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Update consecutive breach confirmation; return summary for breach_watch."""
    now = now if now is not None else time.time()
    conf = _confirmation_section(state)
    required = BREACH_CONFIRM_OBSERVATIONS

    def _summary(count: int, reason: str) -> Dict[str, Any]:
        return {
            'breach_confirmation_count': count,
            'breach_confirmation_required': required,
            'breach_confirmation_reason': reason,
            'software_breach_confirmed': count >= required,
        }

    if not readiness.software_breach_ready or not readiness.quote_pair_valid:
        reset_breach_confirmation(state)
        return _summary(0, readiness.quote_pair_reason)

    if not spread_breached:
        reset_breach_confirmation(state)
        return _summary(0, REASON_READY)

    short_q = readiness.short_quote
    long_q = readiness.long_quote
    assert short_q is not None and long_q is not None

    session_id = readiness.stream_session_id
    if conf.get('last_stream_session_id') and session_id != conf.get('last_stream_session_id'):
        reset_breach_confirmation(state)
        conf = _confirmation_section(state)

    window_started = conf.get('window_started_at')
    if window_started is not None:
        if now - float(window_started) > BREACH_CONFIRM_MAX_WINDOW_SEC:
            reset_breach_confirmation(state)
            conf = _confirmation_section(state)
            window_started = None

    count = int(conf.get('count') or 0)
    if count > 0 and not _quote_pair_advanced(conf, short_q, long_q):
        return _summary(count, REASON_QUOTE_NOT_ADVANCED)

    if count <= 0:
        conf['count'] = 1
        conf['window_started_at'] = now
    else:
        conf['count'] = count + 1

    conf['last_short_sequence'] = short_q.sequence
    conf['last_long_sequence'] = long_q.sequence
    conf['last_short_source_epoch'] = short_q.source_event_epoch
    conf['last_long_source_epoch'] = long_q.source_event_epoch
    conf['last_stream_session_id'] = session_id

    new_count = int(conf['count'])
    if new_count < required:
        return _summary(new_count, REASON_CONFIRMATION_PENDING)
    return _summary(new_count, REASON_READY)


def evaluate_software_breach_exit(
    monitor: 'StopMonitor',
    *,
    streamer_stale: bool = False,
    mqtt_cache_stale: bool = False,
    now: Optional[float] = None,
) -> tuple[bool, QuotePairReadiness, Dict[str, Any]]:
    """Return (should_exit, readiness, confirmation_summary)."""
    state = monitor.state
    now = now if now is not None else time.time()
    if monitor.kill_switch:
        readiness = evaluate_quote_pair_readiness(
            state, monitor.prices,
            streamer_stale=streamer_stale,
            mqtt_cache_stale=mqtt_cache_stale,
            now=now,
        )
        return True, readiness, {'breach_confirmation_count': 0, 'breach_confirmation_required': BREACH_CONFIRM_OBSERVATIONS, 'software_breach_confirmed': True, 'breach_confirmation_reason': 'kill_switch'}

    readiness = evaluate_quote_pair_readiness(
        state, monitor.prices,
        streamer_stale=streamer_stale,
        mqtt_cache_stale=mqtt_cache_stale,
        now=now,
    )
    if not readiness.software_breach_ready or readiness.spread_mid is None:
        reset_breach_confirmation(state)
        return False, readiness, update_breach_confirmation(state, readiness, spread_breached=False, now=now)

    threshold = spread_breach_threshold(state)
    spread_breached = spread_breach_triggered(readiness.spread_mid, threshold)
    confirmation = update_breach_confirmation(state, readiness, spread_breached=spread_breached, now=now)
    should_exit = bool(confirmation.get('software_breach_confirmed') and spread_breached)
    return should_exit, readiness, confirmation


def readiness_to_watch_fields(
    readiness: QuotePairReadiness,
    confirmation: Dict[str, Any],
) -> Dict[str, Any]:
    fields = {
        'quote_pair_valid': readiness.quote_pair_valid,
        'quote_pair_reason': readiness.quote_pair_reason,
        'software_breach_ready': readiness.software_breach_ready,
        'short_source_epoch': readiness.short_source_epoch,
        'long_source_epoch': readiness.long_source_epoch,
        'short_sequence': readiness.short_sequence,
        'long_sequence': readiness.long_sequence,
        'pair_skew_sec': readiness.pair_skew_sec,
        'stream_session_id': readiness.stream_session_id,
        'fill_reference_epoch': readiness.fill_reference_epoch,
        'fill_grace_remaining_sec': readiness.fill_grace_remaining_sec,
        'breach_confirmation_count': confirmation.get('breach_confirmation_count', 0),
        'breach_confirmation_required': confirmation.get('breach_confirmation_required', BREACH_CONFIRM_OBSERVATIONS),
        'breach_confirmation_reason': confirmation.get('breach_confirmation_reason'),
        'software_breach_confirmed': confirmation.get('software_breach_confirmed', False),
    }
    if readiness.spread_mid is not None:
        fields['spread_mid'] = readiness.spread_mid
    return fields
