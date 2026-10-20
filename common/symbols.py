"""Option symbol translation between Schwab, TastyTrade, and canonical OCC form."""
from __future__ import annotations

import re
from typing import Optional, Tuple

# Schwab:  SPXW  260619P05550000  or SPXW 240426P5060
_SCHWAB_RE = re.compile(
    r'^SPXW\s+(\d{6})([CP])(\d+)\s*$', re.IGNORECASE
)
# TastyTrade: .SPXW260619P5550
_TT_RE = re.compile(
    r'^\.?SPXW(\d{6})([CP])(\d+)$', re.IGNORECASE
)


def parse_canonical(symbol: str) -> Optional[Tuple[str, str, int]]:
    """
    Parse any supported symbol into (expiry_yymmdd, opt_type, strike_int).

    expiry is 6-digit YYMMDD, opt_type is C or P, strike is integer dollars.
    """
    symbol = symbol.strip()
    m = _SCHWAB_RE.match(symbol)
    if m:
        expiry = m.group(1)
        opt_type = m.group(2).upper()
        strike_raw = m.group(3)
        # OCC padded strike (e.g. 05550000) is strike * 1000; short form is dollars.
        if len(strike_raw) >= 8:
            strike = int(strike_raw) // 1000
        else:
            strike = int(strike_raw.lstrip('0') or '0')
        return expiry, opt_type, strike
    m = _TT_RE.match(symbol)
    if m:
        return m.group(1), m.group(2).upper(), int(m.group(3))
    return None


def to_schwab(symbol: str) -> str:
    """Convert any supported symbol to Schwab/OCC format."""
    parsed = parse_canonical(symbol)
    if parsed:
        expiry, opt_type, strike = parsed
        return f'SPXW  {expiry}{opt_type}0{strike}000'

    try:
        from clone_trades.models import OptionSpec
        spec = OptionSpec.from_occ_or_streamer(symbol)
        if spec:
            return spec.occ_symbol
    except Exception:
        pass

    return symbol


def to_tastytrade(symbol: str) -> str:
    """Convert any supported symbol to TastyTrade streamer format."""
    parsed = parse_canonical(symbol)
    if parsed:
        expiry, opt_type, strike = parsed
        return f'.SPXW{expiry}{opt_type}{strike}'

    try:
        from clone_trades.models import OptionSpec
        spec = OptionSpec.from_occ_or_streamer(symbol)
        if spec:
            return spec.streamer_symbol
    except Exception:
        pass

    return symbol if symbol.startswith('.') else f'.{symbol}'


def build_schwab_symbol(expiry_yymmdd: str, opt_type: str, strike: int, ticker: str = 'SPXW') -> str:
    clean_ticker = ticker.lstrip('.').strip()
    return f'{clean_ticker:<6}{expiry_yymmdd}{opt_type.upper()}{int(strike) * 1000:08d}'


def build_tastytrade_symbol(expiry_yymmdd: str, opt_type: str, strike: int, ticker: str = 'SPXW') -> str:
    clean_ticker = ticker.lstrip('.').strip()
    return f'.{clean_ticker}{expiry_yymmdd}{opt_type.upper()}{strike}'


def strike_from_symbol(symbol: str) -> Optional[int]:
    parsed = parse_canonical(symbol)
    return parsed[2] if parsed else None


def symbols_equivalent(a: str, b: str) -> bool:
    """True when two Schwab/TastyTrade/OCC symbols refer to the same contract."""
    pa, pb = parse_canonical(a), parse_canonical(b)
    if pa and pb:
        return pa == pb
    return to_tastytrade(a) == to_tastytrade(b)


def mqtt_topic_symbol(symbol: str, broker: str = 'tastytrade') -> str:
    """Symbol string used in MQTT topic suffix."""
    if broker == 'schwab':
        return to_schwab(symbol)
    return to_tastytrade(symbol)


FUTURES_PRODUCT_CODES = {
    'ES', 'MES', 'NQ', 'MNQ', 'YM', 'MYM', 'RTY', 'M2K',
    'CL', 'QM', 'GC', 'MGC', 'ZB', 'ZN', 'ZF', 'ZT',
}


def is_futures_symbol(symbol: str) -> bool:
    """True when the symbol represents a futures contract or product code."""
    if not symbol:
        return False
    s = str(symbol).strip().upper()
    if s.startswith('/'):
        return True
    return s in FUTURES_PRODUCT_CODES


def to_futures_product_code(symbol: str) -> str:
    """Extract standard futures root product code (e.g. '/ES' -> 'ES', '/ESZ6' -> 'ES')."""
    if not symbol:
        return 'ES'
    s = str(symbol).strip().upper().lstrip('/')
    for prod in sorted(FUTURES_PRODUCT_CODES, key=len, reverse=True):
        if s.startswith(prod):
            return prod
    return s
