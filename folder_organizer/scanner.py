"""Read-only folder inventory. Symbolic links and internal journals are skipped."""

from __future__ import annotations

import os
from fnmatch import fnmatchcase
import re
import stat
from pathlib import Path
from threading import Event
from typing import Callable

from .models import FileEntry, ScanResult

APPLICATION_SUFFIXES = {
    ".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg", ".deb", ".rpm",
    ".appimage", ".desktop", ".bat", ".cmd", ".com",
}
INTERNAL_NAMES = {".folder-organizer"}


def normalize_exclude_patterns(patterns: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Validate bounded, relative, case-insensitive folder/name patterns."""
    if not isinstance(patterns, (tuple, list)) or len(patterns) > 100:
        raise ValueError("제외 패턴은 최대 100개의 목록이어야 합니다.")
    result = []
    for value in patterns:
        if not isinstance(value, str):
            raise ValueError("제외 패턴은 문자열이어야 합니다.")
        value = value.strip().replace("\\", "/")
        if not value:
            continue
        if len(value) > 512 or "\0" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value) or ".." in value.split("/"):
            raise ValueError("제외 패턴은 '..' 없는 상대 경로 또는 이름이어야 합니다.")
        result.append(value.rstrip("/"))
    return tuple(dict.fromkeys(result))


def _excluded(relative: Path, patterns: tuple[str, ...]) -> bool:
    path = relative.as_posix().casefold()
    for pattern in patterns:
        pattern = pattern.casefold()
        if "/" not in pattern:
            if any(fnmatchcase(part.casefold(), pattern) for part in relative.parts):
                return True
        elif fnmatchcase(path, pattern) or fnmatchcase(path + "/", pattern):
            return True
    return False


def _creation_time(info: os.stat_result) -> tuple[float | None, str]:
    birthtime = getattr(info, "st_birthtime", None)
    if birthtime is not None:
        return birthtime, "birthtime"
    # On Windows st_ctime is the creation time. On Unix it is metadata-change
    # time and must never be presented as the creation time.
    if os.name == "nt":
        return info.st_ctime, "windows_creation_time"
    return None, "unavailable"


def scan_folder(
    root: str | Path,
    recursive: bool = True,
    include_hidden: bool = False,
    cancel: Event | None = None,
    progress: Callable[[str], None] | None = None,
    *,
    exclude_patterns: tuple[str, ...] | list[str] = (),
    min_size: int = 0,
    max_size: int | None = None,
    progress_detail: Callable[[dict], None] | None = None,
) -> ScanResult:
    """Inventory regular files without following links or changing any file.

    Permission errors and files that disappear mid-scan appear in ``warnings``.
    An invalid root is a ``ValueError``; cancellation returns a marked partial
    inventory. macOS .app bundles are inventoried as individual application
    files, so the displayed total remains the real file byte total.
    """
    patterns = normalize_exclude_patterns(exclude_patterns)
    if type(min_size) is not int or min_size < 0 or (max_size is not None and (type(max_size) is not int or max_size < min_size)):
        raise ValueError("파일 크기 범위는 0 이상의 바이트이며 최대값은 최소값 이상이어야 합니다.")
    folder = Path(os.path.abspath(os.path.expanduser(str(root))))
    if folder.is_symlink():
        raise ValueError("The selected root must be a real folder, not a symbolic link.")
    try:
        folder = folder.resolve(strict=True)
        info = folder.stat()
    except OSError as exc:
        raise ValueError(f"Cannot access selected folder: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("The selected path is not a folder.")
    result = ScanResult(root=str(folder))
    result.scan_options = {"recursive": recursive, "include_hidden": include_hidden,
                           "exclude_patterns": list(patterns), "min_size": min_size, "max_size": max_size}
    total_bytes = 0

    def report_detail(message: str, *, terminal: bool = False) -> None:
        if progress_detail is not None:
            progress_detail({"phase": "scan", "completed": len(result.files),
                             "total": len(result.files) if terminal and not result.cancelled else None,
                             "unit": "파일", "message": message, "bytes": total_bytes,
                             "filtered_files": result.filtered_files,
                             "excluded_directories": result.excluded_directories,
                             "warnings": len(result.warnings), "terminal": terminal,
                             "cancelled": result.cancelled})

    report_detail("폴더 안의 파일을 찾고 있습니다. 전체 개수는 탐색이 끝난 뒤 확인됩니다.")
    # Iterative traversal supports deep trees without Python recursion limits.
    pending = [folder]
    seen_directories: set[tuple[int, int]] = set()
    while pending:
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        directory = pending.pop()
        try:
            current = directory.lstat()
            if stat.S_ISLNK(current.st_mode) or (
                getattr(current, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                result.warnings.append(f"Skipped symbolic link: {directory}")
                continue
            identity = (current.st_dev, current.st_ino)
            if current.st_ino and identity in seen_directories:
                result.warnings.append(f"Skipped repeated directory: {directory}")
                continue
            if current.st_ino:
                seen_directories.add(identity)
            with os.scandir(directory) as stream:
                children = sorted(stream, key=lambda item: item.name.casefold())
        except OSError as exc:
            result.warnings.append(f"Cannot read folder {directory}: {exc}")
            continue
        for child in children:
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                break
            if child.name.casefold() in INTERNAL_NAMES:
                continue
            if not include_hidden and child.name.startswith("."):
                continue
            path = Path(child.path)
            try:
                details = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(details.st_mode) or (
                    getattr(details, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                ):
                    result.warnings.append(f"Skipped symbolic link: {path}")
                    continue
                if not include_hidden and (
                    getattr(details, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 2)
                ):
                    continue
                if stat.S_ISDIR(details.st_mode):
                    if _excluded(path.relative_to(folder), patterns):
                        result.excluded_directories += 1
                        continue
                    if recursive:
                        pending.append(path)
                    continue
                if not stat.S_ISREG(details.st_mode):
                    result.warnings.append(f"Skipped non-regular file: {path}")
                    continue
                # On Windows DirEntry.stat() deliberately reports st_dev/st_ino
                # as zero. Use a path stat for the same stable file identity
                # that open/fstat will check before hashing and recovery.
                if os.name == "nt":
                    details = path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
                        result.warnings.append(f"Skipped changed or linked file: {path}")
                        continue
                relative = path.relative_to(folder)
                if _excluded(relative, patterns) or details.st_size < min_size or (max_size is not None and details.st_size > max_size):
                    result.filtered_files += 1
                    continue
                created, source = _creation_time(details)
                suffix = path.suffix.casefold()
                in_app_bundle = folder.name.casefold().endswith(".app") or any(part.casefold().endswith(".app") for part in relative.parts[:-1])
                result.files.append(FileEntry(
                    path=str(path), relative_path=str(relative), name=path.name,
                    size=details.st_size, modified=details.st_mtime,
                    created=created, created_source=source,
                    kind="application" if suffix in APPLICATION_SUFFIXES or in_app_bundle else "file",
                    suffix=suffix, mtime_ns=details.st_mtime_ns,
                    device=details.st_dev, inode=details.st_ino,
                ))
                total_bytes += details.st_size
                if progress is not None:
                    progress(str(relative))
                report_detail(str(relative))
            except OSError as exc:
                result.warnings.append(f"Cannot inspect {path}: {exc}")
        if result.cancelled:
            break
    result.files.sort(key=lambda item: item.relative_path.casefold())
    report_detail("폴더 분석을 중단했습니다." if result.cancelled else "폴더 탐색을 완료했습니다.", terminal=True)
    return result
