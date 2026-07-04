"""TastyTrade AlertStreamer — real-time fill notifications for stop_monitor."""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class AlertListener:
    """
    Background thread running AlertStreamer (or PaperAlertStreamer).
    Routes fill events to per-order-id queues registered by StopMonitor threads.
    """

    def __init__(self, session: Any, account: Any, paper: bool = False):
        self.session = session
        self.account = account
        self.paper = paper
        self._queues: Dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register(self, order_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._queues[str(order_id)] = q
        return q

    def unregister(self, order_id: str) -> None:
        with self._lock:
            self._queues.pop(str(order_id), None)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        log.info('AlertListener started (paper=%s)', self.paper)

    def stop(self) -> None:
        self._stop.set()

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                asyncio.run(self._listen())
            except Exception as exc:
                log.error('AlertStreamer error: %s — retrying in 10s', exc)
                self._stop.wait(10)

    async def _listen(self) -> None:
        if self.paper:
            from tastytrade.paper import PaperAlertStreamer
            streamer_cls = PaperAlertStreamer
        else:
            from tastytrade import AlertStreamer
            streamer_cls = AlertStreamer

        from tastytrade.order import PlacedOrder

        async with streamer_cls(self.session) as streamer:
            await streamer.subscribe_accounts([self.account])
            async for msg in streamer.listen(PlacedOrder):
                if self._stop.is_set():
                    break
                order_id = str(getattr(msg, 'id', '') or getattr(msg, 'order_id', ''))
                status = str(getattr(msg, 'status', '')).lower()
                event = {'order_id': order_id, 'status': status, 'raw': msg}
                with self._lock:
                    q = self._queues.get(order_id)
                if q:
                    q.put(event)
                log.debug('Alert: order %s -> %s', order_id, status)
