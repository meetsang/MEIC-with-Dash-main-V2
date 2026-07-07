"""GEX dashboard API handlers."""
from __future__ import annotations

import logging
from datetime import date

from flask import jsonify, request

from common.broker_factory import get_shared_broker
from gex_data import (
    DEFAULT_STRIKE_PCT,
    MAX_HEATMAP_EXPIRIES,
    fetch_gex,
    fetch_gex_heatmap,
    list_expirations,
)

log = logging.getLogger(__name__)

SUPPORTED_TICKERS = ['SPX', 'SPY', 'QQQ', 'IWM', 'NDX', 'RUT']


def register_gex_routes(app) -> None:
    @app.route('/api/gex/tickers')
    def gex_tickers():
        return jsonify({'tickers': SUPPORTED_TICKERS})

    @app.route('/api/gex/expirations')
    def gex_expirations():
        ticker = (request.args.get('ticker') or 'SPX').upper()
        try:
            broker = get_shared_broker()
            expiries = broker._run(list_expirations(broker.session, ticker))
            return jsonify({'ticker': ticker, 'expiries': expiries})
        except Exception as exc:
            log.exception('gex expirations failed')
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/gex/calculate')
    def gex_calculate():
        ticker = (request.args.get('ticker') or 'SPX').upper()
        expiry_str = request.args.get('expiry', '')
        strike_pct = float(request.args.get('strike_pct', DEFAULT_STRIKE_PCT))

        if not expiry_str:
            return jsonify({'error': 'expiry is required (YYYY-MM-DD)'}), 400
        try:
            expiry = date.fromisoformat(expiry_str)
        except ValueError:
            return jsonify({'error': 'invalid expiry format; use YYYY-MM-DD'}), 400

        try:
            broker = get_shared_broker()
            result = fetch_gex(broker, ticker, expiry, strike_pct=strike_pct)
            return jsonify(result)
        except Exception as exc:
            log.exception('gex calculate failed')
            return jsonify({'error': str(exc)}), 500

    @app.route('/api/gex/heatmap')
    def gex_heatmap():
        ticker = (request.args.get('ticker') or 'SPX').upper()
        strike_pct = float(request.args.get('strike_pct', DEFAULT_STRIKE_PCT))
        max_expiries = int(request.args.get('max_expiries', MAX_HEATMAP_EXPIRIES))
        try:
            broker = get_shared_broker()
            result = fetch_gex_heatmap(
                broker, ticker, strike_pct=strike_pct, max_expiries=max_expiries
            )
            return jsonify(result)
        except Exception as exc:
            log.exception('gex heatmap failed')
            return jsonify({'error': str(exc)}), 500
