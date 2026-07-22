#!/usr/bin/env python3
"""Thick production MQTT comparison — DXLink, REST, cache paths, stop_monitor JSON.

Read-only. Does not start/stop streamer or stop_monitor.

Samples every second for N seconds:
  - DXLink direct (streamer wire input)
  - REST broker API
  - Ephemeral MQTT subscriber (retained + live)
  - Persistent MqttPriceCache (same class as stop_monitor; long-lived connection)
  - stop_monitor breach_watch on disk (what prod actually uses for spread_mid)
  - mqtt_cache_health.json + streamer_health.json

Usage:
  uv run python scripts/compare_price_sources_prod.py
  uv run python scripts/compare_price_sources_prod.py --duration 30 --trade-json trades/active/MEIC_IC/11-00_P_20260722T105901.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

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
from common.tt_auth import create_tastytrade_session
from meic0dte.app.utilities import central_now
from streaming import config as stream_config


def _quote_mid(quote) -> Optional[float]:
    bid = float(quote.bid_price or 0)
    ask = float(quote.ask_price or 0)
    if bid <= 0 and ask <= 0:
        return None
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2.0, 4)
    return round(bid or ask, 4)


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


def _rest_legs(short_sym: str, long_sym: str) -> Dict[str, Any]:
    from tastytrade.market_data import get_market_data_by_type

    from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST

    broker = get_broker()
    api_syms = [to_schwab(to_tastytrade(s)) for s in (short_sym, long_sym)]
    out: Dict[str, Any] = {'short': None, 'long': None, 'spread': None, 'error': None}
    try:
        items = broker._run(
            get_market_data_by_type(broker.session, options=api_syms),
            priority='NORMAL',
            op=OPERATION_ENTRY_MARKET_DATA_REST,
        )
    except Exception as exc:
        out['error'] = str(exc)
        return out
    by_tt: Dict[str, float] = {}
    for md in items or []:
        mid = broker._mid_from_market_data(md)
        if mid is None:
            continue
        sym = to_tastytrade(getattr(md, 'symbol', '') or '')
        by_tt[sym] = round(float(mid), 4)
    short_tt = to_tastytrade(short_sym)
    long_tt = to_tastytrade(long_sym)
    out['short'] = by_tt.get(short_tt)
    out['long'] = by_tt.get(long_tt)
    if out['short'] is not None and out['long'] is not None:
        out['spread'] = round(float(out['short']) - float(out['long']), 4)
    return out


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _read_prod_breach_watch(trade_json_path: str) -> Dict[str, Any]:
    data = _read_json(trade_json_path) or {}
    watch = data.get('breach_watch') or {}
    short_sym = (data.get('short_leg') or {}).get('symbol')
    long_sym = (data.get('long_leg') or {}).get('symbol')
    return {
        'trade_json': trade_json_path,
        'short_symbol': short_sym,
        'long_symbol': long_sym,
        'spread_mid': watch.get('spread_mid'),
        'updated_at': watch.get('updated_at'),
        'short_source_epoch': watch.get('short_source_epoch'),
        'long_source_epoch': watch.get('long_source_epoch'),
        'short_sequence': watch.get('short_sequence'),
        'long_sequence': watch.get('long_sequence'),
        'stream_session_id': watch.get('stream_session_id'),
        'quote_pair_reason': watch.get('quote_pair_reason'),
        'mqtt_cache_stale': watch.get('mqtt_cache_stale'),
        'streamer_stale': watch.get('streamer_stale'),
    }


@dataclass
class LegState:
    mid: Optional[float] = None
    source_epoch: Optional[float] = None
    received_mono: float = 0.0
    updates: int = 0

    def apply(self, quote) -> None:
        mid = _quote_mid(quote)
        if mid is None:
            return
        self.mid = mid
        self.source_epoch = _quote_source_epoch(quote)
        self.received_mono = time.monotonic()
        self.updates += 1


@dataclass
class MqttLeg:
    scalar: Optional[float] = None
    event_kind: Optional[str] = None
    sequence: Optional[int] = None
    source_epoch: Optional[float] = None
    received_mono: float = 0.0


@dataclass
class TrackLast:
    value: Optional[float] = None
    changed_mono: float = 0.0

    def note(self, val: Optional[float], mono: float) -> Optional[float]:
        if val is None:
            return None
        if self.value is None or val != self.value:
            self.value = val
            self.changed_mono = mono
        return round(mono - self.changed_mono, 3) if self.changed_mono else 0.0


async def _dxlink_dual(
    session,
    short_dx: str,
    long_dx: str,
    short_st: LegState,
    long_st: LegState,
    stop: asyncio.Event,
) -> None:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [short_dx, long_dx])

        async def _consume():
            async for quote in streamer.listen(Quote):
                sym = quote.event_symbol
                if sym == short_dx:
                    short_st.apply(quote)
                elif sym == long_dx:
                    long_st.apply(quote)

        task = asyncio.create_task(_consume())
        try:
            await stop.wait()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _mqtt_dual_listener(short_sym: str, long_sym: str) -> tuple[mqtt.Client, Dict[str, MqttLeg]]:
    prefix = get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
    legs = {
        'short': MqttLeg(),
        'long': MqttLeg(),
    }
    topics = {
        f'{prefix}{to_tastytrade(short_sym)}': 'short',
        f'{prefix}{to_tastytrade(long_sym)}': 'long',
    }
    meta_topics = {
        f'{prefix}{to_tastytrade(short_sym)}{META_TOPIC_SUFFIX}': 'short',
        f'{prefix}{to_tastytrade(long_sym)}{META_TOPIC_SUFFIX}': 'long',
    }

    def _on_message(client, userdata, msg):
        role = topics.get(msg.topic) or meta_topics.get(msg.topic)
        if not role:
            return
        leg = legs[role]
        payload = msg.payload.decode(errors='replace')
        if msg.topic.endswith(META_TOPIC_SUFFIX):
            try:
                meta = json.loads(payload)
                leg.event_kind = meta.get('event_kind')
                leg.sequence = meta.get('sequence')
                leg.source_epoch = meta.get('source_event_epoch')
            except json.JSONDecodeError:
                pass
        else:
            try:
                leg.scalar = round(float(payload), 4)
                leg.received_mono = time.monotonic()
            except ValueError:
                pass

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f'prod-compare-{os.getpid()}-{uuid.uuid4().hex[:8]}',
    )
    client.on_message = _on_message

    def _on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            for t in list(topics) + list(meta_topics):
                c.subscribe(t)

    client.on_connect = _on_connect
    client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
    client.loop_start()
    return client, legs


def _cache_spread(cache: MqttPriceCache, short_sym: str, long_sym: str) -> Dict[str, Any]:
    short_tt = to_tastytrade(short_sym)
    long_tt = to_tastytrade(long_sym)
    short_mid = cache.get_market_mid(short_tt)
    long_mid = cache.get_market_mid(long_tt)
    short_q = cache.get_quote(short_tt, require_current_session=False, allow_pre_subscription=True)
    long_q = cache.get_quote(long_tt, require_current_session=False, allow_pre_subscription=True)
    spread_mid = None
    spread_quote = None
    if short_mid is not None and long_mid is not None:
        spread_mid = round(float(short_mid) - float(long_mid), 4)
    if short_q and long_q:
        spread_quote = round(float(short_q.price) - float(long_q.price), 4)
    now = time.time()
    return {
        'short_mid': round(float(short_mid), 4) if short_mid is not None else None,
        'long_mid': round(float(long_mid), 4) if long_mid is not None else None,
        'spread_mid': spread_mid,
        'short_quote_mid': round(float(short_q.price), 4) if short_q else None,
        'long_quote_mid': round(float(long_q.price), 4) if long_q else None,
        'spread_quote': spread_quote,
        'short_quote_age': round(now - short_q.source_event_epoch, 3) if short_q else None,
        'long_quote_age': round(now - long_q.source_event_epoch, 3) if long_q else None,
        'short_seq': short_q.sequence if short_q else None,
        'long_seq': long_q.sequence if long_q else None,
        'short_kind': short_q.event_kind if short_q else None,
        'long_kind': long_q.event_kind if long_q else None,
        'cache_session': cache.current_stream_session_id(),
    }


async def _run(
    *,
    short_sym: str,
    long_sym: str,
    trade_json: str,
    duration_sec: float,
    interval_sec: float,
) -> Dict[str, Any]:
    short_tt = to_tastytrade(short_sym)
    long_tt = to_tastytrade(long_sym)
    short_dx = short_tt if short_tt.startswith('.') else f'.{short_tt}'
    long_dx = long_tt if long_tt.startswith('.') else f'.{long_tt}'

    session = create_tastytrade_session()
    short_dx_st = LegState()
    long_dx_st = LegState()
    stop = asyncio.Event()

    cache = MqttPriceCache()
    cache.start()
    mqtt_client, mqtt_legs = _mqtt_dual_listener(short_tt, long_tt)

    dx_task = asyncio.create_task(
        _dxlink_dual(session, short_dx, long_dx, short_dx_st, long_dx_st, stop)
    )

    await asyncio.sleep(1.5)

    samples: List[Dict[str, Any]] = []
    track_prod = TrackLast()
    track_cache = TrackLast()
    track_mqtt = TrackLast()
    track_dx = TrackLast()
    t0 = time.monotonic()

    try:
        while (time.monotonic() - t0) < duration_sec:
            mono = time.monotonic()
            rest = _rest_legs(short_tt, long_tt)
            cache_snap = _cache_spread(cache, short_tt, long_tt)
            prod = _read_prod_breach_watch(trade_json)
            mqtt_health = _read_json(ops_path('mqtt_cache_health.json', ROOT))
            streamer_health = read_streamer_health(ROOT)

            dx_spread = None
            if short_dx_st.mid is not None and long_dx_st.mid is not None:
                dx_spread = round(float(short_dx_st.mid) - float(long_dx_st.mid), 4)

            mqtt_spread = None
            if mqtt_legs['short'].scalar is not None and mqtt_legs['long'].scalar is not None:
                mqtt_spread = round(
                    float(mqtt_legs['short'].scalar) - float(mqtt_legs['long'].scalar), 4
                )

            prod_spread = prod.get('spread_mid')
            if prod_spread is not None:
                prod_spread = round(float(prod_spread), 4)

            row = {
                't_sec': round(mono - t0, 2),
                'wall': central_now().strftime('%H:%M:%S'),
                'rest_spread': rest.get('spread'),
                'rest_short': rest.get('short'),
                'dx_spread': dx_spread,
                'dx_short': short_dx_st.mid,
                'mqtt_spread': mqtt_spread,
                'mqtt_short': mqtt_legs['short'].scalar,
                'cache_spread_mid': cache_snap.get('spread_mid'),
                'cache_spread_quote': cache_snap.get('spread_quote'),
                'cache_short': cache_snap.get('short_mid'),
                'cache_short_quote_age': cache_snap.get('short_quote_age'),
                'cache_short_seq': cache_snap.get('short_seq'),
                'prod_spread_mid': prod_spread,
                'prod_short_seq': prod.get('short_sequence'),
                'prod_updated_at': prod.get('updated_at'),
                'prod_short_epoch_age': (
                    round(time.time() - float(prod['short_source_epoch']), 3)
                    if prod.get('short_source_epoch') else None
                ),
                'delta_prod_rest': (
                    round(prod_spread - float(rest['spread']), 4)
                    if prod_spread is not None and rest.get('spread') is not None else None
                ),
                'delta_cache_rest': (
                    round(float(cache_snap['spread_mid']) - float(rest['spread']), 4)
                    if cache_snap.get('spread_mid') is not None and rest.get('spread') is not None
                    else None
                ),
                'delta_mqtt_rest': (
                    round(mqtt_spread - float(rest['spread']), 4)
                    if mqtt_spread is not None and rest.get('spread') is not None else None
                ),
                'delta_dx_rest': (
                    round(dx_spread - float(rest['spread']), 4)
                    if dx_spread is not None and rest.get('spread') is not None else None
                ),
                'delta_prod_cache': (
                    round(prod_spread - float(cache_snap['spread_mid']), 4)
                    if prod_spread is not None and cache_snap.get('spread_mid') is not None else None
                ),
                'sec_since_prod_spread_change': track_prod.note(prod_spread, mono),
                'sec_since_cache_spread_change': track_cache.note(cache_snap.get('spread_mid'), mono),
                'sec_since_mqtt_spread_change': track_mqtt.note(mqtt_spread, mono),
                'sec_since_dx_spread_change': track_dx.note(dx_spread, mono),
                'mqtt_health_age': (mqtt_health or {}).get('age_seconds'),
                'mqtt_health_session': (mqtt_health or {}).get('stream_session_id'),
                'streamer_session': (streamer_health or {}).get('stream_session_id'),
                'cache_session': cache_snap.get('cache_session'),
            }
            samples.append(row)

            elapsed = time.monotonic() - mono
            await asyncio.sleep(max(0.0, interval_sec - elapsed))
    finally:
        stop.set()
        try:
            await asyncio.wait_for(dx_task, timeout=5.0)
        except asyncio.TimeoutError:
            dx_task.cancel()
        cache.stop()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()

    def _col(vals: List[Optional[float]]) -> Dict[str, Any]:
        clean = [v for v in vals if v is not None]
        if not clean:
            return {'min': None, 'max': None, 'avg': None, 'last': None, 'nonzero_count': 0}
        return {
            'min': round(min(clean), 4),
            'max': round(max(clean), 4),
            'avg': round(sum(clean) / len(clean), 4),
            'last': round(clean[-1], 4),
            'nonzero_count': sum(1 for v in clean if abs(v) > 0.01),
        }

    return {
        'started_at': central_now().isoformat(timespec='seconds'),
        'short_symbol': short_tt,
        'long_symbol': long_tt,
        'trade_json': trade_json,
        'duration_sec': duration_sec,
        'sample_count': len(samples),
        'dxlink_updates': {'short': short_dx_st.updates, 'long': long_dx_st.updates},
        'samples': samples,
        'summary': {
            'delta_prod_rest': _col([s.get('delta_prod_rest') for s in samples]),
            'delta_cache_rest': _col([s.get('delta_cache_rest') for s in samples]),
            'delta_mqtt_rest': _col([s.get('delta_mqtt_rest') for s in samples]),
            'delta_dx_rest': _col([s.get('delta_dx_rest') for s in samples]),
            'delta_prod_cache': _col([s.get('delta_prod_cache') for s in samples]),
        },
    }


def _print_report(r: Dict[str, Any]) -> None:
    print('=' * 100)
    print(f'Production MQTT comparison — {r["short_symbol"]} / {r["long_symbol"]}')
    print(f'Started {r["started_at"]}  duration={r["duration_sec"]}s  samples={r["sample_count"]}')
    print(f'Trade JSON: {r["trade_json"]}')
    print(f'DXLink updates: {r["dxlink_updates"]}')
    print('=' * 100)
    print(
        f'{"t":>5} {"wall":>8} {"REST":>6} {"DX":>6} {"MQTT":>6} {"cache":>6} '
        f'{"PROD":>6} {"p-r":>6} {"c-r":>6} {"p-c":>6} {"p_chg":>5} {"c_chg":>5}'
    )
    for row in r['samples']:
        print(
            f'{row["t_sec"]:5.1f} {row["wall"]:>8} '
            f'{_f(row.get("rest_spread")):>6} {_f(row.get("dx_spread")):>6} '
            f'{_f(row.get("mqtt_spread")):>6} {_f(row.get("cache_spread_mid")):>6} '
            f'{_f(row.get("prod_spread_mid")):>6} '
            f'{_f(row.get("delta_prod_rest")):>6} {_f(row.get("delta_cache_rest")):>6} '
            f'{_f(row.get("delta_prod_cache")):>6} '
            f'{_f(row.get("sec_since_prod_spread_change")):>5} '
            f'{_f(row.get("sec_since_cache_spread_change")):>5}'
        )

    print('\nSummary deltas (spread vs REST / prod vs cache):')
    for key, stats in r['summary'].items():
        print(f'  {key}: {stats}')

    last = r['samples'][-1] if r['samples'] else {}
    print('\nLast sample detail:')
    for k in (
        'rest_short', 'dx_short', 'mqtt_short', 'cache_short',
        'cache_short_quote_age', 'cache_short_seq', 'prod_short_seq',
        'prod_short_epoch_age', 'mqtt_health_age', 'mqtt_health_session',
        'streamer_session', 'cache_session', 'prod_updated_at',
    ):
        print(f'  {k}: {last.get(k)}')

    # Verdict
    s = r['summary']
    prod_nz = s['delta_prod_rest'].get('nonzero_count', 0)
    cache_nz = s['delta_cache_rest'].get('nonzero_count', 0)
    prod_cache_nz = s['delta_prod_cache'].get('nonzero_count', 0)
    print('\nVerdict:')
    if prod_nz >= r['sample_count'] * 0.5 and abs(s['delta_prod_rest'].get('last') or 0) > 0.15:
        print('  PROD breach_watch spread is persistently behind REST (stop_monitor MQTT stale)')
    elif prod_cache_nz >= r['sample_count'] * 0.5:
        print('  PROD JSON diverges from fresh MqttPriceCache — stop_monitor process may be stuck')
    elif cache_nz >= r['sample_count'] * 0.5:
        print('  MQTT cache (MqttPriceCache) lags REST while DXLink may be fresh')
    else:
        print('  All sources aligned during this window')
    print()


def _f(v) -> str:
    if v is None:
        return '-'
    return f'{v:.3f}'


def main() -> int:
    parser = argparse.ArgumentParser(description='Thick prod MQTT vs REST/DXLink comparison')
    parser.add_argument(
        '--trade-json',
        default=os.path.join(ROOT, 'trades/active/MEIC_IC/11-00_P_20260722T105901.json'),
    )
    parser.add_argument('--duration', type=float, default=30.0)
    parser.add_argument('--interval', type=float, default=1.0)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    trade = _read_json(args.trade_json) or {}
    short_sym = (trade.get('short_leg') or {}).get('symbol', '.SPXW260722P7495')
    long_sym = (trade.get('long_leg') or {}).get('symbol', '.SPXW260722P7470')

    report = asyncio.run(
        _run(
            short_sym=short_sym,
            long_sym=long_sym,
            trade_json=args.trade_json,
            duration_sec=args.duration,
            interval_sec=args.interval,
        )
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
