"""Inverse MEIC — twin debit spreads with profit target multiple and EOD square-off.

Defined risk capped at entry debit paid (~$0.80 - $1.10).
No stop-loss order placed ("let it bleed" through intraday chops).
Closes on profit target multiple (default 3.0x) or at 14:50 CT EOD square-off.
"""
from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Optional

from blocks.stop.phases import PhaseAction, PhaseBase
from blocks.stop.stop_profile import StopProfile, register_stop_profile
from meic0dte.app.utilities import central_time

log = logging.getLogger(__name__)

INVERSE_MEIC_PROFILE = 'inverse_meic_debit_spread'


class PhaseDebitMultipleProfitTarget(PhaseBase):
    """Profit target exit when debit spread mark reaches a multiple of entry debit (default 3.0x).

    Also handles 14:50 CT EOD settlement square-off for open positions with residual value.
    """

    priority = 5
    name = 'phase_debit_multiple_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        if state.get('status') != 'open':
            return False
        return True

    @staticmethod
    def _leg_mid(prices, symbol: str) -> Optional[float]:
        if not symbol or prices is None:
            return None
        if hasattr(prices, 'get_option_mid'):
            mid = prices.get_option_mid(symbol)
            if mid is not None:
                return mid
        if hasattr(prices, 'get_market_mid'):
            mid = prices.get_market_mid(symbol)
            if mid is not None:
                return mid
        if hasattr(prices, 'get'):
            return prices.get(symbol)
        return None

    def _get_spread_mark_and_target(self, monitor) -> tuple[float, float, float, str]:
        """Compute current mark, entry debit, target multiple, and exit reason if triggered.

        Returns: (current_mark, entry_debit, target_price, trigger_reason)
        """
        state = monitor.state
        short_leg = state.get('short_leg') or {}
        long_leg = state.get('long_leg') or {}
        short_sym = short_leg.get('symbol', '')
        long_sym = long_leg.get('symbol', '')

        short_mid = self._leg_mid(monitor.prices, short_sym)
        long_mid = self._leg_mid(monitor.prices, long_sym)

        if short_mid is None or long_mid is None:
            return 0.0, 0.0, 0.0, ''

        # For a vertical debit spread: value = long_mid - short_mid
        current_mark = round(float(long_mid) - float(short_mid), 2)

        entry = state.get('entry') or {}
        entry_debit = float(
            entry.get('debit')
            or entry.get('net_debit')
            or entry.get('limit_debit')
            or entry.get('fill_price')
            or 0.0
        )
        if entry_debit <= 0:
            long_fill = float(long_leg.get('fill_price') or 0.0)
            short_fill = float(short_leg.get('fill_price') or 0.0)
            if long_fill > 0 and short_fill > 0:
                entry_debit = round(long_fill - short_fill, 2)
        if entry_debit <= 0:
            entry_debit = 0.90  # default fallback if unrecorded

        exit_cfg = state.get('exit_config') or {}
        target_multiple = float(
            exit_cfg.get('profit_target_multiple')
            or state.get('profit_target_multiple')
            or 3.0
        )
        target_price = round(entry_debit * target_multiple, 2)

        reason = ''
        if current_mark >= target_price:
            reason = 'profit_target_multiple_hit'
        else:
            # Check 14:50 CT EOD settlement square-off (11:50 CT on NYSE early-close half-days)
            from common.market_hours import effective_session_time_ct

            eod_time_str = exit_cfg.get('eod_exit_time_cst', '14:50')
            now_dt = central_time()
            eod_time = effective_session_time_ct(eod_time_str, now_dt.date(), default=time(14, 50))
            now_ct = now_dt.time()
            if now_ct >= eod_time:
                if current_mark > 0.10:
                    reason = 'eod_square_off'

        return current_mark, entry_debit, target_price, reason

    def _evaluate_active(self, monitor) -> str:
        current_mark, entry_debit, target_price, reason = self._get_spread_mark_and_target(monitor)
        return PhaseAction.EXIT_REQUIRED if bool(reason) else PhaseAction.NONE

    def execute(self, monitor) -> None:
        current_mark, entry_debit, target_price, reason = self._get_spread_mark_and_target(monitor)
        if not reason:
            return
        log.info(
            'INVERSE_MEIC EXIT triggered: reason=%s mark=%.2f target=%.2f entry=%.2f',
            reason, current_mark, target_price, entry_debit,
        )
        monitor.replace_with_spread_close(reason=reason, debit_limit=current_mark)


def inverse_meic_stop_profile() -> StopProfile:
    return StopProfile(
        name=INVERSE_MEIC_PROFILE,
        spread_type='debit',
        phases=[PhaseDebitMultipleProfitTarget()],
        long_close_delay_sec=0,
    )


register_stop_profile(INVERSE_MEIC_PROFILE, inverse_meic_stop_profile)
