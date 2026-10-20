"""Dashboard API and control tests for DRMA + Hilega-Milega strategy."""
from __future__ import annotations

import json
import pytest

from dashboard.server import app


@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client


def test_api_drma_hm_today_endpoint(client):
    res = client.get('/api/drma_hm/today')
    assert res.status_code == 200
    data = res.get_json()
    assert data['strategy'] == 'DRMA_HM_SPX'
    assert 'paper' in data
    assert 'date' in data
    assert 'session' in data
    assert 'trade' in data
    assert 'history' in data
    assert 'levels' in data


def test_api_drma_hm_config_and_overrides(client):
    # 1. Fetch effective config
    res = client.get('/api/strategy/DRMA_HM_SPX/config')
    assert res.status_code == 200
    cfg = res.get_json()
    assert cfg['strategy'] == 'DRMA_HM_SPX'
    assert 'yaml_defaults' in cfg
    assert 'effective' in cfg

    # 2. Set daily overrides (including paper toggle and targets)
    post_res = client.post(
        '/api/strategy/DRMA_HM_SPX/overrides',
        data=json.dumps({
            'paper': False,
            'trade_mode': 'directional_only',
            'dir_target_delta': 0.45,
            'non_dir_target_credit': 1.25,
        }),
        content_type='application/json',
    )
    assert post_res.status_code == 200
    post_data = post_res.get_json()
    assert post_data['status'] == 'ok'
    assert post_data['overrides']['paper'] is False
    assert post_data['overrides']['trade_mode'] == 'directional_only'

    # Verify effective config reflects updates
    res2 = client.get('/api/strategy/DRMA_HM_SPX/config')
    cfg2 = res2.get_json()
    assert cfg2['effective']['paper'] is False
    assert cfg2['effective']['trade_mode'] == 'directional_only'

    # Verify server paper flag detects override
    from dashboard.server import _strategy_paper_flag
    assert _strategy_paper_flag('DRMA_HM_SPX') is False

    # 3. Clean up by deleting overrides
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/paper')
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/trade_mode')
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/dir_target_delta')
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/non_dir_target_credit')


def test_api_summary_includes_drma_hm(client):
    res = client.get('/api/summary')
    assert res.status_code == 200
    data = res.get_json()
    assert 'drma_hm_pnl' in data
    assert 'drma_hm_paper' in data


def test_sub_strategy_independent_control_and_overrides(client):
    from dashboard.server import _strategy_paper_flag

    # 1. Test individual sub-strategy control endpoint for Directional Options
    res_dir = client.post(
        '/api/strategy/DRMA_HM_DIR/control',
        data=json.dumps({'paper': False}),
        content_type='application/json',
    )
    assert res_dir.status_code == 200
    data_dir = res_dir.get_json()
    assert data_dir['status'] == 'ok'
    assert data_dir['strategy']['paper'] is False

    # Check that dir_paper is False while futures_paper remains True (default)
    assert _strategy_paper_flag('DRMA_HM_DIR') is False
    assert _strategy_paper_flag('DRMA_HM_FUT') is True

    # 2. Test batch sub-strategy overrides via parent DRMA_HM_SPX/overrides
    res_parent = client.post(
        '/api/strategy/DRMA_HM_SPX/overrides',
        data=json.dumps({
            'futures_paper': False,
            'non_dir_paper': True,
        }),
        content_type='application/json',
    )
    assert res_parent.status_code == 200
    parent_ov = res_parent.get_json()
    assert parent_ov['status'] == 'ok'
    assert parent_ov['overrides']['futures_paper'] is False
    assert parent_ov['overrides']['non_dir_paper'] is True

    # 3. Verify /api/drma_hm/today endpoint returns all independent flags
    today_res = client.get('/api/drma_hm/today')
    assert today_res.status_code == 200
    today_data = today_res.get_json()
    assert today_data['dir_paper'] is False
    assert today_data['futures_paper'] is False
    assert today_data['non_dir_paper'] is True

    # 4. Clean up daily overrides
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/dir_paper')
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/futures_paper')
    client.delete('/api/strategy/DRMA_HM_SPX/overrides/non_dir_paper')

    # Verify reversion after deletion
    today_reverted = client.get('/api/drma_hm/today').get_json()
    assert today_reverted['dir_paper'] is True
    assert today_reverted['futures_paper'] is True
    assert today_reverted['non_dir_paper'] is True

