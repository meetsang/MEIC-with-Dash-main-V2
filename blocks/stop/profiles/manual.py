"""Manual spread credit & debit stop profiles — standard stop losses without OCO or IC companion breach."""
from __future__ import annotations

import logging
from blocks.stop.breach import spread_breach_triggered, spread_mark_price
from blocks.stop.phases import (
    Phase1InitialStop,
    Phase2NetCreditUpgrade,
    Phase3SpxProximityClose,
    PhaseAction,
    PhaseBase,
)
from blocks.stop.phases_profit_target import PhaseProfitTarget
from blocks.stop.stop_math import spread_breach_threshold
from blocks.stop.stop_profile import StopProfile, register_stop_profile

log = logging.getLogger(__name__)

MANUAL_CREDIT_SPREAD_PROFILE = 'manual_credit_spread'
MANUAL_DEBIT_SPREAD_PROFILE = 'manual_debit_spread'


class PhaseDebitSpreadStop(PhaseBase):
    """Software stop-loss phase for vertical debit spreads (PDS / CDS)."""

    priority = 10
    name = 'phase_debit_spread_stop'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        if state.get('status') != 'open':
            return False
        if state.get('close_only_mode') or state.get('spread_close_order_id'):
            return False
        if state.get('is_scale_out_split') or (state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT'):
            return False
        spread_type = state.get('spread_type') or getattr(getattr(monitor, 'stop_profile', None), 'spread_type', 'credit')
        if spread_type != 'debit':
            return False
        threshold = spread_breach_threshold(state)
        return threshold > 0

    def _evaluate_active(self, monitor) -> str:
        return PhaseAction.EXIT_REQUIRED if self._breach_triggered(monitor) else PhaseAction.NONE

    def _breach_triggered(self, monitor) -> bool:
        state = monitor.state
        threshold = spread_breach_threshold(state)
        if threshold <= 0:
            return False

        if hasattr(monitor.prices, 'get_quote'):
            from blocks.stop.breach_quote import evaluate_software_breach_exit

            streamer_stale = monitor._streamer_prices_stale()
            mqtt_cache_stale = monitor._mqtt_cache_stale()
            should_exit, _readiness, _confirmation = evaluate_software_breach_exit(
                monitor,
                streamer_stale=streamer_stale,
                mqtt_cache_stale=mqtt_cache_stale,
            )
            return should_exit

        short_sym = (state.get('short_leg') or {}).get('symbol', '')
        long_sym = (state.get('long_leg') or {}).get('symbol', '')
        short_mid = PhaseProfitTarget._leg_mid(monitor.prices, short_sym)
        long_mid = PhaseProfitTarget._leg_mid(monitor.prices, long_sym)
        if short_mid is None or long_mid is None:
            return False
        mark = spread_mark_price(float(short_mid), float(long_mid), spread_type='debit')
        return spread_breach_triggered(mark, threshold, spread_type='debit')

    def execute(self, monitor) -> None:
        state = monitor.state
        threshold = spread_breach_threshold(state)
        short_sym = (state.get('short_leg') or {}).get('symbol', '')
        long_sym = (state.get('long_leg') or {}).get('symbol', '')
        short_mid = PhaseProfitTarget._leg_mid(monitor.prices, short_sym)
        long_mid = PhaseProfitTarget._leg_mid(monitor.prices, long_sym)
        mark = (
            spread_mark_price(float(short_mid), float(long_mid), spread_type='debit')
            if (short_mid is not None and long_mid is not None)
            else None
        )
        log.info(
            'Debit spread software stop breach %s %s: mark=%s <= threshold=%.2f — closing spread',
            state.get('lot', '?'),
            (state.get('entry') or {}).get('side', '?'),
            f'{mark:.2f}' if mark is not None else '?',
            threshold,
        )
        monitor.replace_with_spread_close(reason='spread_stop_breach')


def manual_stop_profile() -> StopProfile:
    return StopProfile(
        name=MANUAL_CREDIT_SPREAD_PROFILE,
        spread_type='credit',
        phases=[
            PhaseProfitTarget(),
            Phase1InitialStop(),
            Phase2NetCreditUpgrade(),
            Phase3SpxProximityClose(),
        ],
        long_close_delay_sec=30,
        profit_target_short=None,
        use_oco_short_stop=False,
        adjust_to_breakeven_on_companion_breach=False,
    )


def manual_debit_stop_profile() -> StopProfile:
    return StopProfile(
        name=MANUAL_DEBIT_SPREAD_PROFILE,
        spread_type='debit',
        phases=[
            PhaseProfitTarget(),
            PhaseDebitSpreadStop(),
        ],
        long_close_delay_sec=0,
        profit_target_short=None,
        use_oco_short_stop=False,
        adjust_to_breakeven_on_companion_breach=False,
    )


register_stop_profile(MANUAL_CREDIT_SPREAD_PROFILE, manual_stop_profile)
register_stop_profile(MANUAL_DEBIT_SPREAD_PROFILE, manual_debit_stop_profile)
