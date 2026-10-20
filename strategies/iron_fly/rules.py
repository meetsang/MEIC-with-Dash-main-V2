"""Dale Perryman's NFH Iron Fly Strategy — pure mathematical and business rules.

Ported from Option Alpha Theta Fly Trap (NFH) 0 DTE Iron Butterfly strategy (v28/v29).
Zero external I/O: fully unit-testable.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional, Tuple

from brokers.base import ComboLeg
from common.symbols import build_schwab_symbol, build_tastytrade_symbol


@dataclass(frozen=True)
class NfhRules:
    """Strategy constants matching Theta Fly Trap (NFH) v28/v29 default bot inputs."""
    # Timing
    first_fly_time: time = time(10, 35, 0)
    tight_threshold_time: time = time(14, 0, 0)         # 2:00 PM Eastern
    late_tight_threshold_time: time = time(15, 30, 0)    # 3:30 PM Eastern
    adds_cutoff: time = time(15, 50, 0)                 # 3:50 PM Eastern (Position Entry Stop Time)
    market_close: time = time(16, 0, 0)

    # Initial Fly
    initial_wing_width: int = 50
    min_mid_price_initial: float = 3.0
    max_mid_price_initial: float = 50.0

    # Pricing & Wings for Adds
    wing_target_mid: float = 0.40
    min_wing_width: int = 20
    max_wing_width: int = 50
    candidate_widths: Tuple[int, ...] = (20, 25, 30, 35, 40, 45, 50)

    # Add Trigger Thresholds (Credit & ITM distance)
    high_trade_price_threshold: float = 20.0
    low_trade_price_threshold: float = 15.0
    itm_high_threshold: float = 15.0     # If credit > 20 and ITM > 15 -> add centered ±20
    itm_mid_threshold: float = 10.0      # If credit 15-20 and ITM > 10 -> add centered ±15
    itm_low_threshold: float = 7.5       # If credit < 15 and ITM > 7.5 -> add centered ±10
    itm_tight_threshold: float = 2.5     # Tight mode: ITM > 2.5 -> add centered ±5
    tight_mid_price_threshold: float = 10.0  # Mid price of 50w IB must be < 10 for tight before 3:30 PM

    # Centering offsets
    offset_high: float = 20.0
    offset_mid: float = 15.0
    offset_low: float = 10.0
    offset_tight: float = 5.0

    # Beyond Breakeven Close
    beyond_breakeven_amount: float = 0.01
    pabbe_close: bool = False
    high_pabbe_trade_price: float = 20.0
    low_pabbe_trade_price: float = 15.0
    high_pabbe_close_amount: float = 2.0
    mid_pabbe_close_amount: float = 1.0
    low_pabbe_close_amount: float = 0.01

    late_bbe_adjustment: bool = False
    late_bbe_time: time = time(15, 30, 0)
    late_bbe_amount: float = 1.50

    eod_bbe_adjustment: bool = False
    eod_bbe_time: time = time(15, 50, 0)
    eod_bbe_amount: float = 2.50

    min_close_ror: float = -0.90         # Return on risk min close threshold (-90%)

    # Rapid move and VIX circuit breakers
    rapid_move_1m_pts: float = 25.0
    rapid_move_2m_pts: float = 40.0
    max_vix_change: float = 15.0

    # Day targets
    day_pt: float = 1000.0
    day_pt_close: bool = False
    day_max_loss: float = -2000.0
    day_max_loss_close: bool = False

    # Spacing / Combo settings
    atm_rounding: int = 5
    combo_tick: float = 0.05
    fee_per_leg: float = 1.30


DEFAULT_RULES = NfhRules()


def is_third_wednesday(d: date) -> bool:
    """Check if date is the third Wednesday of the month (Wednesday on day 15-21)."""
    return d.weekday() == 2 and 15 <= d.day <= 21


def is_last_weekday_of_month(d: date) -> bool:
    """Check if date is the last weekday (Monday-Friday) of its month."""
    _, last_day = calendar.monthrange(d.year, d.month)
    cur = date(d.year, d.month, last_day)
    while cur.weekday() >= 5:  # Saturday or Sunday
        cur = date(d.year, d.month, cur.day - 1)
    return d == cur


def skip_reason(
    d: date,
    fomc_days: Optional[List[str]] = None,
    is_holiday: bool = False,
    is_half_day: bool = False,
    skip_third_wednesday: bool = True,
    skip_last_weekday: bool = True,
) -> Optional[str]:
    """Return skip reason string if trading is blocked today, else None."""
    if is_holiday:
        return 'market_holiday'
    if is_half_day:
        return 'half_day'
    if skip_third_wednesday and is_third_wednesday(d):
        return 'third_wednesday'
    if skip_last_weekday and is_last_weekday_of_month(d):
        return 'last_weekday_of_month'
    if fomc_days:
        d_str = d.isoformat()
        if d_str in fomc_days:
            return 'fomc_decision_day'
    return None


def atm_center(spot: float, rounding: int = 5) -> float:
    """Round SPX spot price to the nearest strike increment (default 5)."""
    return float(round(spot / rounding) * rounding)


def straddle(chain: Dict[Tuple[str, float], Any], strike: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (straddle_mid, call_mid, put_mid) for the given strike from chain.

    Chain dict is keyed by (right, strike) where right is 'C' or 'P'.
    Values can be a tuple/list (mid, delta, bid, ask) or dict with 'mid'.
    """
    call_entry = chain.get(('C', strike))
    put_entry = chain.get(('P', strike))
    if call_entry is None or put_entry is None:
        return None, None, None

    call_mid = call_entry['mid'] if isinstance(call_entry, dict) else call_entry[0]
    put_mid = put_entry['mid'] if isinstance(put_entry, dict) else put_entry[0]

    if call_mid is None or put_mid is None:
        return None, None, None

    return round(float(call_mid + put_mid), 2), float(call_mid), float(put_mid)


def first_fly_due(
    now_time: time,
    has_prior_flies: bool,
    initial_mid_credit: Optional[float] = None,
    rules: NfhRules = DEFAULT_RULES,
    em_open: Optional[float] = None,
    straddle_now: Optional[float] = None,
    em_initial_entry: bool = False,
) -> Tuple[bool, str]:
    """Check if the day's first fly should be placed.

    Default NFH logic:
    - At 10:35 AM Eastern, open an ATM, $50 wide IB in SPX.
    - If em_initial_entry is enabled, requires straddle <= 83% of open EM.
    - Checks initial position mid price is within [min_mid_price_initial, max_mid_price_initial].
    """
    if has_prior_flies:
        return False, 'already_have_flies_today'

    if now_time < rules.first_fly_time:
        return False, 'before_1035'
    if now_time >= rules.adds_cutoff:
        return False, 'after_1550'

    # Optional EM compression trigger if specifically enabled
    if em_initial_entry and em_open is not None:
        if straddle_now is None:
            return False, 'waiting_for_straddle_quote'
        target = round(0.83 * em_open, 2)
        if round(straddle_now, 2) > target:
            return False, f'waiting_for_straddle_target_{target:.2f}'

    # Mid price validation if provided
    if initial_mid_credit is not None:
        if initial_mid_credit < rules.min_mid_price_initial:
            return False, f'credit_{initial_mid_credit:.2f}_below_min_{rules.min_mid_price_initial}'
        if initial_mid_credit > rules.max_mid_price_initial:
            return False, f'credit_{initial_mid_credit:.2f}_above_max_{rules.max_mid_price_initial}'

    return True, 'first_fly_due'


def is_tight_mode(
    now_time: time,
    mid_50w: Optional[float] = None,
    rules: NfhRules = DEFAULT_RULES,
) -> bool:
    """Check if tight threshold mode is active.

    Activates when:
    - now_time >= tight_threshold_time (14:00 ET) AND
      (mid_50w < tight_mid_price_threshold OR now_time >= late_tight_threshold_time (15:30 ET))
    """
    if now_time < rules.tight_threshold_time:
        return False
    if now_time >= rules.late_tight_threshold_time:
        return True
    if mid_50w is not None and mid_50w < rules.tight_mid_price_threshold:
        return True
    return False


def evaluate_add_trigger(
    spot: float,
    closest_fly_center: float,
    closest_fly_credit: float,
    now_time: time,
    mid_50w: Optional[float] = None,
    rules: NfhRules = DEFAULT_RULES,
) -> Tuple[bool, str, float, float, float]:
    """Evaluate whether an additional IB should be opened based on the closest open IB.

    Returns:
        (triggered, scenario_name, itm_threshold, center_offset, itm_distance)
    """
    itm_distance = round(abs(spot - closest_fly_center), 2)

    tight_active = is_tight_mode(now_time, mid_50w, rules)
    if tight_active:
        threshold = rules.itm_tight_threshold   # 2.5
        offset = rules.offset_tight             # 5.0
        scenario = 'tight'
        triggered = itm_distance > threshold
        return triggered, scenario, threshold, offset, itm_distance

    # Standard period: evaluate opening credit of closest IB
    if closest_fly_credit > rules.high_trade_price_threshold:
        threshold = rules.itm_high_threshold    # 15.0
        offset = rules.offset_high              # 20.0
        scenario = 'high_credit'
    elif closest_fly_credit >= rules.low_trade_price_threshold:
        threshold = rules.itm_mid_threshold     # 10.0
        offset = rules.offset_mid               # 15.0
        scenario = 'mid_credit'
    else:
        threshold = rules.itm_low_threshold     # 7.5
        offset = rules.offset_low               # 10.0
        scenario = 'low_credit'

    triggered = itm_distance > threshold
    return triggered, scenario, threshold, offset, itm_distance


def next_center(
    spot: float,
    closest_center: float,
    offset: float,
    open_centers: List[float],
    long_legs_map: Optional[Dict[Tuple[str, float], int]] = None,
    rounding: int = 5,
) -> Optional[float]:
    """Calculate the strike center for the new fly.

    Moves ±offset in the direction spot moved relative to the closest open fly center.
    If spot has moved further than the standard offset, aligns with the move rounded to nearest 5.
    Enforces strike clash prevention (§2.6a).
    """
    direction = 1 if spot > closest_center else -1
    candidate = closest_center + direction * offset

    # If spot moved significantly past the target offset, place candidate near spot
    dist = abs(spot - closest_center)
    if dist >= (offset + rounding):
        candidate = atm_center(spot, rounding=rounding)
        if direction > 0 and candidate < spot:
            candidate += rounding
        elif direction < 0 and candidate > spot:
            candidate -= rounding

    candidate = float(round(candidate / rounding) * rounding)

    if candidate in open_centers:
        return None

    # §2.6a Clash check: do not short a strike the book is already long
    if long_legs_map:
        if long_legs_map.get(('C', candidate), 0) > 0 or long_legs_map.get(('P', candidate), 0) > 0:
            return None

    return candidate


def _get_quote_mid(chain: Dict[Tuple[str, float], Any], right: str, strike: float) -> Optional[float]:
    entry = chain.get((right, strike))
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get('mid')
    return entry[0]


def _get_quote_bid_ask(chain: Dict[Tuple[str, float], Any], right: str, strike: float) -> Tuple[Optional[float], Optional[float]]:
    entry = chain.get((right, strike))
    if entry is None:
        return None, None
    if isinstance(entry, dict):
        return entry.get('bid'), entry.get('ask')
    if len(entry) >= 4:
        return entry[2], entry[3]
    return None, None


def pick_widths(
    chain: Dict[Tuple[str, float], Any],
    center: float,
    short_legs_map: Optional[Dict[Tuple[str, float], int]] = None,
    target_price: float = 0.40,
    min_width: int = 20,
    max_width: int = 50,
    candidate_widths: Tuple[int, ...] = (20, 25, 30, 35, 40, 45, 50),
    is_initial: bool = False,
) -> Tuple[Optional[int], Optional[int]]:
    """Pick call and put widths enforcing clash rules and width bounds.

    - Initial fly: strictly 50 points wide (or capped at max_width).
    - Additional flies: target ~$0.40 long put wing, bounded between min_width (20) and max_width (50).
      Symmetric wings on call side if valid quote and no clash.
    """
    short_map = short_legs_map or {}

    # Initial fly is exactly 50 wide
    if is_initial:
        init_w = max_width
        c_strike = center + init_w
        p_strike = center - init_w
        if short_map.get(('C', c_strike), 0) == 0 and short_map.get(('P', p_strike), 0) == 0:
            return init_w, init_w

    valid_widths = [w for w in candidate_widths if min_width <= w <= max_width]
    if not valid_widths:
        valid_widths = [min_width]

    # Pick long put wing first (strategy anchors on $0.40 for long put wing)
    chosen_pw = None
    widest_quoted_pw = None
    for w in valid_widths:
        long_strike = center - w
        if short_map.get(('P', long_strike), 0) > 0:
            continue
        mid = _get_quote_mid(chain, 'P', long_strike)
        if mid is not None and mid > 0:
            widest_quoted_pw = w
            if mid <= target_price:
                chosen_pw = w
                break
    put_width = chosen_pw if chosen_pw is not None else widest_quoted_pw
    if put_width is None:
        return None, None

    # Pick long call wing: try matching put_width first (symmetric), else find closest width <= target_price
    call_width = None
    if short_map.get(('C', center + put_width), 0) == 0:
        c_mid = _get_quote_mid(chain, 'C', center + put_width)
        if c_mid is not None and c_mid > 0:
            call_width = put_width

    if call_width is None:
        chosen_cw = None
        widest_quoted_cw = None
        for w in valid_widths:
            long_strike = center + w
            if short_map.get(('C', long_strike), 0) > 0:
                continue
            mid = _get_quote_mid(chain, 'C', long_strike)
            if mid is not None and mid > 0:
                widest_quoted_cw = w
                if mid <= target_price:
                    chosen_cw = w
                    break
        call_width = chosen_cw if chosen_cw is not None else widest_quoted_cw

    if call_width is None:
        return None, None

    return int(call_width), int(put_width)


def price_fly(
    chain: Dict[Tuple[str, float], Any],
    center: float,
    cw: int,
    pw: int,
) -> Optional[Tuple[float, float, float]]:
    """Compute (mid_credit, natural_credit, natural_debit) for a 4-leg iron fly.

    Returns None if any leg quote is missing or invalid.
    """
    legs_info = [
        ('C', center, 'short'),
        ('P', center, 'short'),
        ('C', center + cw, 'long'),
        ('P', center - pw, 'long'),
    ]

    short_mids, long_mids = 0.0, 0.0
    short_bids, short_asks = 0.0, 0.0
    long_bids, long_asks = 0.0, 0.0

    for right, strike, role in legs_info:
        mid = _get_quote_mid(chain, right, strike)
        bid, ask = _get_quote_bid_ask(chain, right, strike)
        if mid is None:
            return None
        if bid is None:
            bid = mid
        if ask is None:
            ask = mid

        if role == 'short':
            short_mids += mid
            short_bids += bid
            short_asks += ask
        else:
            long_mids += mid
            long_bids += bid
            long_asks += ask

    mid_credit = round(short_mids - long_mids, 2)
    natural_credit = round(short_bids - long_asks, 2)
    natural_debit = round(short_asks - long_bids, 2)

    return mid_credit, natural_credit, natural_debit


def build_fly_legs(
    expiry_yymmdd: str,
    center: float,
    cw: int,
    pw: int,
    qty: int = 1,
    broker_format: str = 'tastytrade',
) -> List[ComboLeg]:
    """Build 4 opening ComboLegs for an Iron Fly."""
    sym_fn = build_tastytrade_symbol if broker_format == 'tastytrade' else build_schwab_symbol

    c_center = sym_fn(expiry_yymmdd, 'C', int(center))
    p_center = sym_fn(expiry_yymmdd, 'P', int(center))
    c_wing = sym_fn(expiry_yymmdd, 'C', int(center + cw))
    p_wing = sym_fn(expiry_yymmdd, 'P', int(center - pw))

    return [
        ComboLeg(c_center, 'SELL_TO_OPEN', qty),
        ComboLeg(p_center, 'SELL_TO_OPEN', qty),
        ComboLeg(c_wing, 'BUY_TO_OPEN', qty),
        ComboLeg(p_wing, 'BUY_TO_OPEN', qty),
    ]


def build_closing_legs(
    expiry_yymmdd: str,
    center: float,
    cw: int,
    pw: int,
    qty: int = 1,
    broker_format: str = 'tastytrade',
) -> List[ComboLeg]:
    """Build 4 closing ComboLegs for an Iron Fly."""
    sym_fn = build_tastytrade_symbol if broker_format == 'tastytrade' else build_schwab_symbol

    c_center = sym_fn(expiry_yymmdd, 'C', int(center))
    p_center = sym_fn(expiry_yymmdd, 'P', int(center))
    c_wing = sym_fn(expiry_yymmdd, 'C', int(center + cw))
    p_wing = sym_fn(expiry_yymmdd, 'P', int(center - pw))

    return [
        ComboLeg(c_center, 'BUY_TO_CLOSE', qty),
        ComboLeg(p_center, 'BUY_TO_CLOSE', qty),
        ComboLeg(c_wing, 'SELL_TO_CLOSE', qty),
        ComboLeg(p_wing, 'SELL_TO_CLOSE', qty),
    ]


def calculate_bbe_amount(
    opening_credit: float,
    now_time: time,
    rules: NfhRules = DEFAULT_RULES,
) -> float:
    """Calculate Beyond Breakeven (BBE) close amount incorporating PABBE and EOD switches."""
    amount = rules.beyond_breakeven_amount

    if rules.pabbe_close:
        if opening_credit > rules.high_pabbe_trade_price:
            amount = rules.high_pabbe_close_amount
        elif opening_credit >= rules.low_pabbe_trade_price:
            amount = rules.mid_pabbe_close_amount
        else:
            amount = rules.low_pabbe_close_amount

    # Time-based overrides take precedence
    if rules.late_bbe_adjustment and now_time >= rules.late_bbe_time:
        amount = rules.late_bbe_amount
    if rules.eod_bbe_adjustment and now_time >= rules.eod_bbe_time:
        amount = rules.eod_bbe_amount

    return round(amount, 4)


def should_cut(
    spot: float,
    be_low: float,
    be_high: float,
    beyond_be_amount: float = 0.01,
    current_ror: Optional[float] = None,
    min_close_ror: float = -0.90,
) -> Tuple[bool, str]:
    """Check if fly should be cut based on spot moving beyond breakeven.

    IBs are closed if the price moves beyond breakeven point by beyond_be_amount:
      - Downward breach: spot <= (be_low - beyond_be_amount)
      - Upward breach:   spot >= (be_high + beyond_be_amount)
    Position is NOT closed if return on risk (RoR) is < min_close_ror (default -90%).
    """
    if current_ror is not None and current_ror < min_close_ror:
        return False, 'ror_below_min_close'

    if spot <= (be_low - beyond_be_amount):
        return True, 'be_low_breach'
    if spot >= (be_high + beyond_be_amount):
        return True, 'be_high_breach'
    return False, ''


def check_rapid_move(
    spot_trail: List[Tuple[float, float]],
    now_ts: float,
    rules: NfhRules = DEFAULT_RULES,
) -> Tuple[bool, str]:
    """Check for rapid increase/decrease in spot price over 1-minute and 2-minute windows.

    spot_trail: list of (timestamp_in_seconds, spot_price).
    """
    if not spot_trail:
        return False, ''

    # 1-minute window
    pts_1m = [p for ts, p in spot_trail if (now_ts - ts) <= 60.0]
    if len(pts_1m) >= 2:
        range_1m = max(pts_1m) - min(pts_1m)
        if range_1m >= rules.rapid_move_1m_pts:
            return True, f'rapid_move_1m_{range_1m:.1f}pts'

    # 2-minute window
    pts_2m = [p for ts, p in spot_trail if (now_ts - ts) <= 120.0]
    if len(pts_2m) >= 2:
        range_2m = max(pts_2m) - min(pts_2m)
        if range_2m >= rules.rapid_move_2m_pts:
            return True, f'rapid_move_2m_{range_2m:.1f}pts'

    return False, ''


def intrinsic(spot: float, center: float, cw: int, pw: int) -> float:
    """Calculate the intrinsic expiration value of the 4-leg iron fly given settlement spot price.

    Long iron fly holder value to buy back = Call spread value + Put spread value.
    Call spread = max(0, spot - center) - max(0, spot - (center + cw))
    Put spread = max(0, center - spot) - max(0, (center - pw) - spot)
    """
    call_spread = max(0.0, spot - center) - max(0.0, spot - (center + cw))
    put_spread = max(0.0, center - spot) - max(0.0, (center - pw) - spot)
    return round(call_spread + put_spread, 4)


# Backward compatibility aliases for existing references
def tier_index(straddle_now: float, current_tier: int = 0) -> int:
    """Legacy helper for backward compatibility."""
    if straddle_now >= 37.0:
        new_tier = 4
    elif straddle_now >= 27.5:
        new_tier = 3
    elif straddle_now >= 23.0:
        new_tier = 2
    elif straddle_now >= 16.0:
        new_tier = 1
    else:
        new_tier = 0
    return max(current_tier, new_tier)


def tier_params(tier: int) -> Tuple[float, float]:
    """Legacy helper for backward compatibility."""
    if tier >= 4:
        return 25.0, 30.0
    if tier == 3:
        return 20.0, 25.0
    if tier == 2:
        return 15.0, 20.0
    if tier == 1:
        return 10.0, 15.0
    return 2.5, 5.0
