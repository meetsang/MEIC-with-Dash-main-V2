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
        self._completed = False

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
        # If strategy has sub-strategies with independent execution modes, the strategy manages brokers per sub-strategy
        if getattr(self.config, 'has_any_live_sub_strategy', False):
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
        if self.config.name == 'SKYLINE_SPX':
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
            return

        if self.config.name == 'PERMA_BULL_PCS':
            from blocks.stop import state as state_mod
            from strategies.perma_bull_pcs.strategy import PermaBullPCSStrategy

            active = active_dir_for_strategy(self.config.name, self.root)
            for path in state_mod.iter_active_trade_paths():
                if not path.startswith(active):
                    continue
                try:
                    with open(path, encoding='utf-8') as f:
                        trade = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                entry = trade.get('entry') or {}
                if entry.get('strategy') != self.config.name:
                    continue
                if trade.get('status') == 'closed':
                    continue
                PermaBullPCSStrategy.register_open_trade_streamer_legs(trade, self.log)
            return

        if self.config.name == 'MEAN_REVERSION_SPX':
            today = date.today().isoformat()
            path = os.path.join(
                active_dir_for_strategy(self.config.name, self.root),
                f'mean_reversion_{today}.json',
            )
            if not os.path.isfile(path):
                return
            try:
                with open(path, encoding='utf-8') as f:
                    trade = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                self.log.warning('Could not load open Mean Reversion trade for streamer register: %s', exc)
                return
            if trade.get('status') == 'closed':
                return
            from strategies.mean_reversion.strategy import MeanReversionStrategy

            MeanReversionStrategy.register_open_trade_streamer_legs(trade, self.log)
            return

        if self.config.name == 'DRMA_HM_SPX':
            today = date.today().isoformat()
            active = active_dir_for_strategy(self.config.name, self.root)
            if not os.path.isdir(active):
                return
            for fname in os.listdir(active):
                if not (fname.startswith(f'drma_hm_{today}') and fname.endswith('.json')):
                    continue
                path = os.path.join(active, fname)
                try:
                    with open(path, encoding='utf-8') as f:
                        trade = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if trade.get('status') == 'closed':
                    continue
                from strategies.drma_hm.strategy import DRMAHMStrategy
                DRMAHMStrategy.register_open_trade_streamer_legs(trade, self.log)
            return

        if self.config.name == 'IRON_FLY_SPX':
            today = date.today().isoformat()
            path = os.path.join(
                active_dir_for_strategy(self.config.name, self.root),
                f'iron_fly_{today}.json',
            )
            if not os.path.isfile(path):
                return
            try:
                with open(path, encoding='utf-8') as f:
                    book_data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                self.log.warning('Could not load open Iron Fly book for streamer register: %s', exc)
                return
            from strategies.iron_fly.strategy import IronFlyStrategy
            IronFlyStrategy.register_open_trade_streamer_legs(book_data, self.log)
            return

        if self.config.name == 'IRON_FLY_XSP':
            today = date.today().isoformat()
            path = os.path.join(
                active_dir_for_strategy(self.config.name, self.root),
                f'iron_fly_xsp_{today}.json',
            )
            if not os.path.isfile(path):
                return
            try:
                with open(path, encoding='utf-8') as f:
                    book_data = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                self.log.warning('Could not load open XSP Iron Fly book for streamer register: %s', exc)
                return
            from strategies.iron_fly_xsp.strategy import IronFlyXspStrategy
            IronFlyXspStrategy.register_open_trade_streamer_legs(book_data, self.log)
            return

    def _resolve_broker(self) -> None:
        curr_broker = self.strategy.broker
        desired_paper = bool(self.config.paper)
        if curr_broker is not None:
            is_curr_paper = getattr(curr_broker, '_is_journal', False) or getattr(curr_broker, '_is_paper', False)
            if is_curr_paper == desired_paper:
                return
        broker = get_shared_broker_for_strategy(
            self.config.name,
            paper=desired_paper,
            reset=True,
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

    def _has_open_trades(self) -> bool:
        active_dir = active_dir_for_strategy(self.config.name, self.root)
        if not os.path.isdir(active_dir):
            return False
        for f in os.listdir(active_dir):
            if f.endswith('.json') and not f.startswith(('session_', 'halted_')):
                try:
                    with open(os.path.join(active_dir, f), encoding='utf-8') as jf:
                        t = json.load(jf)
                        if t.get('status') == 'open':
                            return True
                except Exception:
                    pass
        return False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.apply_daily_overrides()
        self._check_portfolio_guard()

        state = self._load_session_state()
        has_open_trades = self._has_open_trades()

        if state.get('entered') and self.config.name != 'DRMA_HM_SPX':
            self._register_open_trade_streamer_legs()
            if not has_open_trades:
                self.log.info(
                    '%s: session state shows entered=true and no open trades — skipping re-entry',
                    self.config.name,
                )
                self._completed = True
                return
            self.log.info(
                '%s: session state shows entered=true with active open trades — resuming management',
                self.config.name,
            )

        allowed, reason = self._trading_allowed()
        if not allowed:
            self.log.info('%s: entry blocked (%s) — skipping', self.config.name, reason)
            self.save_session_state(entered=False, skip_reason=reason)
            self._completed = True
            return

        self._stop_event.clear()
        self._completed = False
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
            self._completed = True
        except Exception as exc:
            self._last_error = str(exc)
            self.log.exception('Standalone strategy %s failed', self.config.name)
        finally:
            self._running = False

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def check_health(self) -> None:
        """Watchdog: if thread died unexpectedly during trading hours without stop requested, restart it."""
        if self._stop_event.is_set() or self.is_alive():
            return

        if self._completed:
            return

        state = self._load_session_state()
        if self.config.name != 'DRMA_HM_SPX':
            if state.get('entered') and not self._has_open_trades():
                self._completed = True
                return
            if state.get('skip_reason'):
                self._completed = True
                return

        allowed, _ = self._trading_allowed()
        if allowed:
            self.log.warning(
                'Standalone strategy %s thread died unexpectedly (last_error=%s) — reviving...',
                self.config.name,
                self._last_error,
            )
            self.start()

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
            'running': self._running or self.is_alive(),
            'completed': self._completed,
            'entered': state.get('entered', False),
            'skip_reason': state.get('skip_reason'),
            'overrides': state.get('overrides', {}),
            'halted': is_strategy_halted(eff.name, self.root),
            'last_entry_attempt': self._last_entry_attempt,
            'last_error': self._last_error,
            **(base or {}),
        }
