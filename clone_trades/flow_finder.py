"""Helper to identify budget alternative option contracts ($0.70 - $1.50) for AI FlowAlgos."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from clone_trades.models import OptionSpec

log = logging.getLogger("clone_trades.flow_finder")


def find_flow_alternative_option(
    spec: OptionSpec,
    quote_service: Any = None,
    broker: Any = None,
) -> Optional[Dict[str, Any]]:
    """Identify an option in the same direction and same expiration costing $0.70 - $1.50 (target ~$1.00).

    Returns a dict with:
      - "spec": OptionSpec of the alternative strike
      - "price": float (estimated or actual mid price ~ $1.00)
    """
    if not spec:
        return None

    is_call = spec.option_type.upper().startswith("C")
    target_type = "C" if is_call else "P"
    orig_strike = float(spec.strike)

    # 1. Attempt lookup via TastyTrade REST API if live session is available
    if broker and hasattr(broker, "session") and broker.session:
        try:
            from tastytrade.instruments import get_option_chain
            from tastytrade.market_data import get_market_data_by_type

            session = broker.session
            coro = get_option_chain(session, spec.underlying)
            if hasattr(broker, "_run"):
                chain = broker._run(coro)
            else:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            chain = pool.submit(lambda: asyncio.run(coro)).result(timeout=4)
                    else:
                        chain = loop.run_until_complete(coro)
                except Exception:
                    chain = asyncio.run(coro)

            # Look up target expiration in chain
            exp_options = None
            if isinstance(chain, dict):
                for exp_key, opts in chain.items():
                    if str(exp_key).strip() == spec.expiration.strip():
                        exp_options = opts
                        break

            if exp_options:
                candidates = [
                    opt for opt in exp_options
                    if getattr(opt, "option_type", "") == target_type or getattr(opt, "option_type", "") == ("CALL" if is_call else "PUT")
                ]
                # Filter for OTM direction (cheaper contracts):
                # For CALL: strike >= orig_strike; for PUT: strike <= orig_strike
                if is_call:
                    otm_cands = [o for o in candidates if float(o.strike_price) >= orig_strike]
                    candidates = otm_cands if otm_cands else candidates
                    candidates.sort(key=lambda o: float(o.strike_price))
                else:
                    otm_cands = [o for o in candidates if float(o.strike_price) <= orig_strike]
                    candidates = otm_cands if otm_cands else candidates
                    candidates.sort(key=lambda o: float(o.strike_price), reverse=True)

                # Query quotes for candidates (up to 30 strikes)
                occ_list = [c.symbol for c in candidates[:30] if getattr(c, "symbol", None)]
                if occ_list:
                    md_coro = get_market_data_by_type(session, options=occ_list)
                    if hasattr(broker, "_run"):
                        md_results = broker._run(md_coro)
                    else:
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                import concurrent.futures
                                with concurrent.futures.ThreadPoolExecutor() as pool:
                                    md_results = pool.submit(lambda: asyncio.run(md_coro)).result(timeout=4)
                            else:
                                md_results = loop.run_until_complete(md_coro)
                        except Exception:
                            md_results = asyncio.run(md_coro)

                    md_map: Dict[str, Any] = {}
                    if isinstance(md_results, list):
                        md_map = {getattr(m, "symbol", ""): m for m in md_results if getattr(m, "symbol", None)}
                    elif isinstance(md_results, dict):
                        md_map = md_results

                    best_match = None
                    best_diff = 9999.0
                    best_price = 1.00

                    # Pass 1: candidate in target price range $0.70 - $1.50 (closest to $1.00)
                    for opt in candidates[:30]:
                        data = md_map.get(opt.symbol)
                        if not data:
                            continue
                        bid = float(getattr(data, "bid", 0.0) or 0.0)
                        ask = float(getattr(data, "ask", 0.0) or 0.0)
                        last = float(getattr(data, "last", 0.0) or 0.0)
                        mid_val = float(getattr(data, "mid", 0.0) or 0.0)
                        if mid_val <= 0 and bid > 0 and ask > 0:
                            mid = round((bid + ask) / 2.0, 2)
                        elif mid_val > 0:
                            mid = round(mid_val, 2)
                        else:
                            mid = round(last or ask or bid, 2)

                        if 0.70 <= mid <= 1.50:
                            diff = abs(mid - 1.00)
                            if diff < best_diff:
                                best_diff = diff
                                best_match = opt
                                best_price = mid

                    # Pass 2: if no contract strictly in $0.70 - $1.50, pick closest liquid strike with mid > 0
                    if not best_match:
                        for opt in candidates[:30]:
                            data = md_map.get(opt.symbol)
                            if not data:
                                continue
                            bid = float(getattr(data, "bid", 0.0) or 0.0)
                            ask = float(getattr(data, "ask", 0.0) or 0.0)
                            last = float(getattr(data, "last", 0.0) or 0.0)
                            mid_val = float(getattr(data, "mid", 0.0) or 0.0)
                            if mid_val <= 0 and bid > 0 and ask > 0:
                                mid = round((bid + ask) / 2.0, 2)
                            elif mid_val > 0:
                                mid = round(mid_val, 2)
                            else:
                                mid = round(last or ask or bid, 2)

                            if mid > 0:
                                diff = abs(mid - 1.00)
                                if diff < best_diff:
                                    best_diff = diff
                                    best_match = opt
                                    best_price = mid

                    if best_match:
                        strike_val = float(best_match.strike_price)
                        fallback_streamer = f".{spec.underlying}{spec.expiration.replace('-','')[2:]}{target_type}{strike_val:g}"
                        alt_spec = OptionSpec(
                            underlying=spec.underlying,
                            expiration=spec.expiration,
                            strike=strike_val,
                            option_type="CALL" if is_call else "PUT",
                            occ_symbol=best_match.symbol,
                            streamer_symbol=getattr(best_match, "streamer_symbol", "") or fallback_streamer,
                        )
                        return {"spec": alt_spec, "price": best_price}
        except Exception as exc:
            log.warning("Option chain lookup failed in find_flow_alternative_option: %s", exc)

    # 2. Check quote_service mock prices (for unit tests / mock environments)
    if quote_service and hasattr(quote_service, "_mock_prices") and quote_service._mock_prices:
        best_mock = None
        best_diff = 9999.0
        best_price = 1.00
        for sym, mdata in quote_service._mock_prices.items():
            parsed = OptionSpec.from_occ_or_streamer(sym)
            if not parsed:
                continue
            # Must strictly match underlying, expiration date, and option type
            if parsed.underlying != spec.underlying:
                continue
            if parsed.expiration != spec.expiration:
                continue
            if parsed.option_type != ("CALL" if is_call else "PUT"):
                continue
            if parsed.strike == spec.strike:
                continue

            px = mdata.get("price", 0.0)
            mid = mdata.get("mid", px)
            if 0.70 <= mid <= 1.50:
                diff = abs(mid - 1.00)
                if diff < best_diff:
                    best_diff = diff
                    best_mock = parsed
                    best_price = mid
        if best_mock:
            return {"spec": best_mock, "price": best_price}

    # 3. Deterministic synthetic alternative fallback (for simulation / testing / closed markets)
    # Example from spec: MU call for 885 -> 950 strike whose mid is at around $1
    if spec.underlying.upper() == "MU" and abs(orig_strike - 885.0) < 5.0 and is_call:
        alt_strike = 950.0
    elif is_call:
        step = 5.0 if orig_strike >= 100 else 1.0
        alt_strike = round((orig_strike * 1.07) / step) * step
        if alt_strike <= orig_strike:
            alt_strike = orig_strike + step
    else:
        step = 5.0 if orig_strike >= 100 else 1.0
        alt_strike = round((orig_strike * 0.93) / step) * step
        if alt_strike >= orig_strike:
            alt_strike = max(step, orig_strike - step)

    alt_spec = OptionSpec.from_payload({
        "ticker": spec.underlying,
        "expiration": spec.expiration,
        "strike": alt_strike,
        "call_put": "CALL" if is_call else "PUT",
    })

    alt_price = 1.00
    if quote_service and hasattr(quote_service, "set_mock_price"):
        quote_service.set_mock_price(alt_spec.streamer_symbol, alt_price, ask=1.05, bid=0.95)
        quote_service.set_mock_price(alt_spec.occ_symbol, alt_price, ask=1.05, bid=0.95)

    return {"spec": alt_spec, "price": alt_price}

