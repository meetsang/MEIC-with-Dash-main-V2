"""
MEIC Trade Database (SQLite)
All historical trade records are stored in dashboard/meic_trades.db
"""
import sqlite3, os
from datetime import datetime as dt, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), 'meic_trades.db')

CENTRAL_STD_OFFSET = -6
CENTRAL_DST_OFFSET = -5


def _nth_weekday_of_month(year, month, weekday, n):
    first_day = dt(year, month, 1)
    days_until_weekday = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_weekday + 7 * (n - 1))


def _central_dst_bounds(year):
    dst_start = _nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    dst_end = _nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    return dst_start, dst_end


def central_now_iso():
    utc_now = dt.now(timezone.utc)
    dst_start_local, dst_end_local = _central_dst_bounds(utc_now.year)
    dst_start_utc = (dst_start_local - timedelta(hours=CENTRAL_STD_OFFSET)).replace(tzinfo=timezone.utc)
    dst_end_utc = (dst_end_local - timedelta(hours=CENTRAL_DST_OFFSET)).replace(tzinfo=timezone.utc)
    offset = CENTRAL_DST_OFFSET if dst_start_utc <= utc_now < dst_end_utc else CENTRAL_STD_OFFSET
    return (utc_now + timedelta(hours=offset)).strftime('%Y-%m-%d %H:%M:%S')

def central_today_str():
    return central_now_iso()[:10]

from common import trades_layout

STRATEGY_MEIC = trades_layout.STRATEGY_MEIC
STRATEGY_MANUAL = trades_layout.STRATEGY_MANUAL


def infer_strategy(lot: str, entry_strategy: str | None = None) -> str:
    if entry_strategy in (STRATEGY_MEIC, STRATEGY_MANUAL):
        return entry_strategy
    text = (lot or '').strip().lower()
    if text.startswith('ms-') or text.startswith('ms'):
        return STRATEGY_MANUAL
    return STRATEGY_MEIC


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date_opened      TEXT NOT NULL,
    time_opened      TEXT,
    strategy         TEXT NOT NULL DEFAULT 'MEIC_IC',
    lot              TEXT NOT NULL,
    side             TEXT NOT NULL,          -- 'P' or 'C'
    short_symbol     TEXT,
    long_symbol      TEXT,
    quantity         INTEGER,
    open_credit      REAL,                   -- filled spread credit
    short_open_price REAL,
    long_open_price  REAL,
    short_close_price REAL,
    long_close_price  REAL,
    close_debit      REAL,                   -- short_close - long_close
    pnl              REAL,                   -- (open_credit - close_debit) * 100 * qty
    status           TEXT DEFAULT 'OPEN',    -- 'OPEN' | 'CLOSED'
    open_order_id    TEXT,
    short_close_order_id TEXT,
    created_at       TEXT DEFAULT (CURRENT_TIMESTAMP),
    UNIQUE(date_opened, lot, side)           -- prevent duplicate inserts
);

CREATE TABLE IF NOT EXISTS daily_summary (
    date        TEXT PRIMARY KEY,
    total_pnl   REAL,
    num_trades  INTEGER,
    num_wins    INTEGER,
    num_losses  INTEGER,
    updated_at  TEXT DEFAULT (CURRENT_TIMESTAMP)
);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _migrate_db(conn)


def _migrate_db(conn):
    cols = {row[1] for row in conn.execute('PRAGMA table_info(trades)').fetchall()}
    if 'strategy' not in cols:
        conn.execute(
            "ALTER TABLE trades ADD COLUMN strategy TEXT NOT NULL DEFAULT 'MEIC_IC'"
        )
        conn.execute(
            "UPDATE trades SET strategy = ? WHERE lot LIKE 'ms-%' OR lot LIKE 'ms%'",
            (STRATEGY_MANUAL,),
        )

def upsert_trade(trade: dict):
    """Insert or update a trade record. Called whenever order_params.json changes."""
    from common.expiry_settlement import effective_filled_quantity

    short_close = trade.get('short_close_price')
    long_close  = trade.get('long_close_price')
    open_credit = float(trade.get('filled_price') or 0)
    quantity    = effective_filled_quantity(trade) if 'filled_quantity' in trade else int(trade.get('quantity') or 1)
    if quantity <= 0 and short_close is None and long_close is None:
        return

    close_debit = trade.get('close_debit')
    pnl         = trade.get('pnl')
    status      = trade.get('status') or 'OPEN'

    if short_close is not None and long_close is not None and close_debit is None:
        close_debit = round(float(short_close) - float(long_close), 2)
    if short_close is not None and long_close is not None and pnl is None:
        pnl = round((open_credit - close_debit) * 100 * quantity, 2)
    if short_close is not None and long_close is not None and status == 'OPEN':
        status = 'CLOSED'

    sql = """
        INSERT INTO trades
            (date_opened, time_opened, strategy, lot, side, short_symbol, long_symbol,
             quantity, open_credit, short_open_price, long_open_price,
             short_close_price, long_close_price, close_debit, pnl, status,
             open_order_id, short_close_order_id, created_at)
        VALUES
            (:date_opened,:time_opened,:strategy,:lot,:side,:short_symbol,:long_symbol,
             :quantity,:open_credit,:short_open_price,:long_open_price,
             :short_close_price,:long_close_price,:close_debit,:pnl,:status,
             :open_order_id,:short_close_order_id,:created_at)
        ON CONFLICT(date_opened, lot, side) DO UPDATE SET
            strategy             = excluded.strategy,
            quantity             = excluded.quantity,
            open_credit          = excluded.open_credit,
            short_open_price     = excluded.short_open_price,
            long_open_price      = excluded.long_open_price,
            short_close_price    = excluded.short_close_price,
            long_close_price     = excluded.long_close_price,
            close_debit          = excluded.close_debit,
            pnl                  = excluded.pnl,
            status               = excluded.status,
            short_close_order_id = excluded.short_close_order_id
    """
    with get_conn() as conn:
        conn.execute(sql, {
            'date_opened':          trade.get('date_opened', central_today_str()),
            'time_opened':          trade.get('time_opened', ''),
            'strategy':             trade.get('strategy') or infer_strategy(
                trade.get('lot', ''),
                trade.get('entry_strategy'),
            ),
            'lot':                  trade.get('lot', ''),
            'side':                 trade.get('side', ''),
            'short_symbol':         trade.get('short_symbol', ''),
            'long_symbol':          trade.get('long_symbol', ''),
            'quantity':             quantity,
            'open_credit':          open_credit,
            'short_open_price':     float(trade.get('short_open_price') or 0),
            'long_open_price':      float(trade.get('long_open_price') or 0),
            'short_close_price':    float(short_close) if short_close is not None else None,
            'long_close_price':     float(long_close)  if long_close  is not None else None,
            'close_debit':          close_debit,
            'pnl':                  pnl,
            'status':               status,
            'open_order_id':        trade.get('open_order_id', ''),
            'short_close_order_id': trade.get('short_close_order_id', ''),
            'created_at':           trade.get('created_at', central_now_iso()),
        })
        _refresh_daily_summary(conn, trade.get('date_opened', central_today_str()))

def _refresh_daily_summary(conn, date):
    conn.execute("""
        INSERT INTO daily_summary (date, total_pnl, num_trades, num_wins, num_losses)
        SELECT
            date_opened,
            ROUND(SUM(COALESCE(pnl,0)), 2),
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)
        FROM trades
        WHERE date_opened = ? AND status = 'CLOSED'
        GROUP BY date_opened
        ON CONFLICT(date) DO UPDATE SET
            total_pnl  = excluded.total_pnl,
            num_trades = excluded.num_trades,
            num_wins   = excluded.num_wins,
            num_losses = excluded.num_losses,
            updated_at = excluded.updated_at
    """, (date,))
    conn.execute(
        "UPDATE daily_summary SET updated_at = ? WHERE date = ?",
        (central_now_iso(), date),
    )

def get_trades(date=None, strategy=None, limit=200):
    sql = "SELECT * FROM trades"
    params = []
    clauses = []
    if date:
        clauses.append('date_opened = ?')
        params.append(date)
    if strategy:
        clauses.append('strategy = ?')
        params.append(strategy)
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += " ORDER BY date_opened DESC, lot ASC, side ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _stats_sql(where: str = '') -> str:
    return f"""
        SELECT
            COUNT(*)                                        AS total_trades,
            SUM(COALESCE(pnl,0))                           AS total_pnl,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)       AS wins,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)       AS losses,
            ROUND(AVG(CASE WHEN pnl > 0 THEN pnl END), 2)  AS avg_win,
            ROUND(AVG(CASE WHEN pnl < 0 THEN pnl END), 2)  AS avg_loss,
            ROUND(MAX(pnl), 2)                              AS best_trade,
            ROUND(MIN(pnl), 2)                              AS worst_trade,
            COUNT(DISTINCT date_opened)                     AS trading_days
        FROM trades WHERE status = 'CLOSED' {where}
    """


def _row_to_stats(row) -> dict:
    d = dict(row)
    total = (d.get('wins') or 0) + (d.get('losses') or 0)
    d['win_rate'] = round((d.get('wins') or 0) / total * 100, 1) if total else 0
    d['total_pnl'] = round(d.get('total_pnl') or 0, 2)
    return d


def get_stats(strategy=None):
    where = ''
    params = []
    if strategy:
        where = 'AND strategy = ?'
        params.append(strategy)
    with get_conn() as conn:
        row = conn.execute(_stats_sql(where), params).fetchone()
        return _row_to_stats(row)


def get_stats_by_strategy() -> dict:
    with get_conn() as conn:
        all_row = conn.execute(_stats_sql()).fetchone()
        out = {'all': _row_to_stats(all_row)}
        for key in (STRATEGY_MEIC, STRATEGY_MANUAL):
            row = conn.execute(_stats_sql('AND strategy = ?'), (key,)).fetchone()
            out[key] = _row_to_stats(row)
        return out


def get_daily_summary(days=30, strategy=None):
    if strategy:
        sql = """
            SELECT date_opened AS date,
                   ROUND(SUM(COALESCE(pnl,0)), 2) AS total_pnl,
                   COUNT(*) AS num_trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS num_wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS num_losses
            FROM trades
            WHERE status = 'CLOSED' AND strategy = ?
            GROUP BY date_opened
            ORDER BY date DESC LIMIT ?
        """
        with get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, (strategy, days)).fetchall()]

    sql = """
        SELECT * FROM daily_summary
        ORDER BY date DESC LIMIT ?
    """
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, (days,)).fetchall()]


def get_daily_breakdown(days=31) -> list[dict]:
    """Per-day PnL split by strategy for calendar / charts."""
    sql = """
        SELECT date_opened AS date, strategy,
               ROUND(SUM(COALESCE(pnl,0)), 2) AS total_pnl,
               COUNT(*) AS num_trades,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS num_wins
        FROM trades
        WHERE status = 'CLOSED'
        GROUP BY date_opened, strategy
        ORDER BY date_opened DESC
        LIMIT ?
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, (days * 3,)).fetchall()]

    by_date: dict[str, dict] = {}
    for row in rows:
        d = row['date']
        if d not in by_date:
            by_date[d] = {
                'date': d,
                'pnl': 0.0,
                'numTrades': 0,
                'num_wins': 0,
                'meic_pnl': 0.0,
                'manual_pnl': 0.0,
                'meic_trades': 0,
                'manual_trades': 0,
                'meic_wins': 0,
                'manual_wins': 0,
            }
        bucket = by_date[d]
        pnl = float(row['total_pnl'] or 0)
        n = int(row['num_trades'] or 0)
        wins = int(row['num_wins'] or 0)
        bucket['pnl'] = round(bucket['pnl'] + pnl, 2)
        bucket['numTrades'] += n
        bucket['num_wins'] += wins
        if row['strategy'] == STRATEGY_MANUAL:
            bucket['manual_pnl'] = round(bucket['manual_pnl'] + pnl, 2)
            bucket['manual_trades'] += n
            bucket['manual_wins'] += wins
        else:
            bucket['meic_pnl'] = round(bucket['meic_pnl'] + pnl, 2)
            bucket['meic_trades'] += n
            bucket['meic_wins'] += wins

    result = []
    for d in sorted(by_date.keys(), reverse=True)[:days]:
        item = by_date[d]
        item['winRate'] = round(item['num_wins'] / item['numTrades'] * 100, 0) if item['numTrades'] else 0
        result.append(item)
    return result


def delete_trade(trade_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))


def delete_trades_before(cutoff_date: str) -> int:
    """Remove trade rows (and daily_summary) strictly before cutoff_date (YYYY-MM-DD)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM trades WHERE date_opened < ?", (cutoff_date,))
        conn.execute("DELETE FROM daily_summary WHERE date < ?", (cutoff_date,))
        return cur.rowcount


def delete_trades_by_lots(lots: tuple[str, ...]) -> int:
    """Remove fixture rows from SQLite (e.g. ms-99, ms-100) and refresh summaries."""
    if not lots:
        return 0
    lowered = tuple(sorted({str(l).strip().lower() for l in lots if str(l).strip()}))
    placeholders = ','.join('?' for _ in lowered)
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM trades WHERE lower(lot) IN ({placeholders})",
            lowered,
        )
        deleted = cur.rowcount
    refresh_all_daily_summaries()
    return deleted


def purge_known_test_trades_from_db() -> int:
    from common.test_trades import KNOWN_TEST_LOTS

    return delete_trades_by_lots(tuple(KNOWN_TEST_LOTS))


def refresh_all_daily_summaries() -> None:
    with get_conn() as conn:
        dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT date_opened FROM trades ORDER BY date_opened"
        )]
        for d in dates:
            _refresh_daily_summary(conn, d)

# Initialise on import
init_db()
