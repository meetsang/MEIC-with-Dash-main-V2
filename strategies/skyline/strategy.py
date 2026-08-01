"""Skyline multi-butterfly 0DTE SPX strategy."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from strategies.base import BaseStrategy, StrategyConfig
from strategies.skyline.legs import ButterflyConfig, build_butterfly_legs, round_atm

_ET = ZoneInfo('America/New_York')

_DEFAULT_BUTTERFLIES = [
    ButterflyConfig('BF1', 'call', 0, 25, 50),
    ButterflyConfig('BF2', 'call', 40, 60, 75),
    ButterflyConfig('BF3', 'call', 5, -20, -45),
    ButterflyConfig('BF4', 'put', -35, -60, -80),
]


@dataclass
class SkylineConfig(StrategyConfig):
    name: str = 'SKYLINE_SPX'
    scheduled: bool = False
    paper: bool = True  # default paper; set paper: false in yaml for live path
    confirmed: bool = False
    requires_confirmation: bool = True
    stop_profile: str = 'skyline_hold_to_expiry'
    entry_time_et: str = '09:45'
    gap_down_skip_points: int = 25
    atm_rounding: int = 5
    quantity: int = 1
    butterflies: List[ButterflyConfig] = field(default_factory=lambda: list(_DEFAULT_BUTTERFLIES))
    max_net_debit_per_structure: float = 999999.0
    entry_fill_wait_sec: int = 20
    entry_price_style: str = 'mid'
    max_daily_loss: Optional[float] = None
    root: str = ''

    def to_dict(self) -> Dict[str, Any]:
        d = super().to_dict()
        d['butterflies'] = [
            {
                'name': b.name,
                'type': b.type,
                'wing1': b.wing1,
                'body': b.body,
                'wing2': b.wing2,
            }
            for b in self.butterflies
        ]
        return d


class SkylineStrategy(BaseStrategy):
    """Four-butterfly 0DTE SPX strategy — hold to expiration."""

    def __init__(self, config: SkylineConfig = None, broker=None):
        config = config or SkylineConfig()
        super().__init__(config, broker)
        self.config: SkylineConfig = config
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
            self._wait_for_entry_window()
            if not self._running:
                return

            allowed, reason = self._check_trading_day()
            if not allowed:
                self._write_skip(root, reason)
                return

            spot = self.broker.get_spx_price() if self.broker else None
            if spot is None and self.broker:
                spot = self.broker.fetch_spx_price_api()
            if spot is None:
                self._write_skip(root, 'no_spot_price')
                return

            prior = self._prior_close(root)
            if prior is None:
                self.logger.warning('SKYLINE_SKIPPED_NO_PRIOR_CLOSE')
                self._write_skip(root, 'no_prior_close')
                return

            if spot - prior < -self.config.gap_down_skip_points:
                self.logger.info('SKYLINE_SKIPPED_GAP_DOWN spot=%.2f prior=%.2f', spot, prior)
                self._write_skip(root, 'gap_down')
                return

            atm = round_atm(spot, self.config.atm_rounding)
            today = datetime.now(_ET).date()
            expiry_yymmdd = today.strftime('%y%m%d')

            structures = []
            for bf in self.config.butterflies:
                legs = build_butterfly_legs(atm, expiry_yymmdd, bf, self.config.quantity)
                result = self._place_butterfly(legs)
                structures.append({
                    'name': bf.name,
                    'legs': [
                        {'symbol': leg.symbol, 'action': leg.action, 'qty': leg.quantity, 'fill_price': None}
                        for leg in legs
                    ],
                    'order_id': result.order_id,
                    'status': result.status if result.success else 'rejected',
                    'net_debit': abs(result.filled_price) if result.filled_price else None,
                    'unrealized_pnl': None,
                    'closed_manually': False,
                })
                if result.status != 'filled':
                    self.logger.warning('SKYLINE_LEG_UNFILLED %s status=%s', bf.name, result.status)

            trade = {
                'strategy': self.config.name,
                'paper': self.config.paper,
                'date': today.isoformat(),
                'status': 'open',
                'entry_time_et': datetime.now(_ET).strftime('%H:%M:%S'),
                'atm': atm,
                'quantity': self.config.quantity,
                'skip_reason': None,
                'stop_profile': self.config.stop_profile,
                'structures': structures,
                'net_debit_total': sum(
                    s.get('net_debit') or 0 for s in structures
                ),
                'capital_at_risk': 0.0,
                'unrealized_pnl_total': 0.0,
                'settlement': None,
            }
            self._write_trade_json(root, trade)
            self._save_session_state(root, entered=True, skip_reason=None)
            self._register_streamer_legs(trade)
            self._status.update({'entered': True, 'atm': atm, 'structures': len(structures)})
            self.logger.info('Skyline entry complete: %d structures at ATM=%d', len(structures), atm)

        except Exception:
            self.logger.exception('Skyline strategy run failed')
            raise
        finally:
            self._running = False
            self._status['running'] = False

    def _wait_for_entry_window(self) -> None:
        hour, minute = map(int, self.config.entry_time_et.split(':'))
        target = datetime.now(_ET).replace(hour=hour, minute=minute, second=0, microsecond=0)
        while self._running:
            now = datetime.now(_ET)
            if now >= target:
                return
            time.sleep(min(30, max(1, (target - now).total_seconds())))

    def _check_trading_day(self) -> tuple[bool, str]:
        import run as launcher
        return launcher.check_trading_day()

    def _prior_close(self, root: str) -> Optional[float]:
        from common import config as common_config
        from common.expiry_settlement import resolve_daily_close

        today = datetime.now(_ET).date()
        d = today - timedelta(days=1)
        holidays = set(common_config._get_nyse_holidays(today.year))
        holidays |= set(common_config._get_nyse_holidays((today - timedelta(days=365)).year))
        for _ in range(10):
            key = d.strftime('%y%m%d')
            if d.weekday() < 5 and key not in holidays:
                result = resolve_daily_close(root, d)
                if result:
                    return result[0]
            d -= timedelta(days=1)
        return None

    def _place_butterfly(self, legs):
        from brokers.base import ComboLeg
        assert isinstance(legs[0], ComboLeg)
        if self.config.paper and not (
            getattr(self.broker, '_is_journal', False)
            or getattr(self.broker, '_is_paper', None) is True
        ):
            raise RuntimeError('paper strategy attempted live broker order')

        symbols = [leg.symbol for leg in legs]
        mids = self.broker.fetch_option_mids_api(symbols)
        net_debit = 0.0
        for leg in legs:
            mid = mids.get(leg.symbol)
            if mid is None:
                mid = self.broker.get_option_price(leg.symbol) or 0.5
            if leg.action.startswith('BUY'):
                net_debit += mid * leg.quantity
            else:
                net_debit -= mid * leg.quantity
        net_debit = max(0.05, round(net_debit, 2))
        if net_debit > self.config.max_net_debit_per_structure:
            self.logger.warning(
                'SKYLINE_DEBIT_CEILING net_debit=%.2f ceiling=%.2f — skipping',
                net_debit, self.config.max_net_debit_per_structure,
            )
            from brokers.base import OrderResult
            return OrderResult(False, None, 'rejected', message='debit_ceiling_exceeded')

        price = -net_debit  # negative = debit for TastyTrade SDK
        result = self.broker.place_multi_leg_order(legs, price=price)
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
        from common.trades_layout import active_dir_for_strategy
        path = active_dir_for_strategy(self.config.name, root)
        os.makedirs(path, exist_ok=True)
        return path

    def _register_streamer_legs(self, trade: dict) -> None:
        """Subscribe streamer to open leg symbols so options_quotes.csv records mids."""
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
        """Re-register legs after launcher restart (optsymbols may have been cleared)."""
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
            lot = str(trade.get('strategy') or 'SKYLINE_SPX')
            register_option_symbols(symbols, lot, logger)

    def _write_trade_json(self, root: str, trade: dict) -> None:
        path = os.path.join(self._active_dir(root), f'skyline_{trade["date"]}.json')
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)
            f.flush()
        os.replace(tmp, path)

    def _save_session_state(self, root: str, **fields) -> None:
        today = date.today().isoformat()
        path = os.path.join(self._active_dir(root), f'session_{today}.json')
        state = {}
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
