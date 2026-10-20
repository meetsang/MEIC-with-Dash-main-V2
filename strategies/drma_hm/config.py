"""Configuration dataclass for Daily Reset MA + Hilega-Milega (DRMA_HM) Strategy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from blocks.stop.profiles.drma_hm import DRMA_HM_PROFILE
from strategies.base import StrategyConfig


@dataclass
class DRMAHMConfig(StrategyConfig):
    name: str = 'DRMA_HM_SPX'
    ticker: str = 'SPX'
    enabled: bool = True
    scheduled: bool = False
    paper: bool = True
    confirmed: bool = True
    requires_confirmation: bool = True
    stop_profile: str = DRMA_HM_PROFILE

    # --- Sub-strategy Paper / Live Independent Controls ---
    # When None, sub-strategies inherit the global strategy `self.paper` setting.
    # Set explicitly to False for LIVE trading or True for PAPER trading independently.
    dir_paper: Optional[bool] = None       # Directional options (Debit Spreads / Long Options)
    non_dir_paper: Optional[bool] = None   # Non-directional options (Credit Spreads / Iron Condor)
    futures_paper: Optional[bool] = None   # Directional Futures (Outright ES Futures)

    @property
    def is_dir_paper(self) -> bool:
        return self.paper if self.dir_paper is None else bool(self.dir_paper)

    @property
    def is_non_dir_paper(self) -> bool:
        return self.paper if self.non_dir_paper is None else bool(self.non_dir_paper)

    @property
    def is_futures_paper(self) -> bool:
        return self.paper if self.futures_paper is None else bool(self.futures_paper)

    @property
    def has_any_live_sub_strategy(self) -> bool:
        """Return True if any enabled sub-strategy is configured to execute live orders."""
        if self.is_directional_options_enabled and not self.is_dir_paper:
            return True
        if self.is_non_directional_options_enabled and not self.is_non_dir_paper:
            return True
        if self.is_directional_futures_enabled and not self.is_futures_paper:
            return True
        return False

    def is_sub_strategy_paper(self, sub_strategy: str) -> bool:
        """Return paper status for a named sub-strategy or identifier."""
        s = str(sub_strategy).lower()
        if 'dir' in s and 'non' not in s:
            return self.is_dir_paper
        if 'non_dir' in s or 'credit' in s or 'ic' in s:
            return self.is_non_dir_paper
        if 'fut' in s or 'es' in s:
            return self.is_futures_paper
        return bool(self.paper)

    # --- Strategy Trade Mode ---
    # 'all_three': Take directional options (Grade A+), credit options (Grade A), AND directional futures on ES (Grade A+ only) (Default)
    # 'directional_futures': Take only outright directional futures on ES (Grade A+ only)
    # 'both': Take directional options and non-directional/credit options
    # 'directional_only': Take only directional options (Grade A+)
    # 'non_directional_only': Take only credit spreads (Grade A / B) or neutral ICs
    # 'directional_both': Take directional options AND directional futures on ES (Grade A+ only)
    trade_mode: str = 'all_three'

    @property
    def is_directional_options_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        if mode in ('all', 'all_three', 'both', 'both_options', 'directional_only', 'directional_options', 'directional_both'):
            return True
        return 'dir_opt' in mode or 'directional_options' in mode

    @property
    def is_non_directional_options_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        if mode in ('all', 'all_three', 'both', 'both_options', 'non_directional_only', 'non_directional_options'):
            return True
        return 'non_dir' in mode or 'credit' in mode

    @property
    def is_directional_futures_enabled(self) -> bool:
        mode = self.trade_mode.lower()
        if mode in ('all', 'all_three', 'directional_futures', 'futures_only', 'directional_both', 'futures'):
            return True
        return 'futures' in mode

    # --- Directional Futures Configuration (Outright ES Futures) ---
    futures_symbol: str = 'ES'             # Futures product code / symbol ('ES')
    futures_quantity: int = 1              # Number of ES futures contracts
    futures_profit_target_pts: float = 5.0 # Take profit target in index points (default 5 points)
    futures_stop_loss_pts: float = 5.0     # Stop loss in index points (default 5 points)
    futures_point_value: float = 50.0      # Dollar multiplier per index point ($50 for ES)
    futures_allow_short: bool = True       # Allow shorting ES on bearish signals (BULL = Buy ES, BEAR = Short ES)
    futures_min_grade: str = 'Grade A+ Only' # Minimum setup grade for futures: 'Grade A+ Only' (score 5) or 'Grade A and A+' (score >= 4)

    # Minimum setup grade to trigger entry: 'Grade A and A+' (score >= 4), 'Grade A+ Only' (score 5), 'Grade B and Above' (score >= 3)
    min_grade_to_signal: str = 'Grade A and A+'

    # --- Non-Directional Trades Configuration (Credit Spreads / Iron Condor) ---
    # Selection mode: 'credit' (select short strike by target credit) or 'delta'
    non_dir_selection_mode: str = 'credit'
    non_dir_target_delta: float = 0.15     # Preferred short leg delta (fallback/informational)
    non_dir_min_delta: float = 0.08        # Minimum acceptable short delta
    non_dir_max_delta: float = 0.25        # Maximum acceptable short delta
    non_dir_target_credit: float = 1.00    # Target entry net credit in points (e.g. $1.00)
    non_dir_min_credit: float = 0.40       # Minimum credit required to accept spread
    non_dir_spread_width: int = 10         # Spread width in points (default 10 for SPX)
    non_dir_profit_target_pct: float = 0.50 # Take profit at 50% of credit received
    non_dir_stop_loss_pct: float = 1.00    # Stop loss at 100% of credit received (2x credit)
    non_dir_allow_iron_condor: bool = False # Enter Iron Condor if neutral/chop inside ORB

    # --- Directional Trades Configuration (Debit Spreads / Long Options) ---
    # Vehicle type: 'debit_spread' (Vertical debit spread) or 'long_option' (Single long call/put)
    dir_vehicle_type: str = 'debit_spread'
    # Selection mode: 'debit' (select long strike by target debit) or 'delta'
    dir_selection_mode: str = 'debit'
    dir_target_delta: float = 0.40         # Preferred long leg delta (fallback/informational)
    dir_min_delta: float = 0.25            # Minimum acceptable delta
    dir_max_delta: float = 0.55            # Maximum acceptable delta
    dir_target_debit: float = 2.50         # Target net debit to pay
    dir_max_debit: float = 4.00            # Maximum debit cap
    dir_spread_width: int = 10             # Spread width in points
    dir_profit_target_pct: float = 0.50    # Take profit at 50% gain on debit paid
    dir_stop_loss_pct: float = 0.50        # Stop loss at 50% loss of debit paid
    max_daily_loss: Optional[float] = None # Circuit breaker loss limit in dollars

    # --- Indicator Parameters (SPX Calibrated from Pine Script) ---
    ma_type: str = 'EMA'                   # 'EMA' or 'SMA'
    ma_len: int = 21                       # Length of Daily Reset Moving Average
    orb_minutes: int = 30                  # Duration of Opening Range Breakout (minutes from 08:30 CT / 09:30 ET)
    hm_rsi_len: int = 9                    # Hilega-Milega RSI length
    hm_fast_len: int = 3                   # Hilega-Milega Fast EMA length
    hm_slow_len: int = 21                  # Hilega-Milega Slow WMA length

    # --- Filters & Guards ---
    require_ema_slope: bool = True         # Require active slope before entry
    min_slope_pts: float = 0.50            # Minimum MA delta over 2 bars in points on SPX (corresponds to 0.05 on SPY)
    max_extension_pts: float = 18.0        # Max distance from 21 EMA to enter (pts on SPX, corresponds to 1.8 on SPY)
    pullback_tol_pts: float = 1.5          # Proximity to 21 EMA for pullback test (pts on SPX)
    wait_for_orb: bool = True              # Do not enter before 30m ORB completes (09:00 CT / 10:00 ET)
    filter_lunch_chop: bool = False        # Mute entries during 10:30-12:30 CT (11:30-13:30 ET)
    enable_debounce: bool = True           # Directional lock: 1 entry per leg until flat
    cooldown_bars: int = 5                 # Minimum bars (e.g. 25 min) between new entries
    max_daily_signals: int = 4             # Max trade entries per day (0 = unlimited)
    force_eod_exit: bool = True            # Force square-off before market close
    eod_exit_time_cst: str = '14:50'       # 14:50 CT (15:50 ET / 10 min before close)

    # --- Exit Styles ---
    # 'trend_runner': Rides 21 EMA trail, exits on 2-bar close across MA or full H-M cross across 50
    # 'confirmed_hm': Exits when Fast crosses Slow AND crosses 50 line
    # 'tight_scalp': Exits on very first Fast/Slow micro-hook
    exit_style: str = 'trend_runner'
    min_hold_bars: int = 3                 # Minimum bars to hold before momentum exit can trigger

    # --- External Data Feeds & Manual Fallbacks ---
    enable_yahoo_finance: bool = True      # Auto-fetch ES overnight & SPX prev day from Yahoo Finance API
    manual_es_offset: Optional[float] = None # Manual ES-SPX basis offset (SPX = Scaled ES - offset)
    manual_es_basis: Optional[float] = None  # Alias for manual_es_offset from UI
    manual_onh: Optional[float] = None     # Manual Overnight High for SPX
    manual_onl: Optional[float] = None     # Manual Overnight Low for SPX
    manual_pdh: Optional[float] = None     # Manual Previous Day High for SPX
    manual_pdl: Optional[float] = None     # Manual Previous Day Low for SPX
    manual_pdc: Optional[float] = None     # Manual Previous Day Close for SPX

    # --- Execution & Runtime ---
    quantity: int = 1                      # Quantity of contracts/spreads
    entry_fill_wait_sec: int = 20          # Max wait time for fill in seconds
    poll_interval_sec: float = 5.0         # Strategy check loop interval (seconds)
    root: str = ''
    data_dir: str = 'data'

    def __post_init__(self):
        if self.manual_es_basis is not None and self.manual_es_offset is None:
            self.manual_es_offset = self.manual_es_basis
        elif self.manual_es_offset is not None and self.manual_es_basis is None:
            self.manual_es_basis = self.manual_es_offset
