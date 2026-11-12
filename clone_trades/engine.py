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
from clone_trades.flow_finder import find_flow_alternative_option
from clone_trades.models import (
    CloneSignal,
    CloneTrade,
    ClosedLeg,
    LadderTier,
    OptionSpec,
    classify_sub_strategy,
    distribute_contracts_across_tiers,
    extract_called_by,
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

        self._lock = threading.RLock()

        self.active_trades: Dict[str, CloneTrade] = {}
        self.closed_trades: List[CloneTrade] = []
        self.signals: List[CloneSignal] = []

        self._trades_journal_path = os.path.join(self.config_mgr.data_dir, "trades.jsonl")
        self._signals_journal_path = os.path.join(self.config_mgr.data_dir, "signals.jsonl")

        self._load_state()
        self._signals_dirty = False
        self._trades_dirty = False
        self._last_flush_ts = 0.0

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
                            if trade.id in trades_by_id:
                                prev = trades_by_id[trade.id]
                                if getattr(prev, "highest_price", 0.0) > getattr(trade, "highest_price", 0.0):
                                    trade.highest_price = prev.highest_price
                                    trade.highest_profit_pct = getattr(prev, "highest_profit_pct", 0.0)
                                if getattr(prev, "lowest_price", 0.0) > 0 and (not hasattr(trade, "lowest_price") or trade.lowest_price <= 0 or prev.lowest_price < trade.lowest_price):
                                    trade.lowest_price = prev.lowest_price
                                if getattr(prev, "realized_pnl_usd", 0.0) != 0.0 and trade.realized_pnl_usd == 0.0:
                                    trade.realized_pnl_usd = prev.realized_pnl_usd
                            if trade.realized_pnl_usd == 0.0 and trade.closed_legs:
                                sum_legs = sum(getattr(l, "pnl_usd", 0.0) for l in trade.closed_legs)
                                if sum_legs != 0.0:
                                    trade.realized_pnl_usd = round(sum_legs, 2)
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
                            if sig.id in signals_by_id:
                                prev = signals_by_id[sig.id]
                                if prev.peak_price is not None:
                                    sig.peak_price = max(sig.peak_price or 0.0, prev.peak_price)
                                if prev.bottom_price is not None and prev.bottom_price > 0:
                                    if sig.bottom_price is None or sig.bottom_price <= 0:
                                        sig.bottom_price = prev.bottom_price
                                    else:
                                        sig.bottom_price = min(sig.bottom_price, prev.bottom_price)
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

    def flush_signals(self, force: bool = False) -> None:
        """Persist current signals snapshot to signals.jsonl atomically."""
        with self._lock:
            if not self._signals_dirty and not force:
                return
            tmp_path = self._signals_journal_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for s in self.signals:
                        f.write(json.dumps(s.to_dict()) + "\n")
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, self._signals_journal_path)
                self._signals_dirty = False
            except Exception as exc:
                log.warning("Failed to flush signals journal: %s", exc)

    def flush_trades(self, force: bool = False) -> None:
        """Persist current trades snapshot to trades.jsonl atomically."""
        with self._lock:
            if not self._trades_dirty and not force:
                return
            tmp_path = self._trades_journal_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for t in self.active_trades.values():
                        f.write(json.dumps(t.to_dict()) + "\n")
                    for t in self.closed_trades:
                        f.write(json.dumps(t.to_dict()) + "\n")
                if os.path.exists(tmp_path):
                    os.replace(tmp_path, self._trades_journal_path)
                self._trades_dirty = False
            except Exception as exc:
                log.warning("Failed to flush trades journal: %s", exc)

    def flush_all(self, force: bool = False) -> None:
        """Flush both signals and trades journals."""
        self.flush_signals(force=force)
        self.flush_trades(force=force)
        self._last_flush_ts = time.time()

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

        # 0. Extract caller and classify sub-strategy ("PC", "Genie", or "Others")
        called_by = extract_called_by(payload)
        sub_strat = classify_sub_strategy(called_by)
        sub_cfg = self.config_mgr.get_sub_strategy_config(sub_strat)

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
                called_by=called_by,
                sub_strategy=sub_strat,
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

        market_open = is_market_open()
        is_paper = bool(sub_strat == "Others" or sub_cfg.paper_mode or self.config_mgr.paper_mode)

        # Handle missing quote
        if market_px <= 0:
            if not market_open and not is_paper:
                reason = f"Options market is CLOSED (Mon-Fri 8:30 AM - 3:00 PM CT) and no live quote available for [{sub_strat}]. Auto-take paused."
                log.info("SIGNAL HELD (Market Closed, No Quote): %s %s [%s]", spec.underlying, spec.occ_symbol, sub_strat)
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
                    called_by=called_by,
                    sub_strategy=sub_strat,
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
            elif not is_paper:
                reason = f"Unable to fetch live quote for {spec.occ_symbol} to verify chase guard on [{sub_strat}]."
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
                    called_by=called_by,
                    sub_strategy=sub_strat,
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

        # Requirement 2: AI FlowAlgos companion budget strike ($0.70 - $1.50, target ~$1.00)
        alt_sig = None
        if sub_strat == "AI FlowAlgos" or str(called_by).strip().lower() in ("genie", "cowboy"):
            try:
                alt_res = find_flow_alternative_option(spec, self.quote_service, self.broker)
                if alt_res:
                    alt_spec = alt_res["spec"]
                    alt_px = float(alt_res["price"])
                    alt_sig_id = f"{sig_id}_alt"
                    alt_reason = (
                        f"AI FlowAlgos $0.70-$1.50 target strike ({alt_spec.strike:g} vs orig {spec.strike:g} @ ${alt_px:.2f}) "
                        f"for [{called_by}]."
                    )
                    alt_sig = CloneSignal(
                        id=alt_sig_id,
                        received_at=now_iso,
                        raw_payload={
                            "ticker": alt_spec.underlying,
                            "expiration": alt_spec.expiration,
                            "strike": alt_spec.strike,
                            "call_put": alt_spec.option_type,
                            "price": alt_px,
                            "calledBy": called_by,
                            "parent_signal_id": sig_id,
                            "is_flow_alt": True,
                        },
                        spec=alt_spec,
                        alert_price=alt_px,
                        market_price=alt_px,
                        premium_pct=0.0,
                        status="TRACKING",
                        status_reason=alt_reason,
                        called_by=called_by,
                        sub_strategy="AI FlowAlgos",
                        current_price=alt_px,
                        peak_price=alt_px,
                        bottom_price=alt_px,
                        last_quote_time=now_iso,
                        parent_signal_id=sig_id,
                        is_flow_alt=True,
                    )
                    with self._lock:
                        self.signals.append(alt_sig)
                        self._persist_signal(alt_sig)
            except Exception as exc:
                log.warning("Could not identify AI FlowAlgos alternative strike: %s", exc)

        # 3b. Check Tracking Mode: If sub-strategy is in tracking mode, track contract in real-time
        if getattr(sub_cfg, "execution_mode", "") == "tracking":
            reason = f"Tracking alert for [{sub_strat}] (Alert: ${alert_px:.2f}, Broker: ${market_px:.2f}). Ready for manual Buy / Get In."
            log.info("SIGNAL TRACKING: %s %s [%s] Alert: $%.2f, Broker: $%.2f", spec.underlying, spec.occ_symbol, sub_strat, alert_px, market_px)
            sig = CloneSignal(
                id=sig_id,
                received_at=now_iso,
                raw_payload=payload,
                spec=spec,
                alert_price=alert_px,
                market_price=market_px,
                premium_pct=premium_pct,
                status="TRACKING",
                status_reason=reason,
                called_by=called_by,
                sub_strategy=sub_strat,
                        current_price=market_px,
                peak_price=max(alert_px, market_px) if market_px > 0 else alert_px,
                bottom_price=min(alert_px, market_px) if market_px > 0 else alert_px,
                last_quote_time=now_iso,
                alt_signal_id=alt_sig.id if alt_sig else None,
            )

            with self._lock:
                self.signals.append(sig)
                self._persist_signal(sig)

            resp = {
                "ok": True,
                "status": "TRACKING",
                "signal": sig.to_dict(),
                "message": reason,
            }
            if alt_sig:
                resp["alt_signal"] = alt_sig.to_dict()
            return resp

        # 4. Check Chase Guard (#4) using sub-strategy max_premium_pct
        init_peak_px = max(alert_px, market_px) if market_px > 0 else alert_px
        init_bottom_px = min(alert_px, market_px) if market_px > 0 else alert_px
        max_premium = getattr(sub_cfg, "max_premium_pct", self.config_mgr.max_premium_pct)
        if premium_pct > max_premium:
            reason = (
                f"[{sub_strat}] Trading at {premium_pct:+.1f}% above alert price ${alert_px:.2f} "
                f"(exceeds max allowed chase of {max_premium:.1f}%)"
            )
            log.warning("CHASE EXCEEDED: %s %s [%s] -> %s", spec.underlying, spec.streamer_symbol, sub_strat, reason)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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
        floor_limit = getattr(sub_cfg, "floor_guard_pct", getattr(self.config_mgr, "floor_guard_pct", 75.0))
        if floor_limit > 0 and drop_pct > floor_limit:
            reason = (
                f"[{sub_strat}] Trading at {drop_pct:.1f}% below alert price ${alert_px:.2f} (market: ${market_px:.2f}) "
                f"(exceeds max allowed floor drop of {floor_limit:.1f}%)"
            )
            log.warning("FLOOR EXCEEDED: %s %s [%s] -> %s", spec.underlying, spec.streamer_symbol, sub_strat, reason)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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

        # 4c. Check Allowed Tickers Filter (Requirement 1.c)
        allowed_tickers = getattr(sub_cfg, "allowed_tickers", None)
        if allowed_tickers:
            norm_tickers = [t.strip().upper() for t in allowed_tickers if t.strip()]
            spec_und = spec.underlying.strip().upper()
            und_variants = {spec_und}
            if spec_und in ("SPX", "SPXW"):
                und_variants.update(["SPX", "SPXW"])
            if norm_tickers and not any(v in norm_tickers for v in und_variants):
                reason = (
                    f"[{sub_strat}] Ticker {spec.underlying} is not in allowed tickers list "
                    f"({', '.join(norm_tickers)}). Trade skipped."
                )
                log.info("SIGNAL SKIPPED (Ticker Not In Filter): %s", reason)
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
                    called_by=called_by,
                    sub_strategy=sub_strat,
                    current_price=market_px,
                    peak_price=init_peak_px,
                    bottom_price=init_bottom_px,
                    last_quote_time=now_iso,
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

        # 4d. Check Max Alert Price Cap (Requirement 1.f: Alert price must be < $1.50)
        max_alert = getattr(sub_cfg, "max_alert_price", 0.0)
        if max_alert > 0 and alert_px >= max_alert:
            reason = (
                f"[{sub_strat}] Alert price ${alert_px:.2f} is not less than max allowed "
                f"${max_alert:.2f}. Trade skipped."
            )
            log.info("SIGNAL SKIPPED (Alert Price >= Max): %s", reason)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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

        # 4e. Duplicate / Opposing Trade Guard (Requirement 1.d for Human Alerters)
        if sub_strat in ("TG", "PC", "PC2", "Human Alerters"):
            existing_trade = None
            with self._lock:
                for t in self.active_trades.values():
                    if t.remaining_quantity > 0 and t.spec and t.spec.underlying.strip().upper() == spec.underlying.strip().upper():
                        if t.sub_strategy == sub_strat or (sub_strat == "Human Alerters" and t.sub_strategy in ("Human Alerters", "PC", "TG", "PC2")):
                            existing_trade = t
                            break

            if existing_trade:
                orig_entry = float(existing_trade.entry_price or 0.0)
                curr_px = float(existing_trade.current_price or orig_entry)
                val_remaining_pct = (curr_px / orig_entry * 100.0) if orig_entry > 0 else 100.0
                less_than_50 = (val_remaining_pct < 50.0)
                orig_side = existing_trade.spec.option_type.upper()
                new_side = spec.option_type.upper()
                is_opposite = (orig_side != new_side)

                if not (less_than_50 and is_opposite):
                    if not is_opposite:
                        reason = (
                            f"[{sub_strat}] Duplicate {new_side} trade on {spec.underlying} skipped: "
                            f"already taken by {existing_trade.called_by or 'prior trader'} "
                            f"({existing_trade.spec.streamer_symbol} is still open)."
                        )
                    else:
                        reason = (
                            f"[{sub_strat}] Opposing {new_side} trade on {spec.underlying} blocked: "
                            f"prior {orig_side} trade taken by {existing_trade.called_by or 'prior trader'} "
                            f"has {val_remaining_pct:.1f}% value remaining (must drop below 50% to take opposite side)."
                        )
                    log.info("SIGNAL SKIPPED (Prior Trade Active): %s", reason)
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
                        called_by=called_by,
                        sub_strategy=sub_strat,
                        current_price=market_px,
                        peak_price=init_peak_px,
                        bottom_price=init_bottom_px,
                        last_quote_time=now_iso,
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
                else:
                    log.info(
                        "[%s] Opposing %s trade allowed on %s: prior %s trade by %s has only %.1f%% value remaining (<50%%).",
                        sub_strat, new_side, spec.underlying, orig_side, existing_trade.called_by, val_remaining_pct
                    )

        # 5. Check Sub-Strategy & Daily Auto-Take Flags
        if not getattr(sub_cfg, "enabled", True) or not getattr(sub_cfg, "auto_take_trades", True) or not self.config_mgr.auto_take_trades:
            reason = f"Auto-Take is OFF for sub-strategy [{sub_strat}] (calledBy: {called_by}). Trade skipped on brokerage."
            log.info("SIGNAL SKIPPED (Auto-Take OFF): %s %s [%s]", spec.underlying, spec.streamer_symbol, sub_strat)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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
        if not market_open and not is_paper and not getattr(self.config_mgr, "allow_off_hours_orders", False):
            reason = f"Options market is CLOSED (Mon-Fri 8:30 AM - 3:00 PM CT). Live orders paused for [{sub_strat}]."
            log.info("SIGNAL BLOCKED (Market Closed): %s %s [%s]", spec.underlying, spec.occ_symbol, sub_strat)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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

        # 7. Calculate contract quantity (#5) using sub-strategy allocation (either $ budget or # contracts)
        entry_mode = getattr(sub_cfg, "entry_limit_mode", "dollar")
        if entry_mode == "contracts":
            qty = max(1, int(getattr(sub_cfg, "allocation_contracts", 1)))
        else:
            contract_cost = max(1.0, market_px * 100.0)
            alloc_amt = getattr(sub_cfg, "allocation_amount", self.config_mgr.allocation_amount)
            calc_qty = int(alloc_amt // contract_cost)
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
                sub_strategy=sub_strat,
                called_by=called_by,
                paper_mode=is_paper,
                ladder_tiers=sub_cfg.ladder_tiers if sub_cfg and sub_cfg.ladder_tiers else self.config_mgr.ladder_tiers,
            )
        except Exception as exc:
            log.error("Broker order execution error [%s]: %s", sub_strat, exc)
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
                called_by=called_by,
                sub_strategy=sub_strat,
                current_price=market_px,
                peak_price=init_peak_px,
                bottom_price=init_bottom_px,
                last_quote_time=now_iso,
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
            f"Queued {qty} contracts on brokerage @ ${market_px:.2f} (order #{trade.order_id})"
            if trade.status == "WORKING"
            else f"Filled {qty} contracts @ ${market_px:.2f} ({'Paper' if is_paper else 'Live'} [{sub_strat}])"
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
            called_by=called_by,
            sub_strategy=sub_strat,
            current_price=trade.current_price,
            peak_price=max(init_peak_px, trade.highest_price),
            bottom_price=min(init_bottom_px, trade.lowest_price) if trade.lowest_price > 0 else init_bottom_px,
            last_quote_time=now_iso,
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
        sub_strategy: str = "Others",
        called_by: str = "",
        paper_mode: Optional[bool] = None,
        ladder_tiers: Optional[List[LadderTier]] = None,
        is_roll_runner: bool = False,
        roll_count: int = 0,
        parent_trade_id: Optional[str] = None,
        original_entry_price: float = 0.0,
        roll_runner_every_leg: Optional[bool] = None,
    ) -> CloneTrade:
        """Place buy order on broker (or simulate) and setup unbalanced OCO ladder."""
        cfg = self.config_mgr
        sub_cfg = cfg.get_sub_strategy_config(sub_strategy)

        if paper_mode is None:
            paper_mode = getattr(sub_cfg, "paper_mode", cfg.paper_mode)
            if sub_strategy == "Others":
                paper_mode = True
        if ladder_tiers is None:
            ladder_tiers = list(sub_cfg.ladder_tiers) if sub_cfg and sub_cfg.ladder_tiers else list(cfg.ladder_tiers)

        order_id = f"ord_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        trade_status = "OPEN"
        trade_status_reason = f"Active trade ({'Paper' if paper_mode else 'Live'} [{sub_strategy}])"

        # Attempt broker execution if live
        if not paper_mode:
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
            res_fq = int(getattr(res, "filled_quantity", 0) or 0)
            if getattr(res, "filled_price", None) and float(res.filled_price) > 0 and (res_fq > 0 or broker_status == "filled"):
                fill_price = float(res.filled_price)
                broker_status = "filled"
            elif order_id and hasattr(self.broker, "get_order_status"):
                try:
                    time.sleep(0.3)
                    ost = self.broker.get_order_status(order_id)
                    ost_st = str(getattr(ost, "status", broker_status)).lower() if ost else ""
                    ost_fq = int(getattr(ost, "filled_quantity", 0) or 0) if ost else 0
                    if ost and getattr(ost, "filled_price", None) and float(ost.filled_price) > 0 and (ost_fq > 0 or ost_st == "filled"):
                        fill_price = float(ost.filled_price)
                        broker_status = "filled"
                    elif ost_st:
                        broker_status = ost_st
                except Exception as exc:
                    log.debug("Could not query order status immediately: %s", exc)

            if broker_status != "filled":
                trade_status = "WORKING"
                trade_status_reason = f"Order #{order_id} working on brokerage @ ${fill_price:.2f}"
            else:
                trade_status = "OPEN"
                trade_status_reason = f"Filled {qty} contracts @ ${fill_price:.2f} (Live [{sub_strategy}])"

        contract_cost = fill_price * 100.0
        trade_id = f"clone_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        now_iso = utc_now_iso()

        # Unbalanced OCO Ladder initialization using sub-strategy's ladder tiers
        num_tiers = len(ladder_tiers)
        tier_pcts = [t.exit_pct for t in ladder_tiers]
        contracts_per_tier = distribute_contracts_across_tiers(qty, num_tiers, tier_pcts=tier_pcts)
        tier_distribution = []
        for i, t in enumerate(ladder_tiers):
            eff_exit_pct = t.exit_pct if t.exit_pct > 0 else round(100.0 / max(1, num_tiers), 1)
            tier_distribution.append({
                "tier_id": t.tier_id,
                "target_profit_pct": t.target_profit_pct,
                "stop_loss_pct": t.stop_loss_pct,
                "exit_pct": eff_exit_pct,
                "contracts": contracts_per_tier[i] if i < len(contracts_per_tier) else 0,
            })

        with self._lock:
            _matched_sig = next((s for s in self.signals if s.id == sig_id), None)
            _is_alt = bool(getattr(_matched_sig, "is_flow_alt", False) or str(sig_id).endswith('_alt'))
            _parent_id = getattr(_matched_sig, "parent_signal_id", None)

        # Initial Stop Loss applies to ALL contracts (defined by Tier 1 of ladder)
        if ladder_tiers and len(ladder_tiers) > 0:
            initial_sl_pct = ladder_tiers[0].stop_loss_pct
        else:
            initial_sl_pct = sub_cfg.initial_stop_loss_pct if sub_cfg else cfg.initial_stop_loss_pct

        if initial_sl_pct is None or initial_sl_pct <= -100.0:
            active_stop_price = 0.0
            initial_sl_pct = -100.0
        else:
            active_stop_price = round(max(0.01, fill_price * (1.0 + initial_sl_pct / 100.0)), 2)

        # Next Profit Target for Tier 1
        tier_0 = ladder_tiers[0] if ladder_tiers else LadderTier(1, 25.0, -50.0)
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
            lowest_price=fill_price,
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
            called_by=called_by,
            sub_strategy=sub_strategy,
            paper_mode=paper_mode,
            ladder_tiers=[t.to_dict() for t in ladder_tiers],
            entry_placed_time=time.time(),
            last_chase_time=time.time(),
            chase_count=0,
            max_chase_attempts=3,
            is_flow_alt=_is_alt,
            parent_signal_id=_parent_id,
            is_roll_runner=is_roll_runner,
            roll_count=roll_count,
            parent_trade_id=parent_trade_id,
            original_entry_price=original_entry_price if original_entry_price > 0.0 else fill_price,
            roll_runner_enabled=is_roll_runner,
            roll_runner_qty=qty,
            roll_runner_every_leg=bool(getattr(sub_cfg, 'roll_runner_every_leg', False)) if roll_runner_every_leg is None else roll_runner_every_leg,
        )

        with self._lock:
            self.active_trades[trade.id] = trade
            self._persist_trade(trade)

        log.info(
            "OPENED CLONE TRADE %s [%s, calledBy=%s, paper=%s]: %s %d contracts @ $%.2f (status=%s) | PT1=$%.2f SL=$%.2f",
            trade.id,
            sub_strategy,
            called_by,
            paper_mode,
            spec.streamer_symbol,
            qty,
            fill_price,
            trade.status,
            active_pt_price,
            active_stop_price,
        )
        if trade.status == "OPEN":
            self._place_ladder_oco_orders(trade)
        return trade

    def _monitor_ladder_oco_orders(self, trade: CloneTrade) -> bool:
        """Monitor working ladder OCO orders on broker and sync fills / adjust trailing stops (Req 3.3)."""
        is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode
        if is_paper or not self.broker or not hasattr(self.broker, "get_order_status"):
            return False

        tiers = trade.tier_distribution or []
        working_tiers = [
            td for td in tiers
            if td.get("status") == "working"
            and td.get("oco_order_id")
            and not str(td.get("oco_order_id")).startswith("oco_paper_")
        ]
        if not working_tiers:
            return False

        live_orders = None
        if hasattr(self.broker, "get_live_orders_cached"):
            try:
                live_orders = self.broker.get_live_orders_cached()
            except Exception:
                pass

        now_iso = utc_now_iso()
        pt_hit_occurred = False

        for td in working_tiers:
            oco_id = td.get("oco_order_id")
            try:
                res = self.broker.get_order_status(str(oco_id), live_orders=live_orders)
                if not res or str(getattr(res, "status", "")).lower() != "filled":
                    continue

                filled_px = float(getattr(res, "filled_price", 0.0) or 0.0)
                filled_qty = int(getattr(res, "filled_quantity", 0) or td.get("contracts", 1))

                is_stop_fill = False
                raw_matches = getattr(res, "raw", [])
                if isinstance(raw_matches, list):
                    for child in raw_matches:
                        if getattr(child, "status", None) and str(child.status).lower() == "filled":
                            if str(getattr(child, "order_type", "")).lower() in ("stop limit", "stop_limit", "stop"):
                                is_stop_fill = True
                                break

                pt_px = float(td.get("pt_price", 0.0) or 0.0)
                sl_px = float(td.get("stop_price", 0.0) or 0.0)
                if not is_stop_fill and filled_px > 0:
                    if sl_px > 0 and filled_px <= sl_px * 1.05:
                        is_stop_fill = True
                    elif pt_px > 0 and filled_px < pt_px * 0.98:
                        is_stop_fill = True
                    elif sl_px > 0 and pt_px > 0 and abs(filled_px - sl_px) < abs(filled_px - pt_px):
                        is_stop_fill = True

                td["status"] = "filled_sl" if is_stop_fill else "filled_pt"
                td["remaining_contracts"] = 0
                if filled_px > 0:
                    td["exit_price"] = filled_px

                profit_pct = round(((filled_px - trade.entry_price) / trade.entry_price) * 100.0, 2) if trade.entry_price > 0 else 0.0
                pnl_usd = round((filled_px - trade.entry_price) * 100.0 * filled_qty, 2)

                action_desc = "Stop Loss" if is_stop_fill else f"Tier {td.get('tier_id', 1)} PT"
                leg = ClosedLeg(
                    tier_id=td.get("tier_id", 0) if not is_stop_fill else 0,
                    quantity=filled_qty,
                    exit_price=filled_px,
                    exit_time=now_iso,
                    profit_pct=profit_pct,
                    pnl_usd=pnl_usd,
                    reason=f"{action_desc} Hit via broker OCO ({filled_qty} ctrs) @ ${filled_px:.2f} ({profit_pct:+.1f}%)",
                )
                with self._lock:
                    trade.closed_legs.append(leg)
                    trade.realized_pnl_usd = round(trade.realized_pnl_usd + pnl_usd, 2)
                    self._trades_dirty = True
                    rem_working = sum(
                        t.get("remaining_contracts", t.get("contracts", 0))
                        for t in trade.tier_distribution
                        if t.get("status") == "working"
                    ) if trade.tier_distribution else max(0, trade.remaining_quantity - filled_qty)
                    trade.remaining_quantity = rem_working
                    if trade.remaining_quantity <= 0:
                        trade.status = "CLOSED"
                        trade.status_reason = f"{action_desc} Hit @ ${filled_px:.2f} ({profit_pct:+.1f}%) [Broker OCO]"
                        trade.closed_at = now_iso
                        trade.unrealized_pnl_usd = 0.0
                        self.active_trades.pop(trade.id, None)
                        self.closed_trades.append(trade)
                        self._persist_trade(trade)
                        log.info("TRADE CLOSED via broker OCO: %s (%s @ $%.2f)", trade.id, action_desc, filled_px)
                        all_td = trade.tier_distribution or []
                        final_td_id = all_td[-1].get("tier_id") if all_td else None
                        cur_td_id = td.get("tier_id")
                        is_final_rung = (not all_td) or (cur_td_id is not None and cur_td_id == final_td_id)
                        sub_cfg = self.config_mgr.get_sub_strategy_config(getattr(trade, "sub_strategy", "TG"))
                        roll_every_leg = bool(getattr(sub_cfg, "roll_runner_every_leg", False) or getattr(trade, "roll_runner_every_leg", False))
                        if not is_stop_fill and (is_final_rung or roll_every_leg):
                            try:
                                self._execute_runner_roll(trade, filled_px)
                            except Exception as roll_exc:
                                log.error("Failed to execute runner roll on OCO close of %s: %s", trade.id, roll_exc)
                        elif not is_stop_fill:
                            log.info(
                                "RUNNER ROLL SKIPPED for %s: Broker OCO closed at Tier %s before reaching final ladder rung (Tier %s)",
                                trade.id, cur_td_id, final_td_id
                            )
                        return
                    else:
                        trade.status_reason = f"{action_desc} Hit @ ${filled_px:.2f} ({trade.remaining_quantity} contracts remaining)"
                        if not is_stop_fill:
                            pt_hit_occurred = True
                            trade.current_tier_index += 1
                            if trade.current_tier_index < len(tiers):
                                next_pt = tiers[trade.current_tier_index].get("pt_price")
                                if next_pt:
                                    trade.active_pt_price = next_pt
                            sub_cfg = self.config_mgr.get_sub_strategy_config(getattr(trade, "sub_strategy", "TG"))
                            if getattr(sub_cfg, "roll_runner_every_leg", False) or getattr(trade, "roll_runner_every_leg", False):
                                try:
                                    self._execute_runner_roll(trade, filled_px)
                                except Exception as roll_exc:
                                    log.error("Failed to execute runner roll on intermediate OCO PT of %s: %s", trade.id, roll_exc)
                        self._persist_trade(trade)

            except Exception as exc:
                log.debug("Error monitoring OCO order %s: %s", oco_id, exc)

        if pt_hit_occurred and trade.status == "OPEN" and trade.remaining_quantity > 0:
            self._update_remaining_oco_stops(trade)
        return pt_hit_occurred

    def _update_remaining_oco_stops(self, trade: CloneTrade) -> None:
        """Update stops on remaining working OCO rungs after a PT fill (Requirement 3.3).
        Ratchets stop loss dynamically based on the configured stop_loss_pct for the next rung.
        """
        tiers = trade.tier_distribution or []
        filled_pt_count = len([td for td in tiers if td.get("status") == "filled_pt"])
        if filled_pt_count <= 0:
            return

        # Determine next rung being aimed for to get its configured stop_loss_pct
        next_tier_idx = min(filled_pt_count, len(tiers) - 1)
        next_td = tiers[next_tier_idx]
        target_sl_pct = next_td.get("stop_loss_pct")
        if target_sl_pct is None and getattr(trade, "ladder_tiers", None) and next_tier_idx < len(trade.ladder_tiers):
            target_sl_pct = trade.ladder_tiers[next_tier_idx].get("stop_loss_pct")

        if target_sl_pct is not None and target_sl_pct > -100.0:
            new_sl_price = round(max(0.01, trade.entry_price * (1.0 + target_sl_pct / 100.0)), 2)
        else:
            new_sl_price = 0.0

        trade.active_stop_price = new_sl_price
        trade.active_stop_pct = target_sl_pct if target_sl_pct is not None else -100.0
        log.info(
            "UPDATING OCO STOPS for %s: After %d PT fills, ratcheting stop to $%.2f (%s) for rung %s",
            trade.id,
            filled_pt_count,
            new_sl_price,
            f"{target_sl_pct:+.1f}%" if target_sl_pct is not None else "No Stop",
            next_td.get("tier_id", next_tier_idx + 1),
        )

        for td in tiers:
            if td.get("status") == "working" and td.get("contracts", 0) > 0:
                old_oco_id = td.get("oco_order_id")
                ctrs = td.get("remaining_contracts") or td.get("contracts", 0)
                pt_px = td.get("pt_price", round(trade.entry_price * 1.5, 2))
                td["stop_price"] = new_sl_price

                # Replace OCO order on broker
                if old_oco_id and not str(old_oco_id).startswith("oco_paper_") and self.broker:
                    try:
                        self.broker.cancel_order(str(old_oco_id))
                        time.sleep(0.3)
                        if new_sl_price > 0:
                            nres = self.broker.place_oco_order(
                                trade.spec.occ_symbol,
                                ctrs,
                                profit_price=pt_px,
                                stop_price=new_sl_price,
                                limit_price=new_sl_price,
                                action="SELL_TO_CLOSE",
                            )
                        else:
                            nres = self.broker.place_limit_order(
                                "SELL_TO_CLOSE",
                                trade.spec.occ_symbol,
                                ctrs,
                                pt_px,
                            )
                        if nres and getattr(nres, "order_id", None):
                            td["oco_order_id"] = str(nres.order_id)
                            log.info("REPLACED OCO ORDER for %s rung %s: new order=%s @ SL $%.2f", trade.id, td.get("tier_id"), nres.order_id, new_sl_price)
                    except Exception as exc:
                        log.error("Failed to replace OCO order on broker for %s: %s", trade.id, exc)

        self._persist_trade(trade)

    def _execute_runner_roll(self, parent_trade: CloneTrade, trigger_fill_price: float = 0.0) -> Optional[CloneTrade]:
        """Requirement: Last-Rung Roll (Runner Roll).
        When the final rung of the ladder hits its PT, check if runner roll is enabled.
        If enabled, filter a further OTM contract from option chain in same direction trading
        around the same cost as entry (+-25%, closer one better) and buy at roll_runner_qty.
        Infinite roll: target matches last rung's PT % with no stop; if PT hits, it rolls again.
        """
        try:
            sub_strat = getattr(parent_trade, "sub_strategy", "TG")
            sub_cfg = self.config_mgr.get_sub_strategy_config(sub_strat)

            is_enabled = bool(
                getattr(sub_cfg, "roll_runner_enabled", False)
                or getattr(parent_trade, "is_roll_runner", False)
                or getattr(parent_trade, "roll_runner_enabled", False)
            )
            if not is_enabled:
                return None

            # CRITICAL GUARD: Runner roll MUST NEVER trigger on Stop Loss hits! Roll logic only triggers on PT.
            last_leg = parent_trade.closed_legs[-1] if parent_trade.closed_legs else None
            if last_leg:
                is_sl_leg = (
                    getattr(last_leg, "tier_id", 0) == 0
                    or "stop" in str(getattr(last_leg, "reason", "")).lower()
                    or "sl " in str(getattr(last_leg, "reason", "")).lower()
                    or "sl hit" in str(getattr(last_leg, "reason", "")).lower()
                )
                if is_sl_leg:
                    log.info(
                        "RUNNER ROLL BLOCKED for %s: Exit was a STOP LOSS (%s @ $%.2f). Roll logic only triggers on PT.",
                        parent_trade.id, getattr(last_leg, "reason", ""), getattr(last_leg, "exit_price", 0.0)
                    )
                    return None

            status_reason_lower = str(getattr(parent_trade, "status_reason", "")).lower()
            if "stop loss" in status_reason_lower or "stopped out" in status_reason_lower or "sl breach" in status_reason_lower:
                log.info(
                    "RUNNER ROLL BLOCKED for %s: Trade status indicates STOP LOSS (%s). Roll logic only triggers on PT.",
                    parent_trade.id, getattr(parent_trade, "status_reason", "")
                )
                return None

            roll_every_leg = bool(
                getattr(sub_cfg, "roll_runner_every_leg", False)
                or getattr(parent_trade, "roll_runner_every_leg", False)
            )

            # Guard: Unless roll_runner_every_leg is enabled, roller only works when the LAST rung is completed.
            # E.g. If an expensive option only bought 1 contract and exited on Tier 1 of a 3-tier ladder,
            # it must NOT roll because the final ladder rung was never reached.
            if not roll_every_leg and parent_trade.ladder_tiers and len(parent_trade.ladder_tiers) > 1 and not getattr(parent_trade, "is_roll_runner", False):
                num_tiers = len(parent_trade.ladder_tiers)
                last_leg = parent_trade.closed_legs[-1] if parent_trade.closed_legs else None
                last_tier_id = getattr(last_leg, "tier_id", 0) if last_leg else 0
                if last_tier_id > 0 and last_tier_id < num_tiers:
                    log.info(
                        "RUNNER ROLL BLOCKED for %s: Closed at Tier %d, but ladder has %d tiers (mode=last_leg_only). "
                        "Runner roll only executes when the final rung is completed.",
                        parent_trade.id, last_tier_id, num_tiers
                    )
                    return None

            target_cost = float(
                getattr(parent_trade, "original_entry_price", 0.0)
                or parent_trade.entry_price
                or 0.50
            )
            tolerance_pct = float(getattr(sub_cfg, "roll_runner_tolerance_pct", 25.0) or 25.0)
            roll_qty = max(1, int(getattr(sub_cfg, "roll_runner_qty", 1) or 1))

            from clone_trades.runner_roll import find_runner_roll_option
            cand_res = find_runner_roll_option(
                spec=parent_trade.spec,
                target_price=target_cost,
                tolerance_pct=tolerance_pct,
                quote_service=self.quote_service,
                broker=self.broker,
            )
            if not cand_res:
                log.warning("Runner roll could not find candidate option for %s", parent_trade.id)
                return None

            cand_spec = cand_res["spec"]
            cand_price = float(cand_res["price"])

            last_pt_pct = 100.0
            if parent_trade.ladder_tiers:
                last_t = parent_trade.ladder_tiers[-1]
                last_pt_pct = float(last_t.get("target_profit_pct", 100.0) if isinstance(last_t, dict) else getattr(last_t, "target_profit_pct", 100.0))
            elif sub_cfg and sub_cfg.ladder_tiers:
                last_t = sub_cfg.ladder_tiers[-1]
                last_pt_pct = float(last_t.target_profit_pct)

            runner_tier = LadderTier(
                tier_id=1,
                target_profit_pct=last_pt_pct,
                stop_loss_pct=-100.0,
                exit_pct=100.0,
                description=f"Runner Roll Step (+{last_pt_pct:g}% PT / No Stop)",
            )

            leg_num = len(parent_trade.closed_legs) if parent_trade.closed_legs else 1
            roll_sig_id = f"roll_{parent_trade.id}_{getattr(parent_trade, 'roll_count', 0) + 1}_L{leg_num}_{uuid.uuid4().hex[:4]}"
            new_trade = self._execute_and_open_trade(
                spec=cand_spec,
                qty=roll_qty,
                fill_price=cand_price,
                alert_price=cand_price,
                premium_pct=0.0,
                sig_id=roll_sig_id,
                sub_strategy=sub_strat,
                called_by=parent_trade.called_by,
                paper_mode=parent_trade.paper_mode,
                ladder_tiers=[runner_tier],
                is_roll_runner=True,
                roll_count=getattr(parent_trade, "roll_count", 0) + 1,
                parent_trade_id=parent_trade.id,
                original_entry_price=target_cost,
                roll_runner_every_leg=roll_every_leg,
            )

            with self._lock:
                new_trade.is_roll_runner = True
                new_trade.roll_count = getattr(parent_trade, "roll_count", 0) + 1
                new_trade.parent_trade_id = parent_trade.id
                new_trade.original_entry_price = target_cost
                new_trade.roll_runner_enabled = True
                new_trade.roll_runner_qty = roll_qty
                new_trade.roll_runner_every_leg = roll_every_leg
                new_trade.status_reason = (
                    f"Runner Roll #{new_trade.roll_count} from {parent_trade.spec.streamer_symbol} "
                    f"({new_trade.remaining_quantity} ctrs @ ${cand_price:.2f}, +{last_pt_pct:g}% PT, No Stop)"
                )
                self._persist_trade(new_trade)

                now_iso = utc_now_iso()
                roll_sig = CloneSignal(
                    id=roll_sig_id,
                    received_at=now_iso,
                    raw_payload={
                        "ticker": cand_spec.underlying,
                        "expiration": cand_spec.expiration,
                        "strike": cand_spec.strike,
                        "call_put": cand_spec.option_type,
                        "calledBy": parent_trade.called_by,
                    },
                    spec=cand_spec,
                    alert_price=cand_price,
                    market_price=cand_price,
                    premium_pct=0.0,
                    status="TAKEN",
                    status_reason=f"Runner Roll #{new_trade.roll_count} triggered from {parent_trade.spec.streamer_symbol} PT completion",
                    trade_id=new_trade.id,
                    called_by=parent_trade.called_by,
                    sub_strategy=sub_strat,
                    current_price=cand_price,
                    peak_price=cand_price,
                    bottom_price=cand_price,
                    last_quote_time=now_iso,
                )
                self.signals.append(roll_sig)
                self._persist_signal(roll_sig)

            log.info(
                "RUNNER ROLL EXECUTED: Rolled parent %s -> new trade %s (%s strike %g @ $%.2f, qty=%d, PT=+%.1f%%, No Stop)",
                parent_trade.id,
                new_trade.id,
                cand_spec.streamer_symbol,
                cand_spec.strike,
                cand_price,
                roll_qty,
                last_pt_pct,
            )
            return new_trade
        except Exception as exc:
            log.error("Failed to execute runner roll for trade %s: %s", getattr(parent_trade, "id", "unknown"), exc, exc_info=True)
            return None

    def _place_ladder_oco_orders(self, trade: CloneTrade) -> None:
        """Requirement 3.1 & 3.2: Send all ladder close orders immediately to broker as OCO and monitor them."""
        if getattr(trade, "_ladder_oco_placed", False):
            return
        trade._ladder_oco_placed = True

        tiers = trade.tier_distribution or []
        if not tiers:
            return

        initial_sl_pct = trade.active_stop_pct
        if tiers and tiers[0].get("stop_loss_pct") is not None:
            initial_sl_pct = tiers[0]["stop_loss_pct"]

        if initial_sl_pct is not None and initial_sl_pct > -100.0:
            initial_sl_price = round(max(0.01, trade.entry_price * (1.0 + initial_sl_pct / 100.0)), 2)
        else:
            initial_sl_price = 0.0

        is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode

        for idx, td in enumerate(tiers):
            contracts = td.get("contracts", 0)
            if contracts <= 0:
                continue

            pt_pct = td.get("target_profit_pct", 25.0)
            pt_price = round(trade.entry_price * (1.0 + pt_pct / 100.0), 2)
            td["pt_price"] = pt_price
            td["stop_price"] = initial_sl_price
            td["remaining_contracts"] = contracts
            td["status"] = "working"

            oco_order_id = f"oco_paper_{trade.id}_{td.get('tier_id', idx + 1)}"

            if not is_paper:
                self._resolve_broker()
                if self.broker and not getattr(self.broker, "_is_journal", False):
                    try:
                        if initial_sl_price > 0:
                            res = self.broker.place_oco_order(
                                trade.spec.occ_symbol,
                                contracts,
                                profit_price=pt_price,
                                stop_price=initial_sl_price,
                                limit_price=initial_sl_price,
                                action="SELL_TO_CLOSE",
                            )
                        else:
                            res = self.broker.place_limit_order(
                                "SELL_TO_CLOSE",
                                trade.spec.occ_symbol,
                                contracts,
                                pt_price,
                            )
                        if res and getattr(res, "order_id", None):
                            oco_order_id = str(res.order_id)
                        log.info(
                            "OCO CLOSE ORDER PLACED [%s Rung %d]: %d contracts @ PT $%.2f / SL $%.2f (order=%s)",
                            trade.id, td.get("tier_id", idx + 1), contracts, pt_price, initial_sl_price, oco_order_id,
                        )
                    except Exception as exc:
                        log.error("Failed to place OCO order on broker for %s rung %d: %s", trade.id, idx + 1, exc)

            td["oco_order_id"] = oco_order_id

        self._persist_trade(trade)

    def take_signal_manually(
        self,
        signal_id: str,
        custom_price: Optional[float] = None,
        custom_qty: Optional[int] = None,
        paper_mode: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Allow user to manually take a trade previously held, skipped, or tracked."""
        with self._lock:
            target_sig = next((s for s in self.signals if s.id == signal_id), None)
        if not target_sig:
            return {"ok": False, "error": f"Signal {signal_id} not found"}
        if not target_sig.spec:
            return {"ok": False, "error": "Signal does not have a valid option spec"}

        quote = self.quote_service.get_quote(target_sig.spec)
        market_px = custom_price if custom_price and custom_price > 0 else quote.market_buy_price
        if market_px <= 0:
            market_px = target_sig.current_price or target_sig.alert_price

        sub_strat = getattr(target_sig, "sub_strategy", "Others")
        called_by = getattr(target_sig, "called_by", "")
        sub_cfg = self.config_mgr.get_sub_strategy_config(sub_strat)

        if custom_qty and custom_qty > 0:
            qty = custom_qty
        else:
            entry_mode = getattr(sub_cfg, "entry_limit_mode", "dollar")
            if entry_mode == "contracts":
                qty = max(1, int(getattr(sub_cfg, "allocation_contracts", 1)))
            else:
                cost = max(1.0, market_px * 100.0)
                alloc_amt = getattr(sub_cfg, "allocation_amount", self.config_mgr.allocation_amount)
                qty = max(1, int(alloc_amt // cost))

        premium_pct = round(((market_px - target_sig.alert_price) / target_sig.alert_price) * 100.0, 2)

        if paper_mode is not None:
            target_paper = bool(paper_mode)
        elif self.config_mgr.paper_mode or getattr(sub_cfg, "paper_mode", False):
            target_paper = True
        else:
            # Operator manual take defaults to LIVE brokerage order when engine is live
            target_paper = False

        try:
            trade = self._execute_and_open_trade(
                spec=target_sig.spec,
                qty=qty,
                fill_price=market_px,
                alert_price=target_sig.alert_price,
                premium_pct=premium_pct,
                sig_id=signal_id,
                sub_strategy=sub_strat,
                called_by=called_by,
                paper_mode=target_paper,
                ladder_tiers=sub_cfg.ladder_tiers if sub_cfg and sub_cfg.ladder_tiers else self.config_mgr.ladder_tiers,
            )
        except Exception as exc:
            log.error("Manual take failed on broker: %s", exc)
            return {"ok": False, "error": str(exc)}

        sig_status = "WORKING" if trade.status == "WORKING" else "TAKEN"
        sig_reason = (
            f"Manually queued {qty} contracts on brokerage @ ${market_px:.2f} (order #{trade.order_id})"
            if trade.status == "WORKING"
            else f"Executed {qty} contracts @ ${market_px:.2f} ({'Paper' if trade.paper_mode else 'Live'} [{sub_strat}])"
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
                "paper_mode": trade.paper_mode,
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

    def _update_signal_on_entry_fill_unlocked(self, trade: CloneTrade) -> None:
        """Update corresponding signal status when working order fills on broker."""
        for s in self.signals:
            if s.trade_id == trade.id or (s.spec and s.spec.occ_symbol == trade.spec.occ_symbol and s.status == "WORKING"):
                s.status = "TAKEN"
                s.status_reason = f"Filled {trade.remaining_quantity} contracts @ ${trade.entry_price:.2f} ({'Paper' if trade.paper_mode else 'Live'} [{trade.sub_strategy}])"
                s.market_price = trade.entry_price
                s.trade_id = trade.id
                self._persist_signal(s)
                break

    def _update_signal_on_entry_cancel_unlocked(self, trade: CloneTrade, reason_code: str = "CANCELLED") -> None:
        """Update corresponding signal status when working order is cancelled or aborted."""
        for s in self.signals:
            if s.trade_id == trade.id or (s.spec and s.spec.occ_symbol == trade.spec.occ_symbol and s.status == "WORKING"):
                s.status = "CHASE_EXCEEDED" if reason_code == "CHASE_EXCEEDED" else "CANCELLED"
                s.status_reason = trade.status_reason
                self._persist_signal(s)
                break

    def _handle_working_entry_fill_unlocked(self, trade: CloneTrade, fill_result: Any) -> None:
        """Transition WORKING trade to OPEN when broker reports entry fill."""
        with self._lock:
            trade.status = "OPEN"
            if getattr(fill_result, "filled_price", None) and float(fill_result.filled_price) > 0:
                real_fill = float(fill_result.filled_price)
                trade.entry_price = real_fill
                trade.contract_cost = real_fill * 100.0

            filled_qty = int(getattr(fill_result, "filled_quantity", 0) or 0)
            if filled_qty > 0 and filled_qty != trade.remaining_quantity:
                trade.initial_quantity = filled_qty
                trade.remaining_quantity = filled_qty
                tier_pcts = [t.get("exit_pct", 0) for t in trade.tier_distribution]
                contracts_per_tier = distribute_contracts_across_tiers(filled_qty, len(trade.tier_distribution), tier_pcts=tier_pcts)
                for i, td in enumerate(trade.tier_distribution):
                    td["contracts"] = contracts_per_tier[i] if i < len(contracts_per_tier) else 0

            tier_0 = LadderTier.from_dict(trade.ladder_tiers[0]) if trade.ladder_tiers else LadderTier(1, 25.0, trade.active_stop_pct)
            trade.active_pt_price = round(trade.entry_price * (1.0 + tier_0.target_profit_pct / 100.0), 2)
            if trade.active_stop_pct > -100.0:
                trade.active_stop_price = round(max(0.01, trade.entry_price * (1.0 + trade.active_stop_pct / 100.0)), 2)
            trade.status_reason = f"Filled {trade.remaining_quantity} contracts @ ${trade.entry_price:.2f} (Live [{trade.sub_strategy}])"
            trade._reconcile_entry = False
            self._place_ladder_oco_orders(trade)
            self._update_signal_on_entry_fill_unlocked(trade)
            self._persist_trade(trade)
            log.info(
                "WORKING ORDER FILLED: %s Order #%s filled %d @ $%.2f",
                trade.id, trade.order_id, trade.remaining_quantity, trade.entry_price
            )

    def _handle_entry_chase(self, trade: CloneTrade) -> None:
        """Chase unfilled limit buy order if within chase guard, or cancel if exceeded or attempts exhausted."""
        now = time.time()
        last_chase = getattr(trade, "last_chase_time", 0.0) or getattr(trade, "entry_placed_time", 0.0) or now
        if now - last_chase < 3.0:
            return

        quote = self.quote_service.get_quote(trade.spec)
        current_buy_px = quote.market_buy_price or quote.ask or quote.last or quote.mid
        if not current_buy_px or current_buy_px <= 0:
            return

        sub_cfg = self.config_mgr.get_sub_strategy_config(trade.sub_strategy)
        max_premium = sub_cfg.max_premium_pct if sub_cfg else self.config_mgr.max_premium_pct
        alert_px = trade.webhook_price
        premium_pct = round(((current_buy_px - alert_px) / alert_px) * 100.0, 2) if alert_px > 0 else 0.0

        max_attempts = getattr(trade, "max_chase_attempts", 3)
        chase_count = getattr(trade, "chase_count", 0)

        # 1. Chase guard exceeded or attempts exhausted -> cancel working order cleanly
        if premium_pct > max_premium or chase_count >= max_attempts:
            reason = (
                f"Chase guard exceeded (+{premium_pct:.1f}% > +{max_premium:.1f}%)"
                if premium_pct > max_premium
                else f"Chase attempts exhausted ({chase_count}/{max_attempts})"
            )
            log.warning("CHASE ABORTED %s: %s. Cancelling working order #%s at broker.", trade.id, reason, trade.order_id)
            if self.broker and trade.order_id:
                try:
                    cres = self.broker.cancel_order(trade.order_id)
                    if cres and getattr(cres, "status", "") == "filled":
                        log.info("Order %s filled during cancel attempt", trade.order_id)
                        self._handle_working_entry_fill_unlocked(trade, cres)
                        return
                except Exception as exc:
                    log.warning("Failed to cancel order #%s during chase abort: %s", trade.order_id, exc)

            with self._lock:
                trade.status = "CANCELLED"
                trade.status_reason = f"{reason} - order #{trade.order_id} cancelled at broker"
                trade.remaining_quantity = 0
                trade.closed_at = utc_now_iso()
                self.active_trades.pop(trade.id, None)
                self.closed_trades.append(trade)
                self._update_signal_on_entry_cancel_unlocked(trade, "CHASE_EXCEEDED" if premium_pct > max_premium else "CANCELLED")
                self._persist_trade(trade)
            return

        # 2. Market price has risen above our resting limit price -> chase it higher
        if current_buy_px > trade.entry_price:
            new_price = current_buy_px
            log.info(
                "CHASING ENTRY %s: Attempt %d/%d | Price $%.2f -> $%.2f (Premium: +%.1f%% <= %.1f%%)",
                trade.id, chase_count + 1, max_attempts, trade.entry_price, new_price, premium_pct, max_premium
            )
            if self.broker and trade.order_id:
                try:
                    cres = self.broker.cancel_order(trade.order_id)
                    if cres and getattr(cres, "status", "") == "filled":
                        log.info("Order %s filled while attempting chase replace", trade.order_id)
                        self._handle_working_entry_fill_unlocked(trade, cres)
                        return

                    if not cres or getattr(cres, "status", "") != "cancelled":
                        log.warning(
                            "Cannot chase order %s: cancel returned status '%s' (not confirmed cancelled). Aborting chase to prevent duplicate fill.",
                            trade.order_id, getattr(cres, "status", "unknown")
                        )
                        trade.last_chase_time = now
                        return

                    nres = self.broker.place_limit_order("BUY_TO_OPEN", trade.spec.occ_symbol, trade.initial_quantity, new_price)
                    if nres and getattr(nres, "success", False) and getattr(nres, "order_id", None):
                        with self._lock:
                            trade.order_id = str(nres.order_id)
                            trade.entry_price = new_price
                            trade.contract_cost = new_price * 100.0
                            trade.chase_count = chase_count + 1
                            trade.last_chase_time = now
                            nres_st = str(getattr(nres, "status", "")).lower()
                            if nres_st == "filled" or (getattr(nres, "filled_quantity", 0) or 0) > 0:
                                self._handle_working_entry_fill_unlocked(trade, nres)
                            else:
                                trade.status_reason = f"Chasing entry #{trade.order_id} @ ${new_price:.2f} (attempt {trade.chase_count}/{max_attempts})"
                                self._persist_trade(trade)
                    else:
                        err = getattr(nres, "message", "broker rejected") if nres else "no response"
                        log.warning("Chase placement failed for %s: %s", trade.id, err)
                        trade.last_chase_time = now
                except Exception as exc:
                    log.warning("Exception during entry chase for %s: %s", trade.id, exc)
                    trade.last_chase_time = now

    def update_quotes_and_check_stops(self) -> None:
        """Background loop iteration checking live quotes against ladder PTs and full SL, and updating tracked alerts."""
        try:
            from meic0dte.app.utilities import central_now
            today_cst = central_now().strftime("%Y-%m-%d")
        except Exception:
            today_cst = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            open_trades = list(self.active_trades.values())
            tracking_sigs = [
                s for s in self.signals
                if s.spec and (s.date_cst == today_cst or (s.received_at_cst and s.received_at_cst.startswith(today_cst)))
            ]

        # Update real-time quotes for today's alerts / signals
        for sig in tracking_sigs:
            try:
                q = self.quote_service.get_quote(sig.spec)
                px = q.market_buy_price or q.last or q.mid
                if px and px > 0:
                    with self._lock:
                        sig.current_price = px
                        if sig.peak_price is None or px > sig.peak_price:
                            sig.peak_price = px
                        if sig.bottom_price is None or sig.bottom_price <= 0 or px < sig.bottom_price:
                            sig.bottom_price = px
                        sig.last_quote_time = utc_now_iso()
                        self._signals_dirty = True
            except Exception as exc:
                log.debug("Failed to update quote for tracked signal %s: %s", sig.id, exc)

        now_ts = time.time()
        if (self._signals_dirty or self._trades_dirty) and (now_ts - self._last_flush_ts >= 5.0):
            self.flush_all()

        if not open_trades:
            return

        cfg = self.config_mgr

        for trade in open_trades:
            is_paper = getattr(trade, "paper_mode", False) or cfg.paper_mode

            # Handle WORKING trades (awaiting fill or chasing entry)
            if trade.status == "WORKING":
                if not is_paper and self.broker and trade.order_id and hasattr(self.broker, "get_order_status"):
                    try:
                        ost = self.broker.get_order_status(trade.order_id)
                        ost_st = str(getattr(ost, "status", "")).lower() if ost else ""
                        ost_fq = int(getattr(ost, "filled_quantity", 0) or 0) if ost else 0

                        if ost and (ost_st == "filled" or ost_fq > 0):
                            self._handle_working_entry_fill_unlocked(trade, ost)
                        elif ost_st in ("cancelled", "canceled", "rejected", "expired"):
                            with self._lock:
                                trade.status = "CANCELLED"
                                trade.status_reason = f"Entry order #{trade.order_id} was {ost_st} on broker"
                                trade.remaining_quantity = 0
                                trade.closed_at = utc_now_iso()
                                self.active_trades.pop(trade.id, None)
                                self.closed_trades.append(trade)
                                self._update_signal_on_entry_cancel_unlocked(trade, ost_st)
                                self._persist_trade(trade)
                                log.info("WORKING ORDER %s CANCELLED/REJECTED on broker (%s)", trade.id, ost_st)
                            continue
                        else:
                            # Order is still working/live on broker -> handle entry chase!
                            self._handle_entry_chase(trade)
                            continue
                    except Exception as exc:
                        log.debug("Error checking working order %s: %s", trade.id, exc)
                        continue
                elif is_paper:
                    with self._lock:
                        trade.status = "OPEN"
                        trade.status_reason = f"Active trade (Paper [{trade.sub_strategy}])"
                        self._persist_trade(trade)

            # CRITICAL GUARD: Only evaluate quotes, PTs, and stops for OPEN trades!
            if trade.status != "OPEN":
                continue

            # Reconcile entry fill price on live broker if unverified
            if not is_paper and self.broker and trade.order_id and getattr(trade, "_reconcile_entry", True) and hasattr(self.broker, "get_order_status"):
                try:
                    ost = self.broker.get_order_status(trade.order_id)
                    if ost and getattr(ost, "status", "").lower() == "filled":
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

            if not getattr(trade, "_ladder_oco_placed", False) and trade.status == "OPEN":
                self._place_ladder_oco_orders(trade)

            # Monitor active broker OCO orders for this trade first (Requirement 3.3)
            pt_hit = self._monitor_ladder_oco_orders(trade)
            if trade.status != "OPEN":
                continue
            if pt_hit:
                continue

            # Periodic Broker Reconciliation for OPEN trades (keep dashboard in sync with brokerage)
            if not is_paper and self.broker and hasattr(self.broker, "get_single_leg_position_qty"):
                now_ts = time.time()
                last_sync = getattr(trade, "_last_broker_sync", 0.0)
                if now_ts - last_sync >= 5.0:
                    trade._last_broker_sync = now_ts
                    try:
                        held_qty = self.broker.get_single_leg_position_qty(trade.spec.occ_symbol, priority='LOW')
                        working_qty = sum(
                            td.get("remaining_contracts", td.get("contracts", 0))
                            for td in (trade.tier_distribution or [])
                            if td.get("status") == "working"
                        ) if trade.tier_distribution else trade.remaining_quantity

                        if held_qty <= 0 and working_qty > 0:
                            log.warning(
                                "BROKER_DESYNC_DETECTED: Trade %s has remaining_quantity=%d (working=%d), but broker holds %d contracts. Closing trade to sync.",
                                trade.id, trade.remaining_quantity, working_qty, held_qty
                            )
                            with self._lock:
                                trade.remaining_quantity = 0
                                trade.status = "CLOSED"
                                trade.status_reason = f"Position flat on broker ({trade.spec.occ_symbol})"
                                trade.closed_at = utc_now_iso()
                                trade.unrealized_pnl_usd = 0.0
                                self.active_trades.pop(trade.id, None)
                                self.closed_trades.append(trade)
                                self._persist_trade(trade)
                            continue
                        elif 0 < held_qty < working_qty:
                            log.warning("BROKER_DESYNC_ADJUSTED: Trade %s remaining_quantity adjusted from %d to %d.", trade.id, trade.remaining_quantity, held_qty)
                            with self._lock:
                                trade.remaining_quantity = held_qty
                                self._persist_trade(trade)
                        elif held_qty > working_qty:
                            log.warning(
                                "BROKER_DESYNC_OVERAGE: Trade %s remaining_quantity=%d (working=%d), but broker holds %d contracts. Adjusting remaining_quantity to match broker held contracts.",
                                trade.id, trade.remaining_quantity, working_qty, held_qty
                            )
                            with self._lock:
                                trade.remaining_quantity = held_qty
                                self._persist_trade(trade)
                    except Exception as exc:
                        log.debug("Broker sync check error for %s: %s", trade.id, exc)

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
                    self._trades_dirty = True
                    trade.highest_profit_pct = round(
                        ((current_px - trade.entry_price) / trade.entry_price) * 100.0, 2
                    )
                if not hasattr(trade, "lowest_price") or trade.lowest_price <= 0 or current_px < trade.lowest_price:
                    trade.lowest_price = current_px
                    self._trades_dirty = True

                trade.unrealized_pnl_pct = round(
                    ((current_px - trade.entry_price) / trade.entry_price) * 100.0, 2
                )
                trade.unrealized_pnl_usd = round(
                    (current_px - trade.entry_price) * 100.0 * trade.remaining_quantity, 2
                )

                # 1. Check Stop Loss (Full Position Exit) - No stop loss if active_stop_price <= 0
                if trade.active_stop_price and trade.active_stop_price > 0 and current_px <= trade.active_stop_price:
                    self._trigger_stop_loss_unlocked(trade, current_px)
                    continue

                # 2. Check Profit Target (Unbalanced OCO Partial Exit)
                if current_px >= trade.active_pt_price and trade.remaining_quantity > 0:
                    self._trigger_partial_pt_unlocked(trade, current_px)

        now_ts = time.time()
        if (self._signals_dirty or self._trades_dirty) and (now_ts - self._last_flush_ts >= 5.0):
            self.flush_all()

    def _chase_exit_limit_order(
        self,
        trade: CloneTrade,
        qty: int,
        target_px: float,
        is_stop_loss: bool = False,
    ) -> tuple[bool, float, int]:
        """Software breach limit chase loop (MEIC-style).

        Attempts to fill a SELL_TO_CLOSE order using an active limit order chase.
        For Stop Loss, chases aggressively down to market bid (with minimum floor $0.01).
        For Profit Target, chases at bid/target without selling below entry price if possible.

        Returns (success: bool, fill_price: float, filled_qty: int).
        """
        if not self.broker or getattr(self.broker, "_is_journal", False):
            return True, target_px, qty

        occ_sym = trade.spec.occ_symbol
        max_attempts = 5 if is_stop_loss else 3
        fill_px = target_px

        # Preflight verification: verify actual contracts held
        if hasattr(self.broker, "get_single_leg_position_qty"):
            try:
                held_qty = self.broker.get_single_leg_position_qty(occ_sym, priority='HIGH')
                if held_qty <= 0:
                    log.info("Position already flat on broker for %s (%s)", trade.id, occ_sym)
                    return True, target_px, qty
                if held_qty < qty:
                    qty = held_qty
            except Exception as exc:
                log.warning("Broker preflight query error: %s", exc)

        # For stop loss, try place_market_order first if broker supports it (e.g. for futures or test mocks)
        if is_stop_loss and hasattr(self.broker, "place_market_order"):
            try:
                mres = self.broker.place_market_order("SELL_TO_CLOSE", occ_sym, qty)
                if mres and getattr(mres, "success", False):
                    order_id = str(getattr(mres, "order_id", None) or "")
                    trade.close_order_id = order_id
                    if getattr(mres, "status", "").lower() == "filled" or (getattr(mres, "filled_price", None) and float(mres.filled_price) > 0):
                        real_px = float(getattr(mres, "filled_price", 0.0) or target_px)
                        actual_qty = int(getattr(mres, "filled_quantity", 0) or qty)
                        return True, real_px, actual_qty
                    if order_id and hasattr(self.broker, "get_order_status"):
                        time.sleep(0.4)
                        ost = self.broker.get_order_status(order_id)
                        if getattr(ost, "status", "").lower() == "filled" or (getattr(ost, "filled_price", None) and float(ost.filled_price) > 0):
                            real_px = float(getattr(ost, "filled_price", 0.0) or target_px)
                            actual_qty = int(getattr(ost, "filled_quantity", 0) or qty)
                            return True, real_px, actual_qty
            except Exception as m_exc:
                log.debug("Market order attempt exception: %s", m_exc)

        for attempt in range(1, max_attempts + 1):
            quote = self.quote_service.get_quote(trade.spec)
            bid = quote.market_sell_price or quote.bid or quote.last or target_px

            if is_stop_loss:
                if attempt == max_attempts:
                    limit_px = 0.01
                else:
                    limit_px = max(0.01, round(bid, 2) if bid and bid > 0 else round(target_px, 2))
            else:
                # Profit Target chase: sell at bid if profitable, or at target_px
                if bid and bid > 0:
                    limit_px = round(max(trade.entry_price + 0.01, bid), 2)
                else:
                    limit_px = round(target_px, 2)

            log.info(
                "EXIT CHASE %s [%s] Attempt %d/%d: Placing SELL_TO_CLOSE %d %s @ $%.2f",
                trade.id, "SL" if is_stop_loss else "PT", attempt, max_attempts, qty, occ_sym, limit_px
            )

            try:
                nres = self.broker.place_limit_order("SELL_TO_CLOSE", occ_sym, qty, limit_px)
                if not nres or not getattr(nres, "success", False) or not getattr(nres, "order_id", None):
                    err = getattr(nres, "message", "rejected") if nres else "no response"
                    log.warning("Exit chase order placement failed for %s: %s", trade.id, err)
                    time.sleep(0.5)
                    continue

                order_id = str(nres.order_id)
                trade.close_order_id = order_id

                # Immediate fill check (for mocks or fast fills)
                nres_st = str(getattr(nres, "status", "")).lower()
                nres_fq = int(getattr(nres, "filled_quantity", 0) or 0)
                if nres_st == "filled" or nres_fq >= qty:
                    real_px = float(getattr(nres, "filled_price", 0.0) or limit_px)
                    actual_qty = nres_fq if nres_fq > 0 else qty
                    log.info(
                        "EXIT CHASE FILLED %s: Order #%s filled %d ctrs @ $%.2f",
                        trade.id, order_id, actual_qty, real_px
                    )
                    return True, real_px, actual_qty

                # Wait and poll for fill
                for _ in range(3):
                    time.sleep(0.4)
                    ost = self.broker.get_order_status(order_id)
                    ost_st = str(getattr(ost, "status", "")).lower() if ost else ""
                    ost_fq = int(getattr(ost, "filled_quantity", 0) or 0) if ost else 0
                    if ost_st == "filled" or ost_fq >= qty:
                        real_px = float(getattr(ost, "filled_price", 0.0) or limit_px)
                        actual_qty = ost_fq if ost_fq > 0 else qty
                        log.info(
                            "EXIT CHASE FILLED %s: Order #%s filled %d ctrs @ $%.2f",
                            trade.id, order_id, actual_qty, real_px
                        )
                        return True, real_px, actual_qty
                    if ost_st in ("cancelled", "canceled", "rejected", "expired"):
                        break

                # Not filled -> cancel and chase
                try:
                    cres = self.broker.cancel_order(order_id)
                    time.sleep(0.3)
                    if cres and getattr(cres, "status", "") == "filled":
                        real_px = float(getattr(cres, "filled_price", 0.0) or limit_px)
                        return True, real_px, qty
                except Exception as c_exc:
                    log.debug("Cancel working exit order %s error: %s", order_id, c_exc)

            except Exception as exc:
                log.warning("Exception during exit chase for %s: %s", trade.id, exc)
                time.sleep(0.5)

        # Final check if broker position became flat during the attempts
        if hasattr(self.broker, "get_single_leg_position_qty"):
            try:
                held_qty = self.broker.get_single_leg_position_qty(occ_sym, priority='HIGH')
                if held_qty <= 0:
                    log.info("Position flat at broker after exit chase for %s", trade.id)
                    return True, target_px, qty
            except Exception:
                pass

        return False, target_px, 0

    def _trigger_stop_loss_unlocked(self, trade: CloneTrade, exit_px: float) -> None:
        """Stop triggered: sell all remaining contracts via software breach and chase loop."""
        rem_qty = trade.remaining_quantity
        if rem_qty <= 0:
            return

        now_iso = utc_now_iso()

        # Place sell order on broker if trade is live
        is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode
        if not is_paper:
            self._resolve_broker()
            if self.broker and not getattr(self.broker, "_is_journal", False):
                # Preflight check: verify actual contracts held before selling
                if hasattr(self.broker, "get_single_leg_position_qty"):
                    held_qty = self.broker.get_single_leg_position_qty(trade.spec.occ_symbol, priority='HIGH')
                    if held_qty <= 0:
                        log.error(
                            "BROKER_PREFLIGHT_BLOCKED_SL: Position flat at broker for %s (held_qty=%d, need %d). Syncing trade to CLOSED.",
                            trade.id, held_qty, rem_qty
                        )
                        trade.remaining_quantity = 0
                        trade.status = "CLOSED"
                        trade.status_reason = f"Position flat on brokerage ({trade.spec.occ_symbol})"
                        trade.closed_at = now_iso
                        trade.unrealized_pnl_usd = 0.0
                        self.active_trades.pop(trade.id, None)
                        self.closed_trades.append(trade)
                        self._persist_trade(trade)
                        return
                    elif held_qty < rem_qty:
                        rem_qty = held_qty

                # Cancel any active working OCO orders or prior close orders on broker
                if hasattr(self.broker, "cancel_order"):
                    if getattr(trade, "close_order_id", None) and not str(trade.close_order_id).startswith("paper_"):
                        try:
                            self.broker.cancel_order(trade.close_order_id)
                        except Exception:
                            pass
                    for td in trade.tier_distribution:
                        oco_id = td.get("oco_order_id")
                        if oco_id and not str(oco_id).startswith("oco_paper_") and td.get("status") == "working":
                            try:
                                self.broker.cancel_order(oco_id)
                            except Exception:
                                pass

                    # If any working orders remain for this exact symbol on broker, cancel them to prevent position lock
                    if hasattr(self.broker, "get_live_orders_cached"):
                        try:
                            live_orders = self.broker.get_live_orders_cached() or []
                            for o in live_orders:
                                o_st = str(getattr(o, "status", "")).lower()
                                if o_st in ("received", "routed", "in flight", "working", "live"):
                                    for leg in getattr(o, "legs", []):
                                        if getattr(leg, "symbol", "") == trade.spec.occ_symbol:
                                            try:
                                                self.broker.cancel_order(str(getattr(o, "id", "")))
                                            except Exception:
                                                pass
                        except Exception:
                            pass

                time.sleep(0.3)

                success, fill_px, filled_qty = self._chase_exit_limit_order(
                    trade, rem_qty, exit_px, is_stop_loss=True
                )
                if not success:
                    log.error(
                        "STOP_LOSS_CHASE_UNFILLED: Trade %s SL chase could not fill %d contracts on broker. Trade remains OPEN for retry.",
                        trade.id, rem_qty
                    )
                    trade.status_reason = f"SL breach unfilled ({rem_qty} contracts resting on broker)"
                    self._persist_trade(trade)
                    return
                exit_px = fill_px
                rem_qty = filled_qty

        profit_pct = round(((exit_px - trade.entry_price) / trade.entry_price) * 100.0, 2)
        pnl_usd = round((exit_px - trade.entry_price) * 100.0 * rem_qty, 2)

        for td in trade.tier_distribution:
            td["status"] = "filled_sl"
            td["remaining_contracts"] = 0

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
        trade.remaining_quantity = max(0, trade.remaining_quantity - rem_qty)
        trade.unrealized_pnl_usd = 0.0
        trade.status = "CLOSED"
        trade.status_reason = f"Stopped out @ ${exit_px:.2f} ({profit_pct:+.1f}%)"
        trade.closed_at = now_iso

        self.active_trades.pop(trade.id, None)
        self.closed_trades.append(trade)
        self._persist_trade(trade)

        log.info(
            "STOP LOSS EXECUTED %s [%s, paper=%s]: Sold %d contracts @ $%.2f | P&L: $%.2f",
            trade.id,
            getattr(trade, "sub_strategy", "PC"),
            getattr(trade, "paper_mode", False),
            rem_qty,
            exit_px,
            pnl_usd,
        )

    def _trigger_partial_pt_unlocked(self, trade: CloneTrade, exit_px: float) -> None:
        """Profit target reached: sell this tier's contract tranche, advance to next PT."""
        current_idx = trade.current_tier_index

        # Determine ladder tiers: trade-specific ladder tiers take precedence, then sub-strategy tiers, then global config tiers
        tiers = []
        if getattr(trade, "ladder_tiers", None) and isinstance(trade.ladder_tiers, list) and trade.ladder_tiers:
            tiers = [LadderTier.from_dict(t) if isinstance(t, dict) else t for t in trade.ladder_tiers]
        if not tiers:
            sub_cfg = self.config_mgr.get_sub_strategy_config(getattr(trade, "sub_strategy", "PC"))
            tiers = sub_cfg.ladder_tiers if sub_cfg and sub_cfg.ladder_tiers else self.config_mgr.ladder_tiers

        # Determine contracts to close for this tier
        tier_dist = trade.tier_distribution
        qty_for_tier = 1
        tier_exit_pct = round(100.0 / max(1, len(tiers)), 1)
        if current_idx < len(tier_dist):
            qty_for_tier = tier_dist[current_idx].get("contracts", 1)
            tier_exit_pct = tier_dist[current_idx].get("exit_pct", tier_exit_pct)

        # Cap at remaining quantity
        qty_to_close = min(trade.remaining_quantity, max(1, qty_for_tier))

        # Place partial sell order on broker if live
        is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode
        if not is_paper:
            self._resolve_broker()
            if self.broker and not getattr(self.broker, "_is_journal", False):
                # Preflight check: verify actual contracts held before selling
                if hasattr(self.broker, "get_single_leg_position_qty"):
                    held_qty = self.broker.get_single_leg_position_qty(trade.spec.occ_symbol, priority='HIGH')
                    if held_qty <= 0:
                        log.error(
                            "BROKER_PREFLIGHT_BLOCKED_PT: Position flat at broker for %s (held_qty=%d, need %d). Syncing trade to CLOSED.",
                            trade.id, held_qty, qty_to_close
                        )
                        trade.remaining_quantity = 0
                        trade.status = "CLOSED"
                        trade.status_reason = f"Position flat on brokerage ({trade.spec.occ_symbol})"
                        trade.closed_at = now_iso
                        trade.unrealized_pnl_usd = 0.0
                        self.active_trades.pop(trade.id, None)
                        self.closed_trades.append(trade)
                        self._persist_trade(trade)
                        return
                    elif held_qty < qty_to_close:
                        qty_to_close = held_qty

                # Cancel the resting broker OCO order for this tier
                cur_td = trade.tier_distribution[current_idx] if current_idx < len(trade.tier_distribution) else None
                if cur_td:
                    oco_id = cur_td.get("oco_order_id")
                    if oco_id and not str(oco_id).startswith("oco_paper_") and hasattr(self.broker, "cancel_order"):
                        try:
                            if hasattr(self.broker, "get_order_status"):
                                ost = self.broker.get_order_status(str(oco_id))
                                if ost and str(getattr(ost, "status", "")).lower() == "filled":
                                    self._monitor_ladder_oco_orders(trade)
                                    return
                            cres = self.broker.cancel_order(str(oco_id))
                            if cres and str(getattr(cres, "status", "")).lower() == "filled":
                                self._monitor_ladder_oco_orders(trade)
                                return
                            time.sleep(0.3)
                        except Exception as exc:
                            log.warning("Failed to cancel OCO order %s: %s", oco_id, exc)

                success, fill_px, filled_qty = self._chase_exit_limit_order(
                    trade, qty_to_close, exit_px, is_stop_loss=False
                )
                if not success:
                    log.warning(
                        "PARTIAL_PT_CHASE_UNFILLED: Trade %s Tier %d PT chase could not fill %d contracts. Restoring working order.",
                        trade.id, current_idx + 1, qty_to_close
                    )
                    # Re-establish working limit order at original PT price
                    try:
                        nres = self.broker.place_limit_order("SELL_TO_CLOSE", trade.spec.occ_symbol, qty_to_close, exit_px)
                        if nres and getattr(nres, "order_id", None) and cur_td:
                            cur_td["oco_order_id"] = str(nres.order_id)
                    except Exception as exc:
                        log.warning("Could not re-establish working PT limit order: %s", exc)
                    trade.status_reason = f"Tier {current_idx + 1} PT breach unfilled — limit order resting @ ${exit_px:.2f}"
                    self._persist_trade(trade)
                    return
                exit_px = fill_px
                qty_to_close = filled_qty

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
            "PARTIAL PT HIT %s [%s] (%s): Sold %d contracts @ $%.2f (P&L: $%.2f). Remaining: %d contracts",
            trade.id,
            getattr(trade, "sub_strategy", "PC"),
            tier_name,
            qty_to_close,
            exit_px,
            pnl_usd,
            trade.remaining_quantity,
        )

        # Mark this tier as filled in tier_distribution
        if current_idx < len(trade.tier_distribution):
            cur_td = trade.tier_distribution[current_idx]
            cur_td["status"] = "filled_pt"
            cur_td["filled_price"] = exit_px
            cur_td["filled_at"] = now_iso
            cur_td["remaining_contracts"] = 0

        # Check roll_runner_every_leg setting
        sub_cfg = self.config_mgr.get_sub_strategy_config(getattr(trade, "sub_strategy", "PC"))
        roll_every_leg = bool(getattr(sub_cfg, "roll_runner_every_leg", False) or getattr(trade, "roll_runner_every_leg", False))

        # If all contracts closed
        if trade.remaining_quantity <= 0:
            trade.status = "CLOSED"
            trade.closed_at = now_iso
            trade.unrealized_pnl_usd = 0.0
            is_final_rung = (current_idx >= len(tiers) - 1)
            if is_final_rung:
                trade.status_reason = "All ladder profit targets hit"
            else:
                trade.status_reason = f"Position closed early at Tier {current_idx + 1} PT (contracts exhausted before reaching final rung)"
            self.active_trades.pop(trade.id, None)
            self.closed_trades.append(trade)
            self._persist_trade(trade)
            if is_final_rung or roll_every_leg:
                try:
                    self._execute_runner_roll(trade, exit_px)
                except Exception as roll_exc:
                    log.error("Failed to execute runner roll on PT close of %s: %s", trade.id, roll_exc)
            else:
                log.info(
                    "RUNNER ROLL SKIPPED for %s: Closed at Tier %d of %d (contracts exhausted before reaching final rung)",
                    trade.id, current_idx + 1, len(tiers)
                )
            return

        # If roll_runner_every_leg is enabled, trigger roll on this intermediate rung PT hit
        if roll_every_leg:
            try:
                self._execute_runner_roll(trade, exit_px)
            except Exception as roll_exc:
                log.error("Failed to execute runner roll on intermediate PT of %s: %s", trade.id, roll_exc)

        # Advance to next ladder tier for remaining contracts
        trade.current_tier_index += 1
        next_idx = trade.current_tier_index

        # Ratchet stop loss on all subsequent rungs (Requirement 3.3)
        # e.g. Once rung 1 hits PT, stops for other rungs modified to breakeven (0% = entry_price),
        # and once rung 2 hits, stop for rung 3 is pushed higher (+100c)
        hit_tier = tiers[current_idx] if current_idx < len(tiers) else None
        next_tier = tiers[next_idx] if next_idx < len(tiers) else None
        ratchet_sl_pct = None
        if next_tier and next_tier.stop_loss_pct is not None:
            ratchet_sl_pct = next_tier.stop_loss_pct
        elif hit_tier and hit_tier.stop_loss_pct is not None:
            ratchet_sl_pct = hit_tier.stop_loss_pct

        if ratchet_sl_pct is not None and ratchet_sl_pct > -100.0:
            new_stop_price = round(max(0.01, trade.entry_price * (1.0 + ratchet_sl_pct / 100.0)), 2)
        else:
            new_stop_price = 0.0

        if new_stop_price > trade.active_stop_price or trade.active_stop_price <= 0:
            trade.active_stop_price = new_stop_price
            trade.active_stop_pct = ratchet_sl_pct if ratchet_sl_pct is not None else -100.0

        # Modify stops on subsequent rungs in tier_distribution & replace broker OCO orders
        for rem_i in range(next_idx, len(trade.tier_distribution)):
            rem_td = trade.tier_distribution[rem_i]
            if rem_td.get("status") == "working":
                old_sl = rem_td.get("stop_price", 0.0)
                rem_td["stop_price"] = new_stop_price
                rem_c = rem_td.get("remaining_contracts") or rem_td.get("contracts", 1)
                rem_pt = rem_td.get("pt_price", 0.0)
                if not is_paper and self.broker and rem_td.get("oco_order_id") and not str(rem_td["oco_order_id"]).startswith("oco_paper_"):
                    try:
                        self.broker.cancel_order(rem_td["oco_order_id"])
                        if new_stop_price > 0:
                            new_oco = self.broker.place_oco_order(
                                trade.spec.occ_symbol,
                                rem_c,
                                profit_price=rem_pt,
                                stop_price=new_stop_price,
                                limit_price=new_stop_price,
                                action="SELL_TO_CLOSE",
                            )
                        else:
                            new_oco = self.broker.place_limit_order(
                                "SELL_TO_CLOSE",
                                trade.spec.occ_symbol,
                                rem_c,
                                rem_pt,
                            )
                        if new_oco and getattr(new_oco, "order_id", None):
                            rem_td["oco_order_id"] = str(new_oco.order_id)
                        log.info(
                            "RATCHETED BROKER OCO: %s Rung %d Old SL $%.2f -> New SL $%.2f (order=%s)",
                            trade.id, rem_i + 1, old_sl, new_stop_price, rem_td["oco_order_id"]
                        )
                    except Exception as exc:
                        log.warning("Could not replace broker OCO order on ratchet for %s: %s", trade.id, exc)

        if next_idx < len(tiers):
            trade.active_pt_price = round(trade.entry_price * (1.0 + next_tier.target_profit_pct / 100.0), 2)
            log.info(
                "ADVANCED LADDER %s [%s] -> Next PT=%s ($%.2f), Ratcheted SL=%s ($%.2f)",
                trade.id,
                getattr(trade, "sub_strategy", "PC"),
                next_tier.description,
                trade.active_pt_price,
                ratchet_sl_pct,
                trade.active_stop_price,
            )
        else:
            trade.active_pt_price = 999999.0

        self._persist_trade(trade)

    def close_trade_manual(self, trade_id: str) -> Dict[str, Any]:
        """Manual cancellation if order is working, or 1-click Flatten for all remaining contracts if open."""
        with self._lock:
            trade = self.active_trades.get(trade_id)
            if not trade:
                return {"ok": False, "error": f"Open trade {trade_id} not found"}

            now_iso = utc_now_iso()
            is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode

            # If the trade is still WORKING / QUEUED (not yet filled)
            if trade.status == "WORKING":
                if not is_paper and self.broker and trade.order_id:
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
            if trade.status == "CLOSING":
                return {"ok": False, "error": f"Trade {trade_id} is already closing"}
            trade.status = "CLOSING"
            # Cancel all working OCO orders for this trade
            for td in trade.tier_distribution:
                if td.get("status") == "working":
                    td["status"] = "cancelled"
                    oco_oid = td.get("oco_order_id")
                    if oco_oid and not is_paper and self.broker and hasattr(self.broker, "cancel_order"):
                        try:
                            self.broker.cancel_order(oco_oid)
                        except Exception:
                            pass
            rem_qty = trade.remaining_quantity
            spec = trade.spec
            cur_px = trade.current_price
            occ_sym = spec.occ_symbol if spec else ""
            entry_px = trade.entry_price

        # Perform broker quote and order execution OUTSIDE self._lock to avoid blocking dashboard status
        quote = self.quote_service.get_quote(spec) if spec else None
        exit_px = (quote.market_sell_price if quote else None) or cur_px or entry_px
        close_order_id = None

        if not is_paper and occ_sym:
            try:
                self._resolve_broker()
            except Exception as exc:
                log.error("Could not resolve broker for manual close: %s", exc)

            if self.broker and not getattr(self.broker, "_is_journal", False):
                if hasattr(self.broker, "get_single_leg_position_qty"):
                    held_qty = self.broker.get_single_leg_position_qty(occ_sym, priority='HIGH')
                    if held_qty <= 0:
                        log.warning("Manual flatten: broker position already flat for %s", occ_sym)
                        rem_qty = 0
                    elif held_qty < rem_qty:
                        rem_qty = held_qty

                if rem_qty > 0:
                    try:
                        res = self.broker.place_market_order("SELL_TO_CLOSE", occ_sym, rem_qty)
                        if res and getattr(res, "order_id", None):
                            close_order_id = str(res.order_id)
                        if res and getattr(res, "filled_price", None) and float(res.filled_price) > 0:
                            exit_px = float(res.filled_price)
                        elif close_order_id and hasattr(self.broker, "get_order_status"):
                            try:
                                time.sleep(0.4)
                                ost = self.broker.get_order_status(close_order_id)
                                if getattr(ost, "filled_price", None) and float(ost.filled_price) > 0:
                                    exit_px = float(ost.filled_price)
                            except Exception as exc:
                                log.warning("Could not query close order fill: %s", exc)
                        if res and not res.success:
                            log.warning("Broker place_market_order non-success: %s", getattr(res, "message", "rejected"))
                    except Exception as exc:
                        log.warning("Broker manual close order error: %s", exc)

        with self._lock:
            # Re-acquire lock to finalize trade state
            profit_pct = round(((exit_px - entry_px) / entry_px) * 100.0, 2) if (entry_px > 0 and rem_qty > 0) else 0.0
            pnl_usd = round((exit_px - entry_px) * 100.0 * rem_qty, 2) if rem_qty > 0 else 0.0

            leg = ClosedLeg(
                tier_id=99,
                quantity=rem_qty,
                exit_price=exit_px,
                exit_time=now_iso,
                profit_pct=profit_pct,
                pnl_usd=pnl_usd,
                reason=f"Manual Flatten @ ${exit_px:.2f} for {rem_qty} contracts" if rem_qty > 0 else "Synced flat with broker",
            )
            trade.closed_legs.append(leg)
            trade.realized_pnl_usd += pnl_usd
            trade.remaining_quantity = 0
            trade.unrealized_pnl_usd = 0.0
            trade.status = "CLOSED"
            trade.status_reason = "Manual Flatten"
            trade.closed_at = now_iso
            if close_order_id:
                trade.close_order_id = close_order_id

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

    def _cancel_and_replace_ladder_orders(self, trade: CloneTrade) -> None:
        """Cancel existing working orders on broker/paper and place replacement ladder orders with new PT/SL."""
        is_paper = getattr(trade, "paper_mode", False) or self.config_mgr.paper_mode
        tiers = trade.tier_distribution or []

        # Cancel any active working broker orders
        if not is_paper and self.broker and not getattr(self.broker, "_is_journal", False):
            for td in tiers:
                oco_id = td.get("oco_order_id")
                if oco_id and not str(oco_id).startswith("oco_paper_"):
                    try:
                        log.info("Cancelling working broker order %s for trade %s before replacing ladder", oco_id, trade.id)
                        self.broker.cancel_order(str(oco_id))
                    except Exception as exc:
                        log.warning("Error cancelling broker order %s for %s: %s", oco_id, trade.id, exc)
            time.sleep(0.4)

        # Clear working status on tiers so new rungs can be placed
        for td in tiers:
            td["status"] = "pending"
            td["oco_order_id"] = None

        trade._ladder_oco_placed = False
        if trade.status == "OPEN" and trade.remaining_quantity > 0:
            self._place_ladder_oco_orders(trade)

    def update_open_trades_ladder(self, new_tiers: List[LadderTier], sub_strategy: Optional[str] = None) -> List[CloneTrade]:
        """Apply updated ladder rules to all currently open/active trades."""
        with self._lock:
            updated = []
            for trade in self.active_trades.values():
                if sub_strategy:
                    strat_bucket = classify_sub_strategy(sub_strategy)
                    t_bucket = classify_sub_strategy(trade.sub_strategy)
                    if t_bucket != strat_bucket and trade.sub_strategy != sub_strategy:
                        continue
                if trade.status in ("OPEN", "WORKING") and trade.remaining_quantity > 0:
                    self._apply_ladder_to_trade_unlocked(trade, new_tiers)
                    if trade.status == "OPEN":
                        self._cancel_and_replace_ladder_orders(trade)
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
            if trade.status == "OPEN":
                self._cancel_and_replace_ladder_orders(trade)
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
            today_live_realized = sum(
                t.get("realized_pnl_usd", 0.0) for t in today_closed if not t.get("paper_mode", False)
            ) + sum(
                t.realized_pnl_usd for t in self.active_trades.values() if not getattr(t, "paper_mode", False)
            )
            today_paper_realized = sum(
                t.get("realized_pnl_usd", 0.0) for t in today_closed if t.get("paper_mode", False)
            ) + sum(
                t.realized_pnl_usd for t in self.active_trades.values() if getattr(t, "paper_mode", False)
            )

            total_realized = sum(t.realized_pnl_usd for t in self.closed_trades) + sum(
                t.realized_pnl_usd for t in self.active_trades.values()
            )
            total_live_realized = sum(
                t.realized_pnl_usd for t in self.closed_trades if not getattr(t, "paper_mode", False)
            ) + sum(
                t.realized_pnl_usd for t in self.active_trades.values() if not getattr(t, "paper_mode", False)
            )
            total_paper_realized = sum(
                t.realized_pnl_usd for t in self.closed_trades if getattr(t, "paper_mode", False)
            ) + sum(
                t.realized_pnl_usd for t in self.active_trades.values() if getattr(t, "paper_mode", False)
            )

            total_unrealized = sum(t.unrealized_pnl_usd for t in self.active_trades.values())
            live_unrealized = sum(
                t.unrealized_pnl_usd for t in self.active_trades.values() if not getattr(t, "paper_mode", False)
            )
            paper_unrealized = sum(
                t.unrealized_pnl_usd for t in self.active_trades.values() if getattr(t, "paper_mode", False)
            )

        is_broker_live = (
            self.broker is not None
            and not getattr(self.broker, "_is_journal", False)
            and not getattr(self.broker, "_is_paper", False)
        )
        broker_name = type(self.broker).__name__ if self.broker else "None"

        def _match_set(name: str):
            if name == "Human Alerters":
                return {"Human Alerters", "TG", "PC", "PC2"}
            if name == "Genie":
                return {"Genie", "AI FlowAlgos"}
            if name == "Cowboy":
                return {"Cowboy"}
            if name == "AI FlowAlgos":
                return {"AI FlowAlgos", "Genie", "Cowboy"}
            return {name}

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
            "tracked_signals": [
                s.to_dict() for s in self.signals
                if s.status == "TRACKING" and (s.date_cst == today_cst or (s.received_at_cst and s.received_at_cst.startswith(today_cst)))
            ],
            "sub_strategies": {
                s_name: {
                    "name": s_name,
                    "display_name": self.config_mgr.get_sub_strategy_config(s_name).display_name or s_name,
                    "enabled": self.config_mgr.get_sub_strategy_config(s_name).enabled,
                    "paper_mode": self.config_mgr.get_sub_strategy_config(s_name).paper_mode,
                    "execution_mode": getattr(self.config_mgr.get_sub_strategy_config(s_name), "execution_mode", "live"),
                    "entry_limit_mode": getattr(self.config_mgr.get_sub_strategy_config(s_name), "entry_limit_mode", "contracts"),
                    "allocation_contracts": getattr(self.config_mgr.get_sub_strategy_config(s_name), "allocation_contracts", 2 if s_name in ("TG", "PC2", "Human Alerters") else 1),
                    "allocation_amount": self.config_mgr.get_sub_strategy_config(s_name).allocation_amount,
                    "auto_take_trades": self.config_mgr.get_sub_strategy_config(s_name).auto_take_trades,
                    "max_premium_pct": self.config_mgr.get_sub_strategy_config(s_name).max_premium_pct,
                    "floor_guard_pct": self.config_mgr.get_sub_strategy_config(s_name).floor_guard_pct,
                    "max_alert_price": getattr(self.config_mgr.get_sub_strategy_config(s_name), "max_alert_price", 0.0),
                    "allowed_tickers": getattr(self.config_mgr.get_sub_strategy_config(s_name), "allowed_tickers", []),
                    "roll_runner_enabled": getattr(self.config_mgr.get_sub_strategy_config(s_name), "roll_runner_enabled", False),
                    "roll_runner_qty": getattr(self.config_mgr.get_sub_strategy_config(s_name), "roll_runner_qty", 1),
                    "roll_runner_tolerance_pct": getattr(self.config_mgr.get_sub_strategy_config(s_name), "roll_runner_tolerance_pct", 25.0),
                    "roll_runner_every_leg": getattr(self.config_mgr.get_sub_strategy_config(s_name), "roll_runner_every_leg", False),
                    "ladder_tiers": [t.to_dict() for t in self.config_mgr.get_sub_strategy_config(s_name).ladder_tiers],
                    "active_trades_count": len([t for t in active_list if t.get("sub_strategy") in _match_set(s_name)]),
                    "open_trades_count": len([t for t in active_list if t.get("sub_strategy") in _match_set(s_name)]),
                    "today_signals_count": len([s for s in today_signals if s.get("sub_strategy") in _match_set(s_name)]),
                    "tracked_alerts_count": len([
                        s for s in self.signals
                        if getattr(s, "sub_strategy", "") in _match_set(s_name)
                        and s.status == "TRACKING"
                        and (s.date_cst == today_cst or (s.received_at_cst and s.received_at_cst.startswith(today_cst)))
                    ]),
                    "today_realized_pnl_usd": round(
                        sum(t.get("realized_pnl_usd", 0.0) for t in today_closed if t.get("sub_strategy") in _match_set(s_name))
                        + sum(t.get("realized_pnl_usd", 0.0) for t in active_list if t.get("sub_strategy") in _match_set(s_name)),
                        2,
                    ),
                    "total_unrealized_pnl_usd": round(
                        sum(t.get("unrealized_pnl_usd", 0.0) for t in active_list if t.get("sub_strategy") in _match_set(s_name)),
                        2,
                    ),
                }
                for s_name in ("TG", "PC", "PC2", "Cowboy", "Genie", "AI FlowAlgos", "Others", "Human Alerters")
            },
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
                "today_live_realized_pnl_usd": round(today_live_realized, 2),
                "today_paper_realized_pnl_usd": round(today_paper_realized, 2),
                "total_realized_pnl_usd": round(total_realized, 2),
                "total_live_realized_pnl_usd": round(total_live_realized, 2),
                "total_paper_realized_pnl_usd": round(total_paper_realized, 2),
                "total_unrealized_pnl_usd": round(total_unrealized, 2),
                "live_unrealized_pnl_usd": round(live_unrealized, 2),
                "paper_unrealized_pnl_usd": round(paper_unrealized, 2),
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

    def reload_trades(self) -> int:
        """Reload trades from trades journal file into memory."""
        with self._lock:
            if os.path.exists(self._trades_journal_path):
                trades_by_id: Dict[str, CloneTrade] = {}
                with open(self._trades_journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            trade_dict = json.loads(line)
                            trade = CloneTrade.from_dict(trade_dict)
                            if trade.id in trades_by_id:
                                prev = trades_by_id[trade.id]
                                if getattr(prev, "highest_price", 0.0) > getattr(trade, "highest_price", 0.0):
                                    trade.highest_price = prev.highest_price
                                    trade.highest_profit_pct = getattr(prev, "highest_profit_pct", 0.0)
                                if getattr(prev, "lowest_price", 0.0) > 0 and (not hasattr(trade, "lowest_price") or trade.lowest_price <= 0 or prev.lowest_price < trade.lowest_price):
                                    trade.lowest_price = prev.lowest_price
                                if getattr(prev, "realized_pnl_usd", 0.0) != 0.0 and trade.realized_pnl_usd == 0.0:
                                    trade.realized_pnl_usd = prev.realized_pnl_usd
                            if trade.realized_pnl_usd == 0.0 and trade.closed_legs:
                                sum_legs = sum(getattr(l, "pnl_usd", 0.0) for l in trade.closed_legs)
                                if sum_legs != 0.0:
                                    trade.realized_pnl_usd = round(sum_legs, 2)
                            trades_by_id[trade.id] = trade
                        except Exception as e:
                            log.warning("Skipping corrupted trade line: %s", e)

                today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                self.active_trades.clear()
                self.closed_trades.clear()
                for trade in trades_by_id.values():
                    if trade.status == "OPEN" or trade.status == "WORKING":
                        self.active_trades[trade.id] = trade
                    else:
                        self.closed_trades.append(trade)
            return len(self.active_trades) + len(self.closed_trades)

    def reload_all(self) -> Dict[str, int]:
        s_count = self.reload_signals()
        t_count = self.reload_trades()
        return {"signals_count": s_count, "trades_count": t_count}

    def reload_signals(self) -> int:
        """Reload signals from signals journal file into memory."""
        with self._lock:
            if os.path.exists(self._signals_journal_path):
                signals_by_id: Dict[str, CloneSignal] = {}
                with open(self._signals_journal_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            sig = CloneSignal.from_dict(json.loads(line))
                            if sig.id in signals_by_id:
                                prev = signals_by_id[sig.id]
                                if prev.peak_price is not None:
                                    sig.peak_price = max(sig.peak_price or 0.0, prev.peak_price)
                                if prev.bottom_price is not None and prev.bottom_price > 0:
                                    if sig.bottom_price is None or sig.bottom_price <= 0:
                                        sig.bottom_price = prev.bottom_price
                                    else:
                                        sig.bottom_price = min(sig.bottom_price, prev.bottom_price)
                            signals_by_id[sig.id] = sig
                        except Exception as e:
                            log.warning("Skipping corrupted signal line: %s", e)
                self.signals = list(signals_by_id.values())
            return len(self.signals)

    def delete_today_alt_signals(self, target_date: Optional[str] = None) -> int:
        """Remove flow alternative companion signals generated for target date (default today CST)."""
        if not target_date:
            try:
                from meic0dte.app.utilities import central_now
                target_date = central_now().strftime("%Y-%m-%d")
            except Exception:
                target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._lock:
            alts_to_delete = {
                s.id for s in self.signals
                if (s.date_cst == target_date or (s.received_at_cst and s.received_at_cst.startswith(target_date)))
                and (s.is_flow_alt or s.id.endswith("_alt"))
            }
            if not alts_to_delete:
                return 0

            self.signals = [s for s in self.signals if s.id not in alts_to_delete]
            for s in self.signals:
                if s.alt_signal_id in alts_to_delete:
                    s.alt_signal_id = None

            try:
                with open(self._signals_journal_path, "w", encoding="utf-8") as f:
                    for s in self.signals:
                        f.write(json.dumps(s.to_dict()) + "\n")
            except Exception as exc:
                log.error("Failed to rewrite signals journal after deleting alts: %s", exc)

            return len(alts_to_delete)

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
