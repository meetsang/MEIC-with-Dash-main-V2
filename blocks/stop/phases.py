"""Plugin phase system for stop_monitor."""
from __future__ import annotations

import datetime
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, List

import meic0dte.app.config as app_config
from blocks.stop.breach import spread_mark_price

if TYPE_CHECKING:
    from blocks.stop.monitor import StopMonitor

log = logging.getLogger(__name__)


class PhaseAction:
    """Supervisor routing — not an exit signal by itself."""

    NONE = 'none'
    MAINTENANCE = 'maintenance'
    EXIT_REQUIRED = 'exit_required'


class PhaseBase(ABC):
    priority: int = 100
    name: str = 'base'

    @abstractmethod
    def should_activate(self, monitor: 'StopMonitor') -> bool:
        ...

    @abstractmethod
    def execute(self, monitor: 'StopMonitor') -> None:
        ...

    def evaluate(self, monitor: 'StopMonitor') -> str:
        """Return PhaseAction — only EXIT_REQUIRED may start an exit pipeline."""
        if not self.should_activate(monitor):
            return PhaseAction.NONE
        return self._evaluate_active(monitor)

    def _evaluate_active(self, monitor: 'StopMonitor') -> str:
        return PhaseAction.MAINTENANCE


class Phase1InitialStop(PhaseBase):
    """Monitor initial 2x short stop; handle breach and unexpected states."""

    priority = 10
    name = 'phase1_initial_stop'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        if monitor.state.get('is_scale_out_split') or (monitor.state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT'):
            return False
        return monitor.state.get('status') == 'open'

    def _evaluate_active(self, monitor: 'StopMonitor') -> str:
        if self._exit_required(monitor):
            return PhaseAction.EXIT_REQUIRED
        if self._maintenance_needed(monitor):
            return PhaseAction.MAINTENANCE
        return PhaseAction.NONE

    def _exit_required(self, monitor: 'StopMonitor') -> bool:
        from blocks.stop import state as state_mod
        from blocks.stop.breach_quote import evaluate_software_breach_exit

        state = monitor.state
        active = state.get('active_stop') or {}
        if active.get('type') == 'LIMIT' and active.get('order_id'):
            return False
        if not active.get('order_id') or active.get('type') != 'STOP_LIMIT':
            return False

        streamer_stale = monitor._streamer_prices_stale()
        mqtt_cache_stale = monitor._mqtt_cache_stale()
        should_exit, _readiness, _confirmation = evaluate_software_breach_exit(
            monitor,
            streamer_stale=streamer_stale,
            mqtt_cache_stale=mqtt_cache_stale,
        )
        return should_exit

    def _maintenance_needed(self, monitor: 'StopMonitor') -> bool:
        from blocks.stop import state as state_mod

        state = monitor.state
        stop = state.get('active_stop') or {}
        if stop.get('status') in ('filled', 'cancelled', 'rejected'):
            return True
        if stop.get('type') == 'LIMIT' and stop.get('order_id'):
            return True
        if not state_mod.section(state, 'active_stop').get('order_id'):
            return True
        return False

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
            from blocks.stop.breach_quote import evaluate_software_breach_exit

            streamer_stale = monitor._streamer_prices_stale()
            mqtt_cache_stale = monitor._mqtt_cache_stale()
            should_exit, readiness, confirmation = evaluate_software_breach_exit(
                monitor,
                streamer_stale=streamer_stale,
                mqtt_cache_stale=mqtt_cache_stale,
            )
            if should_exit and readiness.spread_mid is not None:
                stop_price = monitor.current_stop_price()
                log.info(
                    'Software breach %s %s: spread %.2f >= threshold %.2f (2× credit + offset) confirmations=%s/%s',
                    state.get('lot', '?'),
                    (state.get('entry') or {}).get('side', '?'),
                    readiness.spread_mid,
                    stop_price,
                    confirmation.get('breach_confirmation_count'),
                    confirmation.get('breach_confirmation_required'),
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
        if monitor.state.get('is_scale_out_split') or (monitor.state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT'):
            return False
        if monitor.state['phases'].get('short_stoplmt_replaced'):
            return False
        long_p = monitor.prices.get(monitor.state['long_leg']['symbol'])
        return long_p is not None and long_p <= 0.05

    def _evaluate_active(self, monitor: 'StopMonitor') -> str:
        return PhaseAction.MAINTENANCE

    def execute(self, monitor: 'StopMonitor') -> None:
        monitor.upgrade_to_spread_stop()


class Phase3SpxProximityClose(PhaseBase):
    """At 14:51 CST, market-close short if SPX within STRK_IDX_DIFF of strike."""

    priority = 30
    name = 'phase3_spx_proximity'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        if monitor.state.get('status') != 'open':
            return False
        if monitor.state.get('is_scale_out_split') or (monitor.state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT'):
            return False
        from meic0dte.app.utilities import central_time

        now = central_time()
        trigger = datetime.time(14, app_config.STRK_CHK_MIN, 0)
        return now >= trigger

    def _evaluate_active(self, monitor: 'StopMonitor') -> str:
        if self._proximity_triggered(monitor):
            return PhaseAction.EXIT_REQUIRED
        return PhaseAction.NONE

    def _proximity_triggered(self, monitor: 'StopMonitor') -> bool:
        spx = monitor.prices.get_spx()
        if spx is None:
            return False
        short_strike = monitor.state['short_leg']['strike']
        side = monitor.state['entry']['side']
        diff = app_config.STRK_IDX_DIFF
        if side == 'C' and short_strike - spx <= diff:
            return True
        if side == 'P' and spx - short_strike <= diff:
            return True
        return False

    def execute(self, monitor: 'StopMonitor') -> None:
        monitor.execute_spx_proximity_close()


class PhaseCompanionBreachAdjustment(PhaseBase):
    """When companion IC tranche breaches, tighten short stop to breakeven."""

    priority = 8
    name = 'phase_companion_breach_adjustment'

    def should_activate(self, monitor: 'StopMonitor') -> bool:
        if monitor.state.get('status') != 'open':
            return False
        if not getattr(monitor.stop_profile, 'adjust_to_breakeven_on_companion_breach', False):
            return False
        from common.meic_settings import is_companion_tightening_enabled
        from blocks.stop.monitor import _trades_root_for_path
        trades_root = _trades_root_for_path(monitor.json_path) if getattr(monitor, 'json_path', None) else None
        if not is_companion_tightening_enabled(root=trades_root):
            return False
        if monitor.state.get('phases', {}).get('breakeven_tightened'):
            return False
        from blocks.stop.companion_breach import find_companion_trade_state, is_companion_breached

        companion_state = find_companion_trade_state(monitor.state)
        return is_companion_breached(companion_state)

    def _evaluate_active(self, monitor: 'StopMonitor') -> str:
        return PhaseAction.MAINTENANCE

    def execute(self, monitor: 'StopMonitor') -> None:
        monitor.adjust_stop_to_breakeven(reason='companion_breached')


def default_phases() -> List[PhaseBase]:
    import blocks.stop.profiles  # noqa: F401 — ensure registry populated
    from blocks.stop.profiles.meic import meic_stop_profile

    return meic_stop_profile().phases
