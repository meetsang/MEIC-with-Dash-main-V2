"""Local Flask webhook + status page. Paper fills only."""
from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from flask import Flask, jsonify, request

from mnq_paper.engine import EngineError, PaperEngine, parse_side
from mnq_paper.quotes import FutureContract, fetch_rest_quote

log = logging.getLogger('mnq_paper.server')

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>MNQ paper bot</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, sans-serif; background:#0f1419; color:#e7ecf3; margin:0; padding:24px; }
    h1 { font-size:20px; margin:0 0 8px; }
    .sub { color:#8b98a5; margin-bottom:20px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
    .card { background:#1a2332; border:1px solid #2a3545; border-radius:12px; padding:16px; }
    .ok { color:#3dd68c; } .bad { color:#ff6b6b; } .warn { color:#ffd166; }
    table { width:100%; border-collapse:collapse; font-size:13px; }
    th, td { text-align:left; padding:6px 8px; border-bottom:1px solid #2a3545; }
    code { background:#0f1419; padding:2px 6px; border-radius:4px; }
    pre { background:#0f1419; padding:12px; border-radius:8px; overflow:auto; font-size:12px; }
  </style>
</head>
<body>
  <h1>MNQ paper bot</h1>
  <div class="sub">Prices from TastyTrade. <b>No orders are sent to the broker.</b></div>
  <div class="grid">
    <div class="card">
      <div>Quote <span id="sym"></span></div>
      <div style="font-size:28px;margin:8px 0" id="px">—</div>
      <div id="ba" class="sub"></div>
    </div>
    <div class="card">
      <div>Open trade</div>
      <pre id="open">none</pre>
      <div id="u"></div>
    </div>
    <div class="card">
      <div>Webhook</div>
      <div style="margin-top:8px">POST <code id="url"></code></div>
      <div class="sub" style="margin-top:8px">Header <code>X-Webhook-Secret</code></div>
    </div>
  </div>
  <div class="card" style="margin-top:16px">
    <div>Recent paper trades</div>
    <table>
      <thead><tr><th>id</th><th>side</th><th>entry</th><th>exit</th><th>result</th><th>pnl</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
<script>
async function tick(){
  const s = await (await fetch('/status')).json();
  const q = s.quote || {};
  document.getElementById('sym').textContent = q.symbol || s.symbol || '';
  const mark = q.last || q.mid || q.bid || '—';
  document.getElementById('px').textContent = mark;
  document.getElementById('ba').textContent = `bid ${q.bid ?? '—'}  ask ${q.ask ?? '—'}  src ${q.source ?? ''}`;
  document.getElementById('open').textContent = s.open ? JSON.stringify(s.open, null, 2) : 'none';
  document.getElementById('u').textContent = s.unrealized ? `unrealized ${s.unrealized.points} pts  $${s.unrealized.usd}` : '';
  const base = s.public_url || location.origin;
  document.getElementById('url').textContent = base + '/signal';
  const rows = (s.recent || []).slice().reverse().map(t => `<tr>
    <td>${t.id}</td><td>${t.side}</td><td>${t.entry}</td><td>${t.exit}</td>
    <td class="${t.success ? 'ok' : (t.outcome==='flattened'?'warn':'bad')}">${t.outcome} (${t.reason})</td>
    <td>$${t.pnl_usd}</td></tr>`).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="6">no closed trades yet</td></tr>';
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""


def _extract_side(payload: Any) -> str:
    if payload is None:
        return ''
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ''
    for key in ('side', 'action', 'signal', 'direction', 'order'):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    strategy = payload.get('strategy')
    if isinstance(strategy, dict):
        order = strategy.get('order') or {}
        if isinstance(order, dict):
            action = order.get('action') or order.get('side')
            if action:
                return str(action)
    return ''


def create_app(
    engine: PaperEngine,
    *,
    session=None,
    contract: Optional[FutureContract] = None,
    loop=None,
) -> Flask:
    app = Flask('mnq_paper')
    secret = engine.settings.webhook_secret

    def _authorized() -> bool:
        header = request.headers.get('X-Webhook-Secret') or ''
        auth = request.headers.get('Authorization') or ''
        token = header
        if auth.lower().startswith('bearer '):
            token = auth.split(' ', 1)[1].strip()
        if request.args.get('secret'):
            token = request.args.get('secret') or token
        return hmac.compare_digest(token, secret)

    def _rest_fill(side: str):
        if session is None or contract is None or loop is None:
            return None, 'streamer'
        import asyncio
        try:
            fut = asyncio.run_coroutine_threadsafe(fetch_rest_quote(session, contract), loop)
            quote = fut.result(timeout=8)
        except Exception as exc:
            log.warning('REST fill failed, using streamer: %s', exc)
            return None, 'streamer'
        if quote is None:
            return None, 'streamer'
        engine.update_quote(quote)
        return quote.market_fill(parse_side(side)), 'rest'

    @app.get('/')
    def dashboard():
        return DASHBOARD_HTML

    @app.get('/status')
    def status():
        return jsonify(engine.status())

    @app.get('/health')
    def health():
        st = engine.status()
        return jsonify({
            'ok': True,
            'paper': True,
            'has_quote': st['quote'] is not None,
            'open': st['open'] is not None,
        })

    @app.post('/signal')
    def signal():
        if not _authorized():
            return jsonify({'error': 'unauthorized'}), 401
        payload: Any = request.get_json(silent=True)
        if payload is None:
            payload = request.form.to_dict() or {}
            if request.data and not payload:
                payload = request.data.decode('utf-8', errors='replace')
        side_raw = _extract_side(payload)
        if not side_raw and isinstance(payload, dict):
            side_raw = str(payload.get('ticker_side') or '')
        try:
            parse_side(side_raw)
        except EngineError as exc:
            return jsonify({'error': str(exc)}), 400

        qty = stop_points = target_points = None
        note = ''
        if isinstance(payload, dict):
            qty = payload.get('qty') or payload.get('quantity')
            stop_points = payload.get('stop_points') or payload.get('stop')
            target_points = payload.get('target_points') or payload.get('target')
            note = str(payload.get('note') or payload.get('comment') or '')
        try:
            fill, source = _rest_fill(side_raw)
            trade = engine.open_signal(
                side_raw,
                fill=fill,
                qty=None if qty is None else int(qty),
                stop_points=None if stop_points is None else float(stop_points),
                target_points=None if target_points is None else float(target_points),
                note=note,
                entry_source=source,
            )
        except EngineError as exc:
            return jsonify({'error': str(exc), 'status': engine.status()}), exc.status
        log.info(
            'PAPER OPEN %s %s @ %s stop=%s target=%s src=%s',
            trade.side,
            trade.qty,
            trade.entry,
            trade.stop,
            trade.target,
            trade.entry_source,
        )
        return jsonify({'ok': True, 'trade': trade.to_dict(), 'status': engine.status()})

    @app.post('/flatten')
    def flatten():
        if not _authorized():
            return jsonify({'error': 'unauthorized'}), 401
        try:
            closed = engine.flatten()
        except EngineError as exc:
            return jsonify({'error': str(exc)}), exc.status
        return jsonify({'ok': True, 'trade': closed.to_dict()})

    return app
