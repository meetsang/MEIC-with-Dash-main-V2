"""DXLink option Greeks fetch (delta) for chain snapshot."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, List

from common.symbols import to_tastytrade

log = logging.getLogger(__name__)

GREEKS_TIMEOUT_SEC = float(os.environ.get('CHAIN_GREEKS_TIMEOUT_SEC', '15'))
GREEKS_POLL_SEC = float(os.environ.get('CHAIN_GREEKS_POLL_SEC', '0.3'))


async def fetch_option_deltas(session, streamer_symbols: List[str]) -> Dict[str, float]:
    """Return {streamer_symbol: delta} from DXLink Greeks feed."""
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Greeks

    unique = list(dict.fromkeys(s for s in streamer_symbols if s))
    if not unique:
        return {}

    result: Dict[str, float] = {}
    target = set(unique)
    deadline = time.time() + GREEKS_TIMEOUT_SEC

    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, unique, refresh_interval=1.0)
            while time.time() < deadline and len(result) < len(target):
                try:
                    event = await asyncio.wait_for(
                        streamer.get_event(Greeks), timeout=GREEKS_POLL_SEC,
                    )
                except asyncio.TimeoutError:
                    continue
                sym = getattr(event, 'event_symbol', '') or ''
                delta = getattr(event, 'delta', None)
                if sym and delta is not None:
                    result[sym] = float(delta)
    except Exception as exc:
        log.warning('Option delta stream failed: %s', exc)

    log.info('Greeks delta received for %d/%d symbols', len(result), len(unique))
    return result


def fetch_option_deltas_sync(broker, symbols: List[str]) -> Dict[str, float]:
    """Sync wrapper using broker session + _run."""
    streamer_syms = list(dict.fromkeys(to_tastytrade(s) for s in symbols if s))
    if not streamer_syms:
        return {}
    return broker._run(fetch_option_deltas(broker.session, streamer_syms))
