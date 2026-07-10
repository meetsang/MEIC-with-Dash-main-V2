"""Entry scan MQTT fallback configuration (Phase 5)."""
from __future__ import annotations

import os

ENTRY_MQTT_FALLBACK_ENABLED = os.environ.get(
    'ENTRY_MQTT_FALLBACK_ENABLED', 'true',
).lower() in ('1', 'true', 'yes')
ENTRY_REST_MIN_COVERAGE_PCT = float(os.environ.get('ENTRY_REST_MIN_COVERAGE_PCT', '50'))
ENTRY_MQTT_READY_TIMEOUT_SEC = float(os.environ.get('ENTRY_MQTT_READY_TIMEOUT_SEC', '5'))
MAX_MQTT_ENTRY_QUOTE_AGE_SEC = float(os.environ.get('MAX_MQTT_ENTRY_QUOTE_AGE_SEC', '10'))
MAX_MQTT_ENTRY_PAIR_SKEW_SEC = float(os.environ.get('MAX_MQTT_ENTRY_PAIR_SKEW_SEC', '2'))
MQTT_REQUIRE_POST_SCAN_QUOTE = os.environ.get(
    'MQTT_REQUIRE_POST_SCAN_QUOTE', 'true',
).lower() in ('1', 'true', 'yes')
MQTT_ENTRY_SPREAD_WIDTH_TOLERANCE = float(
    os.environ.get('MQTT_SPREAD_WIDTH_TOLERANCE', '0.05'),
)
