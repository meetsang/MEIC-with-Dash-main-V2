"""Terminal-quiet logging — full detail in session log files."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


class TerminalAlertFilter(logging.Filter):
    """Terminal shows WARNING+ and INFO marked with extra terminal=True."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        return bool(getattr(record, 'terminal', False))


def _formatter(prefix: str) -> logging.Formatter:
    tag = prefix.upper() if prefix else 'APP'
    return logging.Formatter(
        f'%(asctime)s [{tag}] %(message)s',
        datefmt='%H:%M:%S',
    )


def _clear_root_handlers() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)


def setup_session_logging(
    logger_name: str,
    log_path: str | Path,
    *,
    stream_prefix: str = '',
    file_mode: str = 'a',
) -> logging.Logger:
    """File: INFO+. Terminal: WARNING+ and explicit terminal INFO only."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear_root_handlers()

    fmt = _formatter(stream_prefix or logger_name)
    file_handler = logging.FileHandler(path, mode=file_mode, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(TerminalAlertFilter())

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return logging.getLogger(logger_name)


def setup_file_only_logging(
    logger_name: str,
    log_path: str | Path,
    *,
    stream_prefix: str = '',
    file_mode: str = 'w',
) -> logging.Logger:
    """Microservices — no terminal output; operators use log files + launcher alerts."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear_root_handlers()

    handler = logging.FileHandler(path, mode=file_mode, encoding='utf-8')
    handler.setLevel(logging.INFO)
    handler.setFormatter(_formatter(stream_prefix or logger_name))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return logging.getLogger(logger_name)


def terminal_info(logger: logging.Logger, msg: str, *args) -> None:
    """INFO that also appears on the operator terminal."""
    logger.info(msg, *args, extra={'terminal': True})


def silence_noisy_loggers(*names: str, level: int = logging.WARNING) -> None:
    for name in names:
        logging.getLogger(name).setLevel(level)
