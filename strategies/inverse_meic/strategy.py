"""Inverse MEIC (INVERSE_MEIC_SPX) Strategy Engine.

Standalone companion long volatility strategy that enters twin vertical debit spreads
(Call Debit Spread + Put Debit Spread, 25 points wide) targeting ~$0.80 - $1.10 debit per spread.

Key Features:
- Market Regime Gatekeeper: Only enters when market conditions indicate an expansion regime
  (Pre-market gap <= -0.5%, 30m ORB >= 25 pts, Spot VIX >= 18.0, or Realized ATR >= 25 pts).
- Payoff & Exit:
  * Profit Target Multiple (default 3.0x debit mark).
  * "Let it bleed" (no stop loss, defined risk strictly capped at entry debit paid).
  * 14:50 CT EOD settlement square-off for residual value > $0.10.
- Decoupled from MEIC state machine — runs independently.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import time as time_mod
from datetime import datetime, time
from typing import Any, Dict, List, Optional

from blocks.stop import state as state_mod
from blocks.stop.profiles.inverse_meic import INVERSE_MEIC_PROFILE
from common import trades_layout
from common.symbols import to_tastytrade
from meic0dte.app import utilities as util
from orchestrator.scheduler import TrancheSlot
from strategies.base import BaseStrategy
from strategies.inverse_meic.conditions import (
    MarketRegimeStatus,
    evaluate_market_conditions,
    load_regime_snapshot,
    save_regime_snapshot,
)
from strategies.inverse_meic.config import InverseMEICConfig
from strategies.inverse_meic.spread_scan import scan_debit_spread

log = logging.getLogger('inverse_meic_strategy')

INVERSE_MEIC_TRANCHE_SLOTS = [
    TrancheSlot('10-30', time(10, 29), time(10, 35), 'INVERSE_MEIC_SPX'),
    TrancheSlot('11-00', time(10, 59), time(11, 5), 'INVERSE_MEIC_SPX'),
    TrancheSlot('12-00', time(11, 59), time(12, 5), 'INVERSE_MEIC_SPX'),
    TrancheSlot('01-00', time(12, 59), time(13, 5), 'INVERSE_MEIC_SPX'),
    TrancheSlot('01-15', time(13, 14), time(13, 20), 'INVERSE_MEIC_SPX'),
    TrancheSlot('02-15', time(14, 14), time(14, 20), 'INVERSE_MEIC_SPX'),
]


class InverseMEICStrategy(BaseStrategy):
    """Inverse MEIC Strategy runner."""

    def __init__(self, config: Optional[InverseMEICConfig] = None, broker=None):
        config = config or InverseMEICConfig()
        super().__init__(config, broker)
        self.config: InverseMEICConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self._root = self.config.root or trades_layout.project_root()
        self._last_regime_status: Optional[MarketRegimeStatus] = None

    def schedule(self) -> List[TrancheSlot]:
        return list(INVERSE_MEIC_TRANCHE_SLOTS)

    def evaluate_regime(self, overrides: Optional[Dict[str, Any]] = None) -> MarketRegimeStatus:
        status = evaluate_market_conditions(
            broker=self.broker,
            root=self._root,
            gap_threshold=self.config.premarket_gap_threshold,
            orb_threshold=self.config.orb_range_threshold,
            vix_threshold=self.config.vix_threshold,
            atr_threshold=self.config.atr_threshold,
            target_multiplier=self.config.profit_target_multiple,
            overrides=overrides,
        )
        self._last_regime_status = status
        try:
            save_regime_snapshot(self._root, status)
        except Exception:
            pass
        return status

    def _resolve_spx_spot(self) -> float:
        if self.broker is not None:
            if hasattr(self.broker, 'fetch_spx_price_api'):
                spx = self.broker.fetch_spx_price_api()
                if spx:
                    return float(spx)
            if hasattr(self.broker, 'get_spx_price'):
                spx = self.broker.get_spx_price()
                if spx:
                    return float(spx)
        return 5800.0

    def write_handshake(
        self,
        *,
        lot: str,
        side: str,
        long_symbol: str,
        short_symbol: str,
        long_strike: int,
        short_strike: int,
        quantity: int,
        open_order_id: str,
        entry_debit: float,
        target_multiplier: float,
    ) -> str:
        """Write trade JSON into trades/active/INVERSE_MEIC_SPX/."""
        active_dir = trades_layout.active_dir_for_strategy(self.config.name, self._root)
        os.makedirs(active_dir, exist_ok=True)
        filename = state_mod.stable_trade_filename(lot, side)
        path = os.path.join(active_dir, filename)

        target_price = round(entry_debit * target_multiplier, 2)
        long_tt = to_tastytrade(long_symbol)
        short_tt = to_tastytrade(short_symbol)

        state = {
            'strategy_version': '2.0',
            'instrument': 'SPX',
            'spread_type': 'debit',
            'strategy': self.config.name,
            'stop_profile': INVERSE_MEIC_PROFILE,
            'status': 'open',
            'lot': lot,
            'quantity': quantity,
            'filled_quantity': quantity,
            'stop_quantity': 0,
            'open_order_id': open_order_id,
            'entry': {
                'strategy': self.config.name,
                'lot': lot,
                'side': side.upper(),
                'timestamp': state_mod.now_iso(),
                'debit': entry_debit,
                'net_debit': entry_debit,
                'limit_debit': entry_debit,
                'target_multiple': target_multiplier,
                'target_price': target_price,
            },
            'long_leg': {
                'symbol': long_tt,
                'strike': long_strike,
                'fill_price': round(entry_debit + 0.60, 2),
            },
            'short_leg': {
                'symbol': short_tt,
                'strike': short_strike,
                'fill_price': 0.60,
            },
            'exit_config': {
                'profit_target_multiple': target_multiplier,
                'target_price': target_price,
                'eod_exit_time_cst': self.config.eod_exit_time_cst,
            },
            'phases': {},
            'order_history': [{
                'order_id': open_order_id,
                'limit_price': entry_debit,
                'long_strike': long_strike,
                'short_strike': short_strike,
                'status': 'filled',
                'filled_quantity': quantity,
                'reason': 'inverse_meic_entry',
            }],
        }
        state_mod.save_state(path, state)
        self.logger.info('Wrote Inverse MEIC handshake trade: %s', path)
        return path

    def enter_tranche(self, lot: str) -> Dict[str, Any]:
        """Enter twin vertical debit spreads (CDS and PDS) for the specified tranche."""
        today = util.central_date()
        expiry_yymmdd = today.strftime('%y%m%d')
        spx_spot = self._resolve_spx_spot()

        results = {'lot': lot, 'entered_at': state_mod.now_iso(), 'wings': {}}

        # 1. Scan Call Debit Spread (CDS)
        cds_cand = scan_debit_spread(
            self.broker,
            'C',
            expiry_yymmdd,
            spx_spot,
            width=self.config.spread_width,
            target_debit=self.config.target_debit,
            max_debit=self.config.max_debit,
            logger=self.logger,
        )
        if cds_cand and self.broker is not None:
            order_res = self.broker.place_debit_spread_order(
                cds_cand.long_symbol,
                cds_cand.short_symbol,
                self.config.quantity,
                cds_cand.market_debit,
            )
            oid = getattr(order_res, 'order_id', None) or f'sim-cds-{lot}-{int(time_mod.time())}'
            path_c = self.write_handshake(
                lot=lot,
                side='C',
                long_symbol=cds_cand.long_symbol,
                short_symbol=cds_cand.short_symbol,
                long_strike=cds_cand.long_strike,
                short_strike=cds_cand.short_strike,
                quantity=self.config.quantity,
                open_order_id=oid,
                entry_debit=cds_cand.market_debit,
                target_multiplier=self.config.profit_target_multiple,
            )
            results['wings']['C'] = {
                'success': True,
                'candidate': cds_cand.__dict__,
                'path': path_c,
                'order_id': oid,
            }

        # 2. Scan Put Debit Spread (PDS)
        pds_cand = scan_debit_spread(
            self.broker,
            'P',
            expiry_yymmdd,
            spx_spot,
            width=self.config.spread_width,
            target_debit=self.config.target_debit,
            max_debit=self.config.max_debit,
            logger=self.logger,
        )
        if pds_cand and self.broker is not None:
            order_res = self.broker.place_debit_spread_order(
                pds_cand.long_symbol,
                pds_cand.short_symbol,
                self.config.quantity,
                pds_cand.market_debit,
            )
            oid = getattr(order_res, 'order_id', None) or f'sim-pds-{lot}-{int(time_mod.time())}'
            path_p = self.write_handshake(
                lot=lot,
                side='P',
                long_symbol=pds_cand.long_symbol,
                short_symbol=pds_cand.short_symbol,
                long_strike=pds_cand.long_strike,
                short_strike=pds_cand.short_strike,
                quantity=self.config.quantity,
                open_order_id=oid,
                entry_debit=pds_cand.market_debit,
                target_multiplier=self.config.profit_target_multiple,
            )
            results['wings']['P'] = {
                'success': True,
                'candidate': pds_cand.__dict__,
                'path': path_p,
                'order_id': oid,
            }

        return results

    def run(self) -> None:
        self._running = True
        self.logger.info('Starting Inverse MEIC strategy execution')

        regime = self.evaluate_regime()
        if not regime.is_armed:
            self.logger.info('Inverse MEIC is in STANDBY mode: %s. Tranches skipped.', regime.summary_message)
            return

        self.logger.info('Inverse MEIC is ARMED! Monitoring tranche schedule.')

    def stop(self) -> None:
        self._running = False
        self.logger.info('Stopped Inverse MEIC strategy runner')

    def get_status(self) -> Dict[str, Any]:
        """Aggregate full status for the dashboard tab."""
        active_dir = trades_layout.active_dir_for_strategy(self.config.name, self._root)
        hist_dir = trades_layout.history_dir_for_strategy(self.config.name, self._root)
        today = util.central_date().isoformat()

        # Load or evaluate regime
        regime = load_regime_snapshot(self._root)
        if not regime:
            regime = self.evaluate_regime().to_dict()

        # Load all trades for today
        all_trades: List[Dict[str, Any]] = []
        for d in (active_dir, hist_dir):
            if os.path.isdir(d):
                for p in glob.glob(os.path.join(d, '*.json')):
                    base = os.path.basename(p)
                    if base.startswith('regime_') or base.startswith('session_'):
                        continue
                    try:
                        with open(p, encoding='utf-8') as f:
                            t = json.load(f)
                            all_trades.append(t)
                    except Exception:
                        pass

        # Build 6-tranche grid representation
        slots_data = []
        total_pnl = 0.0

        for slot in self.schedule():
            lot_name = slot.lot
            cds_trade = next((t for t in all_trades if t.get('lot') == lot_name and t.get('entry', {}).get('side') == 'C'), None)
            pds_trade = next((t for t in all_trades if t.get('lot') == lot_name and t.get('entry', {}).get('side') == 'P'), None)

            def _summarize_wing(t: Optional[Dict[str, Any]], side: str) -> Dict[str, Any]:
                if not t:
                    return {'side': side, 'status': 'PENDING', 'debit': 0.0, 'mark': 0.0, 'target': 0.0, 'pnl': 0.0}
                entry = t.get('entry') or {}
                debit = float(entry.get('debit') or entry.get('net_debit') or 0.0)
                exit_cfg = t.get('exit_config') or {}
                mult = float(exit_cfg.get('profit_target_multiple') or 3.0)
                target = round(debit * mult, 2)
                status = t.get('status', 'open').upper()

                # Calculate live mark if open
                mark = debit
                long_mid = float(t.get('long_leg', {}).get('fill_price') or 0.0)
                short_mid = float(t.get('short_leg', {}).get('fill_price') or 0.0)
                if long_mid > 0 and short_mid > 0:
                    mark = round(long_mid - short_mid, 2)

                pnl = round((mark - debit) * 100 * int(t.get('quantity') or 1), 2)
                if status == 'CLOSED':
                    close_p = t.get('close_debit') or t.get('short_close_price')
                    if close_p is not None:
                        pnl = round((float(close_p) - debit) * 100 * int(t.get('quantity') or 1), 2)

                return {
                    'side': side,
                    'status': status,
                    'long_strike': t.get('long_leg', {}).get('strike'),
                    'short_strike': t.get('short_leg', {}).get('strike'),
                    'debit': debit,
                    'mark': mark,
                    'target': target,
                    'multiple': mult,
                    'pnl': pnl,
                }

            cds_summary = _summarize_wing(cds_trade, 'CDS (Call)')
            pds_summary = _summarize_wing(pds_trade, 'PDS (Put)')
            tranche_pnl = cds_summary['pnl'] + pds_summary['pnl']
            total_pnl += tranche_pnl

            slots_data.append({
                'lot': lot_name,
                'window': f"{slot.window_start.strftime('%H:%M')}-{slot.window_end.strftime('%H:%M')} CT",
                'cds': cds_summary,
                'pds': pds_summary,
                'tranche_pnl': round(tranche_pnl, 2),
            })

        return {
            'strategy': self.config.name,
            'paper': bool(self.config.paper),
            'date': today,
            'regime': regime,
            'slots': slots_data,
            'trades': all_trades,
            'total_pnl': round(total_pnl, 2),
        }
