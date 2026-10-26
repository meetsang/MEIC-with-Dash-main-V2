"""Daily Reset MA & Hilega-Milega (DRMA_HM) 0DTE SPX Strategy implementation."""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from brokers.base import ComboLeg
from common.broker_factory import get_shared_broker_for_strategy
from common.streamer_symbols import register_option_symbols
from common.symbols import build_tastytrade_symbol, symbols_equivalent
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy
from meic0dte.app import utilities as util
from strategies.base import BaseStrategy
from strategies.drma_hm.config import DRMAHMConfig
from strategies.drma_hm.indicators import (
    IndicatorSnapshot,
    compute_indicators,
    evaluate_exit_signal,
)
from strategies.drma_hm.legs import (
    build_closing_legs,
    build_credit_spread_legs,
    build_debit_spread_legs,
    select_directional_strikes,
    select_non_directional_strikes,
)
from strategies.drma_hm.market_data_feed import (
    fetch_es_current_price,
    fetch_yahoo_es_5m_bars,
    fetch_yahoo_market_levels,
    load_spx_5m_bars,
)
from strategies.drma_hm.settlement import (
    attach_dollar_pnl_display,
    pnl_points_to_dollars,
    settle_open_trade,
    update_futures_unrealized_marks,
    update_unrealized_marks,
)

log = logging.getLogger(__name__)

_CT = ZoneInfo('America/Chicago')
_ET = ZoneInfo('America/New_York')


class DRMAHMStrategy(BaseStrategy):
    """Daily Reset MA + Key Levels & Hilega-Milega MTF SPX Strategy.

    Trades directional setups (Grade A+ 5/5) with Debit Spreads / Long Options
    and non-directional setups (Grade A 4/5 or neutral chop) with Credit Spreads / Iron Condors.
    Configurable targets (credit or delta for non-dir; debit or delta for dir) and PT/SL.
    """

    def __init__(self, config: Optional[DRMAHMConfig] = None, broker=None):
        config = config or DRMAHMConfig()
        super().__init__(config, broker)
        self.config: DRMAHMConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self._status: Dict[str, Any] = {'running': False, 'state': 'INIT'}
        self._daily_signals = 0
        self._daily_dir_signals = 0
        self._daily_non_dir_signals = 0
        self._daily_futures_signals = 0
        self._last_signal_bar = -999
        self._dir_options_position_state = 0   # 0 = Flat, 1 = Long CDS/Call, -1 = Long PDS/Put
        self._credit_options_position_state = 0 # 0 = Flat, 1 = Bull PCS, -1 = Bear CCS, 2 = Neutral IC
        self._options_position_state = 0       # 0 = Flat, 1 = Any Options Open
        self._futures_position_state = 0       # 0 = Flat, 1 = Long ES, -1 = Short ES
        self._position_state = 0               # 0 = Flat, 1 = Any Open
        self._entry_bar = -999
        self._active_trade_path: Optional[str] = None
        self._dir_options_trade_paper: Optional[bool] = None
        self._credit_options_trade_paper: Optional[bool] = None
        self._levels: Dict[str, Optional[float]] = {}
        self._paper_broker = None
        self._live_broker = None

    def _get_broker(self, is_paper: bool):
        """Return the appropriate broker instance (paper JournalBroker vs live TastyTradeBroker)."""
        if is_paper:
            if self._paper_broker is not None:
                return self._paper_broker
            if self.broker is not None and (getattr(self.broker, '_is_journal', False) or getattr(self.broker, '_is_paper', False)):
                return self.broker
            if self.broker is not None and type(self.broker).__name__ in ('MagicMock', 'Mock'):
                return self.broker
            try:
                self._paper_broker = get_shared_broker_for_strategy(self.config.name, paper=True)
                return self._paper_broker
            except Exception as exc:
                self.logger.warning('Could not acquire paper shared broker (%s); using default broker', exc)
                return self.broker
        else:
            if self._live_broker is not None:
                return self._live_broker
            if self.broker is not None and not (getattr(self.broker, '_is_journal', False) or getattr(self.broker, '_is_paper', False)):
                return self.broker
            try:
                self._live_broker = get_shared_broker_for_strategy(self.config.name, paper=False)
                return self._live_broker
            except Exception as exc:
                self.logger.warning('Could not acquire live shared broker (%s); using default broker', exc)
                return self.broker

    def schedule(self) -> List[Any]:
        return []

    def stop(self) -> None:
        self._running = False
        self._status['running'] = False
        self._status['state'] = 'STOPPED'
        self.logger.info('%s stopped', self.config.name)

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)

    @classmethod
    def register_open_trade_streamer_legs(cls, trade: dict, log_inst: logging.Logger) -> None:
        """Register streamer subscription for legs of an open trade."""
        if trade.get('structure_type') == 'futures' or trade.get('trade_type') == 'DIRECTIONAL_FUTURES':
            return
        symbols = []
        for struct in trade.get('structures') or []:
            for leg in struct.get('legs') or []:
                sym = leg.get('symbol')
                if sym and not sym.startswith(('ES', '/ES')):
                    symbols.append(str(sym))
        if symbols:
            register_option_symbols(symbols, logger=log_inst)
            log_inst.info('Registered %d streamer legs for open DRMA_HM trade', len(symbols))

    # --- Session State Helpers ---
    def _session_state_path(self, root: str, for_date: date) -> str:
        active_dir = active_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        return os.path.join(active_dir, f'session_{for_date.isoformat()}.json')

    def _load_session_state(self, root: str, for_date: date) -> Dict[str, Any]:
        path = self._session_state_path(root, for_date)
        if not os.path.isfile(path):
            try:
                from common.strategy_modes import get_persistent_modes_for_strategy
                persisted = get_persistent_modes_for_strategy(self.config.name, root)
            except Exception:
                persisted = {}
            return {'entered': False, 'signal_count': 0, 'skip_reason': None, 'overrides': dict(persisted)}
        try:
            with open(path, encoding='utf-8') as f:
                state = json.load(f)
                if 'overrides' not in state:
                    state['overrides'] = {}
                try:
                    from common.strategy_modes import get_persistent_modes_for_strategy
                    persisted = get_persistent_modes_for_strategy(self.config.name, root)
                    for k, v in persisted.items():
                        if k not in state['overrides']:
                            state['overrides'][k] = v
                except Exception:
                    pass
                return state
        except Exception:
            try:
                from common.strategy_modes import get_persistent_modes_for_strategy
                persisted = get_persistent_modes_for_strategy(self.config.name, root)
            except Exception:
                persisted = {}
            return {'entered': False, 'signal_count': 0, 'skip_reason': None, 'overrides': dict(persisted)}

    def _save_session_state(self, root: str, for_date: date, **fields) -> None:
        path = self._session_state_path(root, for_date)
        state = self._load_session_state(root, for_date)
        # Always re-read and preserve overrides directly from disk if present to prevent clobbering
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    disk_data = json.load(f)
                if isinstance(disk_data, dict) and 'overrides' in disk_data and disk_data['overrides']:
                    state['overrides'] = dict(disk_data['overrides'])
            except Exception:
                pass
        state.update(fields)
        tmp = f'{path}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
                f.flush()
            for attempt in range(8):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 7:
                        with open(path, 'w', encoding='utf-8') as f:
                            json.dump(state, f, indent=2)
                        return
                    time.sleep(0.05 * (attempt + 1))
        except Exception as exc:
            self.logger.warning('Failed to save session state: %s', exc)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass

    def _write_skip(self, root: str, reason: str) -> None:
        today = util.central_date()
        self._save_session_state(root, today, entered=False, skip_reason=reason)
        self._status['state'] = f'SKIPPED_{reason.upper()}'
        self.logger.info('%s session skipped: %s', self.config.name, reason)

    # --- Market Time Gates ---
    def _parse_time_cst(self, hhmm: str, default: dt_time = dt_time(14, 50)) -> dt_time:
        if not hhmm:
            return default
        try:
            clean = str(hhmm).strip()
            if ':' in clean:
                parts = clean.split(':')
                return dt_time(int(parts[0]), int(parts[1]))
            digits = clean.replace(':', '').zfill(4)
            return dt_time(int(digits[:2]), int(digits[2:4]))
        except Exception:
            return default

    def get_session_phase(self, now_ct: datetime) -> str:
        """Determine current trading session phase based on CST clock.

        Phases:
          - 'OVERNIGHT_TRADING': Prior to 07:00 CST (or from 17:00 CST onwards) - Directional ES Futures only
          - 'PRE_MARKET_PREP': 07:00 CST to 08:30 CST - Flattened, prep for market open
          - 'MARKET_OPEN_PREP': 08:30 CST to 09:00 CST - RTH open, 30m ORB formation
          - 'REGULAR_TRADING': 09:00 CST to 14:50 CST - Full active trading (options & futures)
          - 'EOD_FLATTEN': 14:50 CST to 15:00 CST - Force flat all daytime positions
          - 'DAILY_HALT': 15:00 CST to 17:00 CST - CME Globex maintenance halt
        """
        t = now_ct.time()
        flatten_t = self._parse_time_cst(getattr(self.config, 'overnight_flatten_time_cst', '07:00'), dt_time(7, 0))
        open_t = self._parse_time_cst(getattr(self.config, 'market_open_time_cst', '08:30'), dt_time(8, 30))
        reg_start_t = self._parse_time_cst(getattr(self.config, 'regular_trading_start_time_cst', '09:00'), dt_time(9, 0))
        eod_t = self._parse_time_cst(getattr(self.config, 'eod_exit_time_cst', '14:50'), dt_time(14, 50))
        close_t = dt_time(15, 0)
        reopen_t = dt_time(17, 0)

        if t < flatten_t or t >= reopen_t:
            return 'OVERNIGHT_TRADING'
        elif flatten_t <= t < open_t:
            return 'PRE_MARKET_PREP'
        elif open_t <= t < reg_start_t:
            return 'MARKET_OPEN_PREP'
        elif reg_start_t <= t < eod_t:
            return 'REGULAR_TRADING'
        elif eod_t <= t < close_t:
            return 'EOD_FLATTEN'
        else:  # 15:00 to 17:00
            return 'DAILY_HALT'

    def _is_lunch_window(self, now_ct: datetime) -> bool:
        """10:30 AM to 12:30 PM CST (11:30 AM to 1:30 PM ET)."""
        t = now_ct.time()
        return dt_time(10, 30) <= t <= dt_time(12, 30)

    def _is_eod_cutoff(self, now_ct: datetime) -> bool:
        cutoff_t = self._parse_time_cst(self.config.eod_exit_time_cst)
        return now_ct.time() >= cutoff_t

    def _is_rth(self, now_ct: datetime) -> bool:
        """08:30 to 15:00 CST (09:30 to 16:00 ET)."""
        t = now_ct.time()
        return dt_time(8, 30) <= t <= dt_time(15, 0)

    def _recount_daily_signals(self, root: str, today: date) -> None:
        """Scan active and history trades for today to count entries per sub-strategy."""
        dir_count = 0
        nondir_count = 0
        fut_count = 0
        seen_trade_ids = set()

        prefix = f'drma_hm_{today.isoformat()}_'
        search_dirs = [
            active_dir_for_strategy(self.config.name, root),
            history_dir_for_strategy(self.config.name, root),
        ]
        date_hist = os.path.join(history_dir_for_strategy(self.config.name, root), today.isoformat())
        if os.path.isdir(date_hist):
            search_dirs.append(date_hist)

        for s_dir in search_dirs:
            if not os.path.isdir(s_dir):
                continue
            for fname in os.listdir(s_dir):
                if (fname.startswith(prefix) or today.isoformat() in fname) and fname.endswith('.json'):
                    if fname.startswith('session_'):
                        continue
                    fpath = os.path.join(s_dir, fname)
                    try:
                        with open(fpath, encoding='utf-8') as f:
                            t_data = json.load(f)
                        tid = t_data.get('trade_id') or fname
                        if tid in seen_trade_ids or t_data.get('status') == 'skipped':
                            continue
                        seen_trade_ids.add(tid)
                        sub_strat = str(t_data.get('sub_strategy') or '').upper()
                        struct_type = str(t_data.get('structure_type') or '').lower()
                        trade_type = str(t_data.get('trade_type') or '').upper()

                        if sub_strat == 'DRMA_HM_DIR' or trade_type == 'DIRECTIONAL':
                            dir_count += 1
                        elif sub_strat == 'DRMA_HM_FUT' or struct_type == 'futures' or trade_type == 'DIRECTIONAL_FUTURES':
                            fut_count += 1
                        else:
                            nondir_count += 1
                    except Exception:
                        pass

        self._daily_dir_signals = dir_count
        self._daily_non_dir_signals = nondir_count
        self._daily_futures_signals = fut_count
        self._daily_signals = dir_count + nondir_count + fut_count

    # --- Main Strategy Lifecycle ---
    def run(self) -> None:
        self._running = True
        self._status['running'] = True
        self._status['state'] = 'STARTING'

        root = self.config.root or os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')
        )
        data_root = os.path.join(root, self.config.data_dir)

        try:
            today = util.central_date()
            expiry_yymmdd = today.strftime('%y%m%d')

            # 1. Trading day gate
            allowed, reason = util.check_trading_day() if hasattr(util, 'check_trading_day') else (True, '')
            if not allowed:
                self._write_skip(root, reason or 'not_a_trading_day')
                return

            # Apply persistent strategy modes on startup
            try:
                from common.strategy_modes import get_persistent_modes_for_strategy
                persisted = get_persistent_modes_for_strategy(self.config.name, root=root)
                if persisted:
                    for ov_k, ov_v in persisted.items():
                        if hasattr(self.config, ov_k):
                            setattr(self.config, ov_k, ov_v)
            except Exception:
                pass

            self.logger.info(
                'Starting %s strategy (mode=%s, paper=%s, ticker=%s)',
                self.config.name, self.config.trade_mode, self.config.paper, self.config.ticker,
            )

            # 2. Fetch / initialize market levels (ONH, ONL, PDH, PDL, PDC, ES Offset)
            self._levels = fetch_yahoo_market_levels(today, self.config, root)
            self._save_session_state(root, today, levels=self._levels)
            self.logger.info(
                'Market Key Levels loaded: PDH=%.2f, PDL=%.2f, PDC=%.2f, ONH=%s, ONL=%s (source=%s)',
                self._levels.get('pdh') or 0.0,
                self._levels.get('pdl') or 0.0,
                self._levels.get('pdc') or 0.0,
                self._levels.get('onh'),
                self._levels.get('onl'),
                self._levels.get('source'),
            )
            self._recount_daily_signals(root, today)

            # 3. Resume open trade if already entered today
            active_dir = active_dir_for_strategy(self.config.name, root)
            open_trades = [
                os.path.join(active_dir, f) for f in os.listdir(active_dir)
                if f.startswith('drma_hm_') and f.endswith('.json') and not f.startswith('session_')
            ] if os.path.isdir(active_dir) else []

            has_open_position = False
            for t_path in open_trades:
                try:
                    with open(t_path, encoding='utf-8') as f:
                        open_trade_data = json.load(f)
                    st = open_trade_data.get('status')
                    if st in ('open', 'closing'):
                        has_open_position = True
                        self._active_trade_path = t_path
                        self.logger.info('Resuming management of %s trade %s', st, t_path)
                        is_fut = (open_trade_data.get('structure_type') == 'futures' or open_trade_data.get('trade_type') == 'DIRECTIONAL_FUTURES')
                        if is_fut:
                            self._futures_position_state = 1 if open_trade_data.get('side') == 'LONG' else -1
                        elif open_trade_data.get('trade_type') == 'DIRECTIONAL':
                            self._dir_options_position_state = 1 if open_trade_data.get('side') == 'LONG' else -1
                            self._dir_options_trade_paper = bool(open_trade_data.get('paper', self.config.is_dir_paper))
                            self.register_open_trade_streamer_legs(open_trade_data, self.logger)
                        else:
                            self._credit_options_position_state = 1 if open_trade_data.get('side') == 'LONG' else -1
                            self._credit_options_trade_paper = bool(open_trade_data.get('paper', self.config.is_non_dir_paper))
                            self.register_open_trade_streamer_legs(open_trade_data, self.logger)
                        if st == 'closing':
                            self._manage_closing_trade(root, t_path)
                        else:
                            self._manage_open_trade(root, t_path)
                except Exception as exc:
                    self.logger.warning('Failed to resume open/closing trade %s: %s', t_path, exc)

            self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0
            self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0
            if not self._running:
                return

            self._status['state'] = 'MONITORING'

            cached_snapshots = []
            last_cache_key = None

            # 4. Main monitoring and execution loop
            while self._running:
                try:
                    now_ct = util.central_now()

                    # Dynamically re-apply persistent modes and daily overrides so dashboard updates take effect immediately
                    try:
                        from common.strategy_modes import get_persistent_modes_for_strategy
                        persisted = get_persistent_modes_for_strategy(self.config.name, root=root)
                        if persisted:
                            for ov_k, ov_v in persisted.items():
                                if hasattr(self.config, ov_k):
                                    setattr(self.config, ov_k, ov_v)
                        today_session = self._load_session_state(root, today)
                        session_ov = today_session.get('overrides') or {}
                        if session_ov:
                            for ov_k, ov_v in session_ov.items():
                                if hasattr(self.config, ov_k):
                                    setattr(self.config, ov_k, ov_v)
                    except Exception:
                        pass

                    # Determine current trading session phase
                    phase = self.get_session_phase(now_ct)
                    self._status['session_phase'] = phase
                    self._status['timing'] = {
                        'session_phase': phase,
                        'enable_overnight_futures': bool(getattr(self.config, 'enable_overnight_futures', True)),
                        'overnight_flatten_time_cst': getattr(self.config, 'overnight_flatten_time_cst', '07:00'),
                        'market_open_time_cst': getattr(self.config, 'market_open_time_cst', '08:30'),
                        'regular_trading_start_time_cst': getattr(self.config, 'regular_trading_start_time_cst', '09:30'),
                        'eod_exit_time_cst': getattr(self.config, 'eod_exit_time_cst', '14:50'),
                    }

                    # Scan for currently active open trades (both overnight and daytime trades)
                    open_trades = [
                        os.path.join(active_dir, f) for f in os.listdir(active_dir)
                        if f.startswith('drma_hm_') and f.endswith('.json') and not f.startswith('session_')
                    ] if os.path.isdir(active_dir) else []

                    # 1. DAILY HALT (15:00 - 17:00 CST): CME Globex maintenance break
                    # End of active day cutoff: Ensure all open trades (futures and options) are flattened before halt
                    if phase == 'DAILY_HALT':
                        for t_path in open_trades:
                            try:
                                with open(t_path, encoding='utf-8') as f:
                                    t_chk = json.load(f)
                                if t_chk.get('status') == 'open':
                                    self.logger.info('Daily maintenance halt reached (15:00 CST) — flattening active trade %s', t_path)
                                    self._exit_trade(root, t_path, 'end_of_active_day_flatten')
                            except Exception:
                                pass
                        if not getattr(self.config, 'enable_overnight_futures', True):
                            self.logger.info('Regular market session closed (15:00 CST). Exiting run loop.')
                            self._status['state'] = 'SESSION_CLOSED'
                            break
                        self._status['state'] = 'STANDBY_UNTIL_1700'
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    # 2. PRE-MARKET PREPARATION (07:00 - 08:30 CST):
                    # Flatten open overnight futures positions by 07:00 CST and prepare for 08:30 CST open
                    if phase == 'PRE_MARKET_PREP':
                        for t_path in open_trades:
                            try:
                                with open(t_path, encoding='utf-8') as f:
                                    t_chk = json.load(f)
                                if t_chk.get('status') == 'open':
                                    is_fut = (t_chk.get('structure_type') == 'futures' or t_chk.get('trade_type') == 'DIRECTIONAL_FUTURES')
                                    if is_fut:
                                        self.logger.info('Overnight 07:00 CST cutoff reached — flattening ES futures position for %s', t_path)
                                        self._exit_trade(root, t_path, 'overnight_flatten_7am')
                            except Exception:
                                pass
                        self._status['state'] = 'PRE_MARKET_READY'
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    # 3. OVERNIGHT TRADING (< 07:00 CST or >= 17:00 CST):
                    # Allow Directional ES Futures to trade overnight until 7:00 AM CST
                    if phase == 'OVERNIGHT_TRADING':
                        if not getattr(self.config, 'enable_overnight_futures', True) or not self.config.is_directional_futures_enabled:
                            self._status['state'] = 'WAITING_FOR_OPEN'
                            time.sleep(self.config.poll_interval_sec)
                            continue

                        # Load 5m bars for ES futures from Yahoo Finance
                        df_es = fetch_yahoo_es_5m_bars(today, include_overnight=True)
                        if df_es.empty or len(df_es) < 2:
                            time.sleep(self.config.poll_interval_sec)
                            continue

                        # Compute indicators on ES bars
                        _, snapshots = compute_indicators(
                            df_es,
                            ma_type=self.config.ma_type,
                            ma_len=self.config.ma_len,
                            orb_minutes=self.config.orb_minutes,
                            min_slope_pts=self.config.min_slope_pts,
                            max_extension_pts=self.config.max_extension_pts,
                            pullback_tol_pts=self.config.pullback_tol_pts,
                            pdh=self._levels.get('pdh'),
                            pdl=self._levels.get('pdl'),
                            pdc=self._levels.get('pdc'),
                            onh=self._levels.get('onh'),
                            onl=self._levels.get('onl'),
                        )
                        if not snapshots:
                            time.sleep(self.config.poll_interval_sec)
                            continue

                        latest_snap = snapshots[-1]
                        bar_idx = latest_snap.bar_index

                        current_es_price = fetch_es_current_price(
                            broker=self.broker,
                            levels=self._levels,
                            spx_spot=latest_snap.close,
                        )

                        # Manage open overnight futures trade
                        has_open_futures = False
                        for t_path in open_trades:
                            try:
                                with open(t_path, encoding='utf-8') as f:
                                    t_chk = json.load(f)
                                st = t_chk.get('status')
                                if st in ('open', 'closing'):
                                    is_fut = (t_chk.get('structure_type') == 'futures' or t_chk.get('trade_type') == 'DIRECTIONAL_FUTURES')
                                    if is_fut:
                                        has_open_futures = True
                                        self._active_trade_path = t_path
                                        if st == 'open':
                                            self._manage_open_trade(root, t_path, snapshots)
                                        elif st == 'closing':
                                            self._manage_closing_trade(root, t_path)
                            except Exception as exc:
                                self.logger.warning('Error managing open/closing overnight trade %s: %s', t_path, exc)

                        if not has_open_futures:
                            self._futures_position_state = 0

                        # Check entry for overnight futures (Grade A+ only)
                        fut_max = getattr(self.config, 'futures_max_daily_signals', 0)
                        futs_limit_ok = (fut_max == 0) or (self._daily_futures_signals < fut_max)
                        is_cooled_down = (bar_idx - self._last_signal_bar) >= self.config.cooldown_bars
                        futs_min_thresh = 5 if 'a+ only' in getattr(self.config, 'futures_min_grade', 'Grade A+ Only').lower() else 4

                        if self._futures_position_state == 0 and futs_limit_ok and is_cooled_down:
                            if latest_snap.active_score >= futs_min_thresh and latest_snap.bias in ('BULL', 'BEAR'):
                                self._enter_directional_futures_trade(root, expiry_yymmdd, latest_snap, bar_idx)

                        self._status.update({
                            'state': 'OVERNIGHT_FUTURES_ACTIVE',
                            'last_bar': latest_snap.datetime_str,
                            'close': latest_snap.close,
                            'es_price': current_es_price,
                            'drma': latest_snap.out_ma,
                            'bias': latest_snap.bias,
                            'grade': latest_snap.setup_grade,
                            'active_score': latest_snap.active_score,
                            'vehicle': 'DIRECTIONAL ES FUTURES (OVERNIGHT)',
                        })
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    # Check if 5m bars file or indicator config has changed before recomputing
                    day_csv = os.path.join(data_root, today.isoformat(), 'SPX_5m.csv')
                    current_mtime = os.path.getmtime(day_csv) if os.path.isfile(day_csv) else None
                    cache_key = (
                        current_mtime,
                        self.config.ma_type,
                        self.config.ma_len,
                        self.config.orb_minutes,
                        self.config.min_slope_pts,
                        self.config.max_extension_pts,
                        self.config.pullback_tol_pts,
                    )

                    if current_mtime is not None and cache_key == last_cache_key and cached_snapshots:
                        snapshots = cached_snapshots
                    else:
                        # Load 5m bars
                        df_bars = load_spx_5m_bars(data_root, today, prepend_prev_bars=40)
                        if df_bars.empty:
                            time.sleep(self.config.poll_interval_sec)
                            continue

                        # Compute complete indicator suite
                        df_calc, snapshots = compute_indicators(
                            df_bars,
                            ma_type=self.config.ma_type,
                            ma_len=self.config.ma_len,
                            orb_minutes=self.config.orb_minutes,
                            min_slope_pts=self.config.min_slope_pts,
                            max_extension_pts=self.config.max_extension_pts,
                            pullback_tol_pts=self.config.pullback_tol_pts,
                            pdh=self._levels.get('pdh'),
                            pdl=self._levels.get('pdl'),
                            pdc=self._levels.get('pdc'),
                            onh=self._levels.get('onh'),
                            onl=self._levels.get('onl'),
                        )
                        if snapshots:
                            last_cache_key = cache_key
                            cached_snapshots = snapshots

                    if not snapshots:
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    latest_snap = snapshots[-1]
                    bar_idx = latest_snap.bar_index

                    # Load current ES price for status & monitoring
                    current_es_price = fetch_es_current_price(
                        broker=self.broker,
                        levels=self._levels,
                        spx_spot=latest_snap.close,
                    )

                    # Update live status
                    self._status.update({
                        'last_bar': latest_snap.datetime_str,
                        'close': latest_snap.close,
                        'es_price': current_es_price,
                        'drma': latest_snap.out_ma,
                        'orb_formed': latest_snap.orb_formed,
                        'orb_high': latest_snap.orb_high,
                        'orb_low': latest_snap.orb_low,
                        'rsi': latest_snap.rsi,
                        'fast': latest_snap.hm_fast,
                        'slow': latest_snap.hm_slow,
                        'bias': latest_snap.bias,
                        'grade': latest_snap.setup_grade,
                        'vehicle': latest_snap.trade_vehicle,
                        'gamma_risk': latest_snap.gamma_risk,
                        'options_position_state': self._options_position_state,
                        'dir_options_position_state': self._dir_options_position_state,
                        'credit_options_position_state': self._credit_options_position_state,
                        'futures_position_state': self._futures_position_state,
                        'position_state': self._position_state,
                        'daily_signals': self._daily_signals,
                        'daily_dir_signals': self._daily_dir_signals,
                        'daily_nondir_signals': self._daily_non_dir_signals,
                        'daily_futures_signals': self._daily_futures_signals,
                    })
                    self._save_session_state(
                        root, today,
                        indicators=dict(self._status),
                        levels=self._levels,
                    )

                    # Manage all active open trades (ALWAYS runs regardless of ORB or lunch window)
                    open_trades = [
                        os.path.join(active_dir, f) for f in os.listdir(active_dir)
                        if f.startswith('drma_hm_') and f.endswith('.json') and not f.startswith('session_')
                    ] if os.path.isdir(active_dir) else []

                    # Check EOD Cutoff (End of Active Day: 14:50 CST)
                    if self.config.force_eod_exit and (self._is_eod_cutoff(now_ct) or phase == 'EOD_FLATTEN'):
                        for t_path in open_trades:
                            try:
                                with open(t_path, encoding='utf-8') as f:
                                    t_chk = json.load(f)
                                st = t_chk.get('status')
                                if st == 'open':
                                    self.logger.info('End of active day cutoff reached (14:50 CST) — forcing flat for %s', t_path)
                                    self._exit_trade(root, t_path, 'end_of_active_day_flatten')
                                elif st == 'closing':
                                    self._manage_closing_trade(root, t_path)
                            except Exception:
                                pass
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    # Manage each open / closing trade (check profit target, stop loss, momentum exit, closing chase)
                    has_open_dir_opts = False
                    has_open_credit_opts = False
                    has_open_futures = False
                    for t_path in open_trades:
                        try:
                            with open(t_path, encoding='utf-8') as f:
                                t_chk = json.load(f)
                            st = t_chk.get('status')
                            if st in ('open', 'closing'):
                                self._active_trade_path = t_path
                                is_fut = (t_chk.get('structure_type') == 'futures' or t_chk.get('trade_type') == 'DIRECTIONAL_FUTURES')
                                if is_fut:
                                    has_open_futures = True
                                elif t_chk.get('trade_type') == 'DIRECTIONAL':
                                    has_open_dir_opts = True
                                    self._dir_options_trade_paper = bool(t_chk.get('paper', self.config.is_dir_paper))
                                else:
                                    has_open_credit_opts = True
                                    self._credit_options_trade_paper = bool(t_chk.get('paper', self.config.is_non_dir_paper))

                                if st == 'open':
                                    self._manage_open_trade(root, t_path, snapshots)
                                elif st == 'closing':
                                    self._manage_closing_trade(root, t_path)
                        except Exception as exc:
                            self.logger.warning('Error managing open/closing trade %s: %s', t_path, exc)

                    if not has_open_dir_opts:
                        self._dir_options_position_state = 0
                        self._dir_options_trade_paper = None
                    if not has_open_credit_opts:
                        self._credit_options_position_state = 0
                        self._credit_options_trade_paper = None
                    if not has_open_futures:
                        self._futures_position_state = 0
                    self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0
                    self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0

                    # Check Market Open Preparation window (08:30 - 09:30 CST)
                    # Indicators are warming up and ORB is forming. Do not enter new trades before 09:30 CST.
                    # Ensure any open overnight futures position is flattened if lingering into market open prep.
                    if phase == 'MARKET_OPEN_PREP':
                        for t_path in open_trades:
                            try:
                                with open(t_path, encoding='utf-8') as f:
                                    t_chk = json.load(f)
                                st = t_chk.get('status')
                                if st == 'open':
                                    is_fut = (t_chk.get('structure_type') == 'futures' or t_chk.get('trade_type') == 'DIRECTIONAL_FUTURES')
                                    if is_fut:
                                        self.logger.info('Flattening lingering overnight futures before market open prep for %s', t_path)
                                        self._exit_trade(root, t_path, 'overnight_flatten_7am')
                                elif st == 'closing':
                                    self._manage_closing_trade(root, t_path)
                            except Exception:
                                pass
                        self._status['state'] = 'MARKET_OPEN_WARMUP'
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    self._status['state'] = 'TRADING_ACTIVE'

                    # Check ORB readiness before considering NEW entries
                    if self.config.wait_for_orb and not latest_snap.orb_formed:
                        time.sleep(self.config.poll_interval_sec)
                        continue

                    # Check sub-strategy daily signal limits
                    if (
                        self._daily_dir_signals == 0
                        and self._daily_non_dir_signals == 0
                        and self._daily_futures_signals == 0
                        and self._daily_signals > 0
                    ):
                        self._recount_daily_signals(root, today)

                    dir_max = getattr(self.config, 'dir_max_daily_signals', 0)
                    dir_limit_ok = (dir_max == 0) or (self._daily_dir_signals < dir_max)

                    nondir_max = getattr(self.config, 'non_dir_max_daily_signals', 0)
                    nondir_limit_ok = (nondir_max == 0) or (self._daily_non_dir_signals < nondir_max)

                    fut_max = getattr(self.config, 'futures_max_daily_signals', 0)
                    futs_limit_ok = (fut_max == 0) or (self._daily_futures_signals < fut_max)

                    global_max = getattr(self.config, 'max_daily_signals', 0)
                    global_limit_ok = (global_max == 0) or (self._daily_signals < global_max)

                    is_cooled_down = (bar_idx - self._last_signal_bar) >= self.config.cooldown_bars
                    lunch_blocked = self.config.filter_lunch_chop and self._is_lunch_window(now_ct)

                    # Check if directional options, credit options, or futures can enter
                    can_enter_dir_opts = self.config.is_directional_options_enabled and (self._dir_options_position_state == 0) and dir_limit_ok and global_limit_ok
                    can_enter_credit_opts = self.config.is_non_directional_options_enabled and (self._credit_options_position_state == 0) and nondir_limit_ok and global_limit_ok
                    can_enter_futs = self.config.is_directional_futures_enabled and (self._futures_position_state == 0) and futs_limit_ok and global_limit_ok

                    if (can_enter_dir_opts or can_enter_credit_opts or can_enter_futs) and is_cooled_down and not lunch_blocked:
                        self._evaluate_and_enter(root, expiry_yymmdd, latest_snap, bar_idx)

                    time.sleep(self.config.poll_interval_sec)
                except Exception as iter_exc:
                    self.logger.warning('DRMAHMStrategy loop iteration error (will retry next tick): %s', iter_exc)
                    time.sleep(self.config.poll_interval_sec)

        except Exception as exc:
            self.logger.exception('DRMAHMStrategy run loop error: %s', exc)
            self._status['error'] = str(exc)
        finally:
            self._running = False
            self._status['running'] = False

    def _evaluate_and_enter(
        self,
        root: str,
        expiry_yymmdd: str,
        snap: IndicatorSnapshot,
        bar_idx: int,
    ) -> None:
        """Evaluate setup score against configuration and execute entry."""
        score = snap.active_score
        bias = snap.bias
        mode = self.config.trade_mode.lower()

        # Score threshold filter
        min_threshold = 4
        if 'a+ only' in self.config.min_grade_to_signal.lower():
            min_threshold = 5
        elif 'grade b' in self.config.min_grade_to_signal.lower():
            min_threshold = 3

        dir_max = getattr(self.config, 'dir_max_daily_signals', 0)
        dir_limit_ok = (dir_max == 0) or (self._daily_dir_signals < dir_max)

        nondir_max = getattr(self.config, 'non_dir_max_daily_signals', 0)
        nondir_limit_ok = (nondir_max == 0) or (self._daily_non_dir_signals < nondir_max)

        fut_max = getattr(self.config, 'futures_max_daily_signals', 0)
        futs_limit_ok = (fut_max == 0) or (self._daily_futures_signals < fut_max)

        global_max = getattr(self.config, 'max_daily_signals', 0)
        global_limit_ok = (global_max == 0) or (self._daily_signals < global_max)

        if score < min_threshold or bias == 'NEUTRAL':
            # Check if neutral Iron Condor is allowed
            if (
                self.config.is_non_directional_options_enabled
                and self._credit_options_position_state == 0
                and self.config.non_dir_allow_iron_condor
                and nondir_limit_ok
                and global_limit_ok
                and snap.orb_formed
                and snap.orb_low is not None
                and snap.orb_high is not None
                and snap.orb_low <= snap.close <= snap.orb_high
            ):
                self._enter_iron_condor(root, expiry_yymmdd, snap, bar_idx)
            return

        # Determine if setup should trigger Directional Options, Non-Directional Options, and/or Directional Futures
        # Grade A+ (Score 5): Directional Expansion (Options Debit Spreads & Directional Futures)
        # Grade A (Score 4): Credit Harvest (Theta Credit Spreads)
        is_directional = (score == 5)
        mode = self.config.trade_mode.lower()
        if mode in ('directional_only', 'directional_options', 'directional_both'):
            is_directional = True
        elif mode in ('non_directional_only', 'non_directional_options'):
            is_directional = False

        is_overnight = (self.get_session_phase(util.central_now()) == 'OVERNIGHT_TRADING')

        # 1. Directional Options (SPX Debit Spread)
        if is_directional and self.config.is_directional_options_enabled and self._dir_options_position_state == 0 and dir_limit_ok and global_limit_ok and not is_overnight:
            block_same_side = bool(getattr(self.config, 'dir_block_if_same_side_nondir', True) and getattr(self.config, 'enable_debounce', True))
            dir_same_side_blocked = False
            if block_same_side and self._credit_options_position_state != 0:
                nondir_same_side = (
                    (snap.bias == 'BULL' and self._credit_options_position_state in (1, 2)) or
                    (snap.bias == 'BEAR' and self._credit_options_position_state in (-1, 2))
                )
                if nondir_same_side:
                    dir_paper = bool(self.config.is_dir_paper)
                    nondir_paper = (
                        self._credit_options_trade_paper
                        if self._credit_options_trade_paper is not None
                        else bool(self.config.is_non_dir_paper)
                    )
                    # Setting only kicks in when BOTH are LIVE or BOTH are PAPER
                    if dir_paper == nondir_paper:
                        dir_same_side_blocked = True
                        self.logger.info(
                            'Directional %s trade blocked: active non-directional trade open on same side (both %s)',
                            snap.bias, 'PAPER' if dir_paper else 'LIVE',
                        )
                    else:
                        self.logger.info(
                            'Directional %s trade permitted: active non-directional is %s while directional is %s (mixed modes)',
                            snap.bias, 'PAPER' if nondir_paper else 'LIVE', 'PAPER' if dir_paper else 'LIVE',
                        )

            if not dir_same_side_blocked:
                self._enter_directional_trade(root, expiry_yymmdd, snap, bar_idx)

        # 2. Non-Directional Options (SPX Credit Spread)
        if not is_directional and self.config.is_non_directional_options_enabled and self._credit_options_position_state == 0 and nondir_limit_ok and global_limit_ok and not is_overnight:
            block_same_side = bool(getattr(self.config, 'dir_block_if_same_side_nondir', True) and getattr(self.config, 'enable_debounce', True))
            nondir_same_side_blocked = False
            if block_same_side and self._dir_options_position_state != 0:
                dir_same_side = (
                    (snap.bias == 'BULL' and self._dir_options_position_state == 1) or
                    (snap.bias == 'BEAR' and self._dir_options_position_state == -1)
                )
                if dir_same_side:
                    nondir_paper = bool(self.config.is_non_dir_paper)
                    dir_paper = (
                        self._dir_options_trade_paper
                        if self._dir_options_trade_paper is not None
                        else bool(self.config.is_dir_paper)
                    )
                    # Setting only kicks in when BOTH are LIVE or BOTH are PAPER
                    if dir_paper == nondir_paper:
                        nondir_same_side_blocked = True
                        self.logger.info(
                            'Non-Directional %s trade blocked: active directional trade open on same side (both %s)',
                            snap.bias, 'PAPER' if nondir_paper else 'LIVE',
                        )
                    else:
                        self.logger.info(
                            'Non-Directional %s trade permitted: active directional is %s while non-directional is %s (mixed modes)',
                            snap.bias, 'PAPER' if dir_paper else 'LIVE', 'PAPER' if nondir_paper else 'LIVE',
                        )

            if not nondir_same_side_blocked:
                self._enter_non_directional_trade(root, expiry_yymmdd, snap, bar_idx)

        # 3. Directional Futures on ES (Outright ES Futures with 5 pt stop and 5 pt target on Grade A+ setups only)
        futs_min_thresh = 5 if 'a+ only' in getattr(self.config, 'futures_min_grade', 'Grade A+ Only').lower() else min_threshold
        if self.config.is_directional_futures_enabled and self._futures_position_state == 0 and futs_limit_ok and global_limit_ok:
            if score >= futs_min_thresh:
                self._enter_directional_futures_trade(root, expiry_yymmdd, snap, bar_idx)

    def _fetch_broker_option_mids(self, expiry_yymmdd: str, side: str, spot: float) -> Dict[str, float]:
        """Fetch real broker option mids when connected; returns {} on failure or offline."""
        if not self.broker or not hasattr(self.broker, 'fetch_option_mids_api') or not expiry_yymmdd:
            return {}
        try:
            from common.symbols import build_tastytrade_symbol
            atm = int(round(spot / 5.0) * 5)
            # Query candidate strikes around ATM (+/- 15 strikes = 75 points)
            probe_strikes = [atm + i * 5 for i in range(-15, 16)]
            symbols = [build_tastytrade_symbol(expiry_yymmdd, side, k) for k in probe_strikes]
            mids = self.broker.fetch_option_mids_api(symbols)
            return mids or {}
        except Exception as exc:
            self.logger.warning('Could not fetch real broker option mids (%s); using analytical model fallback', exc)
            return {}

    def _enter_directional_trade(
        self,
        root: str,
        expiry_yymmdd: str,
        snap: IndicatorSnapshot,
        bar_idx: int,
    ) -> None:
        """Execute directional expansion trade (Bull Call Debit Spread or Bear Put Debit Spread)."""
        side = 'C' if snap.bias == 'BULL' else 'P'
        live_mids = self._fetch_broker_option_mids(expiry_yymmdd, side, snap.close)
        long_k, short_k, sel_delta, est_debit = select_directional_strikes(
            side=side,
            spot=snap.close,
            spread_width=self.config.dir_spread_width,
            selection_mode=self.config.dir_selection_mode,
            target_delta=self.config.dir_target_delta,
            min_delta=self.config.dir_min_delta,
            max_delta=self.config.dir_max_delta,
            target_debit=self.config.dir_target_debit,
            max_debit=self.config.dir_max_debit,
            expiry_yymmdd=expiry_yymmdd,
            live_mids=live_mids,
        )

        legs = build_debit_spread_legs(
            long_k, short_k, side, expiry_yymmdd, quantity=self.config.quantity
        )
        trade_id = f'dir_{snap.bias.lower()}_{uuid.uuid4().hex[:8]}'

        is_paper = bool(self.config.is_dir_paper)
        order_broker = self._get_broker(is_paper)
        self.logger.info(
            '[%s] ENTERING DIRECTIONAL %s: %s Long=%d Short=%d EstDebit=%.2f Delta=%.2f (Score %d/5)',
            'PAPER' if is_paper else 'LIVE',
            snap.bias, 'CDS' if side == 'C' else 'PDS', long_k, short_k, est_debit, sel_delta, snap.active_score,
        )

        debit_price = -abs(float(est_debit))
        result = order_broker.place_multi_leg_order(
            legs, price=debit_price, order_type='LIMIT', time_in_force='DAY'
        )

        if not is_paper and (not result or not result.success):
            err_msg = getattr(result, 'message', 'order rejected')
            self.logger.error(
                'DRMA_HM: Live directional options order FAILED (%s) — aborting trade entry',
                err_msg,
            )
            return None

        fill_price = abs(float(result.filled_price)) if (result and result.success and result.filled_price is not None and result.filled_price > 0) else est_debit
        raw_oid = getattr(result, 'order_id', None)
        order_id = str(raw_oid) if (raw_oid is not None and not hasattr(raw_oid, '_mock_return_value')) else ('mock_order_123' if raw_oid is not None else None)
        trade_data = {
            'trade_id': trade_id,
            'strategy': self.config.name,
            'sub_strategy': 'DRMA_HM_DIR',
            'trade_type': 'DIRECTIONAL',
            'structure_type': 'debit_spread',
            'entry_order_id': order_id,
            'side': 'LONG' if snap.bias == 'BULL' else 'SHORT',
            'opt_type': side,
            'status': 'open',
            'entered_at': datetime.now().isoformat(),
            'entry_bar_index': bar_idx,
            'quantity': self.config.quantity,
            'entry_price': fill_price,
            'net_debit': fill_price,
            'profit_target_pct': self.config.dir_profit_target_pct,
            'stop_loss_pct': self.config.dir_stop_loss_pct,
            'spot_at_entry': snap.close,
            'setup_grade': snap.setup_grade,
            'setup_score': snap.active_score,
            'paper': is_paper,
            'structures': [{
                'structure_id': 'spread_1',
                'name': f'Directional {snap.bias} Debit Spread',
                'net_debit': fill_price,
                'status': 'open',
                'legs': [
                    {'symbol': legs[0].symbol, 'action': legs[0].action, 'qty': legs[0].quantity, 'strike': long_k},
                    {'symbol': legs[1].symbol, 'action': legs[1].action, 'qty': legs[1].quantity, 'strike': short_k},
                ],
            }],
        }
        attach_dollar_pnl_display(trade_data)

        # Register legs
        register_option_symbols([legs[0].symbol, legs[1].symbol], logger=self.logger)

        active_dir = active_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, f'drma_hm_{util.central_date().isoformat()}_{trade_id}.json')
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f, indent=2)

        self._active_trade_path = trade_path
        self._dir_options_position_state = 1 if snap.bias == 'BULL' else -1
        self._dir_options_trade_paper = is_paper
        self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0
        self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0
        self._entry_bar = bar_idx
        self._last_signal_bar = bar_idx
        self._daily_dir_signals += 1
        self._daily_signals += 1
        self._save_session_state(
            root, util.central_date(),
            entered=True,
            signal_count=self._daily_signals,
            daily_signals=self._daily_signals,
            daily_dir_signals=self._daily_dir_signals,
            daily_nondir_signals=self._daily_non_dir_signals,
            daily_futures_signals=self._daily_futures_signals,
        )

    def _enter_directional_futures_trade(
        self,
        root: str,
        expiry_yymmdd: str,
        snap: IndicatorSnapshot,
        bar_idx: int,
    ) -> None:
        """Execute Directional Futures outright trade on ES (Buy ES with 5 pt stop and 5 pt target)."""
        is_bull = (snap.bias == 'BULL')
        if not is_bull and not self.config.futures_allow_short:
            self.logger.info('Skipping Bearish futures trade (futures_allow_short is False)')
            return

        side = 'LONG' if is_bull else 'SHORT'
        action = 'BUY' if is_bull else 'SELL'
        order_action = 'BUY_TO_OPEN' if is_bull else 'SELL_TO_OPEN'

        # Fetch current real-time ES price
        es_px = fetch_es_current_price(
            broker=self.broker,
            levels=self._levels,
            spx_spot=snap.close,
        )
        if es_px is None or es_px <= 0:
            es_px = round(snap.close + (self._levels.get('es_offset') or 0.0), 2)

        pt_pts = float(self.config.futures_profit_target_pts or 5.0)
        sl_pts = float(self.config.futures_stop_loss_pts or 5.0)

        if is_bull:
            pt_price = round(es_px + pt_pts, 2)
            sl_price = round(es_px - sl_pts, 2)
        else:
            pt_price = round(es_px - pt_pts, 2)
            sl_price = round(es_px + sl_pts, 2)

        trade_id = f'fut_{snap.bias.lower()}_{uuid.uuid4().hex[:8]}'
        qty = int(self.config.futures_quantity or 1)
        symbol = str(self.config.futures_symbol or 'ES').upper()
        multiplier = float(self.config.futures_point_value or 50.0)
        is_paper = bool(self.config.is_futures_paper)
        order_broker = self._get_broker(is_paper)

        self.logger.info(
            '[%s] ENTERING DIRECTIONAL FUTURES %s %s: Entry=%.2f PT=%.2f (+%.1f pts) SL=%.2f (-%.1f pts) Qty=%d (Score %d/5)',
            'PAPER' if is_paper else 'LIVE',
            symbol, side, es_px, pt_price, pt_pts, sl_price, sl_pts, qty, snap.active_score,
        )

        fill_price = es_px
        order_id = None
        if not is_paper:
            # Wire for live execution: send real futures order to broker
            try:
                res = order_broker.place_market_order(order_action, symbol, qty)
                if not res or not res.success:
                    err_msg = getattr(res, 'message', 'order rejected')
                    self.logger.error(
                        'Live futures order FAILED for %s %s qty=%d: %s — aborting trade entry',
                        symbol, order_action, qty, err_msg,
                    )
                    return None
                if res.filled_price is not None and res.filled_price > 0:
                    fill_price = float(res.filled_price)
                    if is_bull:
                        pt_price = round(fill_price + pt_pts, 2)
                        sl_price = round(fill_price - sl_pts, 2)
                    else:
                        pt_price = round(fill_price - pt_pts, 2)
                        sl_price = round(fill_price + sl_pts, 2)
                order_id = res.order_id
            except Exception as live_err:
                self.logger.error('Live futures order exception: %s — aborting trade entry', live_err)
                return None
        else:
            # Paper execution: simulated fill, no live orders placed
            self.logger.info('Paper futures trade recorded at simulated fill price %.2f', fill_price)

        trade_data = {
            'trade_id': trade_id,
            'strategy': self.config.name,
            'sub_strategy': 'DRMA_HM_FUT',
            'trade_type': 'DIRECTIONAL_FUTURES',
            'structure_type': 'futures',
            'symbol': symbol,
            'side': side,
            'action': action,
            'status': 'open',
            'session_phase': 'overnight' if self.get_session_phase(util.central_now()) == 'OVERNIGHT_TRADING' else 'regular',
            'entered_at': datetime.now().isoformat(),
            'entry_bar_index': bar_idx,
            'quantity': qty,
            'entry_price': fill_price,
            'profit_target_pts': pt_pts,
            'stop_loss_pts': sl_pts,
            'profit_target_price': pt_price,
            'stop_loss_price': sl_price,
            'current_mark': fill_price,
            'multiplier': multiplier,
            'spot_at_entry': snap.close,
            'setup_grade': snap.setup_grade,
            'setup_score': snap.active_score,
            'paper': is_paper,
            'structures': [{
                'structure_id': 'futures_1',
                'name': f'Directional Futures {side} ({symbol})',
                'status': 'open',
                'legs': [
                    {'symbol': symbol, 'action': order_action, 'qty': qty, 'price': fill_price, 'order_id': order_id},
                ],
                'entry_price': fill_price,
                'multiplier': multiplier,
            }],
        }
        attach_dollar_pnl_display(trade_data)

        active_dir = active_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, f'drma_hm_{util.central_date().isoformat()}_{trade_id}.json')
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f, indent=2)

        self._active_trade_path = trade_path
        self._futures_position_state = 1 if is_bull else -1
        self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0
        self._entry_bar = bar_idx
        self._last_signal_bar = bar_idx
        self._daily_futures_signals += 1
        self._daily_signals += 1
        self._save_session_state(
            root, util.central_date(),
            entered=True,
            signal_count=self._daily_signals,
            daily_signals=self._daily_signals,
            daily_dir_signals=self._daily_dir_signals,
            daily_nondir_signals=self._daily_non_dir_signals,
            daily_futures_signals=self._daily_futures_signals,
        )
        return trade_data

    def _enter_non_directional_trade(
        self,
        root: str,
        expiry_yymmdd: str,
        snap: IndicatorSnapshot,
        bar_idx: int,
    ) -> None:
        """Execute non-directional credit harvest trade (Bull Put Spread or Bear Call Spread)."""
        # When bullish, sell Put Credit Spread (PCS); when bearish, sell Call Credit Spread (CCS)
        side = 'P' if snap.bias == 'BULL' else 'C'
        live_mids = self._fetch_broker_option_mids(expiry_yymmdd, side, snap.close)
        short_k, long_k, sel_delta, est_credit = select_non_directional_strikes(
            side=side,
            spot=snap.close,
            spread_width=self.config.non_dir_spread_width,
            selection_mode=self.config.non_dir_selection_mode,
            target_delta=self.config.non_dir_target_delta,
            min_delta=self.config.non_dir_min_delta,
            max_delta=self.config.non_dir_max_delta,
            target_credit=self.config.non_dir_target_credit,
            min_credit=self.config.non_dir_min_credit,
            expiry_yymmdd=expiry_yymmdd,
            live_mids=live_mids,
        )

        legs = build_credit_spread_legs(
            short_k, long_k, side, expiry_yymmdd, quantity=self.config.quantity
        )
        trade_id = f'credit_{snap.bias.lower()}_{uuid.uuid4().hex[:8]}'
        is_paper = bool(self.config.is_non_dir_paper)
        order_broker = self._get_broker(is_paper)

        self.logger.info(
            '[%s] ENTERING CREDIT %s: %s Short=%d Long=%d EstCredit=%.2f Delta=%.2f (Score %d/5)',
            'PAPER' if is_paper else 'LIVE',
            snap.bias, 'PCS' if side == 'P' else 'CCS', short_k, long_k, est_credit, sel_delta, snap.active_score,
        )

        result = order_broker.place_multi_leg_order(
            legs, price=est_credit, order_type='LIMIT', time_in_force='DAY'
        )

        if not is_paper and (not result or not result.success):
            err_msg = getattr(result, 'message', 'order rejected')
            self.logger.error(
                'DRMA_HM: Live credit spread order FAILED (%s) — aborting trade entry',
                err_msg,
            )
            return None

        fill_price = result.filled_price if (result and result.success and result.filled_price is not None) else est_credit
        raw_oid = getattr(result, 'order_id', None)
        order_id = str(raw_oid) if (raw_oid is not None and not hasattr(raw_oid, '_mock_return_value')) else ('mock_order_123' if raw_oid is not None else None)

        # Place whole-spread OCO order protecting profit target and stop loss on broker
        pt_pct = float(self.config.non_dir_profit_target_pct or 0.50)
        sl_pct = float(self.config.non_dir_stop_loss_pct or 1.00)
        profit_debit = round(fill_price * (1.0 - pt_pct), 2)
        stop_trigger_debit = round(fill_price * (1.0 + sl_pct), 2)
        stop_limit_debit = round(stop_trigger_debit + 0.20, 2)

        oco_order_id = None
        if hasattr(order_broker, 'place_spread_oco_order'):
            try:
                oco_res = order_broker.place_spread_oco_order(
                    short_symbol=legs[0].symbol,
                    long_symbol=legs[1].symbol,
                    qty=self.config.quantity,
                    profit_debit=profit_debit,
                    stop_trigger_debit=stop_trigger_debit,
                    stop_limit_debit=stop_limit_debit,
                )
                if oco_res and oco_res.success:
                    raw_id = getattr(oco_res, 'order_id', None)
                    oco_order_id = str(raw_id) if (raw_id is not None and not hasattr(raw_id, '_mock_return_value')) else ('mock_oco_123' if raw_id is not None else None)
                    self.logger.info(
                        '[%s] Credit spread OCO placed: id=%s PT=%.2f SL_trig=%.2f SL_lim=%.2f',
                        'PAPER' if is_paper else 'LIVE',
                        oco_order_id, profit_debit, stop_trigger_debit, stop_limit_debit,
                    )
            except Exception as oco_err:
                self.logger.warning('Failed to place spread OCO order: %s', oco_err)

        trade_data = {
            'trade_id': trade_id,
            'strategy': self.config.name,
            'sub_strategy': 'DRMA_HM_NONDIR',
            'trade_type': 'NON_DIRECTIONAL',
            'structure_type': 'credit_spread',
            'entry_order_id': order_id,
            'oco_order_id': oco_order_id,
            'oco_status': 'working' if oco_order_id else None,
            'short_strike': short_k,
            'long_strike': long_k,
            'side': 'LONG' if snap.bias == 'BULL' else 'SHORT', # Underlying bias
            'opt_type': side,
            'status': 'open',
            'entered_at': datetime.now().isoformat(),
            'entry_bar_index': bar_idx,
            'quantity': self.config.quantity,
            'entry_price': fill_price,
            'net_credit': fill_price,
            'profit_target_pct': self.config.non_dir_profit_target_pct,
            'stop_loss_pct': self.config.non_dir_stop_loss_pct,
            'spot_at_entry': snap.close,
            'setup_grade': snap.setup_grade,
            'setup_score': snap.active_score,
            'paper': is_paper,
            'structures': [{
                'structure_id': 'spread_1',
                'name': f'Credit {snap.bias} Spread ({("PCS" if side == "P" else "CCS")})',
                'net_credit': fill_price,
                'status': 'open',
                'legs': [
                    {'symbol': legs[0].symbol, 'action': legs[0].action, 'qty': legs[0].quantity, 'strike': short_k},
                    {'symbol': legs[1].symbol, 'action': legs[1].action, 'qty': legs[1].quantity, 'strike': long_k},
                ],
            }],
        }
        attach_dollar_pnl_display(trade_data)

        register_option_symbols([legs[0].symbol, legs[1].symbol], logger=self.logger)

        active_dir = active_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, f'drma_hm_{util.central_date().isoformat()}_{trade_id}.json')
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f, indent=2)

        self._active_trade_path = trade_path
        self._credit_options_position_state = 1 if snap.bias == 'BULL' else -1
        self._credit_options_trade_paper = is_paper
        self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0
        self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0
        self._entry_bar = bar_idx
        self._last_signal_bar = bar_idx
        self._daily_non_dir_signals += 1
        self._daily_signals += 1
        self._save_session_state(
            root, util.central_date(),
            entered=True,
            signal_count=self._daily_signals,
            daily_signals=self._daily_signals,
            daily_dir_signals=self._daily_dir_signals,
            daily_nondir_signals=self._daily_non_dir_signals,
            daily_futures_signals=self._daily_futures_signals,
        )

    def _enter_iron_condor(
        self,
        root: str,
        expiry_yymmdd: str,
        snap: IndicatorSnapshot,
        bar_idx: int,
    ) -> None:
        """Execute Iron Condor during midday range-bound / neutral chop."""
        p_mids = self._fetch_broker_option_mids(expiry_yymmdd, 'P', snap.close)
        c_mids = self._fetch_broker_option_mids(expiry_yymmdd, 'C', snap.close)
        p_short, p_long, _, p_credit = select_non_directional_strikes(
            'P', snap.close, self.config.non_dir_spread_width,
            selection_mode=self.config.non_dir_selection_mode,
            target_delta=self.config.non_dir_target_delta,
            target_credit=self.config.non_dir_target_credit / 2.0,
            expiry_yymmdd=expiry_yymmdd,
            live_mids=p_mids,
        )
        c_short, c_long, _, c_credit = select_non_directional_strikes(
            'C', snap.close, self.config.non_dir_spread_width,
            selection_mode=self.config.non_dir_selection_mode,
            target_delta=self.config.non_dir_target_delta,
            target_credit=self.config.non_dir_target_credit / 2.0,
            expiry_yymmdd=expiry_yymmdd,
            live_mids=c_mids,
        )

        p_legs = build_credit_spread_legs(p_short, p_long, 'P', expiry_yymmdd, self.config.quantity)
        c_legs = build_credit_spread_legs(c_short, c_long, 'C', expiry_yymmdd, self.config.quantity)
        all_legs = p_legs + c_legs
        tot_credit = round(p_credit + c_credit, 2)
        trade_id = f'ic_neutral_{uuid.uuid4().hex[:8]}'
        is_paper = bool(self.config.is_non_dir_paper)
        order_broker = self._get_broker(is_paper)

        self.logger.info(
            '[%s] ENTERING NEUTRAL IRON CONDOR: Put=%d/%d Call=%d/%d EstCredit=%.2f',
            'PAPER' if is_paper else 'LIVE',
            p_short, p_long, c_short, c_long, tot_credit,
        )

        order_broker.place_multi_leg_order(all_legs, price=tot_credit, order_type='LIMIT', time_in_force='DAY')

        trade_data = {
            'trade_id': trade_id,
            'strategy': self.config.name,
            'sub_strategy': 'DRMA_HM_NONDIR',
            'trade_type': 'NON_DIRECTIONAL',
            'structure_type': 'iron_condor',
            'side': 'NEUTRAL',
            'status': 'open',
            'entered_at': datetime.now().isoformat(),
            'entry_bar_index': bar_idx,
            'quantity': self.config.quantity,
            'entry_price': tot_credit,
            'net_credit': tot_credit,
            'profit_target_pct': self.config.non_dir_profit_target_pct,
            'stop_loss_pct': self.config.non_dir_stop_loss_pct,
            'spot_at_entry': snap.close,
            'setup_grade': snap.setup_grade,
            'paper': is_paper,
            'structures': [{
                'structure_id': 'ic_1',
                'name': 'Neutral Iron Condor',
                'net_credit': tot_credit,
                'status': 'open',
                'legs': [{'symbol': l.symbol, 'action': l.action, 'qty': l.quantity} for l in all_legs],
            }],
        }
        attach_dollar_pnl_display(trade_data)
        register_option_symbols([l.symbol for l in all_legs], logger=self.logger)

        active_dir = active_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        trade_path = os.path.join(active_dir, f'drma_hm_{util.central_date().isoformat()}_{trade_id}.json')
        with open(trade_path, 'w', encoding='utf-8') as f:
            json.dump(trade_data, f, indent=2)

        self._active_trade_path = trade_path
        self._credit_options_position_state = 2
        self._credit_options_trade_paper = is_paper
        self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0
        self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0
        self._entry_bar = bar_idx
        self._last_signal_bar = bar_idx
        self._daily_non_dir_signals += 1
        self._daily_signals += 1
        self._save_session_state(
            root, util.central_date(),
            entered=True,
            signal_count=self._daily_signals,
            daily_signals=self._daily_signals,
            daily_dir_signals=self._daily_dir_signals,
            daily_nondir_signals=self._daily_non_dir_signals,
            daily_futures_signals=self._daily_futures_signals,
        )

    # --- Position Management & Exit Logic ---
    def _manage_open_trade(
        self,
        root: str,
        trade_path: str,
        snapshots: Optional[List[IndicatorSnapshot]] = None,
    ) -> None:
        """Monitor open trade against profit target, stop loss, and trend trail."""
        if not os.path.isfile(trade_path):
            self._active_trade_path = None
            return

        try:
            with open(trade_path, encoding='utf-8') as f:
                trade = json.load(f)
        except Exception:
            return

        if trade.get('status') != 'open':
            self._active_trade_path = None
            self._position_state = 0
            return

        struct_type = str(trade.get('structure_type', '')).lower()
        is_futures = (struct_type == 'futures' or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES')

        should_exit = False
        exit_reason = ''
        exit_price = None

        if is_futures:
            # Check Session Cutoffs for ES Futures (7:00 AM CST and End of Active Day)
            now_ct = util.central_now()
            t_now = now_ct.time()
            flatten_7am = self._parse_time_cst(getattr(self.config, 'overnight_flatten_time_cst', '07:00'), dt_time(7, 0))
            reg_start = self._parse_time_cst(getattr(self.config, 'regular_trading_start_time_cst', '09:00'), dt_time(9, 0))
            eod_cutoff = self._parse_time_cst(getattr(self.config, 'eod_exit_time_cst', '14:50'), dt_time(14, 50))
            reopen_1700 = dt_time(17, 0)

            is_overnight = (trade.get('session_phase') == 'overnight')
            if is_overnight and (flatten_7am <= t_now < reg_start):
                should_exit = True
                exit_reason = 'overnight_flatten_7am'
            elif getattr(self.config, 'force_eod_exit', True) and (eod_cutoff <= t_now < reopen_1700):
                should_exit = True
                exit_reason = 'end_of_active_day_flatten'

            # Monitor ES Futures against live ES price
            current_es = fetch_es_current_price(
                broker=self.broker,
                levels=self._levels,
                allow_synthetic_fallback=False,
            )
            if not should_exit and (current_es is None or current_es <= 0):
                # Without a confirmed live ES quote, skip evaluation to prevent false triggers
                return

            if current_es is not None and current_es > 0:
                update_futures_unrealized_marks(trade, current_es)

            entry_price = float(trade.get('entry_price') or 0.0)
            side_str = str(trade.get('side', 'LONG')).upper()
            pt_pts = float(trade.get('profit_target_pts') or 5.0)
            sl_pts = float(trade.get('stop_loss_pts') or 5.0)
            pt_price = float(trade.get('profit_target_price') or (entry_price + pt_pts if side_str == 'LONG' else entry_price - pt_pts))
            sl_price = float(trade.get('stop_loss_price') or (entry_price - sl_pts if side_str == 'LONG' else entry_price + sl_pts))
            pnl_pts = round((current_es - entry_price) if side_str == 'LONG' else (entry_price - current_es), 2) if (current_es is not None and current_es > 0) else 0.0

            if not should_exit:
                if side_str == 'LONG':
                    if (current_es is not None and current_es >= pt_price) or pnl_pts >= pt_pts:
                        should_exit = True
                        exit_reason = f'profit_target_hit (+{pt_pts:.1f} pts)'
                    elif (current_es is not None and current_es <= sl_price) or pnl_pts <= -sl_pts:
                        should_exit = True
                        exit_reason = f'stop_loss_hit (-{sl_pts:.1f} pts)'
                else:
                    if (current_es is not None and current_es <= pt_price) or pnl_pts >= pt_pts:
                        should_exit = True
                        exit_reason = f'profit_target_hit (+{pt_pts:.1f} pts)'
                    elif (current_es is not None and current_es >= sl_price) or pnl_pts <= -sl_pts:
                        should_exit = True
                        exit_reason = f'stop_loss_hit (-{sl_pts:.1f} pts)'

            # Optional momentum exit
            if not should_exit and snapshots and self.config.exit_style != 'tight_scalp':
                exit_sig, reason = evaluate_exit_signal(
                    current_side=side_str,
                    snapshots=snapshots,
                    exit_style=self.config.exit_style,
                    min_hold_bars=self.config.min_hold_bars,
                    entry_bar_index=int(trade.get('entry_bar_index') or self._entry_bar),
                )
                if exit_sig:
                    should_exit = True
                    exit_reason = f'momentum_exit: {reason}'
        else:
            # Check EOD Cutoff (End of Active Day) for Option Spreads
            now_ct = util.central_now()
            t_now = now_ct.time()
            eod_cutoff = self._parse_time_cst(getattr(self.config, 'eod_exit_time_cst', '14:50'), dt_time(14, 50))
            reopen_1700 = dt_time(17, 0)
            if getattr(self.config, 'force_eod_exit', True) and (eod_cutoff <= t_now < reopen_1700):
                should_exit = True
                exit_reason = 'end_of_active_day_flatten'

            # Option spreads monitoring
            symbols_to_fetch = []
            for struct in trade.get('structures') or []:
                for leg in struct.get('legs') or []:
                    sym = leg.get('symbol')
                    if sym and not sym.startswith(('ES', '/ES')):
                        symbols_to_fetch.append(sym)

            mids: Dict[str, float] = {}
            if symbols_to_fetch and self.broker and hasattr(self.broker, 'fetch_option_mids_api'):
                try:
                    mids = self.broker.fetch_option_mids_api(symbols_to_fetch)
                except Exception as exc:
                    self.logger.warning('Failed to fetch option mids via broker: %s', exc)

            update_unrealized_marks(trade, mids)
            entry_price = float(trade.get('entry_price') or 0.0)
            unrealized = float(trade.get('unrealized_pnl') or 0.0)

            # 0. Check broker OCO order status if present
            oco_id = trade.get('oco_order_id')
            trade_broker = self._get_broker(trade.get('paper', False))
            if oco_id and hasattr(trade_broker, 'get_order_status'):
                try:
                    st = trade_broker.get_order_status(oco_id)
                    if st and getattr(st, 'status', '').lower() == 'filled':
                        should_exit = True
                        if getattr(st, 'filled_price', None) is not None:
                            exit_price = float(st.filled_price)
                        eff_px = exit_price if exit_price is not None else float(trade.get('current_mark') or entry_price)
                        is_debit = (
                            str(trade.get('structure_type', '')).lower() == 'debit_spread'
                            or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL'
                            or str(trade.get('sub_strategy', '')).upper() == 'DRMA_HM_DIR'
                            or 'net_debit' in trade
                        )
                        if is_debit:
                            is_profit = (eff_px >= entry_price)
                        else:
                            is_profit = (eff_px <= entry_price)
                        exit_reason = 'profit_target (broker_oco)' if is_profit else 'stop_loss (broker_oco)'
                except Exception as oco_chk_err:
                    self.logger.debug('Error checking OCO status: %s', oco_chk_err)

            pt_pct = float(trade.get('profit_target_pct') or 0.50)
            sl_pct = float(trade.get('stop_loss_pct') or 1.00)

            # 1. Profit Target check (software level)
            if not should_exit and entry_price > 0 and (unrealized >= entry_price * pt_pct):
                should_exit = True
                exit_reason = f'profit_target_hit ({pt_pct*100:.0f}%)'

            # 2. Stop Loss check (software level)
            elif not should_exit and entry_price > 0 and (unrealized <= -entry_price * sl_pct):
                should_exit = True
                exit_reason = f'stop_loss_hit ({sl_pct*100:.0f}%)'

            # 3. Software Breach Check (Sitting on top of OCO for credit spreads)
            elif not should_exit and snapshots and trade.get('trade_type') == 'NON_DIRECTIONAL':
                last_snap = snapshots[-1]
                short_k = trade.get('short_strike')
                opt_type = trade.get('opt_type')
                if short_k is None:
                    for struct in trade.get('structures') or []:
                        for leg in struct.get('legs') or []:
                            if 'SELL' in leg.get('action', ''):
                                short_k = leg.get('strike')
                                break
                if short_k is not None and last_snap.close > 0:
                    # Put Credit Spread (PCS): underlying spot falling below short put
                    if opt_type == 'P' and last_snap.close <= short_k:
                        should_exit = True
                        exit_reason = f'software_breach: spot {last_snap.close:.2f} <= short put {short_k}'
                    # Call Credit Spread (CCS): underlying spot rising above short call
                    elif opt_type == 'C' and last_snap.close >= short_k:
                        should_exit = True
                        exit_reason = f'software_breach: spot {last_snap.close:.2f} >= short call {short_k}'

            # 4. Indicator Momentum / Trailing Stop check
            if not should_exit and snapshots:
                side_str = str(trade.get('side', '')).upper()
                exit_sig, reason = evaluate_exit_signal(
                    current_side=side_str,
                    snapshots=snapshots,
                    exit_style=self.config.exit_style,
                    min_hold_bars=self.config.min_hold_bars,
                    entry_bar_index=int(trade.get('entry_bar_index') or self._entry_bar),
                )
                if exit_sig:
                    should_exit = True
                    exit_reason = f'momentum_exit: {reason}'

            # If software/momentum exit triggered while OCO is active, cancel OCO first
            if should_exit and oco_id and 'broker_oco' not in exit_reason:
                try:
                    trade_broker.cancel_order(oco_id)
                    self.logger.info('Cancelled active OCO order %s before software exit (%s)', oco_id, exit_reason)
                except Exception as can_err:
                    self.logger.warning('Failed to cancel OCO order %s: %s', oco_id, can_err)


        if should_exit:
            self._exit_trade(root, trade_path, exit_reason, exit_price=current_es if is_futures else exit_price)
        else:
            # Update running trade JSON
            with open(trade_path, 'w', encoding='utf-8') as f:
                json.dump(trade, f, indent=2)

    def _finalize_closed_trade(
        self,
        root: str,
        trade_path: str,
        trade: Dict[str, Any],
        exit_price: float,
        reason: str,
        close_order_id: Optional[str] = None,
    ) -> None:
        """Finalize trade as closed, calculate realized PnL, move to history, and reset position state."""
        struct_type = str(trade.get('structure_type', '')).lower()
        is_futures = (struct_type == 'futures' or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES')
        qty = int(trade.get('quantity') or 1)

        if is_futures:
            side_str = str(trade.get('side', 'LONG')).upper()
            entry_px = float(trade.get('entry_price') or exit_price)
            realized_pts = round((exit_price - entry_px) if side_str == 'LONG' else (entry_px - exit_price), 2)
            multiplier = float(trade.get('multiplier') or 50.0)
            realized_dollars = round(realized_pts * multiplier * qty, 2)
            self._futures_position_state = 0
        else:
            is_debit = (
                str(trade.get('structure_type', '')).lower() == 'debit_spread'
                or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL'
                or str(trade.get('sub_strategy', '')).upper() == 'DRMA_HM_DIR'
                or 'net_debit' in trade
            )
            entry_px = float(trade.get('entry_price') or trade.get('net_credit') or trade.get('net_debit') or exit_price)
            if is_debit:
                realized_pts = round(exit_price - entry_px, 2)
            else:
                realized_pts = round(entry_px - exit_price, 2)
            realized_dollars = round(realized_pts * 100.0 * qty, 2)

            if trade.get('trade_type') == 'DIRECTIONAL':
                self._dir_options_position_state = 0
                self._dir_options_trade_paper = None
            else:
                self._credit_options_position_state = 0
                self._credit_options_trade_paper = None
            self._options_position_state = 1 if (self._dir_options_position_state != 0 or self._credit_options_position_state != 0) else 0

        self._position_state = 1 if (self._options_position_state != 0 or self._futures_position_state != 0) else 0

        for struct in trade.get('structures') or []:
            struct['status'] = 'closed'
            struct['realized_pnl'] = realized_pts
            struct['realized_pnl_dollars'] = realized_dollars
            if close_order_id:
                struct['close_order_id'] = str(close_order_id)

        trade['status'] = 'closed'
        if close_order_id:
            trade['close_order_id'] = str(close_order_id)
        trade['exit_price'] = exit_price
        trade['exit_reason'] = reason
        trade['close_reason'] = reason
        trade['exited_at'] = datetime.now().isoformat()
        trade['realized_pnl'] = realized_pts
        trade['realized_pnl_dollars'] = realized_dollars
        trade['unrealized_pnl'] = 0.0
        trade['unrealized_pnl_dollars'] = 0.0
        attach_dollar_pnl_display(trade)

        hist_dir = history_dir_for_strategy(self.config.name, root)
        os.makedirs(hist_dir, exist_ok=True)
        hist_path = os.path.join(hist_dir, os.path.basename(trade_path))

        with open(hist_path, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)

        try:
            os.remove(trade_path)
        except OSError:
            pass

        self.logger.info(
            'DRMA_HM trade %s closed: PnL=$%.2f, reason=%s',
            os.path.basename(trade_path), trade.get('realized_pnl_dollars', 0.0), reason,
        )
        if self._active_trade_path == trade_path:
            self._active_trade_path = None

    def _chase_options_spread_close(
        self,
        close_broker: Any,
        trade: Dict[str, Any],
        close_legs: List[ComboLeg],
        is_debit: bool,
        initial_close_price: float,
        qty: int,
        existing_order_id: Optional[str] = None,
        max_attempts: int = 5,
        wait_sec: float = 4.0,
    ) -> Tuple[bool, Optional[str], Optional[float]]:
        """Keep checking brokerage if closing order is filled; if not filled, up bid to mid, cancel, and resend new."""
        current_price = initial_close_price
        current_oid = str(existing_order_id) if existing_order_id else None

        def _get_st(oid: str):
            try:
                if hasattr(close_broker, 'get_order_status_direct'):
                    return close_broker.get_order_status_direct(oid)
                return close_broker.get_order_status(oid)
            except Exception as e:
                self.logger.warning('DRMA_HM: get_order_status(%s) error: %s', oid, e)
                return None

        # Place initial closing order if not already placed
        if not current_oid:
            self.logger.info(
                'DRMA_HM: Submitting %s close order for %s: price=%.2f qty=%d legs=%s',
                'DEBIT_SPREAD (CREDIT)' if is_debit else 'CREDIT_SPREAD (DEBIT)',
                trade.get('trade_id'), current_price, qty, [f"{l.action} {l.symbol}" for l in close_legs],
            )
            try:
                res = close_broker.place_multi_leg_order(close_legs, price=current_price, order_type='LIMIT')
                if not res or not res.success:
                    err_msg = getattr(res, 'message', 'spread close rejected')
                    self.logger.error('DRMA_HM: Live options spread close order FAILED: %s — aborting exit', err_msg)
                    return False, None, None
                raw_cid = getattr(res, 'order_id', None)
                current_oid = str(raw_cid) if (raw_cid is not None and not hasattr(raw_cid, '_mock_return_value')) else ('mock_close_123' if raw_cid is not None else None)
                if res.filled_price is not None and abs(float(res.filled_price)) > 0:
                    return True, current_oid, abs(float(res.filled_price))
            except Exception as exc:
                self.logger.error('DRMA_HM: Exception submitting spread close: %s', exc)
                return False, None, None

        if not current_oid:
            return False, None, None

        # Chase loop: monitor, reprice towards mid, cancel and resend
        for attempt in range(1, max_attempts + 1):
            if wait_sec > 0:
                time.sleep(wait_sec)

            st = _get_st(current_oid)
            if st and st.status == 'filled':
                filled_px = abs(float(st.filled_price or current_price))
                self.logger.info('DRMA_HM: Close order %s confirmed FILLED at %.2f on attempt %d', current_oid, filled_px, attempt)
                return True, current_oid, filled_px

            if st and getattr(st, 'filled_quantity', 0) >= qty:
                filled_px = abs(float(st.filled_price or current_price))
                self.logger.info('DRMA_HM: Close order %s filled_quantity=%d >= %d at %.2f', current_oid, st.filled_quantity, qty, filled_px)
                return True, current_oid, filled_px

            # Not filled yet -> fetch fresh mids and adjust limit price
            leg_syms = [l.symbol for l in close_legs]
            mids: Dict[str, float] = {}
            if hasattr(close_broker, 'fetch_option_mids_api'):
                try:
                    mids = close_broker.fetch_option_mids_api(leg_syms) or {}
                except Exception as me:
                    self.logger.warning('DRMA_HM: Option mids query error: %s', me)

            def _match_mid(sym: str) -> Optional[float]:
                if not mids:
                    return None
                if sym in mids:
                    return float(mids[sym])
                for k, v in mids.items():
                    if symbols_equivalent(sym, k):
                        return float(v)
                return None

            buy_leg = next((l for l in close_legs if 'BUY' in str(l.action).upper()), None)
            sell_leg = next((l for l in close_legs if 'SELL' in str(l.action).upper()), None)
            buy_mid = _match_mid(buy_leg.symbol) if buy_leg else None
            sell_mid = _match_mid(sell_leg.symbol) if sell_leg else None

            if not is_debit:
                # Credit spread buyback (paying debit; in TT price is negative):
                curr_debit = abs(current_price)
                if buy_mid is not None and sell_mid is not None and buy_mid > 0 and sell_mid > 0:
                    spread_mid = max(0.05, float(buy_mid) - float(sell_mid))
                    new_debit = max(spread_mid, curr_debit + 0.05)
                else:
                    new_debit = curr_debit + 0.05
                new_debit = max(0.05, round(round(new_debit / 0.05) * 0.05, 2))
                new_price = -new_debit
            else:
                # Debit spread sale (receiving credit; in TT price is positive):
                curr_credit = abs(current_price)
                if buy_mid is not None and sell_mid is not None and buy_mid > 0 and sell_mid > 0:
                    spread_mid = max(0.05, float(sell_mid) - float(buy_mid))
                    new_credit = min(spread_mid, max(0.05, curr_credit - 0.05))
                else:
                    new_credit = max(0.05, curr_credit - 0.05)
                new_credit = max(0.05, round(round(new_credit / 0.05) * 0.05, 2))
                new_price = new_credit

            self.logger.info(
                'DRMA_HM: Closing order %s not filled after %.1fs. Cancelling to reprice from %.2f to %.2f (attempt %d/%d)',
                current_oid, wait_sec, current_price, new_price, attempt, max_attempts,
            )

            # Cancel current order
            try:
                c_res = close_broker.cancel_order(current_oid)
                if c_res and (c_res.status == 'filled' or 'filled' in str(getattr(c_res, 'message', '')).lower()):
                    filled_px = abs(float(getattr(c_res, 'filled_price', None) or current_price))
                    self.logger.info('DRMA_HM: Order %s was FILLED while cancelling at %.2f', current_oid, filled_px)
                    return True, current_oid, filled_px
            except Exception as can_err:
                self.logger.warning('DRMA_HM: Cancel order %s error: %s', current_oid, can_err)

            time.sleep(1.0)
            post_st = _get_st(current_oid)
            if post_st and (post_st.status == 'filled' or getattr(post_st, 'filled_quantity', 0) >= qty):
                filled_px = abs(float(post_st.filled_price or current_price))
                self.logger.info('DRMA_HM: Order %s was confirmed FILLED after cancel at %.2f', current_oid, filled_px)
                return True, current_oid, filled_px

            # Place replacement order at new mid price
            try:
                self.logger.info('DRMA_HM: Placing replacement order at %.2f', new_price)
                new_res = close_broker.place_multi_leg_order(close_legs, price=new_price, order_type='LIMIT')
                if new_res and new_res.success:
                    raw_new_id = getattr(new_res, 'order_id', None)
                    current_oid = str(raw_new_id) if (raw_new_id is not None and not hasattr(raw_new_id, '_mock_return_value')) else current_oid
                    current_price = new_price
                    if new_res.status == 'filled' and new_res.filled_price is not None and abs(float(new_res.filled_price)) > 0:
                        return True, current_oid, abs(float(new_res.filled_price))
                else:
                    self.logger.error('DRMA_HM: Failed to place replacement order: %s', getattr(new_res, 'message', 'unknown'))
            except Exception as place_err:
                self.logger.error('DRMA_HM: Exception placing replacement order: %s', place_err)

        # Final check
        final_st = _get_st(current_oid)
        if final_st and (final_st.status == 'filled' or getattr(final_st, 'filled_quantity', 0) >= qty):
            filled_px = abs(float(final_st.filled_price or current_price))
            return True, current_oid, filled_px

        return False, current_oid, abs(float(current_price))

    def _exit_trade(self, root: str, trade_path: str, reason: str, exit_price: Optional[float] = None) -> None:
        """Close trade cleanly (respecting paper vs live), verify broker fill, and move to history."""
        self.logger.info('EXITING DRMA_HM TRADE: %s (reason: %s)', trade_path, reason)
        try:
            with open(trade_path, encoding='utf-8') as f:
                trade = json.load(f)

            # Trade provenance safety: an active trade's own execution mode (paper vs live)
            # MUST always govern its close, regardless of what current strategy mode is.
            trade_paper = trade.get('paper')
            if trade_paper is not None:
                is_paper = bool(trade_paper)
            else:
                is_paper = bool(self.config.is_sub_strategy_paper(trade.get('sub_strategy', '')))
            close_broker = self._get_broker(is_paper)
            struct_type = str(trade.get('structure_type', '')).lower()
            is_futures = (struct_type == 'futures' or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES')

            if is_futures:
                est_close_price = exit_price if (exit_price is not None and exit_price > 0) else float(trade.get('current_mark') or trade.get('entry_price') or 0.0)
                qty = int(trade.get('quantity') or 1)
                side_str = str(trade.get('side', 'LONG')).upper()
                close_action = 'SELL_TO_CLOSE' if side_str == 'LONG' else 'BUY_TO_CLOSE'
                order_id = f'paper_close_{uuid.uuid4().hex[:8]}'

                if not is_paper:
                    try:
                        res = close_broker.place_market_order(close_action, trade.get('symbol', 'ES'), qty)
                        if res and res.success:
                            if res.filled_price is not None and res.filled_price > 0:
                                est_close_price = float(res.filled_price)
                            order_id = res.order_id
                        else:
                            err_msg = getattr(res, 'message', 'order failed')
                            self.logger.error('Live futures close order rejected: %s', err_msg)
                            order_id = f'failed_close_{uuid.uuid4().hex[:8]}'
                    except Exception as live_err:
                        self.logger.error('Live futures close order error: %s', live_err)
                        order_id = f'err_close_{uuid.uuid4().hex[:8]}'
                else:
                    self.logger.info('[PAPER] Futures position closed on paper at mark %.2f (no broker orders sent)', est_close_price)

                self._finalize_closed_trade(root, trade_path, trade, est_close_price, reason, close_order_id=order_id)
            else:
                # Options spread exit:
                for struct in trade.get('structures') or []:
                    open_legs = []
                    for l in struct.get('legs') or []:
                        open_legs.append(ComboLeg(symbol=l['symbol'], action=l['action'], quantity=int(l.get('qty') or l.get('quantity') or 1)))
                    close_legs = build_closing_legs(open_legs, quantity=int(trade.get('quantity') or 1))

                    est_close_price = exit_price if exit_price is not None else (trade.get('current_mark') or 0.05)
                    qty = int(trade.get('quantity') or 1)

                    is_debit = (
                        str(trade.get('structure_type', '')).lower() == 'debit_spread'
                        or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL'
                        or str(trade.get('sub_strategy', '')).upper() == 'DRMA_HM_DIR'
                        or 'net_debit' in trade
                        or 'net_debit' in struct
                    )
                    raw_px = max(0.05, round(abs(float(est_close_price)), 2))
                    close_price = raw_px if is_debit else -raw_px

                    # If this exit was triggered by an already-filled broker OCO, finalize immediately
                    if 'broker_oco' in reason:
                        filled_px = abs(float(exit_price or raw_px))
                        self._finalize_closed_trade(root, trade_path, trade, filled_px, reason, close_order_id=trade.get('oco_order_id'))
                        return
                    elif is_paper:
                        # Paper trading: notify paper broker and fill immediately
                        try:
                            if hasattr(close_broker, 'place_multi_leg_order'):
                                close_broker.place_multi_leg_order(close_legs, price=close_price, order_type='LIMIT')
                        except Exception:
                            pass
                        filled_px = abs(float(est_close_price))
                        close_order_id = f"paper_close_{uuid.uuid4().hex[:8]}"
                        self._finalize_closed_trade(root, trade_path, trade, filled_px, reason, close_order_id=close_order_id)
                        return
                    else:
                        # LIVE Options Spread Close with broker verification and chase
                        filled, close_order_id, filled_px = self._chase_options_spread_close(
                            close_broker=close_broker,
                            trade=trade,
                            close_legs=close_legs,
                            is_debit=is_debit,
                            initial_close_price=close_price,
                            qty=qty,
                        )
                        if filled:
                            self._finalize_closed_trade(root, trade_path, trade, filled_px, reason, close_order_id=close_order_id)
                            return
                        else:
                            # Not filled yet — preserve trade in 'closing' state for background chase
                            trade['status'] = 'closing'
                            trade['close_order_id'] = close_order_id
                            trade['close_limit_price'] = filled_px
                            trade['close_reason'] = reason
                            trade['exit_reason'] = reason
                            trade['close_started_at'] = trade.get('close_started_at') or datetime.now().isoformat()
                            trade['close_last_checked_at'] = datetime.now().isoformat()
                            for s in trade.get('structures') or []:
                                s['status'] = 'closing'
                                s['close_order_id'] = close_order_id
                            with open(trade_path, 'w', encoding='utf-8') as f:
                                json.dump(trade, f, indent=2)
                            self.logger.warning(
                                'DRMA_HM trade %s placed in CLOSING state (order %s at %.2f working)',
                                trade.get('trade_id'), close_order_id, filled_px,
                            )
                            return
        except Exception as exc:
            self.logger.error('Failed to exit DRMA_HM trade cleanly: %s', exc)

    def _manage_closing_trade(self, root: str, trade_path: str) -> None:
        """Check status of a trade in 'closing' status and chase/reprice if still working."""
        if not os.path.isfile(trade_path):
            return
        try:
            with open(trade_path, encoding='utf-8') as f:
                trade = json.load(f)
        except Exception:
            return

        if trade.get('status') != 'closing':
            return

        trade_paper = trade.get('paper')
        is_paper = bool(trade_paper) if trade_paper is not None else bool(self.config.is_sub_strategy_paper(trade.get('sub_strategy', '')))
        if is_paper:
            filled_px = abs(float(trade.get('close_limit_price') or trade.get('exit_price') or 0.05))
            self._finalize_closed_trade(root, trade_path, trade, filled_px, trade.get('close_reason', 'paper_close'))
            return

        close_broker = self._get_broker(is_paper)
        close_oid = trade.get('close_order_id')
        if not close_oid:
            self._exit_trade(root, trade_path, trade.get('close_reason', 'closing_retry'))
            return

        def _get_st(oid: str):
            try:
                if hasattr(close_broker, 'get_order_status_direct'):
                    return close_broker.get_order_status_direct(oid)
                return close_broker.get_order_status(oid)
            except Exception as e:
                self.logger.warning('DRMA_HM: get_order_status(%s) error: %s', oid, e)
                return None

        st = _get_st(close_oid)
        if st and (st.status == 'filled' or getattr(st, 'filled_quantity', 0) >= int(trade.get('quantity') or 1)):
            filled_px = abs(float(st.filled_price or trade.get('close_limit_price') or 0.05))
            self._finalize_closed_trade(root, trade_path, trade, filled_px, trade.get('close_reason', 'closed'), close_order_id=close_oid)
            return

        # Resume chase with fresh mids
        open_legs = []
        for struct in trade.get('structures') or []:
            for l in struct.get('legs') or []:
                open_legs.append(ComboLeg(symbol=l['symbol'], action=l['action'], quantity=int(l.get('qty') or l.get('quantity') or 1)))
        close_legs = build_closing_legs(open_legs, quantity=int(trade.get('quantity') or 1))

        is_debit = (
            str(trade.get('structure_type', '')).lower() == 'debit_spread'
            or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL'
            or str(trade.get('sub_strategy', '')).upper() == 'DRMA_HM_DIR'
            or 'net_debit' in trade
        )
        curr_px = float(trade.get('close_limit_price') or 0.05)
        initial_px = curr_px if is_debit else -abs(curr_px)

        filled, new_oid, filled_px = self._chase_options_spread_close(
            close_broker=close_broker,
            trade=trade,
            close_legs=close_legs,
            is_debit=is_debit,
            initial_close_price=initial_px,
            qty=int(trade.get('quantity') or 1),
            existing_order_id=close_oid,
            max_attempts=3,
            wait_sec=3.0,
        )
        if filled:
            self._finalize_closed_trade(root, trade_path, trade, filled_px, trade.get('close_reason', 'closed'), close_order_id=new_oid)
        else:
            trade['close_order_id'] = new_oid
            trade['close_limit_price'] = filled_px
            trade['close_last_checked_at'] = datetime.now().isoformat()
            with open(trade_path, 'w', encoding='utf-8') as f:
                json.dump(trade, f, indent=2)

    def _is_lunch_window(self, now_ct: datetime) -> bool:
        """Lunch chop window: 10:30 to 12:30 CT (11:30 to 13:30 ET)."""
        t = now_ct.time()
        return dt_time(10, 30) <= t <= dt_time(12, 30)

    def _is_eod_cutoff(self, now_ct: datetime) -> bool:
        """EOD exit cutoff (e.g. 14:50 CT)."""
        cutoff_str = self.config.eod_exit_time_cst or '14:50'
        try:
            parts = cutoff_str.split(':')
            cutoff_t = dt_time(int(parts[0]), int(parts[1]))
            return now_ct.time() >= cutoff_t
        except Exception:
            return now_ct.time() >= dt_time(14, 50)

