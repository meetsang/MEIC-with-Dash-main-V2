"""0DTE Mean Reversion SPX Credit Spread Strategy.

Fades extreme moves when SPX touches/breaches the expected move boundary derived
from the 8:30 AM CST (9:30 AM ET) 0DTE ATM straddle price.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from blocks.stop.profiles.mean_reversion import MEAN_REVERSION_PROFILE
from common.symbols import build_tastytrade_symbol
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy
from meic0dte.app import utilities as util
from strategies.base import BaseStrategy, StrategyConfig
from strategies.mean_reversion.legs import (
    build_closing_legs,
    build_spread_legs,
    compute_expected_move,
    compute_expected_move_boundaries,
    round_atm,
    select_credit_spread_strikes,
)
from strategies.mean_reversion.settlement import (
    attach_dollar_pnl_display,
    pnl_points_to_dollars,
    update_unrealized_marks,
)

_CT = ZoneInfo('America/Chicago')
_ET = ZoneInfo('America/New_York')


@dataclass
class MeanReversionConfig(StrategyConfig):
    name: str = 'MEAN_REVERSION_SPX'
    scheduled: bool = False
    paper: bool = True  # Paper by default
    confirmed: bool = True
    requires_confirmation: bool = True
    stop_profile: str = MEAN_REVERSION_PROFILE

    # Session timing (Central Time, MEIC clock)
    open_time_cst: str = '08:30'       # Straddle expected-move establishment time (9:30 AM ET)
    cutoff_time_cst: str = '12:00'     # Stop taking new setups after 12:00 PM CST (1:00 PM ET)
    market_close_cst: str = '14:55'    # Close any open positions before market close

    # Expected move parameters
    em_multiplier: float = 1.0         # Expected move = ATM Straddle × multiplier
    atm_rounding: int = 5              # Nearest SPX strike interval

    # Spread selection parameters
    spread_width: int = 10             # Spread width in points (5 to 20 per article, 10 default)
    target_delta: float = 0.18         # Preferred short strike delta (~10 to 25 delta per article)
    min_delta: float = 0.10
    max_delta: float = 0.25
    quantity: int = 1                  # Quantity of spreads

    # Exit and risk management parameters
    profit_target_pct: float = 0.50    # Take profit at 50% of credit received
    stop_loss_pct: float = 1.00        # Initial stop loss at 100% of credit received (2x credit)
    breakeven_trigger_pct: float = 0.25 # Move stop to breakeven once 25% profit is reached
    breakeven_lock_pct: float = 0.10   # Lock in 10% profit on breakeven move (0.0 for pure breakeven)

    # Execution controls
    min_entry_credit: float = 0.50     # Reject setup if market credit is too thin
    poll_interval_sec: float = 5.0     # Monitoring poll interval in seconds
    entry_fill_wait_sec: int = 20      # Wait up to 20s for entry fill
    max_daily_loss: Optional[float] = None
    root: str = ''


class MeanReversionStrategy(BaseStrategy):
    """0DTE SPX Mean Reversion Credit Spread Strategy.

    Derives 0DTE expected move at 8:30 AM CST from the ATM straddle.
    Fades upper EM touch with Bear Call Spread; fades lower EM touch with Bull Put Spread.
    Exits at 50% profit target or 100% credit stop loss, trailing to breakeven at 25% gain.
    """

    def __init__(self, config: MeanReversionConfig = None, broker=None):
        config = config or MeanReversionConfig()
        super().__init__(config, broker)
        self.config: MeanReversionConfig = config
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
            allowed, reason = self._check_trading_day()
            if not allowed:
                self._write_skip(root, reason)
                return

            today = util.central_date()
            expiry_yymmdd = today.strftime('%y%m%d')

            # Wait for open time (8:30 AM CST / 9:30 AM ET)
            self._wait_for_open_window(root)
            if not self._running:
                return

            if self._is_disabled_today(root):
                self._write_skip(root, 'disabled_for_today')
                return

            # Check if trade already open or entered today
            session_state = self._load_session_state(root)
            if session_state.get('entered'):
                self.logger.info('MEAN_REVERSION already entered today — resuming monitor if open')
                open_trade = self._load_open_trade(root)
                if open_trade and open_trade.get('status') == 'open':
                    self._manage_open_trade(root, open_trade)
                return

            # 1. Establish Expected Move from ATM Straddle at 8:30 AM CST
            em_data = self._establish_expected_move(root, expiry_yymmdd)
            if not em_data:
                self._write_skip(root, 'failed_to_calculate_expected_move')
                return

            open_spx = em_data['open_spx']
            atm_strike = em_data['atm_strike']
            straddle_price = em_data['straddle_price']
            expected_move = em_data['expected_move']
            upper_em = em_data['upper_em']
            lower_em = em_data['lower_em']

            self.logger.info(
                'MEAN_REVERSION_ESTABLISHED: open_spx=%.2f atm=%d straddle=%.2f EM=%.2f Upper=%.2f Lower=%.2f',
                open_spx, atm_strike, straddle_price, expected_move, upper_em, lower_em,
            )

            # 2. Intraday Monitoring loop: wait for SPX to touch Upper or Lower EM
            while self._running:
                triggered_side = self._monitor_for_trigger(root, upper_em, lower_em)
                if not triggered_side or not self._running:
                    return

                # 3. Execute Mean Reversion Credit Spread Entry
                trade = self._execute_entry(
                    root,
                    side=triggered_side,
                    expiry_yymmdd=expiry_yymmdd,
                    open_spx=open_spx,
                    atm_strike=atm_strike,
                    straddle_price=straddle_price,
                    expected_move=expected_move,
                    upper_em=upper_em,
                    lower_em=lower_em,
                )
                if trade:
                    # 4. Actively manage position until profit target, stop loss, or market close
                    self._manage_open_trade(root, trade)
                    return

                if not self._running:
                    return
                self.logger.info('MEAN_REVERSION: Entry skipped (thin credit), cooling down 60s before resuming monitoring')
                time.sleep(60)

        except Exception:
            self.logger.exception('Mean Reversion strategy run failed')
            raise
        finally:
            self._running = False
            self._status['running'] = False

    def _wait_for_open_window(self, root: str) -> None:
        """Wait until 8:30 AM CST (market open)."""
        hour, minute = map(int, self.config.open_time_cst.split(':'))
        target = util.central_now().replace(hour=hour, minute=minute, second=0, microsecond=0)
        while self._running:
            if self._is_disabled_today(root):
                return
            now = util.central_now()
            if now >= target:
                return
            remaining = (target - now).total_seconds()
            time.sleep(min(15, max(1, remaining)))

    def _establish_expected_move(self, root: str, expiry_yymmdd: str) -> Optional[Dict[str, Any]]:
        """Calculate 0DTE expected move from the ATM straddle at open."""
        # Check if already calculated in session state
        existing = self._load_session_state(root)
        if existing.get('expected_move') and existing.get('upper_em'):
            return existing

        deadline = time.monotonic() + 120.0  # Allow up to 2 minutes for open quotes
        spot = None
        while time.monotonic() < deadline and self._running:
            spot = self.broker.get_spx_price() if self.broker else None
            if spot is None and self.broker:
                spot = self.broker.fetch_spx_price_api()
            if spot and spot > 1000:
                break
            time.sleep(2)

        if not spot:
            self.logger.error('MEAN_REVERSION: No SPX spot price available at open')
            return None

        atm = round_atm(spot, self.config.atm_rounding)
        call_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', atm)
        put_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', atm)

        straddle_price = None
        while time.monotonic() < deadline and self._running:
            mids = self.broker.fetch_option_mids_api([call_sym, put_sym]) if self.broker else {}
            call_mid = mids.get(call_sym) or (self.broker.get_option_price(call_sym) if self.broker else None)
            put_mid = mids.get(put_sym) or (self.broker.get_option_price(put_sym) if self.broker else None)

            if call_mid and put_mid and call_mid > 0 and put_mid > 0:
                straddle_price = round(float(call_mid) + float(put_mid), 2)
                break
            time.sleep(2)

        if not straddle_price:
            self.logger.warning('MEAN_REVERSION: Could not fetch ATM straddle mids, using fallback')
            # Fallback estimation if quote feed delayed: ~0.8% of spot for typical 0DTE straddle
            straddle_price = round(spot * 0.008, 2)

        expected_move = compute_expected_move(straddle_price, self.config.em_multiplier)
        upper_em, lower_em = compute_expected_move_boundaries(spot, expected_move)

        em_data = {
            'open_spx': round(spot, 2),
            'atm_strike': atm,
            'straddle_price': straddle_price,
            'expected_move': expected_move,
            'upper_em': upper_em,
            'lower_em': lower_em,
            'em_calculated_at': util.central_now().strftime('%H:%M:%S'),
        }
        self._save_session_state(root, **em_data)
        self._status.update(em_data)
        return em_data

    def _monitor_for_trigger(self, root: str, upper_em: float, lower_em: float) -> Optional[str]:
        """Intraday monitoring loop: checks for SPX reaching Upper or Lower EM boundary.

        Returns 'C' for Bear Call Spread (fade upside), or 'P' for Bull Put Spread (fade downside).
        """
        chour, cminute = map(int, self.config.cutoff_time_cst.split(':'))
        cutoff = util.central_now().replace(hour=chour, minute=cminute, second=0, microsecond=0)

        self.logger.info(
            'MEAN_REVERSION: Monitoring for touch. Upper EM=%.2f, Lower EM=%.2f (Cutoff: %s CST)',
            upper_em, lower_em, self.config.cutoff_time_cst,
        )

        while self._running:
            if self._is_disabled_today(root):
                self._write_skip(root, 'disabled_for_today')
                return None

            now = util.central_now()
            if now >= cutoff:
                self.logger.info('MEAN_REVERSION: Cutoff reached without EM touch. No trade today.')
                self._write_skip(root, 'no_em_touch_before_cutoff')
                return None

            spot = self.broker.get_spx_price() if self.broker else None
            if spot is None and self.broker:
                spot = self.broker.fetch_spx_price_api()

            if spot:
                dist_up = round(upper_em - spot, 2)
                dist_dn = round(spot - lower_em, 2)
                self._status.update({
                    'current_spx': spot,
                    'dist_to_upper_em': dist_up,
                    'dist_to_lower_em': dist_dn,
                    'monitoring': True,
                })

                # Check upper EM touch -> Bear Call Spread (fade the upside move)
                if spot >= upper_em:
                    self.logger.info(
                        'MEAN_REVERSION_TRIGGER: SPX %.2f >= Upper EM %.2f -> Entering Bear Call Spread',
                        spot, upper_em,
                    )
                    return 'C'

                # Check lower EM touch -> Bull Put Spread (fade the downside move)
                if spot <= lower_em:
                    self.logger.info(
                        'MEAN_REVERSION_TRIGGER: SPX %.2f <= Lower EM %.2f -> Entering Bull Put Spread',
                        spot, lower_em,
                    )
                    return 'P'

            time.sleep(self.config.poll_interval_sec)

        return None

    def _execute_entry(
        self,
        root: str,
        side: str,
        expiry_yymmdd: str,
        open_spx: float,
        atm_strike: int,
        straddle_price: float,
        expected_move: float,
        upper_em: float,
        lower_em: float,
    ) -> Optional[Dict[str, Any]]:
        """Select strikes, place spread order, write trade JSON, and subscribe streamer."""
        spot = self.broker.get_spx_price() if self.broker else None
        if spot is None and self.broker:
            spot = self.broker.fetch_spx_price_api()
        if spot is None:
            spot = upper_em if side == 'C' else lower_em

        # 1. Strike selection targeting ~10-25 delta (default ~18 delta)
        short_k, long_k, delta = select_credit_spread_strikes(
            side=side,
            spot=spot,
            spread_width=self.config.spread_width,
            target_delta=self.config.target_delta,
            min_delta=self.config.min_delta,
            max_delta=self.config.max_delta,
            expiry_yymmdd=expiry_yymmdd,
        )

        short_sym = build_tastytrade_symbol(expiry_yymmdd, side, short_k)
        long_sym = build_tastytrade_symbol(expiry_yymmdd, side, long_k)

        # 2. Get current quote mids
        mids = self.broker.fetch_option_mids_api([short_sym, long_sym]) if self.broker else {}
        short_mid = mids.get(short_sym) or (self.broker.get_option_price(short_sym) if self.broker else 1.50)
        long_mid = mids.get(long_sym) or (self.broker.get_option_price(long_sym) if self.broker else 0.50)
        net_credit = max(0.10, round(float(short_mid) - float(long_mid), 2))

        if net_credit < self.config.min_entry_credit:
            self.logger.warning(
                'MEAN_REVERSION_SKIPPED_THIN_CREDIT: credit=%.2f < min=%.2f (strikes %d/%d)',
                net_credit, self.config.min_entry_credit, short_k, long_k,
            )
            side_label = 'CALL_SPREAD' if side == 'C' else 'PUT_SPREAD'
            signal_label = 'UPPER_EM_BREACH' if side == 'C' else 'LOWER_EM_BREACH'
            em_boundary = upper_em if side == 'C' else lower_em
            self._record_skipped_attempt(
                root=root,
                side=side,
                signal=signal_label,
                spot=spot,
                em_level=em_boundary,
                short_k=short_k,
                long_k=long_k,
                delta=delta,
                credit=net_credit,
                min_credit=self.config.min_entry_credit,
                reason=f'Net credit (${net_credit:.2f}) < min threshold (${self.config.min_entry_credit:.2f})',
            )
            return None

        # 3. Place spread order
        legs = build_spread_legs(short_k, long_k, side, expiry_yymmdd, self.config.quantity)
        result = self._place_spread(legs, net_credit, short_sym, long_sym)

        # 4. Construct trade dictionary
        today_date = util.central_date().isoformat()
        side_label = 'CALL_SPREAD' if side == 'C' else 'PUT_SPREAD'
        signal_label = 'UPPER_EM_BREACH' if side == 'C' else 'LOWER_EM_BREACH'
        initial_stop = round(net_credit * (1.0 + self.config.stop_loss_pct), 2)
        profit_target = round(net_credit * (1.0 - self.config.profit_target_pct), 2)

        structures = [{
            'name': f'MR_{side}_{short_k}_{long_k}',
            'side': side,
            'legs': [
                {'symbol': short_sym, 'action': 'SELL_TO_OPEN', 'qty': self.config.quantity, 'fill_price': short_mid},
                {'symbol': long_sym, 'action': 'BUY_TO_OPEN', 'qty': self.config.quantity, 'fill_price': long_mid},
            ],
            'order_id': result.order_id,
            'status': result.status if result.success else 'rejected',
            'net_credit': net_credit,
            'current_mark': net_credit,
            'unrealized_pnl': 0.0,
            'unrealized_pnl_dollars': 0.0,
            'closed_manually': False,
        }]

        trade = {
            'strategy': self.config.name,
            'paper': self.config.paper,
            'date': today_date,
            'status': 'open',
            'entry_time_cst': util.central_now().strftime('%H:%M:%S'),
            'entry_time_et': datetime.now(_ET).strftime('%H:%M:%S'),
            'side': side_label,
            'opt_type': side,
            'signal': signal_label,
            'spx_at_entry': round(spot, 2),
            'open_spx': open_spx,
            'atm_strike': atm_strike,
            'straddle_price': straddle_price,
            'expected_move': expected_move,
            'upper_em': upper_em,
            'lower_em': lower_em,
            'quantity': self.config.quantity,
            'short_strike': short_k,
            'long_strike': long_k,
            'spread_width': self.config.spread_width,
            'selected_delta': delta,
            'entry_credit': net_credit,
            'current_mark': net_credit,
            'initial_stop_price': initial_stop,
            'current_stop_price': initial_stop,
            'profit_target_price': profit_target,
            'stop_at_breakeven': False,
            'highest_profit_pct': 0.0,
            'unrealized_pnl': 0.0,
            'unrealized_pnl_dollars': 0.0,
            'realized_pnl': None,
            'realized_pnl_dollars': None,
            'exit_reason': None,
            'exit_time_cst': None,
            'exit_price': None,
            'skip_reason': None,
            'stop_profile': self.config.stop_profile,
            'structures': structures,
        }

        self._write_trade_json(root, trade)
        self._save_session_state(root, entered=True, skip_reason=None)
        self._register_streamer_legs(trade)

        self._status.update({
            'entered': True,
            'side': side_label,
            'short_strike': short_k,
            'long_strike': long_k,
            'entry_credit': net_credit,
            'profit_target_price': profit_target,
            'current_stop_price': initial_stop,
        })

        self.logger.info(
            'MEAN_REVERSION_ENTERED: %s %d/%d credit=%.2f target=%.2f stop=%.2f order=%s',
            side_label, short_k, long_k, net_credit, profit_target, initial_stop, result.order_id,
        )
        return trade

    def _manage_open_trade(self, root: str, trade: Dict[str, Any]) -> None:
        """Monitor active trade for 50% profit target, 100% stop, and 25% breakeven trigger."""
        if trade.get('status') != 'open':
            return

        mhour, mminute = map(int, self.config.market_close_cst.split(':'))
        eod_cutoff = util.central_now().replace(hour=mhour, minute=mminute, second=0, microsecond=0)

        entry_credit = float(trade['entry_credit'])
        qty = int(trade.get('quantity') or 1)
        short_strike = int(trade['short_strike'])
        long_strike = int(trade['long_strike'])
        opt_type = str(trade.get('opt_type', 'C'))
        expiry_yymmdd = util.central_date().strftime('%y%m%d')

        short_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, short_strike)
        long_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, long_strike)

        self.logger.info('MEAN_REVERSION: Position management loop active')

        while self._running:
            # Check if closed manually via dashboard
            fresh_trade = self._load_open_trade(root)
            if fresh_trade and fresh_trade.get('status') == 'closed':
                self.logger.info('MEAN_REVERSION: Position closed externally / manually')
                return

            now = util.central_now()

            # EOD Cutoff reached
            if now >= eod_cutoff:
                self.logger.info('MEAN_REVERSION: End of day cutoff reached — closing position')
                self._close_position(root, trade, reason='eod_close', short_sym=short_sym, long_sym=long_sym)
                return

            # Fetch current leg quotes
            mids = self.broker.fetch_option_mids_api([short_sym, long_sym]) if self.broker else {}
            short_mid = mids.get(short_sym) or (self.broker.get_option_price(short_sym) if self.broker else None)
            long_mid = mids.get(long_sym) or (self.broker.get_option_price(long_sym) if self.broker else None)

            if short_mid is not None and long_mid is not None:
                current_mark = round(float(short_mid) - float(long_mid), 2)
                unrealized_points = round(entry_credit - current_mark, 2)
                profit_pct = round(unrealized_points / entry_credit, 4) if entry_credit > 0 else 0.0

                trade['current_mark'] = current_mark
                trade['unrealized_pnl'] = unrealized_points
                trade['unrealized_pnl_dollars'] = pnl_points_to_dollars(unrealized_points, qty)

                if profit_pct > trade.get('highest_profit_pct', 0.0):
                    trade['highest_profit_pct'] = profit_pct

                # Check 1: Breakeven / Lock-in Stop adjustment (+25% profit reached)
                if profit_pct >= self.config.breakeven_trigger_pct and not trade.get('stop_at_breakeven'):
                    trade['stop_at_breakeven'] = True
                    lock_stop = round(entry_credit * (1.0 - self.config.breakeven_lock_pct), 2)
                    trade['current_stop_price'] = lock_stop
                    self.logger.info(
                        'MEAN_REVERSION_BREAKEVEN_ADJUSTED: Profit hit %.1f%% -> Stop moved to %.2f',
                        profit_pct * 100.0, lock_stop,
                    )

                # Check 2: Profit Target Hit (50% gain)
                if current_mark <= trade['profit_target_price']:
                    self.logger.info(
                        'MEAN_REVERSION_PROFIT_TARGET_HIT: mark=%.2f <= target=%.2f',
                        current_mark, trade['profit_target_price'],
                    )
                    self._close_position(
                        root, trade, reason='profit_target_hit',
                        short_sym=short_sym, long_sym=long_sym, exit_mark=current_mark,
                    )
                    return

                # Check 3: Stop Loss Hit (100% loss or Breakeven lock)
                if current_mark >= trade['current_stop_price']:
                    stop_reason = 'breakeven_stop_hit' if trade.get('stop_at_breakeven') else 'stop_loss_hit'
                    self.logger.info(
                        'MEAN_REVERSION_STOP_HIT (%s): mark=%.2f >= stop=%.2f',
                        stop_reason, current_mark, trade['current_stop_price'],
                    )
                    self._close_position(
                        root, trade, reason=stop_reason,
                        short_sym=short_sym, long_sym=long_sym, exit_mark=current_mark,
                    )
                    return

                self._write_trade_json(root, trade)
                self._status.update({
                    'current_mark': current_mark,
                    'unrealized_pnl_dollars': trade['unrealized_pnl_dollars'],
                    'profit_pct': round(profit_pct * 100.0, 1),
                    'stop_at_breakeven': trade.get('stop_at_breakeven'),
                })

            time.sleep(self.config.poll_interval_sec)

    def _close_position(
        self,
        root: str,
        trade: Dict[str, Any],
        reason: str,
        short_sym: str,
        long_sym: str,
        exit_mark: Optional[float] = None,
    ) -> None:
        """Place closing order, record realized PnL, and move trade to history."""
        qty = int(trade.get('quantity') or 1)
        entry_credit = float(trade['entry_credit'])

        if exit_mark is None:
            mids = self.broker.fetch_option_mids_api([short_sym, long_sym]) if self.broker else {}
            sm = mids.get(short_sym) or (self.broker.get_option_price(short_sym) if self.broker else 0.5)
            lm = mids.get(long_sym) or (self.broker.get_option_price(long_sym) if self.broker else 0.1)
            exit_mark = round(float(sm) - float(lm), 2)

        # Place closing order
        if self.broker:
            try:
                self.broker.place_spread_close_order(
                    short_symbol=short_sym,
                    long_symbol=long_sym,
                    qty=qty,
                    debit_limit=exit_mark,
                )
            except Exception as exc:
                self.logger.warning('Closing spread order warning: %s', exc)

        realized_pts = round(entry_credit - exit_mark, 2)
        realized_dlr = pnl_points_to_dollars(realized_pts, qty)

        trade['status'] = 'closed'
        trade['exit_reason'] = reason
        trade['exit_time_cst'] = util.central_now().strftime('%H:%M:%S')
        trade['exit_price'] = exit_mark
        trade['realized_pnl'] = realized_pts
        trade['realized_pnl_dollars'] = realized_dlr
        trade['unrealized_pnl'] = 0.0
        trade['unrealized_pnl_dollars'] = 0.0

        for struct in trade.get('structures') or []:
            struct['status'] = 'closed'
            struct['realized_pnl'] = realized_pts
            struct['realized_pnl_dollars'] = realized_dlr

        # Move from active/ to history/
        today = trade['date']
        active_path = os.path.join(self._active_dir(root), f'mean_reversion_{today}.json')
        hist_dir = history_dir_for_strategy(self.config.name, root)
        os.makedirs(hist_dir, exist_ok=True)
        hist_path = os.path.join(hist_dir, f'mean_reversion_{today}.json')

        tmp = f'{hist_path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)
            f.flush()
        os.replace(tmp, hist_path)

        if os.path.isfile(active_path):
            try:
                os.remove(active_path)
            except OSError:
                pass

        self._save_session_state(root, entered=True, closed=True, exit_reason=reason, realized_pnl=realized_pts)
        self.logger.info(
            'MEAN_REVERSION_CLOSED: reason=%s exit_mark=%.2f realized_pnl=%.2f ($%.2f)',
            reason, exit_mark, realized_pts, realized_dlr,
        )

    def _place_spread(self, legs, credit: float, short_sym: str, long_sym: str):
        """Place credit spread order with journal paper safety check."""
        if self.config.paper and not (
            getattr(self.broker, '_is_journal', False)
            or getattr(self.broker, '_is_paper', None) is True
        ):
            raise RuntimeError('Paper strategy attempted live broker order')

        # Try place_spread_order first, then place_multi_leg_order
        if hasattr(self.broker, 'place_spread_order'):
            result = self.broker.place_spread_order(
                short_symbol=short_sym,
                long_symbol=long_sym,
                qty=self.config.quantity,
                credit=credit,
            )
        else:
            result = self.broker.place_multi_leg_order(legs, price=credit)

        if result.order_id and result.status not in ('filled', 'rejected', 'cancelled'):
            deadline = time.monotonic() + self.config.entry_fill_wait_sec
            while time.monotonic() < deadline and self._running:
                status = self.broker.get_order_status(result.order_id)
                if status.status in ('filled', 'cancelled', 'rejected'):
                    result = status
                    break
                time.sleep(1)
            if result.status not in ('filled', 'cancelled', 'rejected'):
                self.broker.cancel_order(result.order_id)
                result = self.broker.get_order_status(result.order_id)
        return result

    def _active_dir(self, root: str) -> str:
        path = active_dir_for_strategy(self.config.name, root)
        os.makedirs(path, exist_ok=True)
        return path

    def _load_open_trade(self, root: str) -> Optional[Dict[str, Any]]:
        today = util.central_date().isoformat()
        path = os.path.join(self._active_dir(root), f'mean_reversion_{today}.json')
        if not os.path.isfile(path):
            return None
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _write_trade_json(self, root: str, trade: dict) -> None:
        path = os.path.join(self._active_dir(root), f'mean_reversion_{trade["date"]}.json')
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)
            f.flush()
        os.replace(tmp, path)

    def _load_session_state(self, root: str) -> Dict[str, Any]:
        today = util.central_date().isoformat()
        path = os.path.join(self._active_dir(root), f'session_{today}.json')
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_session_state(self, root: str, **fields) -> None:
        today = util.central_date().isoformat()
        path = os.path.join(self._active_dir(root), f'session_{today}.json')
        state = self._load_session_state(root)
        state.update(fields)
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.flush()
        os.replace(tmp, path)

    def _write_skip(self, root: str, reason: str) -> None:
        self._save_session_state(root, entered=False, skip_reason=reason)
        self._status.update({'entered': False, 'skip_reason': reason})

    def _record_skipped_attempt(
        self,
        root: str,
        side: str,
        signal: str,
        spot: float,
        em_level: float,
        short_k: int,
        long_k: int,
        delta: float,
        credit: float,
        min_credit: float,
        reason: str,
    ) -> None:
        state = self._load_session_state(root)
        skipped = list(state.get('skipped_attempts') or [])
        attempt = {
            'time_cst': util.central_now().strftime('%H:%M:%S'),
            'time_et': datetime.now(_ET).strftime('%H:%M:%S'),
            'side': 'CALL_SPREAD' if side == 'C' else 'PUT_SPREAD',
            'signal': signal,
            'spx': round(spot, 2),
            'em_level': round(em_level, 2),
            'strikes': f'{short_k}/{long_k}',
            'delta': round(delta, 2),
            'credit': round(credit, 2),
            'min_credit': round(min_credit, 2),
            'reason': reason,
        }
        skipped.append(attempt)
        self._save_session_state(
            root,
            entered=False,
            skip_reason='thin_credit',
            skipped_attempts=skipped,
            last_skipped_attempt=attempt,
        )
        self._status.update({
            'entered': False,
            'skip_reason': 'thin_credit',
            'skipped_attempts': skipped,
            'last_skipped_attempt': attempt,
        })

    def _is_disabled_today(self, root: str) -> bool:
        from common.daily_overrides import is_disabled_for_today
        return is_disabled_for_today(self.config, self.config.name, root)

    def _check_trading_day(self) -> tuple[bool, str]:
        import run as launcher
        return launcher.check_trading_day()

    def _register_streamer_legs(self, trade: dict) -> None:
        from common.streamer_symbols import register_option_symbols

        symbols: List[str] = []
        for struct in trade.get('structures') or []:
            if struct.get('status') in ('cancelled', 'rejected'):
                continue
            for leg in struct.get('legs') or []:
                sym = leg.get('symbol')
                if sym:
                    symbols.append(str(sym))
        if symbols:
            register_option_symbols(symbols, self.config.name, self.logger)

    @staticmethod
    def register_open_trade_streamer_legs(trade: dict, logger=None) -> None:
        from common.streamer_symbols import register_option_symbols

        symbols: List[str] = []
        for struct in trade.get('structures') or []:
            if struct.get('status') in ('cancelled', 'rejected', 'closed'):
                continue
            for leg in struct.get('legs') or []:
                sym = leg.get('symbol')
                if sym:
                    symbols.append(str(sym))
        if symbols:
            lot = str(trade.get('strategy') or 'MEAN_REVERSION_SPX')
            register_option_symbols(symbols, lot, logger)

    def stop(self) -> None:
        self._running = False
        self._status['running'] = False

    def get_status(self) -> Dict[str, Any]:
        return dict(self._status)
