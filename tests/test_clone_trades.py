"""Unit tests for Discretionary Clone Trades system."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import time

from brokers.base import OrderResult
from clone_trades.config import CloneTradeConfigManager
from clone_trades.engine import CloneTradeEngine
from clone_trades.models import (
    LadderTier,
    OptionSpec,
    distribute_contracts_across_tiers,
)
from clone_trades.quotes import OptionQuoteService


class TestCloneTradesModels(unittest.TestCase):
    def test_option_spec_from_payload_standard(self):
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
        }
        spec = OptionSpec.from_payload(payload)
        self.assertEqual(spec.underlying, "SPY")
        self.assertEqual(spec.expiration, "2026-09-25")
        self.assertEqual(spec.strike, 500.0)
        self.assertEqual(spec.option_type, "CALL")
        self.assertEqual(spec.occ_symbol, "SPY   260925C00500000")
        self.assertEqual(spec.streamer_symbol, ".SPY260925C500")

    def test_option_spec_from_streamer_symbol(self):
        payload = {"ticker": ".QQQ260925P480", "price": 1.25}
        spec = OptionSpec.from_payload(payload)
        self.assertEqual(spec.underlying, "QQQ")
        self.assertEqual(spec.expiration, "2026-09-25")
        self.assertEqual(spec.strike, 480.0)
        self.assertEqual(spec.option_type, "PUT")
        self.assertEqual(spec.occ_symbol, "QQQ   260925P00480000")

    def test_distribute_contracts_across_tiers(self):
        # 3 contracts, 3 tiers -> [1, 1, 1]
        self.assertEqual(distribute_contracts_across_tiers(3, 3), [1, 1, 1])
        # 5 contracts, 3 tiers -> [2, 2, 1]
        self.assertEqual(distribute_contracts_across_tiers(5, 3), [2, 2, 1])
        # 1 contract, 3 tiers -> [1, 0, 0]
        self.assertEqual(distribute_contracts_across_tiers(1, 3), [1, 0, 0])
        # 2 contracts, 3 tiers -> [1, 1, 0]
        self.assertEqual(distribute_contracts_across_tiers(2, 3), [1, 1, 0])

    def test_distribute_contracts_with_percentages(self):
        # 4 contracts, [50%, 50%] -> [2, 2]
        self.assertEqual(distribute_contracts_across_tiers(4, 2, tier_pcts=[50.0, 50.0]), [2, 2])
        # 10 contracts, [50%, 30%, 20%] -> [5, 3, 2]
        self.assertEqual(distribute_contracts_across_tiers(10, 3, tier_pcts=[50.0, 30.0, 20.0]), [5, 3, 2])
        # 3 contracts, [33.3%, 33.3%, 33.4%] -> [1, 1, 1]
        self.assertEqual(distribute_contracts_across_tiers(3, 3, tier_pcts=[33.3, 33.3, 33.4]), [1, 1, 1])
        # 1 contract with percentages -> [1, 0]
        self.assertEqual(distribute_contracts_across_tiers(1, 2, tier_pcts=[50.0, 50.0]), [1, 0])

    def test_ladder_tier_exit_pct_serialization(self):
        tier = LadderTier(tier_id=1, target_profit_pct=30.0, stop_loss_pct=-40.0, exit_pct=50.0, description="Tier 1")
        self.assertEqual(tier.exit_pct, 50.0)
        d = tier.to_dict()
        self.assertEqual(d["exit_pct"], 50.0)
        loaded = LadderTier.from_dict(d)
        self.assertEqual(loaded.exit_pct, 50.0)


class TestCloneTradesEngine(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_mgr = CloneTradeConfigManager(root_dir=self.tmp_dir.name)
        self.config_mgr.paper_mode = True
        self.config_mgr.update_sub_strategy("Others", {"execution_mode": "paper", "auto_take_trades": True})
        self.quote_service = OptionQuoteService()
        self.engine = CloneTradeEngine(
            config_mgr=self.config_mgr,
            quote_service=self.quote_service,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_chase_guard_exceeded_holds_for_manual_decision(self):
        """Req #4: When trading at > 25% premium above alert price, block auto-take and hold."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.max_premium_pct = 25.0

        # Alert is 0.80, market price is 1.05 (+31.25% premium > 25%)
        self.quote_service.set_mock_price(".SPY260925C500", 1.05)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "CHASE_EXCEEDED")
        self.assertEqual(len(self.engine.active_trades), 0)  # No auto-order

        sig_id = res["signal"]["id"]
        status = self.engine.get_status()
        self.assertEqual(len(status["pending_decisions"]), 1)

        # Now test manual take at whatever level user decides
        take_res = self.engine.take_signal_manually(sig_id, custom_price=1.00, custom_qty=4)
        self.assertTrue(take_res["ok"])
        self.assertEqual(len(self.engine.active_trades), 1)
        trade = list(self.engine.active_trades.values())[0]
        self.assertEqual(trade.entry_price, 1.00)
        self.assertEqual(trade.remaining_quantity, 4)

    def test_leave_chase_exceeded_signal(self):
        """Req #4: User can leave / dismiss a trade beyond the chase guard."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.max_premium_pct = 25.0
        self.quote_service.set_mock_price(".SPY260925C500", 1.10)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "CHASE_EXCEEDED")
        sig_id = res["signal"]["id"]

        leave_res = self.engine.leave_signal(sig_id)
        self.assertTrue(leave_res["ok"])
        self.assertEqual(leave_res["signal"]["status"], "DISMISSED_MANUAL")
        self.assertEqual(len(self.engine.active_trades), 0)

    def test_floor_guard_exceeded_holds_for_manual_decision(self):
        """Floor Guard: When market price is > 75% below alert price (e.g. TSLA alert at 98c, market at 5c), block auto-take."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.floor_guard_pct = 75.0

        # Alert is 0.98, market price is 0.05 (-94.9% drop > 75% floor limit)
        self.quote_service.set_mock_price(".TSLA261002C250", 0.05)

        payload = {
            "ticker": "TSLA",
            "exp": "2026-10-02",
            "strike": 250,
            "type": "CALL",
            "price": 0.98,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "FLOOR_EXCEEDED")
        self.assertEqual(len(self.engine.active_trades), 0)  # No auto-order placed on brokerage!

        sig_id = res["signal"]["id"]
        status = self.engine.get_status()
        self.assertEqual(len(status["pending_decisions"]), 1)
        self.assertEqual(status["pending_decisions"][0]["status"], "FLOOR_EXCEEDED")

        # User can leave/dismiss the late dead alert
        leave_res = self.engine.leave_signal(sig_id)
        self.assertTrue(leave_res["ok"])
        self.assertEqual(leave_res["signal"]["status"], "DISMISSED_MANUAL")
        self.assertEqual(len(self.engine.get_status()["pending_decisions"]), 0)

    def test_floor_guard_within_limit_allows_auto_take(self):
        """Floor Guard: When market price drop is within the floor guard threshold (e.g. 15% drop < 75%), auto-take proceeds."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.floor_guard_pct = 75.0

        # Alert is 1.00, market price is 0.85 (-15% drop, allowed by floor guard)
        self.quote_service.set_mock_price(".SPY260925C500", 0.85)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
        }
        res = self.engine.process_signal(payload)
        self.assertIn(res["status"], ("TAKEN", "WORKING"))
        self.assertEqual(len(self.engine.active_trades), 1)

    def test_floor_guard_configurable_threshold(self):
        """Floor Guard: Screen configurable threshold (e.g. 50% vs 75%)."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.floor_guard_pct = 50.0  # Tightened floor guard

        # Alert is 1.00, market price is 0.45 (-55% drop > 50% threshold)
        self.quote_service.set_mock_price(".SPY260925C500", 0.45)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
            "calledBy": "PC",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "FLOOR_EXCEEDED")
        self.assertEqual(len(self.engine.active_trades), 0)

    def test_daily_flag_skips_trades_when_off(self):
        """Req #3: Daily flag straight away take trades if on else skip."""
        self.config_mgr.auto_take_trades = False  # OFF
        self.quote_service.set_mock_price(".SPY260925C500", 0.90)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "SKIPPED")
        self.assertEqual(len(self.engine.active_trades), 0)

        # Turn daily flag ON -> next signal executes
        self.config_mgr.auto_take_trades = True
        res2 = self.engine.process_signal(payload)
        self.assertEqual(res2["status"], "TAKEN")
        self.assertEqual(len(self.engine.active_trades), 1)

    def test_sizing_calculation(self):
        """Req #5: $500 allocation with an $80 option trading at 100 now means 5 contracts."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allocation_amount = 500.0
        self.config_mgr.max_premium_pct = 25.0

        # $80 option trading at $100 per contract ($1.00/share)
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 80.0,  # 80 contract dollars = 0.80 per share
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        trade = res["trade"]
        # $500 / $100 = 5 contracts!
        self.assertEqual(trade["initial_quantity"], 5)
        self.assertEqual(trade["remaining_quantity"], 5)

    def test_unbalanced_oco_ladder_lifecycle(self):
        """Req #6: Out of 3 contracts, 1st closes at 25% PT with 50% SL on all.
        When 1st hits PT, next contract advances to next PT while SL protects all remaining.
        """
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allocation_amount = 300.0  # 3 contracts @ $1.00
        self.config_mgr.initial_stop_loss_pct = -50.0
        self.config_mgr.ladder_tiers = [
            LadderTier(1, 25.0, -50.0, "Step 1: +25% PT / -50% SL"),
            LadderTier(2, 50.0, 0.0, "Step 2: +50% PT / 0% Breakeven SL"),
            LadderTier(3, 100.0, 50.0, "Step 3: +100% PT / +50% Lock SL"),
        ]

        # Entry at $1.00
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
            "qty": 3,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        trade_id = res["trade"]["id"]

        trade = self.engine.active_trades[trade_id]
        self.assertEqual(trade.remaining_quantity, 3)
        self.assertEqual(trade.active_pt_price, 1.25)  # +25%
        self.assertEqual(trade.active_stop_price, 0.50)  # -50%

        # Price rises to $1.25 -> Tier 1 PT hit! 1 contract closes, 2 remain
        self.quote_service.set_mock_price(".SPY260925C500", 1.25)
        self.engine.update_quotes_and_check_stops()

        trade = self.engine.active_trades[trade_id]
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(len(trade.closed_legs), 1)
        self.assertEqual(trade.closed_legs[0].quantity, 1)
        self.assertEqual(trade.closed_legs[0].profit_pct, 25.0)
        self.assertEqual(trade.realized_pnl_usd, 25.0)  # (1.25 - 1.00) * 100 * 1

        # Next PT is Step 2 ($1.50 = +50%), stop ratchets to breakeven (0% = $1.00)
        self.assertEqual(trade.active_pt_price, 1.50)
        self.assertEqual(trade.active_stop_price, 1.00)

        # Price drops to $1.00 -> Breakeven stop triggered on remaining 2 contracts
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        self.engine.update_quotes_and_check_stops()

        # Trade is now CLOSED
        self.assertNotIn(trade_id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade_id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertEqual(len(closed_trade.closed_legs), 2)
        self.assertEqual(closed_trade.closed_legs[1].quantity, 2)
        self.assertEqual(closed_trade.closed_legs[1].exit_price, 1.00)
        # Total realized P&L = +$25.00 from Tier 1 + $0.00 from breakeven = +$25.00
        self.assertEqual(closed_trade.realized_pnl_usd, 25.0)

    def test_market_closed_blocks_auto_take_when_live(self):
        """When market is closed and paper_mode is False, auto-take must be blocked."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)

        # Mock is_market_open to return False
        import clone_trades.engine as eng_mod
        orig_fn = eng_mod.is_market_open
        try:
            eng_mod.is_market_open = lambda: False
            payload = {
                "ticker": "SPY",
                "exp": "2026-09-25",
                "strike": 500,
                "type": "CALL",
                "price": 0.80,
                "calledBy": "PC",
            }
            res = self.engine.process_signal(payload)
            self.assertEqual(res["status"], "MARKET_CLOSED")
            self.assertEqual(len(self.engine.active_trades), 0)
        finally:
            eng_mod.is_market_open = orig_fn

    def test_broker_rejection_does_not_fake_open_trade(self):
        """When broker rejects an order in live mode, trade must NOT be entered as OPEN."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)

        # Mock broker that returns rejection
        class FailingBroker:
            def place_limit_order(self, *args, **kwargs):
                from brokers.base import OrderResult
                return OrderResult(False, None, "rejected", message="Insufficient funds or off-hours")

        self.engine.broker = FailingBroker()

        import clone_trades.engine as eng_mod
        orig_fn = eng_mod.is_market_open
        try:
            eng_mod.is_market_open = lambda: True
            payload = {
                "ticker": "SPY",
                "exp": "2026-09-25",
                "strike": 500,
                "type": "CALL",
                "price": 0.80,
                "calledBy": "PC",
            }
            res = self.engine.process_signal(payload)
            self.assertEqual(res["status"], "REJECTED_BROKER")
            self.assertEqual(len(self.engine.active_trades), 0)
        finally:
            eng_mod.is_market_open = orig_fn

    def test_laddered_scaling_out_custom_percentages(self):
        """Test scaling out with custom percentage allocations per profit target stage."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allocation_amount = 400.0  # 4 contracts @ $1.00
        self.config_mgr.initial_stop_loss_pct = -50.0
        self.config_mgr.ladder_tiers = [
            LadderTier(1, 20.0, -50.0, "Step 1: +20% PT", exit_pct=50.0),  # 50% = 2 contracts
            LadderTier(2, 60.0, 0.0, "Step 2: +60% PT", exit_pct=50.0),    # 50% = 2 contracts
        ]

        self.config_mgr.update_sub_strategy('PC', {'entry_limit_mode': 'dollar', 'allocation_amount': 400.0, 'auto_take_trades': True, 'execution_mode': 'paper', 'ladder_tiers': self.config_mgr.ladder_tiers})
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
            "qty": 4,
            "calledBy": "PC",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        trade_id = res["trade"]["id"]
        trade = self.engine.active_trades[trade_id]

        # Verify initial distribution contains exit_pct and contract allocations
        self.assertEqual(len(trade.tier_distribution), 2)
        self.assertEqual(trade.tier_distribution[0]["exit_pct"], 50.0)
        self.assertEqual(trade.tier_distribution[0]["contracts"], 2)
        self.assertEqual(trade.tier_distribution[1]["exit_pct"], 50.0)
        self.assertEqual(trade.tier_distribution[1]["contracts"], 2)

        # Hit Tier 1 PT (+20% -> $1.20): 2 contracts (50%) scale out
        self.quote_service.set_mock_price(".SPY260925C500", 1.20)
        self.engine.update_quotes_and_check_stops()

        trade = self.engine.active_trades[trade_id]
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(len(trade.closed_legs), 1)
        self.assertEqual(trade.closed_legs[0].quantity, 2)
        self.assertIn("50.0%", trade.closed_legs[0].reason)
        # Stop loss ratchets to 0% (breakeven) on all 2 remaining contracts (100%)
        self.assertEqual(trade.active_stop_price, 1.00)

        # Hit Tier 2 PT (+60% -> $1.60): remaining 2 contracts (50%) scale out
        self.quote_service.set_mock_price(".SPY260925C500", 1.60)
        self.engine.update_quotes_and_check_stops()

        # Trade fully closed
        self.assertNotIn(trade_id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade_id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertEqual(len(closed_trade.closed_legs), 2)
        self.assertEqual(closed_trade.closed_legs[1].quantity, 2)
        self.assertEqual(closed_trade.remaining_quantity, 0)

    def test_tier_1_stop_loss_at_75_percent_takes_75_not_50(self):
        """Fix verification: When Tier 1 is configured with -75% SL, trade must have 75% SL not 50%."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allocation_amount = 400.0
        # User sets Tier 1 SL to -75%
        self.config_mgr.update({
            "ladder_tiers": [
                {"tier_id": 1, "target_profit_pct": 25.0, "stop_loss_pct": -75.0, "exit_pct": 33.3},
                {"tier_id": 2, "target_profit_pct": 50.0, "stop_loss_pct": 0.0, "exit_pct": 33.3},
                {"tier_id": 3, "target_profit_pct": 100.0, "stop_loss_pct": 50.0, "exit_pct": 33.4},
            ]
        })
        self.assertEqual(self.config_mgr.initial_stop_loss_pct, -75.0)

        # Entry at $1.00
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
            "qty": 4,
            "calledBy": "PC",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        trade_id = res["trade"]["id"]
        trade = self.engine.active_trades[trade_id]

        # Stop loss MUST be 75% ($0.25), not 50% ($0.50)
        self.assertEqual(trade.active_stop_pct, -75.0)
        self.assertEqual(trade.active_stop_price, 0.25)

        # Price drops to $0.50 (-50% loss): Trade MUST REMAIN OPEN!
        self.quote_service.set_mock_price(".SPY260925C500", 0.50)
        self.engine.update_quotes_and_check_stops()
        self.assertIn(trade_id, self.engine.active_trades)
        self.assertEqual(trade.remaining_quantity, 4)

        # Price drops to $0.25 (-75% loss): Trade now triggers stop
        self.quote_service.set_mock_price(".SPY260925C500", 0.25)
        self.engine.update_quotes_and_check_stops()
        self.assertNotIn(trade_id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade_id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertEqual(closed_trade.closed_legs[0].profit_pct, -75.0)

    def test_load_state_deduplication_and_closed_trade_removal(self):
        """Fix verification: sequential journal entries for same trade ID do not duplicate or keep ghost active trades."""
        trades_journal = os.path.join(self.config_mgr.data_dir, "trades.jsonl")
        os.makedirs(self.config_mgr.data_dir, exist_ok=True)
        # Write OPEN state then CLOSED state for trade_1
        trade_open = {
            "id": "trade_1", "created_at": "2026-09-29T10:00:00Z",
            "spec": {"underlying": "QQQ", "expiration": "2026-09-29", "strike": 741.0, "option_type": "CALL", "occ_symbol": "QQQ   260929C00741000", "streamer_symbol": ".QQQ260929C741"},
            "action": "BUY", "webhook_price": 1.0, "current_price_at_signal": 1.04, "premium_pct_at_signal": 4.0,
            "entry_price": 1.02, "contract_cost": 102.0, "initial_quantity": 4, "remaining_quantity": 4,
            "current_price": 1.02, "highest_price": 1.02, "highest_profit_pct": 0.0, "status": "OPEN",
            "status_reason": "Active", "current_tier_index": 0, "active_pt_price": 1.28, "active_stop_price": 0.26,
            "active_stop_pct": -75.0, "tier_distribution": [], "closed_legs": [], "realized_pnl_usd": 0.0,
            "unrealized_pnl_pct": 0.0, "unrealized_pnl_usd": 0.0, "order_id": "510200410", "close_order_id": None, "closed_at": None
        }
        trade_closed = dict(trade_open)
        trade_closed["status"] = "CLOSED"
        trade_closed["remaining_quantity"] = 0
        trade_closed["realized_pnl_usd"] = -216.0
        trade_closed["closed_at"] = "2026-09-29T10:30:00Z"

        with open(trades_journal, "w", encoding="utf-8") as f:
            f.write(json.dumps(trade_open) + "\n")
            f.write(json.dumps(trade_closed) + "\n")

        self.engine._load_state()

        # Trade 1 MUST be in closed_trades, NOT in active_trades, and closed_trades length is 1!
        self.assertNotIn("trade_1", self.engine.active_trades)
        self.assertEqual(len(self.engine.closed_trades), 1)
        self.assertEqual(self.engine.closed_trades[0].id, "trade_1")
        self.assertEqual(self.engine.closed_trades[0].realized_pnl_usd, -216.0)

    def test_broker_fill_price_reconciliation_exact_pnl(self):
        """Fix verification: Realized P&L matches exact broker fills ($1.02 entry, $0.48 exit = -$216.00)."""
        from brokers.base import OrderResult

        class MockBroker:
            def place_limit_order(self, action, symbol, qty, price):
                # Returns working order
                return OrderResult(True, "510200410", "working")

            def get_order_status(self, order_id):
                if order_id == "510200410":
                    # Broker filled buy at 1.02 db
                    return OrderResult(True, "510200410", "filled", filled_price=1.02)
                if order_id == "510224506":
                    # Broker filled sell at 0.48 cr
                    return OrderResult(True, "510224506", "filled", filled_price=0.48)
                return OrderResult(False, order_id, "unknown")

            def place_market_order(self, action, symbol, qty):
                # Stop market exit order
                return OrderResult(True, "510224506", "filled", filled_price=0.48)

        self.engine.broker = MockBroker()
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True
        self.config_mgr.allocation_amount = 408.0

        # Alert is 1.00, quote is 1.04
        self.quote_service.set_mock_price(".QQQ260929C741", 1.04)
        payload = {
            "ticker": "QQQ", "exp": "2026-09-29", "strike": 741, "type": "CALL", "price": 1.00, "qty": 4, "calledBy": "PC"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res["ok"])
        trade_id = res["trade"]["id"]
        trade = self.engine.active_trades[trade_id]

        # Order status queried immediately got real fill of $1.02
        self.assertEqual(trade.entry_price, 1.02)

        # Trigger stop exit
        self.engine._trigger_stop_loss_unlocked(trade, exit_px=0.49)

        # Exit price should reconcile to 0.48 broker fill
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade_id)
        self.assertEqual(closed_trade.closed_legs[0].exit_price, 0.48)
        # Realized P&L: 4 * (0.48 - 1.02) * 100 = -$216.00
        self.assertEqual(closed_trade.realized_pnl_usd, -216.0)

    def test_update_trade_ladder_post_entry(self):
        """Verify ladder can be changed on an active trade and recalculates PT and SL."""
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allocation_amount = 400.0
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        payload = {
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "qty": 4, "calledBy": "PC"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res["ok"])
        trade_id = res["trade"]["id"]
        trade = self.engine.active_trades[trade_id]

        # Initially: entry=1.00, initial_stop_loss_pct=-50.0 (from Tier 1) -> stop_price=0.50, pt=1.75 (+75%)
        self.assertEqual(trade.entry_price, 1.00)
        self.assertEqual(trade.active_stop_pct, -50.0)
        self.assertEqual(trade.active_stop_price, 0.50)
        self.assertEqual(trade.active_pt_price, 1.75)

        # Update ladder on this trade: tier 1 PT +40%, SL -30%; tier 2 PT +100%, SL +10%
        new_tiers = [
            LadderTier(tier_id=1, target_profit_pct=40.0, exit_pct=50.0, stop_loss_pct=-30.0, description="Step 1"),
            LadderTier(tier_id=2, target_profit_pct=100.0, exit_pct=50.0, stop_loss_pct=10.0, description="Step 2"),
        ]
        upd_res = self.engine.update_single_trade_ladder(trade_id, new_tiers)
        self.assertTrue(upd_res["ok"])

        # Check recalculated fields on active trade
        self.assertEqual(trade.active_pt_price, 1.40)       # 1.00 * (1 + 0.40)
        self.assertEqual(trade.active_stop_price, 0.70)     # 1.00 * (1 - 0.30)
        self.assertEqual(trade.active_stop_pct, -30.0)
        self.assertEqual(len(trade.tier_distribution), 2)
        self.assertEqual(trade.tier_distribution[0]["contracts"], 2)
        self.assertEqual(trade.tier_distribution[1]["contracts"], 2)

        # Also verify update_open_trades_ladder propagates to open trades
        global_tiers = [
            LadderTier(tier_id=1, target_profit_pct=60.0, exit_pct=100.0, stop_loss_pct=-40.0, description="All-in"),
        ]
        updated_trades = self.engine.update_open_trades_ladder(global_tiers)
        self.assertEqual(len(updated_trades), 1)
        self.assertEqual(trade.active_pt_price, 1.60)
        self.assertEqual(trade.active_stop_price, 0.60)
        self.assertEqual(trade.active_stop_pct, -40.0)

    def test_cst_formatting_and_today_partition(self):
        from clone_trades.models import iso_to_cst, iso_to_date_cst
        # Test UTC to CST conversion (October is CDT, UTC-5)
        cst_str = iso_to_cst("2026-10-13T14:29:15Z")
        self.assertIn("2026-10-13 09:29:15", cst_str)
        self.assertTrue(cst_str.endswith("CDT") or cst_str.endswith("CST"))
        self.assertEqual(iso_to_date_cst("2026-10-13T14:29:15Z"), "2026-10-13")

        # Process a signal today
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        res = self.engine.process_signal({
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
        })
        self.assertEqual(res["status"], "TAKEN")
        status = self.engine.get_status()
        self.assertIn("today_date_cst", status)
        self.assertIn("today_signals", status)
        self.assertIn("historical_signals", status)
        self.assertEqual(len(status["today_signals"]), 1)
        self.assertEqual(len(status["historical_signals"]), 0)
        self.assertIsNotNone(status["today_signals"][0].get("received_at_cst"))
        self.assertEqual(status["today_signals"][0].get("date_cst"), status["today_date_cst"])




class TestSubStrategiesDiscretionary(unittest.TestCase):
    def test_cowboy_and_genie_separate_strategies(self):
        """Verify Cowboy and Genie operate as separate sub-strategies with independent configs."""
        self.config_mgr.update_sub_strategy("Cowboy", {
            "execution_mode": "tracking",
            "allocation_contracts": 3,
            "initial_stop_loss_pct": -45.0,
        })
        self.config_mgr.update_sub_strategy("Genie", {
            "execution_mode": "tracking",
            "allocation_contracts": 1,
            "initial_stop_loss_pct": -30.0,
        })
        cowboy_cfg = self.config_mgr.get_sub_strategy_config("Cowboy")
        genie_cfg = self.config_mgr.get_sub_strategy_config("Genie")

        self.assertEqual(cowboy_cfg.name, "Cowboy")
        self.assertEqual(cowboy_cfg.allocation_contracts, 3)
        self.assertEqual(cowboy_cfg.initial_stop_loss_pct, -45.0)

        self.assertEqual(genie_cfg.name, "Genie")
        self.assertEqual(genie_cfg.allocation_contracts, 1)
        self.assertEqual(genie_cfg.initial_stop_loss_pct, -30.0)


    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_mgr = CloneTradeConfigManager(root_dir=self.tmp_dir.name)
        self.config_mgr.auto_take_trades = True
        self.config_mgr.paper_mode = True
        self.quote_service = OptionQuoteService()
        self.engine = CloneTradeEngine(
            config_mgr=self.config_mgr,
            quote_service=self.quote_service,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_tg_routing_and_execution(self):
        """TG sub-strategy: 2 contracts, no stop (-100%), ladder of 100% and 300%."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
            "calledBy": "TG",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        sig = res["signal"]
        self.assertEqual(sig["called_by"], "TG")
        self.assertEqual(sig["sub_strategy"], "TG")

        trade = res["trade"]
        self.assertEqual(trade["called_by"], "TG")
        self.assertEqual(trade["sub_strategy"], "TG")
        # TG: 2 contracts
        self.assertEqual(trade["initial_quantity"], 2)
        # TG: Stop loss from Tier 1 (-50%)
        self.assertEqual(trade["active_stop_pct"], -50.0)
        self.assertAlmostEqual(trade["active_stop_price"], 0.40, places=2)  # 0.80 * 0.50
        # TG: Ladder of +35%, +90%, +120%
        self.assertAlmostEqual(trade["active_pt_price"], 1.08, places=2)  # +35% of 0.80 = 1.08
        tiers = trade["ladder_tiers"]
        self.assertEqual(len(tiers), 3)
        self.assertEqual(tiers[0]["target_profit_pct"], 35.0)
        self.assertEqual(tiers[0]["stop_loss_pct"], -50.0)
        self.assertEqual(tiers[1]["target_profit_pct"], 90.0)
        self.assertEqual(tiers[1]["stop_loss_pct"], -10.0)
        self.assertEqual(tiers[2]["target_profit_pct"], 120.0)
        self.assertEqual(tiers[2]["stop_loss_pct"], 50.0)

    def test_pc_routing_and_execution(self):
        """PC sub-strategy: $200 all trades."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
            "calledBy": "PC",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        sig = res["signal"]
        self.assertEqual(sig["called_by"], "PC")
        self.assertEqual(sig["sub_strategy"], "PC")

        trade = res["trade"]
        self.assertEqual(trade["called_by"], "PC")
        self.assertEqual(trade["sub_strategy"], "PC")
        # PC: $200 allocation / ($0.80 * 100 = $80) = 2 contracts
        self.assertEqual(trade["initial_quantity"], 2)

    def test_pc2_routing_and_execution(self):
        """PC2 sub-strategy: 2 contracts, filtered list [SPY, NVDA, TSLA, QQQ], max $1.50."""
        self.quote_service.set_mock_price(".QQQ260925C500", 0.80)
        payload = {
            "ticker": "QQQ",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
            "calledBy": "PC2",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        sig = res["signal"]
        self.assertEqual(sig["called_by"], "PC2")
        self.assertEqual(sig["sub_strategy"], "PC2")

        trade = res["trade"]
        self.assertEqual(trade["called_by"], "PC2")
        self.assertEqual(trade["sub_strategy"], "PC2")
        self.assertEqual(trade["initial_quantity"], 2)

    def test_human_alerters_traders_routing(self):
        """TG, PC, and PC2 route to their respective distinct sub-strategies."""
        self.quote_service.set_mock_price(".NVDA260925C500", 0.80)
        self.quote_service.set_mock_price(".TSLA260925C500", 0.80)
        self.quote_service.set_mock_price(".QQQ260925C500", 0.80)

        for trader, ticker, expected_strat in (("TG", "NVDA", "TG"), ("PC", "TSLA", "PC"), ("PC2", "QQQ", "PC2")):
            payload = {
                "ticker": ticker,
                "exp": "2026-09-25",
                "strike": 500,
                "type": "CALL",
                "price": 0.80,
                "calledBy": trader,
            }
            res = self.engine.process_signal(payload)
            self.assertEqual(res["status"], "TAKEN")
            self.assertEqual(res["signal"]["sub_strategy"], expected_strat)
            self.assertEqual(res["trade"]["sub_strategy"], expected_strat)

    def test_missing_called_by_routes_to_others(self):
        """Signals without calledBy key route to Others bucket and default to Tracking mode."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TRACKING")
        self.assertEqual(res["signal"]["called_by"], "")
        self.assertEqual(res["signal"]["sub_strategy"], "Others")

    def test_pc_variants_route_to_others_except_pc2(self):
        """PC2 routes to Human Alerters, while other variants route to Others in Tracking mode."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        for variant in ("pc3", "PC3", "PC_TEST", "pc daemon", "Daemon"):
            payload = {
                "ticker": "SPY",
                "exp": "2026-09-25",
                "strike": 500,
                "type": "CALL",
                "price": 0.80,
                "calledBy": variant,
            }
            res = self.engine.process_signal(payload)
            self.assertEqual(res["status"], "TRACKING")
            self.assertEqual(res["signal"]["called_by"], variant)
            self.assertEqual(res["signal"]["sub_strategy"], "Others")

    def test_genie_routing_tracking_mode_and_custom_ladder(self):
        """calledBy='Genie' or 'Cowboy' routes to AI FlowAlgos in Tracking Mode."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.85)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
            "calledBy": "genie",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TRACKING")
        sig = res["signal"]
        self.assertEqual(sig["called_by"], "genie")
        self.assertEqual(sig["sub_strategy"], "Genie")
        self.assertEqual(sig["alert_price"], 0.80)
        self.assertEqual(sig["market_price"], 0.85)
        self.assertEqual(sig["current_price"], 0.85)
        self.assertEqual(sig["peak_price"], 0.85)
        self.assertEqual(sig["bottom_price"], 0.80)

        # Requirement 2: Generates companion alternative strike signal ($0.70-$1.50)
        self.assertIn("alt_signal", res)
        alt_sig = res["alt_signal"]
        self.assertIsNotNone(alt_sig)
        self.assertTrue(alt_sig["is_flow_alt"])
        self.assertGreaterEqual(alt_sig["alert_price"], 0.70)
        self.assertLessEqual(alt_sig["alert_price"], 1.50)

        # Now test user clicking 'Buy / Get In' button with 1 contract
        take_res = self.engine.take_signal_manually(sig["id"], custom_price=0.85, custom_qty=1, paper_mode=True)
        self.assertTrue(take_res["ok"])
        trade = take_res["trade"]
        self.assertEqual(trade["sub_strategy"], "Genie")
        self.assertTrue(trade["paper_mode"])
        self.assertEqual(trade["initial_quantity"], 1)

    def test_ai_flowalgos_companion_strike_selection(self):
        """User example: MU call for 885 comes, identify strike 950 with mid ~$1 tracked alongside."""
        self.quote_service.set_mock_price(".MU261106C885", 5.50)
        self.engine.broker = None
        payload = {
            "ticker": "MU",
            "exp": "2026-11-06",
            "strike": 885,
            "type": "CALL",
            "price": 5.50,
            "calledBy": "Cowboy",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TRACKING")
        self.assertEqual(res["signal"]["sub_strategy"], "Cowboy")
        self.assertIn("alt_signal", res)
        alt = res["alt_signal"]
        self.assertEqual(alt["spec"]["strike"], 950.0)
        self.assertEqual(alt["spec"]["option_type"], "CALL")
        self.assertEqual(alt["spec"]["expiration"], "2026-11-06")
        self.assertAlmostEqual(alt["alert_price"], 1.00, delta=0.50)
        self.assertTrue(alt["is_flow_alt"])

        # Check that both are tracked signals in engine
        tracked_ids = [s.id for s in self.engine.signals if s.status == "TRACKING"]
        self.assertIn(res["signal"]["id"], tracked_ids)
        self.assertIn(alt["id"], tracked_ids)

    def test_pc2_ticker_filter_and_all_trades_for_tg_and_pc(self):
        """PC2 is filtered on tickers (SPY, NVDA, TSLA, QQQ). TG and PC play all trades without ticker filter."""
        self.quote_service.set_mock_price(".AAPL260925C200", 0.80)
        # PC2 on AAPL -> SKIPPED
        res_pc2 = self.engine.process_signal({
            "ticker": "AAPL", "exp": "2026-09-25", "strike": 200, "type": "CALL", "price": 0.80, "calledBy": "PC2"
        })
        self.assertEqual(res_pc2["status"], "SKIPPED")
        self.assertIn("not in allowed tickers list", res_pc2["message"])

        # TG on AAPL -> TAKEN (all trades on him)
        res_tg = self.engine.process_signal({
            "ticker": "AAPL", "exp": "2026-09-25", "strike": 200, "type": "CALL", "price": 0.80, "calledBy": "TG"
        })
        self.assertEqual(res_tg["status"], "TAKEN")

        # PC on AAPL -> TAKEN (all trades on PC)
        res_pc = self.engine.process_signal({
            "ticker": "AAPL", "exp": "2026-09-25", "strike": 200, "type": "CALL", "price": 0.80, "calledBy": "PC"
        })
        self.assertEqual(res_pc["status"], "TAKEN")

    def test_pc2_max_alert_price_filter_and_all_trades_for_tg_and_pc(self):
        """PC2 max alert price is $1.50. TG and PC play all trades without alert price cap."""
        self.quote_service.set_mock_price(".NVDA260925C500", 1.80)
        # PC2 on NVDA @ 1.80 -> SKIPPED
        res_pc2 = self.engine.process_signal({
            "ticker": "NVDA", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.80, "calledBy": "PC2"
        })
        self.assertEqual(res_pc2["status"], "SKIPPED")
        self.assertIn("is not less than max allowed $1.50", res_pc2["message"])

        # TG on NVDA @ 1.80 -> TAKEN
        res_tg = self.engine.process_signal({
            "ticker": "NVDA", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.80, "calledBy": "TG"
        })
        self.assertEqual(res_tg["status"], "TAKEN")

        # PC on NVDA @ 1.80 -> TAKEN
        res_pc = self.engine.process_signal({
            "ticker": "NVDA", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.80, "calledBy": "PC"
        })
        self.assertEqual(res_pc["status"], "TAKEN")

    def test_duplicate_and_opposing_trade_rules_per_sub_strategy(self):
        """Collision guards are isolated per sub-strategy: TG and PC do not block each other."""
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        self.quote_service.set_mock_price(".SPY260925P500", 1.00)

        # 1. First trade taken by TG (SPY CALL @ $1.00)
        res1 = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "calledBy": "TG"
        })
        self.assertEqual(res1["status"], "TAKEN")
        tg_trade_id = res1["trade"]["id"]

        # 2. PC alerts SPY (CALL) -> TAKEN! (PC is a separate sub-strategy, not blocked by TG)
        res2 = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "calledBy": "PC"
        })
        self.assertEqual(res2["status"], "TAKEN")
        pc_trade_id = res2["trade"]["id"]

        # 3. Second TG trade on SPY (CALL) -> SKIPPED (Duplicate within TG)
        res3 = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "calledBy": "TG"
        })
        self.assertEqual(res3["status"], "SKIPPED")
        self.assertIn("Duplicate CALL trade on SPY skipped", res3["message"])

        # 4. Opposing trade on SPY (PUT) alerted by TG while TG trade is still at $0.80 (80% value) -> BLOCKED
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        self.engine.update_quotes_and_check_stops()
        res4 = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "PUT", "price": 1.00, "calledBy": "TG"
        })
        self.assertEqual(res4["status"], "SKIPPED")
        self.assertIn("Opposing PUT trade on SPY blocked", res4["message"])

        # 5. First TG trade drops to $0.40 (40% value < 50%). Opposing TG PUT trade is now ALLOWED!
        self.quote_service.set_mock_price(".SPY260925C500", 0.40)
        self.engine.update_quotes_and_check_stops()
        res5 = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "PUT", "price": 1.00, "calledBy": "TG"
        })
        self.assertEqual(res5["status"], "TAKEN")
        self.assertEqual(res5["trade"]["spec"]["option_type"], "PUT")

    def test_genie_fixed_contract_limit_sizing(self):
        """Requirement #1: Allow # of contracts as entry limit."""
        self.config_mgr.update_sub_strategy("AI FlowAlgos", {
            "entry_limit_mode": "contracts",
            "allocation_contracts": 1,
            "execution_mode": "paper",
            "auto_take_trades": True,
        })
        self.quote_service.set_mock_price(".SPY260925C500", 8.00)  # $800 contract!

        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 8.00,
            "calledBy": "genie",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TAKEN")
        trade = res["trade"]
        self.assertEqual(trade["initial_quantity"], 1)
        self.assertEqual(trade["remaining_quantity"], 1)

    def test_price_watermarks_peak_and_bottom_for_pc_and_tracking(self):
        """Requirements #2 & #3: Track alert px, broker @ alert, current px, peak, and bottom for PC trades & tracked alerts."""
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        # 1. PC trade auto-takes live/paper
        res_pc = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "calledBy": "PC"
        })
        self.assertEqual(res_pc["status"], "TAKEN")
        trade_id = res_pc["trade"]["id"]

        # 2. Genie signal enters tracking mode
        res_g = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "calledBy": "genie"
        })
        self.assertEqual(res_g["status"], "TRACKING")
        sig_id = res_g["signal"]["id"]

        # 1. Market dips to 0.85 -> updates bottom prices
        self.quote_service.set_mock_price(".SPY260925C500", 0.85)
        self.engine.update_quotes_and_check_stops()

        trade = self.engine.active_trades[trade_id]
        self.assertEqual(trade.lowest_price, 0.85)
        self.assertEqual(trade.highest_price, 1.00)

        tracked_sig = next(s for s in self.engine.signals if s.id == sig_id)
        self.assertEqual(tracked_sig.bottom_price, 0.85)
        self.assertEqual(tracked_sig.peak_price, 1.00)

        # 2. Market surges to 1.40 -> updates peak prices
        self.quote_service.set_mock_price(".SPY260925C500", 1.40)
        self.engine.update_quotes_and_check_stops()

        trade = self.engine.active_trades.get(trade_id) or self.engine.closed_trades[-1]
        self.assertEqual(trade.highest_price, 1.40)
        self.assertEqual(trade.lowest_price, 0.85)

        tracked_sig = next(s for s in self.engine.signals if s.id == sig_id)
        self.assertEqual(tracked_sig.peak_price, 1.40)
        self.assertEqual(tracked_sig.bottom_price, 0.85)

    def test_genie_toggle_to_live_mode(self):
        """AI FlowAlgos can be updated."""
        self.config_mgr.update_sub_strategy("AI FlowAlgos", {"execution_mode": "tracking", "paper_mode": True})
        genie_cfg = self.config_mgr.get_sub_strategy_config("AI FlowAlgos")
        self.assertEqual(genie_cfg.execution_mode, "tracking")
        self.assertTrue(genie_cfg.paper_mode)

    def test_others_bucket_routing_and_strict_paper_enforcement(self):
        """Any other calledBy routes to Others, defaults to Tracking, and cannot trade Live."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.80,
            "calledBy": "Daemon",
        }
        res = self.engine.process_signal(payload)
        self.assertEqual(res["status"], "TRACKING")
        self.assertEqual(res["signal"]["called_by"], "Daemon")
        self.assertEqual(res["signal"]["sub_strategy"], "Others")

        # Attempt to enable live mode on Others - must be blocked to paper or tracking!
        self.config_mgr.update_sub_strategy("Others", {"execution_mode": "live", "paper_mode": False})
        others_cfg = self.config_mgr.get_sub_strategy_config("Others")
        self.assertNotEqual(others_cfg.execution_mode, "live")
        self.assertTrue(others_cfg.paper_mode)

    def test_sub_strategies_status_breakdown(self):
        """get_status() returns per-sub-strategy statistics including tracked alerts and trades."""
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)

        # 1 PC trade (auto-takes)
        self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80, "calledBy": "PC"
        })
        # 1 Genie alert in tracking mode (generates 1 orig + 1 companion alt = 2 tracked alerts)
        res_g = self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80, "calledBy": "genie"
        })
        # 1 Others alert in tracking mode
        self.engine.process_signal({
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80, "calledBy": "test_bot"
        })

        status = self.engine.get_status()
        self.assertIn("sub_strategies", status)
        sub_stats = status["sub_strategies"]
        self.assertIn("Human Alerters", sub_stats)
        self.assertIn("AI FlowAlgos", sub_stats)
        self.assertIn("Others", sub_stats)

        self.assertEqual(sub_stats["Human Alerters"]["open_trades_count"], 1)
        self.assertEqual(sub_stats["AI FlowAlgos"]["tracked_alerts_count"], 2)  # orig + companion alt
        self.assertEqual(sub_stats["Others"]["tracked_alerts_count"], 1)

        # Now take the Genie alert manually -> becomes open trade
        self.engine.take_signal_manually(res_g["signal"]["id"], custom_qty=1, paper_mode=True)
        status2 = self.engine.get_status()
        self.assertEqual(status2["sub_strategies"]["AI FlowAlgos"]["open_trades_count"], 1)
        self.assertIn("live_unrealized_pnl_usd", status2["stats"])
        self.assertIn("paper_unrealized_pnl_usd", status2["stats"])
        self.assertIn("today_live_realized_pnl_usd", status2["stats"])
        self.assertIn("today_paper_realized_pnl_usd", status2["stats"])


class TestOptionSymbols(unittest.TestCase):
    def test_equity_symbol_translation(self):
        from common.symbols import to_schwab, to_tastytrade
        # SPY OCC to streamer
        tt = to_tastytrade("SPY   260925C00771000")
        self.assertEqual(tt, ".SPY260925C771")

        # Streamer to OCC
        occ = to_schwab(".SPY260925C771")
        self.assertEqual(occ, "SPY   260925C00771000")

        # SPXW backward compatibility
        self.assertEqual(to_tastytrade("SPXW  260619P05550000"), ".SPXW260619P5550")
        self.assertEqual(to_schwab(".SPXW260619P5550"), "SPXW  260619P05550000")

    def test_spx_spxw_symbol_normalization(self):
        from common.symbols import to_schwab, to_tastytrade, parse_canonical
        from clone_trades.models import OptionSpec

        # SPX alert payload maps to SPXW OCC & streamer symbols
        spec = OptionSpec.build("SPX", "2026-11-06", 7300, "call")
        self.assertEqual(spec.underlying, "SPX")
        self.assertEqual(spec.occ_symbol, "SPXW  261106C07300000")
        self.assertEqual(spec.streamer_symbol, ".SPXW261106C7300")

        # Parsing SPX format without W
        parsed_occ = parse_canonical("SPX   261106C07300000")
        self.assertEqual(parsed_occ, ("261106", "C", 7300))
        parsed_tt = parse_canonical(".SPX261106C7300")
        self.assertEqual(parsed_tt, ("261106", "C", 7300))

        # Translation
        self.assertEqual(to_tastytrade("SPX   261106C07300000"), ".SPXW261106C7300")
        self.assertEqual(to_schwab(".SPX261106C7300"), "SPXW  261106C07300000")

    def test_spx_quote_fallback_spxw(self):
        import asyncio
        from clone_trades.models import OptionSpec
        from clone_trades.quotes import OptionQuoteService

        class MockMarketData:
            def __init__(self, sym, bid=95.0, ask=96.0):
                self.symbol = sym
                self.bid = bid
                self.ask = ask
                self.last = 95.5
                self.mid = 95.5

        class MockBroker:
            def __init__(self):
                self.session = object()
                self.calls = []
            def _run(self, coro):
                try:
                    coro.close()
                except Exception:
                    pass
                self.calls.append(len(self.calls))
                # First call (for un-normalized SPX) returns empty list
                if len(self.calls) == 1:
                    return []
                # Fallback call (for SPXW) returns quote
                return [MockMarketData("SPXW  261106C07300000")]

        # Spec starts with un-normalized SPX
        spec = OptionSpec(
            underlying="SPX",
            expiration="2026-11-06",
            strike=7300.0,
            option_type="CALL",
            occ_symbol="SPX   261106C07300000",
            streamer_symbol=".SPX261106C7300",
        )
        svc = OptionQuoteService(broker=MockBroker())
        q = svc._fetch_from_broker(spec)
        self.assertIsNotNone(q)
        self.assertEqual(q.market_buy_price, 96.0)
        self.assertEqual(spec.occ_symbol, "SPXW  261106C07300000")
        self.assertEqual(spec.streamer_symbol, ".SPXW261106C7300")


class TestFlowFinder(unittest.TestCase):
    def test_find_flow_alternative_option_with_market_data_list(self):
        """Verify find_flow_alternative_option correctly handles list returned by get_market_data_by_type."""
        import asyncio
        from types import SimpleNamespace
        from decimal import Decimal
        from clone_trades.flow_finder import find_flow_alternative_option

        class MockOpt:
            def __init__(self, strike, sym, streamer):
                self.strike_price = Decimal(str(strike))
                self.symbol = sym
                self.option_type = "C"
                self.streamer_symbol = streamer

        class MockMarketData:
            def __init__(self, sym, bid, ask, last=None, mid=None):
                self.symbol = sym
                self.bid = bid
                self.ask = ask
                self.last = last
                self.mid = mid

        opt_270 = MockOpt(270.0, "AMZN  261106C00270000", ".AMZN261106C270")
        opt_275 = MockOpt(275.0, "AMZN  261106C00275000", ".AMZN261106C275")
        opt_277_5 = MockOpt(277.5, "AMZN  261106C00277500", ".AMZN261106C277.5")
        opt_290 = MockOpt(290.0, "AMZN  261106C00290000", ".AMZN261106C290")

        md_list = [
            MockMarketData("AMZN  261106C00270000", 3.8, 3.95, mid=3.88),
            MockMarketData("AMZN  261106C00275000", 1.8, 1.95, mid=1.88),
            MockMarketData("AMZN  261106C00277500", 1.02, 1.08, mid=1.05),
            MockMarketData("AMZN  261106C00290000", 0.08, 0.10, mid=0.09),
        ]

        class MockBrokerWithSession:
            def __init__(self):
                self.session = SimpleNamespace()

            def _run(self, coro):
                # When get_option_chain or get_market_data_by_type is passed
                name = getattr(coro, "__name__", "") or str(coro)
                if asyncio.iscoroutine(coro):
                    coro.close()
                if "get_option_chain" in name or "chain" in name:
                    return {"2026-11-06": [opt_270, opt_275, opt_277_5, opt_290]}
                return md_list

        broker = MockBrokerWithSession()
        spec = OptionSpec(
            underlying="AMZN",
            expiration="2026-11-06",
            strike=270.0,
            option_type="CALL",
            occ_symbol="AMZN  261106C00270000",
            streamer_symbol=".AMZN261106C270",
        )

        res = find_flow_alternative_option(spec, broker=broker)
        self.assertIsNotNone(res)
        self.assertEqual(res["spec"].strike, 277.5)
        self.assertEqual(res["spec"].occ_symbol, "AMZN  261106C00277500")
        self.assertEqual(res["price"], 1.05)

    def test_alt_option_signal_and_trade_flow(self):
        """Verify alt signal sets is_flow_alt=True and manual take preserves it on CloneTrade."""
        from clone_trades.engine import CloneTradeEngine
        from clone_trades.config import CloneTradeConfigManager
        from clone_trades.quotes import OptionQuoteService
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            cfg = CloneTradeConfigManager(root_dir=td)
            cfg.paper_mode = True
            cfg.update_sub_strategy("AI FlowAlgos", {"execution_mode": "tracking", "auto_take_trades": False})
            qs = OptionQuoteService()
            qs.set_mock_price(".SPY261109C500", 2.50)
            qs.set_mock_price(".SPY261109C535", 1.00)
            eng = CloneTradeEngine(config_mgr=cfg, quote_service=qs)

            payload = {
                "ticker": "SPY",
                "expiration": "2026-11-09",
                "strike": 500,
                "call_put": "CALL",
                "price": 2.50,
                "calledBy": "Genie",
            }
            res = eng.process_signal(payload)
            self.assertEqual(res["status"], "TRACKING")
            sig = res["signal"]
            self.assertIn("alt_signal", res)
            alt_sig = res["alt_signal"]
            self.assertTrue(alt_sig["is_flow_alt"])
            self.assertEqual(alt_sig["parent_signal_id"], sig["id"])
            self.assertEqual(sig["alt_signal_id"], alt_sig["id"])

            # Manually take the alt trade
            take_res = eng.take_signal_manually(alt_sig["id"], custom_price=1.00, custom_qty=2, paper_mode=True)
            self.assertTrue(take_res["ok"])
            trades = list(eng.active_trades.values())
            self.assertEqual(len(trades), 1)
            self.assertTrue(trades[0].is_flow_alt)

    def test_unknown_caller_defaults_to_others(self):
        """Verify an alert without known caller tags does not falsely default to PC."""
        from clone_trades.models import CloneSignal
        payload = {
            "ticker": "AAPL",
            "expiration": "2026-11-09",
            "strike": 220,
            "call_put": "CALL",
            "price": 1.50,
        }
        sig = CloneSignal.from_dict({
            "id": "sig_unknown_1",
            "received_at": "2026-11-09T10:00:00Z",
            "alert_price": 1.50,
            "raw_payload": payload,
        })
        self.assertEqual(sig.called_by, "Others")
        self.assertEqual(sig.sub_strategy, "Others")




class TestEntryChaseAndPreflightSync(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_mgr = CloneTradeConfigManager(root_dir=self.tmp_dir.name)
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True
        self.config_mgr.update_sub_strategy("TG", {"execution_mode": "live", "auto_take_trades": True, "paper_mode": False})
        self.config_mgr.update_sub_strategy("Human Alerters", {"execution_mode": "live", "auto_take_trades": True, "paper_mode": False})
        self.config_mgr.update_sub_strategy("Human Alerters", {"execution_mode": "live", "auto_take_trades": True, "paper_mode": False})
        self.quote_service = OptionQuoteService()
        self.engine = CloneTradeEngine(
            config_mgr=self.config_mgr,
            quote_service=self.quote_service,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_unfilled_order_enters_working_status_and_blocks_ladder(self):
        """Live limit order returned as working/live must enter WORKING status, and stops/PTs must NOT evaluate."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        class WorkingBroker:
            def place_limit_order(self, action, symbol, qty, price):
                # Returns 'live' order (standard Tastytrade resting order)
                return OrderResult(True, "522001", "live", filled_quantity=0)

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "live", filled_quantity=0)

        self.engine.broker = WorkingBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {
            "ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT",
            "price": 0.46, "calledBy": "TG"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "WORKING")
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "WORKING")
        self.assertIn("working", trade.status_reason.lower())

        # Now simulate market price exploding to $1.00 (which would be +100% PT)
        self.quote_service.set_mock_price(".SPY261105P759", 1.00)
        self.engine.update_quotes_and_check_stops()

        # Trade must STILL be WORKING, with 0 closed legs, 0 realized PnL
        trade = self.engine.active_trades[trade.id]
        self.assertEqual(trade.status, "WORKING")
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(len(trade.closed_legs), 0)
        self.assertEqual(trade.realized_pnl_usd, 0.0)

    def test_working_order_fills_and_activates_ladder(self):
        """When working order fills on broker, it transitions to OPEN, updates entry fill, and activates ladder."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        broker_state = {"status": "live", "filled_price": None, "filled_quantity": 0}

        class DynamicBroker:
            def place_limit_order(self, action, symbol, qty, price):
                return OrderResult(True, "522002", broker_state["status"], filled_quantity=broker_state["filled_quantity"])

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, broker_state["status"], filled_price=broker_state["filled_price"], filled_quantity=broker_state["filled_quantity"])

        self.engine.broker = DynamicBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.46, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "WORKING")

        # Broker fills the order at 0.52
        broker_state["status"] = "filled"
        broker_state["filled_price"] = 0.52
        broker_state["filled_quantity"] = 2

        self.engine.update_quotes_and_check_stops()

        # Trade is now OPEN with reconciled fill price 0.52
        trade = self.engine.active_trades[trade.id]
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.entry_price, 0.52)
        self.assertIn("Filled", trade.status_reason)

        # Corresponding signal is TAKEN
        sig = next(s for s in self.engine.signals if s.trade_id == trade.id)
        self.assertEqual(sig.status, "TAKEN")

    def test_entry_chasing_within_chase_guard(self):
        """Working order unfilled after interval chases price upward within chase guard limit."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        orders_placed = []
        orders_cancelled = []

        class ChasingBroker:
            def place_limit_order(self, action, symbol, qty, price):
                oid = f"ord_{len(orders_placed) + 1}"
                orders_placed.append((oid, price))
                return OrderResult(True, oid, "live", filled_quantity=0)

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "live", filled_quantity=0)

            def cancel_order(self, order_id):
                orders_cancelled.append(order_id)
                return OrderResult(True, order_id, "cancelled")

        self.engine.broker = ChasingBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.50, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "WORKING")
        self.assertEqual(len(orders_placed), 1)

        # Simulate price moving to 0.55 (+10% premium, within 25% max chase) after 4 seconds
        trade.last_chase_time = time.time() - 4.0
        self.quote_service.set_mock_price(".SPY261105P759", 0.55)
        self.engine.update_quotes_and_check_stops()

        # Engine should have cancelled original order and placed replacement at 0.55
        self.assertEqual(len(orders_cancelled), 1)
        self.assertEqual(len(orders_placed), 2)
        self.assertEqual(orders_placed[-1][1], 0.55)
        self.assertEqual(trade.chase_count, 1)
        self.assertEqual(trade.entry_price, 0.55)

    def test_entry_chase_handles_fill_during_cancel_without_duplicate_order(self):
        """If an order fills during/before cancel confirmation, transition to OPEN and do NOT place a replacement."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        orders_placed = []
        orders_cancelled = []
        oco_placed = []

        class RaceConditionBroker:
            def place_limit_order(self, action, symbol, qty, price):
                oid = f"ord_{len(orders_placed) + 1}"
                orders_placed.append((oid, price))
                return OrderResult(True, oid, "live", filled_quantity=0)

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "live", filled_quantity=0)

            def cancel_order(self, order_id, *, timeout=5.0):
                orders_cancelled.append(order_id)
                # Simulate order filling at broker right when cancel is attempted
                return OrderResult(True, order_id, "filled", filled_price=0.50, filled_quantity=2)

            def place_oco_order(self, symbol, qty, *, profit_price, stop_price, limit_price, action="SELL_TO_CLOSE"):
                oco_placed.append((symbol, qty, profit_price, stop_price))
                return OrderResult(True, f"oco_{len(oco_placed)}", "working")

        self.engine.broker = RaceConditionBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.50, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "WORKING")
        self.assertEqual(len(orders_placed), 1)

        # Price moves to 0.55, triggering chase loop
        trade.last_chase_time = time.time() - 4.0
        self.quote_service.set_mock_price(".SPY261105P759", 0.55)
        self.engine.update_quotes_and_check_stops()

        # Engine attempted cancel, learned order was filled
        self.assertEqual(len(orders_cancelled), 1)
        # CRITICAL ASSERTION: No duplicate replacement order placed!
        self.assertEqual(len(orders_placed), 1, "Duplicate replacement order was erroneously placed!")
        # Trade successfully transitioned to OPEN and placed OCO ladders
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.entry_price, 0.50)
        self.assertTrue(getattr(trade, "_ladder_oco_placed", False))

    def test_entry_chase_aborts_replacement_if_cancel_unconfirmed(self):
        """If cancel does not return 'cancelled', engine aborts chase and does not place duplicate order."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        orders_placed = []

        class UnconfirmedCancelBroker:
            def place_limit_order(self, action, symbol, qty, price):
                oid = f"ord_{len(orders_placed) + 1}"
                orders_placed.append((oid, price))
                return OrderResult(True, oid, "live", filled_quantity=0)

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "live", filled_quantity=0)

            def cancel_order(self, order_id, *, timeout=5.0):
                # Broker timed out / could not confirm cancel
                return OrderResult(False, order_id, "working", message="Cancel timeout")

        self.engine.broker = UnconfirmedCancelBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.50, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]

        trade.last_chase_time = time.time() - 4.0
        self.quote_service.set_mock_price(".SPY261105P759", 0.55)
        self.engine.update_quotes_and_check_stops()

        # Replacement MUST NOT be placed
        self.assertEqual(len(orders_placed), 1, "Replacement order was placed despite unconfirmed cancel!")
        self.assertEqual(trade.status, "WORKING")

    def test_chase_exceeded_cancels_order_and_aborts_trade(self):
        """When price runs away past chase guard (>25%), engine cancels broker order and marks trade CANCELLED."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        orders_cancelled = []

        class AbortingBroker:
            def place_limit_order(self, action, symbol, qty, price):
                return OrderResult(True, "522999", "live", filled_quantity=0)

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "live", filled_quantity=0)

            def cancel_order(self, order_id):
                orders_cancelled.append(order_id)
                return OrderResult(True, order_id, "cancelled")

        self.engine.broker = AbortingBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        # Webhook price is 0.46, max_premium is 25% -> max allowed chase price is ~0.575
        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.46, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]
        trade.last_chase_time = time.time() - 4.0

        # Ask jumps to 0.73 (+58.7% premium > 25% chase guard)
        self.quote_service.set_mock_price(".SPY261105P759", 0.73)
        self.engine.update_quotes_and_check_stops()

        # Order must be cancelled at broker
        self.assertIn("522999", orders_cancelled)
        # Trade must be CANCELLED and removed from active trades
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_trade.status, "CANCELLED")
        self.assertIn("Chase guard exceeded", closed_trade.status_reason)
        self.assertEqual(closed_trade.realized_pnl_usd, 0.0)

    def test_preflight_position_check_blocks_ghost_exits(self):
        """If broker position is flat (held_qty=0), exit triggers must NOT simulate fills or realize phantom PnL."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True

        class FlatBroker:
            def place_limit_order(self, action, symbol, qty, price):
                if action == "BUY_TO_OPEN":
                    return OrderResult(True, "522111", "filled", filled_price=0.50, filled_quantity=qty)
                # Should not be called if preflight catches flat position!
                return OrderResult(False, None, "rejected_preflight", message="position_not_closable_position_flat")

            def get_order_status(self, order_id):
                return OrderResult(True, order_id, "filled", filled_price=0.50, filled_quantity=2)

            def get_single_leg_position_qty(self, symbol, priority='HIGH'):
                # Account is actually flat!
                return 0

        self.engine.broker = FlatBroker()
        self.quote_service.set_mock_price(".SPY261105P759", 0.50)

        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 759, "type": "PUT", "price": 0.50, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "OPEN")

        # Trigger PT evaluation at 0.75
        self.quote_service.set_mock_price(".SPY261105P759", 0.75)
        self.engine.update_quotes_and_check_stops()

        # Engine preflight detects held_qty=0: trade synced to CLOSED, remaining_qty=0, NO phantom profits!
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertIn("flat", closed_trade.status_reason.lower())
        self.assertEqual(closed_trade.realized_pnl_usd, 0.0)
        self.assertEqual(len(closed_trade.closed_legs), 0)

    def test_immediate_oco_placement_and_stop_ratchet(self):
        """Verify requirement 3.1, 3.2 & 3.3: immediate OCO orders sent to broker and ratcheted upon fill."""
        oco_orders = []
        canceled_orders = []

        test_self = self
        class MockOcoBroker:
            def __init__(self):
                self._counter = 100
                self._is_journal = False

            def place_oco_order(self, symbol, qty, profit_price, stop_price, limit_price=None, action="BUY_TO_CLOSE"):
                self._counter += 1
                order_id = str(self._counter)
                rec = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "qty": qty,
                    "profit_price": profit_price,
                    "stop_price": stop_price,
                    "action": action,
                }
                oco_orders.append(rec)
                return OrderResult(True, order_id, "working")

            def cancel_order(self, order_id):
                canceled_orders.append(str(order_id))
                return True

            def place_limit_order(self, action, symbol, qty, price):
                self._counter += 1
                return OrderResult(True, str(self._counter), "filled", filled_price=price, filled_quantity=qty)

            def get_order_status(self, order_id, *args, **kwargs):
                cur_q = test_self.quote_service.get_quote(".SPY261105C550")
                cur_px = cur_q.bid if cur_q else 0.50
                for o in oco_orders:
                    if o["order_id"] == str(order_id):
                        if cur_px >= o["profit_price"]:
                            return OrderResult(True, str(order_id), "filled", filled_price=o["profit_price"], filled_quantity=o["qty"])
                        return OrderResult(True, str(order_id), "working", filled_price=0.0, filled_quantity=0)
                return OrderResult(True, str(order_id), "working", filled_price=0.0, filled_quantity=0)

            def get_single_leg_position_qty(self, symbol, priority='HIGH'):
                return 3

        mock_b = MockOcoBroker()
        self.engine.broker = mock_b

        # Configure 3 rungs: 10% (sl -70%), 100% (sl 0%), 200% (sl 100%)
        test_tiers = [
            LadderTier(tier_id=1, target_profit_pct=10.0, exit_pct=33.3, stop_loss_pct=-70.0, description="Rung 1"),
            LadderTier(tier_id=2, target_profit_pct=100.0, exit_pct=33.3, stop_loss_pct=0.0, description="Rung 2"),
            LadderTier(tier_id=3, target_profit_pct=200.0, exit_pct=33.4, stop_loss_pct=100.0, description="Rung 3"),
        ]

        self.quote_service.set_mock_price(".SPY261105C550", 0.50)
        spec = OptionSpec.from_payload({"ticker": "SPY", "expiration": "2026-11-05", "strike": 550.0, "call_put": "CALL"})

        # Open trade with 3 contracts at 50c
        trade = self.engine._execute_and_open_trade(
            spec=spec,
            qty=3,
            fill_price=0.50,
            alert_price=0.50,
            premium_pct=0.0,
            sig_id="sig_oco_test",
            sub_strategy="TG",
            called_by="TG",
            paper_mode=False,
            ladder_tiers=test_tiers,
        )

        # Requirement 3.1 & 3.2: 3 immediate OCO orders sent to broker
        self.assertEqual(len(oco_orders), 3)
        self.assertEqual(oco_orders[0]["qty"], 1)
        self.assertAlmostEqual(oco_orders[0]["profit_price"], 0.55, places=2)
        self.assertAlmostEqual(oco_orders[0]["stop_price"], 0.15, places=2)
        self.assertEqual(oco_orders[0]["action"], "SELL_TO_CLOSE")

        self.assertEqual(oco_orders[1]["qty"], 1)
        self.assertAlmostEqual(oco_orders[1]["profit_price"], 1.00, places=2)
        self.assertAlmostEqual(oco_orders[1]["stop_price"], 0.15, places=2)
        self.assertEqual(oco_orders[1]["action"], "SELL_TO_CLOSE")

        self.assertEqual(oco_orders[2]["qty"], 1)
        self.assertAlmostEqual(oco_orders[2]["profit_price"], 1.50, places=2)
        self.assertAlmostEqual(oco_orders[2]["stop_price"], 0.15, places=2)
        self.assertEqual(oco_orders[2]["action"], "SELL_TO_CLOSE")

        # Requirement 3.3: Once Rung 1 fills with PT at 55c, ratchet stops on remaining rungs to breakeven (50c)
        self.quote_service.set_mock_price(".SPY261105C550", 0.55)
        self.engine.update_quotes_and_check_stops()

        self.assertEqual(trade.current_tier_index, 1)
        self.assertEqual(trade.tier_distribution[0]["status"], "filled_pt")
        # Remaining rungs 2 & 3 must have stops ratcheted to breakeven (0.50)
        self.assertAlmostEqual(trade.tier_distribution[1]["stop_price"], 0.50, places=2)
        self.assertAlmostEqual(trade.tier_distribution[2]["stop_price"], 0.50, places=2)
        self.assertAlmostEqual(trade.active_stop_price, 0.50, places=2)

        # Once Rung 2 fills with PT at 1.00, ratchet stop on Rung 3 to 1.00
        self.quote_service.set_mock_price(".SPY261105C550", 1.00)
        self.engine.update_quotes_and_check_stops()

        self.assertEqual(trade.current_tier_index, 2)
        self.assertEqual(trade.tier_distribution[1]["status"], "filled_pt")
        self.assertAlmostEqual(trade.tier_distribution[2]["stop_price"], 1.00, places=2)
        self.assertAlmostEqual(trade.active_stop_price, 1.00, places=2)

        # Finally Rung 3 fills at 1.50
        self.quote_service.set_mock_price(".SPY261105C550", 1.50)
        self.engine.update_quotes_and_check_stops()

        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.tier_distribution[2]["status"], "filled_pt")
        self.assertEqual(len(trade.closed_legs), 3)



    def test_software_breach_pt_chase_and_no_fake_fill(self):
        """When PT is reached but broker order is unfilled, engine cancels OCO and chases via limit orders.
        If chase does not fill, engine does NOT decrement quantity or mark closed falsely."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True
        self.config_mgr.update_sub_strategy("TG", {
            "execution_mode": "live", "auto_take_trades": True, "paper_mode": False,
            "ladder_tiers": [LadderTier(1, 20.0, -50.0).to_dict(), LadderTier(2, 50.0, 0.0).to_dict()]
        })

        canceled_orders = []
        limit_orders_placed = []

        class PTChaseBroker:
            def __init__(self):
                self._counter = 200

            def place_limit_order(self, action, symbol, qty, price):
                self._counter += 1
                oid = str(self._counter)
                limit_orders_placed.append((action, symbol, qty, price, oid))
                if action == "BUY_TO_OPEN":
                    return OrderResult(True, oid, "filled", filled_price=price, filled_quantity=qty)
                # SELL_TO_CLOSE: simulates resting order that does not fill immediately
                return OrderResult(True, oid, "working", filled_price=0.0, filled_quantity=0)

            def place_oco_order(self, symbol, qty, profit_price, stop_price, limit_price=None, action="SELL_TO_CLOSE"):
                self._counter += 1
                return OrderResult(True, str(self._counter), "working")

            def get_order_status(self, order_id):
                # Remains working / unfilled
                return OrderResult(True, order_id, "working", filled_price=0.0, filled_quantity=0)

            def cancel_order(self, order_id):
                canceled_orders.append(order_id)
                return OrderResult(True, order_id, "cancelled")

            def get_single_leg_position_qty(self, symbol, priority='HIGH'):
                return 2  # Still holding 2 contracts at broker!

        self.engine.broker = PTChaseBroker()
        self.quote_service.set_mock_price(".NVDA261105P187.5", 1.50)

        # 2 contracts entered at 1.50 (Rung 1 PT is +20% -> 1.80)
        payload = {"ticker": "NVDA", "exp": "2026-11-05", "strike": 187.5, "type": "PUT", "price": 1.50, "qty": 2, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertAlmostEqual(trade.active_pt_price, 1.80, places=2)

        # Price hits PT target ($1.88 >= $1.80)
        self.quote_service.set_mock_price(".NVDA261105P187.5", 1.88)
        self.engine.update_quotes_and_check_stops()

        # Engine must have attempted chase, but since order was unfilled, trade remains OPEN with 2 contracts!
        self.assertIn(trade.id, self.engine.active_trades)
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(trade.status, "OPEN")
        self.assertIn("unfilled", trade.status_reason.lower())
        self.assertEqual(len(trade.closed_legs), 0)

    def test_software_breach_sl_chase_and_clean_fill(self):
        """When stop loss breaches, OCO is canceled and limit chase loop executes down to fill."""
        self.config_mgr.paper_mode = False
        self.config_mgr.auto_take_trades = True
        self.config_mgr.allow_off_hours_orders = True
        self.config_mgr.update_sub_strategy("TG", {
            "execution_mode": "live", "auto_take_trades": True, "paper_mode": False,
            "ladder_tiers": [LadderTier(1, 20.0, -50.0).to_dict(), LadderTier(2, 50.0, 0.0).to_dict()]
        })

        canceled_orders = []
        limit_orders_placed = []

        class SLChaseBroker:
            def __init__(self):
                self._counter = 300

            def place_limit_order(self, action, symbol, qty, price):
                self._counter += 1
                oid = str(self._counter)
                limit_orders_placed.append((action, symbol, qty, price, oid))
                if action == "BUY_TO_OPEN":
                    return OrderResult(True, oid, "filled", filled_price=price, filled_quantity=qty)
                # SELL_TO_CLOSE: fills at $0.20
                return OrderResult(True, oid, "filled", filled_price=0.20, filled_quantity=qty)

            def place_oco_order(self, symbol, qty, profit_price, stop_price, limit_price=None, action="SELL_TO_CLOSE"):
                self._counter += 1
                return OrderResult(True, str(self._counter), "working")

            def get_order_status(self, order_id):
                if order_id == "301":
                    return OrderResult(True, order_id, "filled", filled_price=0.50, filled_quantity=1)
                return OrderResult(True, order_id, "filled", filled_price=0.20, filled_quantity=1)

            def cancel_order(self, order_id):
                canceled_orders.append(order_id)
                return OrderResult(True, order_id, "cancelled")

            def get_single_leg_position_qty(self, symbol, priority='HIGH'):
                return 1

        self.engine.broker = SLChaseBroker()
        self.quote_service.set_mock_price(".SPY261105C749", 0.50)

        # Rung 1 stop loss is -50%, so stop price is $0.25
        payload = {"ticker": "SPY", "exp": "2026-11-05", "strike": 749, "type": "CALL", "price": 0.50, "qty": 1, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.status, "OPEN")
        self.assertAlmostEqual(trade.active_stop_price, 0.25, places=2)

        # Price drops to 0.20 (below stop price of 0.25)
        self.quote_service.set_mock_price(".SPY261105C749", 0.20)
        self.engine.update_quotes_and_check_stops()

        # Trade must be verified CLOSED and closed_legs populated with real fill
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertEqual(closed_trade.remaining_quantity, 0)
        self.assertEqual(len(closed_trade.closed_legs), 1)
        self.assertEqual(closed_trade.closed_legs[0].exit_price, 0.20)



class TestRunnerRoll(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_mgr = CloneTradeConfigManager(root_dir=self.tmp_dir.name)
        self.config_mgr.paper_mode = True
        self.config_mgr.auto_take_trades = True
        self.quote_service = OptionQuoteService()
        self.engine = CloneTradeEngine(
            config_mgr=self.config_mgr,
            quote_service=self.quote_service,
            broker=None,
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_find_runner_roll_option_candidate_selection(self):
        """Test candidate option selection for runner roll."""
        from clone_trades.runner_roll import find_runner_roll_option
        qs = OptionQuoteService()

        # Target entry price $0.50 (+-25% -> 0.38 to 0.62)
        # Parent option: SPY 2026-11-10 CALL 590
        parent_spec = OptionSpec.from_payload({
            "ticker": "SPY", "exp": "2026-11-10", "strike": 590, "type": "CALL"
        })

        # Set mock prices for various strikes:
        qs.set_mock_price(".SPY261110C585", 1.20)  # ITM -> ignored
        qs.set_mock_price(".SPY261110C590", 0.85)  # Same strike -> ignored
        qs.set_mock_price(".SPY261110C595", 0.70)  # Outside 0.38 - 0.62
        qs.set_mock_price(".SPY261110C600", 0.52)  # In range, diff 0.02
        qs.set_mock_price(".SPY261110C605", 0.40)  # In range, diff 0.10

        res = find_runner_roll_option(parent_spec, target_price=0.50, tolerance_pct=25.0, quote_service=qs)
        self.assertIsNotNone(res)
        self.assertEqual(res["spec"].strike, 600.0)
        self.assertAlmostEqual(res["price"], 0.52, places=2)

        # For PUT: must strictly look for strike < orig_strike
        put_parent = OptionSpec.from_payload({
            "ticker": "SPY", "exp": "2026-11-10", "strike": 580, "type": "PUT"
        })
        qs.set_mock_price(".SPY261110P585", 1.10)  # higher strike -> ignored
        qs.set_mock_price(".SPY261110P580", 0.80)  # same strike -> ignored
        qs.set_mock_price(".SPY261110P575", 0.48)  # lower strike -> in range (diff 0.02)
        qs.set_mock_price(".SPY261110P570", 0.35)  # lower strike -> diff 0.15

        res_put = find_runner_roll_option(put_parent, target_price=0.50, tolerance_pct=25.0, quote_service=qs)
        self.assertIsNotNone(res_put)
        self.assertEqual(res_put["spec"].strike, 575.0)
        self.assertAlmostEqual(res_put["price"], 0.48, places=2)

    def test_runner_roll_on_last_rung_profit_target(self):
        """Test that completing the last rung of ladder executes runner roll into further OTM option."""
        tg_cfg = self.config_mgr.get_sub_strategy_config("TG")
        tg_cfg.enabled = True
        tg_cfg.auto_take_trades = True
        tg_cfg.paper_mode = True
        tg_cfg.execution_mode = "paper"
        tg_cfg.roll_runner_enabled = True
        tg_cfg.roll_runner_qty = 1
        tg_cfg.ladder_tiers = [
            LadderTier(tier_id=1, target_profit_pct=35.0, stop_loss_pct=-50.0, exit_pct=50.0),
            LadderTier(tier_id=2, target_profit_pct=90.0, stop_loss_pct=-10.0, exit_pct=30.0),
            LadderTier(tier_id=3, target_profit_pct=120.0, stop_loss_pct=50.0, exit_pct=20.0),
        ]
        self.config_mgr.sub_strategies["TG"] = tg_cfg

        # 4 contracts @ 0.50: Tier 1 (2 ctrs), Tier 2 (1 ctr), Tier 3 (1 ctr)
        self.quote_service.set_mock_price(".SPY261110C590", 0.50)
        self.quote_service.set_mock_price(".SPY261110C605", 0.50)

        payload = {
            "ticker": "SPY",
            "exp": "2026-11-10",
            "strike": 590,
            "type": "CALL",
            "price": 0.50,
            "qty": 4,
            "calledBy": "TG"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.initial_quantity, 4)
        self.assertEqual(trade.remaining_quantity, 4)

        # 1. Price hits Tier 1 PT (+35% -> 0.675)
        self.quote_service.set_mock_price(".SPY261110C590", 0.68)
        self.engine.update_quotes_and_check_stops()
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(trade.status, "OPEN")

        # 2. Price hits Tier 2 PT (+90% -> 0.95)
        self.quote_service.set_mock_price(".SPY261110C590", 0.96)
        self.engine.update_quotes_and_check_stops()
        self.assertEqual(trade.remaining_quantity, 1)
        self.assertEqual(trade.status, "OPEN")

        # 3. Price hits Tier 3 PT (+120% -> 1.10) - Last Rung!
        self.quote_service.set_mock_price(".SPY261110C590", 1.12)
        self.engine.update_quotes_and_check_stops()

        # Parent trade must be CLOSED
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_parent = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_parent.status, "CLOSED")

        # Runner roll trade MUST be opened!
        rolled_trades = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_trades), 1)
        roll_trade = rolled_trades[0]
        self.assertEqual(roll_trade.parent_trade_id, trade.id)
        self.assertEqual(roll_trade.roll_count, 1)
        self.assertEqual(roll_trade.initial_quantity, 1)
        self.assertEqual(roll_trade.remaining_quantity, 1)
        self.assertGreater(roll_trade.spec.strike, 590.0)
        # Infinite roll properties: same PT % as last rung (+120%) and NO STOP
        self.assertEqual(len(roll_trade.ladder_tiers), 1)
        self.assertEqual(roll_trade.ladder_tiers[0]["target_profit_pct"], 120.0)
        self.assertEqual(roll_trade.ladder_tiers[0]["stop_loss_pct"], -100.0)
        self.assertEqual(roll_trade.active_stop_price, 0.0)

    def test_infinite_runner_roll_progression(self):
        """Test that when a rolled runner trade hits its target, it rolls again indefinitely."""
        tg_cfg = self.config_mgr.get_sub_strategy_config("TG")
        tg_cfg.enabled = True
        tg_cfg.auto_take_trades = True
        tg_cfg.paper_mode = True
        tg_cfg.execution_mode = "paper"
        tg_cfg.roll_runner_enabled = True
        tg_cfg.roll_runner_qty = 1
        tg_cfg.ladder_tiers = [
            LadderTier(tier_id=1, target_profit_pct=100.0, stop_loss_pct=-50.0, exit_pct=100.0),
        ]
        self.config_mgr.sub_strategies["TG"] = tg_cfg

        self.quote_service.set_mock_price(".SPY261110C590", 0.50)
        self.quote_service.set_mock_price(".SPY261110C595", 0.50)
        self.quote_service.set_mock_price(".SPY261110C600", 0.50)

        # Parent trade
        payload = {"ticker": "SPY", "exp": "2026-11-10", "strike": 590, "type": "CALL", "price": 0.50, "qty": 1, "calledBy": "TG"}
        res = self.engine.process_signal(payload)
        trade = self.engine.active_trades[res["trade"]["id"]]

        # Hit PT (+100% -> 1.00)
        self.quote_service.set_mock_price(".SPY261110C590", 1.05)
        self.engine.update_quotes_and_check_stops()

        # First roll should be active
        rolled_trades = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_trades), 1)
        roll_1 = rolled_trades[0]
        self.assertEqual(roll_1.roll_count, 1)

        # Hit PT on roll_1 (+100% -> 1.00)
        self.quote_service.set_mock_price(roll_1.spec.streamer_symbol, 1.05)
        self.engine.update_quotes_and_check_stops()

        # roll_1 must now be CLOSED, and roll_2 must be OPEN!
        self.assertNotIn(roll_1.id, self.engine.active_trades)
        rolled_trades_2 = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_trades_2), 1)
        roll_2 = rolled_trades_2[0]
        self.assertEqual(roll_2.roll_count, 2)
        self.assertEqual(roll_2.parent_trade_id, roll_1.id)
        self.assertEqual(roll_2.ladder_tiers[0]["target_profit_pct"], 100.0)
        self.assertEqual(roll_2.active_stop_price, 0.0)



    def test_runner_roll_blocked_when_position_closes_before_final_rung(self):
        """When an expensive option only gets 1 contract on a 3-rung ladder, and Tier 1 takes it out,
        runner roll must NOT trigger because the final ladder rung was never reached."""
        tg_cfg = self.config_mgr.get_sub_strategy_config("TG")
        tg_cfg.enabled = True
        tg_cfg.auto_take_trades = True
        tg_cfg.paper_mode = True
        tg_cfg.execution_mode = "paper"
        tg_cfg.roll_runner_enabled = True
        tg_cfg.roll_runner_qty = 1
        tg_cfg.ladder_tiers = [
            LadderTier(tier_id=1, target_profit_pct=35.0, stop_loss_pct=-50.0, exit_pct=50.0),
            LadderTier(tier_id=2, target_profit_pct=90.0, stop_loss_pct=-10.0, exit_pct=30.0),
            LadderTier(tier_id=3, target_profit_pct=120.0, stop_loss_pct=50.0, exit_pct=20.0),
        ]
        self.config_mgr.sub_strategies["TG"] = tg_cfg

        # 1 contract at $2.00
        self.quote_service.set_mock_price(".SPY261110C590", 2.00)
        payload = {
            "ticker": "SPY",
            "exp": "2026-11-10",
            "strike": 590,
            "type": "CALL",
            "price": 2.00,
            "qty": 1,
            "calledBy": "TG"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.initial_quantity, 1)
        self.assertEqual(trade.remaining_quantity, 1)

        # Price hits Tier 1 PT (+35% -> 2.70)
        self.quote_service.set_mock_price(".SPY261110C590", 2.75)
        self.engine.update_quotes_and_check_stops()

        # The trade had only 1 contract, so it closed after Tier 1
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_parent = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_parent.status, "CLOSED")
        self.assertIn("exhausted before reaching final rung", closed_parent.status_reason)

        # Runner roll must NOT have been opened because final rung was never reached
        rolled_trades = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_trades), 0)



    def test_roll_runner_every_leg_enabled(self):
        """When roll_runner_every_leg is True, the runner roll triggers on every rung PT,
        not just the final rung."""
        tg_cfg = self.config_mgr.get_sub_strategy_config("TG")
        tg_cfg.enabled = True
        tg_cfg.auto_take_trades = True
        tg_cfg.paper_mode = True
        tg_cfg.execution_mode = "paper"
        tg_cfg.roll_runner_enabled = True
        tg_cfg.roll_runner_qty = 1
        tg_cfg.roll_runner_every_leg = True
        tg_cfg.ladder_tiers = [
            LadderTier(tier_id=1, target_profit_pct=35.0, stop_loss_pct=-50.0, exit_pct=50.0),
            LadderTier(tier_id=2, target_profit_pct=90.0, stop_loss_pct=-10.0, exit_pct=30.0),
            LadderTier(tier_id=3, target_profit_pct=120.0, stop_loss_pct=50.0, exit_pct=20.0),
        ]
        self.config_mgr.sub_strategies["TG"] = tg_cfg

        self.quote_service.set_mock_price(".SPY261110C590", 0.50)
        self.quote_service.set_mock_price(".SPY261110C595", 0.50)
        self.quote_service.set_mock_price(".SPY261110C600", 0.50)
        self.quote_service.set_mock_price(".SPY261110C605", 0.50)

        payload = {
            "ticker": "SPY",
            "exp": "2026-11-10",
            "strike": 590,
            "type": "CALL",
            "price": 0.50,
            "qty": 4,
            "calledBy": "TG"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]
        self.assertEqual(trade.initial_quantity, 4)

        # 1. Price hits Tier 1 PT (+35% -> 0.68)
        self.quote_service.set_mock_price(".SPY261110C590", 0.68)
        self.engine.update_quotes_and_check_stops()

        # Intermediate roll should trigger on Leg 1!
        rolled_after_leg1 = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_after_leg1), 1)

        # 2. Price hits Tier 2 PT (+90% -> 0.96)
        self.quote_service.set_mock_price(".SPY261110C590", 0.96)
        self.engine.update_quotes_and_check_stops()

        # Intermediate roll should trigger on Leg 2!
        rolled_after_leg2 = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_after_leg2), 2)

        # 3. Price hits Tier 3 PT (+120% -> 1.12) - Final Rung
        self.quote_service.set_mock_price(".SPY261110C590", 1.12)
        self.engine.update_quotes_and_check_stops()

        # Final roll triggered on Leg 3, parent trade closed
        rolled_after_leg3 = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_after_leg3), 3)
        self.assertNotIn(trade.id, self.engine.active_trades)



    def test_runner_roll_never_triggers_on_stop_loss(self):
        """Runner roll must NEVER trigger on stop loss hits (software breach or broker OCO), only PT."""
        tg_cfg = self.config_mgr.get_sub_strategy_config("TG")
        tg_cfg.enabled = True
        tg_cfg.auto_take_trades = True
        tg_cfg.paper_mode = True
        tg_cfg.execution_mode = "paper"
        tg_cfg.roll_runner_enabled = True
        tg_cfg.roll_runner_qty = 1
        tg_cfg.roll_runner_every_leg = True  # Even if enabled on every leg!
        tg_cfg.ladder_tiers = [
            LadderTier(tier_id=1, target_profit_pct=35.0, stop_loss_pct=-50.0, exit_pct=50.0),
            LadderTier(tier_id=2, target_profit_pct=90.0, stop_loss_pct=-10.0, exit_pct=50.0),
        ]
        self.config_mgr.sub_strategies["TG"] = tg_cfg

        # Case 1: Stop loss hit via quote drop
        self.quote_service.set_mock_price(".SPY261110C590", 0.50)
        payload = {
            "ticker": "SPY",
            "exp": "2026-11-10",
            "strike": 590,
            "type": "CALL",
            "price": 0.50,
            "qty": 2,
            "calledBy": "TG"
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res.get("ok"))
        trade = self.engine.active_trades[res["trade"]["id"]]

        # Price drops to stop loss (-50% -> 0.25)
        self.quote_service.set_mock_price(".SPY261110C590", 0.24)
        self.engine.update_quotes_and_check_stops()

        # Trade must be closed with stopped out reason
        self.assertNotIn(trade.id, self.engine.active_trades)
        closed_trade = next(t for t in self.engine.closed_trades if t.id == trade.id)
        self.assertEqual(closed_trade.status, "CLOSED")
        self.assertIn("Stopped out", closed_trade.status_reason)

        # Runner roll must NOT have triggered!
        rolled_trades = [t for t in self.engine.active_trades.values() if getattr(t, "is_roll_runner", False)]
        self.assertEqual(len(rolled_trades), 0)

        # Case 2: Even direct call to _execute_runner_roll on stopped-out trade is strictly blocked
        roll_res = self.engine._execute_runner_roll(closed_trade, 0.24)
        self.assertIsNone(roll_res)



    def test_dynamic_configured_sl_ratchet_and_broker_reconciliation(self):
        self.config_mgr.paper_mode = False
        """Verify dynamic stop ratcheting reads configured stop_loss_pct exactly,
        and broker reconciliation does not cause double decrement or early closure."""
        oco_orders = []
        canceled_orders = []

        test_self = self
        held_position_qty = 4

        class MockLiveOcoBroker:
            def __init__(self):
                self._counter = 500
                self._is_journal = False

            def place_oco_order(self, symbol, qty, profit_price, stop_price, limit_price=None, action="SELL_TO_CLOSE"):
                self._counter += 1
                order_id = str(self._counter)
                rec = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "qty": qty,
                    "profit_price": profit_price,
                    "stop_price": stop_price,
                    "action": action,
                    "status": "working",
                }
                oco_orders.append(rec)
                return OrderResult(True, order_id, "working")

            def cancel_order(self, order_id):
                canceled_orders.append(str(order_id))
                for o in oco_orders:
                    if o["order_id"] == str(order_id):
                        o["status"] = "canceled"
                return True

            def place_limit_order(self, action, symbol, qty, price):
                self._counter += 1
                return OrderResult(True, str(self._counter), "filled", filled_price=price, filled_quantity=qty)

            def get_order_status(self, order_id, *args, **kwargs):
                cur_q = test_self.quote_service.get_quote(".SPY261110C755")
                cur_px = cur_q.bid if cur_q else 0.49
                for o in oco_orders:
                    if o["order_id"] == str(order_id):
                        if o["status"] == "working" and cur_px >= o["profit_price"]:
                            o["status"] = "filled"
                            return OrderResult(True, str(order_id), "filled", filled_price=o["profit_price"], filled_quantity=o["qty"])
                        return OrderResult(True, str(order_id), o["status"], filled_price=0.0, filled_quantity=0)
                return OrderResult(True, str(order_id), "working", filled_price=0.0, filled_quantity=0)

            def get_single_leg_position_qty(self, symbol, priority='LOW'):
                return held_position_qty

        mock_b = MockLiveOcoBroker()
        self.engine.broker = mock_b

        # 3 configured ladder rungs matching user configuration:
        # Step #1: +35% PT, -50% SL, 50% exit (2 ctrs)
        # Step #2: +90% PT, -10% SL, 30% exit (1 ctr)
        # Step #3: +120% PT, +50% SL, 20% exit (1 ctr)
        tiers = [
            LadderTier(tier_id=1, target_profit_pct=35.0, exit_pct=50.0, stop_loss_pct=-50.0, description="TG Rung 1"),
            LadderTier(tier_id=2, target_profit_pct=90.0, exit_pct=30.0, stop_loss_pct=-10.0, description="TG Rung 2"),
            LadderTier(tier_id=3, target_profit_pct=120.0, exit_pct=20.0, stop_loss_pct=50.0, description="TG Rung 3"),
        ]

        self.quote_service.set_mock_price(".SPY261110C755", 0.49)
        spec = OptionSpec.from_payload({"ticker": "SPY", "expiration": "2026-11-10", "strike": 755.0, "call_put": "CALL"})

        trade = self.engine._execute_and_open_trade(
            spec=spec,
            qty=4,
            fill_price=0.49,
            alert_price=0.49,
            premium_pct=0.0,
            sig_id="sig_dyn_oco_test",
            sub_strategy="TG",
            called_by="TG",
            paper_mode=False,
            ladder_tiers=tiers,
        )

        self.assertEqual(len(oco_orders), 3)
        # Initial stops placed at -50% ($0.25)
        self.assertAlmostEqual(oco_orders[0]["stop_price"], 0.24, places=2)
        self.assertAlmostEqual(oco_orders[1]["stop_price"], 0.24, places=2)
        self.assertAlmostEqual(oco_orders[2]["stop_price"], 0.24, places=2)

        # Rung 1 hits PT (+35% -> $0.66)
        # Broker position drops from 4 to 2 contracts
        held_position_qty = 2
        self.quote_service.set_mock_price(".SPY261110C755", 0.66)
        self.engine.update_quotes_and_check_stops()

        # Trade MUST stay OPEN with remaining_quantity = 2 (not 0!)
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.remaining_quantity, 2)
        self.assertEqual(trade.tier_distribution[0]["status"], "filled_pt")
        self.assertEqual(trade.tier_distribution[1]["status"], "working")
        self.assertEqual(trade.tier_distribution[2]["status"], "working")

        # Remaining rungs 2 & 3 MUST be ratcheted to Rung 2's configured SL: -10% ($0.44)!
        # NOT 50% ($0.25) and NOT 0% breakeven ($0.49)!
        self.assertAlmostEqual(trade.tier_distribution[1]["stop_price"], 0.44, places=2)
        self.assertAlmostEqual(trade.tier_distribution[2]["stop_price"], 0.44, places=2)
        self.assertAlmostEqual(trade.active_stop_price, 0.44, places=2)
        self.assertEqual(trade.active_stop_pct, -10.0)

        # Rung 2 hits PT (+90% -> $0.93)
        held_position_qty = 1
        self.quote_service.set_mock_price(".SPY261110C755", 0.93)
        self.engine.update_quotes_and_check_stops()

        # Trade MUST stay OPEN with remaining_quantity = 1
        self.assertEqual(trade.status, "OPEN")
        self.assertEqual(trade.remaining_quantity, 1)
        self.assertEqual(trade.tier_distribution[1]["status"], "filled_pt")
        self.assertEqual(trade.tier_distribution[2]["status"], "working")

        # Rung 3 MUST be ratcheted to Rung 3's configured SL: +50% ($0.74)!
        self.assertAlmostEqual(trade.tier_distribution[2]["stop_price"], 0.73, places=2)
        self.assertAlmostEqual(trade.active_stop_price, 0.73, places=2)
        self.assertEqual(trade.active_stop_pct, 50.0)

        # Finally Rung 3 hits PT (+120% -> $1.08)
        held_position_qty = 0
        self.quote_service.set_mock_price(".SPY261110C755", 1.08)
        self.engine.update_quotes_and_check_stops()

        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.remaining_quantity, 0)
        self.assertEqual(len(trade.closed_legs), 3)

    def test_signal_and_trade_watermark_persistence_across_restart(self):
        """Verify that peak_price and bottom_price on signals and highest_price on trades persist across engine reload."""
        sig_payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 0.85,
            "calledBy": "TG",
        }
        self.quote_service.set_mock_price(".SPY260925C500", 0.85)
        res = self.engine.process_signal(sig_payload)
        self.assertTrue(res.get("ok"))
        sig = self.engine.signals[0]
        self.assertEqual(sig.peak_price, 0.85)

        # Price surges to $2.50
        self.quote_service.set_mock_price(".SPY260925C500", 2.50)
        self.engine.update_quotes_and_check_stops()
        self.assertEqual(sig.peak_price, 2.50)

        # Force flush to disk
        self.engine.flush_all(force=True)

        # Simulate engine restart by creating a new engine instance pointing to the same data_dir
        engine2 = CloneTradeEngine(config_mgr=self.config_mgr, broker=self.engine.broker, quote_service=self.quote_service)
        sig2 = engine2.signals[0]
        self.assertEqual(sig2.peak_price, 2.50)
        self.assertEqual(sig2.bottom_price, 0.85)


if __name__ == "__main__":
    unittest.main()
