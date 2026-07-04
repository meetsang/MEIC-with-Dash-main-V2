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
    logging.getLogger(__name__).info('Stop monitor logging to %s', log_path)

    paper = args.paper or tt_config.PAPER_MODE
    broker = get_broker(paper=paper)

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
    runner.run_forever()


if __name__ == '__main__':
    main()
