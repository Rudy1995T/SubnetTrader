"""
Structured logging: Python stdlib logger + JSONL file appender.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


def _ensure_dir(path: str) -> None:
    Path(path).mkdir(parents=True, exist_ok=True)


class JsonlFileHandler(logging.Handler):
    """Appends structured JSON lines to a date-stamped file."""

    def __init__(self, directory: str):
        super().__init__()
        self._dir = directory
        _ensure_dir(self._dir)

    def _file_path(self) -> str:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return os.path.join(self._dir, f"{date_str}.jsonl")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            # Attach extra structured fields if provided
            if hasattr(record, "structured"):
                entry["data"] = record.structured  # type: ignore[attr-defined]

            with open(self._file_path(), "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            self.handleError(record)


class StructuredLogger:
    """Thin wrapper giving structured log calls with optional data payload."""

    def __init__(self, name: str = "subnet_trader"):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

        # Console handler
        if not self._logger.handlers:
            console = logging.StreamHandler(sys.stdout)
            console.setLevel(logging.DEBUG)
            fmt = logging.Formatter(
                "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            console.setFormatter(fmt)
            self._logger.addHandler(console)

            # JSONL handler
            jsonl_handler = JsonlFileHandler(settings.JSONL_DIR)
            jsonl_handler.setLevel(logging.DEBUG)
            self._logger.addHandler(jsonl_handler)

    def _log(self, level: int, msg: str, data: dict | None = None) -> None:
        record = self._logger.makeRecord(
            self._logger.name, level, "(structured)", 0, msg, (), None
        )
        if data is not None:
            record.structured = data  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, msg: str, data: dict | None = None) -> None:
        self._log(logging.DEBUG, msg, data)

    def info(self, msg: str, data: dict | None = None) -> None:
        self._log(logging.INFO, msg, data)

    def warning(self, msg: str, data: dict | None = None) -> None:
        self._log(logging.WARNING, msg, data)

    def error(self, msg: str, data: dict | None = None) -> None:
        self._log(logging.ERROR, msg, data)

    def critical(self, msg: str, data: dict | None = None) -> None:
        self._log(logging.CRITICAL, msg, data)


# Module-level singleton
logger = StructuredLogger()
