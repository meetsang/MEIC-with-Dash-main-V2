"""Execution engine for Discretionary Clone Trades with unbalanced OCO ladder management."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import math
import os
import threading
import time
from typing import Any, Dict, List, Optional
import uuid

from clone_trades.config import CloneTradeConfigManager, get_config
from clone_trades.models import (
    CloneSignal,
    CloneTrade,
    ClosedLeg,
    LadderTier,
    OptionSpec,
    distribute_contracts_across_tiers,
    utc_now_iso,
)
from clone_trades.quotes import OptionQuote, OptionQuoteService

log = logging.getLogger("clone_trades.engine")


def is_market_open() -> bool:
    """Return True if US equity options market is currently open (Mon-Fri 8:30 AM - 3:00 PM CT)."""
    try:
        from meic0dte.app.utilities import central_now
        from datetime import time
        now = central_now()
        if now.weekday() >= 5:  # Saturday or Sunday
            return False
        return time(8, 30) <= now.time() < time(15, 0)
    except Exception:
        return False


class CloneTradeEngine:
    """Core engine managing signals, broker execution, and unbalanced OCO ladders."""

    def __init__(
        self,
        config_mgr: Optional[CloneTradeConfigManager] = None,
        quote_service: Optional[OptionQuoteService] = None,
        broker=None,
    ):
        self.config_mgr = config_mgr or get_config()
        self.broker = broker
        if not self.broker:
            try:
                from common.broker_factory import get_shared_broker_for_strategy
                self.broker = get_shared_broker_for_strategy("CLONE_TRADES", paper=self.config_mgr.paper_mode)
                if hasattr(self.broker, "connect") and not getattr(self.broker, "_connected", False):
                    self.broker.connect()
            except Exception as exc:
                log.warning("Could not initialize clone trades broker: %s", exc)
                self.broker = None

        self.quote_service = quote_service or OptionQuoteService(broker=self.broker)
        if self.broker and not self.quote_service.broker:
            self.quote_service.broker = self.broker

        self._lock = threading.Lock()

        self.active_trades: Dict[str, CloneTrade] = {}
        self.closed_trades: List[CloneTrade] = []
        self.signals: List[CloneSignal] = []

        self._trades_journal_path = os.path.join(self.config_mgr.data_dir, "trades.jsonl")
        self._signals_journal_path = os.path.join(self.config_mgr.data_dir, "signals.jsonl")

        self._load_state()

    def _resolve_broker(self):
        """Ensure self.broker matches the configured paper_mode and is live when paper_mode=False."""
        target_paper = self.config_mgr.paper_mode
        # Preserve mock broker in tests
        if self.broker is not None and (getattr(self.broker, "_is_mock", False) or "Mock" in type(self.broker).__name__):
            return self.broker

        current_is_journal = getattr(self.broker, "_is_journal", False)
        current_is_paper = current_is_journal or getattr(self.broker, "_is_paper", False)

        needs_reload = False
        if not target_paper and (self.broker is None or current_is_journal):
            needs_reload = True
        elif target_paper and (self.broker is None or not current_is_paper):
            needs_reload = True

        if needs_reload:
            try:
                from common.broker_factory import get_shared_broker_for_strategy
                self.broker = get_shared_broker_for_strategy("CLONE_TRADES", paper=target_paper, reset=True)
                if hasattr(self.broker, "connect") and not getattr(self.broker, "_connected", False):
                    self.broker.connect()
                if self.quote_service:
                    self.quote_service.broker = self.broker
                log.info("Clone trades broker resolved: %s (paper=%s)", type(self.broker).__name__, target_paper)
            except Exception as exc:
                log.error("Failed to resolve broker for clone trades (paper=%s): %s", target_paper, exc)
        return self.broker

    def _load_state(self) -> None:
        """Load recent signals and trades from disk."""
        if os.path.exists(self._trades_journal_path):
            try:
                trades_by_id: Dict[str, CloneTrade] = {}
                with open(self._trades_journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            trade_dict = json.loads(line)
                            trade = CloneTrade.from_dict(trade_dict)
                            # Later entries in the journal overwrite earlier states for the same trade ID
                            trades_by_id[trade.id] = trade
                        except Exception as e:
                            log.warning("Skipping corrupted trade line: %s", e)

                today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self.active_trades.clear()
                self.closed_trades.clear()
                for trade in trades_by_id.values():
                    is_expired = False
                    if trade.spec and trade.spec.expiration and trade.spec.expiration < today_iso:
                        is_expired = True

                    if trade.status in ("OPEN", "WORKING") and trade.remaining_quantity > 0 and not is_expired:
                        self.active_trades[trade.id] = trade
                    else:
                        if is_expired and trade.status in ("OPEN", "WORKING"):
                            trade.status = "CLOSED"
                            trade.status_reason = "Expired (contract passed expiration date)"
                            trade.remaining_quantity = 0
                            trade.unrealized_pnl_usd = 0.0
                        self.closed_trades.append(trade)
            except Exception as exc:
                log.warning("Failed to load clone trades journal: %s", exc)

        if os.path.exists(self._signals_journal_path):
            try:
                signals_by_id: Dict[str, CloneSignal] = {}
                with open(self._signals_journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            sig = CloneSignal.from_dict(json.loads(line))
                            signals_by_id[sig.id] = sig
                        except Exception as e:
                            log.warning("Skipping corrupted signal line: %s", e)
                self.signals = list(signals_by_id.values())
            except Exception as exc:
                log.warning("Failed to load clone signals journal: %s", exc)

    def _persist_trade(self, trade: CloneTrade) -> None:
        """Append or update trade in journal."""
        try:
            with open(self._trades_journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(trade.to_dict()) + "\n")
        except Exception as exc:
            log.error("Failed to persist trade %s: %s", trade.id, exc)

    def _persist_signal(self, sig: CloneSignal) -> None:
        try:
            with open(self._signals_journal_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(sig.to_dict()) + "\n")
        except Exception as exc:
            log.error("Failed to persist signal %s: %s", sig.id, exc)

    def _normalize_alert_price(self, raw_price: Any, market_quote_price: float) -> float:
        """Convert alert price to per-share price.
        Handles both per-share notation ($0.80) and contract-dollar notation ($80).
        """
        try:
            px = float(str(raw_price).replace(",", "").replace("$", "").strip())
        except (ValueError, TypeError):
            return market_quote_price if market_quote_price > 0 else 1.0

        if px <= 0:
            return market_quote_price if market_quote_price > 0 else 1.0

        # If alert is e.g. 80 and market is e.g. 1.00 (or alert > 10 and market <= 10)
        if px >= 15.0 and market_quote_price > 0 and market_quote_price <= 15.0:
            # Alert price was sent in contract dollars (e.g. 80 = $0.80/share)
            return px / 100.0

        return px

    def process_signal(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Main entry point for inbound webhook signal."""
        sig_id = f"sig_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
        now_iso = utc_now_iso()

        # 1. Parse option specification
        try:
            spec = OptionSpec.from_payload(payload)
        except Exception as exc:
            log.error("Failed to parse option spec from webhook: %s", exc)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=None,
                alert_price=0.0,
                status="REJECTED_ERROR",
                status_reason=f"Spec parse error: {exc}",
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {"ok": False, "error": str(exc), "status": "REJECTED_ERROR"}

        # 2. Fetch live quote
        quote = self.quote_service.get_quote(spec)
        market_px = quote.market_buy_price
        raw_alert = payload.get("price") or payload.get("entry_price") or payload.get("alert_price")
        alert_px = self._normalize_alert_price(raw_alert, market_px)

        cfg = self.config_mgr
        market_open = is_market_open()

        # Handle missing quote
        if market_px <= 0:
            if not market_open and not cfg.paper_mode:
                reason = "Options market is CLOSED (Mon-Fri 8:30 AM - 3:00 PM CT) and no live quote available. Auto-take paused."
                log.info("SIGNAL HELD (Market Closed, No Quote): %s %s", spec.underlying, spec.occ_symbol)
                sig = CloneSignal(
                    id=sig_id,
                    received_at=now_iso,
                    raw_payload=payload,
                    spec=spec,
                    alert_price=alert_px,
                    market_price=0.0,
                    premium_pct=0.0,
                    status="MARKET_CLOSED",
                    status_reason=reason,
                )
                with self._lock:
                    self.signals.append(sig)
                    self._persist_signal(sig)
                return {
                    "ok": True,
                    "status": "MARKET_CLOSED",
                    "signal": sig.to_dict(),
                    "message": reason,
                }
            elif not cfg.paper_mode:
                reason = f"Unable to fetch live quote for {spec.occ_symbol} to verify chase guard."
                log.warning("REJECTED NO QUOTE: %s", reason)
                sig = CloneSignal(
                    id=sig_id,
                    received_at=now_iso,
                    raw_payload=payload,
                    spec=spec,
                    alert_price=alert_px,
                    market_price=0.0,
                    premium_pct=0.0,
                    status="REJECTED_NO_QUOTE",
                    status_reason=reason,
                )
                with self._lock:
                    self.signals.append(sig)
                    self._persist_signal(sig)
                return {
                    "ok": False,
                    "status": "REJECTED_NO_QUOTE",
                    "error": reason,
                    "signal": sig.to_dict(),
                }
            else:
                # Paper mode simulation fallback
                market_px = alert_px

        # 3. Calculate premium % diff
        if alert_px > 0:
            premium_pct = round(((market_px - alert_px) / alert_px) * 100.0, 2)
        else:
            premium_pct = 0.0

        # 4. Check Chase Guard (#4)
        if premium_pct > cfg.max_premium_pct:
            reason = (
                f"Trading at {premium_pct:+.1f}% above alert price ${alert_px:.2f} "
                f"(exceeds max allowed chase of {cfg.max_premium_pct:.1f}%)"
            )
            log.warning("CHASE EXCEEDED: %s %s -> %s", spec.underlying, spec.streamer_symbol, reason)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="CHASE_EXCEEDED",
                status_reason=reason,
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {
                "ok": True,
                "status": "CHASE_EXCEEDED",
                "signal": sig.to_dict(),
                "message": f"Signal held for manual decision: {reason}",
            }

        # 4b. Check Floor Guard (prevents late alerts on crashing/dead contracts)
        drop_pct = round(((alert_px - market_px) / alert_px) * 100.0, 2) if alert_px > 0 else 0.0
        floor_limit = getattr(cfg, "floor_guard_pct", 75.0)
        if floor_limit > 0 and drop_pct > floor_limit:
            reason = (
                f"Trading at {drop_pct:.1f}% below alert price ${alert_px:.2f} (market: ${market_px:.2f}) "
                f"(exceeds max allowed floor drop of {floor_limit:.1f}%)"
            )
            log.warning("FLOOR EXCEEDED: %s %s -> %s", spec.underlying, spec.streamer_symbol, reason)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="FLOOR_EXCEEDED",
                status_reason=reason,
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {
                "ok": True,
                "status": "FLOOR_EXCEEDED",
                "signal": sig.to_dict(),
                "message": f"Signal held for manual decision: {reason}",
            }

        # 5. Check Daily Flag (#3)
        if not cfg.auto_take_trades:
            reason = "Daily Auto-Take flag is OFF. Trade skipped on brokerage."
            log.info("SIGNAL SKIPPED (Daily Flag OFF): %s %s", spec.underlying, spec.streamer_symbol)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="SKIPPED",
                status_reason=reason,
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {
                "ok": True,
                "status": "SKIPPED",
                "signal": sig.to_dict(),
                "message": reason,
            }

        # 6. Check Market Open Gate
        if not market_open and not cfg.paper_mode and not getattr(cfg, "allow_off_hours_orders", False):
            reason = "Options market is CLOSED (Mon-Fri 8:30 AM - 3:00 PM CT). Auto-take paused."
            log.info("SIGNAL BLOCKED (Market Closed): %s %s", spec.underlying, spec.occ_symbol)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="MARKET_CLOSED",
                status_reason=reason,
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {
                "ok": True,
                "status": "MARKET_CLOSED",
                "signal": sig.to_dict(),
                "message": reason,
            }

        # 7. Calculate contract quantity (#5)
        contract_cost = max(1.0, market_px * 100.0)
        calc_qty = int(cfg.allocation_amount // contract_cost)
        qty = max(1, calc_qty)

        # Allow explicit override from payload if provided and positive
        explicit_qty = payload.get("qty") or payload.get("quantity")
        if explicit_qty:
            try:
                eq = int(explicit_qty)
                if eq > 0:
                    qty = eq
            except Exception:
                pass

        # 8. Execute order and open trade
        try:
            trade = self._execute_and_open_trade(
                spec=spec,
                qty=qty,
                fill_price=market_px,
                alert_price=alert_px,
                premium_pct=premium_pct,
                sig_id=sig_id,
            )
        except Exception as exc:
            log.error("Broker order execution error: %s", exc)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="REJECTED_BROKER",
                status_reason=f"Broker execution failed: {exc}",
            )
            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)
            return {
                "ok": False,
                "status": "REJECTED_BROKER",
                "error": str(exc),
                "signal": sig.to_dict(),
            }

        sig_status = "WORKING" if trade.status == "WORKING" else "TAKEN"
        sig_reason = (
            f"Queued {qty} contracts on brokerage @ ${market_px:.2f} (awaiting market open)"
            if trade.status == "WORKING"
            else f"Filled {qty} contracts @ ${market_px:.2f}"
        )

        sig = CloneSignal(
            id=sig_id,
            received_at=now_iso,
            raw_payload=payload,
            spec=spec,
            alert_price=alert_px,
            market_price=market_px,
            premium_pct=premium_pct,
            status=sig_status,
            status_reason=sig_reason,
            trade_id=trade.id,
        )
        with self._lock:
            self.signals.append(sig)
            self._persist_signal(sig)

        return {
            "ok": True,
            "status": sig_status,
            "trade": trade.to_dict(),
            "signal": sig.to_dict(),
        }

    def _execute_and_open_trade(
        self,
        spec: OptionSpec,
        qty: int,
        fill_price: float,
        alert_price: float,
        premium_pct: float,
        sig_id: str,
    ) -> CloneTrade:
        """Place buy order on broker (or simulate) and setup unbalanced OCO ladder."""
        cfg = self.config_mgr
        order_id = f"ord_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        trade_status = "OPEN"
        trade_status_reason = "Active trade"

        # Attempt broker execution if live
        if not cfg.paper_mode:
            self._resolve_broker()
            if not self.broker:
                raise RuntimeError("Live broker connection unavailable. Order rejected.")
            if getattr(self.broker, "_is_journal", False):
                raise RuntimeError("Live TastyTrade broker unavailable (found JournalBroker in live mode). Order rejected.")

            res = self.broker.place_limit_order(
                "BUY_TO_OPEN", spec.occ_symbol, qty, fill_price
            )
            if not getattr(res, "success", False):
                err_msg = getattr(res, "message", None) or "Broker rejected order"
                raise RuntimeError(f"Broker order rejected: {err_msg}")

            if getattr(res, "order_id", None):
                order_id = str(res.order_id)

            broker_status = str(getattr(res, "status", "")).lower()
            if getattr(res, "filled_price", None) and float(res.filled_price) > 0:
                fill_price = float(res.filled_price)
            elif order_id and hasattr(self.broker, "get_order_status"):
                try:
                    time.sleep(0.3)
                    ost = self.broker.get_order_status(order_id)
                    if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                        fill_price = float(ost.filled_price)
                        broker_status = str(getattr(ost, "status", broker_status)).lower()
                except Exception as exc:
                    log.debug("Could not query order status immediately: %s", exc)

            if broker_status in ("working", "received"):
                trade_status = "WORKING"
                trade_status_reason = f"Order #{order_id} queued on brokerage (awaiting market open)"

        contract_cost = fill_price * 100.0
        trade_id = f"clone_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        now_iso = utc_now_iso()

        # Unbalanced OCO Ladder initialization (#6)
        num_tiers = len(cfg.ladder_tiers)
        tier_pcts = [t.exit_pct for t in cfg.ladder_tiers]
        contracts_per_tier = distribute_contracts_across_tiers(qty, num_tiers, tier_pcts=tier_pcts)
        tier_distribution = []
        for i, t in enumerate(cfg.ladder_tiers):
            eff_exit_pct = t.exit_pct if t.exit_pct > 0 else round(100.0 / max(1, num_tiers), 1)
            tier_distribution.append({
                "tier_id": t.tier_id,
                "target_profit_pct": t.target_profit_pct,
                "stop_loss_pct": t.stop_loss_pct,
                "exit_pct": eff_exit_pct,
                "contracts": contracts_per_tier[i] if i < len(contracts_per_tier) else 0,
            })

        # Initial Stop Loss applies to ALL contracts (defined by Tier 1 of ladder or initial_stop_loss_pct)
        if cfg.ladder_tiers and len(cfg.ladder_tiers) > 0:
            initial_sl_pct = cfg.ladder_tiers[0].stop_loss_pct
        else:
            initial_sl_pct = cfg.initial_stop_loss_pct
        active_stop_price = round(max(0.01, fill_price * (1.0 + initial_sl_pct / 100.0)), 2)

        # Next Profit Target for Tier 1
        tier_0 = cfg.ladder_tiers[0] if cfg.ladder_tiers else LadderTier(1, 25.0, -50.0)
        active_pt_price = round(fill_price * (1.0 + tier_0.target_profit_pct / 100.0), 2)

        trade = CloneTrade(
            id=trade_id,
            created_at=now_iso,
            spec=spec,
            action="BUY",
            webhook_price=alert_price,
            current_price_at_signal=fill_price,
            premium_pct_at_signal=premium_pct,
            entry_price=fill_price,
            contract_cost=contract_cost,
            initial_quantity=qty,
            remaining_quantity=qty,
            current_price=fill_price,
            highest_price=fill_price,
            highest_profit_pct=0.0,
            status=trade_status,
            status_reason=trade_status_reason,
            current_tier_index=0,
            active_pt_price=active_pt_price,
            active_stop_price=active_stop_price,
            active_stop_pct=initial_sl_pct,
            tier_distribution=tier_distribution,
            closed_legs=[],
            realized_pnl_usd=0.0,
            unrealized_pnl_pct=0.0,
            unrealized_pnl_usd=0.0,
            order_id=order_id,
        )

        with self._lock:
            self.active_trades[trade.id] = trade
            self._persist_trade(trade)

        log.info(
            "OPENED CLONE TRADE %s: %s %d contracts @ $%.2f (status=%s) | PT1=$%.2f SL=$%.2f",
            trade.id,
            spec.streamer_symbol,
            qty,
            fill_price,
            trade.status,
            active_pt_price,
            active_stop_price,
        )
        return trade

    def take_signal_manually(
        self,
        signal_id: str,
        custom_price: Optional[float] = None,
        custom_qty: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Allow user to manually take a trade previously held or skipped."""
        with self._lock:
            target_sig = next((s for s in self.signals if s.id == signal_id), None)
        if not target_sig:
            return {"ok": False, "error": f"Signal {signal_id} not found"}
        if not target_sig.spec:
            return {"ok": False, "error": "Signal does not have a valid option spec"}

        quote = self.quote_service.get_quote(target_sig.spec)
        market_px = custom_price if custom_price and custom_price > 0 else quote.market_buy_price
        if market_px <= 0:
            market_px = target_sig.alert_price

        if custom_qty and custom_qty > 0:
            qty = custom_qty
        else:
            cost = max(1.0, market_px * 100.0)
            qty = max(1, int(self.config_mgr.allocation_amount // cost))

        premium_pct = round(((market_px - target_sig.alert_price) / target_sig.alert_price) * 100.0, 2)
        try:
            trade = self._execute_and_open_trade(
                spec=target_sig.spec,
                qty=qty,
                fill_price=market_px,
                alert_price=target_sig.alert_price,
                premium_pct=premium_pct,
                sig_id=signal_id,
            )
        except Exception as exc:
            log.error("Manual take failed on broker: %s", exc)
            return {"ok": False, "error": str(exc)}

        sig_status = "WORKING" if trade.status == "WORKING" else "TAKEN"
        sig_reason = (
            f"Manually queued {qty} contracts on brokerage @ ${market_px:.2f} (awaiting market open)"
            if trade.status == "WORKING"
            else f"Manually executed {qty} contracts @ ${market_px:.2f}"
        )

        with self._lock:
            target_sig.status = sig_status
            target_sig.status_reason = sig_reason
            target_sig.trade_id = trade.id
            target_sig.manual_decision = {
                "action": "TAKE",
                "timestamp": utc_now_iso(),
                "price": market_px,
                "qty": qty,
            }
            self._persist_signal(target_sig)

        return {"ok": True, "trade": trade.to_dict(), "signal": target_sig.to_dict()}

    def leave_signal(self, signal_id: str) -> Dict[str, Any]:
        """User decides to leave / dismiss a trade held by chase guard."""
        with self._lock:
            target_sig = next((s for s in self.signals if s.id == signal_id), None)
            if not target_sig:
                return {"ok": False, "error": f"Signal {signal_id} not found"}

            target_sig.status = "DISMISSED_MANUAL"
            target_sig.status_reason = "Left / dismissed manually by user"
            target_sig.manual_decision = {
                "action": "LEAVE",
                "timestamp": utc_now_iso(),
            }
            self._persist_signal(target_sig)

        return {"ok": True, "signal": target_sig.to_dict()}

    def update_quotes_and_check_stops(self) -> None:
        """Background loop iteration checking live quotes against ladder PTs and full SL."""
        with self._lock:
            open_trades = list(self.active_trades.values())

        if not open_trades:
            return

        cfg = self.config_mgr

        for trade in open_trades:
            # Reconcile entry fill price on live broker if unverified or still working
            if not cfg.paper_mode and self.broker and trade.order_id and hasattr(self.broker, "get_order_status"):
                if trade.status == "WORKING" or getattr(trade, "_reconcile_entry", True):
                    try:
                        ost = self.broker.get_order_status(trade.order_id)
                        if ost and getattr(ost, "status", "").lower() == "filled":
                            trade.status = "OPEN"
                            if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                                real_fill = float(ost.filled_price)
                                if abs(real_fill - trade.entry_price) > 0.001:
                                    log.info("Reconciled real entry fill on %s: $%.2f -> $%.2f", trade.id, trade.entry_price, real_fill)
                                    trade.entry_price = real_fill
                                    trade.contract_cost = real_fill * 100.0
                                    tier_0 = cfg.ladder_tiers[0] if cfg.ladder_tiers else LadderTier(1, 25.0, trade.active_stop_pct)
                                    trade.active_pt_price = round(real_fill * (1.0 + tier_0.target_profit_pct / 100.0), 2)
                                    trade.active_stop_price = round(max(0.01, real_fill * (1.0 + trade.active_stop_pct / 100.0)), 2)
                                    self._persist_trade(trade)
                            trade._reconcile_entry = False
                    except Exception as exc:
                        log.debug("Reconcile check failed for trade %s: %s", trade.id, exc)

            quote = self.quote_service.get_quote(trade.spec)
            current_px = quote.market_sell_price
            if current_px <= 0:
                current_px = quote.last or quote.mid or trade.current_price
            if current_px <= 0:
                continue

            with self._lock:
                trade.current_price = current_px
                if current_px > trade.highest_price:
                    trade.highest_price = current_px
                    trade.highest_profit_pct = round(
                        ((current_px - trade.entry_price) / trade.entry_price) * 100.0, 2
                    )

                trade.unrealized_pnl_pct = round(
                    ((current_px - trade.entry_price) / trade.entry_price) * 100.0, 2
                )
                trade.unrealized_pnl_usd = round(
                    (current_px - trade.entry_price) * 100.0 * trade.remaining_quantity, 2
                )

                # 1. Check Stop Loss (Full Position Exit)
                if current_px <= trade.active_stop_price:
                    self._trigger_stop_loss_unlocked(trade, current_px)
                    continue

                # 2. Check Profit Target (Unbalanced OCO Partial Exit)
                if current_px >= trade.active_pt_price and trade.remaining_quantity > 0:
                    self._trigger_partial_pt_unlocked(trade, current_px)

    def _trigger_stop_loss_unlocked(self, trade: CloneTrade, exit_px: float) -> None:
        """Stop triggered: sell all remaining contracts immediately."""
        rem_qty = trade.remaining_quantity
        if rem_qty <= 0:
            return

        now_iso = utc_now_iso()

        # Place sell order on broker
        if not self.config_mgr.paper_mode:
            self._resolve_broker()
            if self.broker and not getattr(self.broker, "_is_journal", False):
                try:
                    res = self.broker.place_market_order("SELL_TO_CLOSE", trade.spec.occ_symbol, rem_qty)
                    if res and getattr(res, "order_id", None):
                        trade.close_order_id = str(res.order_id)
                    if res and getattr(res, "filled_price", None) and float(res.filled_price) > 0:
                        exit_px = float(res.filled_price)
                    elif getattr(res, "order_id", None) and hasattr(self.broker, "get_order_status"):
                        try:
                            time.sleep(0.4)
                            ost = self.broker.get_order_status(str(res.order_id))
                            if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                                exit_px = float(ost.filled_price)
                        except Exception as exc:
                            log.warning("Could not query close order fill: %s", exc)
                except Exception as exc:
                    log.warning("Broker stop exit order error: %s", exc)

        profit_pct = round(((exit_px - trade.entry_price) / trade.entry_price) * 100.0, 2)
        pnl_usd = round((exit_px - trade.entry_price) * 100.0 * rem_qty, 2)

        leg = ClosedLeg(
            tier_id=0,
            quantity=rem_qty,
            exit_price=exit_px,
            exit_time=now_iso,
            profit_pct=profit_pct,
            pnl_usd=pnl_usd,
            reason=f"Stop Loss Triggered @ ${exit_px:.2f} ({trade.active_stop_pct:+.1f}%) for all {rem_qty} contracts",
        )
        trade.closed_legs.append(leg)
        trade.realized_pnl_usd += pnl_usd
        trade.remaining_quantity = 0
        trade.unrealized_pnl_usd = 0.0
        trade.status = "CLOSED"
        trade.status_reason = f"Stopped out @ ${exit_px:.2f} ({profit_pct:+.1f}%)"
        trade.closed_at = now_iso

        self.active_trades.pop(trade.id, None)
        self.closed_trades.append(trade)
        self._persist_trade(trade)

        log.info(
            "STOP LOSS EXECUTED %s: Sold %d contracts @ $%.2f | P&L: $%.2f",
            trade.id,
            rem_qty,
            exit_px,
            pnl_usd,
        )

    def _trigger_partial_pt_unlocked(self, trade: CloneTrade, exit_px: float) -> None:
        """Profit target reached: sell this tier's contract tranche, advance to next PT."""
        current_idx = trade.current_tier_index
        cfg = self.config_mgr
        tiers = cfg.ladder_tiers

        # Determine contracts to close for this tier
        tier_dist = trade.tier_distribution
        qty_for_tier = 1
        tier_exit_pct = round(100.0 / max(1, len(tiers)), 1)
        if current_idx < len(tier_dist):
            qty_for_tier = tier_dist[current_idx].get("contracts", 1)
            tier_exit_pct = tier_dist[current_idx].get("exit_pct", tier_exit_pct)

        # Cap at remaining quantity
        qty_to_close = min(trade.remaining_quantity, max(1, qty_for_tier))

        # Place partial sell order on broker
        if not self.config_mgr.paper_mode:
            self._resolve_broker()
            if self.broker and not getattr(self.broker, "_is_journal", False):
                try:
                    res = self.broker.place_limit_order("SELL_TO_CLOSE", trade.spec.occ_symbol, qty_to_close, exit_px)
                    if res and getattr(res, "filled_price", None) and float(res.filled_price) > 0:
                        exit_px = float(res.filled_price)
                    elif getattr(res, "order_id", None) and hasattr(self.broker, "get_order_status"):
                        try:
                            time.sleep(0.3)
                            ost = self.broker.get_order_status(str(res.order_id))
                            if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                                exit_px = float(ost.filled_price)
                        except Exception as exc:
                            log.warning("Could not query partial PT fill: %s", exc)
                except Exception as exc:
                    log.warning("Broker partial PT order error: %s", exc)

        profit_pct = round(((exit_px - trade.entry_price) / trade.entry_price) * 100.0, 2)
        pnl_usd = round((exit_px - trade.entry_price) * 100.0 * qty_to_close, 2)
        now_iso = utc_now_iso()

        tier_name = f"Tier {current_idx + 1}"
        leg = ClosedLeg(
            tier_id=current_idx + 1,
            quantity=qty_to_close,
            exit_price=exit_px,
            exit_time=now_iso,
            profit_pct=profit_pct,
            pnl_usd=pnl_usd,
            reason=f"{tier_name} PT Hit ({tier_exit_pct}% / {qty_to_close} ctrs) @ ${exit_px:.2f} (+{profit_pct:.1f}%)",
        )
        trade.closed_legs.append(leg)
        trade.realized_pnl_usd += pnl_usd
        trade.remaining_quantity -= qty_to_close

        log.info(
            "PARTIAL PT HIT %s (%s): Sold %d contracts @ $%.2f (P&L: $%.2f). Remaining: %d contracts",
            trade.id,
            tier_name,
            qty_to_close,
            exit_px,
            pnl_usd,
            trade.remaining_quantity,
        )

        # If all contracts closed
        if trade.remaining_quantity <= 0:
            trade.status = "CLOSED"
            trade.status_reason = "All ladder profit targets hit"
            trade.closed_at = now_iso
            trade.unrealized_pnl_usd = 0.0
            self.active_trades.pop(trade.id, None)
            self.closed_trades.append(trade)
            self._persist_trade(trade)
            return

        # Advance to next ladder tier for remaining contracts
        trade.current_tier_index += 1
        next_idx = trade.current_tier_index
        if next_idx < len(tiers):
            next_tier = tiers[next_idx]
            trade.active_pt_price = round(trade.entry_price * (1.0 + next_tier.target_profit_pct / 100.0), 2)
            # Update stop loss if tier specifies a tighter stop (e.g. breakeven 0% or lock profit)
            if next_tier.stop_loss_pct > trade.active_stop_pct:
                trade.active_stop_pct = next_tier.stop_loss_pct
                trade.active_stop_price = round(trade.entry_price * (1.0 + next_tier.stop_loss_pct / 100.0), 2)
                log.info(
                    "ADVANCED LADDER %s -> Next PT=%s (+$%.2f), Ratcheted SL=%s ($%.2f)",
                    trade.id,
                    next_tier.description,
                    trade.active_pt_price,
                    next_tier.stop_loss_pct,
                    trade.active_stop_price,
                )
        else:
            # Beyond all tiers: keep current stop, let profit run or trail
            trade.active_pt_price = 999999.0

        self._persist_trade(trade)

    def close_trade_manual(self, trade_id: str) -> Dict[str, Any]:
        """Manual cancellation if order is working, or 1-click Flatten for all remaining contracts if open."""
        with self._lock:
            trade = self.active_trades.get(trade_id)
            if not trade:
                return {"ok": False, "error": f"Open trade {trade_id} not found"}

            now_iso = utc_now_iso()

            # If the trade is still WORKING / QUEUED (not yet filled)
            if trade.status == "WORKING":
                if not self.config_mgr.paper_mode and self.broker and trade.order_id:
                    try:
                        self.broker.cancel_order(trade.order_id)
                    except Exception as exc:
                        log.warning("Broker cancel order error: %s", exc)

                trade.status = "CANCELLED"
                trade.status_reason = f"Working order #{trade.order_id} cancelled by user"
                trade.closed_at = now_iso
                trade.remaining_quantity = 0
                trade.unrealized_pnl_usd = 0.0

                self.active_trades.pop(trade.id, None)
                self.closed_trades.append(trade)
                self._persist_trade(trade)
                log.info("CANCELLED WORKING CLONE ORDER %s (Order #%s)", trade.id, trade.order_id)
                return {"ok": True, "trade": trade.to_dict(), "message": "Working order cancelled on brokerage"}

            # If trade is OPEN (filled)
            rem_qty = trade.remaining_quantity
            quote = self.quote_service.get_quote(trade.spec)
            exit_px = quote.market_sell_price or trade.current_price

            if not self.config_mgr.paper_mode:
                self._resolve_broker()
                if self.broker and not getattr(self.broker, "_is_journal", False):
                    try:
                        res = self.broker.place_market_order("SELL_TO_CLOSE", trade.spec.occ_symbol, rem_qty)
                        if res and getattr(res, "order_id", None):
                            trade.close_order_id = str(res.order_id)
                        if res and getattr(res, "filled_price", None) and float(res.filled_price) > 0:
                            exit_px = float(res.filled_price)
                        elif getattr(res, "order_id", None) and hasattr(self.broker, "get_order_status"):
                            try:
                                time.sleep(0.4)
                                ost = self.broker.get_order_status(str(res.order_id))
                                if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                                    exit_px = float(ost.filled_price)
                            except Exception as exc:
                                log.warning("Could not query manual close fill: %s", exc)
                    except Exception as exc:
                        log.warning("Broker flatten error: %s", exc)

            profit_pct = round(((exit_px - trade.entry_price) / trade.entry_price) * 100.0, 2)
            pnl_usd = round((exit_px - trade.entry_price) * 100.0 * rem_qty, 2)

            leg = ClosedLeg(
                tier_id=99,
                quantity=rem_qty,
                exit_price=exit_px,
                exit_time=now_iso,
                profit_pct=profit_pct,
                pnl_usd=pnl_usd,
                reason=f"Manual Flatten @ ${exit_px:.2f} for {rem_qty} contracts",
            )
            trade.closed_legs.append(leg)
            trade.realized_pnl_usd += pnl_usd
            trade.remaining_quantity = 0
            trade.unrealized_pnl_usd = 0.0
            trade.status = "CLOSED"
            trade.status_reason = "Manual Flatten"
            trade.closed_at = now_iso

            self.active_trades.pop(trade.id, None)
            self.closed_trades.append(trade)
            self._persist_trade(trade)

        log.info("MANUAL FLATTEN %s: Closed %d contracts @ $%.2f | P&L: $%.2f", trade_id, rem_qty, exit_px, pnl_usd)
        return {"ok": True, "trade": trade.to_dict()}

    def _apply_ladder_to_trade_unlocked(self, trade: CloneTrade, new_tiers: List[LadderTier]) -> None:
        """Recalculate tier distribution, profit target, and stop loss for an active trade."""
        if not new_tiers:
            return

        num_tiers = len(new_tiers)
        tier_pcts = [t.exit_pct for t in new_tiers]

        # Calculate contract distribution across tiers
        if trade.remaining_quantity == trade.initial_quantity or not trade.closed_legs:
            # Full position remains: distribute across all new tiers
            contracts_per_tier = distribute_contracts_across_tiers(
                trade.remaining_quantity, num_tiers, tier_pcts=tier_pcts
            )
            tier_distribution = []
            for i, t in enumerate(new_tiers):
                eff_exit_pct = t.exit_pct if t.exit_pct > 0 else round(100.0 / max(1, num_tiers), 1)
                tier_distribution.append({
                    "tier_id": t.tier_id,
                    "target_profit_pct": t.target_profit_pct,
                    "stop_loss_pct": t.stop_loss_pct,
                    "exit_pct": eff_exit_pct,
                    "contracts": contracts_per_tier[i] if i < len(contracts_per_tier) else 0,
                })
            trade.tier_distribution = tier_distribution
        else:
            # Position has partial exits: distribute remaining contracts across remaining tiers
            current_idx = min(trade.current_tier_index, num_tiers - 1)
            rem_tiers = new_tiers[current_idx:]
            rem_pcts = [t.exit_pct for t in rem_tiers]
            contracts_rem = distribute_contracts_across_tiers(
                trade.remaining_quantity, len(rem_tiers), tier_pcts=rem_pcts
            )
            tier_distribution = list(trade.tier_distribution[:current_idx])
            for i, t in enumerate(rem_tiers):
                eff_exit_pct = t.exit_pct if t.exit_pct > 0 else round(100.0 / max(1, len(rem_tiers)), 1)
                tier_distribution.append({
                    "tier_id": t.tier_id,
                    "target_profit_pct": t.target_profit_pct,
                    "stop_loss_pct": t.stop_loss_pct,
                    "exit_pct": eff_exit_pct,
                    "contracts": contracts_rem[i] if i < len(contracts_rem) else 0,
                })
            trade.tier_distribution = tier_distribution

        # Update active profit target and stop loss price
        current_idx = min(trade.current_tier_index, num_tiers - 1)
        cur_tier = new_tiers[current_idx]
        trade.active_pt_price = round(trade.entry_price * (1.0 + cur_tier.target_profit_pct / 100.0), 2)
        trade.active_stop_pct = cur_tier.stop_loss_pct
        trade.active_stop_price = round(max(0.01, trade.entry_price * (1.0 + cur_tier.stop_loss_pct / 100.0)), 2)

    def update_open_trades_ladder(self, new_tiers: List[LadderTier]) -> List[CloneTrade]:
        """Apply updated ladder rules to all currently open/active trades."""
        with self._lock:
            updated = []
            for trade in self.active_trades.values():
                if trade.status in ("OPEN", "WORKING") and trade.remaining_quantity > 0:
                    self._apply_ladder_to_trade_unlocked(trade, new_tiers)
                    self._persist_trade(trade)
                    updated.append(trade)
                    log.info(
                        "LADDER UPDATED ON ACTIVE TRADE %s: PT1=$%.2f SL=$%.2f (%.1f%%)",
                        trade.id,
                        trade.active_pt_price,
                        trade.active_stop_price,
                        trade.active_stop_pct,
                    )
            return updated

    def update_single_trade_ladder(self, trade_id: str, raw_tiers: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Update ladder tiers on a specific active trade."""
        with self._lock:
            trade = self.active_trades.get(trade_id)
            if not trade:
                return {"ok": False, "error": f"Open trade {trade_id} not found"}
            if trade.status not in ("OPEN", "WORKING") or trade.remaining_quantity <= 0:
                return {"ok": False, "error": f"Trade {trade_id} is {trade.status} and cannot be modified"}

            num_updates = len(raw_tiers)
            new_tiers = []
            for idx, t in enumerate(raw_tiers):
                t_dict = t.to_dict() if isinstance(t, LadderTier) else (t if isinstance(t, dict) else {})
                raw_exit = t_dict.get("exit_pct", t_dict.get("contracts_pct"))
                if raw_exit is not None and float(raw_exit) > 0:
                    exit_val = float(raw_exit)
                else:
                    exit_val = round(100.0 / num_updates, 1) if idx < num_updates - 1 else round(100.0 - (round(100.0 / num_updates, 1) * (num_updates - 1)), 1)
                new_tiers.append(
                    LadderTier(
                        tier_id=idx + 1,
                        target_profit_pct=float(t_dict.get("target_profit_pct", 25.0)),
                        stop_loss_pct=float(t_dict.get("stop_loss_pct", -50.0)),
                        exit_pct=exit_val,
                        description=str(t_dict.get("description", f"Tier {idx + 1}")),
                    )
                )
            new_tiers.sort(key=lambda x: x.target_profit_pct)

            self._apply_ladder_to_trade_unlocked(trade, new_tiers)
            self._persist_trade(trade)
            log.info(
                "CUSTOM LADDER APPLIED TO TRADE %s: PT=$%.2f SL=$%.2f (%.1f%%)",
                trade.id,
                trade.active_pt_price,
                trade.active_stop_price,
                trade.active_stop_pct,
            )
            return {
                "ok": True,
                "trade": trade.to_dict(),
                "active_pt_price": trade.active_pt_price,
                "active_stop_price": trade.active_stop_price,
                "active_stop_pct": trade.active_stop_pct,
            }

    def get_status(self) -> Dict[str, Any]:
        """Return full current dashboard status with today's vs historical separation in CST."""
        try:
            from meic0dte.app.utilities import central_now
            today_cst = central_now().strftime("%Y-%m-%d")
        except Exception:
            today_cst = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            active_list = [t.to_dict() for t in self.active_trades.values()]
            all_closed_list = [t.to_dict() for t in reversed(self.closed_trades)]
            all_signals_list = [s.to_dict() for s in reversed(self.signals)]

            # Today's vs Historical separation (in Central Time)
            today_signals = [s for s in all_signals_list if s.get("date_cst") == today_cst]
            historical_signals = [s for s in all_signals_list if s.get("date_cst") != today_cst]

            today_closed = [
                t for t in all_closed_list
                if t.get("date_cst") == today_cst or (t.get("closed_at_cst") and t["closed_at_cst"].startswith(today_cst))
            ]
            historical_closed = [
                t for t in all_closed_list
                if not (t.get("date_cst") == today_cst or (t.get("closed_at_cst") and t["closed_at_cst"].startswith(today_cst)))
            ]

            pending_decisions = [
                s.to_dict() for s in self.signals if s.status in ("CHASE_EXCEEDED", "FLOOR_EXCEEDED")
            ]

            today_realized = sum(t.get("realized_pnl_usd", 0.0) for t in today_closed) + sum(
                t.realized_pnl_usd for t in self.active_trades.values()
            )
            total_realized = sum(t.realized_pnl_usd for t in self.closed_trades) + sum(
                t.realized_pnl_usd for t in self.active_trades.values()
            )
            total_unrealized = sum(t.unrealized_pnl_usd for t in self.active_trades.values())

        is_broker_live = (
            self.broker is not None
            and not getattr(self.broker, "_is_journal", False)
            and not getattr(self.broker, "_is_paper", False)
        )
        broker_name = type(self.broker).__name__ if self.broker else "None"

        return {
            "market_open": is_market_open(),
            "config": self.config_mgr.to_dict(),
            "today_date_cst": today_cst,
            "broker_info": {
                "paper_mode": self.config_mgr.paper_mode,
                "broker_name": broker_name,
                "is_live": is_broker_live,
                "is_journal": bool(getattr(self.broker, "_is_journal", False)),
            },
            "active_trades": active_list,
            "today_signals": today_signals,
            "historical_signals": historical_signals,
            "today_closed_trades": today_closed,
            "historical_closed_trades": historical_closed,
            # Today's trades only for the primary view:
            "signals": today_signals,
            "all_signals": all_signals_list[:100],
            "closed_trades": today_closed,
            "all_closed_trades": all_closed_list[:100],
            "pending_decisions": pending_decisions,
            "stats": {
                "active_trades_count": len(active_list),
                "today_signals_count": len(today_signals),
                "historical_signals_count": len(historical_signals),
                "closed_trades_count": len(today_closed),
                "historical_closed_trades_count": len(historical_closed),
                "signals_count": len(today_signals),
                "total_signals_count": len(self.signals),
                "pending_decisions_count": len(pending_decisions),
                "today_realized_pnl_usd": round(today_realized, 2),
                "total_realized_pnl_usd": round(total_realized, 2),
                "total_unrealized_pnl_usd": round(total_unrealized, 2),
            },
        }

    def clear_history(self, scope: str = "all") -> None:
        """Clear closed trades and signals from in-memory and disk.
        If scope == 'historical', clears only entries from prior days (keeps today's trades).
        If scope == 'all', clears all closed trades and signals.
        """
        try:
            from meic0dte.app.utilities import central_now
            today_cst = central_now().strftime("%Y-%m-%d")
        except Exception:
            today_cst = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            if scope == "historical":
                self.closed_trades = [
                    t for t in self.closed_trades
                    if t.created_at and (t.to_dict().get("date_cst") == today_cst or (t.to_dict().get("closed_at_cst") and t.to_dict()["closed_at_cst"].startswith(today_cst)))
                ]
                self.signals = [
                    s for s in self.signals
                    if s.received_at and s.to_dict().get("date_cst") == today_cst
                ]
            else:
                self.closed_trades.clear()
                self.signals.clear()

            try:
                with open(self._signals_journal_path, "w", encoding="utf-8") as f:
                    for s in self.signals:
                        f.write(json.dumps(s.to_dict()) + "\n")
                with open(self._trades_journal_path, "w", encoding="utf-8") as f:
                    for t in self.active_trades.values():
                        f.write(json.dumps(t.to_dict()) + "\n")
                    for t in self.closed_trades:
                        f.write(json.dumps(t.to_dict()) + "\n")
            except Exception as exc:
                log.warning("Failed to rewrite history files: %s", exc)

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full details of a specific trade by ID (active or closed)."""
        with self._lock:
            trade = self.active_trades.get(trade_id)
            if trade:
                return trade.to_dict()
            for ct in reversed(self.closed_trades):
                if ct.id == trade_id:
                    return ct.to_dict()

        # Check journal on disk as fallback
        if os.path.exists(self._trades_journal_path):
            try:
                last_match = None
                with open(self._trades_journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            if data.get("id") == trade_id:
                                last_match = data
                        except Exception:
                            pass
                if last_match:
                    return CloneTrade.from_dict(last_match).to_dict()
            except Exception as exc:
                log.warning("Error reading trade %s from journal: %s", trade_id, exc)
        return None


_GLOBAL_ENGINE: Optional[CloneTradeEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_engine(broker=None) -> CloneTradeEngine:
    global _GLOBAL_ENGINE
    with _ENGINE_LOCK:
        if _GLOBAL_ENGINE is None:
            _GLOBAL_ENGINE = CloneTradeEngine(broker=broker)
        elif broker and not _GLOBAL_ENGINE.broker:
            _GLOBAL_ENGINE.broker = broker
            _GLOBAL_ENGINE.quote_service.broker = broker
        return _GLOBAL_ENGINE
