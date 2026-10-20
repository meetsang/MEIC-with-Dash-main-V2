"""Orchestrator strategy runner for Dale Perryman NFH SPX 0DTE Iron Fly scaled to XSP."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional, Tuple

from common import trades_layout
from common.session_cleanup import central_today
from common.streamer_symbols import register_option_symbols
from common.symbols import build_tastytrade_symbol
from strategies.base import BaseStrategy, StrategyConfig
from strategies.iron_fly.book import FlyStatus, IronFly
from strategies.iron_fly_xsp.book import IronFlyXspBook, IronFlyXspPair
from strategies.iron_fly_xsp.executor import BrokerExecutorXsp, PaperExecutorXsp
from strategies.iron_fly_xsp.orders import build_closing_legs_xsp, build_fly_legs_xsp, build_xsp_symbol
from strategies.iron_fly_xsp.rules import (
    DEFAULT_RULES_XSP,
    NfhRulesXsp,
    atm_center_spx,
    calculate_bbe_amount_xsp,
    evaluate_add_trigger_xsp,
    get_xsp_centers,
    intrinsic_xsp,
    is_tight_mode_xsp,
    next_spx_center_xsp,
    pick_widths_xsp,
    price_fly_xsp,
    should_cut_xsp,
    skip_reason,
    tier_index_xsp,
    tier_params_xsp,
)

log = logging.getLogger(__name__)


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
class IronFlyXspConfig(StrategyConfig):
    name: str = trades_layout.STRATEGY_IRON_FLY_XSP
    underlying: str = 'XSP'
    spx_underlying: str = 'SPX'
    max_open_risk: float = 3000.0       # Scaled down from $15,000 SPX
    daily_loss_stop: float = 500.0      # Scaled down from $2,500 SPX
    contracts: int = 2                  # 2 contracts per trigger pair (1 per fly)
    restart_when_flat: bool = False
    max_flies_per_day: int = 20         # 20 pairs = 40 contracts
    max_open_flies: int = 8             # 8 open pairs = 16 contracts
    stale_spot_s: float = 10.0
    tick_ms: int = 15000                # 15s main sweep cadence
    cut_check_ms: int = 2000            # 2s spot-cut monitoring cadence
    be_buffer_pts: float = 0.01         # 0.01 pt beyond breakeven (scaled from SPX)
    spx_cw_default: int = 50            # Default SPX call width
    spx_pw_default: int = 50            # Default SPX put width
    mode: str = 'PAPER'                 # 'PAPER', 'LIVE_AUTO', 'LIVE_APPROVE'
    confirmed: bool = True

    # Theta Fly Trap (NFH) scaled inputs
    first_fly_time_et: str = '10:35'
    tight_threshold_time_et: str = '14:00'
    late_tight_threshold_time_et: str = '15:30'
    adds_cutoff_et: str = '15:50'
    initial_wing_width: int = 5
    min_mid_price_initial: float = 0.30
    max_mid_price_initial: float = 5.00
    wing_target_mid: float = 0.04
    min_wing_width: int = 2
    max_wing_width: int = 5
    tight_mid_price_threshold: float = 1.00
    high_trade_price_threshold: float = 2.00
    low_trade_price_threshold: float = 1.50
    itm_high_threshold: float = 1.50
    itm_mid_threshold: float = 1.00
    itm_low_threshold: float = 0.75
    itm_tight_threshold: float = 0.25
    offset_high: float = 2.00
    offset_mid: float = 1.50
    offset_low: float = 1.00
    offset_tight: float = 0.50
    beyond_breakeven_amount: float = 0.01
    pabbe_close: bool = False
    high_pabbe_trade_price: float = 2.00
    low_pabbe_trade_price: float = 1.50
    high_pabbe_close_amount: float = 0.20
    mid_pabbe_close_amount: float = 0.10
    low_pabbe_close_amount: float = 0.01
    late_bbe_adjustment: bool = False
    late_bbe_time_et: str = '15:30'
    late_bbe_amount: float = 0.15
    eod_bbe_adjustment: bool = False
    eod_bbe_time_et: str = '15:50'
    eod_bbe_amount: float = 0.25
    min_close_ror: float = -0.90
    rapid_move_close: bool = True
    rapid_move_1m_pts: float = 2.50
    rapid_move_2m_pts: float = 4.00
    day_pt: float = 100.0
    day_pt_close: bool = False
    day_max_loss: float = -200.0
    day_max_loss_close: bool = False
    em_initial_entry: bool = False


class IronFlyXspStrategy(BaseStrategy):
    """
    Executes the Dale Perryman NFH 0DTE ladder on Mini-SPX (XSP):
    - Scaled 1:10 from the SPX NFH strategy.
    - Dual cadence: 15s main sweep loop + 2s beyond break-even cut monitoring loop.
    - Each SPX trigger translates into exactly 2 XSP flies.
    - Fractional .5 centers split into (floor, ceil) integer strikes (e.g. 5835 -> 583 & 584).
    - Integer .0 centers do 2 flies at the center (e.g. 5830 -> 583 & 583).
    """

    def __init__(self, config: Optional[IronFlyXspConfig] = None, broker=None):
        config = config or IronFlyXspConfig()
        super().__init__(config, broker)
        self.config: IronFlyXspConfig = config
        self.book: Optional[IronFlyXspBook] = None
        self.paper_executor: Optional[PaperExecutorXsp] = None
        self.broker_executor: Optional[BrokerExecutorXsp] = None
        self._cut_thread: Optional[threading.Thread] = None
        self._cut_stop = threading.Event()
        self._lock = threading.Lock()
        self.last_spot_update_ts: float = 0.0
        self.last_spot: Optional[float] = None
        self.last_spx_spot: Optional[float] = None
        self.last_center: Optional[int] = None
        self._today_str: str = ''
        self.trades_dir: Optional[str] = getattr(config, 'root', None)
        self._running: bool = False
        self.prices: Any = None
        self.rules: NfhRulesXsp = self._build_rules()

    def _build_rules(self) -> NfhRulesXsp:
        return NfhRulesXsp(
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
            day_pt=self.config.day_pt,
            day_pt_close=self.config.day_pt_close,
            day_max_loss=self.config.day_max_loss,
            day_max_loss_close=self.config.day_max_loss_close,
        )

    def schedule(self) -> List[Any]:
        return []

    def _recover_em_open_if_missing(self) -> None:
        """If XSP starts after 09:45 ET, recover morning 0DTE straddle from SPX session scaled by 10."""
        if self.book.em_open is not None and self.book.em_open > 0:
            return
        root = self.trades_dir or trades_layout.project_root()
        mr_session_path = os.path.join(
            trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_MEAN_REVERSION, root),
            f'session_{self._today_str}.json',
        )
        if os.path.isfile(mr_session_path):
            try:
                with open(mr_session_path, encoding='utf-8') as f:
                    mr_session = json.load(f)
                strad = mr_session.get('straddle_price')
                if strad and float(strad) > 0:
                    self.book.em_open = round(float(strad) / 10.0, 2)
                    self.book.log_event('EM_OPEN_RECOVERED', f'Recovered XSP EM open ${self.book.em_open:.2f} (from SPX ${float(strad):.2f})')
                    log.info('Recovered XSP EM open %.2f from SPX straddle %.2f', self.book.em_open, float(strad))
                    return
            except Exception as exc:
                log.warning('Failed to load Mean Reversion session for XSP EM open recovery: %s', exc)

        spot = self._get_live_spot()
        if spot and spot > 100:
            estimated = round(spot * 0.008, 2)
            self.book.em_open = estimated
            self.book.log_event('EM_OPEN_ESTIMATED', f'Estimated XSP EM open ${estimated:.2f} (0.8% of spot {spot:.2f})')
            log.info('Estimated XSP EM open %.2f from spot %.2f', estimated, spot)

    def start(self) -> None:
        self._today_str = central_today().isoformat()
        root = self.trades_dir or trades_layout.project_root()
        self.book = IronFlyXspBook.load_or_create(self._today_str, root)
        self.rules = self._build_rules()
        self._recover_em_open_if_missing()
        self.book.save()

        if self.config.paper:
            self.paper_executor = PaperExecutorXsp(self.broker)
        else:
            self.broker_executor = BrokerExecutorXsp(self.broker)

        # Start 2s background beyond break-even spot cut thread
        self._cut_stop.clear()
        self._cut_thread = threading.Thread(target=self._spot_cut_loop, daemon=True, name='IronFlyXspCutLoop')
        self._cut_thread.start()
        self._running = True
        log.info('IronFlyXspStrategy initialized for %s (mode=%s)', self._today_str, self.config.mode)

    def run(self) -> None:
        self.start()
        interval = max(1.0, self.config.tick_ms / 1000.0)
        while not self._cut_stop.is_set():
            try:
                self.on_tick()
            except Exception as e:
                log.exception('Error in IronFlyXspStrategy tick: %s', e)
            time.sleep(interval)

    def stop(self) -> None:
        self._running = False
        self._cut_stop.set()
        if self._cut_thread and self._cut_thread.is_alive():
            self._cut_thread.join(timeout=3.0)
        if self.book:
            self.book.save()
        log.info('IronFlyXspStrategy stopped')

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            if not self.book:
                return {'running': self._running, 'name': self.config.name}
            return {
                'name': self.config.name,
                'running': self._running,
                'mode': self.config.mode,
                'paper': self.config.paper,
                'open_pairs': sum(1 for p in self.book.pairs.values() if p.status == FlyStatus.OPEN),
                'total_pairs': len(self.book.pairs),
                'open_risk': self.book.open_risk,
                'realized_pnl': self.book.realized_pnl,
                'unrealized_pnl': self.book.open_pnl,
                'total_fees': self.book.total_fees,
                'current_tier': self.book.current_tier,
                'last_spot': self.last_spot,
            }

    def _get_live_spot(self) -> Optional[float]:
        """Returns live XSP spot price (derived from live SPX / 10 if direct XSP is absent)."""
        prices = getattr(self, 'prices', None)
        if prices:
            try:
                xsp = prices.get_price('XSP') or prices.get_price('.XSP')
                if xsp and float(xsp) > 0:
                    self.last_spot_update_ts = time.time()
                    self.last_spot = float(xsp)
                    return float(xsp)
            except Exception:
                pass
            try:
                spx = prices.get_spx() or prices.get_index()
                if spx and float(spx) > 0:
                    self.last_spx_spot = float(spx)
                    self.last_spot_update_ts = time.time()
                    self.last_spot = round(float(spx) / 10.0, 2)
                    return self.last_spot
            except Exception:
                pass

        if self.broker:
            try:
                spx = self.broker.get_spx_price() if hasattr(self.broker, 'get_spx_price') else None
                if spx is None and hasattr(self.broker, 'fetch_spx_price_api'):
                    spx = self.broker.fetch_spx_price_api()
                if spx and float(spx) > 0:
                    self.last_spx_spot = float(spx)
                    self.last_spot_update_ts = time.time()
                    self.last_spot = round(float(spx) / 10.0, 2)
                    return self.last_spot
            except Exception:
                pass

        return self.last_spot

    def _spot_cut_loop(self) -> None:
        """2-second background cadence: checks live spot against beyond break-even boundaries."""
        interval = max(0.5, self.config.cut_check_ms / 1000.0)
        while not self._cut_stop.is_set():
            try:
                self._check_spot_cuts()
            except Exception as exc:
                log.error('IronFlyXsp: Exception in spot cut loop: %s', exc, exc_info=True)
            self._cut_stop.wait(interval)

    def _check_spot_cuts(self) -> None:
        with self._lock:
            if not self.book:
                return
            spot = self._get_live_spot()
            if spot is None:
                return
            self.book.record_spot(spot)

            # Check quote staleness
            if time.time() - self.last_spot_update_ts > self.config.stale_spot_s:
                return

            chain = self._fetch_xsp_chain()
            now_t = datetime.now().time()

            for pair in list(self.book.pairs.values()):
                if pair.status != FlyStatus.OPEN:
                    continue
                bbe = calculate_bbe_amount_xsp(pair.credit or 0.0, now_t, self.rules)
                ror = (pair.open_pnl / pair.open_risk) if (pair.open_risk and pair.open_risk > 0) else None

                cut_pair, reason_pair = should_cut_xsp(spot, pair.be_low, pair.be_high, beyond_be_amount=bbe, current_ror=ror, min_close_ror=self.config.min_close_ror)
                cut_a, reason_a = should_cut_xsp(spot, pair.fly_a.be_low, pair.fly_a.be_high, beyond_be_amount=bbe, current_ror=ror, min_close_ror=self.config.min_close_ror)
                cut_b, reason_b = should_cut_xsp(spot, pair.fly_b.be_low, pair.fly_b.be_high, beyond_be_amount=bbe, current_ror=ror, min_close_ror=self.config.min_close_ror)

                if cut_pair or cut_a or cut_b:
                    reason = reason_pair or reason_a or reason_b
                    self.book.log_event('SPOT_CUT', f'Pair {pair.pair_id} beyond break-even breach (bbe={bbe:.2f}): {reason}')
                    expiry_yymmdd = datetime.strptime(self._today_str, '%Y-%m-%d').strftime('%y%m%d')
                    if self.config.paper:
                        self.paper_executor.execute_close_pair(pair, expiry_yymmdd, chain, reason)
                    else:
                        self.broker_executor.execute_close_pair(pair, expiry_yymmdd, chain, reason)
                    self.book.save()

    def _fetch_xsp_chain(self) -> Dict[Tuple[str, int], dict]:
        """Fetches option chain for XSP around spot."""
        chain: Dict[Tuple[str, int], dict] = {}
        if not self.broker:
            return chain

        spot = self._get_live_spot()
        if not spot:
            return chain

        spx_spot = self.last_spx_spot or (spot * 10.0)
        spx_center = atm_center_spx(spx_spot)
        center_a, center_b = get_xsp_centers(spx_center)
        min_center = min(center_a, center_b)
        max_center = max(center_a, center_b)
        strikes = list(range(min_center - 10, max_center + 11))

        expiry_yymmdd = datetime.strptime(self._today_str, '%Y-%m-%d').strftime('%y%m%d')
        xsp_symbols = []
        xsp_map = {}
        spx_symbols = []
        spx_map = {}
        for k in strikes:
            c_xsp = build_tastytrade_symbol(expiry_yymmdd, 'C', k, ticker='XSP')
            p_xsp = build_tastytrade_symbol(expiry_yymmdd, 'P', k, ticker='XSP')
            xsp_symbols.extend([c_xsp, p_xsp])
            xsp_map[c_xsp] = ('C', k)
            xsp_map[p_xsp] = ('P', k)

            c_spx = build_tastytrade_symbol(expiry_yymmdd, 'C', k * 10, ticker='SPXW')
            p_spx = build_tastytrade_symbol(expiry_yymmdd, 'P', k * 10, ticker='SPXW')
            spx_symbols.extend([c_spx, p_spx])
            spx_map[c_spx] = ('C', k)
            spx_map[p_spx] = ('P', k)

        try:
            mids = {}
            if hasattr(self.broker, 'fetch_option_mids_api'):
                mids = self.broker.fetch_option_mids_api(xsp_symbols)
                if not mids:
                    spx_mids = self.broker.fetch_option_mids_api(spx_symbols)
                    for s_sym, (right, k) in spx_map.items():
                        spx_mid = spx_mids.get(s_sym)
                        if spx_mid is not None and spx_mid > 0:
                            scaled_mid = round(float(spx_mid) / 10.0, 2)
                            chain[(right, k)] = {
                                'mid': scaled_mid,
                                'bid': round(scaled_mid - 0.05, 2),
                                'ask': round(scaled_mid + 0.05, 2),
                            }
                    if chain:
                        return chain

            for x_sym, (right, k) in xsp_map.items():
                mid = mids.get(x_sym) or (self.broker.get_option_price(x_sym) if self.broker else None)
                if mid is not None and mid > 0:
                    chain[(right, k)] = {
                        'mid': round(float(mid), 2),
                        'bid': round(float(mid) - 0.05, 2),
                        'ask': round(float(mid) + 0.05, 2),
                    }
        except Exception as exc:
            log.warning('IronFlyXsp: fetch chain failed: %s', exc)

        return chain

    def on_tick(self) -> None:
        """15-second main sweep cadence."""
        with self._lock:
            if not self.book:
                return

            today = central_today()
            self._today_str = today.isoformat()

            # Calendar Skip Check
            skip = skip_reason(today)
            if skip:
                self.book.log_event('SKIP', f'Day skipped: {skip}')
                self.book.save()
                return

            spot = self._get_live_spot()
            if spot is None:
                return

            # Staleness guard
            if time.time() - self.last_spot_update_ts > self.config.stale_spot_s:
                return

            # Risk Caps Check
            if self.book.open_risk >= self.config.max_open_risk:
                return
            if self.book.realized_pnl <= -abs(self.config.daily_loss_stop):
                return
            if self.book.pairs_opened_today >= self.config.max_flies_per_day:
                return

            now_t = datetime.now().time()
            if now_t >= self.rules.adds_cutoff:
                return

            open_count = sum(1 for p in self.book.pairs.values() if p.status in (FlyStatus.OPEN, FlyStatus.OPENING))
            if open_count >= self.config.max_open_flies:
                return
            if self.book.pairs and open_count == 0 and not self.config.restart_when_flat:
                return

            chain = self._fetch_xsp_chain()

            spx_spot = self.last_spx_spot or (spot * 10.0)
            spx_center = atm_center_spx(spx_spot)
            center_a, center_b = get_xsp_centers(spx_center)

            # Update Straddle
            straddle_mid = self._get_atm_straddle_mid(center_a, chain)
            if straddle_mid is not None:
                if self.book.em_open is None:
                    self.book.em_open = straddle_mid
                    self.book.log_event('OPEN_EM', f'Initial XSP Straddle EM open: ${straddle_mid:.2f}')
                self.book.em_now = straddle_mid
                if self.book.em_open > 0:
                    self.book.ratio_vs_83 = round(straddle_mid / self.book.em_open, 4)

            # 1. Initial Position Entry
            if not self.book.pairs:
                if now_t < self.rules.first_fly_time:
                    self.book.save()
                    return

                # Open Initial Pair ATM with 5 pt wings ($50 SPX equivalent)
                self._plan_and_open_pair(spx_center, center_a, center_b, chain, is_initial=True)
                self._update_open_pairs_pnl(chain)
                self.book.save()
                return

            # 2. Subsequent Adds
            open_pairs = [p for p in self.book.pairs.values() if p.status == FlyStatus.OPEN]
            if not open_pairs:
                return

            closest_pair = min(open_pairs, key=lambda p: abs(spot - p.center_ideal))
            closest_credit = closest_pair.credit or 0.0

            mid_5w = self._get_atm_straddle_mid(center_a, chain)

            triggered, scenario, threshold, offset, itm_dist = evaluate_add_trigger_xsp(
                spot_xsp=spot,
                closest_center_ideal=closest_pair.center_ideal,
                closest_credit=closest_credit,
                now_time=now_t,
                mid_5w=mid_5w,
                rules=self.rules,
            )

            # Record active tier tag in diary
            self.book.current_tier = 4 if scenario == 'tight' else 3 if scenario == 'high_credit' else 2 if scenario == 'mid_credit' else 1

            if triggered:
                open_spx_centers = [p.spx_center for p in open_pairs]
                offset_spx = offset * 10.0
                cand_spx = next_spx_center_xsp(spx_spot, closest_pair.spx_center, offset_spx, open_spx_centers)
                if cand_spx is not None:
                    c_a, c_b = get_xsp_centers(cand_spx)
                    self._plan_and_open_pair(cand_spx, c_a, c_b, chain, is_initial=False)

            self._update_open_pairs_pnl(chain)
            self.book.save()

    def _get_atm_straddle_mid(self, center: int, chain: Dict[Tuple[str, int], dict]) -> Optional[float]:
        call = chain.get(('C', center))
        put = chain.get(('P', center))
        if call and put:
            cm = float(call.get('mid') or (call.get('bid', 0) + call.get('ask', 0)) / 2)
            pm = float(put.get('mid') or (put.get('bid', 0) + put.get('ask', 0)) / 2)
            return round(cm + pm, 2)
        return None

    def _plan_and_open_pair(
        self,
        spx_center: int,
        center_a: int,
        center_b: int,
        chain: Dict[Tuple[str, int], dict],
        is_initial: bool = False,
    ) -> None:
        short_strikes = self.book.get_short_legs_map()
        cw, pw = pick_widths_xsp(
            self.config.spx_cw_default,
            self.config.spx_pw_default,
            center_a,
            center_b,
            short_strikes,
            min_width=self.rules.min_wing_width,
            max_width=self.rules.max_wing_width,
            is_initial=is_initial,
            chain=chain,
        )

        pair_num = self.book.pairs_opened_today + 1
        pair_id = f'xsp-pair-{pair_num}'

        price_a = price_fly_xsp(center_a, cw, pw, chain)
        price_b = price_fly_xsp(center_b, cw, pw, chain)

        if not price_a or not price_b:
            log.warning('Could not price XSP pair at centers %d, %d', center_a, center_b)
            return

        mid_a = price_a[1]
        mid_b = price_b[1]
        if mid_a <= 0 or mid_b <= 0:
            log.warning('Rejected non-positive pricing for XSP pair: A=%.2f, B=%.2f', mid_a, mid_b)
            return

        ideal_center = spx_center / 10.0
        risk_a = max(0.0, round((max(cw, pw) - mid_a) * 100, 2))
        risk_b = max(0.0, round((max(cw, pw) - mid_b) * 100, 2))
        qty_per_fly = max(1, self.config.contracts // 2)
        fly_a = IronFly(
            id=f'xsp-{pair_num}a',
            center=center_a,
            cw=cw,
            pw=pw,
            call_long=center_a + cw,
            put_long=center_a - pw,
            planned_credit=mid_a,
            quantity=qty_per_fly,
            open_risk=risk_a,
        )
        fly_b = IronFly(
            id=f'xsp-{pair_num}b',
            center=center_b,
            cw=cw,
            pw=pw,
            call_long=center_b + cw,
            put_long=center_b - pw,
            planned_credit=mid_b,
            quantity=qty_per_fly,
            open_risk=risk_b,
        )

        pair = IronFlyXspPair(
            pair_id=pair_id,
            spx_center=spx_center,
            center_ideal=ideal_center,
            fly_a=fly_a,
            fly_b=fly_b,
            status=FlyStatus.OPENING,
        )

        expiry_yymmdd = datetime.strptime(self._today_str, '%Y-%m-%d').strftime('%y%m%d')

        if self.config.mode == 'LIVE_APPROVE':
            self.book.pending_approval[pair_id] = pair
            self.book.log_event('PENDING_PAIR', f'Pair {pair_id} awaiting approval: ideal {ideal_center} ({center_a}/{center_b})')
            return

        ok = False
        if self.config.paper:
            ok = self.paper_executor.execute_open_pair(pair, expiry_yymmdd, chain)
        else:
            ok = self.broker_executor.execute_open_pair(pair, expiry_yymmdd, chain)

        if ok:
            self.book.add_pair(pair)
            self.last_center = spx_center
            all_symbols = [leg.symbol for leg in pair.fly_a.legs + pair.fly_b.legs]
            try:
                register_option_symbols(all_symbols, lot='IRON_FLY_XSP')
            except Exception as e:
                log.debug('Could not register streamer symbols: %s', e)

    def _update_open_pairs_pnl(self, chain: Dict[Tuple[str, int], dict]) -> None:
        for pair in self.book.pairs.values():
            if pair.status != FlyStatus.OPEN:
                continue
            for fly in (pair.fly_a, pair.fly_b):
                pricing = price_fly_xsp(fly.center, fly.cw, fly.pw, chain)
                if pricing:
                    nat, mid = pricing
                    current_mark_debit = max(0.05, max(fly.cw, fly.pw) - mid)
                    fly.current_mark_debit = round(current_mark_debit, 2)
                    gross = ((fly.credit or 0.0) - current_mark_debit) * 100 * fly.quantity
                    fly.unrealized_pnl = round(gross - fly.fees_paid, 2)

    def approve_fly(self, pair_id: str) -> bool:
        with self._lock:
            if not self.book or pair_id not in self.book.pending_approval:
                return False
            pair = self.book.pending_approval.pop(pair_id)
            chain = self._fetch_xsp_chain()
            expiry_yymmdd = datetime.strptime(self._today_str, '%Y-%m-%d').strftime('%y%m%d')
            ok = False
            if self.config.paper:
                ok = self.paper_executor.execute_open_pair(pair, expiry_yymmdd, chain)
            else:
                ok = self.broker_executor.execute_open_pair(pair, expiry_yymmdd, chain)
            if ok:
                self.book.add_pair(pair)
                self.last_center = pair.spx_center
            self.book.save()
            return ok

    def skip_fly(self, pair_id: str) -> bool:
        with self._lock:
            if not self.book or pair_id not in self.book.pending_approval:
                return False
            self.book.pending_approval.pop(pair_id)
            self.book.log_event('SKIP_PAIR', f'Pair {pair_id} skipped by operator')
            self.book.save()
            return True

    def close_all_open_flies(self, reason: str = 'manual_close_all') -> None:
        if not self.book:
            return
        chain = self._fetch_xsp_chain()
        expiry_yymmdd = datetime.strptime(self._today_str, '%Y-%m-%d').strftime('%y%m%d')
        for pair in list(self.book.pairs.values()):
            if pair.status == FlyStatus.OPEN:
                if self.config.paper:
                    self.paper_executor.execute_close_pair(pair, expiry_yymmdd, chain, reason)
                else:
                    self.broker_executor.execute_close_pair(pair, expiry_yymmdd, chain, reason)
        self.book.save()

    @staticmethod
    def register_open_trade_streamer_legs(book_data: dict, logger: logging.Logger) -> None:
        symbols: List[str] = []
        for pair in book_data.get('pairs', {}).values():
            for fkey in ('fly_a', 'fly_b'):
                f = pair.get(fkey, {})
                if f.get('status') in (FlyStatus.OPEN, FlyStatus.OPENING, FlyStatus.CLOSING):
                    for leg in f.get('legs', []):
                        sym = leg.get('symbol')
                        if sym:
                            symbols.append(sym)
        if symbols:
            register_option_symbols(symbols, lot='IRON_FLY_XSP', logger=logger)
            logger.info('Registered %d open XSP Iron Fly legs with streamer', len(symbols))
