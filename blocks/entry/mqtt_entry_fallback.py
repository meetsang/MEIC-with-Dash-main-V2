"""REST-to-MQTT entry scan fallback orchestration (Phase 5)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from blocks.entry.entry_scan_config import (
    ENTRY_MQTT_READY_TIMEOUT_SEC,
    ENTRY_REST_MIN_COVERAGE_PCT,
)
from blocks.entry.entry_quote_validation import evaluate_entry_mqtt_pair
from common.market_quote import REPLAY_EVENT_KIND
from common.mqtt_prices import ensure_cache_started
from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST, PRIORITY_NORMAL
from common.symbols import to_tastytrade

if TYPE_CHECKING:
    from common.mqtt_prices import MqttPriceCache

log = logging.getLogger(__name__)


@dataclass
class EntryScanDiagnostics:
    rest_symbols_requested: int = 0
    rest_symbols_valid: int = 0
    mqtt_symbols_requested: int = 0
    mqtt_symbols_current_session: int = 0
    mqtt_symbols_post_scan: int = 0
    mqtt_valid_pairs: int = 0
    mqtt_rejection_reasons: Dict[str, int] = field(default_factory=dict)
    candidate_source: Optional[str] = None
    cooldown: bool = False
    rate_limited: bool = False
    scan_request_epoch: Optional[float] = None
    failure_reason: Optional[str] = None

    def record_mqtt_rejection(self, reason: str) -> None:
        self.mqtt_rejection_reasons[reason] = self.mqtt_rejection_reasons.get(reason, 0) + 1

    def update_mqtt_symbol_coverage(
        self,
        cache: 'MqttPriceCache',
        symbols: List[str],
        scan_request_epoch: float,
    ) -> None:
        current_session = cache.current_stream_session_id()
        session_count = 0
        post_scan_count = 0
        for sym in symbols:
            quote = cache.get_quote(
                sym,
                require_current_session=False,
                allow_override=False,
                allow_pre_subscription=True,
            )
            if quote is None:
                continue
            if current_session and quote.stream_session_id == current_session:
                session_count += 1
            if quote.source_event_epoch >= scan_request_epoch:
                post_scan_count += 1
        self.mqtt_symbols_current_session = session_count
        self.mqtt_symbols_post_scan = post_scan_count

    def format_failure(self) -> str:
        top_reason = ''
        if self.mqtt_rejection_reasons:
            top_reason = max(self.mqtt_rejection_reasons, key=self.mqtt_rejection_reasons.get)
        return (
            f'entry_scan_failed source=rest_then_mqtt '
            f'rest_coverage={self.rest_symbols_valid}/{self.rest_symbols_requested} '
            f'cooldown={str(self.cooldown).lower()} '
            f'mqtt_current_session={self.mqtt_symbols_current_session}/{self.mqtt_symbols_requested} '
            f'mqtt_post_scan={self.mqtt_symbols_post_scan}/{self.mqtt_symbols_requested} '
            f'mqtt_valid_pairs={self.mqtt_valid_pairs} '
            f'reason={top_reason or self.failure_reason or "no_valid_pairs"}'
        )


@dataclass
class RestEntryFetchResult:
    price_map: Dict[str, float]
    requested: int
    valid: int
    cooldown: bool
    rate_limited: bool
    should_fallback: bool


def mqtt_cache_for_broker(broker) -> 'MqttPriceCache':
    cache = getattr(broker, '_prices', None)
    if cache is not None:
        return cache
    return ensure_cache_started()


def _count_valid_rest_quotes(price_map: Dict[str, float], symbols: List[str]) -> int:
    valid = 0
    for sym in symbols:
        tt = to_tastytrade(sym)
        price = price_map.get(tt)
        if price is not None and float(price) > 0:
            valid += 1
    return valid


def attempt_rest_entry_quotes(
    broker,
    symbols: List[str],
    logger,
    *,
    chunk_size: int = 40,
) -> RestEntryFetchResult:
    """Fetch REST entry quotes with cooldown guard and coverage tracking."""
    from common.broker_cooldown import cooldown_active
    from common.rest_metrics import get_rest_metrics

    unique = list(dict.fromkeys(symbols))
    requested = len(unique)

    if cooldown_active():
        try:
            get_rest_metrics().record_skipped_cooldown(
                OPERATION_ENTRY_MARKET_DATA_REST, PRIORITY_NORMAL,
            )
        except Exception:
            pass
        logger.warning(
            'Entry REST market-data skipped — broker cooldown active (%d symbols)',
            requested,
        )
        return RestEntryFetchResult(
            price_map={},
            requested=requested,
            valid=0,
            cooldown=True,
            rate_limited=False,
            should_fallback=True,
        )

    out: Dict[str, float] = {}
    rate_limited = False
    for i in range(0, len(unique), chunk_size):
        batch = unique[i:i + chunk_size]
        try:
            out.update(broker.fetch_option_mids_api(batch))
        except Exception as exc:
            err = str(exc).lower()
            if '429' in err or 'rate limit' in err or 'cooldown' in err:
                rate_limited = True
                logger.warning('Entry REST batch halted: %s', exc)
                break
            logger.warning('Entry REST batch failed: %s', exc)
            continue

        if rate_limited:
            break

        missing = [sym for sym in batch if to_tastytrade(sym) not in out]
        if missing:
            for j in range(0, len(missing), 10):
                retry_batch = missing[j:j + 10]
                try:
                    out.update(broker.fetch_option_mids_api(retry_batch))
                except Exception as exc:
                    err = str(exc).lower()
                    if '429' in err or 'rate limit' in err or 'cooldown' in err:
                        rate_limited = True
                        logger.warning('Entry REST retry halted: %s', exc)
                        break
                    logger.warning('Entry REST retry failed: %s', exc)
            if rate_limited:
                break

    valid = _count_valid_rest_quotes(out, unique)
    coverage_pct = (100.0 * valid / requested) if requested else 0.0
    should_fallback = rate_limited or coverage_pct < ENTRY_REST_MIN_COVERAGE_PCT
    logger.info(
        'Entry REST quotes: %d/%d valid (%.0f%%) rate_limited=%s fallback=%s',
        valid, requested, coverage_pct, rate_limited, should_fallback,
    )
    return RestEntryFetchResult(
        price_map=out,
        requested=requested,
        valid=valid,
        cooldown=False,
        rate_limited=rate_limited,
        should_fallback=should_fallback,
    )


def prepare_mqtt_entry_fallback(
    broker,
    *,
    expiry: str,
    opt_type: str,
    spx_price: int,
    lot: str,
    logger,
    spread_widths: List[int],
    otm_min: int,
    otm_max: int,
    diagnostics: EntryScanDiagnostics,
) -> float:
    """Register scan symbols and wait for post-scan MQTT quotes."""
    from blocks.entry.spread_scan import _register_scan_symbols, scan_symbol_list

    schwab_symbols: List[str] = []
    for width in spread_widths:
        schwab_symbols.extend(
            scan_symbol_list(
                expiry, opt_type, spx_price,
                spread_width=width, otm_min=otm_min, otm_max=otm_max,
            )
        )
    unique = list(dict.fromkeys(schwab_symbols))
    diagnostics.mqtt_symbols_requested = len(unique)

    scan_request_epoch = time.time()
    diagnostics.scan_request_epoch = scan_request_epoch

    _register_scan_symbols(
        broker, expiry, opt_type, spx_price, lot, logger,
        spread_widths=spread_widths, otm_min=otm_min, otm_max=otm_max,
    )

    cache = mqtt_cache_for_broker(broker)
    deadline = scan_request_epoch + ENTRY_MQTT_READY_TIMEOUT_SEC
    last_post_scan = -1
    while time.time() < deadline:
        diagnostics.update_mqtt_symbol_coverage(cache, unique, scan_request_epoch)
        if diagnostics.mqtt_symbols_post_scan > last_post_scan:
            last_post_scan = diagnostics.mqtt_symbols_post_scan
            if diagnostics.mqtt_symbols_post_scan >= len(unique):
                break
        time.sleep(0.25)

    diagnostics.update_mqtt_symbol_coverage(cache, unique, scan_request_epoch)
    logger.info(
        'MQTT entry fallback ready: session %d/%d post_scan %d/%d',
        diagnostics.mqtt_symbols_current_session,
        diagnostics.mqtt_symbols_requested,
        diagnostics.mqtt_symbols_post_scan,
        diagnostics.mqtt_symbols_requested,
    )
    return scan_request_epoch


def evaluate_mqtt_entry_pair(
    broker,
    short_symbol: str,
    long_symbol: str,
    *,
    scan_request_epoch: float,
    spread_width: int,
    diagnostics: Optional[EntryScanDiagnostics] = None,
) -> Optional[tuple[float, float]]:
    """Return (short_mid, long_mid) when MQTT pair passes entry validation."""
    cache = mqtt_cache_for_broker(broker)
    if cache.last_event_kind(short_symbol) == REPLAY_EVENT_KIND:
        if diagnostics:
            diagnostics.record_mqtt_rejection('replay_event')
        return None
    if cache.last_event_kind(long_symbol) == REPLAY_EVENT_KIND:
        if diagnostics:
            diagnostics.record_mqtt_rejection('replay_event')
        return None

    readiness = evaluate_entry_mqtt_pair(
        cache,
        short_symbol,
        long_symbol,
        scan_request_epoch=scan_request_epoch,
        spread_width=spread_width,
    )
    if not readiness.quote_pair_valid:
        if diagnostics:
            diagnostics.record_mqtt_rejection(readiness.quote_pair_reason)
        return None
    if diagnostics:
        diagnostics.mqtt_valid_pairs += 1
    return readiness.short_mid, readiness.long_mid
