"""Supervised harness for standalone (non-tranche) strategies."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from typing import Any, Dict, Optional

from common.broker_factory import get_shared_broker_for_strategy
from common.daily_overrides import effective_config, is_disabled_for_today
from common.portfolio_guard import PortfolioGuard, PortfolioGuardConfig, is_strategy_halted
from common.trades_layout import active_dir_for_strategy
from strategies.base import BaseStrategy


class StandaloneStrategyRunner:
    """Broker lifecycle, daily idempotency, and crash-safe resume for one strategy."""

    def __init__(self, strategy: BaseStrategy, *, root: str, logger=None):
        self.strategy = strategy
        self.root = root
        self.log = logger or logging.getLogger(strategy.config.name)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_error: Optional[str] = None
        self._last_entry_attempt: Optional[str] = None
        self._running = False

    @property
    def config(self):
        return effective_config(self.strategy.config, self.strategy.config.name, self.root)

    def apply_daily_overrides(self) -> None:
        """Re-apply today's dashboard overrides onto the wrapped strategy config."""
        eff = effective_config(self.strategy.config, self.strategy.config.name, self.root)
        for key, value in eff.__dict__.items():
            if hasattr(self.strategy.config, key):
                setattr(self.strategy.config, key, value)

    def _session_state_path(self, for_date: Optional[date] = None) -> str:
        d = for_date or date.today()
        active = active_dir_for_strategy(self.config.name, self.root)
        os.makedirs(active, exist_ok=True)
        return os.path.join(active, f'session_{d.isoformat()}.json')

    def _load_session_state(self) -> Dict[str, Any]:
        path = self._session_state_path()
        if not os.path.isfile(path):
            return {'entered': False, 'skip_reason': None, 'overrides': {}}
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {'entered': False, 'skip_reason': None, 'overrides': {}}

    def save_session_state(self, **fields) -> None:
        path = self._session_state_path()
        state = self._load_session_state()
        state.update(fields)
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.flush()
        os.replace(tmp, path)

    def _assert_paper_safety(self) -> None:
        if not self.config.paper:
            return
        broker = self.strategy.broker
        if broker is None:
            return
        if getattr(broker, '_is_journal', False):
            return
        is_paper = getattr(broker, '_is_paper', None)
        if is_paper is not True:
            msg = (
                f'CRITICAL: strategy {self.config.name} is paper=true but broker is not paper — halting'
            )
            self.log.critical(msg)
            self._last_error = msg
            self._stop_event.set()
            raise RuntimeError(msg)

    def _register_open_trade_streamer_legs(self) -> None:
        """Re-subscribe streamer to open leg symbols after restart (strategy-specific)."""
        if self.config.name != 'SKYLINE_SPX':
            return
        today = date.today().isoformat()
        path = os.path.join(
            active_dir_for_strategy(self.config.name, self.root),
            f'skyline_{today}.json',
        )
        if not os.path.isfile(path):
            return
        try:
            with open(path, encoding='utf-8') as f:
                trade = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            self.log.warning('Could not load open Skyline trade for streamer register: %s', exc)
            return
        if trade.get('status') == 'closed':
            return
        from strategies.skyline.strategy import SkylineStrategy

        SkylineStrategy.register_open_trade_streamer_legs(trade, self.log)

    def _resolve_broker(self) -> None:
        if self.strategy.broker is not None:
            return
        broker = get_shared_broker_for_strategy(
            self.config.name,
            paper=self.config.paper,
        )
        self.strategy.broker = broker
        self._assert_paper_safety()

    def _trading_allowed(self) -> tuple[bool, str]:
        if is_strategy_halted(self.config.name, self.root):
            return False, 'portfolio_guard_halt'
        if is_disabled_for_today(self.strategy.config, self.config.name, self.root):
            return False, 'disabled_for_today'
        try:
            import run as launcher
            should_trade, reason = launcher.check_trading_day()
            return should_trade, reason
        except Exception as exc:
            self.log.warning('Trading-day check failed: %s', exc)
            return True, 'check_failed_open'

    def _check_portfolio_guard(self) -> None:
        max_loss = getattr(self.config, 'max_daily_loss', None)
        if max_loss is None:
            return
        guard = PortfolioGuard(
            PortfolioGuardConfig(self.config.name, max_daily_loss=max_loss),
            root=self.root,
        )
        status = guard.check()
        if status.breached:
            guard.halt(status)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.apply_daily_overrides()
        self._check_portfolio_guard()

        state = self._load_session_state()
        if state.get('entered'):
            self.log.info(
                '%s: session state shows entered=true — skipping re-entry',
                self.config.name,
            )
            self._register_open_trade_streamer_legs()
            return

        allowed, reason = self._trading_allowed()
        if not allowed:
            self.log.info('%s: entry blocked (%s) — skipping', self.config.name, reason)
            self.save_session_state(entered=False, skip_reason=reason)
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_wrapper,
            name=f'standalone-{self.config.name}',
            daemon=True,
        )
        self._thread.start()

    def _run_wrapper(self) -> None:
        try:
            self.apply_daily_overrides()
            self._resolve_broker()
            self._last_entry_attempt = time.strftime('%Y-%m-%dT%H:%M:%S')
            self.strategy.run()
        except Exception as exc:
            self._last_error = str(exc)
            self.log.exception('Standalone strategy %s failed', self.config.name)
        finally:
            self._running = False

    def stop(self) -> None:
        self._stop_event.set()
        self.strategy.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=30)

    def status(self) -> Dict[str, Any]:
        state = self._load_session_state()
        base = self.strategy.get_status() if hasattr(self.strategy, 'get_status') else {}
        eff = self.config
        return {
            'name': eff.name,
            'paper': eff.paper,
            'running': self._running,
            'entered': state.get('entered', False),
            'skip_reason': state.get('skip_reason'),
            'overrides': state.get('overrides', {}),
            'halted': is_strategy_halted(eff.name, self.root),
            'last_entry_attempt': self._last_entry_attempt,
            'last_error': self._last_error,
            **(base or {}),
        }
