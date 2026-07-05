"""Long leg STC chase after short close (Condition 2 phase 2)."""
from __future__ import annotations

import logging
import os

from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.handlers.monitor_adapter import MonitorAdapter
from blocks.stop.v3.observability import timed_exit_step
from blocks.stop.v3.recovery import mark_exit_progress
from blocks.stop.v3.trade_slot import TradeSlot, save_slot

log = logging.getLogger(__name__)


class LongChaseHandler(MonitorAdapter):
    def __init__(self, slot: TradeSlot, broker, prices, lane: BrokerLane, alert_listener=None):
        self.slot = slot
        self.broker = broker
        self.prices = prices
        self.lane = lane
        self.alert_listener = alert_listener
        self._trade_id = os.path.basename(slot.path)

    def run(self) -> None:
        def _pipeline() -> None:
            with timed_exit_step(
                path=self.slot.path,
                handler='long_chase',
                step='tick',
            ):
                mon = self.monitor()
                mark_exit_progress(self.slot.state, 'long_chase_tick')
                mon._chase_long_close()
                self.sync_state_from_monitor()
                save_slot(self.slot)
                if self.slot.state.get('status') == 'closed':
                    log.info('Long chase complete — closed %s', self.slot.path)
                else:
                    log.debug('Long chase tick — still closing %s', self.slot.path)

        self.lane.run(self._trade_id, _pipeline)
