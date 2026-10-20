"""Dale Perryman's NFH SPX 0DTE Iron Fly Strategy.

Full multi-leg iron fly ladder on SPX 0DTE with beyond break-even cuts and defined-risk wings.
Matches Option Alpha Theta Fly Trap (NFH) v28/v29 specification.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from blocks.stop.profiles.iron_fly import IRON_FLY_PROFILE
from common.streamer_symbols import register_option_symbols
from common.symbols import build_tastytrade_symbol, to_schwab, to_tastytrade
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy, project_root
from strategies.base import BaseStrategy, StrategyConfig
from strategies.iron_fly.book import IronFly, IronFlyBook
from strategies.iron_fly.executor import BaseExecutor, BrokerExecutor, PaperExecutor
from strategies.iron_fly.orders import round_credit_down
from strategies.iron_fly.rules import (
    DEFAULT_RULES,
    NfhRules,
    atm_center,
    build_closing_legs,
    build_fly_legs,
    calculate_bbe_amount,
    check_rapid_move,
    evaluate_add_trigger,
    first_fly_due,
    intrinsic,
    is_tight_mode,
    next_center,
    pick_widths,
    price_fly,
    should_cut,
    skip_reason,
    straddle,
    tier_index,
    tier_params,
)

log = logging.getLogger(__name__)
_ET = ZoneInfo('America/New_York')


def _parse_time_str(t_str: str, default: dtime) -> dtime:
    try:
        parts = t_str.split(':')
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return dtime(h, m, s)
    except Exception:
        return default


@dataclass
class IronFlyConfig(StrategyConfig):
    name: str = 'IRON_FLY_SPX'
    mode: str = 'PAPER'  # OFF | PAPER | LIVE_APPROVE | LIVE_AUTO
    contracts: int = 1
    max_contracts: int = 4
    max_flies_per_day: int = 20
    max_open_risk: float = 15000.0
    daily_loss_stop: float = 2500.0
    daily_loss_flatten: bool = False
    restart_when_flat: bool = False
    atm_rounding: int = 5
    max_entry_slip: float = 0.50
    combo_tick: float = 0.05
    fee_per_leg: float = 1.30
    stale_spot_s: float = 10.0
    spot_poll_s: float = 2.0
    sweep_s: float = 15.0
    fomc_days: List[str] = field(default_factory=lambda: ['2026-10-28', '2026-12-09'])
    skip_half_days: bool = True
    skip_third_wednesday: bool = True
    skip_last_weekday: bool = True
    approve_timeout_s: int = 60
    entry_wait_s: int = 5
    entry_max_attempts: int = 6
    entry_max_s: int = 45
    stop_profile: str = IRON_FLY_PROFILE
    requires_confirmation: bool = True
    confirmed: bool = True
    scheduled: bool = False
    paper: bool = True
    pause_adds: bool = False
    root: str = ''
    tz_name: str = 'America/New_York'

    # Theta Fly Trap (NFH) inputs
    first_fly_time_et: str = '10:35'
    tight_threshold_time_et: str = '14:00'
    late_tight_threshold_time_et: str = '15:30'
    adds_cutoff_et: str = '15:50'
    initial_wing_width: int = 50
    min_mid_price_initial: float = 3.0
    max_mid_price_initial: float = 50.0
    wing_target_mid: float = 0.40
    min_wing_width: int = 20
    max_wing_width: int = 50
    tight_mid_price_threshold: float = 10.0
    high_trade_price_threshold: float = 20.0
    low_trade_price_threshold: float = 15.0
    itm_high_threshold: float = 15.0
    itm_mid_threshold: float = 10.0
    itm_low_threshold: float = 7.5
    itm_tight_threshold: float = 2.5
    offset_high: float = 20.0
    offset_mid: float = 15.0
    offset_low: float = 10.0
    offset_tight: float = 5.0
    beyond_breakeven_amount: float = 0.01
    pabbe_close: bool = False
    high_pabbe_trade_price: float = 20.0
    low_pabbe_trade_price: float = 15.0
    high_pabbe_close_amount: float = 2.0
    mid_pabbe_close_amount: float = 1.0
    low_pabbe_close_amount: float = 0.01
    late_bbe_adjustment: bool = False
    late_bbe_time_et: str = '15:30'
    late_bbe_amount: float = 1.50
    eod_bbe_adjustment: bool = False
    eod_bbe_time_et: str = '15:50'
    eod_bbe_amount: float = 2.50
    min_close_ror: float = -0.90
    rapid_move_close: bool = True
    rapid_move_1m_pts: float = 25.0
    rapid_move_2m_pts: float = 40.0
    max_vix_change: float = 15.0
    max_vix: float = 30.0
    bypass_max_vix: bool = False
    elevated_vol_entry: bool = True
    elevated_vol_time_et: str = '13:30'
    day_pt: float = 1000.0
    day_pt_close: bool = False
    day_max_loss: float = -2000.0
    day_max_loss_close: bool = False
    em_initial_entry: bool = False
    use_current_mid_price: bool = False


class IronFlyStrategy(BaseStrategy):
    """Dale Perryman's NFH Iron Fly Ladder Auto-Trader."""

    def __init__(self, config: Optional[IronFlyConfig] = None, broker=None):
        config = config or IronFlyConfig()
        super().__init__(config, broker)
        self.config: IronFlyConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.book: Optional[IronFlyBook] = None
        self.executor: Optional[BaseExecutor] = None
        self._last_spot: Optional[float] = None
        self._last_spot_time: float = 0.0
        self._cached_chain: Dict[Tuple[str, float], Any] = {}
        self._cached_chain_time: float = 0.0
        self._spot_thread: Optional[threading.Thread] = None
        self._recent_spot_trail: List[Tuple[float, float]] = []  # [(epoch_sec, spot)]
        self._vix_open: Optional[float] = None
        self.rules: NfhRules = self._build_rules()

    def _build_rules(self) -> NfhRules:
        return NfhRules(
            first_fly_time=_parse_time_str(self.config.first_fly_time_et, dtime(10, 35, 0)),
            tight_threshold_time=_parse_time_str(self.config.tight_threshold_time_et, dtime(14, 0, 0)),
            late_tight_threshold_time=_parse_time_str(self.config.late_tight_threshold_time_et, dtime(15, 30, 0)),
            adds_cutoff=_parse_time_str(self.config.adds_cutoff_et, dtime(15, 50, 0)),
            initial_wing_width=self.config.initial_wing_width,
            min_mid_price_initial=self.config.min_mid_price_initial,
            max_mid_price_initial=self.config.max_mid_price_initial,
            wing_target_mid=self.config.wing_target_mid,
            min_wing_width=self.config.min_wing_width,
            max_wing_width=self.config.max_wing_width,
            tight_mid_price_threshold=self.config.tight_mid_price_threshold,
            high_trade_price_threshold=self.config.high_trade_price_threshold,
            low_trade_price_threshold=self.config.low_trade_price_threshold,
            itm_high_threshold=self.config.itm_high_threshold,
            itm_mid_threshold=self.config.itm_mid_threshold,
            itm_low_threshold=self.config.itm_low_threshold,
            itm_tight_threshold=self.config.itm_tight_threshold,
            offset_high=self.config.offset_high,
            offset_mid=self.config.offset_mid,
            offset_low=self.config.offset_low,
            offset_tight=self.config.offset_tight,
            beyond_breakeven_amount=self.config.beyond_breakeven_amount,
            pabbe_close=self.config.pabbe_close,
            high_pabbe_trade_price=self.config.high_pabbe_trade_price,
            low_pabbe_trade_price=self.config.low_pabbe_trade_price,
            high_pabbe_close_amount=self.config.high_pabbe_close_amount,
            mid_pabbe_close_amount=self.config.mid_pabbe_close_amount,
            low_pabbe_close_amount=self.config.low_pabbe_close_amount,
            late_bbe_adjustment=self.config.late_bbe_adjustment,
            late_bbe_time=_parse_time_str(self.config.late_bbe_time_et, dtime(15, 30, 0)),
            late_bbe_amount=self.config.late_bbe_amount,
            eod_bbe_adjustment=self.config.eod_bbe_adjustment,
            eod_bbe_time=_parse_time_str(self.config.eod_bbe_time_et, dtime(15, 50, 0)),
            eod_bbe_amount=self.config.eod_bbe_amount,
            min_close_ror=self.config.min_close_ror,
            rapid_move_1m_pts=self.config.rapid_move_1m_pts,
            rapid_move_2m_pts=self.config.rapid_move_2m_pts,
            max_vix_change=self.config.max_vix_change,
            day_pt=self.config.day_pt,
            day_pt_close=self.config.day_pt_close,
            day_max_loss=self.config.day_max_loss,
            day_max_loss_close=self.config.day_max_loss_close,
            atm_rounding=self.config.atm_rounding,
            combo_tick=self.config.combo_tick,
            fee_per_leg=self.config.fee_per_leg,
        )

    def schedule(self) -> List[Any]:
        return []

    def _get_root(self) -> str:
        return self.config.root or project_root()

    def _book_path(self, today_str: str) -> str:
        active = active_dir_for_strategy(self.config.name, self._get_root())
        return os.path.join(active, f'iron_fly_{today_str}.json')

    def _history_path(self, today_str: str) -> str:
        hist = history_dir_for_strategy(self.config.name, self._get_root())
        return os.path.join(hist, f'iron_fly_{today_str}.json')

    def _init_book(self) -> IronFlyBook:
        today_str = date.today().isoformat()
        path = self._book_path(today_str)
        existing = IronFlyBook.load_from_json(path)
        if existing and existing.day == today_str:
            existing.mode = self.config.mode
            return existing
        book = IronFlyBook(day=today_str, mode=self.config.mode)
        book.save_to_json(path)
        return book

    def _init_executor(self) -> BaseExecutor:
        is_paper = self.config.paper or (self.config.mode == 'PAPER')
        if is_paper or self.broker is None or getattr(self.broker, '_is_journal', False):
            return PaperExecutor(fee_per_leg=self.config.fee_per_leg)
        return BrokerExecutor(
            self.broker,
            entry_wait_s=self.config.entry_wait_s,
            entry_max_attempts=self.config.entry_max_attempts,
            entry_max_s=self.config.entry_max_s,
            max_entry_slip=self.config.max_entry_slip,
            combo_tick=self.config.combo_tick,
        )

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.tz_name))

    def _fetch_spot(self) -> Optional[float]:
        """Fetch SPX spot price with fallback."""
        spot = None
        if self.broker:
            try:
                spot = self.broker.get_spx_price()
                if spot is None and hasattr(self.broker, 'fetch_spx_price_api'):
                    spot = self.broker.fetch_spx_price_api()
            except Exception as exc:
                self.logger.debug('Spot fetch exception: %s', exc)

        if spot is not None and isinstance(spot, (int, float)) and spot > 0:
            self._last_spot = spot
            self._last_spot_time = time.time()
            return spot

        if self._last_spot and (time.time() - self._last_spot_time) < self.config.stale_spot_s:
            return self._last_spot

        return None

    def _fetch_vix(self) -> Optional[float]:
        """Fetch VIX index value if available."""
        if self.broker:
            try:
                if hasattr(self.broker, 'get_vix_price'):
                    v = self.broker.get_vix_price()
                    if isinstance(v, (int, float)):
                        return float(v)
                if hasattr(self.broker, 'get_option_price'):
                    v = self.broker.get_option_price('VIX') or self.broker.get_option_price('.VIX')
                    if isinstance(v, (int, float)):
                        return float(v)
            except Exception:
                pass
        return None

    def _fetch_chain(self) -> Dict[Tuple[str, float], Any]:
        """Fetch 0DTE option chain around current spot with local cache."""
        now = time.time()
        if (now - self._cached_chain_time) < 10.0 and self._cached_chain:
            return self._cached_chain

        chain_dict: Dict[Tuple[str, float], Any] = {}
        if not self.broker:
            return chain_dict

        spot = self._last_spot or self._fetch_spot()
        if not spot:
            return self._cached_chain

        atm = atm_center(spot, self.config.atm_rounding)
        expiry_yymmdd = self.book.day.replace('-', '')[2:]
        step = self.config.atm_rounding or 5
        # Fetch wide enough for 50-point wings
        strikes = list(range(int(atm - 75), int(atm + 80), step))

        symbols_to_fetch = []
        sym_map = {}
        for k in strikes:
            c_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', k)
            p_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', k)
            symbols_to_fetch.extend([c_sym, p_sym])
            sym_map[c_sym] = ('C', float(k))
            sym_map[p_sym] = ('P', float(k))

        try:
            mids_map = {}
            if hasattr(self.broker, 'fetch_option_mids_api'):
                mids_map = self.broker.fetch_option_mids_api(symbols_to_fetch)
            elif self.broker:
                for s in symbols_to_fetch:
                    px = self.broker.get_option_price(s)
                    if px is not None:
                        mids_map[s] = px

            for sym, (right, strike) in sym_map.items():
                mid = mids_map.get(sym) or (self.broker.get_option_price(sym) if self.broker else None)
                if mid is not None and mid > 0:
                    chain_dict[(right, strike)] = {
                        'mid': round(float(mid), 2),
                        'bid': round(float(mid) - 0.10, 2),
                        'ask': round(float(mid) + 0.10, 2),
                        'symbol': sym,
                    }
        except Exception as exc:
            self.logger.warning('Chain fetch error: %s', exc)

        if chain_dict:
            self._cached_chain = chain_dict
            self._cached_chain_time = now

        return self._cached_chain

    def _recover_em_open_if_missing(self) -> None:
        """If engine starts after 09:45 ET, recover morning 0DTE ATM straddle from session or estimation."""
        if self.book.em_open is not None and self.book.em_open > 0:
            return
        root = project_root()
        mr_session_path = os.path.join(
            active_dir_for_strategy('MEAN_REVERSION_SPX', root),
            f'session_{self.book.day}.json',
        )
        if os.path.isfile(mr_session_path):
            try:
                import json
                with open(mr_session_path, encoding='utf-8') as f:
                    mr_session = json.load(f)
                strad = mr_session.get('straddle_price')
                if strad and float(strad) > 0:
                    self.book.em_open = round(float(strad), 2)
                    self.book.add_event('EM_OPEN_RECOVERED', f'Recovered EM open ${self.book.em_open:.2f} from Mean Reversion session')
                    self.logger.info('Recovered Iron Fly EM open %.2f from Mean Reversion session', self.book.em_open)
                    return
            except Exception as exc:
                self.logger.warning('Failed to load Mean Reversion session for EM open recovery: %s', exc)

        spot = self._fetch_spot()
        if spot and spot > 1000:
            estimated = round(spot * 0.008, 2)
            self.book.em_open = estimated
            self.book.add_event('EM_OPEN_ESTIMATED', f'Estimated EM open ${estimated:.2f} (0.8% of spot {spot:.2f})')
            self.logger.info('Estimated Iron Fly EM open %.2f from spot %.2f', estimated, spot)

    def run(self) -> None:
        """Main strategy runner thread."""
        self._running = True
        self._stop_event.clear()
        self.book = self._init_book()
        self.executor = self._init_executor()
        self.rules = self._build_rules()
        self._recover_em_open_if_missing()

        self.logger.info(
            'Starting %s (mode=%s, contracts=%d, paper=%s)',
            self.config.name, self.config.mode, self.config.contracts, self.config.paper
        )
        self.book.add_event('ENGINE_START', f'Iron Fly starting in mode {self.config.mode}')

        # Start 2-second spot cut monitoring thread
        self._spot_thread = threading.Thread(
            target=self._spot_cut_loop,
            name=f'{self.config.name}-spot-cut',
            daemon=True,
        )
        self._spot_thread.start()

        today_str = self.book.day
        b_path = self._book_path(today_str)
        h_path = self._history_path(today_str)

        try:
            while self._running and not self._stop_event.is_set():
                now_dt = self._now()
                now_t = now_dt.time()
                today_d = now_dt.date()

                # 1. Skip day check
                reason = skip_reason(
                    today_d,
                    fomc_days=self.config.fomc_days,
                    is_half_day=False,
                    skip_third_wednesday=self.config.skip_third_wednesday,
                    skip_last_weekday=self.config.skip_last_weekday,
                )
                if reason:
                    self.book.add_event('SKIP_DAY', f'Trading skipped today: {reason}')
                    self._save_state(b_path, h_path)
                    time.sleep(60.0)
                    continue

                # 2. Market hours check (09:30 - 16:00 ET)
                if now_t < dtime(9, 30, 0):
                    time.sleep(10.0)
                    continue

                # 3. Market close / expiry settlement
                if now_t >= dtime(16, 0, 0):
                    self._settle_expired_flies()
                    self._save_state(b_path, h_path)
                    self.logger.info('Market closed. Settled expired flies. Stopping engine for today.')
                    break

                # 4. Sweep execution
                self._run_sweep_cycle(now_t)
                self._save_state(b_path, h_path)

                # Wait for next sweep (15 s)
                self._stop_event.wait(self.config.sweep_s)

        except Exception as exc:
            self.logger.exception('Iron Fly engine loop crashed: %s', exc)
            if self.book:
                self.book.add_event('CRASH', str(exc))
                self._save_state(b_path, h_path)
        finally:
            self._running = False
            self.logger.info('Iron Fly engine stopped.')

    def _run_sweep_cycle(self, now_t: dtime) -> None:
        """Perform one 15-second sweep cycle."""
        spot = self._fetch_spot()
        if spot is None:
            self.logger.warning('No spot price available for sweep')
            return

        now_sec = time.time()
        self._recent_spot_trail.append((now_sec, spot))
        # Keep last 3 minutes of spot prints
        cutoff_sec = now_sec - 180.0
        self._recent_spot_trail = [(ts, p) for ts, p in self._recent_spot_trail if ts >= cutoff_sec]

        with self._lock:
            # Track first-hour high/low (until 10:30) and spot trail
            if now_t <= dtime(10, 30, 0):
                if self.book.first_hour_high is None or spot > self.book.first_hour_high:
                    self.book.first_hour_high = spot
                if self.book.first_hour_low is None or spot < self.book.first_hour_low:
                    self.book.first_hour_low = spot

            now_str = self._now().strftime('%H:%M:%S')
            self.book.spot_trail.append((now_str, spot))

            chain = self._fetch_chain()
            atm = atm_center(spot, self.config.atm_rounding)

            # Record Opening straddle ("EM open") between 09:30:40 and 09:45
            if dtime(9, 30, 40) <= now_t <= dtime(9, 45, 0) and self.book.em_open is None:
                tot, _, _ = straddle(chain, atm)
                if tot is not None and tot > 0:
                    self.book.em_open = tot
                    self.book.add_event('EM_OPEN', f'Recorded EM open: {tot:.2f} at strike {atm}')

            # Update ATM straddle now & ratio
            straddle_now, _, _ = straddle(chain, atm)
            if straddle_now is not None:
                self.book.em_now = straddle_now
                if self.book.em_open:
                    self.book.ratio_vs_83 = round(straddle_now / (0.83 * self.book.em_open), 4)

            # Record VIX open if available
            vix_now = self._fetch_vix()
            if vix_now:
                if self._vix_open is None and now_t <= dtime(10, 0, 0):
                    self._vix_open = vix_now
                if self._vix_open and (vix_now - self._vix_open) >= self.config.max_vix_change:
                    self.book.add_event('VIX_SPIKE', f'VIX increased by {vix_now - self._vix_open:.1f} pts >= {self.config.max_vix_change}. Halting adds.')
                    self.close_all_open_flies(reason='max_vix_change')
                    self.config.pause_adds = True
                    return

            # Check Rapid Price Move Circuit Breaker
            if self.config.rapid_move_close:
                is_rapid, r_reason = check_rapid_move(self._recent_spot_trail, now_sec, self.rules)
                if is_rapid:
                    self.book.add_event('RAPID_MOVE', f'Circuit breaker: {r_reason}. Halting adds & closing open flies.')
                    self.close_all_open_flies(reason=r_reason)
                    self.config.pause_adds = True
                    return

            # Check Day Profit Target Close
            if self.config.day_pt_close:
                net_p = self.book.net_pnl()
                if net_p >= self.config.day_pt:
                    self.book.add_event('DAY_PT', f'Day PT reached (${net_p:.2f} >= ${self.config.day_pt:.2f}). Halting adds.')
                    self.close_all_open_flies(reason='day_pt_close')
                    self.config.pause_adds = True
                    return

            # Check Day Max Loss Close
            if self.config.day_max_loss_close:
                net_p = self.book.net_pnl()
                if net_p <= self.config.day_max_loss:
                    self.book.add_event('DAY_MAX_LOSS', f'Day Max Loss hit (${net_p:.2f} <= ${self.config.day_max_loss:.2f}). Halting adds.')
                    self.close_all_open_flies(reason='day_max_loss_close')
                    self.config.pause_adds = True
                    return

            # Update marks and check cuts on all open flies
            self._evaluate_open_flies(spot, chain, now_t)

            # Check if mode is OFF or paused
            if self.book.mode == 'OFF' or self.config.pause_adds:
                return

            # Check safety caps
            if not self._safety_caps_allow(spot):
                return

            # Check entry time window
            if now_t >= self.rules.adds_cutoff:
                return

            has_flies = bool(self.book.flies)
            open_flies = self.book.get_open_flies()

            # 1. Initial Position Entry
            if not has_flies:
                due, reason = first_fly_due(
                    now_t,
                    has_prior_flies=False,
                    initial_mid_credit=None,
                    rules=self.rules,
                    em_open=self.book.em_open,
                    straddle_now=self.book.em_now,
                    em_initial_entry=self.config.em_initial_entry,
                )
                if due:
                    self._plan_and_open_initial_fly(atm, chain)
                return

            # 2. Subsequent Adds
            if not open_flies:
                if self.config.restart_when_flat and now_t >= self.rules.first_fly_time:
                    self._plan_and_open_initial_fly(atm, chain)
                return

            # Max open flies cap
            cap = 12 if (self.book.first_fly_credit or 0.0) >= 15.0 else 8
            if len(open_flies) >= cap:
                return

            # Evaluate the open IB closest to current SPX spot
            closest_fly = min(open_flies, key=lambda f: abs(spot - f.center))

            # Determine trade price / premium collected
            credit_to_evaluate = closest_fly.fill_credit or closest_fly.planned_credit
            if self.config.use_current_mid_price:
                atm_pricing = price_fly(chain, atm, self.config.max_wing_width, self.config.max_wing_width)
                if atm_pricing:
                    credit_to_evaluate = atm_pricing[0]

            # Price of a 50-wide IB for tight threshold condition check
            mid_50w = None
            pricing_50w = price_fly(chain, atm, 50, 50)
            if pricing_50w:
                mid_50w = pricing_50w[0]

            triggered, scenario, threshold, offset, itm_dist = evaluate_add_trigger(
                spot=spot,
                closest_fly_center=closest_fly.center,
                closest_fly_credit=credit_to_evaluate,
                now_time=now_t,
                mid_50w=mid_50w,
                rules=self.rules,
            )

            # Record active tier tag in diary
            self.book.current_tier = 4 if scenario == 'tight' else 3 if scenario == 'high_credit' else 2 if scenario == 'mid_credit' else 1

            if triggered:
                open_centers = self.book.get_open_centers()
                cand_center = next_center(
                    spot=spot,
                    closest_center=closest_fly.center,
                    offset=offset,
                    open_centers=open_centers,
                    long_legs_map=self.book.get_long_legs_map(),
                    rounding=self.config.atm_rounding,
                )
                if cand_center is not None:
                    self._plan_and_open_add_fly(cand_center, chain, closest_fly=closest_fly, scenario=scenario)

    def _safety_caps_allow(self, spot: float) -> bool:
        """Check safety caps before planning any new fly."""
        # Daily loss stop
        net_loss = -(self.book.realized_pnl() + self.book.unrealized_pnl())
        if net_loss >= self.config.daily_loss_stop:
            self.logger.warning('Daily loss cap breached (loss=%.2f >= cap=%.2f)', net_loss, self.config.daily_loss_stop)
            if self.config.daily_loss_flatten:
                self.close_all_open_flies(reason='daily_loss_flatten')
            return False

        # Max flies per day
        if len(self.book.flies) >= self.config.max_flies_per_day:
            return False

        # Max open risk
        if self.book.open_risk() >= self.config.max_open_risk:
            return False

        # Stale spot check
        if (time.time() - self._last_spot_time) > self.config.stale_spot_s:
            self.logger.warning('Stale spot price (>%ss); blocking new adds', self.config.stale_spot_s)
            return False

        return True

    def _plan_and_open_initial_fly(self, center: float, chain: Dict[Tuple[str, float], Any]) -> None:
        """Plan and open the day's initial ATM, $50 wide IB."""
        cw = self.config.initial_wing_width
        pw = self.config.initial_wing_width

        pricing = price_fly(chain, center, cw, pw)
        if pricing is None:
            self.book.add_event('INITIAL_FLY_WAIT', f'Waiting for option quotes around center {center}')
            return

        mid_credit, nat_credit, _ = pricing
        if mid_credit < self.rules.min_mid_price_initial or mid_credit > self.rules.max_mid_price_initial:
            self.book.add_event('INITIAL_FLY_SKIP', f'Mid credit {mid_credit:.2f} outside valid range [{self.rules.min_mid_price_initial}, {self.rules.max_mid_price_initial}]')
            return

        self._submit_fly(center, cw, pw, mid_credit, nat_credit, is_first=True, scenario='initial_atm_50w')

    def _plan_and_open_add_fly(
        self,
        center: float,
        chain: Dict[Tuple[str, float], Any],
        closest_fly: IronFly,
        scenario: str,
    ) -> None:
        """Plan and open an additional fly with target $0.40 long put wing (20-50 wide)."""
        short_map = self.book.get_short_legs_map()
        cw, pw = pick_widths(
            chain=chain,
            center=center,
            short_legs_map=short_map,
            target_price=self.config.wing_target_mid,
            min_width=self.config.min_wing_width,
            max_width=self.config.max_wing_width,
            candidate_widths=self.rules.candidate_widths,
            is_initial=False,
        )
        if cw is None or pw is None:
            self.book.add_event('WIDTH_SKIP', f'No valid wings for center {center} targeting ${self.config.wing_target_mid}')
            return

        pricing = price_fly(chain, center, cw, pw)
        if pricing is None:
            return

        mid_credit, nat_credit, _ = pricing
        max_wing = max(cw, pw)
        if mid_credit <= 0 or (max_wing - mid_credit) <= 0:
            return

        self._submit_fly(center, cw, pw, mid_credit, nat_credit, is_first=False, scenario=scenario)

    def _submit_fly(
        self,
        center: float,
        cw: int,
        pw: int,
        mid_credit: float,
        nat_credit: float,
        is_first: bool,
        scenario: str,
    ) -> None:
        max_wing = max(cw, pw)
        seq = len(self.book.flies) + 1
        fly_id = f"IF_{self.book.day.replace('-', '')}_{seq}"
        contracts = min(self.config.contracts, self.config.max_contracts)
        max_loss = round((max_wing - mid_credit) * 100 * contracts, 2)

        # Check if adding this fly exceeds max_open_risk
        if (self.book.open_risk() + max_loss) > self.config.max_open_risk:
            self.book.add_event('CAP_RISK_SKIP', f'Adding fly would exceed max_open_risk ({max_loss} + {self.book.open_risk()})')
            return

        expiry_yymmdd = self.book.day.replace('-', '')[2:]
        legs = [
            {'symbol': build_tastytrade_symbol(expiry_yymmdd, 'C', int(center)), 'occ_symbol': to_schwab(build_tastytrade_symbol(expiry_yymmdd, 'C', int(center))), 'action': 'SELL_TO_OPEN', 'strike': center, 'right': 'C', 'qty': contracts},
            {'symbol': build_tastytrade_symbol(expiry_yymmdd, 'P', int(center)), 'occ_symbol': to_schwab(build_tastytrade_symbol(expiry_yymmdd, 'P', int(center))), 'action': 'SELL_TO_OPEN', 'strike': center, 'right': 'P', 'qty': contracts},
            {'symbol': build_tastytrade_symbol(expiry_yymmdd, 'C', int(center + cw)), 'occ_symbol': to_schwab(build_tastytrade_symbol(expiry_yymmdd, 'C', int(center + cw))), 'action': 'BUY_TO_OPEN', 'strike': center + cw, 'right': 'C', 'qty': contracts},
            {'symbol': build_tastytrade_symbol(expiry_yymmdd, 'P', int(center - pw)), 'occ_symbol': to_schwab(build_tastytrade_symbol(expiry_yymmdd, 'P', int(center - pw))), 'action': 'BUY_TO_OPEN', 'strike': center - pw, 'right': 'P', 'qty': contracts},
        ]

        fly = IronFly(
            id=fly_id,
            seq=seq,
            day=self.book.day,
            center=center,
            call_width=cw,
            put_width=pw,
            qty=contracts,
            status='OPENING' if self.book.mode != 'LIVE_APPROVE' else 'PENDING_APPROVAL',
            planned_credit=mid_credit,
            max_loss=max_loss,
            legs=legs,
            diary={
                'scenario': scenario,
                'em_open': self.book.em_open,
                'em_now': self.book.em_now,
                'tier': self.book.current_tier,
                'ratio': self.book.ratio_vs_83,
            }
        )
        self.book.flies[fly_id] = fly

        if self.book.mode == 'LIVE_APPROVE':
            self.book.add_event('PENDING_APPROVAL', f'Fly {fly_id} at center {center} ({cw}/{pw}) waiting approval [{scenario}]')
            return

        self._execute_open(fly, mid_credit, nat_credit, is_first)

    def _execute_open(self, fly: IronFly, mid_credit: float, nat_credit: float, is_first: bool) -> None:
        expiry = self.book.day.replace('-', '')[2:]
        plan = {
            'expiry': expiry,
            'center': fly.center,
            'cw': fly.call_width,
            'pw': fly.put_width,
            'qty': fly.qty,
            'mid_credit': mid_credit,
            'natural_credit': nat_credit,
        }

        res = self.executor.open_fly(plan)
        if res.success and res.status == 'filled':
            fill_credit = res.filled_price or mid_credit
            fly.status = 'OPEN'
            fly.fill_credit = fill_credit
            fly.be_low = round(fly.center - fill_credit, 2)
            fly.be_high = round(fly.center + fill_credit, 2)
            fly.max_loss = round((max(fly.call_width, fly.put_width) - fill_credit) * 100 * fly.qty, 2)
            fly.entry_order_id = res.order_id
            fly.opened_at = self._now().isoformat()
            fly.entry_fill_time = fly.opened_at
            fly.fees = round(4 * fly.qty * self.config.fee_per_leg, 2)

            if is_first:
                self.book.first_fly_credit = fill_credit

            self.book.add_event('OPEN', f'Opened fly {fly.id} at center {fly.center} credit={fill_credit:.2f} BE=[{fly.be_low}, {fly.be_high}]')

            # Streamer symbol registration
            self._register_streamer_symbols([leg['symbol'] for leg in fly.legs])
        else:
            fly.status = 'FAILED'
            fly.close_reason = res.message or 'open_failed'
            self.book.add_event('OPEN_FAILED', f'Failed to open fly {fly.id}: {fly.close_reason}')

    def _spot_cut_loop(self) -> None:
        """2-second spot poll loop specifically for immediate beyond break-even cuts."""
        while self._running and not self._stop_event.is_set():
            try:
                open_flies = self.book.get_open_flies() if self.book else []
                if open_flies:
                    spot = self._fetch_spot()
                    now_t = self._now().time()
                    if spot is not None:
                        for fly in open_flies:
                            if fly.status == 'OPEN' and fly.be_low and fly.be_high:
                                bbe = calculate_bbe_amount(fly.fill_credit or fly.planned_credit, now_t, self.rules)
                                ror = (fly.unrealized_pnl / fly.max_loss) if (fly.max_loss and fly.max_loss > 0) else None
                                cut, reason = should_cut(
                                    spot=spot,
                                    be_low=fly.be_low,
                                    be_high=fly.be_high,
                                    beyond_be_amount=bbe,
                                    current_ror=ror,
                                    min_close_ror=self.config.min_close_ror,
                                )
                                if cut:
                                    self.logger.warning('BEYOND BREAKEVEN CUT TRIGGERED for %s: spot=%.2f BE=[%.2f, %.2f] bbe=%.2f (%s)', fly.id, spot, fly.be_low, fly.be_high, bbe, reason)
                                    self._cut_fly(fly, reason)
            except Exception as exc:
                self.logger.debug('Spot cut loop error: %s', exc)

            self._stop_event.wait(self.config.spot_poll_s)

    def _evaluate_open_flies(self, spot: float, chain: Dict[Tuple[str, float], Any], now_t: dtime) -> None:
        """Update unrealized PnL and check beyond breakeven cuts for all open flies."""
        for fly in self.book.get_open_flies():
            if fly.status != 'OPEN':
                continue

            pricing = price_fly(chain, fly.center, fly.call_width, fly.put_width)
            if pricing:
                mid_credit, _, nat_debit = pricing
                fly.mark_debit = mid_credit
                if fly.fill_credit is not None:
                    fly.unrealized_pnl = round((fly.fill_credit - mid_credit) * 100 * fly.qty, 2)

            # Beyond break-even cut check
            if fly.be_low is not None and fly.be_high is not None:
                bbe = calculate_bbe_amount(fly.fill_credit or fly.planned_credit, now_t, self.rules)
                ror = (fly.unrealized_pnl / fly.max_loss) if (fly.max_loss and fly.max_loss > 0) else None
                cut, reason = should_cut(
                    spot=spot,
                    be_low=fly.be_low,
                    be_high=fly.be_high,
                    beyond_be_amount=bbe,
                    current_ror=ror,
                    min_close_ror=self.config.min_close_ror,
                )
                if cut:
                    self._cut_fly(fly, reason)

    def _cut_fly(self, fly: IronFly, reason: str = 'cut') -> None:
        """Execute cut for a single fly."""
        with self._lock:
            if fly.status != 'OPEN':
                return
            fly.status = 'CLOSING'
            self.book.add_event('CUT_START', f'Cutting fly {fly.id} at center {fly.center} ({reason})')

        res = self.executor.close_fly(fly, reason=reason, market_chain_fn=self._fetch_chain)
        with self._lock:
            if res.success and res.status == 'filled':
                exit_debit = res.filled_price or (fly.mark_debit or 0.0)
                fly.status = 'CLOSED'
                fly.exit_debit = exit_debit
                fly.exit_order_id = res.order_id
                fly.closed_at = self._now().isoformat()
                fly.exit_fill_time = fly.closed_at
                fly.close_reason = reason
                fly.fees += round(4 * fly.qty * self.config.fee_per_leg, 2)
                if fly.fill_credit is not None:
                    fly.realized_pnl = round((fly.fill_credit - exit_debit) * 100 * fly.qty, 2)
                fly.unrealized_pnl = 0.0
                self.book.add_event('CUT_DONE', f'Cut fly {fly.id} closed at debit {exit_debit:.2f} PnL={fly.realized_pnl}')
            else:
                fly.status = 'OPEN'  # revert to open if failed so we keep retrying
                self.book.add_event('CUT_FAILED', f'Cut failed for fly {fly.id}: {res.message}')

    def _settle_expired_flies(self) -> None:
        """Cash-settle held flies at intrinsic after 16:00 ET."""
        spot = self._fetch_spot()
        if spot is None:
            return

        with self._lock:
            for fly in list(self.book.get_open_flies()):
                if fly.status in ('OPEN', 'CLOSING'):
                    fly_intrinsic = intrinsic(spot, fly.center, fly.call_width, fly.put_width)
                    fly.status = 'EXPIRED'
                    fly.exit_debit = fly_intrinsic
                    fly.closed_at = self._now().isoformat()
                    fly.exit_fill_time = fly.closed_at
                    fly.close_reason = 'expiry'
                    if fly.fill_credit is not None:
                        fly.realized_pnl = round((fly.fill_credit - fly_intrinsic) * 100 * fly.qty, 2)
                    fly.unrealized_pnl = 0.0
                    self.book.add_event('EXPIRED', f'Fly {fly.id} expired at intrinsic {fly_intrinsic:.2f} (SPX={spot:.2f}) PnL={fly.realized_pnl}')

    def _save_state(self, b_path: str, h_path: str) -> None:
        if self.book:
            try:
                self.book.save_to_json(b_path)
            except Exception as exc:
                self.logger.warning('Failed to save active book to %s: %s', b_path, exc)
            try:
                self.book.save_to_json(h_path)
            except Exception as exc:
                self.logger.warning('Failed to save history book to %s: %s', h_path, exc)

    def _register_streamer_symbols(self, symbols: List[str]) -> None:
        try:
            register_option_symbols(symbols)
        except Exception as exc:
            self.logger.debug('Streamer symbol registration error: %s', exc)

    def approve_fly(self, fly_id: str) -> bool:
        """Approve a pending fly in LIVE_APPROVE mode."""
        with self._lock:
            if not self.book or fly_id not in self.book.flies:
                return False
            fly = self.book.flies[fly_id]
            if fly.status != 'PENDING_APPROVAL':
                return False
            fly.status = 'OPENING'

        chain = self._fetch_chain()
        pricing = price_fly(chain, fly.center, fly.call_width, fly.put_width)
        mid_credit = pricing[0] if pricing else fly.planned_credit
        nat_credit = pricing[1] if pricing else mid_credit - 0.50
        self._execute_open(fly, mid_credit, nat_credit, is_first=(fly.seq == 1))
        return True

    def skip_fly(self, fly_id: str) -> bool:
        """Skip a pending fly in LIVE_APPROVE mode."""
        with self._lock:
            if not self.book or fly_id not in self.book.flies:
                return False
            fly = self.book.flies[fly_id]
            if fly.status != 'PENDING_APPROVAL':
                return False
            fly.status = 'FAILED'
            fly.close_reason = 'skipped_by_operator'
            self.book.add_event('SKIPPED', f'Fly {fly_id} skipped by operator')
            return True

    def close_all_open_flies(self, reason: str = 'manual_close_all') -> None:
        """Emergency cut of all open flies."""
        if not self.book:
            return
        for fly in self.book.get_open_flies():
            self._cut_fly(fly, reason)

    def stop(self) -> None:
        """Request strategy shutdown."""
        self._running = False
        self._stop_event.set()
        if self._spot_thread and self._spot_thread.is_alive():
            self._spot_thread.join(timeout=5.0)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            if not self.book:
                return {'running': self._running, 'name': self.config.name}
            return {
                'name': self.config.name,
                'running': self._running,
                'mode': self.book.mode,
                'paper': self.config.paper,
                'open_flies': len(self.book.get_open_flies()),
                'total_flies': len(self.book.flies),
                'open_risk': self.book.open_risk(),
                'realized_pnl': self.book.realized_pnl(),
                'unrealized_pnl': self.book.unrealized_pnl(),
                'total_fees': self.book.total_fees(),
                'net_pnl': self.book.net_pnl(),
                'em_open': self.book.em_open,
                'em_now': self.book.em_now,
                'current_tier': self.book.current_tier,
                'last_spot': self._last_spot,
            }

    @staticmethod
    def register_open_trade_streamer_legs(book_data: Dict[str, Any], logger=None) -> None:
        symbols = []
        for f in (book_data.get('flies') or {}).values():
            if f.get('status') in ('OPEN', 'CLOSING'):
                for leg in f.get('legs') or []:
                    sym = leg.get('symbol')
                    if sym:
                        symbols.append(sym)
        if symbols:
            register_option_symbols(symbols)
            if logger:
                logger.info('Registered %d open Iron Fly streamer symbols', len(symbols))
