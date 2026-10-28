"""Choose a recoverable deletion destination without attempting OS deletion.

Windows' ``FOF_ALLOWUNDO`` means "if possible", and a successful Recycle Bin
query does not establish that a particular file will be recycled. Network
shares may have a server-side trash policy that the Windows client cannot
verify. Until a recycle-only backend and its receipt have been validated, this
app moves files to ``delete_files`` instead. This module contains no delete,
unlink, rename or Shell file-operation call.

Microsoft's authoritative API contracts:
https://github.com/MicrosoftDocs/sdk-api/blob/docs/sdk-api-src/content/shobjidl_core/nf-shobjidl_core-ifileoperation-setoperationflags.md
https://github.com/MicrosoftDocs/sdk-api/blob/docs/sdk-api-src/content/shobjidl_core/nf-shobjidl_core-ifileoperationprogresssink-postdeleteitem.md
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
import sys
import stat
from pathlib import Path

DELETE_DIRECTORY = "delete_files"
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4


class RecycleUnavailableError(ValueError):
    """No verified recycle-only implementation may mutate the source."""


def _is_windows() -> bool:
    return sys.platform == "win32"


def _network_path(value: str) -> bool:
    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    return folded.startswith("\\\\?\\unc\\") or (
        normalized.startswith("\\\\") and not folded.startswith(("\\\\?\\", "\\\\.\\"))
    )


def _drive_root(value: str) -> str | None:
    """Parse drive roots without resolving links or touching the file itself."""
    drive, tail = ntpath.splitdrive(value)
    if not tail.startswith(("\\", "/")):
        return None
    if re.fullmatch(r"[A-Za-z]:", drive):
        return drive + "\\"
    if re.fullmatch(r"\\\\\?\\[A-Za-z]:", drive):
        return drive + "\\"
    return None


def _drive_type(root: str) -> int:
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_type = kernel.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    return int(get_type(root))


def is_network_location(path: str | os.PathLike[str]) -> bool:
    """Identify share roots without inspecting their trash policy or contents."""
    value = os.fspath(path)
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    if _network_path(value):
        return True
    if not _is_windows():
        return False
    root = _drive_root(value)
    if root is None:
        return False
    try:
        return _drive_type(root) == _DRIVE_REMOTE
    except (OSError, AttributeError, ValueError):
        return False


def _query_recycle_bin(root: str) -> bool:
    """Query availability only; success cannot authorize deleting a file."""
    from ctypes import wintypes

    class QueryInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_int64),
                    ("i64NumItems", ctypes.c_int64)]

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    query = shell.SHQueryRecycleBinW
    query.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(QueryInfo)]
    query.restype = wintypes.LONG
    information = QueryInfo()
    information.cbSize = ctypes.sizeof(information)
    return int(query(root, ctypes.byref(information))) == 0


def nas_recycle_observation(path: str | os.PathLike[str]) -> dict:
    """Observe a NAS trash folder without trusting its server policy.

    At most two directory metadata requests are made. Folder existence does not
    authorize unlinking a file, and this function never lists trash contents.
    """
    value = os.fspath(path)
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("NAS 확인 경로가 올바르지 않습니다.")
    candidates = []
    if _network_path(value) or (_is_windows() and _drive_root(value)):
        drive, _tail = ntpath.splitdrive(value)
        if drive:
            candidates.append(ntpath.join(drive + "\\", "#recycle"))
        candidates.append(ntpath.join(value, "#recycle"))
    else:
        candidates.append(str(Path(value) / "#recycle"))
    result = {"nas_recycle_detected": False, "nas_recycle_path": None,
              "nas_recycle_policy_verified": False}
    for candidate in dict.fromkeys(candidates):
        try:
            info = os.stat(candidate, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400:
                result.update(nas_recycle_detected=True, nas_recycle_path=candidate)
                break
        except OSError:
            continue
    return result


def deletion_destination(path: str | os.PathLike[str]) -> dict:
    """Return read-only routing evidence for the safe ``delete_files`` move.

    ``recycle_bin_query_succeeded`` is informational. It is deliberately kept
    separate from ``recycle_only_verified``, which remains false in this build.
    A caller must not treat a readable local Recycle Bin as per-file support.
    No files, directories, preferences or network-share settings are changed.
    """
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise ValueError("삭제 경로는 문자열 또는 파일 경로여야 합니다.") from exc
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("삭제 경로가 올바르지 않습니다.")
    result = {
        "kind": "delete_files", "directory_name": DELETE_DIRECTORY,
        "reason_code": "unsupported_platform", "volume_kind": "unknown",
        "recycle_bin_checked": False, "recycle_bin_query_succeeded": None,
        "recycle_only_verified": False,
        "reason": "휴지통 이동을 안전하게 확인할 수 없어 delete_files에 보관합니다.",
    }
    if _network_path(value):
        result.update(reason_code="network_share", volume_kind="network",
                      reason="NAS·네트워크 공유의 서버 휴지통을 확인할 수 없어 delete_files에 보관합니다.")
        return result
    if not _is_windows():
        result["volume_kind"] = "non_windows"
        return result
    root = _drive_root(value)
    if root is None:
        result["reason_code"] = "unknown_windows_volume"
        return result
    try:
        drive_type = _drive_type(root)
    except (OSError, AttributeError, ValueError):
        result["reason_code"] = "volume_query_failed"
        return result
    if drive_type == _DRIVE_REMOTE:
        result.update(reason_code="network_share", volume_kind="network",
                      reason="NAS·네트워크 드라이브의 서버 휴지통을 확인할 수 없어 delete_files에 보관합니다.")
        return result
    result["volume_kind"] = {
        _DRIVE_FIXED: "fixed", _DRIVE_REMOVABLE: "removable",
        _DRIVE_UNKNOWN: "unknown", _DRIVE_NO_ROOT_DIR: "unavailable",
    }.get(drive_type, "unsupported")
    if drive_type != _DRIVE_FIXED:
        result["reason_code"] = "unsupported_volume"
        return result
    result["recycle_bin_checked"] = True
    try:
        result["recycle_bin_query_succeeded"] = _query_recycle_bin(root)
    except (OSError, AttributeError, ValueError):
        result["recycle_bin_query_succeeded"] = False
    result["reason_code"] = ("recycle_only_unverified" if result["recycle_bin_query_succeeded"]
                             else "recycle_bin_query_failed")
    return result


def recycle_file(path: str | os.PathLike[str], expected: dict | None = None) -> dict:
    """Refuse an unverified OS-delete path without mutating the source.

    Kept as an explicit boundary for a future receipt-verified implementation;
    current callers use ``deletion_destination`` and an atomic recoverable move.
    """
    selection = deletion_destination(path)
    raise RecycleUnavailableError(selection["reason"])
