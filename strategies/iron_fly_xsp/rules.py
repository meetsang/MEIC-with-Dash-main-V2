"""Pure rule engine for Dale Perryman NFH SPX 0DTE Iron Fly scaled to XSP.

Converts SPX 0DTE iron fly logic to Mini-SPX (XSP) by an exact 1:10 scaling factor:
- 1 SPX pt = 0.1 XSP pt.
- $50 wide SPX wings = 5.0 wide XSP wings.
- $20 min SPX wings = 2.0 min XSP wings.
- Credit thresholds ($20 / $15) = ($2.00 / $1.50).
- ITM distance triggers ($15 / $10 / $7.5 / $2.5) = ($1.50 / $1.00 / $0.75 / $0.25).
- Center strike offsets ($20 / $15 / $10 / $5) = ($2.00 / $1.50 / $1.00 / $0.50).
- An SPX center ending in 5 (e.g. 5835 -> 583.5) executes as a pair of 2 XSP flies:
    - 1 fly centered at floor(ideal) = 583
    - 1 fly centered at ceil(ideal) = 584
    Average center = 583.5.
- If SPX center is a multiple of 10 (e.g. 5830 -> 583.0):
    - 2 flies centered at 583.
- Beyond breakeven cuts: spot <= be_low - bbe or spot >= be_high + bbe (default bbe = 0.01).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

from strategies.iron_fly.rules import (
    is_last_weekday_of_month,
    is_third_wednesday,
    skip_reason,
)


@dataclass(frozen=True)
class NfhRulesXsp:
    """Strategy constants for XSP scaled 1:10 from SPX NFH specification."""
    # Timing
    first_fly_time: time = time(10, 35, 0)
    tight_threshold_time: time = time(14, 0, 0)         # 2:00 PM Eastern
    late_tight_threshold_time: time = time(15, 30, 0)    # 3:30 PM Eastern
    adds_cutoff: time = time(15, 50, 0)                 # 3:50 PM Eastern
    market_close: time = time(16, 0, 0)

    # Initial Fly
    initial_wing_width: int = 5                         # 50 / 10
    min_mid_price_initial: float = 0.30                 # 3.0 / 10
    max_mid_price_initial: float = 5.00                 # 50.0 / 10

    # Pricing & Wings for Adds
    wing_target_mid: float = 0.04                       # 0.40 / 10
    min_wing_width: int = 2                             # 20 / 10
    max_wing_width: int = 5                             # 50 / 10
    candidate_widths: Tuple[int, ...] = (2, 3, 4, 5)

    # Add Trigger Thresholds (Credit & ITM distance scaled by 10)
    high_trade_price_threshold: float = 2.00            # 20.0 / 10
    low_trade_price_threshold: float = 1.50             # 15.0 / 10
    itm_high_threshold: float = 1.50                    # 15.0 / 10
    itm_mid_threshold: float = 1.00                     # 10.0 / 10
    itm_low_threshold: float = 0.75                     # 7.5 / 10
    itm_tight_threshold: float = 0.25                   # 2.5 / 10
    tight_mid_price_threshold: float = 1.00             # 10.0 / 10

    # Centering offsets (scaled by 10)
    offset_high: float = 2.00                           # 20.0 / 10
    offset_mid: float = 1.50                            # 15.0 / 10
    offset_low: float = 1.00                            # 10.0 / 10
    offset_tight: float = 0.50                          # 5.0 / 10

    # Beyond Breakeven Close
    beyond_breakeven_amount: float = 0.01
    pabbe_close: bool = False
    high_pabbe_trade_price: float = 2.00
    low_pabbe_trade_price: float = 1.50
    high_pabbe_close_amount: float = 0.20               # 2.0 / 10
    mid_pabbe_close_amount: float = 0.10                # 1.0 / 10
    low_pabbe_close_amount: float = 0.01

    late_bbe_adjustment: bool = False
    late_bbe_time: time = time(15, 30, 0)
    late_bbe_amount: float = 0.15                       # 1.50 / 10

    eod_bbe_adjustment: bool = False
    eod_bbe_time: time = time(15, 50, 0)
    eod_bbe_amount: float = 0.25                        # 2.50 / 10

    min_close_ror: float = -0.90                        # -90% max loss guard

    # Rapid move circuit breaker
    rapid_move_1m_pts: float = 2.50                     # 25.0 / 10
    rapid_move_2m_pts: float = 4.00                     # 40.0 / 10

    # Day targets
    day_pt: float = 100.0                               # 1000.0 / 10
    day_pt_close: bool = False
    day_max_loss: float = -200.0                        # -2000.0 / 10
    day_max_loss_close: bool = False

    fee_per_leg: float = 0.65


DEFAULT_RULES_XSP = NfhRulesXsp()


def atm_center_spx(spot_spx: float) -> int:
    """SPX ATM strike rounded to nearest 5 points."""
    return int(round(spot_spx / 5.0) * 5)


def get_xsp_centers(spx_center: int) -> Tuple[int, int]:
    """Convert SPX center (multiple of 5) to two XSP integer centers.

    If spx_center / 10 has a .5 fraction (e.g. 5835 -> 583.5):
        returns (583, 584) - 1 fly at 583 and 1 fly at 584.
    If spx_center / 10 is an integer (e.g. 5830 -> 583.0):
        returns (583, 583) - 2 flies at 583.
    """
    ideal = spx_center / 10.0
    if ideal % 1.0 != 0:
        return int(math.floor(ideal)), int(math.ceil(ideal))
    return int(ideal), int(ideal)


def is_tight_mode_xsp(
    now_time: time,
    mid_5w: Optional[float] = None,
    rules: NfhRulesXsp = DEFAULT_RULES_XSP,
) -> bool:
    """Check if tight threshold mode is active on XSP."""
    if now_time < rules.tight_threshold_time:
        return False
    if now_time >= rules.late_tight_threshold_time:
        return True
    if mid_5w is not None and mid_5w < rules.tight_mid_price_threshold:
        return True
    return False


def evaluate_add_trigger_xsp(
    spot_xsp: float,
    closest_center_ideal: float,
    closest_credit: float,
    now_time: time,
    mid_5w: Optional[float] = None,
    rules: NfhRulesXsp = DEFAULT_RULES_XSP,
) -> Tuple[bool, str, float, float, float]:
    """Evaluate whether an additional XSP IB should be opened based on the closest open IB.

    Returns:
        (triggered, scenario, itm_threshold, center_offset, itm_distance)
    """
    itm_distance = round(abs(spot_xsp - closest_center_ideal), 3)

    tight_active = is_tight_mode_xsp(now_time, mid_5w, rules)
    if tight_active:
        threshold = rules.itm_tight_threshold   # 0.25
        offset = rules.offset_tight             # 0.50
        scenario = 'tight'
        triggered = itm_distance > threshold
        return triggered, scenario, threshold, offset, itm_distance

    # Standard period: evaluate opening credit
    if closest_credit > rules.high_trade_price_threshold:
        threshold = rules.itm_high_threshold    # 1.50
        offset = rules.offset_high              # 2.00
        scenario = 'high_credit'
    elif closest_credit >= rules.low_trade_price_threshold:
        threshold = rules.itm_mid_threshold     # 1.00
        offset = rules.offset_mid               # 1.50
        scenario = 'mid_credit'
    else:
        threshold = rules.itm_low_threshold     # 0.75
        offset = rules.offset_low               # 1.00
        scenario = 'low_credit'

    triggered = itm_distance > threshold
    return triggered, scenario, threshold, offset, itm_distance


def next_spx_center_xsp(
    spot_spx: float,
    closest_spx_center: int,
    offset_spx: float,
    open_spx_centers: List[int],
    long_strikes_spx: Optional[set[int]] = None,
) -> Optional[int]:
    """Calculate the next SPX-equivalent center for XSP pair execution."""
    direction = 1 if spot_spx > closest_spx_center else -1
    candidate = closest_spx_center + int(round(direction * offset_spx))
    # Round to nearest multiple of 5
    candidate = int(round(candidate / 5.0) * 5)

    if candidate in open_spx_centers:
        return None

    if long_strikes_spx and candidate in long_strikes_spx:
        return None

    return candidate


def pick_widths_xsp(
    spx_cw: int,
    spx_pw: int,
    center_a: int,
    center_b: int,
    existing_short_strikes: Optional[set[int]] = None,
    min_width: int = 2,
    max_width: int = 5,
    is_initial: bool = False,
    chain: Optional[Dict[Tuple[str, int], dict]] = None,
) -> Tuple[int, int]:
    """Scales wing widths down by 10 to integer XSP strikes.

    - Initial fly: strictly 5 pt wings ($50 SPX equivalent) if quoted, else widest quoted.
    - Additional flies: bounded between min_width (2) and max_width (5).
    - Enforces strike clash prevention against existing short strikes.
    """
    ideal_w = max_width if is_initial else max(min_width, min(max_width, int(round(spx_cw / 10.0))))
    existing = existing_short_strikes or set()

    # Call width: step out to avoid clash
    cw = ideal_w
    while (center_a + cw in existing or center_b + cw in existing) and cw < max_width:
        cw += 1
    if chain is not None:
        while cw >= min_width and (('C', center_a + cw) not in chain or ('C', center_b + cw) not in chain):
            cw -= 1
        if cw < min_width:
            cw = min_width

    # Put width: step out to avoid clash
    pw = ideal_w
    while (center_a - pw in existing or center_b - pw in existing) and pw < max_width:
        pw += 1
    if chain is not None:
        while pw >= min_width and (('P', center_a - pw) not in chain or ('P', center_b - pw) not in chain):
            pw -= 1
        if pw < min_width:
            pw = min_width

    return cw, pw


def price_fly_xsp(
    center: int,
    cw: int,
    pw: int,
    chain: Dict[Tuple[str, int], dict],
) -> Optional[Tuple[float, float]]:
    """Calculates natural credit and mid credit for an XSP 4-leg iron fly."""
    call_short = chain.get(('C', center))
    put_short = chain.get(('P', center))
    call_long = chain.get(('C', center + cw))
    put_long = chain.get(('P', center - pw))

    if not all([call_short, put_short, call_long, put_long]):
        return None

    cs_bid = float(call_short.get('bid', 0.0))
    ps_bid = float(put_short.get('bid', 0.0))
    cl_ask = float(call_long.get('ask', 0.0))
    pl_ask = float(put_long.get('ask', 0.0))
    nat_credit = cs_bid + ps_bid - cl_ask - pl_ask

    cs_mid = float(call_short.get('mid') or (call_short.get('bid', 0) + call_short.get('ask', 0)) / 2)
    ps_mid = float(put_short.get('mid') or (put_short.get('bid', 0) + put_short.get('ask', 0)) / 2)
    cl_mid = float(call_long.get('mid') or (call_long.get('bid', 0) + call_long.get('ask', 0)) / 2)
    pl_mid = float(put_long.get('mid') or (put_long.get('bid', 0) + put_long.get('ask', 0)) / 2)
    mid_credit = cs_mid + ps_mid - cl_mid - pl_mid

    return round(nat_credit, 2), round(mid_credit, 2)


def calculate_bbe_amount_xsp(
    opening_credit: float,
    now_time: time,
    rules: NfhRulesXsp = DEFAULT_RULES_XSP,
) -> float:
    """Calculate Beyond Breakeven (BBE) close amount for XSP."""
    amount = rules.beyond_breakeven_amount

    if rules.pabbe_close:
        if opening_credit > rules.high_pabbe_trade_price:
            amount = rules.high_pabbe_close_amount
        elif opening_credit >= rules.low_pabbe_trade_price:
            amount = rules.mid_pabbe_close_amount
        else:
            amount = rules.low_pabbe_close_amount

    if rules.late_bbe_adjustment and now_time >= rules.late_bbe_time:
        amount = rules.late_bbe_amount
    if rules.eod_bbe_adjustment and now_time >= rules.eod_bbe_time:
        amount = rules.eod_bbe_amount

    return round(amount, 4)


def should_cut_xsp(
    spot_xsp: float,
    be_low: float,
    be_high: float,
    beyond_be_amount: float = 0.01,
    current_ror: Optional[float] = None,
    min_close_ror: float = -0.90,
) -> Tuple[bool, str]:
    """Evaluates beyond break-even cut for XSP:

    - Downward breach: spot_xsp <= (be_low - beyond_be_amount)
    - Upward breach:   spot_xsp >= (be_high + beyond_be_amount)
    Position is NOT closed if return on risk (RoR) is < min_close_ror (default -90%).
    """
    if be_low is None or be_high is None:
        return False, ''

    if current_ror is not None and current_ror < min_close_ror:
        return False, 'ror_below_min_close'

    if spot_xsp <= (be_low - beyond_be_amount):
        return True, f'spot {spot_xsp:.2f} <= BE low {be_low:.2f} - {beyond_be_amount:.2f}'
    if spot_xsp >= (be_high + beyond_be_amount):
        return True, f'spot {spot_xsp:.2f} >= BE high {be_high:.2f} + {beyond_be_amount:.2f}'
    return False, ''


def intrinsic_xsp(spot_xsp: float, center: int, call_long: int, put_long: int) -> float:
    """Cash-settlement intrinsic debit of an XSP Iron Fly at 4:00 PM ET."""
    short_call_int = max(0.0, spot_xsp - center)
    short_put_int = max(0.0, center - spot_xsp)
    long_call_int = max(0.0, spot_xsp - call_long)
    long_put_int = max(0.0, put_long - spot_xsp)
    total_debit = (short_call_int + short_put_int) - (long_call_int + long_put_int)
    return round(max(0.0, total_debit), 2)


# Backward compatibility aliases
TIER_PARAMS_XSP = {
    0: {'trigger_dist': 2.5, 'spacing': 3.0},
    1: {'trigger_dist': 2.0, 'spacing': 2.5},
    2: {'trigger_dist': 1.5, 'spacing': 2.0},
    3: {'trigger_dist': 1.0, 'spacing': 1.5},
    4: {'trigger_dist': 0.5, 'spacing': 0.5},
}


def tier_index_xsp(straddle_mid: float) -> int:
    """Legacy helper for backward compatibility."""
    if straddle_mid > 5.00:
        return 0
    if straddle_mid > 4.00:
        return 1
    if straddle_mid > 3.00:
        return 2
    if straddle_mid > 2.00:
        return 3
    return 4


def tier_params_xsp(tier: int) -> dict:
    """Legacy helper for backward compatibility."""
    return TIER_PARAMS_XSP.get(tier, TIER_PARAMS_XSP[4])
