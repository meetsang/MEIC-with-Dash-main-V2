"""TastyTrade DXLinkStreamer -> MQTT publisher (parallel to publish.py for Schwab)."""
import os
import sys

_dir = os.path.abspath(os.path.dirname(__file__))
_root = os.path.dirname(_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

import asyncio
import json
import logging
import threading
import time
from datetime import datetime as dt, timezone

import paho.mqtt.publish as publish

from common.broker_factory import get_mqtt_topic_prefix
from common.session_logs import STREAM_TT_BASE, new_session_log_path, relocate_legacy_log
from common.streamer_health import write_health
from common.symbols import to_tastytrade
from common.tt_auth import create_tastytrade_session
from meic0dte.app.utilities import central_now, crossed_market_close
from streaming import config

OPTION_SYMBOL_FILE = config.STREAM_SYMBOLS
TOPIC_PREFIX = get_mqtt_topic_prefix() or 'TASTYTRADE/'

# Index/equity symbols for market_data recorder (MQTT topic = canonical name).
MARKET_WATCH_SYMBOLS = ('SPX', 'VIX', 'VXN', 'QQQ', 'IWM')
_TOPIC_ALIASES = {
    'SPX': 'SPX', '$SPX': 'SPX', '.$SPX': 'SPX',
    'VIX': 'VIX', '$VIX': 'VIX', '.$VIX': 'VIX',
    'VXN': 'VXN', '$VXN': 'VXN', '.$VXN': 'VXN',
    'QQQ': 'QQQ', 'IWM': 'IWM',
}


def _mqtt_symbol(event_symbol: str) -> str:
    sym = (event_symbol or '').strip()
    return _TOPIC_ALIASES.get(sym, sym)


def _get_logger():
    log = logging.getLogger('stream_pub_tt')
    if not log.handlers:
        relocate_legacy_log(_root, STREAM_TT_BASE)
        log_path = str(
            new_session_log_path(_root, STREAM_TT_BASE, when=central_now())
        )
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [TT-STREAM] %(message)s',
            handlers=[
                logging.FileHandler(log_path, mode='w'),
            ],
        )
        log.info('Streamer log: %s', log_path)
    return log


def _get_symbols(slog):
    while True:
        try:
            with open(OPTION_SYMBOL_FILE, 'r') as f:
                data = json.load(f)
                syms = set(data.get('SYMBOLS', []))
                # Convert to TastyTrade format
                return {to_tastytrade(s) if not s.startswith('.') and s != 'SPX' else s
                        for s in syms}
        except Exception as e:
            slog.info('ERROR reading symbol file: %s', e)
            time.sleep(5)


async def _stream_loop(session, slog):
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Trade

    session_started = central_now()
    initial = _get_symbols(slog)
    # Always include index/equity watchlist for market_data recorder
    subscribe_set = set(initial) | set(MARKET_WATCH_SYMBOLS)
    slog.info('Initial symbols: %s', subscribe_set)

    try:
        publish.single(
            config.KILL_SWITCH_TOPIC.replace('SCHWAB/', TOPIC_PREFIX),
            'False',
            retain=True,
            hostname=config.MQTT_BROKER_ADDR,
        )
    except Exception as e:
        slog.info('Kill switch publish error: %s', e)

    last_mids: dict[str, float] = {}
    last_spx_price_ts: str | None = None

    def _mqtt_publish(symbol: str, mid: float) -> None:
        nonlocal last_spx_price_ts
        symbol = _mqtt_symbol(symbol)
        last_mids[symbol] = mid
        if symbol == 'SPX':
            last_spx_price_ts = dt.now(timezone.utc).astimezone().isoformat(timespec='seconds')
        try:
            publish.single(
                TOPIC_PREFIX + symbol,
                str(mid),
                retain=True,
                hostname=config.MQTT_BROKER_ADDR,
            )
        except Exception as e:
            slog.info('MQTT publish error %s: %s', symbol, e)

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, list(subscribe_set))
        await streamer.subscribe(Trade, ['SPX'])

        active = set(subscribe_set)

        async def _handle_quotes():
            async for quote in streamer.listen(Quote):
                if quote.bid_price is None and quote.ask_price is None:
                    continue
                bid = float(quote.bid_price or 0)
                ask = float(quote.ask_price or 0)
                if bid <= 0 and ask <= 0:
                    continue
                mid = (bid + ask) / 2 if bid and ask else (bid or ask)
                topic_name = _mqtt_symbol(quote.event_symbol)
                _mqtt_publish(topic_name, mid)

        async def _handle_trades():
            async for trade in streamer.listen(Trade):
                if trade.event_symbol not in ('SPX', '$SPX'):
                    continue
                price = float(trade.price or 0)
                if price > 0:
                    _mqtt_publish('SPX', price)

        quote_task = asyncio.create_task(_handle_quotes())
        trade_task = asyncio.create_task(_handle_trades())

        tick = 0
        while True:
            if not os.environ.get('MEIC_INTEGRATION', '').lower() in ('1', 'true', 'yes'):
                now = central_now()
                if crossed_market_close(session_started, now):
                    slog.info('3:00 PM CT — stopping stream')
                    quote_task.cancel()
                    trade_task.cancel()
                    return

            tick += 1
            if tick % 5 == 0:
                write_health(
                    last_spx_price_ts=last_spx_price_ts,
                    symbols_subscribed=len(active),
                    status='live' if last_spx_price_ts else 'waiting',
                )
            if tick % 5 == 0 and last_mids:
                for sym, mid in list(last_mids.items()):
                    _mqtt_publish(sym, mid)

            current = _get_symbols(slog) | set(MARKET_WATCH_SYMBOLS)
            new_syms = current - active
            if new_syms:
                slog.info('Adding symbols: %s', new_syms)
                await streamer.subscribe(Quote, list(new_syms))
                active |= new_syms

            await asyncio.sleep(1)


def main():
    slog = _get_logger()
    session = create_tastytrade_session()
    slog.info('TastyTrade streamer starting')
    asyncio.run(_stream_loop(session, slog))
    slog.info('TastyTrade streamer stopped')


if __name__ == '__main__':
    from common.process_lock import process_lock
    from common import tt_config

    with process_lock('streamer', command='publish_tastytrade.py'):
        main()
