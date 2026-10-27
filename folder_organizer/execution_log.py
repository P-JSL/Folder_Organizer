"""Bounded, local execution receipts, separate from diagnostics and undo journals.

Only scalar operation metadata is accepted. Document contents and complete plans
never enter this log. A receipt cannot authorize a mutation or a restore.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import threading

from .diagnostics import _reject_links, _text, log_directory

LOG_NAME = "executions.jsonl"
MAX_LOG_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
MAX_READ_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 32 * 1024
_lock = threading.RLock()
_directory: Path | None = None
_configured = False
_last_error: str | None = None
_max_bytes = MAX_LOG_BYTES
_backup_count = BACKUP_COUNT
_FIELDS = ("timestamp", "event", "execution_id", "action", "status", "message",
           "source", "target", "error", "completed", "total")


def execution_log_directory() -> Path:
    return _directory if _directory is not None else log_directory()


def execution_logging_error() -> str | None:
    return _last_error


def configure_execution_logging(directory: str | Path | None = None, *,
                                max_bytes: int = MAX_LOG_BYTES,
                                backup_count: int = BACKUP_COUNT) -> Path | None:
    """Prepare the local directory; failures are visible but never propagated."""
    global _directory, _configured, _last_error, _max_bytes, _backup_count
    with _lock:
        _directory = Path(directory).absolute() if directory is not None else log_directory()
        _configured, _last_error = True, None
        try:
            if type(max_bytes) is not int or max_bytes < 1024 or type(backup_count) is not int or not 1 <= backup_count <= 10:
                raise ValueError("실행 로그 보관 설정이 올바르지 않습니다.")
            _max_bytes, _backup_count = max_bytes, backup_count
            _reject_links(_directory)
            _directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            _reject_links(_directory / LOG_NAME)
            return _directory
        except (OSError, ValueError):
            _last_error = "실행 로그를 저장하지 못했습니다. 로그 폴더의 쓰기 권한과 저장 공간을 확인하세요. 파일 작업 결과는 작업 기록에서 확인할 수 있습니다."
            return None


def shutdown_execution_logging() -> None:
    global _directory, _configured, _last_error
    with _lock:
        _directory, _configured, _last_error = None, False, None


def _record(record: dict) -> dict:
    """Flatten untrusted local/exported input; never traverse nested objects."""
    result = {}
    for key in _FIELDS:
        value = record.get(key)
        if key in {"completed", "total"}:
            result[key] = min(value, 10**15) if type(value) is int and value >= 0 else None
        elif value is None:
            result[key] = None if key in {"source", "target", "error"} else ""
        elif isinstance(value, (str, int, float, bool)):
            result[key] = _text(value, 2048 if key in {"message", "source", "target", "error"} else 160)
        else:
            result[key] = "[표시할 수 없는 값]"
    return result


def _append(contents: bytes) -> None:
    directory = execution_log_directory()
    _reject_links(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / LOG_NAME
    paths = [path, *[directory / f"{LOG_NAME}.{i}" for i in range(1, _backup_count + 1)]]
    for candidate in paths:
        _reject_links(candidate)
        if candidate.exists() and not candidate.is_file():
            raise OSError("실행 로그가 일반 파일이 아닙니다.")
    if path.exists() and path.stat().st_size + len(contents) > _max_bytes:
        for index in range(_backup_count, 1, -1):
            previous = directory / f"{LOG_NAME}.{index - 1}"
            if previous.exists():
                os.replace(previous, directory / f"{LOG_NAME}.{index}")
        os.replace(path, directory / f"{LOG_NAME}.1")
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "ab") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("실행 로그가 일반 파일이 아닙니다.")
        stream.write(contents)


def record_execution(event: str, *, execution_id: str, action: str, status: str,
                     message: str = "", source=None, target=None, error=None,
                     completed: int | None = None, total: int | None = None) -> None:
    """Append one nonfatal receipt. No operation depends on successful logging."""
    global _last_error
    try:
        with _lock:
            if not _configured:
                configure_execution_logging()
            record = _record({"timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                              "event": event, "execution_id": execution_id, "action": action,
                              "status": status, "message": message,
                              "source": str(source) if source is not None else None,
                              "target": str(target) if target is not None else None,
                              "error": str(error) if error is not None else None,
                              "completed": completed, "total": total})
            contents = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
            if len(contents) > MAX_RECORD_BYTES:
                raise ValueError("실행 로그 항목이 너무 큽니다.")
            _append(contents)
    except Exception:
        _last_error = "실행 로그를 저장하지 못했습니다. 로그 폴더의 쓰기 권한과 저장 공간을 확인하세요. 파일 작업 결과는 작업 기록에서 확인할 수 있습니다."


def read_execution_logs(limit: int = 1000, status: str | None = None,
                        action: str | None = None, query: str = "") -> list[dict]:
    """Newest first; corrupt lines are skipped within a strict byte budget."""
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("표시할 실행 로그 수는 1~10,000이어야 합니다.")
    remaining, result = MAX_READ_BYTES, []
    query = str(query).casefold()[:512]
    directory = execution_log_directory()
    with _lock:
        for path in [directory / LOG_NAME, *[directory / f"{LOG_NAME}.{i}" for i in range(1, 11)]]:
            if remaining <= 0:
                break
            try:
                _reject_links(path)
                # A hand-edited path may be a FIFO/device. Nonblocking open and
                # descriptor validation keep the read-only viewer responsive.
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    amount = min(info.st_size, remaining)
                    stream.seek(info.st_size - amount)
                    contents = stream.read(amount)
                remaining -= len(contents)
                lines = contents.splitlines()
                if amount < info.st_size and lines:
                    lines = lines[1:]
                for line in reversed(lines):
                    if len(line) > MAX_RECORD_BYTES:
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        continue
                    if not isinstance(value, dict) or not all(isinstance(value.get(field), str) for field in ("timestamp", "event", "execution_id", "action", "status")):
                        continue
                    record = _record(value)
                    if status and record["status"] != status:
                        continue
                    if action and record["action"] != action:
                        continue
                    if query and query not in json.dumps(record, ensure_ascii=False).casefold():
                        continue
                    result.append(record)
                    if len(result) >= limit:
                        return result
            except OSError:
                continue
    return result


def export_execution_logs(path: str | Path, records: list[dict] | None = None) -> Path:
    """Export metadata only, exclusively to a new JSONL file."""
    destination = Path(path).absolute()
    _reject_links(destination)
    selected = read_execution_logs() if records is None else records
    if not isinstance(selected, list) or len(selected) > 10_000:
        raise ValueError("실행 로그 내보내기는 최대 10,000개 항목을 지원합니다.")
    lines, total = [], 0
    for value in selected:
        if not isinstance(value, dict):
            raise ValueError("내보낼 실행 로그 형식이 올바르지 않습니다.")
        encoded = (json.dumps(_record(value), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        total += len(encoded)
        if len(encoded) > MAX_RECORD_BYTES or total > MAX_READ_BYTES:
            raise ValueError("실행 로그 내보내기는 최대 8 MB입니다. 검색 조건을 좁혀주세요.")
        lines.append(encoded)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        for encoded in lines:
            stream.write(encoded)
    return destination
