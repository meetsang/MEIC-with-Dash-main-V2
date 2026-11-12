"""
In-Process Service Manager for Low-Memory / Micro VM Execution
==============================================================
Runs bot subsystems (market_data, stop_monitor, DXLink streamer, clone relay)
as daemon threads within a single Python interpreter instead of separate OS
processes.

Benefits on low-memory VMs (e.g. GCP e2-micro with 1 GB RAM):
  1. Shares a single copy of `sys.modules` (`pandas`, `numpy`, `tastytrade`,
     `pydantic`, `httpx`), saving ~250–320 MB of duplicate interpreter RSS.
  2. Shares a single in-memory `SharedBroker` and OAuth session token.
  3. Provides `InProcessServiceHandle` with the identical API as `subprocess.Popen`
     (`.poll()`, `.terminate()`, `.wait()`, `.pid`, `.returncode`), allowing
     `run.py` to supervise and restart services seamlessly without code changes.

Activation:
  Set `IN_PROCESS_SERVICES=true` or `LOW_MEMORY_MODE=true` in `.env`,
  or pass `--in-process` to `run.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


def is_in_process_mode() -> bool:
    """Return True if in-process service mode is activated via env var."""
    for key in ('IN_PROCESS_SERVICES', 'LOW_MEMORY_MODE', 'MEIC_IN_PROCESS'):
        val = os.environ.get(key, '').strip().lower()
        if val in ('1', 'true', 'yes', 'on'):
            return True
    return False


class InProcessServiceHandle:
    """Drop-in replacement for subprocess.Popen wrapping an in-process thread."""

    def __init__(
        self,
        name: str,
        target: Callable[[], Any],
        stop_fn: Callable[[], None],
    ):
        self.name = name
        self._target = target
        self._stop_fn = stop_fn
        self._stop_requested = False
        self._started = False
        self.pid = os.getpid()
        self.returncode: Optional[int] = None
        self._thread = threading.Thread(
            target=self._run_wrapper,
            name=f'inproc-{name}',
            daemon=True,
        )

    def start(self) -> 'InProcessServiceHandle':
        self._started = True
        self._write_lock()
        self._thread.start()
        log.info('Started in-process service %s (thread=%s, pid=%s)', self.name, self._thread.name, self.pid)
        return self

    def _write_lock(self) -> None:
        try:
            from common.process_lock import acquire_lock
            acquire_lock(
                self.name,
                command=f"inproc-{self.name}",
                extra={"in_process": True, "thread_name": f"inproc-{self.name}", "launcher_pid": self.pid},
                force=True,
            )
        except Exception as exc:
            log.warning("Could not acquire lock for in-process service %s: %s", self.name, exc)

    def _release_lock(self) -> None:
        try:
            from common.process_lock import release_lock
            release_lock(self.name)
        except Exception as exc:
            log.warning("Could not release lock for in-process service %s: %s", self.name, exc)

    def _run_wrapper(self) -> None:
        try:
            self._target()
            self.returncode = 0
            log.info('In-process service %s exited normally', self.name)
        except Exception as exc:
            self.returncode = 1
            if not self._stop_requested:
                log.exception('In-process service %s failed: %s', self.name, exc)
            else:
                log.info('In-process service %s stopped after termination request', self.name)
        finally:
            self._release_lock()

    def poll(self) -> Optional[int]:
        if not self._started or self._thread.is_alive():
            return None
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self._stop_requested = True
        self._release_lock()
        try:
            self._stop_fn()
        except Exception as exc:
            log.warning('Error requesting stop for in-process service %s: %s', self.name, exc)

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout: Optional[float] = None) -> int:
        self._thread.join(timeout=timeout)
        return self.returncode if self.returncode is not None else 0

    def is_alive(self) -> bool:
        return self._thread.is_alive()


def start_in_process_market_data() -> InProcessServiceHandle:
    """Start MarketDataRecorder as an in-process thread."""
    from market_data.recorder import MarketDataRecorder

    recorder = MarketDataRecorder()

    def _stop():
        recorder._stop = True

    handle = InProcessServiceHandle(
        name='market_data',
        target=recorder.run,
        stop_fn=_stop,
    )
    return handle.start()


def start_in_process_stop_monitor(paper: bool = False) -> Optional[InProcessServiceHandle]:
    """Start centralized stop_monitor supervisor as an in-process thread."""
    from blocks.stop.runner import MonitorRunner
    from common import tt_config
    from common.broker_factory import get_shared_broker
    from common.tt_auth import create_tastytrade_session, get_account
    from blocks.stop.alerts import AlertListener

    use_paper = bool(paper or tt_config.PAPER_MODE)
    broker = get_shared_broker(paper=use_paper)

    alert_listener = None
    if tt_config.BROKER == 'tastytrade':
        session = getattr(broker, 'session', None)
        account = getattr(broker, 'account', None)
        if session is not None and account is not None:
            alert_listener = AlertListener(session, account, paper=use_paper)
        else:
            try:
                session = create_tastytrade_session(paper=use_paper)
                account = get_account(session)
                alert_listener = AlertListener(session, account, paper=use_paper)
            except Exception as exc:
                log.warning('Could not initialize AlertListener for in-process stop_monitor: %s', exc)

    runner = MonitorRunner(
        broker=broker,
        poll_interval=5.0,
        alert_listener=alert_listener,
    )

    def _stop():
        runner._stop.set()
        runner.shutdown()

    handle = InProcessServiceHandle(
        name='stop_monitor',
        target=runner.run_forever,
        stop_fn=_stop,
    )
    return handle.start()


def start_in_process_streamer() -> InProcessServiceHandle:
    """Start DXLinkStreamer as an in-process thread running its own asyncio loop."""
    import threading
    from streaming.publish_tastytrade import run_streamer_loop

    stop_event = threading.Event()

    def _run():
        run_streamer_loop(stop_event=stop_event)

    def _stop():
        stop_event.set()

    handle = InProcessServiceHandle(
        name='streamer',
        target=_run,
        stop_fn=_stop,
    )
    return handle.start()


def start_in_process_clone_relay(
    port: int = 5005,
    target: str = 'http://127.0.0.1:5002',
    root: Optional[str] = None,
) -> Optional[InProcessServiceHandle]:
    """Start clone trades micro-relay on a daemon thread using werkzeug make_server."""
    import werkzeug.serving
    from clone_trades.relay import create_relay_app, load_allowed_ips, load_secret

    proj_root = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    secret = load_secret(proj_root)
    allowed_ips = load_allowed_ips(proj_root)
    app = create_relay_app(target, secret, allowed_ips)

    server = werkzeug.serving.make_server('0.0.0.0', port, app, threaded=True)

    def _run():
        server.serve_forever()

    def _stop():
        server.shutdown()

    handle = InProcessServiceHandle(
        name='clone_relay',
        target=_run,
        stop_fn=_stop,
    )
    return handle.start()
