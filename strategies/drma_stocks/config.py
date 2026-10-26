"""Configuration dataclass for Daily Reset MA + Hilega-Milega on Individual Equities/Stocks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from blocks.stop.profiles.drma_hm import DRMA_HM_PROFILE
from strategies.base import StrategyConfig


@dataclass
class DRMAStocksConfig(StrategyConfig):
    name: str = 'DRMA_HM_STOCKS'
    ticker: str = 'STOCKS'                 # Multi-ticker aggregator identifier
    enabled: bool = True
    scheduled: bool = False
    paper: bool = True                     # Everything remains paper per requirement
    confirmed: bool = True
    requires_confirmation: bool = False
    stop_profile: str = DRMA_HM_PROFILE

    # --- Sub-strategy Paper settings (forced to paper) ---
    dir_paper: bool = True
    non_dir_paper: bool = True
    stock_paper: bool = True

    # --- Trade Mode ---
    # 'all_three': Directional Spreads (Grade A+), Non-Directional Spreads (Grade A), Direct Stock Equity (Grade A+)
    # 'stock_only': Direct stock equity buy/sell only
    # 'options_only': Directional and Non-Directional options spreads only
    # 'directional_only': Directional spreads and direct stock only
    trade_mode: str = 'all_three'

    @property
    def is_directional_options_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        return mode in ('all', 'all_three', 'both', 'options_only', 'directional_only')

    @property
    def is_non_directional_options_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        return mode in ('all', 'all_three', 'both', 'options_only', 'non_directional_only')

    @property
    def is_stock_equity_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        return mode in ('all', 'all_three', 'stock_only', 'directional_only', 'stock')

    # --- Direct Equity Stock Configuration (Futures Equivalent) ---
    stock_allocation_dollars: float = 5000.0  # Fixed dollar allocation per trade ($5,000 buying power)
    stock_profit_target_pct: float = 0.03      # 3.0% profit target gain on stock price

    @property
    def equity_allocation_dollars(self) -> float:
        return self.stock_allocation_dollars
    stock_stop_loss_pct: float = 0.02          # 2.0% stop loss on stock price
    stock_allow_short: bool = True             # Allow shorting stock on confirmed Bear Grade A+
    stock_min_grade: str = 'Grade A+ Only'     # Minimum setup grade for direct stock buy/sell (5/5)

    # --- Options Spread Trades Configuration ---
    spread_width: int = 10                     # Strict 10-wide strike width
    min_dte: int = 0                           # Minimum DTE for options (nearest weekly)
    max_dte: int = 7                           # Maximum DTE for options (nearest weekly 0-7 DTE)
    options_quantity: int = 1                  # Default 1 contract spread per signal
    
    # Non-Directional Spreads (Credit Spreads)
    non_dir_profit_target_pct: float = 0.50    # Take profit at 50% credit gain
    non_dir_stop_loss_pct: float = 1.00        # Stop loss at 100% of credit (2x credit)
    non_dir_target_delta: float = 0.15

    # Directional Spreads (Debit Spreads)
    dir_profit_target_pct: float = 0.50        # Take profit at 50% debit gain
    dir_stop_loss_pct: float = 0.50            # Stop loss at 50% debit loss
    dir_target_delta: float = 0.40

    min_grade_to_signal: str = 'Grade A and A+' # 'Grade A and A+' or 'Grade A+ Only'

    # --- Indicator Parameters ---
    ma_type: str = 'EMA'
    ma_len: int = 21
    orb_minutes: int = 30                      # 30m ORB (marked between 8:30 AM and 9:00 AM CST)
    hm_rsi_len: int = 9
    hm_fast_len: int = 3
    hm_slow_len: int = 21

    # --- Session & Timing (CST) ---
    filter_volatile_opening_hour: bool = False # Allow trading starting at 09:00 CST once 30m ORB forms
    market_open_time_cst: str = '08:30'        # 08:30 CST: Market open (RTH open) & 30m ORB marking starts
    regular_trading_start_time_cst: str = '09:00' # 09:00 CST: 30m ORB established, regular trading begins
    force_eod_exit: bool = True                # Force exit before market close
    eod_exit_time_cst: str = '14:50'           # 14:50 CST: End of active day cutoff

    # --- Dynamic Filters for Stocks ---
    require_ema_slope: bool = True
    min_slope_pct: float = 0.0004              # Min 2-bar slope delta as % of stock price (0.04%)
    max_extension_pct: float = 0.035           # Max distance from 21 EMA as % of stock price (3.5%)
    wait_for_orb: bool = True
    cooldown_bars: int = 3                     # Minimum bars between new entries on the same ticker
    max_daily_signals_per_ticker: int = 3      # Max signals per stock per day
    exit_style: str = 'trend_runner'
    min_hold_bars: int = 3

    # --- Runtime ---
    poll_interval_sec: float = 10.0
    root: str = ''
    data_dir: str = 'data'
