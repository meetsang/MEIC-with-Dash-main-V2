"""MQTT message counter for integration / test reporting."""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Dict, Optional

import paho.mqtt.client as mqtt

from common.broker_factory import get_mqtt_topic_prefix
from streaming import config as stream_config


class MqttStatsCollector:
    """Subscribe to TASTYTRADE/# and count messages per topic."""

    def __init__(self, topic_prefix: Optional[str] = None):
        self._prefix = topic_prefix or get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
        self._counts: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()
        self._client: Optional[mqtt.Client] = None
        self._started_at: Optional[float] = None

    def start(self) -> None:
        self._client = mqtt.Client()
        self._client.on_message = self._on_message
        self._client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
        self._client.subscribe(f'{self._prefix}#')
        self._client.loop_start()
        self._started_at = time.time()

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()

    def _on_message(self, client, userdata, msg) -> None:
        if not msg.topic.startswith(self._prefix):
            return
        sym = msg.topic[len(self._prefix):]
        with self._lock:
            self._counts[sym] += 1

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def total(self) -> int:
        with self._lock:
            return sum(self._counts.values())

    def report_lines(self, watch_symbols: Optional[list] = None) -> list[str]:
        snap = self.snapshot()
        lines = [f'  MQTT total messages: {sum(snap.values())}']
        if watch_symbols:
            for sym in watch_symbols:
                lines.append(f'  MQTT {sym}: {snap.get(sym, 0)}')
        if snap:
            top = sorted(snap.items(), key=lambda x: -x[1])[:10]
            lines.append('  MQTT top topics:')
            for sym, cnt in top:
                lines.append(f'    {sym}: {cnt}')
        return lines
