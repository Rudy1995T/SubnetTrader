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
    """Appends structured JSON lines to a date-stamped file.

    Keeps a long-lived file handle, reopened on date rollover. Caps individual
    file size at MAX_BYTES; on overflow the active file is rotated to a numbered
    suffix and a fresh file is opened.
    """

    MAX_BYTES = 10 * 1024 * 1024  # 10 MB

    def __init__(self, directory: str):
        super().__init__()
        self._dir = directory
        _ensure_dir(self._dir)
        self._current_date: str | None = None
        self._current_path: str | None = None
        self._fh = None

    def _file_path_for_today(self) -> tuple[str, str]:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return date_str, os.path.join(self._dir, f"{date_str}.jsonl")

    def _open(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        date_str, path = self._file_path_for_today()
        self._current_date = date_str
        self._current_path = path
        self._fh = open(path, "a", buffering=1)  # line-buffered

    def _rotate_if_oversized(self) -> None:
        if not self._current_path:
            return
        try:
            size = os.path.getsize(self._current_path)
        except OSError:
            return
        if size < self.MAX_BYTES:
            return
        # Close current handle, find next free .N suffix, rename, reopen.
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        n = 1
        while True:
            candidate = f"{self._current_path}.{n}"
            if not os.path.exists(candidate):
                break
            n += 1
        try:
            os.rename(self._current_path, candidate)
        except OSError:
            pass
        self._fh = open(self._current_path, "a", buffering=1)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if hasattr(record, "structured"):
                entry["data"] = record.structured  # type: ignore[attr-defined]

            date_str, _ = self._file_path_for_today()
            if self._fh is None or date_str != self._current_date:
                self._open()
            self._rotate_if_oversized()
            assert self._fh is not None
            self._fh.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        super().close()


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
