"""MQTT quote-pair validation for entry scans (Phase 5)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from blocks.entry.entry_scan_config import (
    MAX_MQTT_ENTRY_PAIR_SKEW_SEC,
    MAX_MQTT_ENTRY_QUOTE_AGE_SEC,
    MQTT_ENTRY_SPREAD_WIDTH_TOLERANCE,
    MQTT_REQUIRE_POST_SCAN_QUOTE,
)
from blocks.stop.breach import spread_mark_price
from blocks.stop.breach_quote import (
    REASON_MISSING_QUOTE,
    REASON_NEGATIVE_SPREAD,
    REASON_NONPOSITIVE_PRICE,
    REASON_OLD_SESSION,
    REASON_PAIR_SKEW,
    REASON_PRE_SUBSCRIPTION,
    REASON_READY,
    REASON_REPLAY_EVENT,
    REASON_SOURCE_STALE,
    REASON_SPREAD_OVER_WIDTH,
)
from common.market_quote import (
    REPLAY_EVENT_KIND,
    QuoteSnapshot,
    is_genuine_event_kind,
    quote_is_pre_subscription,
)

if TYPE_CHECKING:
    from common.mqtt_prices import MqttPriceCache


REASON_PRE_SCAN = 'pre_scan'


@dataclass(frozen=True)
class EntryPairReadiness:
    quote_pair_valid: bool
    quote_pair_reason: str
    short_mid: Optional[float]
    long_mid: Optional[float]
    pair_skew_sec: Optional[float] = None


def _lookup_quote(cache: 'MqttPriceCache', symbol: str) -> Optional[QuoteSnapshot]:
    return cache.get_quote(
        symbol,
        require_current_session=True,
        allow_override=False,
        allow_pre_subscription=False,
    )


def evaluate_entry_mqtt_pair(
    cache: 'MqttPriceCache',
    short_sym: str,
    long_sym: str,
    *,
    scan_request_epoch: float,
    spread_width: int,
    now: Optional[float] = None,
) -> EntryPairReadiness:
    """Validate short/long MQTT QuoteSnapshot pair for entry candidate selection."""
    now = now if now is not None else time.time()
    empty = EntryPairReadiness(
        quote_pair_valid=False,
        quote_pair_reason=REASON_MISSING_QUOTE,
        short_mid=None,
        long_mid=None,
    )

    current_session = cache.current_stream_session_id()
    short_q = _lookup_quote(cache, short_sym)
    long_q = _lookup_quote(cache, long_sym)

    if short_q is None or long_q is None:
        for sym in (short_sym, long_sym):
            if cache.last_event_kind(sym) == REPLAY_EVENT_KIND:
                return EntryPairReadiness(False, REASON_REPLAY_EVENT, None, None)
        return empty

    if current_session:
        if short_q.stream_session_id != current_session or long_q.stream_session_id != current_session:
            return EntryPairReadiness(False, REASON_OLD_SESSION, None, None)

    for quote in (short_q, long_q):
        if not is_genuine_event_kind(quote.event_kind):
            if quote.event_kind == REPLAY_EVENT_KIND:
                return EntryPairReadiness(False, REASON_REPLAY_EVENT, None, None)
            return EntryPairReadiness(False, REASON_MISSING_QUOTE, None, None)

    if MQTT_REQUIRE_POST_SCAN_QUOTE:
        if short_q.source_event_epoch < scan_request_epoch or long_q.source_event_epoch < scan_request_epoch:
            return EntryPairReadiness(False, REASON_PRE_SCAN, None, None)

    if quote_is_pre_subscription(short_q) or quote_is_pre_subscription(long_q):
        return EntryPairReadiness(False, REASON_PRE_SUBSCRIPTION, None, None)

    for quote in (short_q, long_q):
        if quote.subscription_epoch and quote.source_event_epoch < quote.subscription_epoch:
            return EntryPairReadiness(False, REASON_PRE_SUBSCRIPTION, None, None)

    short_age = now - short_q.source_event_epoch
    long_age = now - long_q.source_event_epoch
    if short_age > MAX_MQTT_ENTRY_QUOTE_AGE_SEC or long_age > MAX_MQTT_ENTRY_QUOTE_AGE_SEC:
        return EntryPairReadiness(False, REASON_SOURCE_STALE, None, None)

    pair_skew = abs(short_q.source_event_epoch - long_q.source_event_epoch)
    if pair_skew > MAX_MQTT_ENTRY_PAIR_SKEW_SEC:
        return EntryPairReadiness(
            False, REASON_PAIR_SKEW, None, None, pair_skew_sec=round(pair_skew, 3),
        )

    if short_q.price <= 0 or long_q.price <= 0:
        return EntryPairReadiness(
            False, REASON_NONPOSITIVE_PRICE, short_q.price, long_q.price,
            pair_skew_sec=round(pair_skew, 3),
        )

    spread_mid = spread_mark_price(short_q.price, long_q.price)
    if spread_mid < 0:
        return EntryPairReadiness(
            False, REASON_NEGATIVE_SPREAD, short_q.price, long_q.price,
            pair_skew_sec=round(pair_skew, 3),
        )

    if spread_mid > spread_width + MQTT_ENTRY_SPREAD_WIDTH_TOLERANCE:
        return EntryPairReadiness(
            False, REASON_SPREAD_OVER_WIDTH, short_q.price, long_q.price,
            pair_skew_sec=round(pair_skew, 3),
        )

    return EntryPairReadiness(
        quote_pair_valid=True,
        quote_pair_reason=REASON_READY,
        short_mid=short_q.price,
        long_mid=long_q.price,
        pair_skew_sec=round(pair_skew, 3),
    )
