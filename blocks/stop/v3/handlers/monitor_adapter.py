"""Shared StopMonitor adapter for V3 exit handlers."""
from __future__ import annotations

import queue
from typing import Any, Optional

from brokers.base import BrokerBase
from blocks.stop.alerts import AlertListener
from blocks.stop.monitor import StopMonitor
from blocks.stop.v3.trade_slot import TradeSlot


class MonitorAdapter:
    broker: BrokerBase
    prices: Any
    lane: Any
    alert_listener: Optional[AlertListener]
    slot: TradeSlot

    def monitor(self) -> StopMonitor:
        if self.slot.legacy_monitor is None:
            fill_q: Optional[queue.Queue] = None
            active = self.slot.state.get('active_stop') or {}
            oid = active.get('order_id')
            if oid and self.alert_listener:
                try:
                    fill_q = self.alert_listener.register(str(oid))
                except Exception:
                    pass
            mon = StopMonitor(
                self.slot.path,
                self.broker,
                self.prices,
                fill_queue=fill_q,
                alert_listener=self.alert_listener,
            )
            mon._skip_dashboard_commands = True
            self.slot.legacy_monitor = mon
        self.slot.legacy_monitor.state = self.slot.state
        return self.slot.legacy_monitor

    def sync_state_from_monitor(self) -> None:
        if self.slot.legacy_monitor is not None:
            self.slot.state = self.slot.legacy_monitor.state
