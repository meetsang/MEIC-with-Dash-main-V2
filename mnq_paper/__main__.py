"""Run the MNQ paper webhook + TastyTrade price streamer.

Never places orders. REST is used for the entry snapshot; DXLink watches
the paper stop / profit target.

    uv run python -m mnq_paper
    uv run python -m mnq_paper --no-ngrok
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import common.win_ssl_env  # noqa: F401

from mnq_paper.config import load_settings
from mnq_paper.engine import PaperEngine
from mnq_paper.journal import Journal
from mnq_paper.ngrok_tunnel import start_ngrok
from mnq_paper.quotes import fetch_rest_quote, resolve_contract
from mnq_paper.server import create_app
from mnq_paper.streamer import stream_mnq

log = logging.getLogger('mnq_paper')


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [MNQ-PAPER] %(levelname)s %(message)s',
    )


def _start_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_run, name='mnq-aio', daemon=True)
    thread.start()
    return loop


async def _boot(session, settings, engine):
    if hasattr(session, 'validate'):
        await session.validate()
    contract = await resolve_contract(session, settings.product_code, settings.rest_symbol)
    log.info(
        'Contract %s streamer=%s expiry=%s',
        contract.symbol,
        contract.streamer_symbol,
        contract.expiration_date,
    )
    try:
        rest = await fetch_rest_quote(session, contract)
        if rest is not None:
            engine.update_quote(rest)
    except Exception as exc:
        log.warning('Initial REST quote failed: %s', exc)
    stop_event = asyncio.Event()
    asyncio.create_task(stream_mnq(session, contract, engine, stop_event))
    return contract, stop_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='MNQ paper webhook bot (TastyTrade prices only)')
    parser.add_argument('--no-ngrok', action='store_true', help='Do not start an ngrok tunnel')
    parser.add_argument('--host', default=None)
    parser.add_argument('--port', type=int, default=None)
    args = parser.parse_args(argv)
    _setup_logging()

    settings = load_settings()
    journal = Journal(settings.journal_path, settings.open_path)
    engine = PaperEngine(settings, journal)
    if engine.open is not None:
        log.info('Resumed open paper trade %s %s @ %s', engine.open.id, engine.open.side, engine.open.entry)

    from common.tt_auth import create_tastytrade_session

    # Live TastyTrade session for market data only. Local fills stay paper.
    session = create_tastytrade_session(paper=False)
    loop = _start_loop()

    try:
        contract, stop_event = asyncio.run_coroutine_threadsafe(
            _boot(session, settings, engine),
            loop,
        ).result(timeout=45)
    except Exception:
        log.exception('Failed to start TastyTrade market data')
        return 1

    app = create_app(engine, session=session, contract=contract, loop=loop)
    host = args.host or settings.host
    port = args.port or settings.port

    public = None
    if settings.ngrok_enabled and not args.no_ngrok:
        public = start_ngrok(port, settings.ngrok_authtoken)
        engine.set_public_url(public)

    log.info('PAPER MODE — broker orders disabled')
    log.info('Local dashboard http://127.0.0.1:%s', port)
    if public:
        log.info('Friend webhook: POST %s/signal  header X-Webhook-Secret', public)
    else:
        log.info('No ngrok URL. Share http://127.0.0.1:%s/signal on LAN, or install ngrok.', port)
    log.info('Webhook secret is in %s (or MNQ_WEBHOOK_SECRET)', settings.secret_path)

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        loop.call_soon_threadsafe(stop_event.set)
        loop.call_soon_threadsafe(loop.stop)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
