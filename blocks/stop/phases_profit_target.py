"""Profit-target exit phase for credit and debit spreads."""
from __future__ import annotations

from blocks.stop.breach import profit_target_hit, spread_mark_price
from blocks.stop.phases import PhaseAction, PhaseBase


class PhaseProfitTarget(PhaseBase):
    """Close once mark reaches configured profit target."""

    priority = 5
    name = 'phase_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        if state.get('is_scale_out_split') or (state.get('active_stop') or {}).get('type') in ('SPREAD_OCO', 'SPREAD_LIMIT'):
            return False
        exit_cfg = state.get('exit_config') or {}
        target_pct = exit_cfg.get('profit_target_pct')
        if target_pct is None:
            target_pct = state.get('profit_target_pct')
        return (
            state.get('status') == 'open'
            and target_pct is not None
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

        spread_type = getattr(monitor.stop_profile, 'spread_type', 'credit')
        mark = spread_mark_price(short_mid, long_mid, spread_type=spread_type)

        entry = state.get('entry') or {}
        entry_price = float(
            entry.get('net_credit')
            or entry.get('credit')
            or entry.get('debit')
            or short_leg.get('fill_price')
            or 0
        )
        return profit_target_hit(
            mark, entry_price, float(target_pct), spread_type=spread_type,
        )

    def execute(self, monitor) -> None:
        monitor.replace_with_limit_close(reason='profit_target_hit')


class PhaseSpreadDebitProfitTarget(PhaseBase):
    """Place a resting spread-close limit at profit_target_debit as soon as the entry is open."""

    priority = 5
    name = 'phase_spread_debit_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        exit_cfg = state.get('exit_config') or {}
        if state.get('status') != 'open':
            return False
        if exit_cfg.get('profit_target_debit') is None:
            return False
        if state.get('spread_close_order_id') or state.get('close_only_mode'):
            return False
        return True

    def _evaluate_active(self, monitor) -> str:
        return PhaseAction.MAINTENANCE

    def execute(self, monitor) -> None:
        exit_cfg = monitor.state.get('exit_config') or {}
        debit_limit = float(exit_cfg.get('profit_target_debit'))
        monitor.replace_with_spread_close(
            reason='profit_target_resting',
            debit_limit=debit_limit,
        )
