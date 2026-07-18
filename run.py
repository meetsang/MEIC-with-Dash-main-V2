"""
MEIC Autotrader - Single launcher
Starts streamer, stop_monitor (TastyTrade), and thin tranches at scheduled times.

Usage:
  python run.py [--paper]
  python run.py --integration-tranche   # off-hours: dashboard + streamer + one tranche, no stop_monitor
"""
import os
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import common.win_ssl_env  # noqa: F401 — before any HTTPS/ssl usage

import argparse
import glob
import time, subprocess, logging, json, threading
from datetime import datetime as dt, time as t, timedelta, timezone

# Make sure project root is in path
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import config as common_config
from common import tt_config
from common.auth import refresh_token as _refresh_token
from common.broker_factory import get_streamer_script, use_thin_tranches
from common.session_cleanup import run_session_cleanup
from common.trades_layout import ops_path
from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from strategies.loader import load_enabled_strategies
from strategies.validate import StrategyConfigError, validate_startup_config
from common.session_logs import LAUNCHER_BASE, MARKET_DATA_BASE, new_session_log_path, relocate_all_legacy_logs
from common.logging_config import setup_session_logging, terminal_info
from common.service_health import check_mqtt_cache_health, check_stop_monitor_health, check_streamer_health, scan_price_gate_trades
from meic0dte.app.utilities import central_now
from common.runtime_session import MEIC_SPX_0DTE, runtime_should_stop_for_session

# Tranches moved to strategies/meic/strategy.py (MEIC_TRANCHE_SLOTS) — loaded via Orchestrator.

STREAM_STOP_HOUR   = 15   # Streamer/bot both stop at 3:00 PM
CLEANUP_EOD_HOUR   = 15   # EOD history sync at 3:30 PM (archive only at morning 8:20)
CLEANUP_EOD_MIN    = 30
STREAM_START_HOUR  = 8    # Start streaming at 8:30 AM Central
STREAM_START_MIN   = 30

CENTRAL_STD_OFFSET = -6
CENTRAL_DST_OFFSET = -5


def _nth_weekday_of_month(year, month, weekday, n):
    """Return the date for the nth weekday in a month."""
    first_day = dt(year, month, 1)
    days_until_weekday = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_weekday + 7 * (n - 1))


def _central_dst_bounds(year):
    """Return the local wall-clock DST transition bounds for Central time."""
    dst_start = _nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    dst_end = _nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    return dst_start, dst_end


def _is_central_dst(local_dt):
    dst_start, dst_end = _central_dst_bounds(local_dt.year)
    return dst_start <= local_dt < dst_end


def _central_offset_hours(local_dt):
    return CENTRAL_DST_OFFSET if _is_central_dst(local_dt) else CENTRAL_STD_OFFSET


def _utc_to_central(utc_dt):
    """Convert a UTC datetime to Central wall time as a naive datetime."""
    year = utc_dt.year
    dst_start_local, dst_end_local = _central_dst_bounds(year)

    # Convert local transition instants to UTC so any UTC timestamp can be classified.
    dst_start_utc = (dst_start_local - timedelta(hours=CENTRAL_STD_OFFSET)).replace(tzinfo=timezone.utc)
    dst_end_utc = (dst_end_local - timedelta(hours=CENTRAL_DST_OFFSET)).replace(tzinfo=timezone.utc)

    offset = CENTRAL_DST_OFFSET if dst_start_utc <= utc_dt < dst_end_utc else CENTRAL_STD_OFFSET
    return (utc_dt + timedelta(hours=offset)).replace(tzinfo=None)


def _central_now():
    return _utc_to_central(dt.now(timezone.utc))


def _central_to_utc(local_dt):
    """Convert a Central wall-clock datetime to UTC as a naive datetime."""
    offset = _central_offset_hours(local_dt)
    return local_dt - timedelta(hours=offset)

_relocated = relocate_all_legacy_logs(ROOT)
LAUNCHER_LOG = str(new_session_log_path(ROOT, LAUNCHER_BASE, when=central_now()))

log = setup_session_logging('launcher', LAUNCHER_LOG, stream_prefix='LAUNCHER', file_mode='w')
if _relocated:
    log.info("Relocated legacy logs: %s", ", ".join(str(p) for p in _relocated))
log.info("Launcher log: %s", LAUNCHER_LOG)

STATUS_FILE = os.path.join(ROOT, 'dashboard', 'bot_status.json')
_SUBPROCESS_QUIET = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
_terminal_warn_at: dict[str, float] = {}
_TERMINAL_WARN_COOLDOWN_SEC = 300.0


def _terminal_warn_once(key: str, msg: str) -> None:
    """Rate-limited operator terminal alert (WARNING → passes terminal filter)."""
    now = time.time()
    last = _terminal_warn_at.get(key, 0.0)
    if now - last < _TERMINAL_WARN_COOLDOWN_SEC:
        return
    _terminal_warn_at[key] = now
    log.warning(msg)


def _check_running_services_health(*, stop_mon_running: bool) -> None:
    ok, detail = check_streamer_health(ROOT)
    if not ok:
        _terminal_warn_once('streamer_stale', detail)
    if stop_mon_running:
        ok, detail = check_stop_monitor_health(ROOT)
        if not ok:
            _terminal_warn_once('stop_monitor_stale', detail)
        ok, detail = check_mqtt_cache_health(ROOT)
        if not ok:
            _terminal_warn_once('stop_monitor_mqtt_stale', detail)
    ok, detail = scan_price_gate_trades(ROOT)
    if not ok:
        _terminal_warn_once('price_gates', detail)

def _write_status(state: str, reason: str = ''):
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump({'state': state, 'reason': reason,
                       'ts': _central_now().strftime('%Y-%m-%d %H:%M:%S')}, f)
    except Exception:
        pass


def _token_refresh_loop():
    """Refresh the Schwab access token every 25 minutes in the background.
    Schwab tokens expire after 30 min — this keeps them fresh all day.
    """
    import logging as _logging
    from common.logging_config import setup_file_only_logging

    rlog = setup_file_only_logging(
        'token_refresh',
        LAUNCHER_LOG,
        stream_prefix='TOKEN',
        file_mode='a',
    )
    while True:
        time.sleep(25 * 60)  # wait 25 minutes
        try:
            _refresh_token.refresh(rlog)
            rlog.info('Token refreshed successfully.')
        except Exception as e:
            rlog.error(f'Token refresh failed: {e}')


def wait_until_central(target):
    """Block until Central wall time reaches target (naive datetime).

    Re-checks wall clock each iteration so resume is correct after OS sleep/hibernate
    (unlike a single long time.sleep, which pauses while the machine is suspended).
    """
    while _central_now() < target:
        diff = (_central_to_utc(target) - dt.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        if diff > 60:
            time.sleep(30)  # coarse sleep when far away
        else:
            time.sleep(1)   # fine sleep when close


def wait_until(hour, minute, second=0):
    """Block until the next occurrence of HH:MM:SS in Central time."""
    while True:
        now = _central_now()
        target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if now >= target:
            return  # already past this time today
        wait_until_central(target)
        return


def start_streamer():
    script = get_streamer_script()
    log.info("Starting %s ...", script)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, script)],
        cwd=ROOT,
        **_SUBPROCESS_QUIET,
    )
    log.info(f"Streamer PID: {proc.pid}")
    return proc


def start_stop_monitor(paper: bool = False):
    """Start centralized stop_monitor supervisor (TastyTrade mode)."""
    if not use_thin_tranches():
        return None
    cmd = [sys.executable, os.path.join(ROOT, 'blocks', 'stop', 'run.py')]
    if paper or tt_config.PAPER_MODE:
        cmd.append('--paper')
    log.info('Starting stop_monitor: %s', ' '.join(cmd))
    proc = subprocess.Popen(cmd, cwd=ROOT, **_SUBPROCESS_QUIET)
    log.info('Stop monitor PID: %s', proc.pid)
    return proc


def start_market_data_recorder():
    """MQTT index/equity OHLC recorder — runs alongside streamer."""
    log.info('Starting market_data recorder ...')
    proc = subprocess.Popen(
        [sys.executable, '-m', 'market_data.run'],
        cwd=ROOT,
        **_SUBPROCESS_QUIET,
    )
    log.info('Market data recorder PID: %s', proc.pid)
    return proc


def run_tranche(wait: bool = False, lot: str | None = None, extra_env: dict | None = None):
    def _launch():
        log.info("Launching MEIC entry for lot via app_main (session CSV workers) ...")
        env = os.environ.copy()
        if lot:
            env['MEIC_LOT'] = lot
        if extra_env:
            env.update(extra_env)
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "meic0dte", "app_main.py")],
            cwd=ROOT,
            env=env,
            **_SUBPROCESS_QUIET,
        )
        proc.wait()
        log.info(f"Tranche finished (exit code {proc.returncode})")
        return proc.returncode

    if wait:
        return _launch()
    threading.Thread(target=_launch, daemon=True).start()
    return None


def integration_session(
    paper: bool = False,
    expiry: str = '2026-06-22',
    duration: int = 300,
    lot: str = 'integration-session',
) -> None:
    """Full-path integration: streamer + stop_monitor + tranche + MQTT stats."""
    import json

    from common.integration_report import REPORT_PATH, clear
    from common.mqtt_stats import MqttStatsCollector

    clear()
    session_env = {
        'MEIC_INTEGRATION': '1',
        'MEIC_FORCE_TRADE': '1',
        'MEIC_EXPIRY': expiry,
    }
    for key, val in session_env.items():
        os.environ[key] = val

    log.info('Integration session: expiry=%s duration=%ss lot=%s', expiry, duration, lot)
    streamer = start_streamer()
    stop_mon = start_stop_monitor(paper=paper)
    time.sleep(10)

    mqtt = MqttStatsCollector()
    mqtt.start()

    code = run_tranche(wait=True, lot=lot, extra_env=session_env)
    log.info('Tranche subprocess exit %s — collecting MQTT for %ss', code, duration)
    time.sleep(duration)

    print('\n--- Integration session report ---')
    report_path = REPORT_PATH
    if os.path.exists(report_path):
        with open(report_path, encoding='utf-8') as f:
            events = json.load(f)
        print(f'  Order events: {len(events)}')
        for ev in events:
            if ev.get('event') == 'open_order':
                print(
                    f"    {ev.get('side')} order_id={ev.get('order_id')} "
                    f"status={ev.get('status')} credit={ev.get('credit')} "
                    f"strikes={ev.get('short_strike')}/{ev.get('long_strike')}"
                )
    else:
        print('  No integration_report.json (no orders recorded)')

    active = glob.glob(os.path.join(ROOT, tt_config.TRADES_ACTIVE_DIR, '*.json'))
    print(f'  {tt_config.TRADES_ACTIVE_DIR} JSON files: {len(active)}')
    for p in active:
        try:
            with open(p, encoding='utf-8') as f:
                st = json.load(f)
            print(
                f"    {p}  status={st.get('status')} "
                f"filled={st.get('filled_quantity')}/{st.get('quantity')} "
                f"order={st.get('open_order_id')}"
            )
        except (json.JSONDecodeError, OSError):
            print(f'    {p}')

    for line in mqtt.report_lines(watch_symbols=['SPX']):
        print(line)
    mqtt.stop()

    streamer.terminate()
    streamer.wait()
    if stop_mon:
        stop_mon.terminate()
        stop_mon.wait()
    log.info('Integration session complete.')


def check_trading_day():
    """Returns (should_trade, reason). Fetches fresh FOMC dates from the Fed site."""
    today     = _central_now()
    yesterday = today - timedelta(days=1)

    today_str     = today.strftime('%y%m%d')
    yesterday_str = yesterday.strftime('%y%m%d')
    today_fmt     = today.strftime('%Y-%m-%d')

    log.info(f"Checking if {today_fmt} is a trading day ...")

    # Re-fetch FOMC dates fresh each morning (also fetch prior year in case Jan 1)
    fomc_days     = set(common_config._fetch_fomc_dates(today.year))
    fomc_days    |= set(common_config._fetch_fomc_dates(yesterday.year))
    market_closed = set(common_config._get_nyse_holidays(today.year))

    if today_str in market_closed:
        return False, f"NYSE is CLOSED today ({today_fmt}). No trading."
    if today_str in fomc_days:
        return False, f"Today ({today_fmt}) is a FOMC day. Skipping trading."
    if yesterday_str in fomc_days:
        return False, f"Yesterday ({yesterday.strftime('%Y-%m-%d')}) was a FOMC day — skipping the day after. No trading."
    return True, f"{today_fmt} is a normal trading day. Proceeding."


def _run_eod_cleanup_if_due(logger) -> None:
    """After 3:00 PM session end, wait until 3:30 for EOD sync (no active/ archive)."""
    now = _central_now()
    if now.time() < t(STREAM_STOP_HOUR, 0):
        return
    if now.time() < t(CLEANUP_EOD_HOUR, CLEANUP_EOD_MIN):
        logger.info('Waiting until %02d:%02d for end-of-day sync ...', CLEANUP_EOD_HOUR, CLEANUP_EOD_MIN)
        wait_until(CLEANUP_EOD_HOUR, CLEANUP_EOD_MIN)
    try:
        run_session_cleanup('eod', logger)
    except Exception:
        logger.exception('End-of-day session sync failed')


def main(
    paper: bool = False,
    *,
    force: bool = False,
    tranche_now: bool = False,
    once: bool = False,
    no_stop_monitor: bool = False,
    lot: str | None = None,
):
    now = _central_now()
    log.info(f"MEIC Launcher started at {now.strftime('%H:%M:%S')} (broker={tt_config.BROKER}, paper={paper or tt_config.PAPER_MODE})")

    try:
        validate_startup_config()
        log.info('Strategy config validated (config/strategies.yaml)')
    except StrategyConfigError as exc:
        log.error('Invalid strategies.yaml — %s', exc)
        _write_status('skipped', str(exc))
        return

    # --- Daily check: skip if holiday or FOMC ---
    if not force:
        should_trade, reason = check_trading_day()
        if not should_trade:
            terminal_info(log, reason)
            log.info("Exiting. No trades will be placed today.")
            _write_status('skipped', reason)
            return
        log.info(reason)
    else:
        log.info('Force mode — skipping trading-day check.')

    from common.trading_gate import initialize_for_session_date
    from common.probe_coordinator import (
        meic_tranches_from_slots,
        start_coordinator,
        stop_coordinator,
    )
    from strategies.meic.strategy import MEIC_TRANCHE_SLOTS

    session_date_ct = now.strftime('%Y-%m-%d')
    initialize_for_session_date(session_date_ct)

    # Background startup + pre-tranche probes — do NOT block service start.
    try:
        start_coordinator(
            session_date_ct=session_date_ct,
            tranches=meic_tranches_from_slots(MEIC_TRANCHE_SLOTS),
            paper=bool(paper or tt_config.PAPER_MODE),
            logger=log,
        )
    except Exception:
        log.exception('Failed to start REST probe coordinator')

    _write_status('running', 'Bot is active.')

    # Wait for stream start time if we're early (unless force + tranche_now)
    if not force and now.time() < t(STREAM_START_HOUR, STREAM_START_MIN):
        log.info(f"Waiting until {STREAM_START_HOUR:02d}:{STREAM_START_MIN:02d} to start streamer ...")
        wait_until(STREAM_START_HOUR, STREAM_START_MIN)

    session_started = _central_now()
    if (
        not force
        and not tranche_now
        and runtime_should_stop_for_session(
            session_started,
            session_started,
            profile=MEIC_SPX_0DTE,
        )
    ):
        log.info(
            'MEIC session already closed before service start — '
            'skipping streamer, stop_monitor, and market_data recorder.'
        )
        stop_coordinator()
        _run_eod_cleanup_if_due(log)
        _write_status('stopped', 'Session already closed before service start.')
        return

    streamer = start_streamer()
    market_data = start_market_data_recorder()
    stop_mon = None if no_stop_monitor else start_stop_monitor(paper=paper)
    if no_stop_monitor:
        log.info('stop_monitor disabled for this session.')
    time.sleep(10)  # give streamer time to connect before first tranche

    strategies = load_enabled_strategies()
    scheduled = [s for s in strategies if s.schedule()]
    log.info(
        'Loaded %d enabled strategies (%d scheduled): %s',
        len(strategies),
        len(scheduled),
        ', '.join(s.config.name for s in strategies) or 'none',
    )
    if not scheduled:
        log.warning('No scheduled strategies enabled — entry monitor will have no MEIC rows.')

    bootstrap_meic_session_if_missing(ROOT)
    # Prefer session CSV pause/skip eligibility when available
    try:
        from blocks.session.plan import load_meic_session_today
        from common.probe_coordinator import get_coordinator, meic_tranches_from_session_plan

        plan = load_meic_session_today(ROOT)
        coord = get_coordinator()
        if plan is not None and coord is not None:
            eligible = meic_tranches_from_session_plan(plan)
            if eligible:
                coord.set_tranches(eligible)
    except Exception:
        log.exception('Could not refresh probe coordinator tranche list from session CSV')

    entry_runner = EntryMonitorRunner(root=ROOT, logger=log)

    if tranche_now:
        log.info('Tranche-now: firing entry workers for lot via app_main ...')
        code = run_tranche(wait=True, lot=lot or 'test-offhours')
        log.info('Tranche-now finished (exit %s).', code)
        streamer.terminate()
        streamer.wait()
        market_data.terminate()
        market_data.wait()
        if stop_mon:
            stop_mon.terminate()
            stop_mon.wait()
        stop_coordinator()
        _write_status('stopped', 'Integration tranche complete.')
        log.info('Session complete (--once / --integration-tranche).')
        return

    # Token refresh is handled by the global thread started in __main__
    last_tick_mono = time.monotonic()
    stall_sec = float(os.environ.get('LAUNCHER_STALL_WARN_SEC', '30'))

    try:
        while True:
            now = _central_now()
            loop_mono = time.monotonic()
            gap = loop_mono - last_tick_mono
            if gap > stall_sec:
                log.critical(
                    'LAUNCHER_MAIN_LOOP_STALL gap_sec=%.1f threshold=%.1f',
                    gap,
                    stall_sec,
                )
            last_tick_mono = loop_mono

            # MEIC SPX 0DTE daytime session — not a global platform shutdown rule.
            if runtime_should_stop_for_session(
                session_started,
                now,
                profile=MEIC_SPX_0DTE,
            ):
                log.info("MEIC SPX 0DTE session end — shutting down trading runtime.")
                break

            # --- Health checks: restart crashed subprocesses ---
            if streamer.poll() is not None:
                log.critical(
                    'STREAMER exited unexpectedly (code %s) — restarting ...',
                    streamer.returncode,
                )
                streamer = start_streamer()

            if stop_mon is not None and stop_mon.poll() is not None:
                log.critical(
                    'STOP_MONITOR exited unexpectedly (code %s) — restarting ...',
                    stop_mon.returncode,
                )
                stop_mon = start_stop_monitor(paper=paper)

            if market_data.poll() is not None:
                log.critical(
                    'MARKET_DATA exited unexpectedly (code %s) — restarting ...',
                    market_data.returncode,
                )
                market_data = start_market_data_recorder()

            _check_running_services_health(stop_mon_running=stop_mon is not None)

            # Entry monitor — one worker per session CSV row (replaces Orchestrator)
            entry_runner.tick(now)
            # Fill sync is owned solely by stop_monitor (see BROKER_REST_RESILIENCE spec §9)

            if once and entry_runner.any_fired():
                log.info('--once: entry worker fired, shutting down.')
                break

            time.sleep(5)

    finally:
        stop_coordinator()
        log.info("Stopping streamer ...")
        streamer.terminate()
        streamer.wait()
        log.info("Stopping market_data recorder ...")
        market_data.terminate()
        market_data.wait()
        if stop_mon:
            log.info("Stopping stop_monitor ...")
            stop_mon.terminate()
            stop_mon.wait()
        _run_eod_cleanup_if_due(log)
        _write_status('stopped', 'Bot finished for the day.')
        log.info("All done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='MEIC Autotrader Launcher')
    parser.add_argument('--paper', action='store_true', help='Enable paper trading mode')
    parser.add_argument(
        '--integration-session',
        action='store_true',
        help='5-min full-path test: streamer + stop_monitor + tranche + MQTT report',
    )
    parser.add_argument('--expiry', default='2026-06-22', help='Expiry for integration session (YYYY-MM-DD)')
    parser.add_argument('--duration', type=int, default=300, help='Seconds to collect MQTT after tranche')
    parser.add_argument(
        '--integration-tranche',
        action='store_true',
        help='Off-hours test: dashboard + streamer + one tranche via app_main (no stop_monitor)',
    )
    parser.add_argument('--force', action='store_true', help='Skip trading-day and schedule waits')
    parser.add_argument('--tranche-now', action='store_true', help='Fire one tranche immediately after streamer starts')
    parser.add_argument('--once', action='store_true', help='Exit after tranche-now or first scheduled tranche')
    parser.add_argument('--no-stop-monitor', action='store_true', help='Do not start stop_monitor subprocess')
    parser.add_argument('--one-day', action='store_true', help='Run one trading day then exit (Task Scheduler mode)')
    parser.add_argument('--lot', default=None, help='Tranche lot label (sets MEIC_LOT for app_main)')
    args = parser.parse_args()
    one_day = args.one_day or os.environ.get('RUN_ONE_DAY_DEFAULT', 'false').lower() in ('1', 'true', 'yes')
    if args.paper:
        os.environ['PAPER_MODE'] = 'true'
        # Reload tt_config paper flag
        tt_config.PAPER_MODE = True

    integration = args.integration_tranche
    if integration:
        args.force = True
        args.tranche_now = True
        args.once = True
        args.no_stop_monitor = True
        if not args.lot:
            args.lot = 'test-offhours'

    import atexit
    from common.process_lock import acquire_lock, release_lock

    if not acquire_lock('launcher', paper=tt_config.PAPER_MODE, command='run.py'):
        raise SystemExit('Launcher already running — exit to avoid duplicate broker traffic')
    atexit.register(release_lock, 'launcher')

    # Start dashboard server once — stays running all week
    dash = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'dashboard', 'server.py')],
        cwd=ROOT,
        **_SUBPROCESS_QUIET,
    )
    log.info("Dashboard started (PID %s) at http://localhost:5002", dash.pid)
    time.sleep(0.75)
    if dash.poll() is not None:
        log.critical(
            'DASHBOARD failed to stay running (exit code %s) — '
            'often a stale runtime/locks/dashboard.lock; remove it if no dashboard is listening on :5002',
            dash.returncode,
        )

    # Refresh Schwab token only when using Schwab broker
    if tt_config.BROKER == 'schwab':
        try:
            _refresh_token.refresh(log)
            log.info('Token refreshed on startup.')
        except Exception as _e:
            log.warning(f'Startup token refresh failed (will retry in 25 min): {_e}')

        _t_refresh_global = threading.Thread(target=_token_refresh_loop, daemon=True)
        _t_refresh_global.start()
        log.info('Token auto-refresh thread started (24/7, every 25 min).')

    try:
        if getattr(args, 'integration_session', False):
            os.environ['MEIC_INTEGRATION'] = '1'
            integration_session(
                paper=args.paper,
                expiry=args.expiry,
                duration=args.duration,
                lot=args.lot or 'integration-session',
            )
        elif integration:
            main(
                paper=args.paper,
                force=True,
                tranche_now=True,
                once=True,
                no_stop_monitor=True,
                lot=args.lot,
            )
        elif one_day:
            try:
                run_session_cleanup('morning', log)
            except Exception:
                log.exception('Morning session cleanup failed')
            main(
                paper=args.paper,
                force=args.force,
                tranche_now=args.tranche_now,
                once=args.once,
                no_stop_monitor=args.no_stop_monitor,
                lot=args.lot,
            )
        else:
            while True:
                now = _central_now()
                # Skip weekends — sleep until Monday 8:20 AM
                if now.weekday() >= 5 and not args.force:  # 5=Sat, 6=Sun
                    days_until_monday = 7 - now.weekday()
                    next_monday = (now + timedelta(days=days_until_monday)).replace(
                        hour=8, minute=20, second=0, microsecond=0)
                    secs = (_central_to_utc(next_monday) - dt.now(timezone.utc).replace(tzinfo=None)).total_seconds()
                    log.info(f"Weekend — sleeping until Monday {next_monday.strftime('%Y-%m-%d %H:%M')} ({secs/3600:.1f}h)")
                    _write_status('stopped', 'Weekend — bot resumes Monday.')
                    wait_until_central(next_monday)
                    continue

                # Run today's trading session
                try:
                    run_session_cleanup('morning', log)
                except Exception:
                    log.exception('Morning session cleanup failed')
                if dash.poll() is not None:
                    log.critical(
                        'DASHBOARD exited unexpectedly (code %s)',
                        dash.returncode,
                    )
                main(
                    paper=args.paper,
                    force=args.force,
                    tranche_now=args.tranche_now,
                    once=args.once,
                    no_stop_monitor=args.no_stop_monitor,
                    lot=args.lot,
                )

                if args.once or args.tranche_now:
                    break

                # After 3PM, sleep until 8:20 AM next weekday
                now = _central_now()
                tomorrow = now + timedelta(days=1)
                # Skip to Monday if tomorrow is weekend
                while tomorrow.weekday() >= 5:
                    tomorrow = tomorrow + timedelta(days=1)
                next_start = tomorrow.replace(hour=8, minute=20, second=0, microsecond=0)
                secs = (_central_to_utc(next_start) - dt.now(timezone.utc).replace(tzinfo=None)).total_seconds()
                log.info(f"Sleeping until next trading day {next_start.strftime('%Y-%m-%d %H:%M')} ({secs/3600:.1f}h)")
                _write_status('stopped', f"Done for today. Resuming {next_start.strftime('%a %b %d at %H:%M')}.")
                wait_until_central(next_start)

    except KeyboardInterrupt:
        log.info("Launcher interrupted by user.")
    finally:
        dash.terminate()
        dash.wait()
        log.info("Dashboard stopped.")
