"""Per-strategy daily loss cap (portfolio guard)."""
from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from common.history_adapter import is_skyline_trade
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy
from strategies.skyline.settlement import pnl_points_to_dollars

log = logging.getLogger(__name__)

_CLOSE_ACTION = 'halt_and_close'
_HALT_ONLY_ACTION = 'halt_only'


@dataclass
class PortfolioGuardConfig:
    strategy_name: str
    max_daily_loss: Optional[float] = None
    action: str = _CLOSE_ACTION


@dataclass
class GuardStatus:
    breached: bool
    total_pnl: float = 0.0
    reason: str = ''


def halt_file_path(strategy_name: str, root: str, for_date: Optional[date] = None) -> str:
    d = (for_date or date.today()).isoformat()
    return os.path.join(active_dir_for_strategy(strategy_name, root), f'halted_{d}.json')


def is_strategy_halted(strategy_name: str, root: str) -> bool:
    return os.path.isfile(halt_file_path(strategy_name, root))


def write_halt_file(strategy_name: str, root: str, *, reason: str, action: str) -> str:
    path = halt_file_path(strategy_name, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'strategy': strategy_name,
        'date': date.today().isoformat(),
        'reason': reason,
        'action': action,
    }
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.flush()
    os.replace(tmp, path)
    return path


class PortfolioGuard:
    """Aggregate P/L for one strategy and halt when max_daily_loss is breached."""

    def __init__(self, config: PortfolioGuardConfig, *, root: str):
        self.config = config
        self.root = root

    def _sum_trade_pnl(self, trade: Dict[str, Any]) -> float:
        if is_skyline_trade(trade):
            qty = int(trade.get('quantity') or 1)
            if trade.get('realized_pnl_total') is not None:
                return pnl_points_to_dollars(trade['realized_pnl_total'], qty)
            if trade.get('unrealized_pnl_total') is not None:
                return pnl_points_to_dollars(trade['unrealized_pnl_total'], qty)
            total = 0.0
            for struct in trade.get('structures') or []:
                if struct.get('realized_pnl') is not None:
                    total += pnl_points_to_dollars(struct['realized_pnl'], qty)
                elif struct.get('unrealized_pnl') is not None:
                    total += pnl_points_to_dollars(struct['unrealized_pnl'], qty)
            return total
        if trade.get('realized_pnl') is not None:
            return float(trade['realized_pnl'])
        if trade.get('unrealized_pnl_total') is not None:
            return float(trade['unrealized_pnl_total'])
        if trade.get('unrealized_pnl') is not None:
            return float(trade['unrealized_pnl'])
        pnl = trade.get('pnl')
        return float(pnl) if pnl is not None else 0.0

    def _iter_today_trades(self) -> List[Dict[str, Any]]:
        today = date.today().isoformat()
        trades: List[Dict[str, Any]] = []
        for base in (
            active_dir_for_strategy(self.config.strategy_name, self.root),
            history_dir_for_strategy(self.config.strategy_name, self.root),
        ):
            if not os.path.isdir(base):
                continue
            for path in glob.glob(os.path.join(base, '*.json')):
                if 'session_' in os.path.basename(path) or 'halted_' in os.path.basename(path):
                    continue
                try:
                    with open(path, encoding='utf-8') as f:
                        trade = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if str(trade.get('date', ''))[:10] == today or today in os.path.basename(path):
                    trades.append(trade)
        return trades

    def check(self) -> GuardStatus:
        if self.config.max_daily_loss is None:
            return GuardStatus(breached=False)
        total = sum(self._sum_trade_pnl(t) for t in self._iter_today_trades())
        limit = -abs(float(self.config.max_daily_loss))
        if total <= limit:
            return GuardStatus(
                breached=True,
                total_pnl=total,
                reason=f'portfolio_guard: pnl {total:.2f} <= limit {limit:.2f}',
            )
        return GuardStatus(breached=False, total_pnl=total)

    def halt(self, status: GuardStatus) -> None:
        write_halt_file(
            self.config.strategy_name,
            self.root,
            reason=status.reason,
            action=self.config.action,
        )
        log.warning(
            'PORTFOLIO_GUARD_HALT strategy=%s pnl=%.2f action=%s',
            self.config.strategy_name,
            status.total_pnl,
            self.config.action,
        )
        if self.config.action == _CLOSE_ACTION:
            from common.manual_close import close_all_structures
            close_all_structures(self.config.strategy_name, root=self.root)
