"""One-pass, read-only storage summaries of an existing file inventory.

The summary never opens or stats a path. Top lists use fixed-size heaps rather
than sorting every file, so the work is O(n log k), O(n) for bounded k <= 100.
Folder aggregation keeps one counter per immediate child folder, not a repeated
list of its paths. A view may filter its retained FileEntry objects on demand.
"""
from __future__ import annotations

import heapq
import math
from pathlib import Path
import time
from typing import Iterable

from .models import FileEntry


TYPE_LABELS = {
    "applications": "응용 프로그램", "documents": "문서·텍스트", "images": "사진·이미지",
    "video": "동영상", "audio": "음악·소리", "archives": "압축 파일", "other": "기타 파일",
}
_SUFFIXES = {
    "documents": frozenset({".txt", ".md", ".csv", ".tsv", ".pdf", ".doc", ".docx", ".xls", ".xlsx",
                             ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".hwp", ".hwpx",
                             ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".ini"}),
    "images": frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".avif", ".svg", ".ico"}),
    "video": frozenset({".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".m4v", ".mpeg", ".mpg"}),
    "audio": frozenset({".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".aiff", ".opus"}),
    "archives": frozenset({".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".zst"}),
}
_CATEGORY_BY_SUFFIX = {}
for _category, _extensions in _SUFFIXES.items():
    for _extension in _extensions:
        _CATEGORY_BY_SUFFIX.setdefault(_extension, _category)
FOLDER_LIMIT = 20
DATE_LABELS = {
    "recent_30": "최근 30일 수정", "year": "31일~1년 전 수정", "older": "1년 이상 수정 없음",
    "future": "미래의 수정 날짜", "unknown": "날짜 확인 불가",
}


def inventory_category(entry: FileEntry) -> str:
    """Return a fixed category key; this is a display aid, not file inspection."""
    if entry.kind == "application":
        return "applications"
    return _CATEGORY_BY_SUFFIX.get(entry.suffix.casefold(), "other")


def inventory_folder(entry: FileEntry, root: str | Path) -> str:
    """Return the immediate inventory subfolder, or '.' for a direct child."""
    # Use the recorded relative name without any filesystem lookup. It also
    # makes an exported Windows inventory readable on another platform.
    relative = entry.relative_path.replace("\\", "/")
    # Preserve PurePosixPath's treatment of redundant separators and '.' while
    # avoiding a path object, cached parsing state and parts tuple per file.
    parts = [part for part in relative.split("/") if part and part != "."]
    if relative.startswith("/") or ".." in parts or not parts:
        return "[범위 밖 경로]"
    return parts[0] if len(parts) > 1 else "."


def inventory_date_bucket(entry: FileEntry, now: float | None = None) -> str:
    """Group modification ages without making up unavailable creation dates."""
    reference = time.time() if now is None else now
    modified = entry.modified
    if not isinstance(modified, (int, float)) or not math.isfinite(modified):
        return "unknown"
    age = reference - modified
    if age < 0:
        return "future"
    if age <= 30 * 86400:
        return "recent_30"
    if age <= 365 * 86400:
        return "year"
    return "older"


def _record(key: str, label: str) -> dict:
    return {"key": key, "label": label, "count": 0, "bytes": 0}


def _top(records: Iterable[dict], limit: int) -> list[dict]:
    """A fixed-capacity heap; stable, readable ties do not require a full sort."""
    return heapq.nlargest(limit, records, key=lambda item: (item["bytes"], item["count"], item["key"]))


def summarize_inventory(entries: Iterable[FileEntry], root: str | Path, top_limit: int = 10,
                        *, now: float | None = None) -> dict:
    """Describe only inventoried files, including zero-byte and old-file counts.

    This counts logical file sizes once per inventory entry, including hard
    links. It is not physical disk usage, a sum of recoverable storage, nor a
    claim that old or empty files are safe to delete. No source path is opened.
    """
    if type(top_limit) is not int or not 1 <= top_limit <= 100:
        raise ValueError("큰 파일 표시는 1~100개로 설정하세요.")
    reference = time.time() if now is None else now
    if not isinstance(reference, (int, float)) or not math.isfinite(reference):
        raise ValueError("날짜 기준이 올바르지 않습니다.")
    types = {key: _record(key, label) for key, label in TYPE_LABELS.items()}
    dates = {key: _record(key, label) for key, label in DATE_LABELS.items()}
    folders: dict[str, dict] = {}
    total_files = total_bytes = zero_bytes = 0
    largest: list[tuple[int, int, FileEntry]] = []
    for position, entry in enumerate(entries):
        total_files += 1
        total_bytes += entry.size
        zero_bytes += entry.size == 0
        category = types[inventory_category(entry)]
        folder_key = inventory_folder(entry, root)
        folder = folders.get(folder_key)
        if folder is None:
            folder = folders[folder_key] = _record(folder_key, "선택 폴더 바로 아래" if folder_key == "." else folder_key)
        date = dates[inventory_date_bucket(entry, now=reference)]
        for record in (category, folder, date):
            record["count"] += 1
            record["bytes"] += entry.size
        # The sequence number breaks size ties without comparing FileEntry or
        # repeatedly comparing long filenames. Original inventory order wins.
        if len(largest) < top_limit:
            heapq.heappush(largest, (entry.size, -position, entry))
        elif entry.size > largest[0][0]:
            # Later entries never displace an equal-size earlier entry. Build
            # a tuple only for files which actually enter the bounded heap.
            heapq.heapreplace(largest, (entry.size, -position, entry))
    selected_folders = _top(folders.values(), FOLDER_LIMIT)
    others = {"count": total_files - sum(record["count"] for record in selected_folders),
              "bytes": total_bytes - sum(record["bytes"] for record in selected_folders),
              "folders": len(folders) - len(selected_folders)}
    ordered = sorted(largest, key=lambda item: (item[0], item[1]), reverse=True)
    return {
        "total_files": total_files, "total_bytes": total_bytes, "zero_byte_files": zero_bytes,
        "entry_visits": total_files,
        "by_type": _top(types.values(), len(types)), "by_folder": selected_folders,
        "folder_count": len(folders), "folders_truncated": bool(others["folders"]), "folder_others": others,
        "largest_files": [{"path": entry.path, "relative_path": entry.relative_path, "name": entry.name,
                           "size": entry.size, "modified": entry.modified, "kind": entry.kind, "suffix": entry.suffix}
                          for _size, _sequence, entry in ordered],
        "date_buckets": list(dates.values()),
        "limits": {"largest_files": top_limit, "folders": FOLDER_LIMIT},
        "reference_time": reference,
    }
