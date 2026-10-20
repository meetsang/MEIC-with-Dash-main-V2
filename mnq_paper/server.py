"""Local Flask webhook + status page. Paper fills only."""
from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from flask import Flask, jsonify, request

from mnq_paper.engine import EngineError, PaperEngine, parse_side
from mnq_paper.models import utc_now_iso
from mnq_paper.quotes import FutureContract, fetch_rest_quote
from mnq_paper.signals import (
    build_signal_record,
    extract_action,
    extract_quantity,
    extract_strategy,
    normalize_payload,
    strategy_allowed,
)

log = logging.getLogger('mnq_paper.server')

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>MNQ Paper Signal Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090d14;
      --card-bg: rgba(18, 26, 40, 0.85);
      --card-border: rgba(45, 62, 88, 0.6);
      --text: #f0f4fc;
      --text-muted: #8b9cb5;
      --accent: #3b82f6;
      --accent-glow: rgba(59, 130, 246, 0.25);
      --green: #10b981;
      --green-glow: rgba(16, 185, 129, 0.2);
      --red: #ef4444;
      --red-glow: rgba(239, 68, 68, 0.2);
      --amber: #f59e0b;
    }
    * { box-sizing: border-box; }
    body {
      font-family: 'Plus Jakarta Sans', system-ui, sans-serif;
      background: var(--bg);
      background-image: radial-gradient(circle at 10% 20%, rgba(30, 58, 138, 0.15) 0%, transparent 40%),
                        radial-gradient(circle at 90% 80%, rgba(16, 185, 129, 0.08) 0%, transparent 40%);
      color: var(--text);
      margin: 0;
      padding: 28px 24px;
      min-height: 100vh;
    }
    .container { max-width: 1200px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .badge-paper {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.4);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }
    .badge-live {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.4);
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
    }
    h1 { font-size: 22px; font-weight: 800; margin: 0; }
    .sub { color: var(--text-muted); font-size: 13px; margin-top: 4px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; margin-bottom: 20px; }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 20px;
      backdrop-filter: blur(12px);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    }
    .card-title {
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .price-val {
      font-family: 'JetBrains Mono', monospace;
      font-size: 36px;
      font-weight: 700;
      color: #ffffff;
      margin: 8px 0;
    }
    .metric-row { display: flex; gap: 16px; font-size: 13px; color: var(--text-muted); }
    .metric-item { display: flex; flex-direction: column; gap: 2px; }
    .metric-item span:last-child { color: var(--text); font-family: 'JetBrains Mono', monospace; font-weight: 600; }
    .btn-flatten {
      background: #dc2626;
      color: #ffffff;
      border: none;
      padding: 8px 16px;
      border-radius: 8px;
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn-flatten:hover { background: #b91c1c; box-shadow: 0 0 12px var(--red-glow); }
    .tag-long {
      background: rgba(16, 185, 129, 0.2);
      color: #34d399;
      border: 1px solid #10b981;
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 700;
      font-size: 12px;
    }
    .tag-short {
      background: rgba(239, 68, 68, 0.2);
      color: #f87171;
      border: 1px solid #ef4444;
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 700;
      font-size: 12px;
    }
    .pnl-pos { color: #34d399; font-weight: 700; }
    .pnl-neg { color: #f87171; font-weight: 700; }
    code {
      font-family: 'JetBrains Mono', monospace;
      background: rgba(10, 15, 25, 0.8);
      border: 1px solid rgba(255, 255, 255, 0.08);
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 12px;
      color: #93c5fd;
    }
    pre {
      font-family: 'JetBrains Mono', monospace;
      background: rgba(10, 15, 25, 0.9);
      border: 1px solid var(--card-border);
      padding: 14px;
      border-radius: 10px;
      overflow-x: auto;
      font-size: 12px;
      color: #cbd5e1;
      max-height: 220px;
    }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th {
      text-align: left;
      padding: 10px 12px;
      color: var(--text-muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-bottom: 1px solid var(--card-border);
    }
    td {
      padding: 10px 12px;
      border-bottom: 1px solid rgba(45, 62, 88, 0.3);
      font-family: 'JetBrains Mono', monospace;
      font-size: 12.5px;
    }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="brand">
        <h1>MNQ Paper Signals</h1>
        <span class="badge-paper">Paper Trading Only</span>
        <span class="badge-live" id="streamer-badge">DXLink Connected</span>
      </div>
      <div>
        <span class="sub">Stop: <b>40 pts</b> | Target: <b>25 pts</b> | Tick: <b>0.25</b></span>
      </div>
    </header>

    <div class="grid">
      <!-- Quote Card -->
      <div class="card">
        <div class="card-title">
          <span>Market Quote</span>
          <span id="sym" style="color:#60a5fa">/MNQU6</span>
        </div>
        <div class="price-val" id="px">—</div>
        <div class="metric-row">
          <div class="metric-item"><span>Bid</span><span id="q-bid">—</span></div>
          <div class="metric-item"><span>Ask</span><span id="q-ask">—</span></div>
          <div class="metric-item"><span>Last</span><span id="q-last">—</span></div>
          <div class="metric-item"><span>Source</span><span id="q-src">—</span></div>
        </div>
      </div>

      <!-- Open Trade Card -->
      <div class="card">
        <div class="card-title">
          <span>Active Paper Trade</span>
          <button id="btn-flatten" class="btn-flatten" style="display:none" onclick="flattenTrade()">Flatten Position</button>
        </div>
        <div id="open-trade-body">
          <div style="color:var(--text-muted);margin:16px 0;font-size:14px;">No active position</div>
        </div>
      </div>

      <!-- Webhook Info Card -->
      <div class="card">
        <div class="card-title">
          <span>Webhook Endpoint</span>
          <span style="font-size:11px;color:#34d399">Ready</span>
        </div>
        <div style="margin:8px 0;">
          <code id="url">/signal</code>
        </div>
        <div class="sub" style="margin-top:10px;font-size:12px;">
          Accepts Bullish (<code>buy</code>) and Bearish (<code>sell</code>) signals.<br/>
          Auto bracket: <b>40 SL</b> / <b>25 PT</b>.
        </div>
      </div>
    </div>

    <!-- Closed Trades History -->
    <div class="card">
      <div class="card-title">
        <span>Recent Paper Trades History</span>
        <span id="closed-count" style="color:var(--text-muted);font-size:12px;">0 trades</span>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Strategy</th>
              <th>Side</th>
              <th>Qty</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Target / Stop</th>
              <th>Result</th>
              <th>PnL</th>
              <th>Signal Time</th>
              <th>Entry Time</th>
              <th>Exit Time</th>
            </tr>
          </thead>
          <tbody id="rows">
            <tr><td colspan="12" style="text-align:center;color:var(--text-muted);padding:24px;">No closed trades yet</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

<script>
async function flattenTrade() {
  if (!confirm('Flatten open paper position now?')) return;
  try {
    const res = await fetch('/flatten', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      tick();
    } else {
      alert('Error flattening: ' + (data.error || 'unknown'));
    }
  } catch (err) {
    alert('Request failed: ' + err);
  }
}

async function tick(){
  try {
    const res = await fetch('/status');
    const s = await res.json();
    const q = s.quote || {};
    
    document.getElementById('sym').textContent = q.symbol || s.symbol || '/MNQU6';
    const mark = q.last || q.mid || q.bid || '—';
    document.getElementById('px').textContent = typeof mark === 'number' ? mark.toFixed(2) : mark;
    document.getElementById('q-bid').textContent = q.bid ? q.bid.toFixed(2) : '—';
    document.getElementById('q-ask').textContent = q.ask ? q.ask.toFixed(2) : '—';
    document.getElementById('q-last').textContent = q.last ? q.last.toFixed(2) : '—';
    document.getElementById('q-src').textContent = q.source || '—';

    const base = s.public_url || location.origin;
    document.getElementById('url').textContent = base + '/signal';

    const open = s.open;
    const btnFlatten = document.getElementById('btn-flatten');
    const openBody = document.getElementById('open-trade-body');

    if (open) {
      btnFlatten.style.display = 'inline-block';
      const isLong = open.side === 'long';
      const sideTag = isLong ? '<span class="tag-long">LONG (BULLISH)</span>' : '<span class="tag-short">SHORT (BEARISH)</span>';
      const u = s.unrealized;
      let pnlHtml = '—';
      if (u) {
        const cls = u.points >= 0 ? 'pnl-pos' : 'pnl-neg';
        pnlHtml = `<span class="${cls}">${u.points >= 0 ? '+' : ''}${u.points} pts ($${u.usd})</span>`;
      }
      openBody.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
          <div>${sideTag} <b style="margin-left:8px;">${open.qty}x @ ${open.entry.toFixed(2)}</b></div>
          <div>${pnlHtml}</div>
        </div>
        <div class="metric-row" style="margin-bottom:10px;">
          <div class="metric-item"><span>Stop (-${open.stop_points || 40} pts)</span><span style="color:#f87171">${open.stop.toFixed(2)}</span></div>
          <div class="metric-item"><span>Target (+${open.target_points || 25} pts)</span><span style="color:#34d399">${open.target.toFixed(2)}</span></div>
          <div class="metric-item"><span>Strategy</span><span>${open.signal?.strategy || 'apexnq'}</span></div>
          <div class="metric-item"><span>Account</span><span>${open.signal?.account || 'Sim101'}</span></div>
        </div>
        <div class="sub" style="font-size:11px;">
          Signal: <code>${open.signal_received_at || open.opened_at}</code> | Entry: <code>${open.opened_at}</code>
        </div>
      `;
    } else {
      btnFlatten.style.display = 'none';
      openBody.innerHTML = `<div style="color:var(--text-muted);margin:16px 0;font-size:14px;">No active position — waiting for webhook</div>`;
    }

    const recent = s.recent || [];
    document.getElementById('closed-count').textContent = `${recent.length} trade${recent.length === 1 ? '' : 's'}`;
    if (recent.length > 0) {
      const rows = recent.slice().reverse().map(t => {
        const isWin = t.outcome === 'win';
        const isFlatten = t.outcome === 'flattened';
        const pnlCls = t.pnl_usd >= 0 ? 'pnl-pos' : 'pnl-neg';
        const tagCls = t.side === 'long' ? 'tag-long' : 'tag-short';
        const resCls = isWin ? 'pnl-pos' : (isFlatten ? 'color:#f59e0b' : 'pnl-neg');
        const strat = t.signal?.strategy || 'apexnq';
        return `<tr>
          <td><code>${t.id}</code></td>
          <td>${strat}</td>
          <td><span class="${tagCls}">${t.side.toUpperCase()}</span></td>
          <td>${t.qty}</td>
          <td>${t.entry.toFixed(2)}</td>
          <td>${t.exit.toFixed(2)}</td>
          <td>${t.target.toFixed(2)} / ${t.stop.toFixed(2)}</td>
          <td class="${resCls}">${t.outcome.toUpperCase()} (${t.reason})</td>
          <td class="${pnlCls}">${t.pnl_usd >= 0 ? '+' : ''}$${t.pnl_usd} (${t.pnl_points} pts)</td>
          <td style="font-size:11px;color:var(--text-muted)">${t.signal_received_at || '—'}</td>
          <td style="font-size:11px;color:var(--text-muted)">${t.opened_at || '—'}</td>
          <td style="font-size:11px;color:var(--text-muted)">${t.closed_at || '—'}</td>
        </tr>`;
      }).join('');
      document.getElementById('rows').innerHTML = rows;
    } else {
      document.getElementById('rows').innerHTML = `<tr><td colspan="12" style="text-align:center;color:var(--text-muted);padding:24px;">No closed trades yet</td></tr>`;
    }
  } catch (err) {
    console.error('Poll error', err);
  }
}
tick();
setInterval(tick, 1500);
</script>
</body>
</html>
"""


def _extract_side_from_payload(payload: Any) -> str:
    norm = normalize_payload(payload)
    return extract_action(norm)


def create_app(
    engine: PaperEngine,
    *,
    session=None,
    contract: Optional[FutureContract] = None,
    loop=None,
) -> Flask:
    app = Flask('mnq_paper')
    secret = (engine.settings.webhook_secret or '').strip()

    def _authorized(payload_dict: Optional[dict] = None) -> bool:
        if not secret:
            return True
        header = request.headers.get('X-Webhook-Secret') or ''
        auth = request.headers.get('Authorization') or ''
        token = header
        if auth.lower().startswith('bearer '):
            token = auth.split(' ', 1)[1].strip()
        if request.args.get('secret'):
            token = request.args.get('secret') or token
        if not token and payload_dict and isinstance(payload_dict, dict):
            token = str(payload_dict.get('secret') or payload_dict.get('passphrase') or payload_dict.get('key') or '')
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

    @app.route('/signal', methods=['POST'])
    @app.route('/webhook', methods=['POST'])
    def signal():
        # 1. Record the exact signal received timestamp immediately
        signal_received_at = utc_now_iso()

        # 2. Extract payload body
        raw_payload: Any = request.get_json(silent=True)
        if raw_payload is None:
            raw_payload = request.form.to_dict() or {}
            if request.data and not raw_payload:
                try:
                    import json
                    raw_payload = json.loads(request.data.decode('utf-8', errors='replace'))
                except Exception:
                    raw_payload = request.data.decode('utf-8', errors='replace')

        payload_dict = normalize_payload(raw_payload)

        # 3. Check authorization
        if not _authorized(payload_dict):
            return jsonify({'error': 'unauthorized'}), 401

        # 4. Check strategy filter if configured
        if engine.settings.allowed_strategy:
            if not strategy_allowed(payload_dict, engine.settings.allowed_strategy):
                strat = extract_strategy(payload_dict)
                log.warning('Rejected signal with strategy %r (allowed=%r)', strat, engine.settings.allowed_strategy)
                return jsonify({
                    'error': f'Strategy {strat!r} not allowed (expected {engine.settings.allowed_strategy!r})',
                }), 400

        # 5. Extract action / direction
        side_raw = extract_action(payload_dict)
        if not side_raw and isinstance(raw_payload, str):
            side_raw = raw_payload.strip()
        try:
            parse_side(side_raw)
        except EngineError as exc:
            return jsonify({'error': str(exc)}), 400

        # 6. Extract quantity, stop_points, target_points, notes
        qty = extract_quantity(payload_dict)
        stop_points = None
        target_points = None
        note = ''
        if isinstance(payload_dict, dict):
            raw_stop = payload_dict.get('stop_points') or payload_dict.get('stop')
            if raw_stop is not None and str(raw_stop).strip():
                try:
                    stop_points = float(str(raw_stop).strip())
                except ValueError:
                    pass
            raw_target = payload_dict.get('target_points') or payload_dict.get('target')
            if raw_target is not None and str(raw_target).strip():
                try:
                    target_points = float(str(raw_target).strip())
                except ValueError:
                    pass
            note = str(payload_dict.get('note') or payload_dict.get('comment') or '')

        # Build full signal record with received_at
        signal_record = build_signal_record(payload_dict, received_at=signal_received_at)

        # 7. Obtain paper fill and open trade
        try:
            fill, source = _rest_fill(side_raw)
            trade = engine.open_signal(
                side_raw,
                fill=fill,
                qty=qty,
                stop_points=stop_points,
                target_points=target_points,
                note=note,
                entry_source=source,
                signal_received_at=signal_received_at,
                signal=signal_record,
            )
        except EngineError as exc:
            return jsonify({'error': str(exc), 'status': engine.status()}), exc.status

        log.info(
            'PAPER OPEN %s %sx @ %s stop=%s target=%s (SL=%s PT=%s) src=%s recv=%s',
            trade.side.upper(),
            trade.qty,
            trade.entry,
            trade.stop,
            trade.target,
            trade.stop_points,
            trade.target_points,
            trade.entry_source,
            trade.signal_received_at,
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
        log.info('PAPER FLATTEN trade=%s exit=%s pnl=$%s', closed.id, closed.exit, closed.pnl_usd)
        return jsonify({'ok': True, 'trade': closed.to_dict()})

    return app

