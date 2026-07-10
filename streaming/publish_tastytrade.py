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
import time
from datetime import datetime as dt, timezone

import paho.mqtt.publish as publish

from common.broker_factory import get_mqtt_topic_prefix
from common.market_watch import (
    SPX_LADDER_VOLUME_ENABLED,
    TRADE_SIZE_TOPIC_SUFFIX,
    VOLUME_TOPIC_SUFFIX,
    WATCH_SYMBOLS,
    log_sidecar_disabled_once,
    mqtt_topic_from_dxlink,
    sidecar_option_collection_enabled,
)
from common.market_quote import REPLAY_EVENT_KIND
from common.mqtt_stream_provenance import (
    HEARTBEAT_TOPIC,
    SESSION_TOPIC,
    StreamPublishState,
    build_heartbeat_payload,
    build_quote_meta,
    build_session_payload,
    heartbeat_interval_sec,
    legacy_republish_enabled,
    meta_topic_for,
)
from common.session_logs import STREAM_TT_BASE, new_session_log_path, relocate_legacy_log
from common.streamer_health import write_health
from common.tt_auth import create_tastytrade_session
from meic0dte.app.utilities import central_now
from streaming import config
from streaming.ladder_subscribe import (
    LadderSubscribeGuard,
    build_quote_subscribe_set,
    build_trade_subscribe_set,
)

OPTION_SYMBOL_FILE = config.STREAM_SYMBOLS
TOPIC_PREFIX = get_mqtt_topic_prefix() or 'TASTYTRADE/'


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


def _read_ladder_meta() -> dict:
    path = os.path.join(_root, 'streaming', 'spx_ladder_symbols.json')
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _quote_source_epoch(quote) -> float:
    for attr in ('time', 'event_time', 'eventTime'):
        raw = getattr(quote, attr, None)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1e12:
            value /= 1000.0
        if value > 0:
            return value
    return time.time()


def _trade_source_epoch(trade) -> float:
    for attr in ('time', 'event_time', 'eventTime'):
        raw = getattr(trade, attr, None)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 1e12:
            value /= 1000.0
        if value > 0:
            return value
    return time.time()


async def _stream_loop(session, slog):
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Trade

    _session_started = central_now()
    guard = LadderSubscribeGuard()
    quote_set, sub_meta = build_quote_subscribe_set()
    trade_set = build_trade_subscribe_set(quote_set)
    slog.info(
        'Initial subscribe — watch+legs+ladder=%d (legs=%d ladder=%d watch=%d)',
        sub_meta['total'],
        sub_meta['trade_leg_count'],
        sub_meta['ladder_count'],
        sub_meta['watch_count'],
    )
    if not sidecar_option_collection_enabled():
        log_sidecar_disabled_once(slog)

    pub_state = StreamPublishState()
    heartbeat_every = max(1, int(round(heartbeat_interval_sec())))

    try:
        publish.single(
            config.KILL_SWITCH_TOPIC.replace('SCHWAB/', TOPIC_PREFIX),
            'False',
            retain=True,
            hostname=config.MQTT_BROKER_ADDR,
        )
    except Exception as exc:
        slog.info('Kill switch publish error: %s', exc)

    last_spx_price_ts: str | None = None
    ladder_last_update: str | None = None

    def _mqtt_publish_scalar(symbol: str, mid: float) -> None:
        nonlocal last_spx_price_ts
        symbol = mqtt_topic_from_dxlink(symbol)
        if symbol == 'SPX':
            last_spx_price_ts = dt.now(timezone.utc).astimezone().isoformat(timespec='seconds')
        try:
            publish.single(
                TOPIC_PREFIX + symbol,
                str(mid),
                retain=True,
                hostname=config.MQTT_BROKER_ADDR,
            )
        except Exception as exc:
            slog.info('MQTT publish error %s: %s', symbol, exc)

    def _mqtt_publish_aux(topic: str, value: str, *, retain: bool = True) -> None:
        try:
            publish.single(
                TOPIC_PREFIX + topic,
                value,
                retain=retain,
                hostname=config.MQTT_BROKER_ADDR,
            )
        except Exception as exc:
            slog.info('MQTT aux publish error %s: %s', topic, exc)

    def _publish_session() -> None:
        payload = build_session_payload(
            stream_session_id=pub_state.stream_session_id,
            started_epoch=pub_state.started_epoch,
            symbols_with_quotes=len(pub_state.last_mids),
        )
        _mqtt_publish_aux(SESSION_TOPIC, json.dumps(payload))
        slog.info('Published stream session %s', pub_state.stream_session_id)

    def _publish_heartbeat() -> None:
        payload = build_heartbeat_payload(
            stream_session_id=pub_state.stream_session_id,
            symbols_with_quotes=len(pub_state.last_mids),
        )
        _mqtt_publish_aux(HEARTBEAT_TOPIC, json.dumps(payload))

    def _publish_genuine(
        dx_symbol: str,
        mid: float,
        *,
        event_kind: str,
        source_event_epoch: float,
    ) -> None:
        mqtt_sym = mqtt_topic_from_dxlink(dx_symbol)
        sub_epoch = pub_state.subscription_epoch_for(dx_symbol)
        seq = pub_state.next_sequence(dx_symbol)
        published = time.time()
        meta = build_quote_meta(
            symbol=mqtt_sym,
            source_event_epoch=source_event_epoch,
            stream_session_id=pub_state.stream_session_id,
            subscription_epoch=sub_epoch,
            sequence=seq,
            event_kind=event_kind,
            published_epoch=published,
        )
        pub_state.record_genuine(dx_symbol, mid, meta)
        _mqtt_publish_aux(meta_topic_for(mqtt_sym), json.dumps(meta))
        _mqtt_publish_scalar(mqtt_sym, mid)

    def _publish_legacy_replay() -> None:
        if not legacy_republish_enabled():
            return
        for sym, mid in list(pub_state.last_mids.items()):
            last_meta = pub_state.last_genuine_meta.get(sym)
            if last_meta is None:
                _mqtt_publish_scalar(sym, mid)
                continue
            replay_meta = {
                **last_meta,
                'event_kind': REPLAY_EVENT_KIND,
                'published_epoch': round(time.time(), 6),
            }
            _mqtt_publish_aux(meta_topic_for(sym), json.dumps(replay_meta))
            _mqtt_publish_scalar(sym, mid)

    _publish_session()

    async with DXLinkStreamer(session) as streamer:
        active_quotes = set(quote_set)
        active_trades = set(trade_set)
        pub_state.note_subscriptions(active_quotes)
        await streamer.subscribe(Quote, list(active_quotes))
        if active_trades:
            await streamer.subscribe(Trade, list(active_trades))

        async def _handle_quotes():
            async for quote in streamer.listen(Quote):
                if quote.bid_price is None and quote.ask_price is None:
                    continue
                bid = float(quote.bid_price or 0)
                ask = float(quote.ask_price or 0)
                if bid <= 0 and ask <= 0:
                    continue
                mid = (bid + ask) / 2 if bid and ask else (bid or ask)
                _publish_genuine(
                    quote.event_symbol,
                    mid,
                    event_kind='dxlink_quote',
                    source_event_epoch=_quote_source_epoch(quote),
                )

        async def _handle_trades():
            async for trade in streamer.listen(Trade):
                raw_sym = trade.event_symbol
                mqtt_sym = mqtt_topic_from_dxlink(raw_sym)
                price = float(trade.price or 0)
                if mqtt_sym == 'SPX':
                    if price > 0:
                        _publish_genuine(
                            'SPX',
                            price,
                            event_kind='dxlink_trade',
                            source_event_epoch=_trade_source_epoch(trade),
                        )
                    continue
                if mqtt_sym in WATCH_SYMBOLS and mqtt_sym != 'SPX':
                    size = int(trade.size or 0)
                    day_vol = int(trade.day_volume or 0)
                    if size > 0:
                        _mqtt_publish_aux(f'{mqtt_sym}{TRADE_SIZE_TOPIC_SUFFIX}', str(size))
                    if day_vol > 0:
                        _mqtt_publish_aux(f'{mqtt_sym}{VOLUME_TOPIC_SUFFIX}', str(day_vol))
                    if price > 0 and mqtt_sym not in pub_state.last_mids:
                        _publish_genuine(
                            mqtt_sym,
                            price,
                            event_kind='dxlink_trade',
                            source_event_epoch=_trade_source_epoch(trade),
                        )
                elif (
                    SPX_LADDER_VOLUME_ENABLED
                    and sidecar_option_collection_enabled()
                    and str(raw_sym).startswith('.SPXW')
                    and price > 0
                ):
                    day_vol = int(trade.day_volume or 0)
                    if day_vol > 0:
                        _mqtt_publish_aux(f'{raw_sym}{VOLUME_TOPIC_SUFFIX}', str(day_vol))

        quote_task = asyncio.create_task(_handle_quotes())
        trade_task = asyncio.create_task(_handle_trades())

        tick = 0
        while True:
            # Session lifecycle is owned by run.py (or another launcher profile).
            # Do not self-stop at cash close — overnight/futures streamers may run longer.

            tick += 1
            if tick % heartbeat_every == 0:
                ladder_meta = _read_ladder_meta()
                ladder_last_update = ladder_meta.get('updated_at')
                write_health(
                    last_spx_price_ts=last_spx_price_ts,
                    symbols_subscribed=len(active_quotes),
                    status='live' if last_spx_price_ts else 'waiting',
                    ladder_enabled=sidecar_option_collection_enabled(),
                    ladder_symbol_count=sub_meta.get('ladder_count', 0),
                    total_subscribed_symbols=len(active_quotes),
                    ladder_last_update=ladder_last_update,
                    ladder_last_error=guard.last_error,
                    stream_session_id=pub_state.stream_session_id,
                )
                _publish_heartbeat()
                _publish_legacy_replay()

            quote_set, sub_meta = build_quote_subscribe_set()
            trade_set = build_trade_subscribe_set(quote_set)
            new_quotes = guard.filter_subscribe(quote_set - active_quotes)
            new_trades = trade_set - active_trades
            if new_quotes:
                slog.info('Adding quote symbols: %d', len(new_quotes))
                try:
                    await streamer.subscribe(Quote, list(new_quotes))
                    guard.mark_success(new_quotes)
                    pub_state.note_subscriptions(new_quotes)
                    active_quotes |= new_quotes
                except Exception as exc:
                    slog.info('Quote subscribe error: %s', exc)
                    guard.mark_failed(new_quotes, str(exc))
            if new_trades:
                slog.info('Adding trade symbols: %d', len(new_trades))
                try:
                    await streamer.subscribe(Trade, list(new_trades))
                    active_trades |= new_trades
                except Exception as exc:
                    slog.info('Trade subscribe error: %s', exc)

            await asyncio.sleep(1)


def main():
    slog = _get_logger()
    session = create_tastytrade_session()
    slog.info('TastyTrade streamer starting')
    asyncio.run(_stream_loop(session, slog))
    slog.info('TastyTrade streamer stopped')


if __name__ == '__main__':
    from common.process_lock import process_lock

    with process_lock('streamer', command='publish_tastytrade.py'):
        main()
