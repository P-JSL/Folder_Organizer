"""Bounded, local-only diagnostic logs. Logging failures never stop file work.

Callers log operation metadata, never document contents, credentials or complete
plan/journal objects. Paths and error messages can appear in these local logs;
the viewer makes that clear before a user chooses to export them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import faulthandler
from itertools import islice
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import sys
import threading
import traceback
from typing import Any
import uuid

LOG_NAME = "organizer.log"
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
_lock = threading.RLock()
_logger: logging.Logger | None = None
_directory: Path | None = None
_configured = False
_last_error: str | None = None
_crash_stream = None
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|token|authorization|api[_-]?key|credential)", re.I)
_SECRET_TEXT = re.compile(r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _text(value: Any, limit: int = 2048) -> str:
    text = str(value)
    text = _BEARER.sub("Bearer [가림]", text)
    text = _SECRET_TEXT.sub(r"\1[가림]", text)
    text = text.replace("\x00", "�")
    return text[:limit] + ("…" if len(text) > limit else "")


def _clean(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "[세부 항목 생략]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, dict):
        return {_text(key, 100): "[가림]" if _SECRET_KEY.search(str(key)) else _clean(item, depth + 1)
                for key, item in islice(value.items(), 32)}
    if isinstance(value, (list, tuple)):
        return [_clean(item, depth + 1) for item in value[:32]]
    return _text(value)


def log_directory() -> Path:
    """Return the configured local log directory, even when writing failed."""
    if _directory is not None:
        return _directory
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "FolderOrganizer" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "FolderOrganizer"
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "FolderOrganizer" / "logs"


def _reject_links(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise OSError("로그 경로에 심볼릭 링크가 있어 사용하지 않습니다.")
        if component.exists() and getattr(component.lstat(), "st_file_attributes", 0) & 0x400:
            raise OSError("로그 경로에 재분석 지점이 있어 사용하지 않습니다.")


class _NonfatalHandler(RotatingFileHandler):
    def handleError(self, record):
        global _last_error
        _last_error = "로그를 저장하지 못했습니다. 로그 폴더의 쓰기 권한과 저장 공간을 확인하세요."


def configure_logging(directory: str | Path | None = None, *, max_bytes: int = MAX_LOG_BYTES,
                      backup_count: int = BACKUP_COUNT) -> Path | None:
    """Configure a thread-safe UTF-8 rotating log; return None on write failure."""
    global _logger, _directory, _configured, _last_error
    with _lock:
        if _logger is not None:
            for handler in list(_logger.handlers):
                _logger.removeHandler(handler)
                handler.close()
        _logger = None
        _directory = Path(directory).absolute() if directory is not None else log_directory()
        _configured, _last_error = True, None
        try:
            if type(max_bytes) is not int or max_bytes < 1024 or type(backup_count) is not int or not 1 <= backup_count <= 10:
                raise ValueError("로그 보관 크기와 개수가 올바르지 않습니다.")
            _reject_links(_directory)
            _directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _reject_links(_directory / LOG_NAME)
            handler = _NonfatalHandler(_directory / LOG_NAME, maxBytes=max_bytes,
                                       backupCount=backup_count, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger = logging.getLogger("FolderOrganizer.local")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            logger.addHandler(handler)
            _logger = logger
            return _directory
        except (OSError, ValueError):
            _last_error = "로그를 저장하지 못했습니다. 로그 폴더의 쓰기 권한과 저장 공간을 확인하세요."
            return None


def logging_error() -> str | None:
    return _last_error


def shutdown_logging() -> None:
    """Flush and close owned handles, useful at application/test shutdown."""
    global _logger, _configured, _directory
    with _lock:
        if _logger is not None:
            for handler in list(_logger.handlers):
                _logger.removeHandler(handler)
                handler.close()
        _logger, _directory, _configured = None, None, False


def log_event(event: str, message: str = "", *, level: str = "INFO",
              operation_id: str | None = None, details: dict | None = None) -> None:
    """Write metadata without letting a logging failure interrupt an operation."""
    try:
        if not _configured:
            configure_logging()
        with _lock:
            if _logger is None:
                return
            level = str(level).upper()
            if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                level = "INFO"
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "level": level, "operation_id": _text(operation_id, 128) if operation_id else None,
                "event": _text(event, 128), "message": _text(message), "details": _clean(details or {}),
            }
            encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
                record["details"] = {"truncated": True}
                encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            _logger.log(getattr(logging, level), encoded)
    except Exception:
        # The operation itself must remain usable if logging is unavailable.
        return


def _log_paths() -> list[Path]:
    directory = log_directory()
    _reject_links(directory)
    if not directory.exists():
        return []
    return [directory / LOG_NAME, *[directory / f"{LOG_NAME}.{number}" for number in range(1, 11)]]


def read_logs(*, limit: int = 1000, level: str | None = None, query: str = "") -> list[dict]:
    """Read newest-first, bounded local records; ignore damaged/oversize lines."""
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("표시할 로그 수는 1~10,000이어야 합니다.")
    records, remaining = [], MAX_READ_BYTES
    query = str(query).casefold()[:512]
    selected_level = str(level).upper() if level else None
    for path in _log_paths():
        if remaining <= 0:
            break
        try:
            _reject_links(path)
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                amount = min(size, remaining)
                stream.seek(size - amount)
                content = stream.read(amount)
            remaining -= len(content)
            lines = content.splitlines()
            if amount < size and lines:
                lines = lines[1:]
            for line in reversed(lines):
                if len(line) > MAX_RECORD_BYTES:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(record, dict) or not isinstance(record.get("timestamp"), str) or not isinstance(record.get("message"), str):
                    continue
                # Treat hand-edited logs like any other local input. Keep
                # nesting and field lengths bounded before searching/display.
                record = _clean(record)
                if selected_level and record.get("level") != selected_level:
                    continue
                if query and query not in json.dumps(record, ensure_ascii=False).casefold():
                    continue
                records.append(record)
                if len(records) >= limit:
                    return records
        except FileNotFoundError:
            continue
        except OSError:
            continue
    return records


def export_logs(path: str | Path, records: list[dict] | None = None) -> Path:
    """Export selected metadata to a new JSONL file without overwriting files."""
    destination = Path(path).absolute()
    _reject_links(destination)
    selected = read_logs() if records is None else records
    if not isinstance(selected, list) or len(selected) > 10_000:
        raise ValueError("로그 내보내기는 최대 10,000개 항목을 지원합니다.")
    lines, size = [], 0
    for record in selected:
        encoded = (json.dumps(_clean(record), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_RECORD_BYTES:
            raise ValueError("내보낼 로그 항목의 크기가 제한을 초과했습니다.")
        size += len(encoded)
        if size > MAX_READ_BYTES:
            raise ValueError("로그 내보내기 크기는 최대 8 MB입니다. 검색 조건을 좁혀주세요.")
        lines.append(encoded)
    with destination.open("xb") as stream:
        for encoded in lines:
            stream.write(encoded)
    return destination


def enable_crash_logging() -> Path | None:
    """Retain native crash stacks when supported, without replacing other hooks.

    A native stack overflow cannot be caught by a Python exception hook. Each
    app launch gets one crash file, with at most three files retained. Existing
    faulthandler installations (including test runners) remain untouched.
    """
    global _crash_stream
    try:
        if faulthandler.is_enabled():
            return None
        directory = log_directory()
        _reject_links(directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        previous = sorted(directory.glob("crash-*.log"), key=lambda path: path.lstat().st_mtime, reverse=True)
        for path in previous[2:]:
            if not path.is_symlink() and path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        path = directory / f"crash-{os.getpid()}-{uuid.uuid4().hex[:12]}.log"
        stream = path.open("xb")
        try:
            faulthandler.enable(file=stream, all_threads=True)
        except Exception:
            stream.close()
            path.unlink(missing_ok=True)
            raise
        _crash_stream = stream
        return path
    except (OSError, RuntimeError, ValueError):
        return None


def install_exception_hook():
    """Log unhandled exceptions and preserve Python's existing error behavior."""
    previous_sys, previous_thread = sys.excepthook, threading.excepthook
    enable_crash_logging()

    def capture(exception_type, value, tb):
        frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                  for frame in traceback.extract_tb(tb)[-20:]]
        log_event("unhandled_exception", _text(value), level="ERROR",
                  details={"exception_type": exception_type.__name__, "frames": frames})

    def system_hook(exception_type, value, tb):
        capture(exception_type, value, tb)
        previous_sys(exception_type, value, tb)

    def thread_hook(arguments):
        capture(arguments.exc_type, arguments.exc_value, arguments.exc_traceback)
        previous_thread(arguments)

    sys.excepthook, threading.excepthook = system_hook, thread_hook

    def restore():
        if sys.excepthook is system_hook:
            sys.excepthook = previous_sys
        if threading.excepthook is thread_hook:
            threading.excepthook = previous_thread

    return restore
