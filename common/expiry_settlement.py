"""0DTE spread settlement at expiry using SPX cash close (3 PM CT).

Credit spreads that expire fully OTM are worth $0 — full credit kept.
Debit spreads that expire OTM are worth $0 — full premium lost.
When the short leg is ITM, intrinsic value is used for the spread at expiry.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
from datetime import date, datetime, time
from typing import Any, Dict, Optional, Tuple

_SYMBOL_EXPIRY_RE = re.compile(r'SPXW?(\d{6})', re.IGNORECASE)


def spread_intrinsic_at_expiry(
    side: str,
    short_strike: float,
    long_strike: float,
    spx_close: float,
) -> float:
    """Per-share spread debit at expiry (what it costs to close)."""
    side = (side or 'P').upper()
    spx = float(spx_close)
    short_k = float(short_strike)
    long_k = float(long_strike)
    if side == 'P':
        short_intr = max(0.0, short_k - spx)
        long_intr = max(0.0, long_k - spx)
    else:
        short_intr = max(0.0, spx - short_k)
        long_intr = max(0.0, spx - long_k)
    return round(max(0.0, short_intr - long_intr), 2)


def short_leg_itm(side: str, short_strike: float, spx_close: float) -> bool:
    side = (side or 'P').upper()
    spx = float(spx_close)
    short_k = float(short_strike)
    if side == 'P':
        return spx < short_k
    return spx > short_k


def _expiry_from_trade(trade: Dict[str, Any]) -> Optional[date]:
    for leg_key in ('short_leg', 'long_leg'):
        sym = (trade.get(leg_key) or {}).get('symbol', '')
        m = _SYMBOL_EXPIRY_RE.search(sym or '')
        if m:
            yy, mm, dd = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
            return date(2000 + int(yy), int(mm), int(dd))
    ts = (trade.get('entry') or {}).get('timestamp', '')
    if ts:
        try:
            return date.fromisoformat(ts[:10])
        except ValueError:
            pass
    return None


def settlement_cutoff_reached(for_date: date, now: Optional[datetime] = None) -> bool:
    """True after 3:00 PM Central on expiry day, or any time on a later calendar day."""
    if now is None:
        from meic0dte.app.utilities import central_now
        now = central_now()
    today = now.date()
    if today > for_date:
        return True
    if today < for_date:
        return False
    return now.time() >= time(15, 0)


def _spx_csv_candidates(root: str) -> list[str]:
    here = os.path.abspath(root)
    candidates = []
    for _ in range(6):
        candidates.append(os.path.join(here, 'index-ohlc-downloader', 'data', 'SPX_daily_ohlc.csv'))
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return candidates


def get_spx_settlement_close(for_date: date, root: Optional[str] = None) -> Optional[float]:
    """SPX cash close for settlement — file override, then daily OHLC CSV."""
    root = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    trades_root = os.path.join(root, 'trades')
    for pattern in (
        os.path.join(trades_root, 'settlement', f'{for_date.isoformat()}.json'),
        os.path.join(trades_root, f'spx_settlement_{for_date.isoformat()}.json'),
    ):
        if os.path.isfile(pattern):
            try:
                with open(pattern, encoding='utf-8') as f:
                    data = json.load(f)
                val = data.get('spx_close') or data.get('close')
                if val is not None:
                    return float(val)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass

    iso = for_date.isoformat()
    for csv_path in _spx_csv_candidates(root):
        if not os.path.isfile(csv_path):
            continue
        try:
            with open(csv_path, encoding='utf-8', newline='') as f:
                for row in csv.DictReader(f):
                    if (row.get('date') or '').strip() == iso:
                        return float(row['close'])
        except (OSError, KeyError, TypeError, ValueError):
            continue

    val = _spx_from_market_data(root, for_date)
    if val is not None:
        return val

    from meic0dte.app.utilities import central_now
    today = central_now().date()
    if for_date < today:
        val = _spx_from_trade_snapshots(root, for_date)
        if val is not None:
            return val
    else:
        val = _spx_from_mqtt_snapshot()
        if val is not None:
            return val
        val = _spx_from_trade_snapshots(root, for_date)
        if val is not None:
            return val
    return None


def _spx_from_market_data(root: str, for_date: date) -> Optional[float]:
    """Last SPX poll/1m bar at or before 3:00 PM CT from market_data recorder."""
    day_dir = os.path.join(root, 'data', for_date.isoformat())
    if not os.path.isdir(day_dir):
        return None
    cutoff = f'{for_date.isoformat()} 15:00:00'
    for name, col in (('SPX_polls.csv', 'price'), ('SPX_1m.csv', 'close')):
        path = os.path.join(day_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            last = None
            with open(path, encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts = (row.get('timestamp') or row.get('datetime') or '').strip()
                    if ts and ts <= cutoff:
                        last = float(row[col])
            if last is not None:
                return last
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return None


def _spx_from_mqtt_snapshot() -> Optional[float]:
    try:
        from common.mqtt_prices import get_shared_cache
        cache = get_shared_cache()
        if not cache.is_running():
            cache.start()
        spx = cache.get_spx()
        if spx is not None and float(spx) > 1000:
            return float(spx)
    except Exception:
        pass
    return None


def _parse_event_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except ValueError:
        try:
            return datetime.strptime(ts[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return None


def _spx_from_trade_snapshots(root: str, for_date: date) -> Optional[float]:
    """Best-effort SPX from stop_history spx_price_at_event on the session date."""
    iso = for_date.isoformat()
    best_ts: Optional[datetime] = None
    best_spx: Optional[float] = None
    for path in iter_trade_json_paths(root, for_date):
        try:
            with open(path, encoding='utf-8') as f:
                trade = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        for ev in trade.get('stop_history') or []:
            spx = ev.get('spx_price_at_event')
            ts = ev.get('timestamp') or ''
            if spx is None or not str(ts).startswith(iso):
                continue
            try:
                val = float(spx)
            except (TypeError, ValueError):
                continue
            if val < 1000:
                continue
            evt = _parse_event_ts(ts)
            if evt is None:
                continue
            if evt.hour > 15 or (evt.hour == 15 and evt.minute > 0):
                continue
            if best_ts is None or evt > best_ts:
                best_ts = evt
                best_spx = val
    return best_spx


def ensure_spx_settlement_close(for_date: date, root: Optional[str] = None) -> Optional[float]:
    """Resolve SPX SET for a date and persist trades/settlement/YYYY-MM-DD.json."""
    root = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    settlement_path = os.path.join(root, 'trades', 'settlement', f'{for_date.isoformat()}.json')
    if os.path.isfile(settlement_path):
        try:
            with open(settlement_path, encoding='utf-8') as f:
                data = json.load(f)
            val = data.get('spx_close') or data.get('close')
            if val is not None:
                return float(val)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    spx = get_spx_settlement_close(for_date, root=root)
    if spx is None:
        return None
    write_spx_settlement_close(for_date, spx, root=root)
    return spx


def write_spx_settlement_close(for_date: date, spx_close: float, root: Optional[str] = None) -> str:
    root = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    directory = os.path.join(root, 'trades', 'settlement')
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f'{for_date.isoformat()}.json')
    payload = {
        'date': for_date.isoformat(),
        'spx_close': float(spx_close),
        'source': 'manual',
        'note': '3 PM CT SPX cash close for 0DTE expiry settlement',
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.write('\n')
    return path


def settled_close_prices(
    side: str,
    short_strike: float,
    long_strike: float,
    spx_close: float,
) -> Tuple[float, float]:
    """Synthetic leg marks at expiry when spread expires or is ITM."""
    side = (side or 'P').upper()
    spx = float(spx_close)
    short_k = float(short_strike)
    long_k = float(long_strike)
    if side == 'P':
        short_close = round(max(0.0, short_k - spx), 2)
        long_close = round(max(0.0, long_k - spx), 2)
    else:
        short_close = round(max(0.0, spx - short_k), 2)
        long_close = round(max(0.0, spx - long_k), 2)
    return short_close, long_close


def has_real_close_fills(trade: Dict[str, Any]) -> bool:
    """True when the trade was closed with recorded leg fill prices."""
    status = (trade.get('status') or '').lower()
    sc = trade.get('short_close_price')
    lc = trade.get('long_close_price')
    return status == 'closed' and sc is not None and lc is not None


def compute_otm_decay_pnl(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Full OTM expiry: credit spread keeps full credit; debit spread loses full premium."""
    entry = trade.get('entry') or {}
    filled_qty = int(trade.get('filled_quantity') or trade.get('quantity') or 0)
    if filled_qty <= 0:
        return None

    spread_type = (trade.get('spread_type') or 'credit').lower()
    net_open = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
    if spread_type == 'debit':
        pnl = round(-net_open * 100 * filled_qty, 2)
    else:
        pnl = round(net_open * 100 * filled_qty, 2)

    return {
        'close_debit': 0.0,
        'short_close_price': 0.0,
        'long_close_price': 0.0,
        'pnl': pnl,
        'status': 'CLOSED',
        'settled': True,
    }


def compute_settled_pnl(
    trade: Dict[str, Any],
    spx_close: float,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Return close fields + pnl when trade can be settled at expiry; else None."""
    entry = trade.get('entry') or {}
    short_leg = trade.get('short_leg') or {}
    long_leg = trade.get('long_leg') or {}
    side = (entry.get('side') or trade.get('side') or 'P').upper()
    short_strike = short_leg.get('strike')
    long_strike = long_leg.get('strike')
    if short_strike is None or long_strike is None:
        return None

    filled_qty = int(trade.get('filled_quantity') or trade.get('quantity') or 0)
    if filled_qty <= 0:
        return None

    spread_type = (trade.get('spread_type') or 'credit').lower()
    net_open = float(entry.get('net_credit') or entry.get('limit_credit') or 0)

    status = (trade.get('status') or '').lower()
    sc = trade.get('short_close_price')
    lc = trade.get('long_close_price')
    if has_real_close_fills(trade):
        close_debit = round(float(sc) - float(lc), 2)
        if spread_type == 'debit':
            pnl = round((close_debit - net_open) * 100 * filled_qty, 2)
        else:
            pnl = round((net_open - close_debit) * 100 * filled_qty, 2)
        return {
            'close_debit': close_debit,
            'short_close_price': float(sc),
            'long_close_price': float(lc),
            'pnl': pnl,
            'status': 'CLOSED',
            'settled': False,
        }

    expiry = _expiry_from_trade(trade)
    if expiry is None or not settlement_cutoff_reached(expiry, now=now):
        return None

    close_debit = spread_intrinsic_at_expiry(side, short_strike, long_strike, spx_close)
    short_close, long_close = settled_close_prices(side, short_strike, long_strike, spx_close)
    if spread_type == 'debit':
        pnl = round((close_debit - net_open) * 100 * filled_qty, 2)
    else:
        pnl = round((net_open - close_debit) * 100 * filled_qty, 2)

    return {
        'close_debit': close_debit,
        'short_close_price': short_close,
        'long_close_price': long_close,
        'pnl': pnl,
        'status': 'CLOSED',
        'settled': True,
        'spx_close': float(spx_close),
        'short_itm': short_leg_itm(side, short_strike, spx_close),
    }


def trade_to_history_row(
    trade: Dict[str, Any],
    spx_close: Optional[float] = None,
    *,
    assume_otm_expiry: bool = False,
) -> Optional[Dict[str, Any]]:
    """Map trade JSON to dashboard/db upsert payload."""
    entry = trade.get('entry') or {}
    short_leg = trade.get('short_leg') or {}
    long_leg = trade.get('long_leg') or {}
    side = (entry.get('side') or trade.get('side') or '').upper()
    lot = trade.get('lot') or entry.get('lot') or ''
    if not lot or not side:
        return None

    ts = entry.get('timestamp') or ''
    date_opened = ts[:10] if ts else date.today().isoformat()

    if has_real_close_fills(trade):
        settled = compute_settled_pnl(trade, spx_close or 0.0)
    elif assume_otm_expiry:
        expiry = _expiry_from_trade(trade)
        if expiry is None or not settlement_cutoff_reached(expiry):
            return None
        settled = compute_otm_decay_pnl(trade)
    else:
        spx = spx_close
        if spx is None:
            expiry = _expiry_from_trade(trade)
            if expiry is not None:
                spx = get_spx_settlement_close(expiry)
        settled = compute_settled_pnl(trade, spx) if spx is not None else None

    if settled is None:
        return None

    net_open = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
    quantity = int(trade.get('filled_quantity') or trade.get('quantity') or 1)
    entry_strategy = entry.get('strategy') or trade.get('strategy')
    from common import trades_layout

    strategy = trades_layout.STRATEGY_MEIC
    if entry_strategy in (trades_layout.STRATEGY_MEIC, trades_layout.STRATEGY_MANUAL):
        strategy = entry_strategy
    elif (lot or '').strip().lower().startswith('ms'):
        strategy = trades_layout.STRATEGY_MANUAL

    return {
        'date_opened': date_opened,
        'time_opened': ts,
        'strategy': strategy,
        'lot': lot,
        'side': side,
        'short_symbol': short_leg.get('symbol', ''),
        'long_symbol': long_leg.get('symbol', ''),
        'quantity': quantity,
        'filled_price': net_open,
        'short_open_price': float(short_leg.get('fill_price') or 0),
        'long_open_price': float(long_leg.get('fill_price') or 0),
        'short_close_price': settled['short_close_price'],
        'long_close_price': settled['long_close_price'],
        'close_debit': settled['close_debit'],
        'pnl': settled['pnl'],
        'status': settled['status'],
        'open_order_id': trade.get('open_order_id', ''),
        'short_close_order_id': trade.get('short_close_order_id', ''),
        'settled_at_expiry': settled.get('settled', False),
    }


def iter_trade_json_paths(root: str, for_date: Optional[date] = None) -> list[str]:
    """Active + dated history JSON paths for MEIC and Manual."""
    from common import trades_layout

    paths: list[str] = []
    base = root or trades_layout.project_root()
    strategies = (
        (trades_layout.STRATEGY_MEIC, trades_layout.MEIC_ACTIVE, trades_layout.MEIC_HISTORY),
        (trades_layout.STRATEGY_MANUAL, trades_layout.MANUAL_ACTIVE, trades_layout.MANUAL_HISTORY),
    )
    iso = for_date.isoformat() if for_date else None
    for _strategy, active_rel, history_rel in strategies:
        active = os.path.join(base, active_rel)
        history = os.path.join(base, history_rel)
        if os.path.isdir(active):
            paths.extend(sorted(glob.glob(os.path.join(active, '*.json'))))
        if iso and os.path.isdir(os.path.join(history, iso)):
            paths.extend(sorted(glob.glob(os.path.join(history, iso, '*.json'))))
        if os.path.isdir(history):
            paths.extend(sorted(glob.glob(os.path.join(history, '*.json'))))
    # de-dupe while preserving order
    seen = set()
    out = []
    for p in paths:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def iter_all_trade_json_paths(root: str) -> list[str]:
    """Every trade JSON under active + history trees (recursive)."""
    from common import trades_layout

    paths: list[str] = []
    base = root or trades_layout.project_root()
    for active_rel, history_rel in (
        (trades_layout.MEIC_ACTIVE, trades_layout.MEIC_HISTORY),
        (trades_layout.MANUAL_ACTIVE, trades_layout.MANUAL_HISTORY),
    ):
        for rel in (active_rel, history_rel):
            directory = os.path.join(base, rel)
            if os.path.isdir(directory):
                paths.extend(sorted(glob.glob(os.path.join(directory, '**', '*.json'), recursive=True)))
    seen = set()
    out = []
    for p in paths:
        norm = os.path.normpath(p)
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
