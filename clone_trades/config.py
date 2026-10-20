"""Configuration manager for Discretionary Clone Trades."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from typing import Any, Dict, List, Optional

from clone_trades.models import LadderTier

log = logging.getLogger("clone_trades.config")

DEFAULT_LADDER_TIERS = [
    LadderTier(tier_id=1, target_profit_pct=25.0, stop_loss_pct=-75.0, exit_pct=33.3, description="Tier 1: +25% PT (33% exit) / -75% SL (100%)"),
    LadderTier(tier_id=2, target_profit_pct=50.0, stop_loss_pct=0.0, exit_pct=33.3, description="Tier 2: +50% PT (33% exit) / 0% Breakeven SL (100%)"),
    LadderTier(tier_id=3, target_profit_pct=100.0, stop_loss_pct=50.0, exit_pct=33.4, description="Tier 3: +100% PT (34% exit) / +50% Profit Lock SL (100%)"),
]


class CloneTradeConfigManager:
    """Thread-safe configuration manager with JSON file persistence."""

    def __init__(self, root_dir: Optional[str] = None):
        if not root_dir:
            root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.root_dir = root_dir
        self.data_dir = os.path.join(root_dir, "trades", "clone_trades")
        self.config_path = os.path.join(self.data_dir, "config.json")
        self.secret_path = os.path.join(self.data_dir, "webhook_secret.txt")
        self._lock = threading.RLock()

        # Config fields
        self.auto_take_trades: bool = False
        self.max_premium_pct: float = 25.0
        self.floor_guard_pct: float = 75.0
        self.allocation_amount: float = 500.0
        self.initial_stop_loss_pct: float = -75.0
        self.ladder_tiers: List[LadderTier] = list(DEFAULT_LADDER_TIERS)
        self.paper_mode: bool = False
        self.allow_off_hours_orders: bool = False
        self.allowed_ips: List[str] = []
        self.webhook_secret: str = ""

        self._ensure_dirs()
        self._load_secret()
        self._load_config()

    def _ensure_dirs(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)

    def _load_secret(self) -> None:
        env_secret = os.environ.get("CLONE_WEBHOOK_SECRET", "").strip()
        if env_secret:
            self.webhook_secret = env_secret
            return

        if os.path.exists(self.secret_path):
            try:
                with open(self.secret_path, "r", encoding="utf-8") as f:
                    sec = f.read().strip()
                if sec:
                    self.webhook_secret = sec
                    return
            except Exception as exc:
                log.warning("Failed to read webhook secret file: %s", exc)

        # Generate fresh secure secret
        self.webhook_secret = secrets.token_hex(16)
        try:
            with open(self.secret_path, "w", encoding="utf-8") as f:
                f.write(self.webhook_secret)
        except Exception as exc:
            log.warning("Failed to save webhook secret: %s", exc)

    def _load_config(self) -> None:
        if not os.path.exists(self.config_path):
            self._save_config_unlocked()
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.auto_take_trades = bool(data.get("auto_take_trades", False))
            self.max_premium_pct = float(data.get("max_premium_pct", 25.0))
            self.floor_guard_pct = float(data.get("floor_guard_pct", 75.0))
            self.allocation_amount = float(data.get("allocation_amount", 500.0))
            self.initial_stop_loss_pct = float(data.get("initial_stop_loss_pct", -50.0))
            self.paper_mode = bool(data.get("paper_mode", False))
            self.allow_off_hours_orders = bool(data.get("allow_off_hours_orders", False))
            raw_ips = data.get("allowed_ips", [])
            self.allowed_ips = [str(x).strip() for x in raw_ips if str(x).strip()] if isinstance(raw_ips, list) else []

            raw_tiers = data.get("ladder_tiers", [])
            if raw_tiers and isinstance(raw_tiers, list):
                loaded_tiers = [LadderTier.from_dict(t) for t in raw_tiers]
                # If any loaded tiers have exit_pct <= 0, backfill sensible defaults
                num_t = len(loaded_tiers)
                if num_t > 0 and all(t.exit_pct <= 0 for t in loaded_tiers):
                    for i, t in enumerate(loaded_tiers):
                        t.exit_pct = round(100.0 / num_t, 1) if i < num_t - 1 else round(100.0 - (round(100.0 / num_t, 1) * (num_t - 1)), 1)
                self.ladder_tiers = loaded_tiers
            else:
                self.ladder_tiers = list(DEFAULT_LADDER_TIERS)

            # Synchronize initial stop loss with ladder tier 1 so they never drift
            if self.ladder_tiers:
                self.initial_stop_loss_pct = self.ladder_tiers[0].stop_loss_pct
        except Exception as exc:
            log.error("Failed to load clone trades config, using defaults: %s", exc)
            self._save_config_unlocked()

    def _save_config_unlocked(self) -> None:
        data = {
            "auto_take_trades": self.auto_take_trades,
            "max_premium_pct": self.max_premium_pct,
            "floor_guard_pct": self.floor_guard_pct,
            "allocation_amount": self.allocation_amount,
            "initial_stop_loss_pct": self.initial_stop_loss_pct,
            "paper_mode": self.paper_mode,
            "allow_off_hours_orders": self.allow_off_hours_orders,
            "allowed_ips": list(self.allowed_ips),
            "ladder_tiers": [t.to_dict() for t in self.ladder_tiers],
        }
        try:
            temp_path = self.config_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(temp_path, self.config_path)
        except Exception as exc:
            log.error("Failed to save clone trades config: %s", exc)

    def update(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            if "auto_take_trades" in updates:
                self.auto_take_trades = bool(updates["auto_take_trades"])
            if "max_premium_pct" in updates:
                self.max_premium_pct = max(0.0, float(updates["max_premium_pct"]))
            if "floor_guard_pct" in updates:
                self.floor_guard_pct = max(0.0, float(updates["floor_guard_pct"]))
            if "allocation_amount" in updates:
                self.allocation_amount = max(10.0, float(updates["allocation_amount"]))
            if "initial_stop_loss_pct" in updates:
                self.initial_stop_loss_pct = float(updates["initial_stop_loss_pct"])
            if "paper_mode" in updates:
                self.paper_mode = bool(updates["paper_mode"])
            if "allow_off_hours_orders" in updates:
                self.allow_off_hours_orders = bool(updates["allow_off_hours_orders"])
            if "allowed_ips" in updates and isinstance(updates["allowed_ips"], list):
                self.allowed_ips = [str(x).strip() for x in updates["allowed_ips"] if str(x).strip()]
            if "ladder_tiers" in updates and isinstance(updates["ladder_tiers"], list):
                tiers = []
                num_updates = len(updates["ladder_tiers"])
                for idx, t in enumerate(updates["ladder_tiers"]):
                    raw_exit = t.get("exit_pct", t.get("contracts_pct"))
                    if raw_exit is not None and float(raw_exit) > 0:
                        exit_val = float(raw_exit)
                    else:
                        exit_val = round(100.0 / num_updates, 1) if idx < num_updates - 1 else round(100.0 - (round(100.0 / num_updates, 1) * (num_updates - 1)), 1)
                    tiers.append(
                        LadderTier(
                            tier_id=idx + 1,
                            target_profit_pct=float(t.get("target_profit_pct", 25.0)),
                            stop_loss_pct=float(t.get("stop_loss_pct", -50.0)),
                            exit_pct=exit_val,
                            description=str(t.get("description", f"Tier {idx + 1}")),
                        )
                    )
                # Ensure tiers are sorted by target profit ascending
                tiers.sort(key=lambda x: x.target_profit_pct)
                self.ladder_tiers = tiers
                # Keep initial stop loss aligned with Tier 1
                if self.ladder_tiers:
                    self.initial_stop_loss_pct = self.ladder_tiers[0].stop_loss_pct
            elif "initial_stop_loss_pct" in updates and self.ladder_tiers:
                self.ladder_tiers[0].stop_loss_pct = self.initial_stop_loss_pct

            self._save_config_unlocked()
            return self.to_dict()

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "auto_take_trades": self.auto_take_trades,
                "max_premium_pct": self.max_premium_pct,
                "floor_guard_pct": self.floor_guard_pct,
                "allocation_amount": self.allocation_amount,
                "initial_stop_loss_pct": self.initial_stop_loss_pct,
                "paper_mode": self.paper_mode,
                "allow_off_hours_orders": self.allow_off_hours_orders,
                "allowed_ips": list(self.allowed_ips),
                "ladder_tiers": [t.to_dict() for t in self.ladder_tiers],
                "webhook_secret": self.webhook_secret,
            }


_GLOBAL_CONFIG: Optional[CloneTradeConfigManager] = None
_CONFIG_LOCK = threading.Lock()


def get_config(root_dir: Optional[str] = None) -> CloneTradeConfigManager:
    global _GLOBAL_CONFIG
    with _CONFIG_LOCK:
        if _GLOBAL_CONFIG is None:
            _GLOBAL_CONFIG = CloneTradeConfigManager(root_dir)
        return _GLOBAL_CONFIG
