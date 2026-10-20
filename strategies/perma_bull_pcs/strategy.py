"""Perma Bull PCS — 5-wide 0DTE put credit spread targeting ~$1 credit at 8:45 AM CT."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from blocks.stop.profiles.perma_bull_pcs import PERMA_BULL_PCS_PROFILE
from meic0dte.app import utilities as util
from strategies.base import BaseStrategy, StrategyConfig


@dataclass
class PermaBullPCSConfig(StrategyConfig):
    name: str = 'PERMA_BULL_PCS'
    scheduled: bool = False
    paper: bool = False
    confirmed: bool = False
    stop_profile: str = PERMA_BULL_PCS_PROFILE
    entry_time_et: str = '08:45'  # US/Central — same clock as MEIC session windows
    spread_width: int = 5
    target_credit: float = 1.0
    quantity: int = 1
    profit_target_debit: float = 0.15  # close spread at 15c (85c profit on ~$1 credit)
    root: str = ''


class PermaBullPCSStrategy(BaseStrategy):
    """Daily PCS entry — hold to expiry, no automated stop."""

    def __init__(self, config: PermaBullPCSConfig = None, broker=None):
        config = config or PermaBullPCSConfig()
        super().__init__(config, broker)
        self.config: PermaBullPCSConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self._status: Dict[str, Any] = {'running': False}

    def schedule(self):
        return []

    def run(self) -> None:
        self._running = True
        self._status['running'] = True
        root = self.config.root or os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')
        )

        try:
            today = util.central_date()
            expiry_yymmdd = today.strftime('%y%m%d')
            lot = f'perma-{today.isoformat()}'

            from common.trades_layout import active_dir_for_strategy
            active_dir = active_dir_for_strategy(self.config.name, root)
            session_path = os.path.join(active_dir, f'session_{today.isoformat()}.json')
            if os.path.isfile(session_path):
                try:
                    with open(session_path, encoding='utf-8') as f:
                        session_state = json.load(f)
                    if session_state.get('entered'):
                        self.logger.info('PERMA_BULL_PCS already entered today — trade managed by stop_monitor')
                        return
                except Exception as exc:
                    self.logger.warning('Failed reading Perma Bull session state: %s', exc)

            self._wait_for_entry_window(root)
            if not self._running:
                return

            if self._is_disabled_today(root):
                self._write_skip(root, 'disabled_for_today')
                return

            allowed, reason = self._check_trading_day()
            if not allowed:
                self._write_skip(root, reason)
                return

            from blocks.entry.credit_spread import CreditSpreadEntry

            entry = CreditSpreadEntry(self.broker, log=self.logger)
            candidates = entry.scan_for_target(
                'P',
                expiry_yymmdd,
                lot,
                spread_width=self.config.spread_width,
                target_credit=self.config.target_credit,
                max_results=1,
            )
            if not candidates:
                self.logger.warning('PERMA_BULL_NO_CANDIDATE expiry=%s width=%d target=%.2f',
                                    expiry_yymmdd, self.config.spread_width, self.config.target_credit)
                self._write_skip(root, 'no_candidate')
                return

            candidate = candidates[0]
            short_sym, long_sym, order_id, credit, short_k, long_k = entry.open_candidate(
                candidate,
                'P',
                self.config.quantity,
                lot,
            )

            trade_path = entry.write_handshake(
                lot=lot,
                side='P',
                short_symbol=short_sym,
                long_symbol=long_sym,
                short_strike=short_k,
                long_strike=long_k,
                quantity=self.config.quantity,
                open_order_id=order_id,
                limit_credit=credit,
                strategy=self.config.name,
                active_directory=active_dir,
                stop_profile=self.config.stop_profile,
                exit_config={
                    'profit_target_debit': self.config.profit_target_debit,
                },
            )
            self._register_streamer_legs(short_sym, long_sym)
            self._save_session_state(
                root,
                entered=True,
                skip_reason=None,
                trade_path=trade_path,
            )
            self._status.update({
                'entered': True,
                'lot': lot,
                'short_strike': short_k,
                'long_strike': long_k,
                'credit': credit,
                'trade_path': trade_path,
            })
            self.logger.info(
                'Perma Bull PCS placed: %d/%d credit=%.2f order=%s path=%s',
                short_k, long_k, credit, order_id, trade_path,
            )
        except Exception:
            self.logger.exception('Perma Bull PCS strategy run failed')
            raise
        finally:
            self._running = False
            self._status['running'] = False

    def _wait_for_entry_window(self, root: str) -> None:
        hour, minute = map(int, self.config.entry_time_et.split(':'))
        target = util.central_now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        while self._running:
            if self._is_disabled_today(root):
                return
            now = util.central_now()
            if now >= target:
                return
            time.sleep(min(30, max(1, (target - now).total_seconds())))

    def _is_disabled_today(self, root: str) -> bool:
        from common.daily_overrides import is_disabled_for_today

        return is_disabled_for_today(self.config, self.config.name, root)

    def _check_trading_day(self) -> tuple[bool, str]:
        import run as launcher

        return launcher.check_trading_day()

    def _active_dir(self, root: str) -> str:
        from common.trades_layout import active_dir_for_strategy

        path = active_dir_for_strategy(self.config.name, root)
        os.makedirs(path, exist_ok=True)
        return path

    def _register_streamer_legs(self, short_sym: str, long_sym: str) -> None:
        from common.streamer_symbols import register_option_symbols

        register_option_symbols([short_sym, long_sym], self.config.name, self.logger)

    @staticmethod
    def register_open_trade_streamer_legs(trade: dict, logger=None) -> None:
        from common.streamer_symbols import register_option_symbols

        short_sym = (trade.get('short_leg') or {}).get('symbol')
        long_sym = (trade.get('long_leg') or {}).get('symbol')
        symbols: List[str] = []
        if short_sym:
            symbols.append(str(short_sym))
        if long_sym:
            symbols.append(str(long_sym))
        if symbols:
            lot = str(trade.get('lot') or trade.get('entry', {}).get('lot') or 'PERMA_BULL_PCS')
            register_option_symbols(symbols, lot, logger)

    def _save_session_state(self, root: str, **fields) -> None:
        today = util.central_date().isoformat()
        path = os.path.join(self._active_dir(root), f'session_{today}.json')
        state: Dict[str, Any] = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as f:
                    state = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        state.update(fields)
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.flush()
        os.replace(tmp, path)

    def _write_skip(self, root: str, reason: str) -> None:
        self._save_session_state(root, entered=False, skip_reason=reason)
        self._status.update({'entered': False, 'skip_reason': reason})

    def stop(self) -> None:
        self._running = False
        self._status['running'] = False

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)
