"""
Operator Notifications & Daily Morning Readiness Check
======================================================
Supports zero-cost Email-to-SMS carrier gateways (via SMTP), Twilio SMS REST API,
and Slack Incoming Webhooks for the 8:20 AM CST daily token/health check and
critical runtime alerts.

Configuration (via `.env` or environment variables):
  1. Email-to-SMS Carrier Gateway (Free, zero third-party cost):
     - SMTP_HOST         (default: smtp.gmail.com)
     - SMTP_PORT         (default: 587)
     - SMTP_USER         (e.g., your.email@gmail.com)
     - SMTP_APP_PASSWORD (16-char Google App Password)
     - ALERT_SMS_TO      (comma-separated carrier gateways, e.g.:
                          1234567890@vtext.com        [Verizon]
                          1234567890@txt.att.net      [AT&T]
                          1234567890@tmomail.net      [T-Mobile]
                          1234567890@msg.fi.google.com [Google Fi])

  2. Twilio SMS REST API (Optional):
     - TWILIO_ACCOUNT_SID
     - TWILIO_AUTH_TOKEN
     - TWILIO_FROM_NUMBER (e.g., +15551234567)
     - TWILIO_TO_NUMBER   (comma-separated, e.g., +15559876543)

  3. Slack Incoming Webhook (Optional):
     - SLACK_WEBHOOK_URL  (or configured via common.auth.config)
"""

from __future__ import annotations

import datetime
from email.message import EmailMessage
import logging
import os
import smtplib
from typing import Dict, Optional

import requests

from zoneinfo import ZoneInfo
from common.config import check_trading_day

CENTRAL_TZ = ZoneInfo('America/Chicago')
from common.market_hours import is_early_close_day_ct, market_close_time_ct

log = logging.getLogger('notifications')


def _send_smtp_sms(title: str, message: str, logger: Optional[logging.Logger] = None) -> bool:
    """Send a compact text message via SMTP to carrier Email-to-SMS gateways."""
    _log = logger or log
    sms_to_raw = os.environ.get('ALERT_SMS_TO', '').strip()
    smtp_user = os.environ.get('SMTP_USER', '').strip()
    smtp_pass = os.environ.get('SMTP_APP_PASSWORD', '').strip()
    if not sms_to_raw or not smtp_user or not smtp_pass:
        return False

    recipients = [r.strip() for r in sms_to_raw.split(',') if r.strip()]
    if not recipients:
        return False

    smtp_host = os.environ.get('SMTP_HOST', 'smtp.gmail.com').strip() or 'smtp.gmail.com'
    try:
        smtp_port = int(os.environ.get('SMTP_PORT', '587').strip())
    except ValueError:
        smtp_port = 587

    msg = EmailMessage()
    msg['Subject'] = title[:48]
    msg['From'] = smtp_user
    msg['To'] = ', '.join(recipients)
    # Carrier SMS gateways truncate around 160 chars per segment; keep body clean
    msg.set_content(message[:300])

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        _log.info('SMS alert sent via SMTP gateway to %d recipient(s).', len(recipients))
        return True
    except Exception as exc:
        _log.warning('Failed to send SMTP SMS alert: %s', exc)
        return False


def _send_twilio_sms(title: str, message: str, logger: Optional[logging.Logger] = None) -> bool:
    """Send SMS via Twilio REST API using standard HTTP Basic Auth (no SDK required)."""
    _log = logger or log
    account_sid = os.environ.get('TWILIO_ACCOUNT_SID', '').strip()
    auth_token = os.environ.get('TWILIO_AUTH_TOKEN', '').strip()
    from_number = os.environ.get('TWILIO_FROM_NUMBER', '').strip()
    to_numbers_raw = os.environ.get('TWILIO_TO_NUMBER', '').strip()

    if not (account_sid and auth_token and from_number and to_numbers_raw):
        return False

    to_numbers = [n.strip() for n in to_numbers_raw.split(',') if n.strip()]
    if not to_numbers:
        return False

    url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    body_text = f'{title}: {message}' if title else message
    sent_any = False

    for to_num in to_numbers:
        try:
            resp = requests.post(
                url,
                data={'From': from_number, 'To': to_num, 'Body': body_text[:320]},
                auth=(account_sid, auth_token),
                timeout=10,
            )
            if 200 <= resp.status_code < 300:
                sent_any = True
            else:
                _log.warning('Twilio SMS to %s returned HTTP %s: %s', to_num, resp.status_code, resp.text[:160])
        except Exception as exc:
            _log.warning('Failed to send Twilio SMS to %s: %s', to_num, exc)

    if sent_any:
        _log.info('SMS alert sent via Twilio to %d recipient(s).', len(to_numbers))
    return sent_any


def _send_slack_webhook(title: str, message: str, logger: Optional[logging.Logger] = None) -> bool:
    """Send message via Slack Incoming Webhook if configured."""
    _log = logger or log
    webhook_url = os.environ.get('SLACK_WEBHOOK_URL', '').strip()
    if not webhook_url:
        try:
            from common.auth import config as auth_cfg
            webhook_url = getattr(auth_cfg, 'SLACK_WEBHOOK_URL', '').strip()
        except Exception:
            webhook_url = ''
    if not webhook_url:
        return False

    text = f'*{title}*\n{message}' if title else message
    try:
        resp = requests.post(webhook_url, json={'text': text}, timeout=10)
        if 200 <= resp.status_code < 300:
            _log.info('Slack alert sent successfully.')
            return True
        _log.warning('Slack webhook returned HTTP %s: %s', resp.status_code, resp.text[:160])
    except Exception as exc:
        _log.warning('Failed to send Slack alert: %s', exc)
    return False


def send_operator_alert(
    title: str,
    message: str,
    *,
    level: str = 'info',
    logger: Optional[logging.Logger] = None,
) -> Dict[str, bool]:
    """
    Dispatch an operator alert across all configured channels (SMTP Email-to-SMS,
    Twilio SMS, and Slack webhook). Never raises; returns per-channel status dict.
    """
    _log = logger or log
    results = {
        'smtp_sms': _send_smtp_sms(title, message, logger=_log),
        'twilio_sms': _send_twilio_sms(title, message, logger=_log),
        'slack': _send_slack_webhook(title, message, logger=_log),
    }
    if not any(results.values()):
        _log.info('[Alert:%s] %s — %s (no external SMS/Slack channel configured)', level.upper(), title, message)
    return results


def run_morning_readiness_check(
    root: str,
    logger: Optional[logging.Logger] = None,
    *,
    session_label: str = '08:20 CST Morning Check',
) -> dict:
    """
    Execute the 8:20 AM CST (or Sunday 4:30 PM CST) readiness check:
      1. Force-refresh and validate the TastyTrade OAuth session token.
      2. Check NYSE trading day status and half-day (early close) schedule.
      3. Send an operator SMS/Slack summary alert so the operator knows before
         market open whether the bot is healthy or needs intervention.
    """
    _log = logger or log
    now_ct = datetime.datetime.now(CENTRAL_TZ)
    today = now_ct.date()

    from common.tt_auth import verify_tastytrade_oauth
    oauth_ok, oauth_msg = verify_tastytrade_oauth()

    should_trade, day_reason = check_trading_day(now_ct)
    early_close = is_early_close_day_ct(today)
    close_t = market_close_time_ct(today)
    close_str = close_t.strftime('%H:%M')

    status_tag = 'OK' if oauth_ok else 'AUTH FAIL'
    sched_str = (
        f'HALF-DAY (close {close_str} CT)'
        if early_close
        else (f'OPEN (close {close_str} CT)' if should_trade else f'CLOSED ({day_reason})')
    )

    title = f'MEIC {status_tag} ({now_ct.strftime("%m/%d %H:%M")})'
    body = f'{session_label}: TT OAuth={"OK" if oauth_ok else "FAIL"} ({oauth_msg}). Market: {sched_str}.'

    _log.info('Morning readiness check: %s', body)
    alert_results = send_operator_alert(
        title,
        body,
        level='info' if oauth_ok else 'error',
        logger=_log,
    )

    return {
        'timestamp': now_ct.isoformat(),
        'oauth_ok': oauth_ok,
        'oauth_message': oauth_msg,
        'should_trade': should_trade,
        'day_reason': day_reason,
        'is_early_close_day': early_close,
        'market_close_ct': close_str,
        'alerts_sent': alert_results,
    }
