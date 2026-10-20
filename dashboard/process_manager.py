"""Process & Strategy Management for MEIC Autotrader Dashboard.

Provides runtime process inspection, restart/stop controls, and strategy re-arm capabilities.
"""
from __future__ import annotations

import datetime
import glob
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import psutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCKS_DIR = os.path.join(ROOT, "runtime", "locks")

log = logging.getLogger("dashboard.process_manager")

# Mapping of services to their primary scripts/commands
SERVICES = {
    "launcher": {
        "label": "Master Bot Launcher",
        "lock": "launcher",
        "command_hint": "run.py",
        "launch_cmd": [sys.executable, os.path.join(ROOT, "run.py"), "--one-day"],
        "default_log": "logs/launcher_*.log",
        "managed_by": "Task Scheduler / Dashboard",
    },
    "streamer": {
        "label": "Market Data Streamer",
        "lock": "streamer",
        "command_hint": "publish_tastytrade.py",
        "launch_cmd": [sys.executable, os.path.join(ROOT, "streaming", "publish_tastytrade.py")],
        "default_log": "logs/stream_pub_tt_*.log",
        "managed_by": "Launcher (Auto-restarts)",
    },
    "stop_monitor": {
        "label": "Stop Monitor Supervisor",
        "lock": "stop_monitor",
        "command_hint": "blocks/stop/run.py",
        "launch_cmd": [sys.executable, os.path.join(ROOT, "blocks", "stop", "run.py"), "--paper"],
        "default_log": "meic0dte/logs/stop_monitor.log",
        "managed_by": "Launcher (Auto-restarts)",
    },
    "market_data": {
        "label": "Market Data OHLC Recorder",
        "lock": "market_data",
        "command_hint": "market_data.run",
        "launch_cmd": [sys.executable, "-m", "market_data.run"],
        "default_log": "logs/market_data_*.log",
        "managed_by": "Launcher (Auto-restarts)",
    },
    "clone_relay": {
        "label": "Discretionary Webhook Relay",
        "lock": "clone_relay",
        "command_hint": "relay.py",
        "launch_cmd": [sys.executable, os.path.join(ROOT, "clone_trades", "relay.py"), "--ngrok"],
        "default_log": "logs/clone_relay_*.log",
        "managed_by": "Standalone Daemon (Port 5005)",
    },
    "dashboard": {
        "label": "Dashboard Web Server",
        "lock": "dashboard",
        "command_hint": "dashboard/server.py",
        "launch_cmd": [sys.executable, os.path.join(ROOT, "dashboard", "server.py")],
        "default_log": "logs/dashboard_*.log",
        "managed_by": "Standalone Daemon (Port 5002)",
    },
}

# Strategy file mappings for modification inspection
STRATEGY_FILES: Dict[str, List[str]] = {
    "MEIC_IC": [
        os.path.join(ROOT, "strategies", "meic", "strategy.py"),
        os.path.join(ROOT, "meic0dte", "open", "open_spread_tt.py"),
        os.path.join(ROOT, "meic0dte", "app_main.py"),
    ],
    "MANUAL_SPREAD": [
        os.path.join(ROOT, "blocks", "entry", "manual_worker.py"),
        os.path.join(ROOT, "manual_spread", "entry.py"),
        os.path.join(ROOT, "dashboard", "manual_spread_handlers.py"),
    ],
    "CLONE_TRADES": [
        os.path.join(ROOT, "clone_trades", "relay.py"),
        os.path.join(ROOT, "clone_trades", "handlers.py"),
        os.path.join(ROOT, "clone_trades", "ladder.py"),
    ],
    "SKYLINE_SPX": [
        os.path.join(ROOT, "strategies", "skyline", "strategy.py"),
    ],
    "PERMA_BULL_PCS": [
        os.path.join(ROOT, "strategies", "perma_bull_pcs", "strategy.py"),
    ],
    "MEAN_REVERSION_SPX": [
        os.path.join(ROOT, "strategies", "mean_reversion", "strategy.py"),
    ],
    "IRON_FLY_SPX": [
        os.path.join(ROOT, "strategies", "iron_fly", "strategy.py"),
    ],
    "IRON_FLY_XSP": [
        os.path.join(ROOT, "strategies", "iron_fly_xsp", "strategy.py"),
    ],
    "DRMA_HM_SPX": [
        os.path.join(ROOT, "strategies", "drma_hm", "strategy.py"),
        os.path.join(ROOT, "strategies", "drma_hm", "indicators.py"),
        os.path.join(ROOT, "strategies", "drma_hm", "market_data_feed.py"),
        os.path.join(ROOT, "strategies", "drma_hm", "legs.py"),
    ],
}


def _find_proc_by_hint(hint: str) -> Optional[psutil.Process]:
    """Find a running process whose cmdline contains hint."""
    hint_low = hint.lower().replace("/", "\\")
    hint_alt = hint.lower().replace("\\", "/")
    for p in psutil.process_iter(["pid", "name"]):
        try:
            cmd = " ".join(p.cmdline()).lower()
            if hint_low in cmd or hint_alt in cmd:
                return p
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return None


def _get_log_tail_path(pattern_or_path: str) -> Optional[str]:
    """Resolve a log file pattern to the latest existing file."""
    if "*" in pattern_or_path:
        full_pattern = os.path.join(ROOT, pattern_or_path)
        matches = sorted(glob.glob(full_pattern))
        return matches[-1] if matches else None
    direct = os.path.join(ROOT, pattern_or_path)
    return direct if os.path.isfile(direct) else None


def get_service_status(svc_key: str) -> Dict[str, Any]:
    """Return live status dict for a registered service."""
    meta = SERVICES.get(svc_key, {})
    lock_name = meta.get("lock", svc_key)
    lock_file = os.path.join(LOCKS_DIR, f"{lock_name}.lock")

    pid = 0
    alive = False
    started_at = None
    memory_mb = 0.0
    cpu_pct = 0.0

    # 1. Try reading process lock
    if os.path.isfile(lock_file):
        try:
            with open(lock_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                pid = int(data.get("pid") or 0)
                if data.get("ts"):
                    started_at = datetime.datetime.fromtimestamp(data["ts"]).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    # 2. Check if PID is alive via psutil
    proc: Optional[psutil.Process] = None
    if pid > 0:
        try:
            p = psutil.Process(pid)
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                proc = p
                alive = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            alive = False

    # 3. Fallback scan by hint if lock missing or dead PID
    if not alive and meta.get("command_hint"):
        fallback = _find_proc_by_hint(meta["command_hint"])
        if fallback:
            proc = fallback
            pid = proc.pid
            alive = True

    if proc and alive:
        try:
            memory_mb = round(proc.memory_info().rss / (1024 * 1024), 1)
            cpu_pct = round(proc.cpu_percent(interval=None), 1)
            create_ts = proc.create_time()
            started_at = datetime.datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    log_path = _get_log_tail_path(meta.get("default_log", ""))

    return {
        "service": svc_key,
        "label": meta.get("label", svc_key),
        "pid": pid if alive else None,
        "alive": alive,
        "managed_by": meta.get("managed_by", "Process"),
        "command_hint": meta.get("command_hint", ""),
        "started_at": started_at,
        "memory_mb": memory_mb if alive else 0.0,
        "cpu_pct": cpu_pct if alive else 0.0,
        "log_path": log_path,
        "has_log": bool(log_path and os.path.exists(log_path)),
    }


def get_all_services_status() -> Dict[str, Dict[str, Any]]:
    """Return dictionary of status for all registered services."""
    res = {}
    for k in SERVICES:
        res[k] = get_service_status(k)
    return res


def get_strategies_inspect_status() -> Dict[str, Any]:
    """Inspect all 9 strategies: session state, overrides, and code modifications."""
    from common.daily_overrides import load_session_state
    from common.broker_factory import is_strategy_paper

    today = datetime.date.today()
    results = {}

    for strat_name, files in STRATEGY_FILES.items():
        state = load_session_state(strat_name, ROOT)
        overrides = state.get("overrides") or {}
        entered = bool(state.get("entered", False))
        skip_reason = state.get("skip_reason")
        enabled = overrides.get("enabled", True)
        paper = is_strategy_paper(strat_name)

        # Check file modifications
        latest_mtime = 0.0
        latest_file = ""
        modified_today = False

        for fpath in files:
            if os.path.exists(fpath):
                mtime = os.path.getmtime(fpath)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = os.path.basename(fpath)
                mdate = datetime.datetime.fromtimestamp(mtime).date()
                if mdate == today:
                    modified_today = True

        mtime_str = (
            datetime.datetime.fromtimestamp(latest_mtime).strftime("%I:%M %p CST")
            if latest_mtime > 0
            else "Unknown"
        )

        results[strat_name] = {
            "name": strat_name,
            "enabled": enabled,
            "paper": paper,
            "entered": entered,
            "skip_reason": skip_reason,
            "latest_file": latest_file,
            "last_modified": mtime_str,
            "modified_today": modified_today,
            "status_label": "Entered" if entered else ("Paused" if not enabled else ("Skipped" if skip_reason else "Active / Ready")),
        }

    return results


def stop_service(service_name: str) -> Dict[str, Any]:
    """Gracefully terminate a service."""
    status = get_service_status(service_name)
    pid = status.get("pid")
    if not pid or not status.get("alive"):
        return {"status": "ok", "message": f"{service_name} is already stopped"}

    try:
        proc = psutil.Process(pid)
        # Terminate children first
        for child in proc.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
        return {"status": "ok", "message": f"Successfully stopped {service_name} (PID {pid})"}
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        return {"status": "error", "error": str(exc)}


def restart_service(service_name: str) -> Dict[str, Any]:
    """Restart a background service."""
    from common.process_lock import release_lock

    if service_name == "all":
        # Restart launcher and clone relay
        r_launcher = restart_service("launcher")
        r_relay = restart_service("clone_relay")
        return {
            "status": "ok",
            "message": "Restarted launcher and relay processes",
            "launcher": r_launcher,
            "clone_relay": r_relay,
        }

    meta = SERVICES.get(service_name)
    if not meta:
        return {"status": "error", "error": f"Unknown service: {service_name}"}

    # 1. Terminate existing process if alive
    status = get_service_status(service_name)
    old_pid = status.get("pid")
    if old_pid and status.get("alive"):
        try:
            proc = psutil.Process(old_pid)
            for child in proc.children(recursive=True):
                try:
                    child.terminate()
                except Exception:
                    pass
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            log.info("Terminated existing %s (PID %s)", service_name, old_pid)
        except Exception as exc:
            log.warning("Could not terminate old %s PID %s: %s", service_name, old_pid, exc)

    # 2. Release lock if held
    lock_name = meta.get("lock", service_name)
    release_lock(lock_name)
    time.sleep(0.5)

    # 3. If service is managed by launcher and launcher is alive, launcher will auto-respawn it!
    # Exception: launcher itself or clone_relay or dashboard must be spawned directly.
    launcher_status = get_service_status("launcher")
    if service_name in ("streamer", "stop_monitor", "market_data") and launcher_status.get("alive"):
        log.info(
            "Service %s killed; Launcher (PID %s) auto-respawn health check will restart it within 5s",
            service_name,
            launcher_status.get("pid"),
        )
        return {
            "status": "ok",
            "message": f"{service_name} stopped. Launcher will cleanly restart it with fresh code within 5 seconds.",
            "mode": "launcher_managed",
        }

    # 4. Direct spawn (detached on Windows)
    cmd = meta.get("launch_cmd")
    if not cmd:
        return {"status": "error", "error": f"No launch command defined for {service_name}"}

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)

    try:
        new_proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            creationflags=creationflags,
        )
        log.info("Spawned new %s with PID %s", service_name, new_proc.pid)
        return {
            "status": "ok",
            "message": f"Successfully started {service_name} (PID {new_proc.pid})",
            "new_pid": new_proc.pid,
        }
    except Exception as exc:
        log.exception("Failed to spawn %s", service_name)
        return {"status": "error", "error": str(exc)}


def rearm_strategy_state(strategy_name: str) -> Dict[str, Any]:
    """Reset today's session state for a strategy so it evaluates market entry again."""
    from common.daily_overrides import load_session_state, save_session_state

    state = load_session_state(strategy_name, ROOT)
    state["entered"] = False
    state["skip_reason"] = None
    save_session_state(strategy_name, ROOT, state)

    log.info("Strategy %s re-armed for today (entered=False, skip_reason=None)", strategy_name)
    return {
        "status": "ok",
        "strategy": strategy_name,
        "message": f"{strategy_name} session state re-armed for today. Ready for new evaluation.",
    }


def rearm_all_strategies() -> Dict[str, Any]:
    """Re-arm session state for all strategies."""
    results = {}
    for strat in STRATEGY_FILES:
        results[strat] = rearm_strategy_state(strat)
    return {"status": "ok", "message": "All strategies re-armed for today", "results": results}
