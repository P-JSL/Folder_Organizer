"""Previewed file operations with verified copies, signed journals and safe undo.

No API in this module permanently deletes a user's source file. A move first
creates and verifies a second copy; quarantine is just a journalled move. Plans
are valid only in the process which created them. Journals survive restarts.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Callable, Iterator
import uuid

STATE_DIRECTORY = ".folder-organizer"
ORGANIZED_DIRECTORY = "정리됨"
MODES = {"quarantine", "copy", "move", "date", "extension"}
_PLAN_KEY = secrets.token_bytes(32)
_CHUNK = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class OperationError(ValueError):
    """A plan, path, or filesystem state cannot be used safely."""


def _json(data: dict) -> bytes:
    unsigned = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sign(data: dict, key: bytes) -> str:
    return hmac.new(key, _json(data), hashlib.sha256).hexdigest()


def _check_signature(data: dict, key: bytes) -> None:
    signature = data.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _sign(data, key)):
        raise OperationError("계획 또는 작업 기록이 변경되었습니다. 다시 미리보기를 만드세요.")


def _absolute(value: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(value)))


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _safe_path(path: Path, *, require: bool = False) -> None:
    """Reject links/reparse points in *every* existing path component."""
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if require and component == path:
                raise OperationError(f"경로가 없습니다: {path}") from None
            continue
        if _is_link(info):
            raise OperationError(f"심볼릭 링크·재분석 지점은 처리하지 않습니다: {component}")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise OperationError(f"상위 경로가 폴더가 아닙니다: {component}")


def _root(value: str | Path) -> Path:
    root = _absolute(value)
    _safe_path(root, require=True)
    if not root.is_dir():
        raise OperationError("선택한 경로가 폴더가 아닙니다.")
    if root == Path(root.anchor) or root == Path.home():
        raise OperationError("드라이브 전체나 홈 전체 대신 정리할 하위 폴더를 선택하세요.")
    if os.name != "nt":
        for system in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/run", "/sbin", "/sys", "/usr", "/var"):
            if root == Path(system) or Path(system) in root.parents:
                raise OperationError("시스템 폴더는 정리 대상으로 사용할 수 없습니다.")
    else:
        protected = [os.environ.get("WINDIR"), os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
        for system in filter(None, protected):
            directory = _absolute(system)
            if root == directory or directory in root.parents:
                raise OperationError("시스템·설치 프로그램 폴더는 정리 대상으로 사용할 수 없습니다.")
    if STATE_DIRECTORY in root.parts:
        raise OperationError("복원 기록 폴더는 정리 대상으로 사용할 수 없습니다.")
    return root


def _inside(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


def _source(path: str | Path, root: Path) -> Path:
    source = _absolute(path)
    if not _inside(source, root) or STATE_DIRECTORY in source.relative_to(root).parts:
        raise OperationError(f"선택 폴더 밖 또는 복원 기록의 파일입니다: {source}")
    _safe_path(source, require=True)
    if not stat.S_ISREG(source.lstat().st_mode):
        raise OperationError(f"일반 파일만 처리할 수 있습니다: {source}")
    return source


def _same(info: os.stat_result, item: dict) -> bool:
    return (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino) == (
        item["size"], item["mtime_ns"], item["device"], item["inode"]
    )


def _fingerprint(path: Path) -> dict:
    _safe_path(path, require=True)
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise OperationError(f"일반 파일만 처리할 수 있습니다: {path}")
        digest = hashlib.sha256()
        while chunk := os.read(fd, _CHUNK):
            digest.update(chunk)
        after = os.fstat(fd)
        fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
        if any(getattr(before, f) != getattr(after, f) for f in fields):
            raise OperationError(f"읽는 동안 파일이 변경되었습니다: {path}")
        _safe_path(path, require=True)
        current = path.lstat()
        if any(getattr(after, f) != getattr(current, f) for f in fields):
            raise OperationError(f"읽는 동안 파일이 교체되었습니다: {path}")
        return {"size": after.st_size, "mtime_ns": after.st_mtime_ns,
                "device": after.st_dev, "inode": after.st_ino,
                "sha256": digest.hexdigest()}
    finally:
        os.close(fd)


def _verify(path: Path, item: dict, *, identity: bool = True) -> dict:
    current = _fingerprint(path)
    keys = ("size", "mtime_ns", "device", "inode", "sha256") if identity else ("size", "sha256")
    if any(current[key] != item[key] for key in keys):
        raise OperationError(f"미리보기 이후 변경되거나 교체된 파일입니다: {path}")
    return current


def _unique(candidate: Path, reserved: set[str]) -> Path:
    original = candidate
    counter = 0
    while candidate.exists() or candidate.is_symlink() or str(candidate).casefold() in reserved:
        counter += 1
        if counter > 100_000:
            raise OperationError(f"사용 가능한 파일 이름을 만들 수 없습니다: {original}")
        candidate = original.with_name(f"{original.stem} ({counter}){original.suffix}")
    reserved.add(str(candidate).casefold())
    _safe_path(candidate)
    return candidate


def _extension_folder(source: Path) -> str:
    suffix = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source.suffix.casefold().lstrip("."))[:40].rstrip(" .")
    suffix = suffix or "확장자 없음"
    # These remain reserved device names on Windows even when used as a folder.
    if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", suffix, re.IGNORECASE):
        suffix = "_" + suffix
    return suffix


def build_plan(root: str | Path, paths: list[str], mode: str,
               destination: str | None = None, date_basis: str = "modified") -> dict:
    """Hash and preview selected regular files without modifying the filesystem.

    Unsupported/missing sources cause an error; files already in their intended
    folder are skipped with warnings. No name collisions are overwritten.
    """
    directory = _root(root)
    if mode not in MODES:
        raise OperationError("지원하지 않는 정리 방식입니다.")
    if date_basis not in {"modified", "created"}:
        raise OperationError("날짜 기준은 modified 또는 created여야 합니다.")
    if mode in {"copy", "move"}:
        if not destination:
            raise OperationError("대상 폴더를 선택하세요.")
        target_root = _absolute(destination)
        _safe_path(target_root)
        if target_root.exists() and not target_root.is_dir():
            raise OperationError("대상 경로가 폴더가 아닙니다.")
        if STATE_DIRECTORY in target_root.parts:
            raise OperationError("복원 기록 폴더를 대상으로 선택할 수 없습니다.")
        if target_root == Path(target_root.anchor):
            raise OperationError("드라이브 전체 대신 대상 하위 폴더를 선택하세요.")
    else:
        target_root = directory
    identifier = uuid.uuid4().hex
    plan = {"id": identifier, "root": str(directory), "mode": mode,
            "destination": str(target_root), "date_basis": date_basis,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "items": [], "warnings": []}
    selected = list(dict.fromkeys(_source(p, directory) for p in paths))
    if not selected:
        raise OperationError("정리할 파일을 선택하세요.")
    reserved: set[str] = set()
    for source in selected:
        # Bundles are applications, not collections of independently movable
        # documents. Inventory may show their contents, but moving one member
        # would make the bundle unusable. An explicit verified copy is safe.
        if mode != "copy" and any(parent.name.casefold().endswith(".app") for parent in source.parents):
            raise OperationError(
                f"앱 번들의 내부 파일은 개별 이동·정리할 수 없습니다. "
                f"응용 프로그램 전체를 운영체제에서 관리하거나 복사를 선택하세요: {source}"
            )
        relative = source.relative_to(directory)
        if mode in {"date", "extension"} and relative.parts[0] == ORGANIZED_DIRECTORY:
            plan["warnings"].append(f"이미 정리 폴더 안에 있어 건너뜀: {relative}")
            continue
        if mode == "quarantine":
            target = directory / STATE_DIRECTORY / "quarantine" / identifier / relative
        elif mode in {"copy", "move"}:
            target = target_root / relative
        elif mode == "date":
            info = source.stat()
            timestamp = info.st_mtime if date_basis == "modified" else getattr(info, "st_birthtime", None)
            if timestamp is None and date_basis == "created" and os.name == "nt":
                timestamp = info.st_ctime
            if timestamp is None:
                plan["warnings"].append(f"생성일을 제공하지 않는 파일이라 건너뜀: {relative}")
                continue
            month = dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m")
            target = directory / ORGANIZED_DIRECTORY / month / relative
        else:
            suffix = _extension_folder(source)
            target = directory / ORGANIZED_DIRECTORY / suffix / relative
        if source == target:
            plan["warnings"].append(f"원본과 대상이 같아 건너뜀: {relative}")
            continue
        target = _unique(target, reserved)
        if target in selected:
            raise OperationError("대상이 다른 선택 파일과 충돌합니다.")
        fingerprint = _fingerprint(source)
        plan["items"].append({"source": str(source), "target": str(target),
                              "action": "copy" if mode == "copy" else "move", **fingerprint})
    plan["signature"] = _sign(plan, _PLAN_KEY)
    return plan


def _mkdir(path: Path) -> None:
    _safe_path(path)
    path.mkdir(parents=True, exist_ok=True)
    _safe_path(path, require=True)
    if not path.is_dir():
        raise OperationError(f"폴더가 아닙니다: {path}")


def _state(root: Path) -> Path:
    path = root / STATE_DIRECTORY
    _mkdir(path)
    return path


def _key(state: Path) -> bytes:
    path = state / "journal.key"
    _safe_path(path)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
    _safe_path(path, require=True)
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OperationError("작업 기록 키가 일반 파일이 아닙니다.")
        key = stream.read(33)
    if len(key) != 32:
        raise OperationError("작업 기록 키가 손상되었습니다. 기록을 덮어쓰지 않습니다.")
    return key


@contextlib.contextmanager
def _lock(state: Path) -> Iterator[None]:
    path = state / "operations.lock"
    _safe_path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _BINARY, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise OperationError("다른 정리·복원 작업이 실행 중입니다.") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise OperationError("다른 정리·복원 작업이 실행 중입니다.") from None
        yield
    finally:
        os.close(fd)


def _write_journal(path: Path, journal: dict, key: bytes, *, initial: bool = False) -> None:
    _safe_path(path)
    journal["signature"] = _sign(journal, key)
    contents = json.dumps(journal, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if initial:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        _sync_directory(path.parent)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path, require=True)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sync_directory(path: Path) -> None:
    """Flush directory entries where the OS supports it (Windows has no dir fd)."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hide_owned_partial(path: Path, device: int, inode: int,
                        before_rename: Callable[[str], None] | None) -> bool:
    """Keep failed copy bytes out of the visible folder without deleting them."""
    try:
        _safe_path(path, require=True)
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (device, inode):
            directory = path.parent / STATE_DIRECTORY / "failed-copies"
            _mkdir(directory)
            holding = directory / f"{uuid.uuid4().hex}-{path.name[-100:]}"
            if before_rename is not None:
                before_rename(str(holding))
            _rename_anchored(path, holding)
            return True
    except (OSError, OperationError):
        pass
    return False


def _copy_verified(source: Path, target: Path, item: dict,
                   on_created: Callable[[os.stat_result], None],
                   on_failed_path: Callable[[str], None] | None = None) -> None:
    _safe_path(source, require=True)
    _safe_path(target)
    _mkdir(target.parent)
    source_fd = os.open(source, os.O_RDONLY | _NOFOLLOW | _BINARY)
    target_fd: int | None = None
    owned: os.stat_result | None = None
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or not _same(before, item):
            raise OperationError(f"복사 전에 원본이 변경되었습니다: {source}")
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        owned = os.fstat(target_fd)
        on_created(owned)
        digest = hashlib.sha256()
        while chunk := os.read(source_fd, _CHUNK):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                if written <= 0:
                    raise OSError("복사 데이터를 쓸 수 없습니다.")
                view = view[written:]
        os.fsync(target_fd)
        after = os.fstat(source_fd)
        if not _same(after, item) or digest.hexdigest() != item["sha256"]:
            raise OperationError(f"복사 중 원본이 변경되었습니다: {source}")
        # Preserve ordinary executable/read/write bits, never special set-id bits.
        # Metadata is applied to our held descriptor, never a path another
        # program might have replaced with a symlink while data was written.
        if hasattr(os, "fchmod"):
            os.fchmod(target_fd, stat.S_IMODE(before.st_mode) & 0o777)
        if os.utime in os.supports_fd:
            os.utime(target_fd, ns=(before.st_atime_ns, before.st_mtime_ns))
        elif os.name == "nt":
            _windows_file_times(target_fd, before)
        _verify(source, item)
        _verify(target, item, identity=False)
        _sync_directory(target.parent)
    except BaseException:
        if target_fd is not None:
            os.close(target_fd)
            target_fd = None
        if owned is not None:
            _hide_owned_partial(target, owned.st_dev, owned.st_ino, on_failed_path)
        raise
    finally:
        os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)


def _windows_file_times(fd: int, original: os.stat_result) -> None:
    """Preserve Windows times using the held file handle, without path races."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    def convert(nanoseconds: int) -> FileTime:
        ticks = nanoseconds // 100 + 116_444_736_000_000_000
        return FileTime(ticks & 0xFFFFFFFF, ticks >> 32)

    access = convert(original.st_atime_ns)
    modified = convert(original.st_mtime_ns)
    function = ctypes.windll.kernel32.SetFileTime
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileTime),
                         ctypes.POINTER(FileTime), ctypes.POINTER(FileTime)]
    function.restype = wintypes.BOOL
    if not function(wintypes.HANDLE(msvcrt.get_osfhandle(fd)), None,
                    ctypes.byref(access), ctypes.byref(modified)):
        raise ctypes.WinError()


@contextlib.contextmanager
def _directory_descriptor(path: Path) -> Iterator[int | None]:
    """Anchor POSIX renames to no-follow directory descriptors."""
    descriptor: int | None = None
    try:
        if os.name == "posix" and os.open in os.supports_dir_fd:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
            descriptor = os.open(path.anchor, flags)
            for part in path.parts[1:]:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        else:
            _safe_path(path, require=True)
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _rename_anchored(source: Path, target: Path) -> None:
    _safe_path(source, require=True)
    _safe_path(target)
    with _directory_descriptor(source.parent) as source_directory, _directory_descriptor(target.parent) as target_directory:
        if source_directory is not None and target_directory is not None:
            os.rename(source.name, target.name, src_dir_fd=source_directory,
                      dst_dir_fd=target_directory)
        else:
            os.rename(source, target)
    _sync_directory(source.parent)
    _sync_directory(target.parent)


def _return_retained(holding: Path, original: Path) -> bool:
    """Restore an unexpected captured file with an exclusive hardlink.

    This never replaces a newly created original. The retained link is kept as
    well: removing unexpected user bytes is never part of failure recovery.
    """
    try:
        _safe_path(original)
        _safe_path(holding, require=True)
        os.link(holding, original, follow_symlinks=False)
        _sync_directory(original.parent)
        return True
    except (OSError, OperationError, NotImplementedError):
        return False


def _retain_verified(path: Path, expected: dict, root: Path, operation_id: str,
                     item: dict, field: str, save: Callable[[], None]) -> Path:
    """Atomically retain bytes before removing a visible path, never unlink them.

    Even an edit/replacement after a final verification cannot destroy bytes:
    the captured file is checked *after* its rename and unexpected contents are
    returned when possible, otherwise retained at the journalled recovery path.
    Files on another volume use a hidden retention folder beside that file.
    """
    _safe_path(path, require=True)
    root_state = _state(root)
    state = root_state if path.parent.stat().st_dev == root_state.stat().st_dev else path.parent / STATE_DIRECTORY
    holding_directory = state / "retained" / operation_id
    _mkdir(holding_directory)
    holding = holding_directory / f"{uuid.uuid4().hex}-{path.name[-100:]}"
    item[field] = str(holding)
    save()  # durable intent exists before the atomic rename
    _rename_anchored(path, holding)
    try:
        _verify(holding, expected)
    except (OSError, OperationError) as exc:
        restored = _return_retained(holding, path)
        item["recovery_path"] = str(holding)
        save()
        location = f"원래 위치와 {holding}" if restored else str(holding)
        raise OperationError(f"작업 직전 파일이 변경되었습니다. 변경된 파일을 보존했습니다: {location}. {exc}") from exc
    save()
    return holding


def execute_plan(plan: dict) -> dict:
    """Execute an unchanged preview. Individual failures never discard originals."""
    _check_signature(plan, _PLAN_KEY)
    directory = _root(plan["root"])
    if not re.fullmatch(r"[0-9a-f]{32}", plan.get("id", "")):
        raise OperationError("유효하지 않은 작업 ID입니다.")
    if not plan["items"]:
        raise OperationError("실행할 파일이 없습니다. 미리보기의 안내를 확인하세요.")
    state = _state(directory)
    with _lock(state):
        key = _key(state)
        journals = state / "history"
        _mkdir(journals)
        journal_path = journals / f"{plan['id']}.json"
        journal = {"version": 1, "id": plan["id"], "root": str(directory),
                   "mode": plan["mode"], "created_at": plan["created_at"],
                   "status": "running", "items": [], "journal_path": str(journal_path)}
        for item in plan["items"]:
            journal["items"].append({**item, "state": "pending", "error": None,
                                     "target_created": False})
        _write_journal(journal_path, journal, key, initial=True)
        for item in journal["items"]:
            source, target = Path(item["source"]), Path(item["target"])
            try:
                _source(source, directory)
                _verify(source, item)
                _safe_path(target)
                item["state"] = "copying"
                _write_journal(journal_path, journal, key)

                def created(info: os.stat_result) -> None:
                    item["target_created"] = True
                    item["target_device"] = info.st_dev
                    item["target_inode"] = info.st_ino
                    _write_journal(journal_path, journal, key)

                def failed_copy(path: str) -> None:
                    item["partial_backup"] = path
                    _write_journal(journal_path, journal, key)

                _copy_verified(source, target, item, created, failed_copy)
                item["state"] = "copied"
                _write_journal(journal_path, journal, key)
                if item["action"] == "move":
                    _source(source, directory)
                    _verify(source, item)
                    _verify(target, item, identity=False)
                    _retain_verified(source, item, directory, journal["id"], item,
                                     "original_backup", lambda: _write_journal(journal_path, journal, key))
                    # A destination edited during the retire step is never the
                    # last original: the exact source is retained privately.
                    _verify(target, item, identity=False)
                item["state"] = "completed"
            except (OSError, OperationError) as exc:
                item["error"] = str(exc)
                if item["target_created"] and target.exists():
                    item["state"] = "copy_preserved"
                else:
                    item["state"] = "failed"
                    item["target_created"] = False
            _write_journal(journal_path, journal, key)
        successes = sum(item["state"] == "completed" for item in journal["items"])
        journal["status"] = "completed" if successes == len(journal["items"]) else "partial" if successes else "failed"
        _write_journal(journal_path, journal, key)
        return journal


def _read_journal(path: Path, root: Path, key: bytes) -> dict:
    _safe_path(path, require=True)
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OperationError("작업 기록이 일반 파일이 아닙니다.")
        journal = json.load(stream)
    _check_signature(journal, key)
    if journal.get("root") != str(root) or path.name != f"{journal.get('id')}.json" or journal.get("version") != 1:
        raise OperationError("작업 기록의 폴더 또는 형식이 일치하지 않습니다.")
    return journal


def list_history(root: str | Path) -> list[dict]:
    """List valid durable journals, raising on tampered records instead of trusting them."""
    directory = _root(root)
    state = directory / STATE_DIRECTORY
    _safe_path(state)
    history = state / "history"
    _safe_path(history)
    if not history.exists():
        return []
    key = _key(state)
    result = [_read_journal(path, directory, key) for path in history.glob("*.json")]
    return sorted(result, key=lambda item: item["created_at"], reverse=True)


def undo_operation(root: str | Path, operation_id: str) -> dict:
    """Restore moved files or remove unchanged copies; conflicts remain recoverable."""
    directory = _root(root)
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise OperationError("유효하지 않은 작업 ID입니다.")
    state = directory / STATE_DIRECTORY
    _safe_path(state, require=True)
    with _lock(state):
        key = _key(state)
        journal_path = state / "history" / f"{operation_id}.json"
        journal = _read_journal(journal_path, directory, key)
        for item in reversed(journal["items"]):
            if item["state"] in {"restored", "unchanged"}:
                continue
            if not item.get("target_created"):
                item["state"] = "unchanged"
                continue
            source, target = Path(item["source"]), Path(item["target"])
            try:
                if not _inside(source, directory) or STATE_DIRECTORY in source.relative_to(directory).parts:
                    raise OperationError("복원 원본 경로가 선택 폴더 밖입니다.")
                _safe_path(source)
                _safe_path(target, require=True)
                target_fingerprint = _verify(target, item, identity=False)
                if (target_fingerprint["device"], target_fingerprint["inode"]) != (item["target_device"], item["target_inode"]):
                    raise OperationError(f"작업 이후 대상 파일이 교체되었습니다: {target}")
                if item["action"] == "copy":
                    # A copy must not become the last remaining original by undo.
                    _source(source, directory)
                    _verify(source, item)
                elif source.exists() or source.is_symlink():
                    # An interrupted move may have left the untouched source, or
                    # an interrupted undo may already have restored our copy.
                    # A newly created/replaced original is always a conflict.
                    try:
                        _verify(source, item.get("restore_fingerprint", item))
                    except (OSError, OperationError):
                        raise OperationError(f"원래 위치에 파일이 있어 덮어쓰지 않습니다: {source}") from None
                else:
                    recovery = Path(item.get("original_backup", str(target)))
                    recovery_fingerprint = _verify(recovery, item, identity=False)
                    created_restore: list[os.stat_result] = []
                    def failed_restore(path: str) -> None:
                        item["restore_partial_backup"] = path
                        _write_journal(journal_path, journal, key)

                    _copy_verified(recovery, source, recovery_fingerprint, created_restore.append, failed_restore)
                    _verify(source, item, identity=False)
                    item["restore_fingerprint"] = _fingerprint(source)
                    _write_journal(journal_path, journal, key)
                _verify(target, item, identity=False)
                _safe_path(target, require=True)
                _retain_verified(target, target_fingerprint, directory, journal["id"], item,
                                 "undo_backup", lambda: _write_journal(journal_path, journal, key))
                item["state"] = "restored"
                item["error"] = None
            except (OSError, OperationError) as exc:
                item["state"] = "undo_failed"
                item["error"] = str(exc)
            _write_journal(journal_path, journal, key)
        journal["status"] = "undone" if all(item["state"] in {"restored", "unchanged"} for item in journal["items"]) else "partial_undo"
        _write_journal(journal_path, journal, key)
        return journal
