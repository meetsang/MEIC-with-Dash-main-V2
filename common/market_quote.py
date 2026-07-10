"""MQTT quote provenance model for trading decisions."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import FrozenSet

GENUINE_EVENT_KINDS: FrozenSet[str] = frozenset({'dxlink_quote', 'dxlink_trade'})
REPLAY_EVENT_KIND = 'replay'
HEARTBEAT_EVENT_KIND = 'heartbeat'
SESSION_EVENT_KIND = 'session'


@dataclass(frozen=True)
class QuoteSnapshot:
    symbol: str
    price: float
    source_event_epoch: float
    received_epoch: float
    published_epoch: float
    stream_session_id: str
    subscription_epoch: float
    sequence: int
    event_kind: str
    source: str = 'mqtt_dxlink'

    @property
    def source_age_sec(self) -> float:
        return max(0.0, time.time() - self.source_event_epoch)

    @property
    def mid(self) -> float:
        return self.price


def is_genuine_event_kind(event_kind: str) -> bool:
    return event_kind in GENUINE_EVENT_KINDS


def quote_is_pre_subscription(
    snapshot: QuoteSnapshot,
    *,
    tolerance_sec: float = 0.001,
) -> bool:
    return snapshot.source_event_epoch + tolerance_sec < snapshot.subscription_epoch
