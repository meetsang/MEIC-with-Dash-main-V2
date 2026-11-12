"""Daily Reset MA + Hilega-Milega (DRMA_HM) Multi-Stock Strategy Implementation."""
from __future__ import annotations

import glob
import json
import logging
import os
import uuid
import time
from datetime import date, datetime, time as dt_time
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from common.config import check_trading_day
from common.market_hours import effective_session_time_str_ct, market_close_time_ct
from common.session_cleanup import central_today
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy
from strategies.base import BaseStrategy
from strategies.drma_stocks.config import DRMAStocksConfig
from strategies.drma_stocks.market_data import (
    compute_stock_indicators,
    fetch_stock_5m_bars,
    fetch_stock_daily_levels,
    get_nearest_weekly_expiry,
    select_stock_options_strikes,
)
from strategies.drma_stocks.settlement import (
    move_trade_to_history,
    settle_stock_trade,
    update_stock_unrealized_marks,
)
from strategies.drma_stocks.watchlist import get_active_symbols, load_watchlist

log = logging.getLogger(__name__)

_CT = ZoneInfo('America/Chicago')
_ET = ZoneInfo('America/New_York')


class DRMAStocksStrategy(BaseStrategy):
    """DRMA + Hilega-Milega Intraday Strategy on Individual Stocks.

    Tracks a configurable watchlist of equities (e.g. NVDA, MU, AMZN, SPCX, MSTR).
    Executes:
      1. Directional 10-wide Debit Spreads on Grade A+ setups (0-7 DTE weekly options).
      2. Non-Directional 10-wide Credit Spreads on Grade A setups (0-7 DTE weekly options).
      3. Direct Stock Equity Buy / Sell on Grade A+ setups (fixed dollar allocation).
    All signals across all stocks club together into a unified dashboard view.
    Everything is executed in paper mode.
    """

    def __init__(self, config: Optional[DRMAStocksConfig] = None, broker=None):
        config = config or DRMAStocksConfig()
        super().__init__(config, broker)
        self.config: DRMAStocksConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self._status: Dict[str, Any] = {'running': False, 'state': 'INIT', 'stocks': {}}
        self._ticker_states: Dict[str, Dict[str, Any]] = {}

    def stop(self) -> None:
        self._running = False
        self._status['running'] = False
        self._status['state'] = 'STOPPED'
        self.logger.info('%s stopped', self.config.name)

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)

    def _resolve_eod_exit_time(self, root: Optional[str] = None, for_date: Optional[date] = None) -> str:
        """Resolve EOD flatten cutoff, dynamically aligning with DRMA_HM_SPX and NYSE half-days."""
        raw = getattr(self.config, 'eod_exit_time_cst', '14:50') or '14:50'
        if root:
            try:
                from common.daily_overrides import get_overrides
                stocks_ov = get_overrides(self.config.name, root)
                if stocks_ov and stocks_ov.get('eod_exit_time_cst'):
                    raw = str(stocks_ov['eod_exit_time_cst'])
                else:
                    hm_ov = get_overrides('DRMA_HM_SPX', root)
                    if hm_ov and hm_ov.get('eod_exit_time_cst'):
                        raw = str(hm_ov['eod_exit_time_cst'])
            except Exception:
                pass
        return effective_session_time_str_ct(raw, for_date or central_today(), default=dt_time(14, 50))

    def run(self) -> None:
        self._running = True
        self._status['running'] = True
        self._status['state'] = 'RUNNING'
        root = getattr(self.config, 'root', None) or os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')
        )
        poll_interval = float(getattr(self.config, 'poll_interval_sec', 10.0) or 10.0)

        try:
            while self._running:
                today = central_today()
                now_ct = datetime.now(_CT)
                now_t = now_ct.time()
                market_close = market_close_time_ct(today)

                # Check if trading day
                try:
                    should_trade, reason = check_trading_day()
                    if not should_trade:
                        self.logger.info("Not a trading day (%s) ? exiting %s run loop", reason, self.config.name)
                        break
                except Exception:
                    pass

                self.evaluate_all_stocks(root, target_date=today)

                # Past market close (15:00 CST), finish daytime session
                if now_t >= market_close:
                    self.logger.info("Market close (15:00 CST) reached ? %s session complete", self.config.name)
                    break

                time.sleep(poll_interval)
        except Exception as exc:
            self.logger.error("Error running %s: %s", self.config.name, exc, exc_info=True)
            self._status['state'] = 'ERROR'
            self._status['error'] = str(exc)
        finally:
            self._running = False
            self._status['running'] = False
            self._status['state'] = 'STOPPED'

    def _get_ticker_state(self, ticker: str) -> Dict[str, Any]:
        if ticker not in self._ticker_states:
            self._ticker_states[ticker] = {
                'position_state': 0,     # 0 = Flat, 1 = Long, -1 = Short
                'last_signal_bar': -999,
                'daily_signals': 0,
            }
        return self._ticker_states[ticker]

    def evaluate_all_stocks(self, root: str, target_date: Optional[date] = None) -> Dict[str, Any]:
        """Evaluate indicators and process trade signals across all watchlist stocks."""
        today = target_date or central_today()
        today_str = today.isoformat()
        active_dir = active_dir_for_strategy(self.config.name, root)
        hist_dir = history_dir_for_strategy(self.config.name, root)
        os.makedirs(active_dir, exist_ok=True)
        os.makedirs(hist_dir, exist_ok=True)

        symbols = get_active_symbols(root)
        results: Dict[str, Any] = {}
        active_trades = self._load_active_trades(active_dir, today_str)

        current_spots: Dict[str, float] = {}

        for sym in symbols:
            try:
                stock_res = self._evaluate_single_stock(sym, today, root, active_trades, active_dir, hist_dir)
                results[sym] = stock_res
                if stock_res and stock_res.get('spot', 0.0) > 0:
                    current_spots[sym] = float(stock_res['spot'])
            except Exception as exc:
                self.logger.warning("Error evaluating stock %s: %s", sym, exc)

        # Enforce open trade management and day-trade flattening across ALL active trades
        self._manage_all_active_trades(active_trades, hist_dir, current_spots, today, root)

        # Write session snapshot
        session_path = os.path.join(active_dir, f'session_{today_str}.json')
        try:
            with open(session_path, 'w', encoding='utf-8') as f:
                json.dump({'date': today_str, 'stocks': results, 'updated_at': datetime.now().isoformat()}, f, indent=2)
        except Exception:
            pass

        self._status = {
            'running': self._running,
            'state': 'READY',
            'stocks': results,
            'updated_at': datetime.now().isoformat(),
        }
        return results

    def _manage_all_active_trades(
        self,
        active_trades: List[Dict[str, Any]],
        hist_dir: str,
        current_spots: Dict[str, float],
        today: date,
        root: Optional[str] = None,
    ) -> None:
        """Enforce day-trading rules, EOD flattening, and exits across all active trades."""
        now_ct = datetime.now(_CT)
        now_ct_str = now_ct.strftime('%H:%M')
        today_str = today.isoformat()
        eod_cutoff = self._resolve_eod_exit_time(root)
        force_eod = getattr(self.config, 'force_eod_exit', True)

        for t_info in active_trades:
            trade = t_info['trade']
            trade_path = t_info['path']
            ticker = t_info.get('ticker') or trade.get('ticker')

            if trade.get('status') != 'open':
                continue

            # Prior day trade: Day trading only - must never carry trades to more than a single day
            entered_at_str = str(trade.get('entered_at') or '')
            trade_date_str = entered_at_str[:10] if len(entered_at_str) >= 10 else today_str
            is_prior_day = bool(trade_date_str and trade_date_str < today_str)
            is_eod_cutoff = (now_ct_str >= eod_cutoff)

            spot = current_spots.get(ticker, 0.0)
            if spot <= 0:
                spot = float(trade.get('current_spot') or trade.get('underlying_entry_price') or trade.get('entry_price') or 1.0)

            if spot > 0:
                update_stock_unrealized_marks(trade, spot)

            # If prior day or EOD cutoff reached, ensure trade exits immediately
            if force_eod and (is_prior_day or is_eod_cutoff):
                self._check_trade_exit(trade, trade_path, spot, hist_dir, today=today, root=root)

    def _evaluate_single_stock(
        self,
        ticker: str,
        today: date,
        root: str,
        active_trades: List[Dict[str, Any]],
        active_dir: str,
        hist_dir: str,
    ) -> Dict[str, Any]:
        """Process market data, indicators, and trade lifecycle for a single stock."""
        levels = fetch_stock_daily_levels(ticker, today, root=root)
        df_bars = fetch_stock_5m_bars(ticker, today, root)

        latest_snap = None
        current_spot = 0.0

        if not df_bars.empty and 'close' in df_bars.columns:
            df_out, snaps = compute_stock_indicators(ticker, df_bars, levels, self.config)
            if snaps:
                latest_snap = snaps[-1]
                current_spot = latest_snap.close

        # Monitor active trades for this stock
        ticker_trades = [t for t in active_trades if t.get('ticker') == ticker]
        for t_info in ticker_trades:
            t = t_info['trade']
            p = t_info['path']
            eff_spot = current_spot if current_spot > 0 else float(t.get('current_spot') or t.get('underlying_entry_price') or t.get('entry_price') or 1.0)
            if eff_spot > 0:
                update_stock_unrealized_marks(t, eff_spot)
                self._check_trade_exit(t, p, eff_spot, hist_dir, today=today, root=root)

        # Signal Generation and Entry
        if latest_snap and current_spot > 0:
            self._check_entry_signal(ticker, latest_snap, current_spot, today, active_dir, ticker_trades)

        summary = {
            'ticker': ticker,
            'symbol': ticker,
            'spot': current_spot,
            'price': current_spot,
            'levels': levels,
            'ema_21': latest_snap.out_ma if latest_snap else 0.0,
            'distance_ema': round(current_spot - latest_snap.out_ma, 2) if (latest_snap and latest_snap.out_ma) else 0.0,
            'distance_ema_pct': round((current_spot - latest_snap.out_ma) / latest_snap.out_ma * 100, 2) if (latest_snap and latest_snap.out_ma) else 0.0,
            'hm_fast': latest_snap.hm_fast if latest_snap else 50.0,
            'hm_slow': latest_snap.hm_slow if latest_snap else 50.0,
            'hm_bias': 'BULLISH' if latest_snap and latest_snap.bias == 'BULL' else ('BEARISH' if latest_snap and latest_snap.bias == 'BEAR' else 'NEUTRAL'),
            'rsi_9': latest_snap.rsi if latest_snap else 50.0,
            'score': latest_snap.active_score if latest_snap else 0,
            'grade': latest_snap.setup_grade if latest_snap else 'CHOP / NO EDGE',
            'recommended_bias': 'BULLISH' if latest_snap and latest_snap.bias == 'BULL' else ('BEARISH' if latest_snap and latest_snap.bias == 'BEAR' else 'NEUTRAL'),
            'recommended_trade': latest_snap.trade_vehicle if latest_snap else 'WAITING',
            'orb_formed': latest_snap.orb_formed if latest_snap else False,
            'orb_high': latest_snap.orb_high if latest_snap else None,
            'orb_low': latest_snap.orb_low if latest_snap else None,
            'last_bar': latest_snap.datetime_str if latest_snap else '',
            'status': 'ACTIVE',
            'indicators': {
                'last_bar': latest_snap.datetime_str if latest_snap else '',
                'close': latest_snap.close if latest_snap else current_spot,
                'drma': latest_snap.out_ma if latest_snap else 0.0,
                'orb_formed': latest_snap.orb_formed if latest_snap else False,
                'orb_high': latest_snap.orb_high if latest_snap else None,
                'orb_low': latest_snap.orb_low if latest_snap else None,
                'rsi': latest_snap.rsi if latest_snap else 50.0,
                'fast': latest_snap.hm_fast if latest_snap else 50.0,
                'slow': latest_snap.hm_slow if latest_snap else 50.0,
                'bias': latest_snap.bias if latest_snap else 'NEUTRAL',
                'grade': latest_snap.setup_grade if latest_snap else 'CHOP / NO EDGE',
                'vehicle': latest_snap.trade_vehicle if latest_snap else 'WAITING',
                'active_score': latest_snap.active_score if latest_snap else 0,
            } if latest_snap else None,
            'active_positions': len([t for t in ticker_trades if t['trade'].get('status') == 'open']),
        }
        return summary

    def _check_entry_signal(
        self,
        ticker: str,
        snap: Any,
        spot: float,
        today: date,
        active_dir: str,
        existing_trades: List[Dict[str, Any]],
    ) -> None:
        """Evaluate if current stock snapshot qualifies for trade entries."""
        st = self._get_ticker_state(ticker)
        if st['daily_signals'] >= self.config.max_daily_signals_per_ticker:
            return

        has_open = any(t['trade'].get('status') == 'open' for t in existing_trades)
        if has_open:
            return  # 1 position per stock at a time to prevent overexposure

        # 1. Require 30m ORB (8:30 - 9:00 AM CST) to be formed
        if getattr(self.config, 'wait_for_orb', True):
            if not getattr(snap, 'orb_formed', False):
                return

        # 2. Check regular trading start time (default 09:00 CST upon 30m ORB completion)
        reg_start = getattr(self.config, 'regular_trading_start_time_cst', '09:00')
        now_ct = datetime.now(_CT)
        bar_time_str = ''
        bar_dt_str = getattr(snap, 'datetime_str', '')
        if bar_dt_str:
            try:
                parts = bar_dt_str.split(' ')
                if len(parts) > 1:
                    bar_time_str = parts[1][:5]
            except Exception:
                pass
        eval_time = bar_time_str or now_ct.strftime('%H:%M')
        if eval_time < reg_start:
            return

        # 3. Do not enter new positions after EOD cutoff (14:50 CST normal, 11:50 CST on half-days)
        eod_cutoff = self._resolve_eod_exit_time(getattr(self.config, 'root', None), for_date=today)
        now_ct_str = datetime.now(_CT).strftime('%H:%M')
        if now_ct_str >= eod_cutoff or eval_time >= eod_cutoff:
            return

        is_bull = (snap.bias == 'BULL')
        is_bear = (snap.bias == 'BEAR')
        if not (is_bull or is_bear):
            return

        score = getattr(snap, 'active_score', 0)
        grade = getattr(snap, 'setup_grade', '')
        expiry = get_nearest_weekly_expiry(today, self.config.min_dte, self.config.max_dte)

        # 1. Grade A+ (Score 5/5): Direct Stock Equity Buy/Sell + Directional Spreads
        if score == 5:
            # Direct Stock Buy / Sell
            if self.config.is_stock_equity_enabled:
                if is_bull or (is_bear and self.config.stock_allow_short):
                    self._create_direct_stock_trade(ticker, snap, spot, active_dir)

            # Directional 10-wide Debit Spread (Calls or Puts)
            if self.config.is_directional_options_enabled:
                self._create_option_spread_trade(
                    ticker=ticker,
                    snap=snap,
                    spot=spot,
                    expiry=expiry,
                    spread_type='debit_spread',
                    side='C' if is_bull else 'P',
                    active_dir=active_dir,
                )

            st['daily_signals'] += 1

        # 2. Grade A (Score 4/5): Non-Directional 10-wide Credit Spreads (PCS or CCS)
        elif score == 4 and 'a+ only' not in self.config.min_grade_to_signal.lower():
            if self.config.is_non_directional_options_enabled:
                self._create_option_spread_trade(
                    ticker=ticker,
                    snap=snap,
                    spot=spot,
                    expiry=expiry,
                    spread_type='credit_spread',
                    side='P' if is_bull else 'C',  # Bull = PCS, Bear = CCS
                    active_dir=active_dir,
                )
                st['daily_signals'] += 1

    def _create_direct_stock_trade(
        self,
        ticker: str,
        snap: Any,
        spot: float,
        active_dir: str,
    ) -> None:
        """Create a paper Direct Stock Buy/Sell trade (the futures equivalent)."""
        is_bull = (snap.bias == 'BULL')
        side = 'LONG' if is_bull else 'SHORT'
        # Dollar allocation: $5,000 buying power -> shares = $5,000 / spot
        shares = max(1, int(self.config.stock_allocation_dollars / spot))
        pt_price = round(spot * (1.0 + self.config.stock_profit_target_pct) if is_bull else spot * (1.0 - self.config.stock_profit_target_pct), 2)
        sl_price = round(spot * (1.0 - self.config.stock_stop_loss_pct) if is_bull else spot * (1.0 + self.config.stock_stop_loss_pct), 2)

        trade_id = f"{ticker}_equity_{side.lower()}_{uuid.uuid4().hex[:6]}"
        trade_data = {
            'trade_id': trade_id,
            'lot': trade_id,
            'ticker': ticker,
            'strategy': self.config.name,
            'sub_strategy': 'DRMA_STOCK_EQUITY',
            'trade_type': 'DIRECT_STOCK',
            'structure_type': 'equity_stock',
            'side': side,
            'shares': shares,
            'quantity': shares,
            'entry_price': spot,
            'current_mark': spot,
            'underlying_entry_price': spot,
            'profit_target_price': pt_price,
            'stop_loss_price': sl_price,
            'profit_target_pct': self.config.stock_profit_target_pct,
            'stop_loss_pct': self.config.stock_stop_loss_pct,
            'status': 'open',
            'entered_at': datetime.now().isoformat(),
            'paper': True,
            'unrealized_pnl_dollars': 0.0,
            'realized_pnl_dollars': 0.0,
            'reason': f'DRMA Grade A+ (Score {snap.active_score}/5)',
        }

        path = os.path.join(active_dir, f"{trade_id}.json")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(trade_data, f, indent=2)
            self.logger.info("Created PAPER Direct Stock trade: %s %s %d shs @ %.2f", side, ticker, shares, spot)
        except Exception as exc:
            self.logger.error("Failed to write trade %s: %s", path, exc)

    def _create_option_spread_trade(
        self,
        ticker: str,
        snap: Any,
        spot: float,
        expiry: str,
        spread_type: str,
        side: str,
        active_dir: str,
    ) -> None:
        """Create a paper 10-wide option spread trade (Debit or Credit)."""
        long_k, short_k, long_sym, short_sym = select_stock_options_strikes(
            ticker=ticker,
            side=side,
            spot=spot,
            spread_type=spread_type,
            spread_width=self.config.spread_width,
            expiry_yymmdd=expiry,
        )

        is_debit = (spread_type == 'debit_spread')
        sub_strategy = 'DRMA_STOCK_DIR' if is_debit else 'DRMA_STOCK_NONDIR'
        side_label = 'CDS' if (is_debit and side == 'C') else ('PDS' if is_debit else ('PCS' if side == 'P' else 'CCS'))
        est_entry_price = 2.50 if is_debit else 1.00  # Default initial paper fill estimate

        pt_pct = self.config.dir_profit_target_pct if is_debit else self.config.non_dir_profit_target_pct
        sl_pct = self.config.dir_stop_loss_pct if is_debit else self.config.non_dir_stop_loss_pct

        if is_debit:
            pt_price = round(est_entry_price * (1.0 + pt_pct), 2)
            sl_price = round(max(0.0, est_entry_price * (1.0 - sl_pct)), 2)
        else:
            pt_price = round(max(0.0, est_entry_price * (1.0 - pt_pct)), 2)
            sl_price = round(est_entry_price * (1.0 + sl_pct), 2)

        trade_id = f"{ticker}_{side_label.lower()}_{uuid.uuid4().hex[:6]}"
        trade_data = {
            'trade_id': trade_id,
            'lot': trade_id,
            'ticker': ticker,
            'strategy': self.config.name,
            'sub_strategy': sub_strategy,
            'trade_type': 'DIRECTIONAL' if is_debit else 'NON_DIRECTIONAL',
            'structure_type': spread_type,
            'side': 'LONG' if snap.bias == 'BULL' else 'SHORT',
            'opt_type': side,
            'expiry': expiry,
            'quantity': self.config.options_quantity,
            'entry_price': est_entry_price,
            'current_mark': est_entry_price,
            'underlying_entry_price': spot,
            'spread_width': self.config.spread_width,
            'long_strike': long_k,
            'short_strike': short_k,
            'profit_target_pct': pt_pct,
            'stop_loss_pct': sl_pct,
            'profit_target_price': pt_price,
            'stop_loss_price': sl_price,
            'profit_target': pt_price,
            'stop_loss': sl_price,
            'status': 'open',
            'entered_at': datetime.now().isoformat(),
            'paper': True,
            'unrealized_pnl_dollars': 0.0,
            'realized_pnl_dollars': 0.0,
            'structures': [{
                'name': side_label,
                'legs': [
                    {'action': 'BUY', 'symbol': long_sym, 'strike': long_k, 'ratio': 1},
                    {'action': 'SELL', 'symbol': short_sym, 'strike': short_k, 'ratio': 1},
                ]
            }],
            'reason': f'DRMA {snap.setup_grade} (Score {snap.active_score}/5)',
        }

        path = os.path.join(active_dir, f"{trade_id}.json")
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(trade_data, f, indent=2)
            self.logger.info("Created PAPER Options Spread: %s %s %s Long=%s Short=%s", ticker, side_label, expiry, long_k, short_k)
        except Exception as exc:
            self.logger.error("Failed to write trade %s: %s", path, exc)

    def _check_trade_exit(
        self,
        trade: Dict[str, Any],
        trade_path: str,
        spot: float,
        hist_dir: str,
        today: Optional[date] = None,
        root: Optional[str] = None,
    ) -> None:
        """Evaluate profit target, stop loss, and EOD cutoff rules for an active stock trade."""
        if trade.get('status') != 'open':
            return

        fname = os.path.basename(trade_path)
        if hist_dir and os.path.isfile(os.path.join(hist_dir, fname)):
            try:
                if os.path.isfile(trade_path):
                    os.remove(trade_path)
            except Exception:
                pass
            return

        should_exit = False
        exit_reason = ''
        structure = str(trade.get('structure_type') or '').lower()

        now_ct = datetime.now(_CT)
        now_ct_str = now_ct.strftime('%H:%M')
        today_date = today or now_ct.date()
        today_str = today_date.isoformat()

        # Day trading algo only: Never carry trades to more than a single day
        entered_at_str = str(trade.get('entered_at') or '')
        trade_date_str = entered_at_str[:10] if len(entered_at_str) >= 10 else today_str
        is_prior_day = bool(trade_date_str and trade_date_str < today_str)

        # EOD Cutoff Check: Align with DRMA flatten time (default 14:50 CST)
        eod_cutoff = self._resolve_eod_exit_time(root)
        is_eod_cutoff = (now_ct_str >= eod_cutoff)

        if getattr(self.config, 'force_eod_exit', True) and (is_prior_day or is_eod_cutoff):
            should_exit = True
            exit_reason = f'EOD Cutoff ({eod_cutoff} CST)'
            if 'stock' in structure or 'equity' in structure:
                exit_price = spot if spot > 0 else float(trade.get('current_spot') or trade.get('underlying_entry_price') or trade.get('entry_price') or 1.0)
            else:
                exit_price = float(trade.get('current_mark') or trade.get('entry_price') or 1.0)
        elif 'stock' in structure or 'equity' in structure:
            side = str(trade.get('side', 'LONG')).upper()
            pt_px = float(trade.get('profit_target_price') or 0.0)
            sl_px = float(trade.get('stop_loss_price') or 0.0)
            if side == 'LONG':
                trade['profit_target_price'] = pt_px
                trade['stop_loss_price'] = sl_px
                trade['profit_target'] = pt_px
                trade['stop_loss'] = sl_px
                if pt_px > 0 and spot >= pt_px:
                    should_exit = True
                    exit_reason = f'Stock PT Hit (+{self.config.stock_profit_target_pct*100:.1f}%)'
                elif sl_px > 0 and spot <= sl_px:
                    should_exit = True
                    exit_reason = f'Stock SL Hit (-{self.config.stock_stop_loss_pct*100:.1f}%)'
            else:
                trade['profit_target_price'] = pt_px
                trade['stop_loss_price'] = sl_px
                trade['profit_target'] = pt_px
                trade['stop_loss'] = sl_px
                if pt_px > 0 and spot <= pt_px:
                    should_exit = True
                    exit_reason = f'Stock PT Hit (+{self.config.stock_profit_target_pct*100:.1f}%)'
                elif sl_px > 0 and spot >= sl_px:
                    should_exit = True
                    exit_reason = f'Stock SL Hit (-{self.config.stock_stop_loss_pct*100:.1f}%)'
            exit_price = spot
        else:
            entry_px = float(trade.get('entry_price') or 1.0)
            mark = float(trade.get('current_mark') or entry_px)
            pt_pct = float(trade.get('profit_target_pct') or 0.50)
            sl_pct = float(trade.get('stop_loss_pct') or 0.50)
            is_debit = ('debit' in structure)

            if is_debit:
                pt_price = round(entry_px * (1.0 + pt_pct), 2)
                sl_price = round(max(0.0, entry_px * (1.0 - sl_pct)), 2)
                trade['profit_target_price'] = pt_price
                trade['stop_loss_price'] = sl_price
                trade['profit_target'] = pt_price
                trade['stop_loss'] = sl_price
                # Debit spread: PT when mark >= entry + entry*pt_pct
                if mark >= pt_price:
                    should_exit = True
                    exit_reason = f'Options PT Hit (+{pt_pct*100:.0f}%)'
                elif mark <= sl_price:
                    should_exit = True
                    exit_reason = f'Options SL Hit (-{sl_pct*100:.0f}%)'
            else:
                pt_price = round(max(0.0, entry_px * (1.0 - pt_pct)), 2)
                sl_price = round(entry_px * (1.0 + sl_pct), 2)
                trade['profit_target_price'] = pt_price
                trade['stop_loss_price'] = sl_price
                trade['profit_target'] = pt_price
                trade['stop_loss'] = sl_price
                # Credit spread: PT when mark <= entry * (1 - pt_pct)
                if mark <= pt_price:
                    should_exit = True
                    exit_reason = f'Credit PT Hit (+{pt_pct*100:.0f}%)'
                elif mark >= sl_price:
                    should_exit = True
                    exit_reason = f'Credit SL Hit (-{sl_pct*100:.0f}%)'
            exit_price = mark

        if should_exit:
            self.logger.info("Exiting stock trade %s: %s @ %.2f", trade.get('trade_id'), exit_reason, exit_price)
            settle_stock_trade(trade, exit_price, exit_reason)
            try:
                with open(trade_path, 'w', encoding='utf-8') as f:
                    json.dump(trade, f, indent=2)
                move_trade_to_history(trade_path, hist_dir)
            except Exception as exc:
                self.logger.error("Error settling stock trade: %s", exc)
        else:
            fname = os.path.basename(trade_path)
            if hist_dir and os.path.isfile(os.path.join(hist_dir, fname)):
                try:
                    if os.path.isfile(trade_path):
                        os.remove(trade_path)
                except Exception:
                    pass
                return
            if trade.get('status') == 'closed':
                return
            try:
                with open(trade_path, 'w', encoding='utf-8') as f:
                    json.dump(trade, f, indent=2)
            except Exception:
                pass

    def _load_active_trades(self, active_dir: str, today_str: str) -> List[Dict[str, Any]]:
        """Find all active trades in the active directory."""
        trades = []
        if not os.path.isdir(active_dir):
            return trades
        hist_dir = os.path.join(os.path.dirname(active_dir), 'history', os.path.basename(active_dir))
        for path in glob.glob(os.path.join(active_dir, '*.json')):
            if os.path.basename(path).startswith('session_'):
                continue
            fname = os.path.basename(path)
            if os.path.isdir(hist_dir) and os.path.isfile(os.path.join(hist_dir, fname)):
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    t = json.load(f)
                if isinstance(t, dict):
                    if t.get('status') == 'closed':
                        try:
                            os.remove(path)
                        except Exception:
                            pass
                        continue
                    if t.get('status') in ('open', 'closing'):
                        trades.append({'trade': t, 'path': path, 'ticker': t.get('ticker')})
            except Exception:
                continue
        return trades
