"""Tests for quiet terminal logging helpers."""
from __future__ import annotations

import logging

from common.logging_config import TerminalAlertFilter, terminal_info


def test_terminal_filter_blocks_routine_info():
    filt = TerminalAlertFilter()
    record = logging.LogRecord('x', logging.INFO, '', 0, 'poll', (), None)
    assert filt.filter(record) is False


def test_terminal_filter_passes_warning():
    filt = TerminalAlertFilter()
    record = logging.LogRecord('x', logging.WARNING, '', 0, 'stale', (), None)
    assert filt.filter(record) is True


def test_terminal_filter_passes_flagged_info():
    filt = TerminalAlertFilter()
    record = logging.LogRecord('x', logging.INFO, '', 0, 'holiday', (), None)
    record.terminal = True
    assert filt.filter(record) is True


def test_terminal_info_sets_extra():
    logger = logging.getLogger('test.terminal_info')
    logger.handlers.clear()
    captured = []

    class _Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = _Capture()
    handler.addFilter(TerminalAlertFilter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    terminal_info(logger, 'NYSE closed')
    assert len(captured) == 1
    assert captured[0].getMessage() == 'NYSE closed'
