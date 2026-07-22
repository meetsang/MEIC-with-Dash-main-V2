#!/usr/bin/env python3
"""Compare live DXLink quotes (streamer input) vs REST for one SPXW option.

Opens a **separate** DXLink subscription — does NOT start publish_tastytrade.py,
does NOT publish MQTT, and does NOT touch streamer locks. Safe while bot runs.

Answers: is the streamer receiving stale DXLink data, or is MQTT/cache downstream?

Usage (from repo root):
  uv run python scripts/compare_dxlink_rest_stream.py
  uv run python scripts/compare_dxlink_rest_stream.py --duration 10 --include-mqtt
  uv run python scripts/compare_dxlink_rest_stream.py --symbol .SPXW260722P7495 --json
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
from common.symbols import to_schwab, to_tastytrade
from common.tt_auth import create_tastytrade_session
from meic0dte.app.utilities import central_now
from streaming import config as stream_config


def _default_symbol(strike: int = 7495, side: str = 'P') -> str:
    expiry = central_now().strftime('%y%m%d')
    return f'.SPXW{expiry}{side.upper()}{int(strike)}'


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


def _rest_snapshot(symbol: str) -> Dict[str, Any]:
    from tastytrade.market_data import get_market_data_by_type

    from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST

    tt_sym = to_tastytrade(symbol)
    api_sym = to_schwab(tt_sym)
    out: Dict[str, Any] = {
        'mid': None,
        'bid': None,
        'ask': None,
        'last': None,
        'mark': None,
        'error': None,
    }
    broker = get_broker()
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
        out['error'] = 'empty response'
        return out
    md = items[0]
    for key in ('bid', 'ask', 'last', 'mark', 'mid'):
        val = getattr(md, key, None)
        if val is not None:
            try:
                out[key] = round(float(val), 4)
            except (TypeError, ValueError):
                pass
    out['mid'] = broker._mid_from_market_data(md)
    if out['mid'] is not None:
        out['mid'] = round(float(out['mid']), 4)
    return out


@dataclass
class DxLinkState:
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    source_epoch: Optional[float] = None
    received_mono: float = 0.0
    update_count: int = 0

    def apply(self, quote) -> None:
        bid = float(quote.bid_price or 0) or None
        ask = float(quote.ask_price or 0) or None
        mid = _quote_mid(quote)
        if mid is None:
            return
        self.bid = round(bid, 4) if bid else None
        self.ask = round(ask, 4) if ask else None
        self.mid = mid
        self.source_epoch = _quote_source_epoch(quote)
        self.received_mono = time.monotonic()
        self.update_count += 1


@dataclass
class MqttState:
    scalar: Optional[float] = None
    received_mono: float = 0.0
    event_kind: Optional[str] = None
    sequence: Optional[int] = None


async def _dxlink_listener(
    session,
    dx_symbol: str,
    state: DxLinkState,
    stop_event: asyncio.Event,
) -> None:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, [dx_symbol])

        async def _consume():
            async for quote in streamer.listen(Quote):
                if quote.event_symbol != dx_symbol:
                    continue
                state.apply(quote)

        task = asyncio.create_task(_consume())
        try:
            await stop_event.wait()
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _start_mqtt_listener(symbol: str) -> tuple[mqtt.Client, MqttState]:
    prefix = get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
    tt_sym = to_tastytrade(symbol)
    price_topic = f'{prefix}{tt_sym}'
    meta_topic = f'{prefix}{tt_sym}__META'
    state = MqttState()

    def _on_message(client, userdata, msg):
        topic = msg.topic
        payload = msg.payload.decode(errors='replace')
        if topic == price_topic:
            try:
                state.scalar = round(float(payload), 4)
                state.received_mono = time.monotonic()
            except ValueError:
                pass
        elif topic == meta_topic:
            try:
                meta = json.loads(payload)
                state.event_kind = meta.get('event_kind')
                state.sequence = meta.get('sequence')
            except json.JSONDecodeError:
                pass

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f'dxlink-rest-{os.getpid()}-{uuid.uuid4().hex[:8]}',
    )
    client.on_message = _on_message

    def _on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            c.subscribe(price_topic)
            c.subscribe(meta_topic)

    client.on_connect = _on_connect
    client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
    client.loop_start()
    return client, state


async def _run_compare(
    symbol: str,
    *,
    duration_sec: float,
    sample_interval_sec: float,
    include_mqtt: bool,
) -> Dict[str, Any]:
    tt_sym = to_tastytrade(symbol)
    dx_symbol = tt_sym if tt_sym.startswith('.') else f'.{tt_sym}'

    session = create_tastytrade_session()
    dx_state = DxLinkState()
    stop_event = asyncio.Event()

    mqtt_client: Optional[mqtt.Client] = None
    mqtt_state: Optional[MqttState] = None
    if include_mqtt:
        mqtt_client, mqtt_state = _start_mqtt_listener(tt_sym)

    listener_task = asyncio.create_task(
        _dxlink_listener(session, dx_symbol, dx_state, stop_event)
    )

    # Let DXLink connect and deliver first quote.
    await asyncio.sleep(1.0)

    samples: List[Dict[str, Any]] = []
    started = central_now()
    t0 = time.monotonic()
    sample_idx = 0

    try:
        while (time.monotonic() - t0) < duration_sec:
            loop_start = time.monotonic()
            rest = _rest_snapshot(tt_sym)
            now = central_now()
            dx_age = (
                None
                if dx_state.received_mono <= 0
                else round(time.monotonic() - dx_state.received_mono, 3)
            )
            mqtt_age = None
            mqtt_scalar = None
            mqtt_kind = None
            mqtt_seq = None
            if mqtt_state is not None:
                mqtt_scalar = mqtt_state.scalar
                mqtt_kind = mqtt_state.event_kind
                mqtt_seq = mqtt_state.sequence
                if mqtt_state.received_mono > 0:
                    mqtt_age = round(time.monotonic() - mqtt_state.received_mono, 3)

            rest_mid = rest.get('mid')
            dx_mid = dx_state.mid
            row: Dict[str, Any] = {
                't_sec': round(time.monotonic() - t0, 2),
                'wall_ts': now.strftime('%H:%M:%S'),
                'dxlink_bid': dx_state.bid,
                'dxlink_ask': dx_state.ask,
                'dxlink_mid': dx_mid,
                'dxlink_age_sec': dx_age,
                'dxlink_updates': dx_state.update_count,
                'rest_mid': rest_mid,
                'rest_bid': rest.get('bid'),
                'rest_ask': rest.get('ask'),
                'rest_last': rest.get('last'),
                'rest_mark': rest.get('mark'),
                'rest_error': rest.get('error'),
                'delta_dx_rest': (
                    round(float(dx_mid) - float(rest_mid), 4)
                    if dx_mid is not None and rest_mid is not None
                    else None
                ),
            }
            if include_mqtt:
                row.update({
                    'mqtt_scalar': mqtt_scalar,
                    'mqtt_age_sec': mqtt_age,
                    'mqtt_event_kind': mqtt_kind,
                    'mqtt_sequence': mqtt_seq,
                    'delta_mqtt_rest': (
                        round(float(mqtt_scalar) - float(rest_mid), 4)
                        if mqtt_scalar is not None and rest_mid is not None
                        else None
                    ),
                    'delta_dx_mqtt': (
                        round(float(dx_mid) - float(mqtt_scalar), 4)
                        if dx_mid is not None and mqtt_scalar is not None
                        else None
                    ),
                })
            samples.append(row)
            sample_idx += 1

            elapsed = time.monotonic() - loop_start
            sleep_for = max(0.0, sample_interval_sec - elapsed)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(listener_task, timeout=5.0)
        except asyncio.TimeoutError:
            listener_task.cancel()
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

    deltas_dx_rest = [s['delta_dx_rest'] for s in samples if s.get('delta_dx_rest') is not None]
    deltas_mqtt_rest = [s['delta_mqtt_rest'] for s in samples if s.get('delta_mqtt_rest') is not None]
    deltas_dx_mqtt = [s['delta_dx_mqtt'] for s in samples if s.get('delta_dx_mqtt') is not None]

    def _stats(vals: List[float]) -> Dict[str, Optional[float]]:
        if not vals:
            return {'min': None, 'max': None, 'avg': None, 'last': None}
        return {
            'min': round(min(vals), 4),
            'max': round(max(vals), 4),
            'avg': round(sum(vals) / len(vals), 4),
            'last': round(vals[-1], 4),
        }

    verdict = 'unknown'
    if deltas_dx_rest and all(abs(v) <= 0.15 for v in deltas_dx_rest):
        verdict = 'dxlink_matches_rest'
    elif deltas_dx_rest and any(abs(v) > 0.5 for v in deltas_dx_rest):
        verdict = 'dxlink_stale_vs_rest'
    if include_mqtt and deltas_dx_mqtt and deltas_dx_rest:
        if deltas_dx_mqtt and all(abs(v) <= 0.05 for v in deltas_dx_mqtt):
            if any(abs(v) > 0.5 for v in deltas_dx_rest):
                verdict = 'mqtt_matches_dxlink_both_stale_vs_rest'
            else:
                verdict = 'mqtt_matches_dxlink'
        elif any(abs(v) > 0.5 for v in deltas_dx_mqtt):
            verdict = 'mqtt_stale_vs_dxlink'

    return {
        'started_at': started.isoformat(timespec='seconds'),
        'symbol': tt_sym,
        'dxlink_symbol': dx_symbol,
        'duration_sec': duration_sec,
        'sample_interval_sec': sample_interval_sec,
        'include_mqtt': include_mqtt,
        'dxlink_total_updates': dx_state.update_count,
        'samples': samples,
        'summary': {
            'delta_dx_rest': _stats(deltas_dx_rest),
            'delta_mqtt_rest': _stats(deltas_mqtt_rest),
            'delta_dx_mqtt': _stats(deltas_dx_mqtt),
            'verdict': verdict,
        },
    }


def _print_human(report: Dict[str, Any]) -> None:
    sym = report['symbol']
    print('=' * 88)
    print(f'DXLink (streamer input) vs REST{" vs MQTT" if report["include_mqtt"] else ""} — {sym}')
    print(
        f'Started {report["started_at"]}  duration={report["duration_sec"]}s  '
        f'interval={report["sample_interval_sec"]}s  dxlink_updates={report["dxlink_total_updates"]}'
    )
    print('=' * 88)

    if report['include_mqtt']:
        print(
            f'{"t":>5} {"wall":>8} {"dx_mid":>7} {"rest":>7} {"mqtt":>7} '
            f'{"d_dx-r":>7} {"d_mq-r":>7} {"d_dx-mq":>7} {"dx_age":>6} {"mq_age":>6}'
        )
    else:
        print(f'{"t":>5} {"wall":>8} {"dx_mid":>7} {"rest":>7} {"d_dx-r":>7} {"dx_age":>6}')

    for row in report['samples']:
        if report['include_mqtt']:
            print(
                f'{row["t_sec"]:5.1f} {row["wall_ts"]:>8} '
                f'{_fmt(row.get("dxlink_mid")):>7} {_fmt(row.get("rest_mid")):>7} {_fmt(row.get("mqtt_scalar")):>7} '
                f'{_fmt(row.get("delta_dx_rest")):>7} {_fmt(row.get("delta_mqtt_rest")):>7} '
                f'{_fmt(row.get("delta_dx_mqtt")):>7} '
                f'{_fmt(row.get("dxlink_age_sec")):>6} {_fmt(row.get("mqtt_age_sec")):>6}'
            )
        else:
            print(
                f'{row["t_sec"]:5.1f} {row["wall_ts"]:>8} '
                f'{_fmt(row.get("dxlink_mid")):>7} {_fmt(row.get("rest_mid")):>7} '
                f'{_fmt(row.get("delta_dx_rest")):>7} {_fmt(row.get("dxlink_age_sec")):>6}'
            )

    summary = report['summary']
    print('\nSummary (delta = A - B)')
    print(f'  dxlink - rest:  {summary["delta_dx_rest"]}')
    if report['include_mqtt']:
        print(f'  mqtt - rest:    {summary["delta_mqtt_rest"]}')
        print(f'  dxlink - mqtt:  {summary["delta_dx_mqtt"]}')
    print(f'\nVerdict: {summary["verdict"]}')
    print(
        '\nInterpretation:'
        '\n  dxlink_matches_rest        -> streamer input is fresh; MQTT/cache is suspect'
        '\n  dxlink_stale_vs_rest       -> DXLink itself lags REST (streamer source stale)'
        '\n  mqtt_matches_dxlink        -> MQTT tracks DXLink; both may lag REST together'
        '\n  mqtt_stale_vs_dxlink       -> MQTT/cache diverges from live DXLink (downstream bug)'
        '\n  mqtt_matches_dxlink_both_stale_vs_rest -> DXLink+MQTT agree but both behind REST'
    )
    print()


def _fmt(val) -> str:
    if val is None:
        return '-'
    if isinstance(val, float):
        return f'{val:.3f}'
    return str(val)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Compare live DXLink quotes vs REST (optional MQTT) for one option.',
    )
    parser.add_argument('--symbol', default=_default_symbol(), help='TastyTrade option symbol')
    parser.add_argument('--duration', type=float, default=10.0, help='Run length in seconds')
    parser.add_argument('--interval', type=float, default=1.0, help='REST sample interval')
    parser.add_argument(
        '--include-mqtt',
        action='store_true',
        help='Also sample running bot MQTT topics each tick',
    )
    parser.add_argument('--json', action='store_true', help='Print JSON report')
    args = parser.parse_args()

    report = asyncio.run(
        _run_compare(
            args.symbol,
            duration_sec=args.duration,
            sample_interval_sec=args.interval,
            include_mqtt=args.include_mqtt,
        )
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
