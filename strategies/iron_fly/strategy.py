"""SPX 9 Iron Fly strategy — ported from spx-bot-main."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from strategies.base import BaseStrategy, StrategyConfig


@dataclass
class IronFlyConfig(StrategyConfig):
    name: str = 'Iron_Fly_SPX'
    entry_hour: int = 8
    entry_minute: int = 33
    n_above: int = 4
    n_below: int = 4
    step: int = 5
    width: int = 60
    per_if_stop: float = 4.0
    portfolio_stop: float = 40.0
    tz_name: str = 'America/Chicago'
    phase_names: List[str] = field(default_factory=lambda: [
        'PortfolioStopPhase',
        'PerIFStopPhase',
    ])


def _round_nickel(val: float) -> float:
    d = Decimal(str(val))
    nickel = Decimal('0.05')
    return float((d / nickel).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * nickel)


class IronFlyStrategy(BaseStrategy):
    """Simplified Iron Fly using broker abstraction."""

    def __init__(self, config: IronFlyConfig = None, broker=None):
        config = config or IronFlyConfig()
        super().__init__(config, broker)
        self.config: IronFlyConfig = config
        self.logger = logging.getLogger(self.config.name)
        self._running = False
        self.active_bodies: Dict[float, dict] = {}
        self.realized_pnl = 0.0

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.tz_name))

    def run(self) -> None:
        self._running = True
        if self.broker is None:
            from common.broker_factory import get_broker
            self.broker = get_broker(paper=self.config.paper)
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        from tastytrade.instruments import get_option_chain

        session = self.broker.session
        self.logger.info('Iron Fly strategy starting')

        while self._running:
            if not self._entry_window_open():
                await asyncio.sleep(30)
                continue

            chain = await get_option_chain(session, self.config.ticker)
            today = self.now().date()
            expiry = next((e for e in chain if e == today), None)
            if not expiry:
                self.logger.warning('No 0DTE expiry found')
                await asyncio.sleep(60)
                continue

            atm = self.broker.get_spx_price()
            if atm is None:
                await asyncio.sleep(10)
                continue
            atm_body = round(atm / 5) * 5
            bodies = self._ladder_bodies(atm_body)

            for body in bodies:
                if not self._running:
                    break
                ok = await self._open_if(chain[expiry], body)
                if ok:
                    self.active_bodies[body] = {'open': True, 'entry_credit': ok}

            await self._monitor_loop(chain[expiry])
            break

    def _ladder_bodies(self, atm: float) -> List[float]:
        bodies = [atm]
        for i in range(1, self.config.n_below + 1):
            bodies.append(atm - i * self.config.step)
        for i in range(1, self.config.n_above + 1):
            bodies.append(atm + i * self.config.step)
        return sorted(bodies)

    def _entry_window_open(self) -> bool:
        target = time(self.config.entry_hour, self.config.entry_minute)
        return self.now().time() >= target and not self.active_bodies

    async def _open_if(self, options, body: float) -> Optional[float]:
        """Open iron fly at body; returns entry credit if successful."""
        self.logger.info('Would open IF at body %s (integrate NewOrder via broker)', body)
        # Full 4-leg order construction requires option chain objects;
        # extend TastyTradeBroker.place_iron_fly() for production use.
        return None

    async def _monitor_loop(self, options) -> None:
        while self._running and self.active_bodies:
            total_pnl = self.realized_pnl
            for body, fly in list(self.active_bodies.items()):
                if not fly.get('open'):
                    continue
                # PnL monitoring via broker streaming — extend as needed
                pass
            if total_pnl <= -self.config.portfolio_stop:
                self.logger.info('Portfolio stop hit')
                break
            await asyncio.sleep(2)

    def stop(self) -> None:
        self._running = False
        self.logger.info('Iron Fly stopped')

    def get_status(self) -> Dict[str, Any]:
        return {
            'name': self.config.name,
            'running': self._running,
            'active_positions': len(self.active_bodies),
            'realized_pnl': self.realized_pnl,
        }
