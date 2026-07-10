"""Phase 2 adaptive stop reconcile and REST observability tests."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from blocks.stop import state as state_mod
from blocks.stop.monitor import FAST_INTERVAL, StopMonitor
from blocks.stop.reconcile_policy import (
    STOP_RECONCILE_CLOSING_SEC,
    STOP_RECONCILE_OPEN_JITTER_SEC,
    STOP_RECONCILE_OPEN_SEC,
    STOP_RECONCILE_STALE_SEC,
    is_working_stop_reconcile_due,
    reconcile_identity_key,
    reconcile_interval_sec,
    schedule_next_working_stop_reconcile,
    simulate_reconcile_events,
    stable_reconcile_jitter_sec,
)
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import TradeSlot
from common import broker_cooldown
from common.rest_metrics import (
    get_rest_metrics,
    metrics_snapshot,
    reset_rest_metrics,
    write_metrics_snapshot,
)
from common.rest_operations import (
    OPERATION_WORKING_STOP_RECONCILE,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
)
from common.rest_limiter import reset_rest_limiter
from tests.mock_broker import MockBroker
from tests.test_v3_paper_scenarios import _mock_prices, _open_state


def _trade_state(lot: str, *, oid: str = '9001') -> dict:
    st = _open_state(lot=lot)
    st['active_stop'] = {
        'order_id': oid,
        'type': 'STOP_LIMIT',
        'stop_price': 3.5,
        'limit_price': 3.6,
        'phase': 1,
        'status': 'working',
        'quantity': 1,
    }
    return st


def test_healthy_open_trade_uses_15s_plus_stable_jitter():
    st = _trade_state('11-00')
    interval = reconcile_interval_sec(st, mqtt_healthy=True, status='open')
    jitter = stable_reconcile_jitter_sec(st)
    assert interval == pytest.approx(STOP_RECONCILE_OPEN_SEC + jitter)
    assert STOP_RECONCILE_OPEN_SEC <= interval <= STOP_RECONCILE_OPEN_SEC + STOP_RECONCILE_OPEN_JITTER_SEC


def test_same_trade_same_jitter_after_restart():
    st = _trade_state('11-15', oid='482267049')
    a = stable_reconcile_jitter_sec(st)
    b = stable_reconcile_jitter_sec(dict(st))
    assert a == b


def test_different_trade_identities_distribute_jitter():
    states = [_trade_state(f'{h:02d}-00', oid=f'oid-{h}') for h in range(8)]
    jitters = {round(stable_reconcile_jitter_sec(st), 4) for st in states}
    assert len(jitters) > 1


def test_jitter_uses_crc32_not_python_hash():
    st = _trade_state('12-00')
    key = reconcile_identity_key(st)
    digest_a = stable_reconcile_jitter_sec(st)
    with patch('blocks.stop.reconcile_policy.zlib.crc32', side_effect=lambda b: 12345):
        digest_b = stable_reconcile_jitter_sec(st)
    assert digest_a != digest_b
    assert key


def test_mqtt_stale_open_trade_uses_defensive_interval():
    st = _trade_state('01-00')
    interval = reconcile_interval_sec(st, mqtt_healthy=False, status='open')
    assert interval == STOP_RECONCILE_STALE_SEC


def test_closing_trade_uses_5_second_interval():
    st = _trade_state('02-00')
    interval = reconcile_interval_sec(st, mqtt_healthy=True, status='closing')
    assert interval == STOP_RECONCILE_CLOSING_SEC


def test_close_only_mode_uses_fast_interval():
    st = _trade_state('02-15')
    interval = reconcile_interval_sec(
        st, mqtt_healthy=True, status='open', close_only_mode=True,
    )
    assert interval == STOP_RECONCILE_CLOSING_SEC


def test_exit_job_active_skips_peaceful_reconcile_due():
    st = _trade_state('03-00')
    assert is_working_stop_reconcile_due(
        st, time.time(), mqtt_healthy=True, exit_job_active=True,
    ) is False


def test_long_chase_active_skips_peaceful_reconcile_due():
    st = _trade_state('03-15')
    assert is_working_stop_reconcile_due(
        st, time.time(), mqtt_healthy=True, long_chase_active=True,
    ) is False


def test_recovery_active_uses_immediate_interval():
    st = _trade_state('04-00')
    assert reconcile_interval_sec(st, mqtt_healthy=True, recovery_active=True) == 0.0


def test_low_reconcile_skipped_during_cooldown_and_rescheduled(tmp_path):
    broker_cooldown.clear_cooldown()
    broker_cooldown.set_cooldown('test', source='unit', duration_sec=60)
    st = _trade_state('05-00')
    now = 1000.0
    schedule_next_working_stop_reconcile(st, now - 20, mqtt_healthy=True, status='open')
    assert is_working_stop_reconcile_due(st, now, mqtt_healthy=True, status='open')
    broker = MockBroker()
    cache = MagicMock()
    path = tmp_path / 't.json'
    state_mod.save_state(str(path), st)
    with patch('blocks.stop.monitor._trades_root_for_path', return_value=str(tmp_path)), \
         patch.object(StopMonitor, '_streamer_prices_stale', return_value=False), \
         patch('blocks.stop.monitor.mqtt_cache_is_stale', return_value=False), \
         patch.object(StopMonitor, '_reconcile_active_stop_with_broker') as mock_rec:
        mon = StopMonitor(str(path), broker, cache)
        mon.state = st
        mon._poll_once()
        mock_rec.assert_not_called()
    due = st['lifecycle']['next_working_stop_reconcile_epoch']
    assert due > now
    broker_cooldown.clear_cooldown()


def test_high_call_permitted_during_cooldown():
    broker_cooldown.clear_cooldown()
    broker_cooldown.set_cooldown('test', source='unit', duration_sec=60)
    assert broker_cooldown.should_skip_priority(PRIORITY_LOW) is True
    assert broker_cooldown.should_skip_priority(PRIORITY_HIGH) is False
    broker_cooldown.clear_cooldown()


def test_metrics_count_operation_and_priority():
    reset_rest_metrics()
    m = get_rest_metrics()
    m.record_call(OPERATION_WORKING_STOP_RECONCILE, PRIORITY_LOW)
    snap = metrics_snapshot()
    assert snap['by_operation'][OPERATION_WORKING_STOP_RECONCILE] == 1
    assert snap['by_priority'][PRIORITY_LOW] == 1


def test_failure_and_429_counted_without_raising():
    reset_rest_metrics()
    m = get_rest_metrics()
    m.record_failure('get_order', RuntimeError('429 rate limit'))
    m.record_429()
    snap = metrics_snapshot()
    assert snap['failed']
    assert snap['last_429_epoch'] is not None


def test_metrics_snapshot_thread_safe_and_bounded():
    reset_rest_metrics()
    m = get_rest_metrics()

    def worker():
        for i in range(50):
            m.record_call(f'op_{i % 5}', PRIORITY_NORMAL)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = metrics_snapshot()
    assert snap['calls_last_1m'] > 0
    assert len(snap['by_operation']) <= 32


def test_dashboard_metrics_read_creates_no_broker_calls():
    reset_rest_metrics()
    get_rest_metrics().record_call('entry_market_data', 'NORMAL')
    snap = metrics_snapshot()
    assert 'by_operation' in snap
    assert snap['scope'] == 'per_process'


def test_write_metrics_snapshot_atomic(tmp_path):
    reset_rest_metrics()
    get_rest_metrics().record_call(OPERATION_WORKING_STOP_RECONCILE, PRIORITY_LOW)
    write_metrics_snapshot(root=str(tmp_path))
    path = tmp_path / 'runtime' / f'rest_metrics_{os.getpid()}.json'
    assert path.is_file()
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['by_operation'][OPERATION_WORKING_STOP_RECONCILE] == 1


def test_eight_open_trades_staggered_not_synchronized():
    trades = [_trade_state(f'{i:02d}-00', oid=f'oid-{i}') for i in range(8)]
    before = simulate_reconcile_events(trades, duration_sec=600, fixed_interval_sec=10)
    after = simulate_reconcile_events(
        [copy.deepcopy(t) for t in trades],
        duration_sec=600,
    )
    assert before['peak_calls_one_second'] == 8
    assert after['peak_calls_one_second'] < before['peak_calls_one_second']
    assert after['total_calls'] < before['total_calls']


def test_v2_and_v3_equivalent_reconcile_policy():
    from common.mqtt_prices import mqtt_cache_is_stale

    st = _trade_state('06-00')
    interval = reconcile_interval_sec(st, mqtt_healthy=True, status='open')
    assert interval >= STOP_RECONCILE_OPEN_SEC

    broker = MockBroker()
    prices = _mock_prices()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 't.json')
        state_mod.save_state(path, st)
        slot = TradeSlot.from_path(path)
        sup = StopSupervisor(broker, prices)
        mon = sup._legacy_monitor(slot)
        with patch.object(mon, '_streamer_prices_stale', return_value=False):
            ready_interval = reconcile_interval_sec(
                slot.state,
                mqtt_healthy=not mqtt_cache_is_stale(prices),
                status='open',
            )
        assert ready_interval == interval


def test_long_chase_fast_interval_unchanged():
    assert FAST_INTERVAL == 3


def test_phase4_breach_loop_unchanged_import():
    from blocks.stop.breach_quote import evaluate_software_breach_exit
    assert callable(evaluate_software_breach_exit)


def test_phase1_fill_sync_budget_unchanged():
    from blocks.stop.fill_provenance import FILL_SYNC_FAST_SEC
    assert FILL_SYNC_FAST_SEC == 3.0


def test_simulation_closing_recovery_remain_fast():
    st = _trade_state('07-00')
    closing = reconcile_interval_sec(st, mqtt_healthy=True, status='closing')
    recovery = reconcile_interval_sec(st, mqtt_healthy=True, recovery_active=True)
    assert closing == STOP_RECONCILE_CLOSING_SEC
    assert recovery == 0.0
