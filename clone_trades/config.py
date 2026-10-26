"""Configuration manager for Discretionary Clone Trades."""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from typing import Any, Dict, List, Optional

from dataclasses import dataclass, field
from clone_trades.models import LadderTier, classify_sub_strategy

log = logging.getLogger("clone_trades.config")

DEFAULT_LADDER_TIERS = [
    LadderTier(tier_id=1, target_profit_pct=25.0, stop_loss_pct=-75.0, exit_pct=33.3, description="Tier 1: +25% PT (33% exit) / -75% SL (100%)"),
    LadderTier(tier_id=2, target_profit_pct=50.0, stop_loss_pct=0.0, exit_pct=33.3, description="Tier 2: +50% PT (33% exit) / 0% Breakeven SL (100%)"),
    LadderTier(tier_id=3, target_profit_pct=100.0, stop_loss_pct=50.0, exit_pct=33.4, description="Tier 3: +100% PT (34% exit) / +50% Profit Lock SL (100%)"),
]

DEFAULT_PC_LADDER_TIERS = [
    LadderTier(tier_id=1, target_profit_pct=20.0, stop_loss_pct=-50.0, exit_pct=50.0, description="Step 1: +20% PT (50% exit) / -50% SL (100%)"),
    LadderTier(tier_id=2, target_profit_pct=100.0, stop_loss_pct=-25.0, exit_pct=20.0, description="Step 2: +100% PT (20% exit) / -25% SL (100%)"),
    LadderTier(tier_id=3, target_profit_pct=200.0, stop_loss_pct=50.0, exit_pct=10.0, description="Step 3: +200% PT (10% exit) / +50% Profit Lock SL (100%)"),
    LadderTier(tier_id=4, target_profit_pct=300.0, stop_loss_pct=150.0, exit_pct=10.0, description="Step 4: +300% PT (10% exit) / +150% Profit Lock SL (100%)"),
    LadderTier(tier_id=5, target_profit_pct=400.0, stop_loss_pct=250.0, exit_pct=10.0, description="Step 5: +400% PT (10% exit) / +250% Profit Lock SL (100%)"),
]

DEFAULT_GENIE_LADDER_TIERS = [
    LadderTier(tier_id=1, target_profit_pct=30.0, stop_loss_pct=-40.0, exit_pct=33.3, description="Genie Step 1: +30% PT (33% exit) / -40% SL (100%)"),
    LadderTier(tier_id=2, target_profit_pct=60.0, stop_loss_pct=0.0, exit_pct=33.3, description="Genie Step 2: +60% PT (33% exit) / 0% Breakeven SL (100%)"),
    LadderTier(tier_id=3, target_profit_pct=120.0, stop_loss_pct=50.0, exit_pct=33.4, description="Genie Step 3: +120% PT (34% exit) / +50% Profit Lock SL (100%)"),
]

DEFAULT_OTHERS_LADDER_TIERS = [
    LadderTier(tier_id=1, target_profit_pct=25.0, stop_loss_pct=-50.0, exit_pct=33.3, description="Others Step 1: +25% PT (33% exit) / -50% SL (100%)"),
    LadderTier(tier_id=2, target_profit_pct=50.0, stop_loss_pct=0.0, exit_pct=33.3, description="Others Step 2: +50% PT (33% exit) / 0% Breakeven SL (100%)"),
    LadderTier(tier_id=3, target_profit_pct=100.0, stop_loss_pct=50.0, exit_pct=33.4, description="Others Step 3: +100% PT (34% exit) / +50% Profit Lock SL (100%)"),
]


@dataclass
class SubStrategyConfig:
    name: str  # "PC", "Genie", "Others"
    display_name: str = ""
    enabled: bool = True
    paper_mode: bool = False
    execution_mode: str = "live"  # "tracking", "paper", "live"
    entry_limit_mode: str = "dollar"  # "dollar" or "contracts"
    allocation_amount: float = 500.0  # Dollar budget limit
    allocation_contracts: int = 1  # Contract quantity limit
    auto_take_trades: bool = True
    max_premium_pct: float = 25.0
    floor_guard_pct: float = 75.0
    initial_stop_loss_pct: float = -50.0
    ladder_tiers: List[LadderTier] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name or self.name,
            "enabled": self.enabled,
            "paper_mode": self.paper_mode,
            "execution_mode": self.execution_mode,
            "entry_limit_mode": self.entry_limit_mode,
            "allocation_amount": self.allocation_amount,
            "allocation_contracts": self.allocation_contracts,
            "auto_take_trades": self.auto_take_trades,
            "max_premium_pct": self.max_premium_pct,
            "floor_guard_pct": self.floor_guard_pct,
            "initial_stop_loss_pct": self.initial_stop_loss_pct,
            "ladder_tiers": [t.to_dict() for t in self.ladder_tiers],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_name: str = "PC") -> SubStrategyConfig:
        name = str(data.get("name", default_name))
        raw_tiers = data.get("ladder_tiers", [])
        tiers = [LadderTier.from_dict(t) for t in raw_tiers] if raw_tiers else []
        paper_mode = bool(data.get("paper_mode", False))
        exec_mode = str(data.get("execution_mode", "")).strip().lower()

        if name == "Others":
            # Requirement: Others cannot be Live: Tracking or Paper
            if exec_mode == "live" or not exec_mode:
                exec_mode = "tracking"
            paper_mode = True
        elif name == "Genie":
            if not exec_mode:
                exec_mode = "tracking"
            paper_mode = (exec_mode != "live")
        elif not exec_mode:
            exec_mode = "paper" if paper_mode else "live"

        entry_limit_mode = str(data.get("entry_limit_mode", "contracts" if name == "Genie" else "dollar")).strip().lower()
        if entry_limit_mode not in ("dollar", "contracts"):
            entry_limit_mode = "contracts" if name == "Genie" else "dollar"
        allocation_contracts = max(1, int(data.get("allocation_contracts", 1)))

        initial_sl = float(data.get("initial_stop_loss_pct", -50.0))
        if tiers and len(tiers) > 0:
            initial_sl = tiers[0].stop_loss_pct

        return cls(
            name=name,
            display_name=str(data.get("display_name", name)),
            enabled=bool(data.get("enabled", True)),
            paper_mode=paper_mode,
            execution_mode=exec_mode,
            entry_limit_mode=entry_limit_mode,
            allocation_amount=float(data.get("allocation_amount", 500.0)),
            allocation_contracts=allocation_contracts,
            auto_take_trades=bool(data.get("auto_take_trades", True if exec_mode != "tracking" else False)),
            max_premium_pct=float(data.get("max_premium_pct", 25.0)),
            floor_guard_pct=float(data.get("floor_guard_pct", 75.0)),
            initial_stop_loss_pct=initial_sl,
            ladder_tiers=tiers,
        )


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

        # Config fields (root / PC sync)
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

        # Sub-strategies map: "PC", "Genie", "Others"
        self.sub_strategies: Dict[str, SubStrategyConfig] = {}

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
            self._init_default_sub_strategies()
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
                num_t = len(loaded_tiers)
                if num_t > 0 and all(t.exit_pct <= 0 for t in loaded_tiers):
                    for i, t in enumerate(loaded_tiers):
                        t.exit_pct = round(100.0 / num_t, 1) if i < num_t - 1 else round(100.0 - (round(100.0 / num_t, 1) * (num_t - 1)), 1)
                self.ladder_tiers = loaded_tiers
            else:
                self.ladder_tiers = list(DEFAULT_LADDER_TIERS)

            if self.ladder_tiers:
                self.initial_stop_loss_pct = self.ladder_tiers[0].stop_loss_pct

            # Load sub-strategies map
            raw_sub = data.get("sub_strategies", {})
            self.sub_strategies = {}
            if isinstance(raw_sub, dict) and raw_sub:
                for k, v in raw_sub.items():
                    norm_k = classify_sub_strategy(k)
                    if isinstance(v, dict):
                        self.sub_strategies[norm_k] = SubStrategyConfig.from_dict(v, default_name=norm_k)

            # Ensure all standard sub-strategies are present
            self._ensure_standard_sub_strategies()
            self._save_config_unlocked()

        except Exception as exc:
            log.error("Failed to load clone trades config, using defaults: %s", exc)
            self._init_default_sub_strategies()
            self._save_config_unlocked()

    def _init_default_sub_strategies(self) -> None:
        """Initialize PC, Genie, and Others default sub-strategy configs."""
        self.sub_strategies = {
            "PC": SubStrategyConfig(
                name="PC",
                display_name="PC Discretionary",
                enabled=True,
                paper_mode=self.paper_mode,
                execution_mode="paper" if self.paper_mode else "live",
                entry_limit_mode="dollar",
                allocation_amount=self.allocation_amount,
                allocation_contracts=1,
                auto_take_trades=self.auto_take_trades if self.auto_take_trades is not None else True,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=self.initial_stop_loss_pct,
                ladder_tiers=list(self.ladder_tiers) if self.ladder_tiers else list(DEFAULT_PC_LADDER_TIERS),
            ),
            "Genie": SubStrategyConfig(
                name="Genie",
                display_name="Genie Discretionary",
                enabled=True,
                paper_mode=True,  # Default to tracking/paper
                execution_mode="tracking",
                entry_limit_mode="contracts",
                allocation_contracts=1,
                auto_take_trades=False,
                allocation_amount=self.allocation_amount,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=-40.0,
                ladder_tiers=list(DEFAULT_GENIE_LADDER_TIERS),
            ),
            "Others": SubStrategyConfig(
                name="Others",
                display_name="Others Bucket (Tracking)",
                enabled=True,
                paper_mode=True,  # Forced to paper/tracking
                execution_mode="tracking",
                entry_limit_mode="dollar",
                allocation_contracts=1,
                auto_take_trades=False,
                allocation_amount=self.allocation_amount,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=-50.0,
                ladder_tiers=list(DEFAULT_OTHERS_LADDER_TIERS),
            ),
        }

    def _ensure_standard_sub_strategies(self) -> None:
        """Ensure PC, Genie, and Others exist in sub_strategies."""
        if "PC" not in self.sub_strategies:
            self.sub_strategies["PC"] = SubStrategyConfig(
                name="PC",
                display_name="PC Discretionary",
                enabled=True,
                paper_mode=self.paper_mode,
                execution_mode="paper" if self.paper_mode else "live",
                entry_limit_mode="dollar",
                allocation_contracts=1,
                auto_take_trades=self.auto_take_trades,
                allocation_amount=self.allocation_amount,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=self.initial_stop_loss_pct,
                ladder_tiers=list(self.ladder_tiers) if self.ladder_tiers else list(DEFAULT_PC_LADDER_TIERS),
            )
        else:
            # Sync PC settings with root
            pc = self.sub_strategies["PC"]
            if not pc.ladder_tiers:
                pc.ladder_tiers = list(self.ladder_tiers)
            self.paper_mode = pc.paper_mode
            self.auto_take_trades = pc.auto_take_trades

        if "Genie" not in self.sub_strategies:
            self.sub_strategies["Genie"] = SubStrategyConfig(
                name="Genie",
                display_name="Genie Discretionary",
                enabled=True,
                paper_mode=True,
                execution_mode="tracking",
                entry_limit_mode="contracts",
                allocation_contracts=1,
                auto_take_trades=False,
                allocation_amount=self.allocation_amount,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=-40.0,
                ladder_tiers=list(DEFAULT_GENIE_LADDER_TIERS),
            )
        else:
            genie = self.sub_strategies["Genie"]
            if not getattr(genie, "execution_mode", ""):
                genie.execution_mode = "tracking"
                genie.paper_mode = True

        if "Others" not in self.sub_strategies:
            self.sub_strategies["Others"] = SubStrategyConfig(
                name="Others",
                display_name="Others Bucket (Tracking)",
                enabled=True,
                paper_mode=True,
                execution_mode="tracking",
                entry_limit_mode="dollar",
                allocation_contracts=1,
                auto_take_trades=False,
                allocation_amount=self.allocation_amount,
                max_premium_pct=self.max_premium_pct,
                floor_guard_pct=self.floor_guard_pct,
                initial_stop_loss_pct=-50.0,
                ladder_tiers=list(DEFAULT_OTHERS_LADDER_TIERS),
            )
        else:
            # Enforce paper/tracking mode on Others
            others = self.sub_strategies["Others"]
            others.paper_mode = True
            if getattr(others, "execution_mode", "") == "live" or not getattr(others, "execution_mode", ""):
                others.execution_mode = "tracking"

    def get_sub_strategy_config(self, sub_strat: str) -> SubStrategyConfig:
        with self._lock:
            bucket = classify_sub_strategy(sub_strat)
            if bucket == "PC" and "PC" in self.sub_strategies:
                pc = self.sub_strategies["PC"]
                pc.paper_mode = self.paper_mode
                pc.auto_take_trades = self.auto_take_trades
                pc.max_premium_pct = self.max_premium_pct
                pc.floor_guard_pct = self.floor_guard_pct
                pc.allocation_amount = self.allocation_amount
                pc.initial_stop_loss_pct = self.initial_stop_loss_pct
                if self.ladder_tiers:
                    pc.ladder_tiers = list(self.ladder_tiers)
                return pc
            if bucket in self.sub_strategies:
                return self.sub_strategies[bucket]
            return self.sub_strategies.get("Others") or self.sub_strategies["PC"]

    def update_sub_strategy(self, name: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            bucket = classify_sub_strategy(name)
            cfg = self.sub_strategies.get(bucket)
            if not cfg:
                cfg = SubStrategyConfig(name=bucket)
                self.sub_strategies[bucket] = cfg

            if "enabled" in updates:
                cfg.enabled = bool(updates["enabled"])
            if "execution_mode" in updates:
                mode = str(updates["execution_mode"]).strip().lower()
                if bucket == "Others" and mode == "live":
                    mode = "tracking"
                cfg.execution_mode = mode
                cfg.paper_mode = (mode != "live")
                if mode == "tracking":
                    cfg.auto_take_trades = False
                elif mode in ("live", "paper") and "auto_take_trades" not in updates:
                    cfg.auto_take_trades = True
            if "paper_mode" in updates and "execution_mode" not in updates:
                if bucket == "Others":
                    cfg.paper_mode = True  # Enforce paper only
                    cfg.execution_mode = "tracking"
                else:
                    cfg.paper_mode = bool(updates["paper_mode"])
                    cfg.execution_mode = "paper" if cfg.paper_mode else "live"
            if "entry_limit_mode" in updates:
                m = str(updates["entry_limit_mode"]).strip().lower()
                if m in ("dollar", "contracts"):
                    cfg.entry_limit_mode = m
            if "allocation_contracts" in updates:
                cfg.allocation_contracts = max(1, int(updates["allocation_contracts"]))
            if "auto_take_trades" in updates:
                cfg.auto_take_trades = bool(updates["auto_take_trades"])
            if "allocation_amount" in updates:
                cfg.allocation_amount = max(10.0, float(updates["allocation_amount"]))
            if "max_premium_pct" in updates:
                cfg.max_premium_pct = max(0.0, float(updates["max_premium_pct"]))
            if "floor_guard_pct" in updates:
                cfg.floor_guard_pct = max(0.0, float(updates["floor_guard_pct"]))
            if "initial_stop_loss_pct" in updates:
                cfg.initial_stop_loss_pct = float(updates["initial_stop_loss_pct"])

            if "ladder_tiers" in updates and isinstance(updates["ladder_tiers"], list):
                tiers = []
                num_updates = len(updates["ladder_tiers"])
                for idx, t in enumerate(updates["ladder_tiers"]):
                    t_dict = t.to_dict() if isinstance(t, LadderTier) else (t if isinstance(t, dict) else {})
                    raw_exit = t_dict.get("exit_pct", t_dict.get("contracts_pct"))
                    if raw_exit is not None and float(raw_exit) > 0:
                        exit_val = float(raw_exit)
                    else:
                        exit_val = round(100.0 / num_updates, 1) if idx < num_updates - 1 else round(100.0 - (round(100.0 / num_updates, 1) * (num_updates - 1)), 1)
                    tiers.append(
                        LadderTier(
                            tier_id=idx + 1,
                            target_profit_pct=float(t_dict.get("target_profit_pct", 25.0)),
                            stop_loss_pct=float(t_dict.get("stop_loss_pct", -50.0)),
                            exit_pct=exit_val,
                            description=str(t_dict.get("description", f"Tier {idx + 1}")),
                        )
                    )
                tiers.sort(key=lambda x: x.target_profit_pct)
                cfg.ladder_tiers = tiers
                if cfg.ladder_tiers:
                    cfg.initial_stop_loss_pct = cfg.ladder_tiers[0].stop_loss_pct
            elif "initial_stop_loss_pct" in updates and cfg.ladder_tiers:
                cfg.ladder_tiers[0].stop_loss_pct = cfg.initial_stop_loss_pct

            # If updating PC, also keep root config synchronized
            if bucket == "PC":
                self.auto_take_trades = cfg.auto_take_trades
                self.max_premium_pct = cfg.max_premium_pct
                self.floor_guard_pct = cfg.floor_guard_pct
                self.allocation_amount = cfg.allocation_amount
                self.initial_stop_loss_pct = cfg.initial_stop_loss_pct
                self.paper_mode = cfg.paper_mode
                self.ladder_tiers = list(cfg.ladder_tiers)

            self._save_config_unlocked()
            return cfg.to_dict()

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
            "sub_strategies": {k: v.to_dict() for k, v in self.sub_strategies.items()},
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
            # Handle nested sub_strategies dictionary if provided
            if "sub_strategies" in updates and isinstance(updates["sub_strategies"], dict):
                for s_name, s_upd in updates["sub_strategies"].items():
                    if isinstance(s_upd, dict):
                        self.update_sub_strategy(s_name, s_upd)

            # Handle targeted sub-strategy update
            if "sub_strategy" in updates and isinstance(updates.get("sub_strategy_data"), dict):
                self.update_sub_strategy(str(updates["sub_strategy"]), updates["sub_strategy_data"])

            # Handle root updates (legacy & PC sync)
            if "auto_take_trades" in updates:
                self.auto_take_trades = bool(updates["auto_take_trades"])
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].auto_take_trades = self.auto_take_trades
            if "max_premium_pct" in updates:
                self.max_premium_pct = max(0.0, float(updates["max_premium_pct"]))
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].max_premium_pct = self.max_premium_pct
            if "floor_guard_pct" in updates:
                self.floor_guard_pct = max(0.0, float(updates["floor_guard_pct"]))
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].floor_guard_pct = self.floor_guard_pct
            if "allocation_amount" in updates:
                self.allocation_amount = max(10.0, float(updates["allocation_amount"]))
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].allocation_amount = self.allocation_amount
            if "initial_stop_loss_pct" in updates:
                self.initial_stop_loss_pct = float(updates["initial_stop_loss_pct"])
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].initial_stop_loss_pct = self.initial_stop_loss_pct
            if "paper_mode" in updates:
                self.paper_mode = bool(updates["paper_mode"])
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].paper_mode = self.paper_mode
            if "allow_off_hours_orders" in updates:
                self.allow_off_hours_orders = bool(updates["allow_off_hours_orders"])
            if "allowed_ips" in updates and isinstance(updates["allowed_ips"], list):
                self.allowed_ips = [str(x).strip() for x in updates["allowed_ips"] if str(x).strip()]
            if "ladder_tiers" in updates and isinstance(updates["ladder_tiers"], list):
                tiers = []
                num_updates = len(updates["ladder_tiers"])
                for idx, t in enumerate(updates["ladder_tiers"]):
                    t_dict = t.to_dict() if isinstance(t, LadderTier) else (t if isinstance(t, dict) else {})
                    raw_exit = t_dict.get("exit_pct", t_dict.get("contracts_pct"))
                    if raw_exit is not None and float(raw_exit) > 0:
                        exit_val = float(raw_exit)
                    else:
                        exit_val = round(100.0 / num_updates, 1) if idx < num_updates - 1 else round(100.0 - (round(100.0 / num_updates, 1) * (num_updates - 1)), 1)
                    tiers.append(
                        LadderTier(
                            tier_id=idx + 1,
                            target_profit_pct=float(t_dict.get("target_profit_pct", 25.0)),
                            stop_loss_pct=float(t_dict.get("stop_loss_pct", -50.0)),
                            exit_pct=exit_val,
                            description=str(t_dict.get("description", f"Tier {idx + 1}")),
                        )
                    )
                tiers.sort(key=lambda x: x.target_profit_pct)
                self.ladder_tiers = tiers
                if self.ladder_tiers:
                    self.initial_stop_loss_pct = self.ladder_tiers[0].stop_loss_pct
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].ladder_tiers = list(tiers)
                    self.sub_strategies["PC"].initial_stop_loss_pct = self.initial_stop_loss_pct
            elif "initial_stop_loss_pct" in updates and self.ladder_tiers:
                self.ladder_tiers[0].stop_loss_pct = self.initial_stop_loss_pct
                if "PC" in self.sub_strategies:
                    self.sub_strategies["PC"].initial_stop_loss_pct = self.initial_stop_loss_pct

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
                "sub_strategies": {k: v.to_dict() for k, v in self.sub_strategies.items()},
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
