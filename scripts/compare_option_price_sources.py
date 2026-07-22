#!/usr/bin/env python3
"""Read-only price source comparison for one SPXW option leg.

Compares TastyTrade REST vs MQTT (streamer publish) vs local CSV snapshots.
Safe to run while the bot is live — does not write trades, optsymbols, or locks.

Usage (from repo root):
  uv run python scripts/compare_option_price_sources.py
  uv run python scripts/compare_option_price_sources.py --symbol .SPXW260722P7495
  uv run python scripts/compare_option_price_sources.py --wait 5 --json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import common.win_ssl_env  # noqa: F401

import paho.mqtt.client as mqtt

from common.broker_factory import get_broker, get_mqtt_topic_prefix
from common.mqtt_prices import MqttPriceCache
from common.mqtt_stream_provenance import META_TOPIC_SUFFIX
from common.streamer_health import read_health as read_streamer_health
from common.symbols import to_schwab, to_tastytrade
from common.trades_layout import ops_path
from market_data import config as md_config
from meic0dte.app.utilities import central_now
from streaming import config as stream_config


def _default_symbol(strike: int = 7495, side: str = 'P') -> str:
    expiry = central_now().strftime('%y%m%d')
    return f'.SPXW{expiry}{side.upper()}{int(strike)}'


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _rest_quote(symbol: str) -> Dict[str, Any]:
    """Single REST market-data read (read-only)."""
    from tastytrade.market_data import get_market_data_by_type

    from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST

    broker = get_broker()
    tt_sym = to_tastytrade(symbol)
    api_sym = to_schwab(tt_sym)
    out: Dict[str, Any] = {
        'symbol': tt_sym,
        'api_symbol': api_sym,
        'mid': None,
        'bid': None,
        'ask': None,
        'last': None,
        'mark': None,
        'quote_mid': None,
        'error': None,
    }
    try:
        items = broker._run(
            get_market_data_by_type(broker.session, options=[api_sym]),
            priority='NORMAL',
            op=OPERATION_ENTRY_MARKET_DATA_REST,
        )
    except Exception as exc:
        out['error'] = str(exc)
        return out

    if not items:
        out['error'] = 'empty REST response'
        return out

    md = items[0]
    for field in ('bid', 'ask', 'last', 'mark', 'mid'):
        val = getattr(md, field, None)
        if val is not None:
            try:
                out[field] = round(float(val), 4)
            except (TypeError, ValueError):
                pass

    bid = out.get('bid')
    ask = out.get('ask')
    if bid and ask and bid > 0 and ask > 0:
        out['quote_mid'] = round((bid + ask) / 2.0, 4)

    out['mid'] = broker._mid_from_market_data(md)
    if out['mid'] is not None:
        out['mid'] = round(float(out['mid']), 4)
    return out


def _mqtt_snapshot(symbol: str, *, wait_sec: float) -> Dict[str, Any]:
    """Subscribe briefly with a unique client id; read retained + any live publishes."""
    prefix = get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
    tt_sym = to_tastytrade(symbol)
    price_topic = f'{prefix}{tt_sym}'
    meta_topic = f'{prefix}{tt_sym}{META_TOPIC_SUFFIX}'

    state: Dict[str, Any] = {
        'price_topic': price_topic,
        'meta_topic': meta_topic,
        'scalar': None,
        'scalar_received_at': None,
        'meta': None,
        'meta_received_at': None,
        'messages_seen': 0,
        'error': None,
    }
    done = {'ready': False}

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            state['error'] = f'mqtt connect rc={reason_code}'
            done['ready'] = True
            return
        client.subscribe(price_topic)
        client.subscribe(meta_topic)

    def _on_message(client, userdata, msg):
        state['messages_seen'] += 1
        now_iso = datetime.now().astimezone().isoformat(timespec='seconds')
        topic = msg.topic
        payload = msg.payload.decode(errors='replace')
        if topic == price_topic:
            try:
                state['scalar'] = round(float(payload), 4)
                state['scalar_received_at'] = now_iso
            except ValueError:
                state['scalar_raw'] = payload
        elif topic == meta_topic:
            try:
                state['meta'] = json.loads(payload)
                state['meta_received_at'] = now_iso
            except json.JSONDecodeError:
                state['meta_raw'] = payload

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f'price-compare-{os.getpid()}-{uuid.uuid4().hex[:8]}',
    )
    client.on_connect = _on_connect
    client.on_message = _on_message
    try:
        client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
        client.loop_start()
        deadline = time.time() + max(0.5, wait_sec)
        while time.time() < deadline:
            if state['scalar'] is not None and state['meta'] is not None:
                break
            time.sleep(0.05)
    except Exception as exc:
        state['error'] = str(exc)
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
    return state


def _cache_snapshot(symbol: str, *, wait_sec: float) -> Dict[str, Any]:
    """What stop_monitor / market_data see via MqttPriceCache (separate subscriber)."""
    tt_sym = to_tastytrade(symbol)
    cache = MqttPriceCache()
    out: Dict[str, Any] = {
        'get_market_mid': None,
        'get': None,
        'quote_snapshot': None,
        'cache_stale': None,
        'stream_session_id': None,
        'error': None,
    }
    try:
        cache.start()
        deadline = time.time() + max(0.5, wait_sec)
        mid = None
        while time.time() < deadline:
            mid = cache.get_market_mid(tt_sym)
            if mid is not None:
                break
            time.sleep(0.05)
        out['get_market_mid'] = round(float(mid), 4) if mid is not None else None
        plain = cache.get(tt_sym)
        out['get'] = round(float(plain), 4) if plain is not None else None
        snap = cache.get_quote(tt_sym, require_current_session=False, allow_pre_subscription=True)
        if snap is not None:
            out['quote_snapshot'] = {
                'price': round(float(snap.price), 4),
                'event_kind': snap.event_kind,
                'source_event_epoch': snap.source_event_epoch,
                'source_age_sec': round(snap.source_age_sec, 3),
                'stream_session_id': snap.stream_session_id,
                'sequence': snap.sequence,
            }
        health = cache.cache_health()
        out['cache_stale'] = health.get('stale')
        out['stream_session_id'] = health.get('stream_session_id')
    except Exception as exc:
        out['error'] = str(exc)
    finally:
        cache.stop()
    return out


def _last_csv_row(path: str, symbol: str) -> Optional[Dict[str, Any]]:
    if not os.path.isfile(path):
        return None
    tt_sym = to_tastytrade(symbol)
    last: Optional[Dict[str, Any]] = None
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            row_sym = (row.get('symbol') or '').strip()
            if row_sym == tt_sym:
                last = dict(row)
            elif 'strike' in row and 'side' in row:
                # spx_ladder_quotes.csv: snapshot_ts,strike,side,symbol,mid
                if row_sym == tt_sym:
                    last = dict(row)
    return last


def _csv_snapshots(symbol: str, *, day) -> Dict[str, Any]:
    day_path = md_config.day_dir(day)
    options_path = md_config.options_quotes_path(day_path)
    ladder_path = md_config.spx_ladder_quotes_path(day_path)
    options_row = _last_csv_row(options_path, symbol)
    ladder_row = _last_csv_row(ladder_path, symbol)
    return {
        'day_path': day_path,
        'options_quotes_csv': options_path if os.path.isfile(options_path) else None,
        'options_quotes_last': options_row,
        'spx_ladder_quotes_csv': ladder_path if os.path.isfile(ladder_path) else None,
        'spx_ladder_quotes_last': ladder_row,
    }


def _health_context() -> Dict[str, Any]:
    streamer = read_streamer_health(ROOT)
    mqtt_health = _read_json(ops_path('mqtt_cache_health.json', ROOT))
    return {
        'streamer_health': streamer,
        'stop_monitor_mqtt_cache_health': mqtt_health,
    }


def _spread_hint(rest: Dict[str, Any], mqtt_scalar: Optional[float], cache_mid: Optional[float]) -> List[str]:
    notes: List[str] = []
    rest_mid = rest.get('mid')
    if rest_mid is not None and mqtt_scalar is not None:
        delta = round(float(mqtt_scalar) - float(rest_mid), 4)
        notes.append(f'MQTT scalar vs REST mid: {delta:+.4f}')
    if rest_mid is not None and cache_mid is not None:
        delta = round(float(cache_mid) - float(rest_mid), 4)
        notes.append(f'Cache mid vs REST mid: {delta:+.4f}')
    return notes


def build_report(
    symbol: str,
    *,
    wait_sec: float,
) -> Dict[str, Any]:
    tt_sym = to_tastytrade(symbol)
    now = central_now()
    rest = _rest_quote(tt_sym)
    mqtt = _mqtt_snapshot(tt_sym, wait_sec=wait_sec)
    cache = _cache_snapshot(tt_sym, wait_sec=wait_sec)
    csvs = _csv_snapshots(tt_sym, day=now.date())
    health = _health_context()

    mqtt_scalar = mqtt.get('scalar')
    cache_mid = cache.get('get_market_mid')
    notes = _spread_hint(rest, mqtt_scalar, cache_mid)

    meta = mqtt.get('meta') or {}
    if meta.get('event_kind') == 'replay':
        notes.append('MQTT meta event_kind=replay (heartbeat republish — may be stale)')
    if cache.get('quote_snapshot') and meta:
        cs = cache['quote_snapshot']
        if cs.get('price') is not None and mqtt_scalar is not None:
            if abs(float(cs['price']) - float(mqtt_scalar)) > 0.01:
                notes.append(
                    f'Cache quote price ({cs["price"]}) differs from MQTT scalar ({mqtt_scalar})'
                )

    return {
        'checked_at': now.isoformat(timespec='seconds'),
        'symbol': tt_sym,
        'wait_sec': wait_sec,
        'rest': rest,
        'mqtt_streamer_publish': {
            'description': 'Direct MQTT topics written by publish_tastytrade.py',
            'scalar': mqtt_scalar,
            'scalar_received_at': mqtt.get('scalar_received_at'),
            'meta': meta,
            'meta_received_at': mqtt.get('meta_received_at'),
            'messages_seen': mqtt.get('messages_seen'),
            'error': mqtt.get('error'),
        },
        'mqtt_consumer_cache': {
            'description': 'MqttPriceCache path used by stop_monitor and market_data recorder',
            'get_market_mid': cache_mid,
            'get': cache.get('get'),
            'quote_snapshot': cache.get('quote_snapshot'),
            'cache_stale': cache.get('cache_stale'),
            'stream_session_id': cache.get('stream_session_id'),
            'error': cache.get('error'),
        },
        'csv': csvs,
        'health': health,
        'notes': notes,
    }


def _print_human(report: Dict[str, Any]) -> None:
    sym = report['symbol']
    print('=' * 72)
    print(f'Option price source comparison — {sym}')
    print(f'Checked at {report["checked_at"]}  (MQTT wait {report["wait_sec"]}s)')
    print('=' * 72)

    rest = report['rest']
    print('\nREST (TastyTrade API)')
    if rest.get('error'):
        print(f'  ERROR: {rest["error"]}')
    else:
        print(f'  mid (broker logic):  {rest.get("mid")}')
        print(f'  bid / ask:           {rest.get("bid")} / {rest.get("ask")}')
        print(f'  quote mid (b+a)/2:   {rest.get("quote_mid")}')
        print(f'  last:                {rest.get("last")}')
        print(f'  mark:                {rest.get("mark")}')

    mp = report['mqtt_streamer_publish']
    print('\nMQTT - streamer publish (retained + live)')
    if mp.get('error'):
        print(f'  ERROR: {mp["error"]}')
    print(f'  scalar:              {mp.get("scalar")}  @ {mp.get("scalar_received_at")}')
    meta = mp.get('meta') or {}
    if meta:
        print(f'  meta event_kind:     {meta.get("event_kind")}')
        print(f'  meta sequence:       {meta.get("sequence")}')
        print(f'  meta source_epoch:   {meta.get("source_event_epoch")}')
        print(f'  meta stream_session: {meta.get("stream_session_id")}')
    print(f'  messages seen:       {mp.get("messages_seen")}')

    cc = report['mqtt_consumer_cache']
    print('\nMQTT - consumer cache (stop_monitor / CSV recorder path)')
    if cc.get('error'):
        print(f'  ERROR: {cc["error"]}')
    print(f'  get_market_mid:      {cc.get("get_market_mid")}')
    qs = cc.get('quote_snapshot') or {}
    if qs:
        print(f'  quote_snapshot:      price={qs.get("price")} kind={qs.get("event_kind")} '
              f'age={qs.get("source_age_sec")}s seq={qs.get("sequence")}')
    print(f'  cache stale:         {cc.get("cache_stale")}')

    csvs = report['csv']
    print('\nCSV snapshots (last row for symbol — not live)')
    opt = csvs.get('options_quotes_last')
    lad = csvs.get('spx_ladder_quotes_last')
    if opt:
        print(f'  options_quotes:      {opt.get("snapshot_ts")} mid={opt.get("mid")}')
    else:
        print('  options_quotes:      (no row)')
    if lad:
        print(f'  spx_ladder_quotes:   {lad.get("snapshot_ts")} mid={lad.get("mid")}')
    else:
        print('  spx_ladder_quotes:   (no row)')

    sh = (report.get('health') or {}).get('streamer_health') or {}
    print('\nStreamer health file')
    print(f'  status:              {sh.get("status")}')
    print(f'  last_spx_price_ts:   {sh.get("last_spx_price_ts")}')
    print(f'  stream_session_id:   {sh.get("stream_session_id")}')

    notes = report.get('notes') or []
    if notes:
        print('\nNotes')
        for note in notes:
            print(f'  - {note}')
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Compare REST vs MQTT vs CSV for one SPXW option (read-only).',
    )
    parser.add_argument(
        '--symbol',
        default=_default_symbol(),
        help='TastyTrade symbol (default: today 7495P)',
    )
    parser.add_argument(
        '--wait',
        type=float,
        default=3.0,
        help='Seconds to wait for MQTT retained/live messages (default: 3)',
    )
    parser.add_argument('--json', action='store_true', help='Print JSON instead of table')
    args = parser.parse_args()

    report = build_report(args.symbol, wait_sec=args.wait)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
