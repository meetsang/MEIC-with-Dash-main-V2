"""MQTT streamer topic helpers and session identifiers."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from common.market_watch import mqtt_topic_from_dxlink
from meic0dte.app.utilities import central_now

META_TOPIC_SUFFIX = '__META'
SESSION_TOPIC = '__SESSION'
HEARTBEAT_TOPIC = '__HEARTBEAT'


def make_stream_session_id() -> str:
    stamp = central_now().strftime('%Y%m%d-%H%M%S')
    micro = int(time.time() * 1_000_000) % 1_000_000
    return f'{stamp}-{os.getpid()}-{micro}'


def legacy_republish_enabled() -> bool:
    return os.environ.get('TT_LEGACY_REPUBLISH_LAST_MIDS', 'false').strip().lower() in (
        '1',
        'true',
        'yes',
    )


def heartbeat_interval_sec() -> float:
    try:
        return float(os.environ.get('MQTT_HEARTBEAT_INTERVAL_SEC', '5'))
    except (TypeError, ValueError):
        return 5.0


def meta_topic_for(symbol: str) -> str:
    return f'{symbol}{META_TOPIC_SUFFIX}'


def build_quote_meta(
    *,
    symbol: str,
    source_event_epoch: float,
    stream_session_id: str,
    subscription_epoch: float,
    sequence: int,
    event_kind: str,
    published_epoch: float | None = None,
) -> Dict[str, Any]:
    return {
        'symbol': symbol,
        'source_event_epoch': round(float(source_event_epoch), 6),
        'published_epoch': round(float(published_epoch or time.time()), 6),
        'stream_session_id': stream_session_id,
        'subscription_epoch': round(float(subscription_epoch), 6),
        'sequence': int(sequence),
        'event_kind': event_kind,
    }


def build_session_payload(
    *,
    stream_session_id: str,
    started_epoch: float | None = None,
    symbols_with_quotes: int = 0,
) -> Dict[str, Any]:
    return {
        'stream_session_id': stream_session_id,
        'started_epoch': round(float(started_epoch or time.time()), 6),
        'event_kind': 'session',
        'symbols_with_quotes': int(symbols_with_quotes),
    }


def build_heartbeat_payload(
    *,
    stream_session_id: str,
    symbols_with_quotes: int = 0,
    published_epoch: float | None = None,
) -> Dict[str, Any]:
    return {
        'stream_session_id': stream_session_id,
        'published_epoch': round(float(published_epoch or time.time()), 6),
        'event_kind': 'heartbeat',
        'symbols_with_quotes': int(symbols_with_quotes),
    }


class StreamPublishState:
    """Per-streamer-session quote publish bookkeeping."""

    def __init__(self, stream_session_id: Optional[str] = None):
        self.stream_session_id = stream_session_id or make_stream_session_id()
        self.started_epoch = time.time()
        self.sequences: Dict[str, int] = {}
        self.subscription_epochs: Dict[str, float] = {}
        self.last_mids: Dict[str, float] = {}
        self.last_genuine_meta: Dict[str, Dict[str, Any]] = {}

    def note_subscriptions(self, symbols) -> None:
        now = time.time()
        for sym in symbols:
            mqtt_sym = mqtt_topic_from_dxlink(sym)
            if mqtt_sym not in self.subscription_epochs:
                self.subscription_epochs[mqtt_sym] = now

    def subscription_epoch_for(self, symbol: str) -> float:
        mqtt_sym = mqtt_topic_from_dxlink(symbol)
        if mqtt_sym not in self.subscription_epochs:
            self.subscription_epochs[mqtt_sym] = time.time()
        return self.subscription_epochs[mqtt_sym]

    def next_sequence(self, symbol: str) -> int:
        mqtt_sym = mqtt_topic_from_dxlink(symbol)
        seq = self.sequences.get(mqtt_sym, 0) + 1
        self.sequences[mqtt_sym] = seq
        return seq

    def record_genuine(self, symbol: str, mid: float, meta: Dict[str, Any]) -> None:
        mqtt_sym = mqtt_topic_from_dxlink(symbol)
        self.last_mids[mqtt_sym] = mid
        self.last_genuine_meta[mqtt_sym] = dict(meta)
