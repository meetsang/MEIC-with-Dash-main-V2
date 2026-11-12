"""
MEIC Trade Database (SQLite)
All historical trade records are stored in dashboard/meic_trades.db
"""
import sqlite3, os, contextlib
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
STRATEGY_SKYLINE = trades_layout.STRATEGY_SKYLINE
STRATEGY_PERMA_BULL = trades_layout.STRATEGY_PERMA_BULL
STRATEGY_MEAN_REVERSION = trades_layout.STRATEGY_MEAN_REVERSION
STRATEGY_IRON_FLY = trades_layout.STRATEGY_IRON_FLY
STRATEGY_IRON_FLY_XSP = trades_layout.STRATEGY_IRON_FLY_XSP
STRATEGY_DRMA_HM = trades_layout.STRATEGY_DRMA_HM
STRATEGY_DRMA_HM_DIR = trades_layout.STRATEGY_DRMA_HM_DIR
STRATEGY_DRMA_HM_NONDIR = trades_layout.STRATEGY_DRMA_HM_NONDIR
STRATEGY_DRMA_HM_FUT = trades_layout.STRATEGY_DRMA_HM_FUT
STRATEGY_DRMA_STOCKS = trades_layout.STRATEGY_DRMA_STOCKS
STRATEGY_DRMA_STOCK_DIR = trades_layout.STRATEGY_DRMA_STOCK_DIR
STRATEGY_DRMA_STOCK_NONDIR = trades_layout.STRATEGY_DRMA_STOCK_NONDIR
STRATEGY_DRMA_STOCK_EQUITY = trades_layout.STRATEGY_DRMA_STOCK_EQUITY
STRATEGY_CLONE_TRADES = trades_layout.STRATEGY_CLONE_TRADES
STRATEGY_INVERSE_MEIC = trades_layout.STRATEGY_INVERSE_MEIC


def _truthy_paper(value) -> bool:
    return value in (1, True, '1', 'true', 'True')


def _paper_sql_clause(*, include_paper: bool = False, paper_only: bool = False) -> str:
    if paper_only:
        return 'AND paper = 1'
    if not include_paper:
        return 'AND (paper IS NULL OR paper = 0)'
    return ''


def _strategy_sql_filter(strategy: str) -> tuple[str, list]:
    if strategy == STRATEGY_DRMA_HM_DIR:
        return "(strategy = ? AND lot LIKE 'dir_%')", [STRATEGY_DRMA_HM]
    elif strategy == STRATEGY_DRMA_HM_NONDIR:
        return "(strategy = ? AND (lot LIKE 'credit_%' OR lot LIKE 'ic_%'))", [STRATEGY_DRMA_HM]
    elif strategy == STRATEGY_DRMA_HM_FUT:
        return "(strategy = ? AND lot LIKE 'fut_%')", [STRATEGY_DRMA_HM]
    elif strategy == STRATEGY_DRMA_STOCK_DIR:
        return "(strategy = ? AND (lot LIKE '%_cds_%' OR lot LIKE '%_pds_%' OR lot LIKE '%_dir_%'))", [STRATEGY_DRMA_STOCKS]
    elif strategy == STRATEGY_DRMA_STOCK_NONDIR:
        return "(strategy = ? AND (lot LIKE '%_pcs_%' OR lot LIKE '%_ccs_%' OR lot LIKE '%_credit_%' OR lot LIKE '%_nondir_%'))", [STRATEGY_DRMA_STOCKS]
    elif strategy == STRATEGY_DRMA_STOCK_EQUITY:
        return "(strategy = ? AND (lot LIKE '%_equity_%' OR lot LIKE '%_stock_%'))", [STRATEGY_DRMA_STOCKS]
    else:
        return "strategy = ?", [strategy]


def infer_strategy(lot: str, entry_strategy: str | None = None) -> str:
    if entry_strategy in (STRATEGY_MEIC, STRATEGY_MANUAL, STRATEGY_SKYLINE, STRATEGY_PERMA_BULL, STRATEGY_MEAN_REVERSION, STRATEGY_IRON_FLY, STRATEGY_IRON_FLY_XSP, STRATEGY_DRMA_HM, STRATEGY_DRMA_STOCKS, STRATEGY_CLONE_TRADES, STRATEGY_INVERSE_MEIC):
        return entry_strategy
    text = (lot or '').strip().lower()
    if text.startswith('inv-') or text.startswith('inverse'):
        return STRATEGY_INVERSE_MEIC
    if text.startswith('ms-') or text.startswith('ms'):
        return STRATEGY_MANUAL
    if text.startswith('skyline-') or text.startswith('skyline'):
        return STRATEGY_SKYLINE
        return STRATEGY_SKYLINE
    if text.startswith('perma-') or text.startswith('perma'):
        return STRATEGY_PERMA_BULL
    if text.startswith('mr-') or text.startswith('mean'):
        return STRATEGY_MEAN_REVERSION
    if text.startswith('xsp-') or text.startswith('xsp'):
        return STRATEGY_IRON_FLY_XSP
    if text.startswith('if-') or text.startswith('iron'):
        return STRATEGY_IRON_FLY
    if text.startswith('clone-') or text.startswith('disc-'):
        return STRATEGY_CLONE_TRADES
    if text.startswith('drma') or text.startswith('dir_') or text.startswith('credit_') or text.startswith('ic_') or text.startswith('fut_'):
        return STRATEGY_DRMA_HM
    return STRATEGY_MEIC


SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date_opened      TEXT NOT NULL,
    time_opened      TEXT,
    time_closed      TEXT,
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
    paper            INTEGER NOT NULL DEFAULT 0,
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

@contextlib.contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()

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
    if 'paper' not in cols:
        conn.execute('ALTER TABLE trades ADD COLUMN paper INTEGER NOT NULL DEFAULT 0')
    if 'time_closed' not in cols:
        conn.execute('ALTER TABLE trades ADD COLUMN time_closed TEXT')

def upsert_trade(trade: dict, *, export_csv: bool = True):
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
            (date_opened, time_opened, time_closed, strategy, lot, side, short_symbol, long_symbol,
             quantity, open_credit, short_open_price, long_open_price,
             short_close_price, long_close_price, close_debit, pnl, status,
             open_order_id, short_close_order_id, created_at, paper)
        VALUES
            (:date_opened,:time_opened,:time_closed,:strategy,:lot,:side,:short_symbol,:long_symbol,
             :quantity,:open_credit,:short_open_price,:long_open_price,
             :short_close_price,:long_close_price,:close_debit,:pnl,:status,
             :open_order_id,:short_close_order_id,:created_at,:paper)
        ON CONFLICT(date_opened, lot, side) DO UPDATE SET
            time_closed          = coalesce(excluded.time_closed, trades.time_closed),
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
            short_close_order_id = excluded.short_close_order_id,
            paper                = excluded.paper
    """
    paper_val = 1 if _truthy_paper(trade.get('paper')) else 0
    with get_conn() as conn:
        conn.execute(sql, {
            'date_opened':          trade.get('date_opened', central_today_str()),
            'time_opened':          trade.get('time_opened', ''),
            'time_closed':          trade.get('time_closed') or trade.get('exit_time') or '',
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
            'paper':                paper_val,
        })
        _refresh_daily_summary(conn, trade.get('date_opened', central_today_str()), export_csv=export_csv)

def _refresh_daily_summary(conn, date, *, export_csv: bool = True):
    conn.execute("""
        INSERT INTO daily_summary (date, total_pnl, num_trades, num_wins, num_losses)
        SELECT
            date_opened,
            ROUND(SUM(COALESCE(pnl,0)), 2),
            COUNT(*),
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)
        FROM trades
        WHERE date_opened = ? AND status = 'CLOSED' AND (paper IS NULL OR paper = 0)
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
    if export_csv:
        try:
            from dashboard.strategy_pnl_csv import export_strategy_daily_pnl_csv
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            export_strategy_daily_pnl_csv(root)
        except Exception:
            pass

def get_trades(date=None, strategy=None, limit=200, *, ticker=None, include_paper=False, paper_only=False):
    sql = "SELECT * FROM trades"
    params = []
    clauses = []
    if date:
        clauses.append('date_opened = ?')
        params.append(date)
    if strategy:
        strat_clause, strat_params = _strategy_sql_filter(strategy)
        clauses.append(strat_clause)
        params.extend(strat_params)
    if ticker:
        clauses.append('(lot LIKE ? OR long_symbol = ?)')
        params.extend([f"{ticker}_%", ticker])
    paper_clause = _paper_sql_clause(include_paper=include_paper, paper_only=paper_only)
    if paper_clause:
        clauses.append(paper_clause.replace('AND ', '', 1))
    if clauses:
        sql += ' WHERE ' + ' AND '.join(clauses)
    sql += " ORDER BY date_opened DESC, lot ASC, side ASC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for row in rows:
        row['paper'] = bool(row.get('paper'))
        strat = row.get('strategy')
        lot = str(row.get('lot') or '').lower()
        if strat == STRATEGY_DRMA_HM:
            if lot.startswith('dir_'):
                row['sub_strategy'] = STRATEGY_DRMA_HM_DIR
                row['sub_strategy_label'] = 'Directional Spread'
            elif lot.startswith(('credit_', 'ic_')):
                row['sub_strategy'] = STRATEGY_DRMA_HM_NONDIR
                row['sub_strategy_label'] = 'Non-Directional Spread'
            elif lot.startswith('fut_'):
                row['sub_strategy'] = STRATEGY_DRMA_HM_FUT
                row['sub_strategy_label'] = 'Directional Futures'
            else:
                row['sub_strategy'] = STRATEGY_DRMA_HM
                row['sub_strategy_label'] = 'DRMA Other'
        elif strat == STRATEGY_DRMA_STOCKS:
            parts = lot.split('_')
            ticker_val = parts[0].upper() if len(parts) > 1 else str(row.get('long_symbol') or 'STOCK').upper()
            row['ticker'] = ticker_val
            if '_cds_' in lot or '_pds_' in lot or '_dir_' in lot:
                row['sub_strategy'] = STRATEGY_DRMA_STOCK_DIR
                row['sub_strategy_label'] = 'Directional Spread'
            elif '_pcs_' in lot or '_ccs_' in lot or '_credit_' in lot or '_nondir_' in lot:
                row['sub_strategy'] = STRATEGY_DRMA_STOCK_NONDIR
                row['sub_strategy_label'] = 'Non-Directional Spread'
            elif '_equity_' in lot or '_stock_' in lot:
                row['sub_strategy'] = STRATEGY_DRMA_STOCK_EQUITY
                row['sub_strategy_label'] = 'Direct Stock'
            else:
                row['sub_strategy'] = STRATEGY_DRMA_STOCKS
                row['sub_strategy_label'] = 'DRMA Stock'
    return rows


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


def get_stats(strategy=None, *, include_paper=False, paper_only=False):
    where = _paper_sql_clause(include_paper=include_paper, paper_only=paper_only)
    params = []
    if strategy:
        strat_clause, strat_params = _strategy_sql_filter(strategy)
        where += ' AND ' + strat_clause
        params.extend(strat_params)
    with get_conn() as conn:
        row = conn.execute(_stats_sql(where), params).fetchone()
        return _row_to_stats(row)


def get_stats_by_strategy(*, include_paper=False, paper_only=False) -> dict:
    where = _paper_sql_clause(include_paper=include_paper, paper_only=paper_only)
    with get_conn() as conn:
        all_row = conn.execute(_stats_sql(where)).fetchone()
        out = {'all': _row_to_stats(all_row)}
        for key in trades_layout.registered_strategies():
            row = conn.execute(
                _stats_sql(where + ' AND strategy = ?'),
                (key,),
            ).fetchone()
            out[key] = _row_to_stats(row)
        out[STRATEGY_DRMA_HM_DIR] = get_stats(STRATEGY_DRMA_HM_DIR, include_paper=include_paper, paper_only=paper_only)
        out[STRATEGY_DRMA_HM_NONDIR] = get_stats(STRATEGY_DRMA_HM_NONDIR, include_paper=include_paper, paper_only=paper_only)
        out[STRATEGY_DRMA_HM_FUT] = get_stats(STRATEGY_DRMA_HM_FUT, include_paper=include_paper, paper_only=paper_only)
        out[STRATEGY_DRMA_STOCK_DIR] = get_stats(STRATEGY_DRMA_STOCK_DIR, include_paper=include_paper, paper_only=paper_only)
        out[STRATEGY_DRMA_STOCK_NONDIR] = get_stats(STRATEGY_DRMA_STOCK_NONDIR, include_paper=include_paper, paper_only=paper_only)
        out[STRATEGY_DRMA_STOCK_EQUITY] = get_stats(STRATEGY_DRMA_STOCK_EQUITY, include_paper=include_paper, paper_only=paper_only)
        return out


def get_stocks_performance_breakdown(*, include_paper=True) -> dict:
    """Return historical performance breakdown per sub-strategy and per ticker for DRMA Stocks."""
    trades = get_trades(strategy=STRATEGY_DRMA_STOCKS, limit=5000, include_paper=include_paper)
    closed = [t for t in trades if str(t.get('status', '')).upper() == 'CLOSED']

    by_ticker: dict[str, dict] = {}
    by_substrat: dict[str, dict] = {
        'all': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
        'dir_spread': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
        'nondir_spread': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
        'equity_stock': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
    }

    for t in closed:
        ticker = str(t.get('ticker') or 'UNKNOWN').upper()
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                'ticker': ticker,
                'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0,
                'sub_strategies': {
                    'dir_spread': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
                    'nondir_spread': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
                    'equity_stock': {'trades': 0, 'wins': 0, 'losses': 0, 'total_pnl': 0.0, 'win_rate': 0.0},
                }
            }
        pnl = float(t.get('pnl') or 0.0)
        is_win = (pnl > 0)
        is_loss = (pnl < 0)

        sub = str(t.get('sub_strategy') or '')
        if 'DIR' in sub and 'NON' not in sub:
            cat = 'dir_spread'
        elif 'NONDIR' in sub:
            cat = 'nondir_spread'
        else:
            cat = 'equity_stock'

        tk = by_ticker[ticker]
        tk['trades'] += 1
        tk['total_pnl'] = round(tk['total_pnl'] + pnl, 2)
        if is_win: tk['wins'] += 1
        elif is_loss: tk['losses'] += 1

        tk_sub = tk['sub_strategies'][cat]
        tk_sub['trades'] += 1
        tk_sub['total_pnl'] = round(tk_sub['total_pnl'] + pnl, 2)
        if is_win: tk_sub['wins'] += 1
        elif is_loss: tk_sub['losses'] += 1

        by_substrat['all']['trades'] += 1
        by_substrat['all']['total_pnl'] = round(by_substrat['all']['total_pnl'] + pnl, 2)
        if is_win: by_substrat['all']['wins'] += 1
        elif is_loss: by_substrat['all']['losses'] += 1

        bs = by_substrat[cat]
        bs['trades'] += 1
        bs['total_pnl'] = round(bs['total_pnl'] + pnl, 2)
        if is_win: bs['wins'] += 1
        elif is_loss: bs['losses'] += 1

    for tk in by_ticker.values():
        tot = tk['wins'] + tk['losses']
        tk['win_rate'] = round(tk['wins'] / tot * 100, 1) if tot > 0 else 0.0
        for sub_k, sub_v in tk['sub_strategies'].items():
            stot = sub_v['wins'] + sub_v['losses']
            sub_v['win_rate'] = round(sub_v['wins'] / stot * 100, 1) if stot > 0 else 0.0

    for sub_k, sub_v in by_substrat.items():
        stot = sub_v['wins'] + sub_v['losses']
        sub_v['win_rate'] = round(sub_v['wins'] / stot * 100, 1) if stot > 0 else 0.0

    return {
        'by_ticker': by_ticker,
        'by_sub_strategy': by_substrat,
        'total_trades': len(closed),
    }


def get_daily_summary(days=30, strategy=None):
    if strategy:
        strat_clause, strat_params = _strategy_sql_filter(strategy)
        sql = f"""
            SELECT date_opened AS date,
                   ROUND(SUM(COALESCE(pnl,0)), 2) AS total_pnl,
                   COUNT(*) AS num_trades,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS num_wins,
                   SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) AS num_losses
            FROM trades
            WHERE status = 'CLOSED' AND {strat_clause}
            GROUP BY date_opened
            ORDER BY date DESC LIMIT ?
        """
        with get_conn() as conn:
            return [dict(r) for r in conn.execute(sql, (*strat_params, days)).fetchall()]

    sql = """
        SELECT * FROM daily_summary
        ORDER BY date DESC LIMIT ?
    """
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, (days,)).fetchall()]


def get_daily_breakdown(days=31, *, include_paper=False, paper_only=False) -> list[dict]:
    """Per-day PnL split by strategy and sub-strategy for calendar / charts."""
    paper_clause = _paper_sql_clause(include_paper=include_paper, paper_only=paper_only)
    sql = f"""
        SELECT date_opened AS date, strategy, lot, pnl
        FROM trades
        WHERE status = 'CLOSED' {paper_clause}
        ORDER BY date_opened DESC
    """
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]

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
                'skyline_pnl': 0.0,
                'perma_bull_pnl': 0.0,
                'mean_reversion_pnl': 0.0,
                'iron_fly_pnl': 0.0,
                'iron_fly_xsp_pnl': 0.0,
                'drma_hm_pnl': 0.0,
                'drma_dir_pnl': 0.0,
                'drma_nondir_pnl': 0.0,
                'drma_fut_pnl': 0.0,
                'meic_trades': 0,
                'manual_trades': 0,
                'skyline_trades': 0,
                'perma_bull_trades': 0,
                'mean_reversion_trades': 0,
                'iron_fly_trades': 0,
                'iron_fly_xsp_trades': 0,
                'drma_hm_trades': 0,
                'drma_dir_trades': 0,
                'drma_nondir_trades': 0,
                'drma_fut_trades': 0,
                'meic_wins': 0,
                'manual_wins': 0,
                'skyline_wins': 0,
                'perma_bull_wins': 0,
                'mean_reversion_wins': 0,
                'iron_fly_wins': 0,
                'iron_fly_xsp_wins': 0,
                'drma_hm_wins': 0,
                'drma_dir_wins': 0,
                'drma_nondir_wins': 0,
                'drma_fut_wins': 0,
                'drma_fut_wins': 0,
                'drma_stocks_pnl': 0.0,
                'drma_stocks_trades': 0,
                'drma_stocks_wins': 0,
                'drma_stock_dir_pnl': 0.0,
                'drma_stock_dir_trades': 0,
                'drma_stock_dir_wins': 0,
                'drma_stock_nondir_pnl': 0.0,
                'drma_stock_nondir_trades': 0,
                'drma_stock_nondir_wins': 0,
                'drma_stock_equity_pnl': 0.0,
                'drma_stock_equity_trades': 0,
                'drma_stock_equity_wins': 0,
            }
        bucket = by_date[d]
        pnl = float(row['pnl'] or 0)
        wins = 1 if pnl > 0 else 0
        bucket['pnl'] = round(bucket['pnl'] + pnl, 2)
        bucket['numTrades'] += 1
        bucket['num_wins'] += wins

        strat = row['strategy']
        lot = (row.get('lot') or '').lower()
        if strat == STRATEGY_MANUAL:
            bucket['manual_pnl'] = round(bucket['manual_pnl'] + pnl, 2)
            bucket['manual_trades'] += 1
            bucket['manual_wins'] += wins
        elif strat == STRATEGY_SKYLINE:
            bucket['skyline_pnl'] = round(bucket['skyline_pnl'] + pnl, 2)
            bucket['skyline_trades'] += 1
            bucket['skyline_wins'] += wins
        elif strat == STRATEGY_PERMA_BULL:
            bucket['perma_bull_pnl'] = round(bucket['perma_bull_pnl'] + pnl, 2)
            bucket['perma_bull_trades'] += 1
            bucket['perma_bull_wins'] += wins
        elif strat == STRATEGY_MEAN_REVERSION:
            bucket['mean_reversion_pnl'] = round(bucket['mean_reversion_pnl'] + pnl, 2)
            bucket['mean_reversion_trades'] += 1
            bucket['mean_reversion_wins'] += wins
        elif strat == STRATEGY_IRON_FLY:
            bucket['iron_fly_pnl'] = round(bucket['iron_fly_pnl'] + pnl, 2)
            bucket['iron_fly_trades'] += 1
            bucket['iron_fly_wins'] += wins
        elif strat == STRATEGY_IRON_FLY_XSP:
            bucket['iron_fly_xsp_pnl'] = round(bucket['iron_fly_xsp_pnl'] + pnl, 2)
            bucket['iron_fly_xsp_trades'] += 1
            bucket['iron_fly_xsp_wins'] += wins
        elif strat == STRATEGY_DRMA_HM:
            bucket['drma_hm_pnl'] = round(bucket['drma_hm_pnl'] + pnl, 2)
            bucket['drma_hm_trades'] += 1
            bucket['drma_hm_wins'] += wins
            if lot.startswith('dir_'):
                bucket['drma_dir_pnl'] = round(bucket['drma_dir_pnl'] + pnl, 2)
                bucket['drma_dir_trades'] += 1
                bucket['drma_dir_wins'] += wins
            elif lot.startswith(('credit_', 'ic_')):
                bucket['drma_nondir_pnl'] = round(bucket['drma_nondir_pnl'] + pnl, 2)
                bucket['drma_nondir_trades'] += 1
                bucket['drma_nondir_wins'] += wins
            elif lot.startswith('fut_'):
                bucket['drma_fut_pnl'] = round(bucket['drma_fut_pnl'] + pnl, 2)
                bucket['drma_fut_trades'] += 1
                bucket['drma_fut_wins'] = bucket.get('drma_fut_wins', 0) + wins
        elif strat == STRATEGY_DRMA_STOCKS:
            bucket['drma_stocks_pnl'] = round(bucket.get('drma_stocks_pnl', 0.0) + pnl, 2)
            bucket['drma_stocks_trades'] = bucket.get('drma_stocks_trades', 0) + 1
            bucket['drma_stocks_wins'] = bucket.get('drma_stocks_wins', 0) + wins
            if 'dir' in lot and 'non' not in lot:
                bucket['drma_stock_dir_pnl'] = round(bucket.get('drma_stock_dir_pnl', 0.0) + pnl, 2)
                bucket['drma_stock_dir_trades'] = bucket.get('drma_stock_dir_trades', 0) + 1
                bucket['drma_stock_dir_wins'] = bucket.get('drma_stock_dir_wins', 0) + wins
            elif 'nondir' in lot:
                bucket['drma_stock_nondir_pnl'] = round(bucket.get('drma_stock_nondir_pnl', 0.0) + pnl, 2)
                bucket['drma_stock_nondir_trades'] = bucket.get('drma_stock_nondir_trades', 0) + 1
                bucket['drma_stock_nondir_wins'] = bucket.get('drma_stock_nondir_wins', 0) + wins
            else:
                bucket['drma_stock_equity_pnl'] = round(bucket.get('drma_stock_equity_pnl', 0.0) + pnl, 2)
                bucket['drma_stock_equity_trades'] = bucket.get('drma_stock_equity_trades', 0) + 1
                bucket['drma_stock_equity_wins'] = bucket.get('drma_stock_equity_wins', 0) + wins
        elif strat == STRATEGY_MEIC:
            bucket['meic_pnl'] = round(bucket['meic_pnl'] + pnl, 2)
            bucket['meic_trades'] += 1
            bucket['meic_wins'] += wins
        else:
            bucket['meic_pnl'] = round(bucket['meic_pnl'] + pnl, 2)
            bucket['meic_trades'] += 1
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
            _refresh_daily_summary(conn, d, export_csv=False)
    try:
        from dashboard.strategy_pnl_csv import export_strategy_daily_pnl_csv
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        export_strategy_daily_pnl_csv(root)
    except Exception:
        pass

# Initialise on import
init_db()
