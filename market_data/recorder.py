"""Main loop — poll MQTT, aggregate OHLC, write data/YYYY-MM-DD/."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.logging_config import setup_file_only_logging
from common.session_logs import MARKET_DATA_BASE, new_session_log_path
from market_data import config
from market_data.aggregator import SymbolState
from market_data.mqtt_reader import MqttQuoteReader
from market_data.option_snapshots import OptionQuoteSnapshotWriter
from meic0dte.app.utilities import central_now, crossed_market_close

log = logging.getLogger(__name__)


class MarketDataRecorder:
    def __init__(self):
        self._reader = MqttQuoteReader()
        self._option_snapshots = OptionQuoteSnapshotWriter(self._reader.cache)
        self._states: dict[str, SymbolState] = {}
        self._stop = False
        self._session_started = central_now()

    def _day_path(self) -> str:
        today = central_now().date()
        path = config.day_dir(today)
        os.makedirs(path, exist_ok=True)
        return path

    def _state_for(self, symbol: str) -> SymbolState:
        day_path = self._day_path()
        st = self._states.get(symbol)
        if st is None:
            st = SymbolState(symbol=symbol, day_path=day_path)
            self._states[symbol] = st
        else:
            st.ensure_day(day_path)
        return st

    def run(self) -> None:
        log_path = new_session_log_path(ROOT, MARKET_DATA_BASE, when=central_now())
        setup_file_only_logging(
            'market_data.recorder',
            log_path,
            stream_prefix='MKT-DATA',
            file_mode='w',
        )
        log.info('Market data log: %s', log_path)
        log.info(
            'Market data recorder starting — symbols=%s poll=%ss option_snapshot=%ss intervals=%s',
            config.WATCH_SYMBOLS,
            config.POLL_INTERVAL_SEC,
            config.OPTION_SNAPSHOT_INTERVAL_SEC,
            config.BAR_INTERVALS_MIN,
        )
        self._reader.start()
        if not self._reader.wait_for_any(timeout=180):
            log.warning('No MQTT prices yet — continuing (is streamer running?)')

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        last_poll = 0.0
        while not self._stop:
            now = central_now()
            if crossed_market_close(self._session_started, now, close_hour=15):
                log.info('3:00 PM CT — stopping market data recorder')
                break

            self._tick_minute_boundaries(now)

            if time.time() - last_poll >= config.POLL_INTERVAL_SEC:
                ts = central_now()
                prices = self._reader.poll_latest()
                for sym, price in prices.items():
                    self._state_for(sym).record_poll(ts, price)
                if prices:
                    log.info('Poll %s — %s', ts.strftime('%H:%M:%S'), prices)
                last_poll = time.time()

            self._option_snapshots.maybe_write(now, day_path=self._day_path())

            time.sleep(1.0)

        self._flush_all()
        self._reader.stop()
        log.info('Market data recorder stopped')

    def _tick_minute_boundaries(self, now: datetime) -> None:
        """Finalize completed minutes on wall-clock even between MQTT polls."""
        minute = now.replace(second=0, microsecond=0)
        for st in self._states.values():
            if st.minute_start and minute > st.minute_start:
                st._finalize_minute(st.minute_start)
                st.minute_prices = []
                st.minute_start = minute

    def _flush_all(self) -> None:
        for st in self._states.values():
            st.flush()

    def _handle_signal(self, signum, frame) -> None:
        log.info('Signal %s — shutting down', signum)
        self._stop = True


def main() -> None:
    MarketDataRecorder().run()


if __name__ == '__main__':
    main()
