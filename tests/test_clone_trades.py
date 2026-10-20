"""Unit tests for Discretionary Clone Trades system."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

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

        self.quote_service.set_mock_price(".SPY260925C500", 1.00)
        payload = {
            "ticker": "SPY",
            "exp": "2026-09-25",
            "strike": 500,
            "type": "CALL",
            "price": 1.00,
            "qty": 4,
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
            "ticker": "QQQ", "exp": "2026-09-29", "strike": 741, "type": "CALL", "price": 1.00, "qty": 4
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
            "ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00, "qty": 4
        }
        res = self.engine.process_signal(payload)
        self.assertTrue(res["ok"])
        trade_id = res["trade"]["id"]
        trade = self.engine.active_trades[trade_id]

        # Initially: entry=1.00, initial_stop_loss_pct=-75.0 -> stop_price=0.25, pt=1.25 (+25%)
        self.assertEqual(trade.entry_price, 1.00)
        self.assertEqual(trade.active_stop_pct, -75.0)
        self.assertEqual(trade.active_stop_price, 0.25)
        self.assertEqual(trade.active_pt_price, 1.25)

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


if __name__ == "__main__":
    unittest.main()
