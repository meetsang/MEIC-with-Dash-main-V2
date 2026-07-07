"""Condition 1 — software breach / phase execution (V3 §5.1)."""
from __future__ import annotations

import logging
import os

from blocks.stop.phases import PhaseBase
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.handlers.monitor_adapter import MonitorAdapter
from blocks.stop.v3.observability import timed_exit_step
from blocks.stop.v3.recovery import exit_action_confirmed, mark_exit_progress, mark_exit_started
from blocks.stop.v3.trade_slot import TradeSlot, save_slot

log = logging.getLogger(__name__)


class SoftwareBreachHandler(MonitorAdapter):
    def __init__(
        self,
        slot: TradeSlot,
        broker,
        prices,
        lane: BrokerLane,
        phase: PhaseBase,
        alert_listener=None,
    ):
        self.slot = slot
        self.broker = broker
        self.prices = prices
        self.lane = lane
        self.phase = phase
        self.alert_listener = alert_listener
        self._trade_id = os.path.basename(slot.path)

    def run(self) -> None:
        mechanism = f'breach_{self.phase.name}'

        def _pipeline() -> None:
            with timed_exit_step(
                path=self.slot.path,
                handler=mechanism,
                step='pipeline',
            ):
                mon = self.monitor()
                mark_exit_progress(self.slot.state, f'phase_execute:{self.phase.name}')
                save_slot(self.slot)
                log.info(
                    'Software breach handler %s for %s',
                    self.phase.name,
                    self.slot.path,
                )
                self.phase.execute(mon)
                self.sync_state_from_monitor()
                if exit_action_confirmed(self.slot.state):
                    if not self.slot.state.get('exit_started_at'):
                        mark_exit_started(
                            self.slot.state,
                            step=f'breach_{self.phase.name}',
                            mechanism=mechanism,
                        )
                    if not self.slot.state.get('exit_handler'):
                        self.slot.state['exit_handler'] = mechanism
                mark_exit_progress(self.slot.state, f'phase_done:{self.phase.name}')
                save_slot(self.slot)

        self.lane.run(self._trade_id, _pipeline)
