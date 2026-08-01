"""Broker factory — returns the correct broker and streamer for the configured BROKER."""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any, Dict, Optional, Tuple

from common import tt_config

log = logging.getLogger(__name__)

_shared_broker: Any = None
_shared_key: Optional[Tuple[Any, ...]] = None
_strategy_brokers: Dict[Tuple[Any, ...], Any] = {}
_shared_lock = threading.Lock()
_shared_created_at: Optional[float] = None
_shared_use_count = 0


def _live_broker_blocked_in_tests() -> bool:
    if os.environ.get('MEIC_ALLOW_LIVE_BROKER_TESTS') == '1':
        return False
    return 'pytest' in sys.modules


def get_broker(paper: Optional[bool] = None, *, _test_override: bool = False):
    """Instantiate a fresh broker/session (isolated — prefer get_shared_broker in long-running processes)."""
    if _live_broker_blocked_in_tests() and not _test_override:
        raise RuntimeError(
            'Live broker creation blocked during tests. '
            'Use MockBroker or set MEIC_ALLOW_LIVE_BROKER_TESTS=1 for integration tests.',
        )
    broker_name = tt_config.BROKER

    if broker_name == 'tastytrade':
        from common.tt_auth import create_tastytrade_session
        from brokers.tastytrade_broker import TastyTradeBroker

        use_paper = paper if paper is not None else tt_config.PAPER_MODE
        session = create_tastytrade_session(paper=use_paper)
        broker = TastyTradeBroker(session)
        log.info('Broker ready (paper=%s)', use_paper)
        return broker

    if broker_name == 'schwab':
        raise NotImplementedError(
            'Schwab uses legacy meic0dte/order.py directly. '
            'Set BROKER=tastytrade for the new broker abstraction.',
        )

    raise ValueError(f'Unknown BROKER: {broker_name}')


def get_shared_broker(paper: Optional[bool] = None, *, reset: bool = False):
    """Reuse one broker instance per process (paper/live flag + PID)."""
    global _shared_broker, _shared_key, _shared_created_at, _shared_use_count

    use_paper = paper if paper is not None else tt_config.PAPER_MODE
    key = (tt_config.BROKER, bool(use_paper), os.getpid())

    with _shared_lock:
        if reset or _shared_broker is None or _shared_key != key:
            _shared_broker = get_broker(paper=use_paper)
            _shared_key = key
            _shared_created_at = __import__('time').time()
            _shared_use_count = 1
            log.info('Shared broker created paper=%s pid=%s', use_paper, os.getpid())
            return _shared_broker
        _shared_use_count += 1
        return _shared_broker


def get_shared_broker_for_strategy(
    strategy_name: str,
    *,
    paper: bool,
    timeout_sec: Optional[float] = None,
    reset: bool = False,
):
    """One cached broker per (strategy_name, paper) pair per process."""
    global _strategy_brokers

    key = (tt_config.BROKER, bool(paper), strategy_name, os.getpid())

    with _shared_lock:
        if not reset and key in _strategy_brokers:
            return _strategy_brokers[key]

    # Build outside the lock — get_shared_broker() also acquires _shared_lock.
    if paper:
        from brokers.journal_broker import JournalBroker

        underlying = get_shared_broker(paper=False)
        broker = JournalBroker(underlying, strategy_name=strategy_name)
        log.info(
            'Strategy journal broker created strategy=%s (live data, no orders) pid=%s',
            strategy_name, os.getpid(),
        )
    else:
        broker = get_broker(paper=False)
        log.info(
            'Strategy broker created strategy=%s paper=%s pid=%s',
            strategy_name, paper, os.getpid(),
        )

    with _shared_lock:
        if reset or key not in _strategy_brokers:
            _strategy_brokers[key] = broker
        return _strategy_brokers[key]


def get_shared_broker_bounded(
    paper: Optional[bool] = None,
    *,
    timeout_sec: Optional[float] = None,
    reset: bool = False,
):
    """Create/reuse shared broker with a hard init timeout (broker create + account bootstrap)."""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    deadline = float(
        timeout_sec
        if timeout_sec is not None
        else os.environ.get('BROKER_INIT_TIMEOUT_SEC', '15')
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: get_shared_broker(paper=paper, reset=reset))
        try:
            return fut.result(timeout=max(0.1, deadline))
        except FuturesTimeout as exc:
            raise TimeoutError(
                f'shared broker init exceeded {deadline:.0f}s',
            ) from exc


def reset_shared_broker() -> None:
    global _shared_broker, _shared_key, _shared_created_at, _shared_use_count, _strategy_brokers
    with _shared_lock:
        _shared_broker = None
        _shared_key = None
        _shared_created_at = None
        _shared_use_count = 0
        _strategy_brokers = {}


def shared_broker_stats() -> dict:
    with _shared_lock:
        return {
            'has_shared': _shared_broker is not None,
            'created_at': _shared_created_at,
            'reuse_count': _shared_use_count,
            'key': _shared_key,
        }


def get_streamer_script() -> str:
    """Return path to streamer entry script relative to project root."""
    if tt_config.BROKER == 'tastytrade':
        return os.path.join('streaming', 'publish_tastytrade.py')
    return os.path.join('streaming', 'publish.py')


def get_mqtt_topic_prefix() -> str:
    if tt_config.BROKER == 'tastytrade':
        return 'TASTYTRADE/'
    return 'SCHWAB/'


def is_tastytrade_mode() -> bool:
    return tt_config.BROKER == 'tastytrade'


def use_thin_tranches() -> bool:
    """Thin tranche + stop_monitor architecture for TastyTrade."""
    return is_tastytrade_mode()
