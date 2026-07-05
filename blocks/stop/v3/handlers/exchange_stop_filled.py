"""Condition 2 — exchange stop filled → long chase (V3 §5.2)."""
from __future__ import annotations

import logging
import os

from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.handlers.monitor_adapter import MonitorAdapter
from blocks.stop.v3.observability import timed_exit_step
from blocks.stop.v3.recovery import mark_exit_progress, mark_exit_started
from blocks.stop.v3.trade_slot import TradeSlot, save_slot

log = logging.getLogger(__name__)


class ExchangeStopFilledHandler(MonitorAdapter):
    def __init__(self, slot: TradeSlot, broker, prices, lane: BrokerLane, alert_listener=None):
        self.slot = slot
        self.broker = broker
        self.prices = prices
        self.lane = lane
        self.alert_listener = alert_listener
        self._trade_id = os.path.basename(slot.path)

    def run(self, *, stop: dict | None = None, broker_result=None) -> None:
        active = stop or self.slot.state.get('active_stop') or {}
        if not active.get('order_id'):
            return

        if not self.slot.state.get('exit_started_at'):
            mark_exit_started(
                self.slot.state,
                step='exchange_stop_filled',
                mechanism='exchange_stop',
            )
        self.slot.state['exit_handler'] = 'exchange_stop'
        if not self.slot.state.get('close_mechanism'):
            self.slot.state['close_mechanism'] = 'exchange_stop'

        def _pipeline() -> None:
            with timed_exit_step(
                path=self.slot.path,
                handler='exchange_stop',
                step='pipeline',
            ):
                mon = self.monitor()
                mark_exit_progress(self.slot.state, 'stop_filled_record')
                save_slot(self.slot)
                mon.handle_stop_order_update(active, broker_result=broker_result)
                self.sync_state_from_monitor()
                mark_exit_progress(self.slot.state, 'short_closed_waiting_long')
                save_slot(self.slot)
                log.info(
                    'Exchange stop filled — long chase scheduled in %ss (%s)',
                    mon.long_close_delay_sec,
                    self.slot.path,
                )

        self.lane.run(self._trade_id, _pipeline)
