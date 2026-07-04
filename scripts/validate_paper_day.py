#!/usr/bin/env python3
"""Offline paper-day gate checks for V2.5 session CSV cutover.

Validates IMPLEMENTATION.md §10 gates without broker connectivity:
  - Session CSV bootstrap (12 rows, idempotent)
  - Pause state in CSV only (no pause_tranches.json required)
  - Entry monitor skip paused / fire in window
  - One JSON per slot (stable filename + order_history on retry)
  - Stop runner gate (open + full fill)
  - Spread scan pick (non-overlap candidate, not candidates[0])
  - Dashboard ghost pick (pick_best_trade)

Run from repo root:
  python scripts/validate_paper_day.py
  python scripts/validate_paper_day.py --verbose
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, time
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import load_meic_session_today
from blocks.stop import state as state_mod
from blocks.stop.runner import MonitorRunner
from common.trade_pick import pick_best_trade
from blocks.entry.spread_scan import SpreadCandidate, pick_meic_candidate
from orchestrator.scheduler import TrancheSlot


def _ok(msg: str, verbose: bool) -> None:
    if verbose:
        print(f'  OK  {msg}')


def _fail(msg: str) -> str:
    return msg


def check_bootstrap(verbose: bool) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        bootstrap_meic_session_if_missing(tmp)
        plan1 = load_meic_session_today(tmp)
        if plan1 is None:
            return [_fail('bootstrap did not create MEIC session CSV')]
        n1 = len(plan1.rows)
        bootstrap_meic_session_if_missing(tmp)
        plan2 = load_meic_session_today(tmp)
        n2 = len(plan2.rows) if plan2 else 0
        if n1 != 12:
            errors.append(_fail(f'expected 12 session rows, got {n1}'))
        elif n1 != n2:
            errors.append(_fail('bootstrap not idempotent (row count changed)'))
        else:
            _ok(f'bootstrap: {n1} rows, idempotent', verbose)
    return errors


def check_pause_csv_only(verbose: bool) -> list[str]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        bootstrap_meic_session_if_missing(
            tmp,
            slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
        )
        plan = load_meic_session_today(tmp)
        plan.update_row('11-00_P', paused=True)
        plan.save()
        plan = load_meic_session_today(tmp)
        row = plan.row_by_slot_key('11-00_P')
        if not row or not row.paused:
            errors.append(_fail('CSV pause flag not persisted'))
        else:
            _ok('pause via session CSV', verbose)

        runner = EntryMonitorRunner(root=tmp)
        now = datetime(2026, 6, 25, 11, 0, 0)
        with patch.object(runner, '_run_worker'):
            runner.tick(now)
        if '11-00_P' in runner._fired:
            errors.append(_fail('entry monitor fired paused row'))
        elif '11-00_C' not in runner._fired:
            errors.append(_fail('entry monitor did not fire unpaused row in window'))
        else:
            _ok('entry monitor skips paused, fires unpaused side', verbose)
    return errors


def check_one_json_retry(verbose: bool) -> list[str]:
    errors: list[str] = []
    from blocks.entry.handshake import write_credit_spread_handshake

    with tempfile.TemporaryDirectory() as tmp:
        path1 = write_credit_spread_handshake(
            lot='02-00',
            side='P',
            short_symbol='.SPXW260625P7000',
            long_symbol='.SPXW260625P6975',
            short_strike=7000,
            long_strike=6975,
            quantity=1,
            open_order_id='oid-1',
            limit_credit=1.0,
            active_directory=tmp,
            entry_ts='20260625T135959',
        )
        path2 = write_credit_spread_handshake(
            lot='02-00',
            side='P',
            short_symbol='.SPXW260625P7005',
            long_symbol='.SPXW260625P6980',
            short_strike=7005,
            long_strike=6980,
            quantity=1,
            open_order_id='oid-2',
            limit_credit=1.05,
            active_directory=tmp,
            existing_path=path1,
            reason='cancelled_for_chase',
        )
        if path1 != path2:
            errors.append(_fail('retry changed trade JSON filename'))
        else:
            st = state_mod.load_state(path2)
            if st.get('open_order_id') != 'oid-2' or len(st.get('order_history') or []) < 2:
                errors.append(_fail('order_history / pending update missing on retry'))
            else:
                _ok('one JSON per slot on chase retry', verbose)
    return errors


def check_stop_gate(verbose: bool) -> list[str]:
    errors: list[str] = []
    from unittest.mock import MagicMock, patch

    broker = MagicMock()
    with tempfile.TemporaryDirectory() as tmp:
        pending = os.path.join(tmp, 'pending.json')
        open_full = os.path.join(tmp, 'open.json')
        pending_st = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='11-00',
            side='P',
            short_symbol='.SPXW260625P7000',
            long_symbol='.SPXW260625P6975',
            short_strike=7000,
            long_strike=6975,
            target_quantity=1,
            open_order_id='123',
            limit_credit=1.0,
        )
        state_mod.save_state(pending, pending_st)
        open_st = dict(pending_st)
        open_st.update({'status': 'open', 'filled_quantity': 1, 'quantity': 1})
        state_mod.save_state(open_full, open_st)

        runner = MonitorRunner(broker, watch_dir=tmp)
        with patch('blocks.stop.runner.StopMonitor') as mock_mon:
            runner.add(pending)
            runner.add(open_full)
            if mock_mon.call_count != 1:
                errors.append(_fail(f'stop runner gate: expected 1 monitor, got {mock_mon.call_count}'))
            else:
                _ok('stop runner gates on open + full fill', verbose)
    return errors


def check_scan_pick(verbose: bool) -> list[str]:
    errors: list[str] = []
    c_overlap = SpreadCandidate(
        short_symbol='.SPXW260625P7325', long_symbol='.SPXW260625P7300',
        short_strike=7325, long_strike=7300, market_credit=1.70,
        short_mid=2.55, long_mid=0.82, overlap_warning='overlap',
    )
    c_clean = SpreadCandidate(
        short_symbol='.SPXW260625P7320', long_symbol='.SPXW260625P7295',
        short_strike=7320, long_strike=7295, market_credit=1.30,
        short_mid=1.98, long_mid=0.68, overlap_warning=None,
    )
    pick = pick_meic_candidate([c_overlap, c_clean])
    if pick is None or pick.short_strike != 7320:
        errors.append(_fail('pick_meic_candidate did not skip overlap first candidate'))
    else:
        _ok('scan picks first non-overlap candidate', verbose)
    return errors


def check_dashboard_pick(verbose: bool) -> list[str]:
    errors: list[str] = []
    ghosts = [
        {'status': 'pending_fill', 'entry': {'net_credit': 0.5}, '_filename': 'ghost_a.json'},
        {'status': 'open', 'entry': {'net_credit': 1.2}, '_filename': 'real_b.json'},
    ]
    best = pick_best_trade(ghosts)
    if best.get('_filename') != 'real_b.json':
        errors.append(_fail('pick_best_trade did not prefer open over pending_fill'))
    else:
        _ok('dashboard pick_best_trade avoids ghost pending', verbose)
    return errors


def check_spread_kill_api(verbose: bool) -> list[str]:
    errors: list[str] = []
    from brokers.base import BrokerBase
    if not hasattr(BrokerBase, 'place_spread_close_order'):
        errors.append(_fail('BrokerBase.place_spread_close_order missing'))
    else:
        _ok('spread close broker API present', verbose)
    from blocks.stop.monitor import StopMonitor
    if not hasattr(StopMonitor, 'replace_with_spread_close'):
        errors.append(_fail('StopMonitor.replace_with_spread_close missing'))
    else:
        _ok('spread close monitor handler present', verbose)
    return errors


CHECKS = [
    ('bootstrap', check_bootstrap),
    ('pause_csv', check_pause_csv_only),
    ('one_json_retry', check_one_json_retry),
    ('stop_gate', check_stop_gate),
    ('scan_pick', check_scan_pick),
    ('dashboard_pick', check_dashboard_pick),
    ('spread_kill', check_spread_kill_api),
]


def main() -> int:
    parser = argparse.ArgumentParser(description='V2.5 paper-day offline gates')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    print('Paper-day validation (offline)')
    all_errors: list[str] = []
    for name, fn in CHECKS:
        if args.verbose:
            print(f'\n[{name}]')
        errs = fn(args.verbose)
        all_errors.extend(errs)

    if all_errors:
        print('\nFAILED:')
        for e in all_errors:
            print(f'  - {e}')
        return 1

    print(f'\nAll {len(CHECKS)} gates passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
