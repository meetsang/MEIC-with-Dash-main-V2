"""Broker factory — returns the correct broker and streamer for the configured BROKER."""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from common import tt_config

log = logging.getLogger(__name__)


def get_broker(paper: Optional[bool] = None):
    """Instantiate broker for current BROKER env setting."""
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
            'Set BROKER=tastytrade for the new broker abstraction.'
        )

    raise ValueError(f'Unknown BROKER: {broker_name}')


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
