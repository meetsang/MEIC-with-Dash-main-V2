"""Helper to identify further OTM candidate options for Last-Rung Runner Roll."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from clone_trades.models import OptionSpec

log = logging.getLogger("clone_trades.runner_roll")


def find_runner_roll_option(
    spec: OptionSpec,
    target_price: float,
    tolerance_pct: float = 25.0,
    quote_service: Any = None,
    broker: Any = None,
) -> Optional[Dict[str, Any]]:
    """Identify a further OTM contract from option chain in same direction trading around target_price (+-25%).

    Parameters:
      spec: OptionSpec of the completed / rolling trade
      target_price: Entry cost to target (e.g. $0.50)
      tolerance_pct: Search tolerance percentage around target_price (default 25.0%)
      quote_service: OptionQuoteService instance
      broker: Live broker instance

    Returns a dict with:
      - "spec": OptionSpec of the candidate strike
      - "price": float (estimated or actual entry price)
    """
    if not spec:
        return None

    if target_price <= 0.0:
        target_price = 0.50

    min_px = max(0.01, round(target_price * (1.0 - (tolerance_pct / 100.0)), 2))
    max_px = round(target_price * (1.0 + (tolerance_pct / 100.0)), 2)

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

            # Match target expiration in chain
            exp_options = None
            if isinstance(chain, dict):
                for exp_key, opts in chain.items():
                    if str(exp_key).strip() == spec.expiration.strip():
                        exp_options = opts
                        break

            if exp_options:
                candidates = [
                    opt for opt in exp_options
                    if getattr(opt, "option_type", "") == target_type
                    or getattr(opt, "option_type", "") == ("CALL" if is_call else "PUT")
                ]
                # Filter for strictly further OTM:
                # For CALL: cand_strike > orig_strike (higher strike)
                # For PUT:  cand_strike < orig_strike (lower strike)
                if is_call:
                    candidates = [o for o in candidates if float(o.strike_price) > orig_strike]
                    candidates.sort(key=lambda o: float(o.strike_price))
                else:
                    candidates = [o for o in candidates if float(o.strike_price) < orig_strike]
                    candidates.sort(key=lambda o: float(o.strike_price), reverse=True)

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
                    best_diff = 99999.0
                    best_price = target_price

                    # Pass 1: candidates within tolerance window [min_px, max_px], closer one better
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

                        if min_px <= mid <= max_px:
                            diff = abs(mid - target_price)
                            if diff < best_diff:
                                best_diff = diff
                                best_match = opt
                                best_price = mid

                    # Pass 2: fallback to any further OTM with positive quote, closest to target_price
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
                                diff = abs(mid - target_price)
                                if diff < best_diff:
                                    best_diff = diff
                                    best_match = opt
                                    best_price = mid

                    if best_match:
                        strike_val = float(best_match.strike_price)
                        fallback_streamer = f".{spec.underlying}{spec.expiration.replace('-','')[2:]}{target_type}{strike_val:g}"
                        cand_spec = OptionSpec(
                            underlying=spec.underlying,
                            expiration=spec.expiration,
                            strike=strike_val,
                            option_type="CALL" if is_call else "PUT",
                            occ_symbol=best_match.symbol,
                            streamer_symbol=getattr(best_match, "streamer_symbol", "") or fallback_streamer,
                        )
                        log.info(
                            "RUNNER ROLL OPTION FOUND (Broker): %s strike %.2f @ $%.2f (target: $%.2f)",
                            cand_spec.underlying, cand_spec.strike, best_price, target_price
                        )
                        return {"spec": cand_spec, "price": best_price}

        except Exception as exc:
            log.warning("Option chain lookup failed in find_runner_roll_option: %s", exc)

    # 2. Check quote_service mock prices (for unit tests / mock environments)
    if quote_service and hasattr(quote_service, "_mock_prices") and quote_service._mock_prices:
        best_mock = None
        best_diff = 99999.0
        best_price = target_price

        # Pass 1: candidates in tolerance window
        for sym, mdata in quote_service._mock_prices.items():
            parsed = OptionSpec.from_occ_or_streamer(sym)
            if not parsed:
                continue
            if parsed.underlying != spec.underlying or parsed.expiration != spec.expiration:
                continue
            if parsed.option_type != ("CALL" if is_call else "PUT"):
                continue

            # Must be strictly further OTM
            if is_call and parsed.strike <= orig_strike:
                continue
            if not is_call and parsed.strike >= orig_strike:
                continue

            px = mdata.get("price", 0.0)
            mid = mdata.get("mid", px)
            if min_px <= mid <= max_px:
                diff = abs(mid - target_price)
                if diff < best_diff:
                    best_diff = diff
                    best_mock = parsed
                    best_price = mid

        # Pass 2: fallback to any positive mock price
        if not best_mock:
            for sym, mdata in quote_service._mock_prices.items():
                parsed = OptionSpec.from_occ_or_streamer(sym)
                if not parsed:
                    continue
                if parsed.underlying != spec.underlying or parsed.expiration != spec.expiration:
                    continue
                if parsed.option_type != ("CALL" if is_call else "PUT"):
                    continue
                if is_call and parsed.strike <= orig_strike:
                    continue
                if not is_call and parsed.strike >= orig_strike:
                    continue

                px = mdata.get("price", 0.0)
                mid = mdata.get("mid", px)
                if mid > 0:
                    diff = abs(mid - target_price)
                    if diff < best_diff:
                        best_diff = diff
                        best_mock = parsed
                        best_price = mid

        if best_mock:
            log.info(
                "RUNNER ROLL OPTION FOUND (Mock): %s strike %.2f @ $%.2f (target: $%.2f)",
                best_mock.underlying, best_mock.strike, best_price, target_price
            )
            return {"spec": best_mock, "price": best_price}

    # 3. Deterministic synthetic alternative fallback (for simulation / testing / closed markets)
    step = 5.0 if orig_strike >= 100 else 1.0
    if is_call:
        cand_strike = orig_strike + step
    else:
        cand_strike = max(step, orig_strike - step)

    cand_spec = OptionSpec.from_payload({
        "ticker": spec.underlying,
        "expiration": spec.expiration,
        "strike": cand_strike,
        "call_put": "CALL" if is_call else "PUT",
    })

    cand_price = round(target_price, 2)
    if quote_service and hasattr(quote_service, "set_mock_price"):
        quote_service.set_mock_price(cand_spec.streamer_symbol, cand_price, ask=cand_price + 0.02, bid=max(0.01, cand_price - 0.02))
        quote_service.set_mock_price(cand_spec.occ_symbol, cand_price, ask=cand_price + 0.02, bid=max(0.01, cand_price - 0.02))

    log.info(
        "RUNNER ROLL OPTION SYNTHETIC: %s strike %.2f @ $%.2f (target: $%.2f)",
        cand_spec.underlying, cand_spec.strike, cand_price, target_price
    )
    return {"spec": cand_spec, "price": cand_price}

