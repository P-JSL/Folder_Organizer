"""Read-only folder inventory. Symbolic links and internal journals are skipped."""

from __future__ import annotations

import os
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
) -> ScanResult:
    """Inventory regular files without following links or changing any file.

    Permission errors and files that disappear mid-scan appear in ``warnings``.
    An invalid root is a ``ValueError``; cancellation returns a marked partial
    inventory. macOS .app bundles are inventoried as individual application
    files, so the displayed total remains the real file byte total.
    """
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
            if child.name in INTERNAL_NAMES:
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
                    if recursive:
                        pending.append(path)
                    continue
                if not stat.S_ISREG(details.st_mode):
                    result.warnings.append(f"Skipped non-regular file: {path}")
                    continue
                created, source = _creation_time(details)
                relative = path.relative_to(folder)
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
                if progress is not None:
                    progress(str(relative))
            except OSError as exc:
                result.warnings.append(f"Cannot inspect {path}: {exc}")
        if result.cancelled:
            break
    result.files.sort(key=lambda item: item.relative_path.casefold())
    return result
