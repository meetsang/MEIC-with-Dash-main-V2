"""Profit-target exit phase for credit and debit spreads."""
from __future__ import annotations

import logging
from blocks.stop.breach import profit_target_hit, spread_mark_price
from blocks.stop.phases import PhaseAction, PhaseBase

log = logging.getLogger(__name__)


class PhaseProfitTarget(PhaseBase):
    """Close once mark reaches configured profit target."""

    priority = 5
    name = 'phase_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        if state.get('manual_management') or state.get('is_scale_out_split') or (state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT', 'OCO'):
            return False
        if state.get('close_only_mode') or state.get('spread_close_order_id'):
            return False
        exit_cfg = state.get('exit_config') or {}
        target_pct = exit_cfg.get('profit_target_pct')
        if target_pct is None:
            target_pct = state.get('profit_target_pct')
        if target_pct is None:
            return False
        try:
            pct_f = float(target_pct)
        except (TypeError, ValueError):
            return False
        spread_type = state.get('spread_type') or getattr(getattr(monitor, 'stop_profile', None), 'spread_type', 'credit')
        if spread_type == 'debit':
            return state.get('status') == 'open' and pct_f > 0.0
        return state.get('status') == 'open' and pct_f > 0.0

    def _evaluate_active(self, monitor) -> str:
        return PhaseAction.EXIT_REQUIRED if self._target_hit(monitor) else PhaseAction.NONE

    @staticmethod
    def _leg_mid(prices, symbol: str) -> float | None:
        if not symbol or prices is None:
            return None
        if hasattr(prices, 'get_option_mid'):
            return prices.get_option_mid(symbol)
        if hasattr(prices, 'get_market_mid'):
            mid = prices.get_market_mid(symbol)
            if mid is not None:
                return mid
        if hasattr(prices, 'get'):
            return prices.get(symbol)
        return None

    def _target_hit(self, monitor) -> bool:
        state = monitor.state
        exit_cfg = state.get('exit_config') or {}
        target_pct = exit_cfg.get('profit_target_pct')
        if target_pct is None:
            target_pct = state.get('profit_target_pct')
        if target_pct is None:
            return False

        short_leg = state.get('short_leg') or {}
        long_leg = state.get('long_leg') or {}
        short_mid = self._leg_mid(monitor.prices, short_leg.get('symbol', ''))
        long_mid = self._leg_mid(monitor.prices, long_leg.get('symbol', ''))
        if short_mid is None or long_mid is None:
            return False

        spread_type = state.get('spread_type') or getattr(getattr(monitor, 'stop_profile', None), 'spread_type', 'credit')
        mark = spread_mark_price(short_mid, long_mid, spread_type=spread_type)

        entry = state.get('entry') or {}
        if spread_type == 'debit':
            entry_price = float(
                entry.get('net_debit')
                or entry.get('debit')
                or entry.get('limit_debit')
                or entry.get('net_credit')
                or entry.get('limit_credit')
                or 0
            )
            if entry_price <= 0:
                l_px = float(long_leg.get('fill_price') or 0)
                s_px = float(short_leg.get('fill_price') or 0)
                if l_px > 0 and s_px > 0:
                    entry_price = round(l_px - s_px, 2)
        else:
            entry_price = float(
                entry.get('net_credit')
                or entry.get('credit')
                or entry.get('limit_credit')
                or entry.get('debit')
                or short_leg.get('fill_price')
                or 0
            )
        return profit_target_hit(
            mark, entry_price, float(target_pct), spread_type=spread_type,
        )

    def execute(self, monitor) -> None:
        state = monitor.state
        spread_type = state.get('spread_type') or getattr(getattr(monitor, 'stop_profile', None), 'spread_type', 'credit')
        if spread_type == 'debit':
            monitor.replace_with_spread_close(reason='profit_target_hit')
        else:
            monitor.replace_with_limit_close(reason='profit_target_hit')


class PhaseSpreadDebitProfitTarget(PhaseBase):
    """Profit-target exit phase at configured spread debit (e.g. 0.15 debit for PCS)."""

    priority = 5
    name = 'phase_spread_debit_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        if state.get('manual_management') or state.get('is_scale_out_split') or (state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT', 'OCO'):
            return False
        exit_cfg = state.get('exit_config') or {}
        target_debit = exit_cfg.get('profit_target_debit')
        if target_debit is None:
            target_debit = state.get('profit_target_debit')
        return (
            state.get('status') == 'open'
            and target_debit is not None
            and not state.get('close_only_mode')
            and not state.get('spread_close_order_id')
        )

    def _evaluate_active(self, monitor) -> str:
        return PhaseAction.EXIT_REQUIRED if self._target_hit(monitor) else PhaseAction.NONE

    @staticmethod
    def _leg_mid(prices, symbol: str) -> float | None:
        if not symbol or prices is None:
            return None
        if hasattr(prices, 'get_option_mid'):
            return prices.get_option_mid(symbol)
        if hasattr(prices, 'get_market_mid'):
            mid = prices.get_market_mid(symbol)
            if mid is not None:
                return mid
        if hasattr(prices, 'get'):
            return prices.get(symbol)
        return None

    def _target_hit(self, monitor) -> bool:
        state = monitor.state
        exit_cfg = state.get('exit_config') or {}
        target_debit = exit_cfg.get('profit_target_debit')
        if target_debit is None:
            target_debit = state.get('profit_target_debit')
        if target_debit is None:
            return False

        short_leg = state.get('short_leg') or {}
        long_leg = state.get('long_leg') or {}
        short_sym = short_leg.get('symbol', '')
        long_sym = long_leg.get('symbol', '')
        if not short_sym or not long_sym:
            return False

        short_mid = self._leg_mid(monitor.prices, short_sym)
        long_mid = self._leg_mid(monitor.prices, long_sym)
        if short_mid is None or long_mid is None:
            return False

        spread_type = getattr(getattr(monitor, 'stop_profile', None), 'spread_type', 'credit')
        mark = spread_mark_price(short_mid, long_mid, spread_type=spread_type)

        # For credit spreads (like PCS), mark is cost to buy back (short - long).
        # Profit target is hit when mark <= target_debit.
        return mark <= float(target_debit)

    def execute(self, monitor) -> None:
        exit_cfg = monitor.state.get('exit_config') or {}
        debit_limit = float(
            exit_cfg.get('profit_target_debit')
            or monitor.state.get('profit_target_debit')
            or 0.15
        )
        short_leg = monitor.state.get('short_leg') or {}
        long_leg = monitor.state.get('long_leg') or {}
        short_mid = self._leg_mid(monitor.prices, short_leg.get('symbol', ''))
        long_mid = self._leg_mid(monitor.prices, long_leg.get('symbol', ''))
        mark_info = f'mark={short_mid - long_mid:.2f} ' if (short_mid is not None and long_mid is not None) else ''
        log.info(
            'PERMA_BULL_PCS profit target hit (%starget_debit=%.2f) — replacing with spread close',
            mark_info,
            debit_limit,
        )
        monitor.replace_with_spread_close(
            reason='profit_target_hit',
            debit_limit=debit_limit,
        )
