#!/usr/bin/env python3
"""
Ad-hoc integration tests for MEIC autotrader.

Run offline unit tests (no broker):
    python tests/run_tests.py

Run broker smoke checks:
    python tests/adhoc_integration.py check-all
    python tests/adhoc_integration.py check-auth
    python tests/adhoc_integration.py check-prices

Place explicit spread (uses open_spread_tt + broker.place_spread_order):
    python tests/adhoc_integration.py place-trade --side C --expiry 2026-07-22 \\
        --short-strike 7525 --long-strike 7550 --quantity 1

Place stop on existing short leg ($3 stop on 7635C, 5 contracts):
    python tests/adhoc_integration.py place-stop --side C --short-strike 7635 \\
        --quantity 5 --stop 3.0 --expiry 2026-06-19

Seed stop_monitor JSON for an existing open spread (no new entry):
    python tests/adhoc_integration.py seed-stop --side P --short-strike 5550 --long-strike 5520 \\
        --short-fill 4.0 --long-fill 2.5 --credit 1.5

Run stop_monitor on active JSON files for N seconds:
    python tests/adhoc_integration.py run-stop-monitor --seconds 120 --paper

Full smoke (checks + optional trade + monitor):
    python tests/adhoc_integration.py full-smoke --paper --side P --monitor-seconds 60
"""
from __future__ import annotations

import argparse
import glob
import logging
import os
import socket
import sys
import threading
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

log = logging.getLogger('adhoc')


def parse_expiry_yymmdd(value: str | None) -> str:
    """Parse YYYY-MM-DD or YYMMDD into YYMMDD for option symbols."""
    from datetime import datetime

    if not value:
        return datetime.now().strftime('%y%m%d')
    value = value.strip()
    if len(value) == 6 and value.isdigit():
        return value
    return datetime.strptime(value, '%Y-%m-%d').strftime('%y%m%d')


def _open_spread(broker, args, side: str, lot: str, qty: int):
    from meic0dte.open import open_spread_tt

    if args.short_strike is not None and args.long_strike is not None:
        if not args.expiry:
            raise ValueError('--expiry is required when using --short-strike/--long-strike')
        expiry = parse_expiry_yymmdd(args.expiry)
        return open_spread_tt.open_spread_at_strikes_tt(
            broker,
            side,
            qty,
            lot,
            log,
            expiry,
            args.short_strike,
            args.long_strike,
            credit=args.credit,
        )
    return open_spread_tt.open_spread_tt(broker, 1, side, qty, lot, log)


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s [ADHOC] %(levelname)s %(message)s',
    )


def cmd_check_env(_args) -> int:
    from common import tt_config

    print('--- Environment ---')
    print(f'  BROKER          = {tt_config.BROKER}')
    print(f'  PAPER_MODE      = {tt_config.PAPER_MODE}')
    print(f'  TT_IS_TEST      = {tt_config.TT_IS_TEST}')
    print(f'  TRADES_ACTIVE   = {tt_config.TRADES_ACTIVE_DIR}')

    ok = True
    if tt_config.BROKER == 'tastytrade':
        if not tt_config.TT_CLIENT_SECRET and not tt_config.TASTYWARE_API_KEY:
            print('  FAIL: Set TT_CLIENT_SECRET+TT_REFRESH_TOKEN or TASTYWARE_API_KEY')
            ok = False
        if not tt_config.PAPER_MODE and not tt_config.TT_REFRESH_TOKEN:
            print('  FAIL: TT_REFRESH_TOKEN required for live mode')
            ok = False
        if tt_config.PAPER_MODE and not tt_config.TASTYWARE_API_KEY:
            print('  WARN: PAPER_MODE=true but TASTYWARE_API_KEY missing')
    elif tt_config.BROKER == 'schwab':
        from common.auth import config as auth_config
        if not auth_config.CLIENT_ID:
            print('  FAIL: SCHWAB_CLIENT_ID missing in .env')
            ok = False
    else:
        print(f'  FAIL: Unknown BROKER={tt_config.BROKER}')
        ok = False

    print('  RESULT:', 'OK' if ok else 'FAILED')
    return 0 if ok else 1


def cmd_check_mqtt(_args) -> int:
    from streaming import config as stream_config

    host = stream_config.MQTT_BROKER_ADDR
    port = 1883
    print(f'--- MQTT ({host}:{port}) ---')
    try:
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        print('  RESULT: OK (broker reachable)')
        return 0
    except OSError as e:
        print(f'  FAIL: {e}')
        print('  Install Mosquitto and start the service (see README).')
        return 1


def cmd_check_auth(args) -> int:
    from common import tt_config
    from common.broker_factory import get_broker

    print('--- Broker auth ---')
    if tt_config.BROKER == 'schwab':
        from common.auth import refresh_token
        try:
            refresh_token.refresh(log)
            print('  Schwab token refresh: OK')
            return 0
        except Exception as e:
            print(f'  FAIL: {e}')
            return 1

    try:
        broker = get_broker(paper=args.paper)
        if broker._connected:
            print('  TastyTrade session: OK')
            return 0
        print('  FAIL: connect() returned False')
        return 1
    except Exception as e:
        print(f'  FAIL: {e}')
        return 1


def cmd_check_prices(args) -> int:
    from common.broker_factory import get_broker

    print('--- Market data ---')
    broker = get_broker(paper=args.paper)
    spx = broker.get_spx_price()
    print(f'  SPX mid (MQTT streamer): {spx}')
    if spx is None:
        print('  WARN: No SPX on MQTT — start streaming/publish_tastytrade.py first')
        return 1
    print('  RESULT: OK')
    return 0


def cmd_place_trade(args) -> int:
    """Place one thin spread and write trades/active/*.json for stop_monitor."""
    from datetime import datetime

    from common.broker_factory import get_broker
    from meic0dte.open import open_spread_tt

    side = args.side.upper()
    lot = args.lot or f"test-{datetime.now().strftime('%H%M')}"
    qty = args.quantity

    print(f'--- Place trade (side={side}, lot={lot}, paper={args.paper}) ---')
    if args.short_strike is not None:
        print(f'  Strikes: {args.short_strike}/{args.long_strike}  expiry={args.expiry}')

    broker = get_broker(paper=args.paper)
    try:
        short_sym, long_sym, order_id, credit, short_strike, long_strike = _open_spread(
            broker, args, side, lot, qty
        )
    except ValueError as e:
        print(f'  FAIL: {e}')
        return 1
    except Exception as e:
        print(f'  FAIL: {e}')
        return 1

    time.sleep(3)
    order_info = open_spread_tt.wait_for_fill(broker, order_id, log, max_wait=45)
    if order_info.get('status') != 'FILLED':
        print(f'  FAIL: order status={order_info.get("status")}')
        return 1

    short_fill = broker.get_option_price(short_sym) or credit / 2
    long_fill = broker.get_option_price(long_sym) or credit / 4
    net_credit = round(short_fill - long_fill, 2)

    path = open_spread_tt.write_trade_state(
        lot=lot,
        opt_type=side,
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=short_strike,
        long_strike=long_strike,
        short_fill=short_fill,
        long_fill=long_fill,
        net_credit=net_credit,
        quantity=qty,
        open_order_id=order_id,
    )
    print(f'  Open order: {order_id}')
    print(f'  Short: {short_sym} @ ~{short_fill:.2f}')
    print(f'  Long:  {long_sym} @ ~{long_fill:.2f}')
    print(f'  Credit: {net_credit:.2f}')
    print(f'  State file: {path}')
    print('  Next: python tests/adhoc_integration.py run-stop-monitor --seconds 120')
    return 0


def cmd_seed_stop(args) -> int:
    """Create trades/active JSON without placing a new entry (existing position)."""
    from datetime import datetime

    from common.symbols import build_tastytrade_symbol
    from blocks.stop import state as state_mod

    side = args.side.upper()
    expiry = parse_expiry_yymmdd(args.expiry)
    short_strike = args.short_strike
    long_strike = args.long_strike
    short_sym = build_tastytrade_symbol(expiry, side, short_strike)
    long_sym = build_tastytrade_symbol(expiry, side, long_strike)

    lot = args.lot or f"seed-{datetime.now().strftime('%H%M')}"
    state_mod.ensure_dirs()
    path = os.path.join(
        state_mod.active_dir(),
        state_mod.state_filename('MEIC_IC', lot, side, open_order_id=args.open_order_id or 'manual-seed'),
    )

    st = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot=lot,
        side=side,
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=short_strike,
        long_strike=long_strike,
        short_fill=args.short_fill,
        long_fill=args.long_fill,
        net_credit=args.credit,
        quantity=args.quantity,
        open_order_id=args.open_order_id or 'manual-seed',
    )
    if getattr(args, 'skip_auto_stop', False):
        st['active_stop'] = {'status': 'manual', 'skip_initial': True}
    state_mod.save_state(path, st)
    print(f'--- Seeded stop_monitor state ---')
    print(f'  {path}')
    if getattr(args, 'skip_auto_stop', False):
        print('  Auto initial stop skipped (use place-stop for explicit price).')
    else:
        print('  stop_monitor will place initial 2x short stop on pickup.')
        try:
            from common import tt_config
            from common.broker_factory import get_broker
            from blocks.stop.broker_sync import adopt_active_stop_from_broker

            broker = get_broker(paper=getattr(args, 'paper', None) or tt_config.PAPER_MODE)
            if adopt_active_stop_from_broker(st, broker):
                state_mod.save_state(path, st)
                oid = state_mod.section(st, 'active_stop').get('order_id')
                print(f'  Linked existing broker stop {oid} into JSON (avoids duplicate at broker)')
        except Exception as exc:
            log.debug('Broker stop sync on seed: %s', exc)
    print('  Run: python tests/adhoc_integration.py run-stop-monitor --seconds 120')
    return 0


def cmd_place_stop(args) -> int:
    """Place a stop-limit on the short leg via broker (existing position)."""
    from datetime import datetime

    import meic0dte.app.config as app_config
    from common.broker_factory import get_broker
    from common.symbols import build_tastytrade_symbol
    from blocks.stop import state as state_mod
    from blocks.stop.monitor import StopMonitor
    from blocks.stop.mqtt_prices import MqttPriceCache
    from unittest.mock import MagicMock

    side = args.side.upper()
    expiry = parse_expiry_yymmdd(args.expiry)
    short_sym = build_tastytrade_symbol(expiry, side, args.short_strike)
    stop_price = args.stop
    limit_price = (
        args.limit
        if args.limit is not None
        else round(stop_price + app_config.LIMIT_OFFSET, 2)
    )

    print(
        f'--- Place stop (short={short_sym}, qty={args.quantity}, '
        f'stop={stop_price} debit, limit={limit_price} debit) ---'
    )

    broker = get_broker(paper=args.paper)

    if args.write_state:
        if args.long_strike is None:
            print('  FAIL: --write-state requires --long-strike')
            return 1
        lot = args.lot or f"stop-{datetime.now().strftime('%H%M')}"
        state_mod.ensure_dirs()
        path = os.path.join(
            state_mod.active_dir(),
            state_mod.state_filename('MEIC_IC', lot, side, open_order_id='manual-stop'),
        )
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot=lot,
            side=side,
            short_symbol=short_sym,
            long_symbol=build_tastytrade_symbol(expiry, side, args.long_strike),
            short_strike=args.short_strike,
            long_strike=args.long_strike,
            short_fill=args.short_fill,
            long_fill=args.long_fill,
            net_credit=args.credit,
            quantity=args.quantity,
            open_order_id=args.open_order_id or 'existing-position',
        )
        state_mod.save_state(path, st)
        prices = MagicMock(spec=MqttPriceCache)
        prices.get = lambda _sym: None
        prices.kill_switch = False
        prices.get_spx = lambda: broker.get_spx_price()
        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        if not mon.setup_stop_at_price(stop_price, limit_price, reason='manual_adhoc_stop'):
            print('  FAIL: broker rejected stop order')
            return 1
        print(f'  State file: {path}')
        print(f'  Stop order: {mon.state["active_stop"]["order_id"]}')
        return 0

    result = broker.place_stop_order(
        short_sym, args.quantity, stop_price, limit_price
    )
    if not result.success:
        print(f'  FAIL: {result.message}')
        return 1
    print(f'  Stop order: {result.order_id}')
    print(f'  Symbol: {short_sym}')
    print(f'  Stop: {stop_price}  Limit: {limit_price}  Qty: {args.quantity}')
    return 0


def _wrap_broker_partial_sim(args, broker):
    """Return broker wrapped to fake open-order leg fills when simulating Scenario 4."""
    if not getattr(args, 'simulate_partial_fill', False):
        return broker
    from common.simulated_open_order import SimulatedOpenOrderBroker

    step = int(getattr(args, 'partial_step', 1) or 1)
    partial_qty = getattr(args, 'partial_qty', None)
    wrapped = SimulatedOpenOrderBroker(
        broker,
        args.open_order_id,
        quantity=args.quantity,
        short_fill=args.short_fill,
        long_fill=args.long_fill,
        credit=args.credit,
        step=step,
        partial_qty=partial_qty,
    )
    pq = partial_qty if partial_qty is not None else max(1, args.quantity // 2)
    if step <= 1:
        print(
            f'  Simulated open order {args.open_order_id}: '
            f'{pq}/{args.quantity} spread units filled (short+long @ '
            f'{args.short_fill:.2f}/{args.long_fill:.2f})'
        )
    else:
        print(
            f'  Simulated open order {args.open_order_id}: '
            f'full spread {args.quantity} @ credit {args.credit:.2f}'
        )
    return wrapped


def _wrap_broker_stop_fill_sim(args, broker):
    """Fake exchange stop fill when simulating Scenario 5 (stop → long close)."""
    if not getattr(args, 'simulate_stop_fill', False):
        return broker
    from common.simulated_stop_fill import SimulatedStopFillBroker
    from blocks.stop import state as state_mod

    lot = getattr(args, 'lot', None)
    paths = _active_json_paths(lot)
    if not paths:
        print('  ERROR: --simulate-stop-fill needs active JSON for --lot')
        return broker
    st = state_mod.load_state(paths[0])
    active = st.get('active_stop') or {}
    stop_oid = active.get('order_id')
    if not stop_oid:
        print('  ERROR: JSON has no active_stop.order_id — seed or place stop first')
        return broker
    qty = int(st.get('filled_quantity') or st.get('quantity') or args.quantity)
    print(f'  Simulated stop fill: order {stop_oid} ({qty} contracts) → expect long leg LIMIT')
    return SimulatedStopFillBroker(broker, str(stop_oid), quantity=qty)


def _active_json_paths(lot: str | None = None) -> list[str]:
    from blocks.stop import state as state_mod

    paths = glob.glob(os.path.join(state_mod.active_dir(), '*.json'))
    if not lot:
        return paths
    safe = lot.replace('-', '')
    return [p for p in paths if safe in os.path.basename(p)]


def _trade_state(runner, path: str) -> dict:
    """Prefer monitor in-memory state; fall back to disk."""
    from blocks.stop import state as state_mod

    if runner is not None:
        st = runner.trade_state(path)
        if st is not None:
            return st
    return state_mod.load_state(path)


def _wait_for_active_stop(
    paths: list[str],
    timeout: float = 60.0,
    runner=None,
) -> bool:
    """Wait until JSON records a working stop (placed or adopted from broker)."""
    from blocks.stop import state as state_mod

    deadline = time.time() + timeout
    while time.time() < deadline:
        for path in paths:
            try:
                st = _trade_state(runner, path) if runner else state_mod.load_state(path)
            except PermissionError:
                continue
            active = st.get('active_stop')
            if not active or not active.get('order_id'):
                continue
            if active.get('status') in ('working', 'live', 'received', 'contingent', 'open'):
                return True
        time.sleep(0.5)
    return False


def cmd_sync_broker_stop(args) -> int:
    """Repair JSON/broker drift via strict orphan-stop adoption (dry-run unless --apply)."""
    from common.broker_factory import get_broker
    from blocks.stop import state as state_mod
    from blocks.stop.broker_sync import cancel_all_close_orders_on_short, repair_orphan_stop
    from blocks.stop.stop_ownership import collect_claimed_stop_order_ids, scan_duplicate_stop_ownership

    paths = _active_json_paths(getattr(args, 'lot', None))
    if not paths:
        print('  No files in trades/active/')
        return 1

    duplicates = scan_duplicate_stop_ownership(paths)
    if duplicates:
        for dup in duplicates:
            print(f'  CRITICAL duplicate active_stop {dup.order_id}: {list(dup.paths)}')

    broker = get_broker(paper=args.paper)
    apply = bool(getattr(args, 'apply', False))
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'--- Repair orphan stops ({mode}, {len(paths)} file(s)) ---')

    claimed = set(collect_claimed_stop_order_ids(paths).keys())
    for path in paths:
        st = state_mod.load_state(path)
        if getattr(args, 'cancel_broker_stops', False):
            if not apply:
                print(f'  DRY-RUN would cancel broker BTC + clear JSON: {path}')
                continue
            n = cancel_all_close_orders_on_short(st, broker)
            st['active_stop'] = None
            st['stop_quantity'] = 0
            state_mod.save_state(path, st)
            print(f'  Cancelled {n} broker close order(s); cleared JSON: {path}')
            continue

        outcome = repair_orphan_stop(
            st,
            broker,
            apply=apply,
            repair_reason='sync_broker_stop_cli',
            claimed_order_ids=claimed,
        )
        print(f'  {path}: {outcome.status} — {outcome.message}')
        if apply and outcome.status == 'adopted':
            state_mod.save_state(path, st)
            oid = state_mod.section(st, 'active_stop').get('order_id')
            if oid:
                claimed.add(str(oid))
            print(f'    → linked broker stop {oid}')
    return 0


def cmd_run_stop_monitor(args) -> int:
    from common import tt_config
    from common.broker_factory import get_broker
    from common.tt_auth import create_tastytrade_session, get_account
    from blocks.stop.alerts import AlertListener
    from blocks.stop.runner import MonitorRunner
    from blocks.stop import state as state_mod

    active = _active_json_paths(getattr(args, 'lot', None))
    print(f'--- Stop monitor ({len(active)} active file(s), {args.seconds}s) ---')
    if not active:
        print('  No files in trades/active/. Run place-trade or seed-stop first.')
        if getattr(args, 'lot', None):
            print(f'  (filtered by lot={args.lot})')
        return 1

    for p in active:
        print(f'  - {p}')

    paper = args.paper or tt_config.PAPER_MODE
    broker = get_broker(paper=paper)
    broker = _wrap_broker_stop_fill_sim(args, broker)
    broker = _wrap_broker_partial_sim(args, broker)
    alert_listener = None
    if tt_config.BROKER == 'tastytrade':
        session = create_tastytrade_session(paper=paper)
        account = get_account(session)
        alert_listener = AlertListener(session, account, paper=paper)

    runner = MonitorRunner(
        broker=broker,
        poll_interval=args.poll,
        alert_listener=alert_listener,
    )
    runner.prices.start()
    if alert_listener:
        alert_listener.start()

    mqtt = None
    watch = ['SPX']
    for p in active:
        st = state_mod.load_state(p)
        from common.symbols import to_tastytrade
        watch.append(to_tastytrade(st['short_leg']['symbol']))
        watch.append(to_tastytrade(st['long_leg']['symbol']))
    if getattr(args, 'mqtt_report', True):
        from common.mqtt_stats import MqttStatsCollector
        mqtt = MqttStatsCollector()
        mqtt.start()

    for p in active:
        runner.add(p)

    if getattr(args, 'simulate_breach', False):
        from blocks.stop.breach import spread_mark_price

        print('  Waiting for active_stop in JSON (from Step 1 stop placement)...')
        if not _wait_for_active_stop(active, timeout=60, runner=runner):
            print(
                '  ERROR: Breach simulation requires active_stop from Step 1 '
                '(run stop-session --seed without --simulate-breach first).'
            )
            runner.shutdown()
            if mqtt:
                mqtt.stop()
            return 1

        for p in active:
            st = _trade_state(runner, p)
            oid = (st.get('active_stop') or {}).get('order_id')
            print(f'  active_stop ready: {p} order_id={oid}')

        for p in active:
            st = _trade_state(runner, p)
            threshold = round(float(st['short_leg']['two_x_short']) + 0.20, 2)
            long_p = float(st['long_leg']['fill_price'])
            short_p = round(threshold + long_p + 0.50, 2)
            short_sym = st['short_leg']['symbol']
            long_sym = st['long_leg']['symbol']
            market_short = runner.prices.wait_for(short_sym, timeout=15.0)
            if market_short is None:
                print(f'  WARN: No live MQTT mid for {short_sym} yet — limit may not place')
            runner.prices.set_override(short_sym, short_p)
            runner.prices.set_override(long_sym, long_p)
            spread = spread_mark_price(short_p, long_p)
            print(
                f'  Breach simulation on {p}: override short={short_p} long={long_p} '
                f'spread={spread:.2f} threshold={threshold:.2f}'
            )
            print(
                f'    Limit close will use live MQTT short mid'
                f'{f"={market_short:.2f}" if market_short is not None else " (waiting for streamer)"}'
                f' — not the override'
            )
        time.sleep(15)

    stop_at = time.time() + args.seconds

    def _stop():
        while time.time() < stop_at:
            time.sleep(1)
        runner.shutdown()

    t = threading.Thread(target=_stop, daemon=True)
    t.start()
    print(f'  Monitoring until {args.seconds}s elapsed (Ctrl+C to stop early)...')
    try:
        while time.time() < stop_at:
            time.sleep(2)
            print('  status:', runner.status())
    except KeyboardInterrupt:
        pass
    finally:
        runner.shutdown()

    if mqtt:
        print('--- MQTT during stop monitor ---')
        for line in mqtt.report_lines(watch_symbols=watch):
            print(line)
        mqtt.stop()

    closed = glob.glob(os.path.join(state_mod.closed_dir(), '*.json'))
    print(f'  Closed trades: {len(closed)}')
    return 0


def cmd_partial_fill_session(args) -> int:
    """Scenario 4: pending JSON + streamer + stop_monitor while spread legs fill."""
    import subprocess

    from common.symbols import build_tastytrade_symbol, to_tastytrade
    from meic0dte.open import open_spread_tt
    from blocks.stop import state as state_mod

    side = args.side.upper()
    expiry = parse_expiry_yymmdd(args.expiry)
    short_sym = build_tastytrade_symbol(expiry, side, args.short_strike)
    long_sym = build_tastytrade_symbol(expiry, side, args.long_strike)
    lot = args.lot or 'jun22-ccs-partial'
    oid = args.open_order_id or '476911300'
    existing = _active_json_paths(lot)
    step = int(getattr(args, 'partial_step', 1) or 1)

    print('--- Partial fill session (Scenario 4) ---')

    if getattr(args, 'simulate_partial_fill', False):
        if step >= 2:
            if not existing:
                print('  ERROR: Step 2 needs Step 1 JSON. Run first with --partial-step 1:')
                print(
                    '    uv run python tests/adhoc_integration.py partial-fill-session '
                    '--simulate-partial-fill --partial-step 1 --partial-qty 2 ...'
                )
                return 1
            print(f'  Step 2: reusing {existing[0]}')
        elif existing and not getattr(args, 'force_pending', False):
            print(f'  Step 1: reusing existing JSON {existing[0]}')
        else:
            print(f'  Step 1: writing pending JSON for simulated open order {oid}')
            path = open_spread_tt.write_pending_trade_state(
                lot=lot,
                opt_type=side,
                short_symbol=short_sym,
                long_symbol=long_sym,
                short_strike=args.short_strike,
                long_strike=args.long_strike,
                target_quantity=args.quantity,
                open_order_id=oid,
                limit_credit=args.credit,
            )
            print(f'  {path}')
    elif getattr(args, 'reuse_json', False) and existing:
        print(f'  Reusing {existing[0]}')
    else:
        if not oid or oid == 'pending-manual':
            print('  ERROR: --open-order-id required unless --simulate-partial-fill')
            return 1
        print(f'  Writing pending JSON for open order {oid}')
        path = open_spread_tt.write_pending_trade_state(
            lot=lot,
            opt_type=side,
            short_symbol=short_sym,
            long_symbol=long_sym,
            short_strike=args.short_strike,
            long_strike=args.long_strike,
            target_quantity=args.quantity,
            open_order_id=oid,
            limit_credit=args.credit,
        )
        print(f'  {path}')

    if getattr(args, 'simulate_partial_fill', False):
        pq = getattr(args, 'partial_qty', None) or max(1, args.quantity // 2)
        if step <= 1:
            print(
                f'  Expect: STOP_LIMIT for {pq} spread unit(s) (short+long filled together); '
                f'resize when more units fill.'
            )
        else:
            print(
                f'  Expect: cancel prior short STOP_LIMIT and re-place for {args.quantity} '
                f'(sim updates JSON only — no long-leg entry order at broker).'
            )
    else:
        print('  Expect: stop placed when broker reports paired spread-unit fills; resize on more fills.')

    os.environ['MEIC_INTEGRATION'] = '1'
    streamer = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'streaming', 'publish_tastytrade.py')],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    print(f'  Streamer PID: {streamer.pid}')
    time.sleep(8)

    args.mqtt_report = True
    args.lot = lot
    args.open_order_id = oid
    code = cmd_run_stop_monitor(args)
    streamer.terminate()
    streamer.wait()
    return code


def cmd_test_long_close(args) -> int:
    """Place SELL_TO_CLOSE limit on long leg only (isolated test, JSON stays open)."""
    import subprocess
    from unittest.mock import patch

    from common.broker_factory import get_broker
    from blocks.stop import state as state_mod
    from blocks.stop.monitor import StopMonitor
    from blocks.stop.mqtt_prices import MqttPriceCache

    paths = _active_json_paths(args.lot)
    if not paths:
        print('  No active JSON for lot=%s' % args.lot)
        return 1
    path = paths[0]
    st = state_mod.load_state(path)
    if st.get('status') not in ('open', 'pending_fill'):
        print(f'  Trade status is {st.get("status")!r} — expected open')
        return 1

    side = st.get('entry', {}).get('side', args.side).upper()
    long_sym = st['long_leg']['symbol']
    json_qty = int(st.get('filled_quantity') or st.get('quantity') or 1)
    qty = json_qty if args.quantity is None else int(args.quantity)
    print('--- Test long leg close only ---')
    print(f'  JSON: {path}')
    print(f'  Long: {long_sym} qty={qty} (SELL_TO_CLOSE limit at MQTT mid)')
    if args.quantity is not None and args.quantity != json_qty:
        print(f'  (CLI --quantity {args.quantity} overrides JSON filled_quantity {json_qty})')
    print('  Short stop is NOT touched; JSON is NOT moved to closed.')

    os.environ['MEIC_INTEGRATION'] = '1'
    streamer = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'streaming', 'publish_tastytrade.py')],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    time.sleep(8)

    broker = get_broker(paper=args.paper)
    prices = MqttPriceCache()
    prices.start()
    mid = prices.wait_for(long_sym, timeout=20.0)
    print(f'  MQTT mid for long: {mid}')

    with patch('common.streamer_symbols.register_spread_symbols'):
        mon = StopMonitor(path, broker, prices)
    mon.state['filled_quantity'] = qty
    mon._close_long_leg()

    prices.stop()
    streamer.terminate()
    streamer.wait()
    print('  Check TastyTrade for working SELL_TO_CLOSE on the long strike.')
    return 0


def cmd_stop_fill_session(args) -> int:
    """
    Scenario 5: simulate exchange stop filled → monitor places long leg close.
    Cancels the live short stop at broker by default so you do not keep two exits.
    """
    import subprocess

    from common.broker_factory import get_broker
    from blocks.stop import state as state_mod

    lot = args.lot or 'jun22-ccs-7635'
    paths = _active_json_paths(lot)
    if not paths:
        print('  ERROR: No active JSON. Run stop-session --seed first.')
        return 1
    path = paths[0]
    st = state_mod.load_state(path)
    active = st.get('active_stop') or {}
    stop_oid = active.get('order_id')
    if not stop_oid:
        print('  ERROR: No active_stop in JSON — place exchange stop first (Scenario 2 Step 1).')
        return 1

    print('--- Stop fill session (Scenario 5: stop filled → long close) ---')
    print(f'  JSON: {path}')
    print(f'  active_stop: {stop_oid}')

    broker = get_broker(paper=args.paper)
    if not getattr(args, 'keep_live_stop', False):
        print(f'  Cancelling live short stop {stop_oid} at broker before sim...')
        cancel = broker.cancel_order(str(stop_oid))
        print(f'  Cancel result: {cancel.status} ({cancel.message or "ok"})')
    else:
        print('  WARNING: --keep-live-stop: live stop remains; long close will also be placed')

    os.environ['MEIC_INTEGRATION'] = '1'
    streamer = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'streaming', 'publish_tastytrade.py')],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    print(f'  Streamer PID: {streamer.pid}')
    time.sleep(8)

    args.mqtt_report = True
    args.lot = lot
    args.simulate_stop_fill = True
    code = cmd_run_stop_monitor(args)
    streamer.terminate()
    streamer.wait()

    closed = glob.glob(os.path.join(state_mod.closed_dir(), '*.json'))
    print(f'  Closed trades: {len(closed)}')
    print('  Expect: SELL_TO_CLOSE LIMIT on long leg; JSON moved to trades/closed/')
    return code


def cmd_stop_session(args) -> int:
    """Seed existing position JSON + streamer + stop_monitor + MQTT report."""
    import subprocess

    from common.mqtt_stats import MqttStatsCollector
    from common.symbols import build_tastytrade_symbol, to_tastytrade

    lot = args.lot or 'jun22-ccs-7635'
    existing = _active_json_paths(lot)

    if args.simulate_breach:
        if args.seed and existing and not getattr(args, 'force_seed', False):
            print('--- Breach step: reusing Step 1 JSON (skipping --seed wipe) ---')
            print(f'  {existing[0]}')
            args.seed = False
        elif not args.seed and not existing:
            print('  ERROR: Breach test needs Step 1 JSON. Run first without --simulate-breach:')
            print(
                '    uv run python tests/adhoc_integration.py stop-session --seed '
                '--side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 ...'
            )
            return 1

    print('--- Stop session (existing position flow) ---')
    if args.seed:
        code = cmd_seed_stop(args)
        if code != 0:
            return code

    os.environ['MEIC_INTEGRATION'] = '1'
    streamer = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'streaming', 'publish_tastytrade.py')],
        cwd=ROOT,
        env=os.environ.copy(),
    )
    print(f'  Streamer PID: {streamer.pid}')
    time.sleep(8)

    args.mqtt_report = True
    code = cmd_run_stop_monitor(args)

    exp = parse_expiry_yymmdd(args.expiry)
    side = args.side.upper()
    watch = [
        'SPX',
        to_tastytrade(build_tastytrade_symbol(exp, side, args.short_strike)),
    ]
    if args.long_strike:
        watch.append(to_tastytrade(build_tastytrade_symbol(exp, side, args.long_strike)))

    print('--- MQTT summary (post-run snapshot) ---')
    mqtt = MqttStatsCollector()
    mqtt.start()
    time.sleep(5)
    for line in mqtt.report_lines(watch_symbols=watch):
        print(line)
    mqtt.stop()
    streamer.terminate()
    streamer.wait()
    return code


def cmd_simulate_breach(args) -> int:
    """Offline: show when Phase 1 software breach fires vs exchange stop."""
    from blocks.stop.breach import spread_breach_triggered, spread_mark_price
    import meic0dte.app.config as app_config

    short_fill = args.short_fill
    long_fill = args.long_fill
    stop_mult = app_config.STOP_PRCNT_C if args.side.upper() == 'C' else app_config.STOP_PRCNT_P
    two_x_short = round(round(short_fill * 2.0 / 0.05) * 0.05, 2)
    exchange_stop = round(((short_fill - 0.10) * stop_mult) / 0.05) * 0.05
    exchange_stop = round(exchange_stop, 2)
    phase1_threshold = round(two_x_short + 0.20, 2)
    net_credit = round(short_fill - long_fill, 2)

    print('--- Breach simulation (Phase 1 software monitor) ---')
    print(f'  Entry: short={short_fill:.2f} long={long_fill:.2f} credit={net_credit:.2f}')
    print(f'  Exchange stop on SHORT @ {exchange_stop:.2f} debit (BUY TO CLOSE stop-limit)')
    print(f'  Phase 1 spread threshold: {phase1_threshold:.2f}  (two_x_short + 0.20)')
    print()
    print('  Scenario                          spread   breach?')
    scenarios = [
        ('unchanged', short_fill, long_fill),
        ('short +50%', short_fill * 1.5, long_fill),
        ('short 2x', short_fill * 2, long_fill),
        ('short 2x long up', short_fill * 2, long_fill * 1.5),
        ('at threshold', phase1_threshold + long_fill, long_fill),
    ]
    for label, short_p, long_p in scenarios:
        spread = spread_mark_price(short_p, long_p)
        breach = spread_breach_triggered(spread, phase1_threshold)
        print(f'  {label:30s}  {spread:6.2f}   {breach}')
    print()
    print('  Exchange STOP: triggers when short leg trade price hits stop trigger.')
    print('  Phase 1: software replaces stop when (short_mid - long_mid) >= threshold.')
    return 0


def cmd_integration_session(args) -> int:
    """Delegate to run.py --integration-session (full production path)."""
    import subprocess

    cmd = [
        sys.executable,
        os.path.join(ROOT, 'run.py'),
        '--integration-session',
        '--expiry', args.expiry,
        '--duration', str(args.duration),
        '--lot', args.lot or 'integration-session',
    ]
    if args.paper:
        cmd.append('--paper')
    print('--- Launching run.py integration session ---')
    print('  ', ' '.join(cmd))
    return subprocess.call(cmd, cwd=ROOT)


def cmd_check_all(args) -> int:
    codes = [
        cmd_check_env(args),
        cmd_check_mqtt(args),
        cmd_check_auth(args),
        cmd_check_prices(args),
    ]
    return max(codes)


def cmd_full_smoke(args) -> int:
    code = cmd_check_all(args)
    if code != 0:
        return code
    if args.place:
        code = cmd_place_trade(args)
        if code != 0:
            return code
    if args.monitor_seconds > 0:
        args.seconds = args.monitor_seconds
        return cmd_run_stop_monitor(args)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='MEIC ad-hoc integration tests')
    parser.add_argument('--paper', action='store_true', help='Use paper/tastyware session')
    parser.add_argument('-v', '--verbose', action='store_true')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('check-env', help='Validate .env configuration')
    sub.add_parser('check-mqtt', help='Test Mosquitto connectivity')
    p_auth = sub.add_parser('check-auth', help='Test broker authentication')
    p_auth.set_defaults(func=cmd_check_auth)

    p_prices = sub.add_parser('check-prices', help='Fetch SPX price')
    p_prices.set_defaults(func=cmd_check_prices)

    p_all = sub.add_parser('check-all', help='Run all connectivity checks')
    p_all.set_defaults(func=cmd_check_all)

    p_trade = sub.add_parser('place-trade', help='Place one thin spread + write JSON')
    p_trade.add_argument('--side', choices=['P', 'C'], default='P')
    p_trade.add_argument('--expiry', default=None, help='YYYY-MM-DD or YYMMDD (required with strikes)')
    p_trade.add_argument('--short-strike', type=int, default=None)
    p_trade.add_argument('--long-strike', type=int, default=None)
    p_trade.add_argument('--credit', type=float, default=None, help='Limit credit; quote if omitted')
    p_trade.add_argument('--lot', default=None)
    p_trade.add_argument('--quantity', type=int, default=1)
    p_trade.set_defaults(func=cmd_place_trade)

    p_seed = sub.add_parser('seed-stop', help='Seed JSON for existing position')
    p_seed.add_argument('--side', choices=['P', 'C'], required=True)
    p_seed.add_argument('--expiry', default=None, help='YYYY-MM-DD or YYMMDD (default: today)')
    p_seed.add_argument('--short-strike', type=int, required=True)
    p_seed.add_argument('--long-strike', type=int, required=True)
    p_seed.add_argument('--short-fill', type=float, default=4.0)
    p_seed.add_argument('--long-fill', type=float, default=2.5)
    p_seed.add_argument('--credit', type=float, default=1.5)
    p_seed.add_argument('--quantity', type=int, default=1)
    p_seed.add_argument('--lot', default=None)
    p_seed.add_argument('--open-order-id', default=None)
    p_seed.add_argument(
        '--skip-auto-stop',
        action='store_true',
        help='Do not let stop_monitor place 2x short stop on pickup',
    )
    p_seed.set_defaults(func=cmd_seed_stop)

    p_stop = sub.add_parser('place-stop', help='Place stop on short leg (existing position)')
    p_stop.add_argument('--side', choices=['P', 'C'], required=True)
    p_stop.add_argument('--expiry', default=None, help='YYYY-MM-DD or YYMMDD (default: today)')
    p_stop.add_argument('--short-strike', type=int, required=True)
    p_stop.add_argument('--long-strike', type=int, default=None, help='Required with --write-state')
    p_stop.add_argument('--stop', type=float, required=True, help='Stop trigger price on short leg')
    p_stop.add_argument('--limit', type=float, default=None, help='Limit price (default: stop + offset)')
    p_stop.add_argument('--quantity', type=int, default=1)
    p_stop.add_argument('--short-fill', type=float, default=4.0)
    p_stop.add_argument('--long-fill', type=float, default=2.5)
    p_stop.add_argument('--credit', type=float, default=1.5)
    p_stop.add_argument('--lot', default=None)
    p_stop.add_argument('--open-order-id', default=None)
    p_stop.add_argument(
        '--write-state',
        action='store_true',
        help='Write trades/active JSON and record stop via stop_monitor',
    )
    p_stop.set_defaults(func=cmd_place_stop)

    p_sync = sub.add_parser(
        'sync-broker-stop',
        help='Repair orphan broker stop into JSON (dry-run unless --apply)',
    )
    p_sync.add_argument('--lot', default=None, help='Only sync JSONs matching this lot')
    p_sync.add_argument(
        '--apply',
        action='store_true',
        help='Write adoption to JSON (default: dry-run only)',
    )
    p_sync.add_argument(
        '--cancel-broker-stops',
        action='store_true',
        help='Cancel all broker close orders on short leg and clear JSON (requires --apply)',
    )
    p_sync.set_defaults(func=cmd_sync_broker_stop)

    p_repair = sub.add_parser(
        'repair-orphaned-stops',
        help='Alias for sync-broker-stop (explicit repair path)',
    )
    p_repair.add_argument('--lot', default=None)
    p_repair.add_argument('--apply', action='store_true')
    p_repair.add_argument('--cancel-broker-stops', action='store_true')
    p_repair.set_defaults(func=cmd_sync_broker_stop)

    p_mon = sub.add_parser('run-stop-monitor', help='Run stop_monitor on active JSONs')
    p_mon.add_argument('--seconds', type=int, default=120)
    p_mon.add_argument('--poll', type=float, default=5.0)
    p_mon.add_argument('--lot', default=None, help='Only monitor JSONs matching this lot name')
    p_mon.add_argument('--simulate-breach', action='store_true')
    p_mon.add_argument(
        '--simulate-stop-fill',
        action='store_true',
        help='Treat active_stop as filled; triggers long leg close (Scenario 5)',
    )
    p_mon.set_defaults(func=cmd_run_stop_monitor)

    p_int = sub.add_parser(
        'integration-session',
        help='Full off-hours flow: streamer + tranche + stop_monitor + MQTT report',
    )
    p_int.add_argument('--expiry', default='2026-06-22', help='Target expiry YYYY-MM-DD')
    p_int.add_argument('--duration', type=int, default=300, help='MQTT collection seconds')
    p_int.add_argument('--lot', default=None)
    p_int.set_defaults(func=cmd_integration_session)

    p_ss = sub.add_parser(
        'stop-session',
        help='Seed existing position + streamer + stop_monitor + MQTT report',
    )
    p_ss.add_argument('--seed', action='store_true', help='Call seed-stop first (Step 1 only)')
    p_ss.add_argument(
        '--force-seed',
        action='store_true',
        help='With --seed + --simulate-breach, wipe JSON even if Step 1 file exists',
    )
    p_ss.add_argument('--side', choices=['P', 'C'], default='C')
    p_ss.add_argument('--expiry', default='2026-06-22')
    p_ss.add_argument('--short-strike', type=int, default=7635)
    p_ss.add_argument('--long-strike', type=int, default=7660)
    p_ss.add_argument('--short-fill', type=float, default=1.45)
    p_ss.add_argument('--long-fill', type=float, default=0.85)
    p_ss.add_argument('--credit', type=float, default=0.6)
    p_ss.add_argument('--quantity', type=int, default=5)
    p_ss.add_argument('--lot', default='jun22-ccs-7635')
    p_ss.add_argument('--open-order-id', default='476911300')
    p_ss.add_argument(
        '--skip-auto-stop',
        action='store_true',
        help='Do not let stop_monitor place 2x short stop on pickup (passed to seed)',
    )
    p_ss.add_argument('--seconds', type=int, default=300)
    p_ss.add_argument('--poll', type=float, default=5.0)
    p_ss.add_argument(
        '--simulate-breach',
        action='store_true',
        help='Inject MQTT price overrides to trigger Phase 1 limit close',
    )
    p_ss.set_defaults(func=cmd_stop_session)

    p_pf = sub.add_parser(
        'partial-fill-session',
        help='Scenario 4: pending JSON + monitor while spread legs fill (or simulate)',
    )
    p_pf.add_argument('--side', choices=['P', 'C'], default='C')
    p_pf.add_argument('--expiry', default='2026-06-22')
    p_pf.add_argument('--short-strike', type=int, default=7635)
    p_pf.add_argument('--long-strike', type=int, default=7660)
    p_pf.add_argument('--short-fill', type=float, default=1.45)
    p_pf.add_argument('--long-fill', type=float, default=0.85)
    p_pf.add_argument('--credit', type=float, default=0.6)
    p_pf.add_argument('--quantity', type=int, default=5)
    p_pf.add_argument('--lot', default='jun22-ccs-partial')
    p_pf.add_argument('--open-order-id', default='476911300')
    p_pf.add_argument('--seconds', type=int, default=300)
    p_pf.add_argument('--poll', type=float, default=5.0)
    p_pf.add_argument(
        '--simulate-partial-fill',
        action='store_true',
        help='Fake open-order leg fills via get_order_status (no live partial spread needed)',
    )
    p_pf.add_argument(
        '--partial-step',
        type=int,
        choices=[1, 2],
        default=1,
        help='1=partial paired spread units; 2=full spread (reuse Step 1 JSON)',
    )
    p_pf.add_argument(
        '--partial-qty',
        type=int,
        default=None,
        help='Contracts filled on short leg in Step 1 (default: half of --quantity)',
    )
    p_pf.add_argument(
        '--force-pending',
        action='store_true',
        help='Rewrite pending JSON even if lot file exists (Step 1 sim only)',
    )
    p_pf.add_argument(
        '--reuse-json',
        action='store_true',
        help='Skip writing pending JSON when file exists (live partial fill)',
    )
    p_pf.set_defaults(func=cmd_partial_fill_session)

    p_tlc = sub.add_parser(
        'test-long-close',
        help='Place long leg SELL_TO_CLOSE only (JSON stays open; for Jun 22 position test)',
    )
    p_tlc.add_argument('--lot', default='jun22-ccs-7635')
    p_tlc.add_argument('--side', choices=['P', 'C'], default='C')
    p_tlc.add_argument('--quantity', type=int, default=None, help='Override qty (default: JSON filled_quantity)')
    p_tlc.set_defaults(func=cmd_test_long_close)

    p_sf = sub.add_parser(
        'stop-fill-session',
        help='Scenario 5: sim exchange stop filled → long leg close (cancel live stop first)',
    )
    p_sf.add_argument('--lot', default='jun22-ccs-7635')
    p_sf.add_argument('--quantity', type=int, default=5)
    p_sf.add_argument('--seconds', type=int, default=120)
    p_sf.add_argument('--poll', type=float, default=5.0)
    p_sf.add_argument(
        '--keep-live-stop',
        action='store_true',
        help='Do not cancel working short stop before sim (not recommended)',
    )
    p_sf.set_defaults(func=cmd_stop_fill_session)

    p_breach = sub.add_parser(
        'simulate-breach',
        help='Offline: Phase 1 breach vs exchange stop (no broker)',
    )
    p_breach.add_argument('--side', choices=['P', 'C'], default='C')
    p_breach.add_argument('--short-fill', type=float, default=4.0)
    p_breach.add_argument('--long-fill', type=float, default=2.5)
    p_breach.set_defaults(func=cmd_simulate_breach)

    p_smoke = sub.add_parser('full-smoke', help='check-all + optional trade + monitor')
    p_smoke.add_argument('--place', action='store_true', help='Also place a test trade')
    p_smoke.add_argument('--side', choices=['P', 'C'], default='P')
    p_smoke.add_argument('--lot', default=None)
    p_smoke.add_argument('--quantity', type=int, default=1)
    p_smoke.add_argument('--monitor-seconds', type=int, default=0)
    p_smoke.set_defaults(func=cmd_full_smoke)

    args = parser.parse_args()
    _setup_logging(args.verbose)

    if args.paper:
        os.environ['PAPER_MODE'] = 'true'
        import importlib
        import common.tt_config as tc
        importlib.reload(tc)

    # Commands without func use direct dispatch
    dispatch = {
        'check-env': cmd_check_env,
        'check-mqtt': cmd_check_mqtt,
    }
    fn = getattr(args, 'func', None) or dispatch.get(args.command)
    if fn:
        return fn(args)
    print('Unknown command')
    return 1


if __name__ == '__main__':
    sys.exit(main())
