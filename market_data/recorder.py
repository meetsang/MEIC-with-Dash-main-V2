"""Main loop — MQTT ticks → OHLC, option snapshots → data/YYYY-MM-DD/."""
from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import time
from datetime import datetime
from typing import Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.logging_config import setup_file_only_logging
from common.session_logs import MARKET_DATA_BASE, new_session_log_path
from market_data import config
from market_data.aggregator import SymbolState
from market_data.mqtt_reader import MqttQuoteReader
from market_data.option_snapshots import OptionQuoteSnapshotWriter
from market_data.watch_symbols import watch_symbol_from_mqtt_topic
from meic0dte.app.utilities import central_from_epoch, central_now, crossed_market_close

log = logging.getLogger(__name__)

TickItem = Tuple[str, float, float]


class MarketDataRecorder:
    def __init__(self):
        self._reader = MqttQuoteReader()
        self._option_snapshots = OptionQuoteSnapshotWriter(self._reader.cache)
        self._states: dict[str, SymbolState] = {}
        self._tick_queue: queue.SimpleQueue[TickItem] = queue.SimpleQueue()
        self._tick_listener = self._enqueue_tick
        self._stop = False
        self._session_started = central_now()
        self._last_tick_log_mono = 0.0

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

    def _enqueue_tick(self, topic_symbol: str, price: float, epoch: float) -> None:
        watch = watch_symbol_from_mqtt_topic(topic_symbol)
        if watch is None:
            return
        self._tick_queue.put((watch, price, epoch))

    def _drain_ticks(self) -> int:
        count = 0
        while True:
            try:
                sym, price, epoch = self._tick_queue.get_nowait()
            except queue.Empty:
                break
            ts = central_from_epoch(epoch)
            self._state_for(sym).record_tick(ts, price)
            count += 1
        return count

    def _maybe_log_tick_summary(self) -> None:
        if not self._states:
            return
        if self._last_tick_log_mono and (
            time.monotonic() - self._last_tick_log_mono
        ) < config.POLL_INTERVAL_SEC:
            return
        summary = {
            sym: st.minute_prices[-1][1]
            for sym, st in sorted(self._states.items())
            if st.minute_prices
        }
        if summary:
            log.info('Ticks %s — %s', central_now().strftime('%H:%M:%S'), summary)
        self._last_tick_log_mono = time.monotonic()

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
            'Market data recorder starting — symbols=%s ticks=on_arrival option_snapshot=%ss intervals=%s',
            config.WATCH_SYMBOLS,
            config.OPTION_SNAPSHOT_INTERVAL_SEC,
            config.BAR_INTERVALS_MIN,
        )
        self._reader.start()
        self._reader.add_tick_listener(self._tick_listener)
        if not self._reader.wait_for_any(timeout=180):
            log.warning('No MQTT prices yet — continuing (is streamer running?)')

        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            while not self._stop:
                now = central_now()
                if crossed_market_close(self._session_started, now, close_hour=15):
                    log.info('3:00 PM CT — stopping market data recorder')
                    break

                self._drain_ticks()
                self._tick_minute_boundaries(now)
                self._maybe_log_tick_summary()
                self._option_snapshots.maybe_write(now, day_path=self._day_path())

                time.sleep(0.25)
        finally:
            self._reader.remove_tick_listener(self._tick_listener)

        self._drain_ticks()
        self._flush_all()
        self._reader.stop()
        log.info('Market data recorder stopped')

    def _tick_minute_boundaries(self, now: datetime) -> None:
        """Finalize completed minutes on wall-clock even between MQTT ticks."""
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
