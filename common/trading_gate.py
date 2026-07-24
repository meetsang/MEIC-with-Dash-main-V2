"""Cross-process new-risk REST gate — runtime/trading_gate.json."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from common.broker_cooldown import cooldown_active, cooldown_snapshot
from common.data_utils import load_json_safe, save_json_safe

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_GATE_PATH = os.path.join(ROOT, 'runtime', 'trading_gate.json')
SCHEMA_VERSION = 1

_LOCK = threading.Lock()

REST_HEALTHY = 'healthy'
REST_STATUSES_BLOCKING = frozenset({
    'rate_limited', 'auth_failed', 'unavailable', 'unknown', 'timed_out',
})


def _gate_path() -> str:
    return os.environ.get('MEIC_TRADING_GATE_PATH', DEFAULT_GATE_PATH)


def gate_enabled() -> bool:
    return os.environ.get('NEW_RISK_GATE_ENABLED', 'true').lower() in ('1', 'true', 'yes')


def _require_operator_resume() -> bool:
    return os.environ.get('NEW_RISK_REQUIRE_OPERATOR_RESUME', 'true').lower() in ('1', 'true', 'yes')


def _rest_ready_max_age_sec() -> float:
    return float(os.environ.get('REST_READY_MAX_AGE_SEC', '60'))


def _default_state(session_date_ct: str) -> Dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'session_date_ct': session_date_ct,
        'generation': 0,
        'rest_status': 'unknown',
        'rest_detail': '',
        'rest_source': '',
        'rest_observed_at_epoch': None,
        'new_risk_latched': False,
        'latch_reason': '',
        'latch_source': '',
        'latched_at_epoch': None,
        'cleared_at_epoch': None,
        'cleared_by': None,
        'last_probe': None,
        'last_successful_probe_epoch': None,
        'last_resume_epoch': None,
        'startup_probe': None,
        'probes_by_tranche': {},
    }


def read_state() -> Dict[str, Any]:
    data = load_json_safe(_gate_path())
    if not isinstance(data, dict):
        return _default_state('')
    return data


def _write_state(state: Dict[str, Any]) -> None:
    save_json_safe(_gate_path(), state)


def _mutate(mutator) -> Dict[str, Any]:
    with _LOCK:
        state = read_state()
        mutator(state)
        state['generation'] = int(state.get('generation') or 0) + 1
        _write_state(state)
        return state


def initialize_for_session_date(session_date_ct: str) -> Dict[str, Any]:
    """Start-of-day gate init — preserve same-day cooldown, reset prior-day latch."""
    with _LOCK:
        state = read_state()
        if state.get('session_date_ct') == session_date_ct and state.get('schema_version') == SCHEMA_VERSION:
            return state
        fresh = _default_state(session_date_ct)
        if cooldown_active():
            fresh['rest_status'] = 'rate_limited'
            fresh['rest_detail'] = 'broker cooldown active at session init'
            fresh['new_risk_latched'] = True
            fresh['latch_reason'] = 'cooldown_active_at_session_start'
            fresh['latch_source'] = 'session_init'
            fresh['latched_at_epoch'] = time.time()
        state = fresh
        state['generation'] = 1
        _write_state(state)
        log.info('Trading gate initialized for session %s', session_date_ct)
        return state


def rest_healthy() -> bool:
    if not gate_enabled():
        return True
    return read_state().get('rest_status') == REST_HEALTHY


def effective_new_risk_blocked() -> bool:
    if not gate_enabled():
        return False
    if cooldown_active():
        return True
    state = read_state()
    if state.get('new_risk_latched'):
        return True
    return state.get('rest_status') != REST_HEALTHY


@dataclass(frozen=True)
class GateDecision:
    blocked: bool
    reason: str = ''
    detail: str = ''


def _blocked_reason(state: Dict[str, Any]) -> GateDecision:
    if cooldown_active():
        snap = cooldown_snapshot()
        return GateDecision(
            blocked=True,
            reason='broker_cooldown_active',
            detail=str(snap.get('reason') or 'broker cooldown active'),
        )
    if state.get('new_risk_latched'):
        return GateDecision(
            blocked=True,
            reason=str(state.get('latch_reason') or 'new_risk_latched'),
            detail=str(state.get('rest_detail') or state.get('latch_source') or ''),
        )
    rest = state.get('rest_status') or 'unknown'
    if rest != REST_HEALTHY:
        return GateDecision(
            blocked=True,
            reason=f'rest_{rest}',
            detail=str(state.get('rest_detail') or ''),
        )
    return GateDecision(blocked=False)


def evaluate_new_risk_gate(
    *,
    require_fresh_probe: bool = False,
    strategy: str = '',
    tranche_id: Optional[str] = None,
) -> GateDecision:
    """Non-blocking gate evaluation — never creates a broker or runs REST.

    For MEIC scheduled spawn pass tranche_id (lot, e.g. '11-00'). REST readiness
    comes from the coordinator's single pre_tranche probe for that key.
    """
    if not gate_enabled():
        return GateDecision(blocked=False)

    state = read_state()

    # Always honor cooldown + latch (Jul 10)
    if cooldown_active():
        snap = cooldown_snapshot()
        return GateDecision(
            blocked=True,
            reason='broker_cooldown_active',
            detail=str(snap.get('reason') or 'broker cooldown active'),
        )
    if state.get('new_risk_latched'):
        return GateDecision(
            blocked=True,
            reason=str(state.get('latch_reason') or 'new_risk_latched'),
            detail=str(state.get('rest_detail') or state.get('latch_source') or ''),
        )

    if not require_fresh_probe:
        return GateDecision(blocked=False)

    if os.environ.get('REST_PROBE_BEFORE_NEW_ENTRY', 'true').lower() not in ('1', 'true', 'yes'):
        return GateDecision(blocked=False)

    if tranche_id:
        return _tranche_rest_readiness(
            state,
            strategy=strategy or 'MEIC_IC',
            tranche_id=tranche_id,
        )

    # Manual / non-tranche: global REST must be healthy + recent success on file
    rest = state.get('rest_status') or 'unknown'
    if rest != REST_HEALTHY:
        return GateDecision(
            blocked=True,
            reason=f'rest_{rest}',
            detail=str(state.get('rest_detail') or ''),
        )
    last_ok = float(state.get('last_successful_probe_epoch') or 0)
    if last_ok > 0 and time.time() - last_ok <= _rest_ready_max_age_sec():
        return GateDecision(blocked=False)
    return GateDecision(
        blocked=True,
        reason='rest_probe_stale',
        detail='no fresh probe on file — use dashboard Re-check or wait for coordinator',
    )


def _tranche_rest_readiness(
    state: Dict[str, Any],
    *,
    strategy: str,
    tranche_id: str,
) -> GateDecision:
    probes = state.get('probes_by_tranche') or {}
    rec = probes.get(tranche_id)
    if not isinstance(rec, dict):
        return GateDecision(
            blocked=True,
            reason='rest_probe_missing',
            detail=f'no pre_tranche probe record for {tranche_id}',
        )
    phase = str(rec.get('status_phase') or '')
    performed = bool(rec.get('performed'))
    if phase in ('scheduled', 'running') or (not performed and phase not in ('completed', 'timed_out')):
        return GateDecision(
            blocked=True,
            reason='rest_probe_pending',
            detail=f'pre_tranche probe for {tranche_id} still {phase or "pending"}',
        )
    if phase == 'timed_out' or (performed and rec.get('status') == 'timed_out'):
        return GateDecision(
            blocked=True,
            reason='rest_timed_out',
            detail=str(rec.get('detail') or f'pre_tranche probe timed out for {tranche_id}'),
        )
    if performed and rec.get('ok') is True:
        return GateDecision(blocked=False)
    if performed and rec.get('ok') is False:
        return GateDecision(
            blocked=True,
            reason=f"rest_{rec.get('status') or 'failed'}",
            detail=str(rec.get('detail') or f'pre_tranche probe failed for {tranche_id}'),
        )
    return GateDecision(
        blocked=True,
        reason='rest_probe_missing',
        detail=f'incomplete pre_tranche probe for {tranche_id}',
    )


def _probe_record_from_result(result) -> Dict[str, Any]:
    return {
        'source': getattr(result, 'source', '') or '',
        'strategy': getattr(result, 'strategy', '') or '',
        'tranche_id': getattr(result, 'tranche_id', '') or '',
        'session_date_ct': getattr(result, 'session_date_ct', '') or '',
        'performed': bool(getattr(result, 'performed', True)),
        'status_phase': getattr(result, 'status_phase', None) or 'completed',
        'ok': bool(result.ok),
        'status': result.status,
        'detail': result.detail,
        'attempted_at_epoch': result.attempted_at_epoch,
        'completed_at_epoch': result.completed_at_epoch,
        'latency_ms': result.latency_ms,
        'http_status': result.http_status,
        'operation': getattr(result, 'operation', 'rest_health_probe_orders'),
    }


def mark_probe_scheduled(
    *,
    source: str,
    session_date_ct: str,
    strategy: str = '',
    tranche_id: str = '',
) -> Dict[str, Any]:
    def _apply(state: Dict[str, Any]) -> None:
        rec = {
            'source': source,
            'strategy': strategy,
            'tranche_id': tranche_id,
            'session_date_ct': session_date_ct,
            'performed': False,
            'status_phase': 'scheduled',
            'ok': None,
            'status': 'pending',
            'detail': '',
            'attempted_at_epoch': None,
            'completed_at_epoch': None,
        }
        if source == 'startup' or not tranche_id:
            state['startup_probe'] = rec
        else:
            probes = dict(state.get('probes_by_tranche') or {})
            probes[tranche_id] = rec
            state['probes_by_tranche'] = probes

    return _mutate(_apply)


def mark_probe_running(
    *,
    source: str,
    session_date_ct: str,
    strategy: str = '',
    tranche_id: str = '',
) -> Dict[str, Any]:
    now = time.time()

    def _apply(state: Dict[str, Any]) -> None:
        if source == 'startup' or not tranche_id:
            rec = dict(state.get('startup_probe') or {})
            rec.update({
                'source': source,
                'session_date_ct': session_date_ct,
                'performed': False,
                'status_phase': 'running',
                'attempted_at_epoch': now,
            })
            state['startup_probe'] = rec
        else:
            probes = dict(state.get('probes_by_tranche') or {})
            rec = dict(probes.get(tranche_id) or {})
            rec.update({
                'source': source,
                'strategy': strategy,
                'tranche_id': tranche_id,
                'session_date_ct': session_date_ct,
                'performed': False,
                'status_phase': 'running',
                'attempted_at_epoch': now,
            })
            probes[tranche_id] = rec
            state['probes_by_tranche'] = probes

    return _mutate(_apply)


def record_probe_result(result) -> Dict[str, Any]:
    """Apply RestProbeResult — successful probe does not clear latch."""

    def _apply(state: Dict[str, Any]) -> None:
        state['rest_status'] = result.status
        state['rest_detail'] = result.detail
        state['rest_source'] = getattr(result, 'source', '') or state.get('rest_source', '')
        state['rest_observed_at_epoch'] = result.completed_at_epoch
        rec = _probe_record_from_result(result)
        state['last_probe'] = {
            'attempted_at_epoch': result.attempted_at_epoch,
            'completed_at_epoch': result.completed_at_epoch,
            'ok': result.ok,
            'status': result.status,
            'latency_ms': result.latency_ms,
            'http_status': result.http_status,
            'operation': getattr(result, 'operation', 'rest_health_probe_orders'),
            'source': rec['source'],
            'tranche_id': rec['tranche_id'],
            'strategy': rec['strategy'],
            'performed': True,
            'status_phase': rec.get('status_phase') or 'completed',
        }
        source = rec['source']
        tranche_id = rec['tranche_id']
        if source == 'startup' or not tranche_id:
            state['startup_probe'] = rec
        else:
            probes = dict(state.get('probes_by_tranche') or {})
            probes[tranche_id] = rec
            state['probes_by_tranche'] = probes
        if result.ok:
            state['last_successful_probe_epoch'] = result.completed_at_epoch
        elif result.status in REST_STATUSES_BLOCKING:
            state['new_risk_latched'] = True
            state['latch_reason'] = f'rest_{result.status}'
            state['latch_source'] = getattr(result, 'source', 'rest_probe')
            state['latched_at_epoch'] = result.completed_at_epoch

    return _mutate(_apply)


def latch_new_risk(
    reason: str,
    *,
    source: str = '',
    detail: str = '',
    rest_status: Optional[str] = None,
) -> Dict[str, Any]:
    def _apply(state: Dict[str, Any]) -> None:
        state['new_risk_latched'] = True
        state['latch_reason'] = reason
        state['latch_source'] = source
        state['latched_at_epoch'] = time.time()
        if detail:
            state['rest_detail'] = detail
        if rest_status:
            state['rest_status'] = rest_status
            state['rest_observed_at_epoch'] = time.time()

    return _mutate(_apply)


def record_cooldown_latch(reason: str, *, source: str = 'broker') -> None:
    """Best-effort hook from set_cooldown — never raises."""
    try:
        status = 'rate_limited'
        low = reason.lower()
        if '401' in low or 'unauthorized' in low or '403' in low or 'forbidden' in low:
            status = 'auth_failed'
        elif 'timeout' in low or 'timed out' in low:
            status = 'unavailable'
        latch_new_risk(
            'broker_cooldown_set',
            source=source,
            detail=reason,
            rest_status=status,
        )
    except Exception:
        log.exception('trading_gate record_cooldown_latch failed')


def resume_new_risk(cleared_by: str = 'operator') -> GateDecision:
    if effective_new_risk_blocked() and cooldown_active():
        return GateDecision(blocked=True, reason='broker_cooldown_active', detail='cooldown still active')
    state = read_state()
    if state.get('rest_status') != REST_HEALTHY:
        return GateDecision(
            blocked=True,
            reason=f'rest_{state.get("rest_status")}',
            detail=str(state.get('rest_detail') or ''),
        )
    last_ok = float(state.get('last_successful_probe_epoch') or 0)
    if time.time() - last_ok > _rest_ready_max_age_sec():
        return GateDecision(blocked=True, reason='stale_probe', detail='re-check REST before resume')
    if has_unresolved_visibility_unknown():
        return GateDecision(
            blocked=True,
            reason='visibility_unknown_active',
            detail='reconcile cooldown_blind entries before resume',
        )

    def _apply(s: Dict[str, Any]) -> None:
        s['new_risk_latched'] = False
        s['latch_reason'] = ''
        s['latch_source'] = ''
        s['cleared_at_epoch'] = time.time()
        s['cleared_by'] = cleared_by
        s['last_resume_epoch'] = time.time()

    _mutate(_apply)
    log.warning('New-risk latch cleared by %s', cleared_by)
    return GateDecision(blocked=False)


def has_unresolved_visibility_unknown(root: Optional[str] = None) -> bool:
    root = root or ROOT
    from common import trades_layout

    for strategy in (trades_layout.STRATEGY_MEIC, trades_layout.STRATEGY_MANUAL):
        base = trades_layout.active_dir_for_strategy(strategy, root)
        if not os.path.isdir(base):
            continue
        for fname in os.listdir(base):
            if not fname.endswith('.json'):
                continue
            path = os.path.join(base, fname)
            try:
                state = load_json_safe(path) or {}
            except Exception:
                continue
            if state.get('entry_control') == 'cooldown_blind':
                return True
            oo = state.get('open_order') if isinstance(state.get('open_order'), dict) else {}
            if (oo.get('status') or '').lower() == 'visibility_unknown':
                return True
    return False


def summary_for_dashboard() -> Dict[str, Any]:
    state = read_state()
    snap = cooldown_snapshot()
    blocked = effective_new_risk_blocked()
    rest = state.get('rest_status') or 'unknown'
    latched = bool(state.get('new_risk_latched'))
    fresh_probe = (
        float(state.get('last_successful_probe_epoch') or 0) > 0
        and time.time() - float(state.get('last_successful_probe_epoch') or 0) <= _rest_ready_max_age_sec()
    )
    resume_allowed = (
        latched
        and not cooldown_active()
        and rest == REST_HEALTHY
        and fresh_probe
        and not has_unresolved_visibility_unknown()
    )

    last_probe = state.get('last_probe') or {}
    return {
        'rest_status': rest,
        'rest_healthy': rest == REST_HEALTHY,
        'cooldown_active': snap.get('active', False),
        'cooldown_until_epoch': snap.get('until'),
        'cooldown_remaining_sec': snap.get('remaining_sec'),
        'new_risk_latched': bool(state.get('new_risk_latched')),
        'new_risk_blocked': blocked,
        'reason': state.get('latch_reason') or '',
        'detail': state.get('rest_detail') or '',
        'last_probe': {
            'at_epoch': last_probe.get('completed_at_epoch'),
            'ok': last_probe.get('ok'),
            'latency_ms': last_probe.get('latency_ms'),
            'http_status': last_probe.get('http_status'),
            'status': last_probe.get('status'),
        },
        'resume_allowed': resume_allowed,
        'session_date_ct': state.get('session_date_ct'),
        'last_successful_probe_epoch': state.get('last_successful_probe_epoch'),
    }
