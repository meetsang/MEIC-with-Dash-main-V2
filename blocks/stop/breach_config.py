"""Software-breach readiness and confirmation configuration."""
from __future__ import annotations

import os

BREACH_FILL_GRACE_SEC = float(os.environ.get('BREACH_FILL_GRACE_SEC', '10'))
MAX_MQTT_BREACH_QUOTE_AGE_SEC = float(os.environ.get('MAX_MQTT_BREACH_QUOTE_AGE_SEC', '5'))
MAX_MQTT_PAIR_SKEW_SEC = float(os.environ.get('MAX_MQTT_PAIR_SKEW_SEC', '2'))
MQTT_SPREAD_WIDTH_TOLERANCE = float(os.environ.get('MQTT_SPREAD_WIDTH_TOLERANCE', '0.05'))
BREACH_CONFIRM_OBSERVATIONS = int(os.environ.get('BREACH_CONFIRM_OBSERVATIONS', '2'))
BREACH_CONFIRM_MAX_WINDOW_SEC = float(os.environ.get('BREACH_CONFIRM_MAX_WINDOW_SEC', '3'))
