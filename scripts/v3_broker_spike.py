#!/usr/bin/env python3
"""V3-0 broker parallelism spike — measure serial vs parallel get_order_status.

Safe read-only probe against TastyTrade (no order placement). Works after hours
if session is valid and live orders exist.

Usage:
  python scripts/v3_broker_spike.py
  python scripts/v3_broker_spike.py --order-ids 480934535 480934537
  python scripts/v3_broker_spike.py --lane-size 6 --workers 8
  python scripts/v3_broker_spike.py --mock-only   # BrokerLane overhead only
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_order_ids(broker, explicit: Optional[List[str]]) -> List[str]:
    if explicit:
        return [str(x) for x in explicit]
    try:
        orders = broker._run(broker.account.get_live_orders(broker.session))  # type: ignore[attr-defined]
    except Exception as exc:
        print(f'Could not fetch live orders: {exc}')
        return []
    ids: List[str] = []
    for o in orders or []:
        oid = getattr(o, 'id', None) or getattr(o, 'order_id', None)
        if oid is not None:
            ids.append(str(oid))
    return ids[:12]


def _serial_probe(broker, order_ids: List[str], rounds: int) -> float:
    t0 = time.perf_counter()
    for _ in range(rounds):
        for oid in order_ids:
            broker.get_order_status(oid)
    return time.perf_counter() - t0


def _parallel_probe(broker, order_ids: List[str], rounds: int, workers: int) -> float:
    def _one(oid: str) -> None:
        broker.get_order_status(oid)

    t0 = time.perf_counter()
    for _ in range(rounds):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, order_ids))
    return time.perf_counter() - t0


def _lane_probe(broker, order_ids: List[str], rounds: int, lane_size: int) -> float:
    from blocks.stop.v3.broker_lane import BrokerLane

    lane = BrokerLane(max_concurrent=lane_size)

    def _trade_job(trade_id: str, oid: str) -> None:
        lane.run(trade_id, lambda: broker.get_order_status(oid))

    t0 = time.perf_counter()
    for _ in range(rounds):
        threads = [
            threading.Thread(
                target=_trade_job,
                args=(f'trade-{i}', oid),
                daemon=True,
            )
            for i, oid in enumerate(order_ids)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
    return time.perf_counter() - t0


def _mock_probe(workers: int, lane_size: int) -> None:
    from blocks.stop.v3.broker_lane import BrokerLane
    from tests.mock_broker import MockBroker

    broker = MockBroker()
    for i in range(workers):
        broker.orders[str(9000 + i)] = broker.get_order_status('9001')

    order_ids = [str(9000 + i) for i in range(workers)]
    lane = BrokerLane(max_concurrent=lane_size)

    def _job(i: int) -> None:
        oid = order_ids[i % len(order_ids)]
        lane.run(f't{i}', lambda: broker.get_order_status(oid))

    t0 = time.perf_counter()
    threads = [threading.Thread(target=_job, args=(i,), daemon=True) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f'Mock lane probe: {workers} workers, lane={lane_size}, wall={time.perf_counter() - t0:.3f}s')


def main() -> int:
    parser = argparse.ArgumentParser(description='V3-0 broker parallelism spike')
    parser.add_argument('--order-ids', nargs='*', help='Order IDs to probe (default: live book)')
    parser.add_argument('--rounds', type=int, default=2, help='Repeat count per mode')
    parser.add_argument('--workers', type=int, default=6, help='Parallel worker count')
    parser.add_argument('--lane-size', type=int, default=6, help='BrokerLane semaphore size')
    parser.add_argument('--mock-only', action='store_true', help='Skip TT; test BrokerLane only')
    parser.add_argument('--paper', action='store_true', help='Paper TT session')
    args = parser.parse_args()

    if args.mock_only:
        _mock_probe(args.workers, args.lane_size)
        return 0

    from common import tt_config
    from common.broker_factory import get_broker

    paper = args.paper or tt_config.PAPER_MODE
    broker = get_broker(paper=paper)
    if not broker.connect():
        print('Broker connect failed')
        return 1

    order_ids = _load_order_ids(broker, args.order_ids)
    if len(order_ids) < 2:
        print('Need at least 2 order IDs — pass --order-ids or ensure live book has orders')
        return 1

    n = len(order_ids)
    print(f'V3-0 spike: {n} orders, {args.rounds} rounds, workers={args.workers}, lane={args.lane_size}')

    serial = _serial_probe(broker, order_ids, args.rounds)
    parallel = _parallel_probe(broker, order_ids, args.rounds, args.workers)
    lane = _lane_probe(broker, order_ids, args.rounds, args.lane_size)

    ops = n * args.rounds
    print(f'  Serial:   {serial:.3f}s  ({ops} ops, {ops / serial:.1f} ops/s)')
    print(f'  Parallel: {parallel:.3f}s  ({ops} ops, {ops / parallel:.1f} ops/s)')
    print(f'  Lane:     {lane:.3f}s  ({ops} ops, {ops / lane:.1f} ops/s)')
    if serial > 0:
        print(f'  Speedup parallel vs serial: {serial / parallel:.2f}x')
        print(f'  Speedup lane vs serial:     {serial / lane:.2f}x')
    print('Done — review for 429/errors in logs; tune STOP_BROKER_LANE_SIZE accordingly.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
