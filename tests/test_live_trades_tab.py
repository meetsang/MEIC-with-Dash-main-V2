import pytest
import json
import os
import sys

# Ensure root is on path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
dashboard_dir = os.path.join(ROOT, 'dashboard')
if dashboard_dir not in sys.path:
    sys.path.insert(0, dashboard_dir)

from dashboard.server import app, build_all_live_trades


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_build_all_live_trades_structure():
    """Verify build_all_live_trades returns structured dictionary with open and closed trades and stats."""
    res = build_all_live_trades()
    assert isinstance(res, dict)
    assert 'trades' in res
    assert 'open_trades' in res
    assert 'closed_trades' in res
    assert 'stats' in res
    assert 'updated_at' in res

    stats = res['stats']
    open_trades = res['open_trades']
    closed_trades = res['closed_trades']

    assert stats['open_count'] == len(open_trades)
    assert stats['closed_count'] == len(closed_trades)
    assert stats['live_count'] + stats['paper_count'] == len(open_trades)
    assert 'total_unrealized_pnl' in stats
    assert 'total_realized_pnl' in stats
    assert 'strategy_counts' in stats

    for t in open_trades:
        assert 'id' in t
        assert 'strategy_key' in t
        assert 'strategy_label' in t
        assert 'tab_id' in t
        assert 'paper' in t
        assert 'symbol' in t
        assert 'structure' in t
        assert 'quantity' in t
        assert 'unrealized_pnl' in t
        assert 'status' in t
        assert 'actions' in t
        assert isinstance(t['actions'], list)

    for t in closed_trades:
        assert 'id' in t
        assert 'strategy_key' in t
        assert 'symbol' in t
        assert 'structure' in t
        assert 'realized_pnl' in t
        assert t['status'] == 'CLOSED'


def test_live_trades_api_endpoint(client):
    """Verify /api/live_trades endpoint returns 200 and valid JSON with open and closed lists."""
    response = client.get('/api/live_trades')
    assert response.status_code == 200
    assert response.is_json
    data = response.get_json()
    assert 'trades' in data
    assert 'open_trades' in data
    assert 'closed_trades' in data
    assert 'stats' in data
    assert isinstance(data['open_trades'], list)
    assert isinstance(data['closed_trades'], list)


def test_index_html_live_stack_subtab(client):
    """Verify index.html contains Live Stack strictly under Today tab with simple Open/Closed toggle."""
    response = client.get('/')
    assert response.status_code == 200
    html = response.get_data(as_text=True)

    # Verify mainTabs has original 5 tabs without outer Live tab
    assert 'id="mainTabs"' in html
    assert 'showTab(\'today\')' in html or 'showTab("today")' in html
    assert 'showTab(\'history\')' in html or 'showTab("history")' in html
    assert 'showTab(\'gex\')' in html or 'showTab("gex")' in html
    assert 'showTab(\'strategy-controls\')' in html or 'showTab("strategy-controls")' in html
    assert 'showTab(\'system\')' in html or 'showTab("system")' in html

    # Verify Live Stack is the active subtab on initial load and refresh
    assert 'data-strategy="live"' in html
    assert 'class="nav-link active" href="#" data-strategy="live"' in html
    assert 'id="strategy-live" class="strategy-panel" data-visible="true"' in html
    assert "let activeStrategy = 'live';" in html
    # Verify Live Stack is in strategyTabs under Today
    assert 'id="strategyTabs"' in html
    assert 'data-strategy="live"' in html
    assert 'showStrategy(\'live\')' in html or 'showStrategy("live")' in html
    assert 'id="live-nav-badge"' in html

    # Verify every strategy tab has data-strategy attribute for clean highlighting
    for strat in ['live', 'meic', 'manual', 'clone_trades', 'skyline', 'perma-bull', 'iron_fly']:
        assert f'data-strategy="{strat}"' in html

    # Verify strategy-live panel exists and has the single Open/Closed toggle
    assert 'id="strategy-live"' in html
    assert 'id="live-toggle-open"' in html
    assert 'id="live-toggle-closed"' in html
    assert 'setLiveFilterState(\'open\')' in html or 'setLiveFilterState("open")' in html
    assert 'setLiveFilterState(\'closed\')' in html or 'setLiveFilterState("closed")' in html

    # Verify stack cards container and metrics
    assert 'id="live-cards-stack"' in html
    assert 'id="live-metric-title"' in html
    assert 'id="live-stat-count"' in html
    assert 'id="live-stat-pnl"' in html

    # Verify replicated action handlers
    assert 'liveTabActionClose' in html
    assert 'liveTabActionScaleOut' in html
    assert 'liveTabActionCancel' in html
    assert 'liveTabActionLadder' in html
    assert 'liveTabActionManageManually' in html

    # Verify table and filter dropdown elements
    assert 'id="live-filter-strategy"' in html
    assert 'id="live-filter-substrat"' in html
    assert 'id="live-filter-reset-btn"' in html
    assert 'id="live-trades-table"' in html
    assert 'id="live-trades-tbody"' in html
    assert 'onLiveStrategyChange' in html
    assert 'onLiveSubStrategyChange' in html
    assert 'resetLiveFilters' in html

    # Verify separate Strategy and Sub-Strategy columns
    assert '>Strategy</th>' in html
    assert '>Sub-Strategy</th>' in html

    # Verify Strategy P&L breakdown section on top
    assert 'id="live-strategy-pnl-chips"' in html
    assert 'id="live-strat-pnl-title"' in html
    assert 'setLiveFilterStrategy' in html


def test_live_only_filtering_and_sub_strategy():
    """Verify strictly only live trades (paper=False) are returned, enriched with sub_strategy, and closed trades are not in open."""
    res = build_all_live_trades()
    open_trades = res.get('open_trades', [])
    closed_trades = res.get('closed_trades', [])
    stats = res.get('stats', {})

    # Verify MEIC paper trades are strictly excluded
    strat_keys_open = [t.get('strategy_key') for t in open_trades]
    strat_keys_closed = [t.get('strategy_key') for t in closed_trades]
    assert 'MEIC_IC' not in strat_keys_open
    assert 'MEIC_IC' not in strat_keys_closed
    assert 'MEIC_IC' not in stats.get('open_strategy_pnl', {})
    assert 'MEIC_IC' not in stats.get('closed_strategy_pnl', {})
    # No paper trades allowed in live stack
    assert stats.get('paper_count') == 0
    assert 'open_strategy_pnl' in stats
    assert 'closed_strategy_pnl' in stats

    # Verify closed trades (such as ms-793 and ms-794) are not in open_trades
    open_lots = [t.get('lot') for t in open_trades if t.get('lot')]
    assert 'ms-793' not in open_lots
    assert 'ms-794' not in open_lots

    for t in open_trades:
        assert t.get('paper') is False
        assert 'sub_strategy' in t
        assert 'sub_strategy_label' in t
        assert t['sub_strategy'] is not None

    for t in closed_trades:
        assert t.get('paper') is False
        assert 'sub_strategy' in t
        assert 'sub_strategy_label' in t
        assert t['sub_strategy'] is not None

    # Verify all 10 closed manual trades, Perma Bull PCS, and DRMA-HM SPX are present
    closed_lots = [t.get('lot') for t in closed_trades if t.get('lot')]
    for expected_manual in ['ms-793', 'ms-794', 'ms-804', 'ms-804-s1', 'ms-804-s2', 'ms-804-s3', 'ms-823', 'ms-824', 'ms-826', 'ms-827']:
        assert expected_manual in closed_lots, f"Missing manual trade: {expected_manual}"
    assert stats['closed_strategy_counts'].get('MANUAL_SPREAD') == 10
    assert 'PERMA_BULL_PCS' in stats['closed_strategy_counts']
    assert stats['closed_strategy_counts'].get('DRMA_HM_SPX') == 5


