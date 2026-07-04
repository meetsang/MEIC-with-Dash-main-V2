"""Plugin phase system for stop_monitor."""
from __future__ import annotations

import datetime
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

import meic0dte.app.config as app_config
from blocks.stop.breach import spread_breach_triggered, spread_mark_price

if TYPE_CHECKING:
    from blocks.stop.monitor import StopMonitor

log = logging.getLogger(__name__)


class PhaseBase(ABC):
    priority: int = 100
    name: str = 'base'

    @abstractmethod
    def should_activate(self, monitor: 'StopMonitor') -> bool:
        ...

    @abstractmethod
    def execute(self, monitor: 'StopMonitor') -> None:
        ...


class Phase1InitialStop(PhaseBase):
    """Monitor initial 2x short stop; handle breach and unexpected states."""

    priority = 10
    name = 'phase1_initial_stop'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        return monitor.state.get('status') == 'open'

    def execute(self, monitor: 'StopMonitor') -> None:
        from blocks.stop import state as state_mod

        state = monitor.state
        stop = state.get('active_stop') or {}
        if stop.get('status') in ('filled', 'cancelled', 'rejected'):
            monitor.handle_stop_order_update(stop)
            return

        if stop.get('type') == 'LIMIT' and stop.get('order_id'):
            monitor._sync_active_close_order()
            stop = state.get('active_stop') or {}
            if stop.get('status') == 'filled':
                return
            target = monitor.breach_short_limit_target(state['short_leg']['symbol'])
            if target is not None and monitor.needs_breach_limit_reprice(stop, target):
                monitor.replace_with_limit_close(reason='breach_limit_reprice')
            return

        short_p = monitor.prices.get(state['short_leg']['symbol'])
        long_p = monitor.prices.get(state['long_leg']['symbol'])
        if short_p is not None and long_p is not None and state_mod.section(state, 'active_stop').get('order_id'):
            if stop.get('type') != 'STOP_LIMIT':
                return
            spread_price = spread_mark_price(short_p, long_p)
            stop_price = monitor.current_stop_price()
            if spread_breach_triggered(spread_price, stop_price) or monitor.kill_switch:
                log.info(
                    'Software breach %s %s: spread %.2f >= threshold %.2f (2× credit + offset)',
                    state.get('lot', '?'),
                    (state.get('entry') or {}).get('side', '?'),
                    spread_price,
                    stop_price,
                )
                monitor.replace_with_limit_close(reason='spread_stop_breach')
                return

        if not state_mod.section(state, 'active_stop').get('order_id'):
            monitor._ensure_stop_for_filled_qty()


class Phase2NetCreditUpgrade(PhaseBase):
    """When long leg <= $0.05, switch stop to 2x net credit."""

    priority = 20
    name = 'phase2_net_credit_upgrade'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        if monitor.state.get('status') != 'open':
            return False
        if monitor.state['phases'].get('short_stoplmt_replaced'):
            return False
        long_p = monitor.prices.get(monitor.state['long_leg']['symbol'])
        return long_p is not None and long_p <= 0.05

    def execute(self, monitor: 'StopMonitor') -> None:
        monitor.upgrade_to_spread_stop()


class Phase3SpxProximityClose(PhaseBase):
    """At 14:51 CST, market-close short if SPX within STRK_IDX_DIFF of strike."""

    priority = 30
    name = 'phase3_spx_proximity'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        if monitor.state.get('status') != 'open':
            return False
        from meic0dte.app.utilities import central_time

        now = central_time()
        trigger = datetime.time(14, app_config.STRK_CHK_MIN, 0)
        return now >= trigger

    def execute(self, monitor: 'StopMonitor') -> None:
        monitor.execute_spx_proximity_close()


def default_phases() -> List[PhaseBase]:
    import blocks.stop.profiles  # noqa: F401 — ensure registry populated
    from blocks.stop.profiles.meic import meic_stop_profile

    return meic_stop_profile().phases
