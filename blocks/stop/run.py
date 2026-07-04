"""CLI entry point for stop_monitor supervisor."""
import argparse
import logging
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import tt_config
from common.broker_factory import get_broker
from common.tt_auth import create_tastytrade_session, get_account
from blocks.stop.alerts import AlertListener
from blocks.stop.runner import MonitorRunner

log = logging.getLogger(__name__)

VALID_ENGINES = frozenset({'v2', 'v3'})


def stop_monitor_engine() -> str:
    """v2 = MonitorRunner (default); v3 = StopSupervisor (stub until V3-2a)."""
    engine = os.environ.get('STOP_MONITOR_ENGINE', 'v2').strip().lower()
    if engine not in VALID_ENGINES:
        raise SystemExit(
            f'Unknown STOP_MONITOR_ENGINE={engine!r} — use v2 or v3',
        )
    return engine


def _run_v2(*, broker, poll_interval: float, alert_listener) -> None:
    runner = MonitorRunner(
        broker=broker,
        poll_interval=poll_interval,
        alert_listener=alert_listener,
    )
    runner.run_forever()


def _run_v3(*, broker, poll_interval: float, alert_listener) -> None:
    raise SystemExit(
        'STOP_MONITOR_ENGINE=v3 is not implemented yet (V3-2a StopSupervisor). '
        'Unset STOP_MONITOR_ENGINE or set STOP_MONITOR_ENGINE=v2. '
        'See changes/STOP_MONITOR_V3_DESIGN.md §11.',
    )


def main():
    parser = argparse.ArgumentParser(description='MEIC Stop Monitor')
    parser.add_argument('--poll', type=float, default=5.0, help='Poll interval seconds')
    parser.add_argument('--paper', action='store_true', help='Use paper trading session')
    args = parser.parse_args()

    log_dir = os.path.join(ROOT, 'meic0dte', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, 'stop_monitor.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [STOP-MON] %(levelname)s %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_path, mode='a', encoding='utf-8'),
        ],
    )
    log.info('Stop monitor logging to %s', log_path)

    engine = stop_monitor_engine()
    log.info('Stop monitor engine: %s (STOP_MONITOR_ENGINE)', engine)

    paper = args.paper or tt_config.PAPER_MODE
    broker = get_broker(paper=paper)

    alert_listener = None
    if tt_config.BROKER == 'tastytrade':
        session = create_tastytrade_session(paper=paper)
        account = get_account(session)
        alert_listener = AlertListener(session, account, paper=paper)

    run_kwargs = {
        'broker': broker,
        'poll_interval': args.poll,
        'alert_listener': alert_listener,
    }
    if engine == 'v3':
        _run_v3(**run_kwargs)
    else:
        _run_v2(**run_kwargs)


if __name__ == '__main__':
    main()
