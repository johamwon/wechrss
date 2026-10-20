"""Logging configuration for WechRss.

Provides structured logging with configurable level and output.
Set environment variables to control behavior:
- LOG_LEVEL: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)
- LOG_FILE: Path to log file (optional, defaults to stderr only)
- LOG_FORMAT: 'text' for human-readable, 'json' for structured (default: text)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def setup_logging() -> logging.Logger:
    """Configure and return the root application logger."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = os.getenv("LOG_FORMAT", "text").lower()
    log_file = os.getenv("LOG_FILE", "").strip()

    root_logger = logging.getLogger("wechrss")
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    if log_format == "json":
        formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Console handler (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return root_logger


class JsonFormatter(logging.Formatter):
    """Simple JSON log formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        log_entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, ensure_ascii=False)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the wechrss namespace."""
    return logging.getLogger(f"wechrss.{name}")
