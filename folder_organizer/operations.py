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
import shutil
import stat
from typing import Callable, Iterator
import uuid

from . import execution_log
from . import recovery_metadata
from . import deletion
from .deletion import DELETE_DIRECTORY

STATE_DIRECTORY = ".folder-organizer"
ORGANIZED_DIRECTORY = "정리됨"
MODES = {"quarantine", "copy", "move", "date", "extension"}
_PLAN_KEY = secrets.token_bytes(32)
_CHUNK = 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class OperationError(ValueError):
    """A plan, path, or filesystem state cannot be used safely."""


def _progress(callback, phase: str, completed: int, total: int | None, message: str) -> None:
    if callback:
        callback({"phase": phase, "completed": completed, "total": total,
                  "unit": "files", "message": message})


def _available_parent(path: Path) -> Path:
    while not path.exists():
        if path == path.parent:
            raise OperationError("대상 드라이브를 확인할 수 없습니다.")
        path = path.parent
    _safe_path(path, require=True)
    return path


def _capacity(items: list[dict]) -> None:
    """Require space for verified copies, without assuming retained bytes vanish."""
    volumes: dict[int, tuple[Path, int]] = {}
    for item in items:
        parent = _available_parent(Path(item["target"]).parent)
        device = parent.stat().st_dev
        previous = volumes.get(device, (parent, 0))
        volumes[device] = (previous[0], previous[1] + item["size"])
    for parent, needed in volumes.values():
        # Retained originals are not counted as reclaimed storage.
        reserve = max(1024 * 1024, needed // 20)
        if shutil.disk_usage(parent).free < needed + reserve:
            raise OperationError(f"검증 복사와 안전 여유 공간이 부족합니다: {parent} (필요 {needed + reserve:,} 바이트)")


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
    network_share_root = os.name == "nt" and root == Path(root.anchor) and deletion.is_network_location(root)
    if (root == Path(root.anchor) and not network_share_root) or root == Path.home():
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
    if STATE_DIRECTORY.casefold() in {part.casefold() for part in root.parts}:
        raise OperationError("복원 기록 폴더는 정리 대상으로 사용할 수 없습니다.")
    return root


def _inside(path: Path, root: Path) -> bool:
    return path != root and root in path.parents


def _source(path: str | Path, root: Path) -> Path:
    source = _absolute(path)
    if not _inside(source, root) or STATE_DIRECTORY.casefold() in {part.casefold() for part in source.relative_to(root).parts}:
        raise OperationError(f"선택 폴더 밖 또는 복원 기록의 파일입니다: {source}")
    if source.relative_to(root).parts[0].casefold() == DELETE_DIRECTORY.casefold():
        raise OperationError("delete_files에 보관한 파일은 다시 정리 대상으로 처리하지 않습니다.")
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
        if (any(getattr(before, f) != getattr(after, f) for f in fields)
                or before.st_ctime_ns != after.st_ctime_ns):
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


def _deletion_target(directory: Path, source: Path, reserved: set[str], *, counters: dict | None = None) -> Path:
    """One visible fallback folder, with exclusive collision-safe flat names."""
    destination = directory / DELETE_DIRECTORY
    if _inside(source, destination):
        raise OperationError("delete_files에 보관한 파일은 다시 삭제 대상으로 선택하지 않습니다.")
    _safe_path(destination)
    if destination.exists() and not destination.is_dir():
        raise OperationError("delete_files 위치에 파일이 있어 안전하게 보관할 수 없습니다.")
    original = destination / source.name
    if counters is None:
        return _unique(original, reserved)
    key = source.name.casefold()
    counter = counters.get(key, 0)
    while True:
        candidate = original if counter == 0 else original.with_name(f"{original.stem} ({counter}){original.suffix}")
        counter += 1
        if counter > 100_001:
            raise OperationError("delete_files의 파일 이름 충돌이 너무 많습니다.")
        if not candidate.exists() and not candidate.is_symlink() and str(candidate).casefold() not in reserved:
            counters[key] = counter
            reserved.add(str(candidate).casefold())
            _safe_path(candidate)
            return candidate


def build_plan(root: str | Path, paths: list[str], mode: str,
               destination: str | None = None, date_basis: str = "modified", *,
               progress_detail: Callable[[dict], None] | None = None) -> dict:
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
    if mode == "quarantine":
        plan["deletion_storage"] = "delete_files"
    selected = list(dict.fromkeys(_source(p, directory) for p in paths))
    selected_paths = set(selected)
    if not selected:
        raise OperationError("정리할 파일을 선택하세요.")
    reserved: set[str] = set()
    deletion_counters: dict = {}
    _progress(progress_detail, "preview", 0, len(selected), "변경 전 파일 검증")
    for index, source in enumerate(selected, 1):
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
            target = _deletion_target(directory, source, reserved, counters=deletion_counters)
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
        if mode != "quarantine":
            target = _unique(target, reserved)
        if target in selected_paths:
            raise OperationError("대상이 다른 선택 파일과 충돌합니다.")
        fingerprint = _fingerprint(source)
        plan["items"].append({"source": str(source), "target": str(target),
                              "action": "copy" if mode == "copy" else "move", **fingerprint})
        if mode == "quarantine":
            plan["items"][-1]["deletion_storage"] = "delete_files"
        _progress(progress_detail, "preview", index, len(selected), f"미리보기 검증: {relative}")
    plan["signature"] = _sign(plan, _PLAN_KEY)
    return plan


def build_routed_plan(root: str | Path, routes: list[dict], *,
                      destination: str | Path | None = None,
                      progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Build a signed classification preview from validated relative routes.

    A route includes source, relative_target, action and an optional rule label.
    No directory or file is created while making the preview.
    """
    directory = _root(root)
    target_root = _absolute(destination) if destination else directory / ORGANIZED_DIRECTORY
    _safe_path(target_root)
    if target_root == Path(target_root.anchor) or STATE_DIRECTORY.casefold() in {part.casefold() for part in target_root.parts}:
        raise OperationError("드라이브 전체 또는 복원 기록 폴더는 분류 대상으로 사용할 수 없습니다.")
    if target_root.exists() and not target_root.is_dir():
        raise OperationError("분류 대상 경로가 폴더가 아닙니다.")
    identifier = uuid.uuid4().hex
    plan = {"id": identifier, "root": str(directory), "mode": "rules",
            "destination": str(target_root), "date_basis": "modified",
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "items": [], "warnings": [], "safety": ["미리보기 확인 후 실행", "원본·대상 재검증", "덮어쓰기 방지", "보호 사본과 작업 기록"]}
    reserved: set[str] = set()
    seen: set[str] = set()
    _progress(progress_detail, "preview", 0, len(routes), "자동 분류 미리보기 검증")
    for index, route in enumerate(routes, 1):
        source = _source(route["source"], directory)
        key = str(source).casefold()
        if key in seen:
            raise OperationError("동일한 파일을 여러 규칙의 대상으로 지정할 수 없습니다.")
        seen.add(key)
        action = route.get("action", "copy")
        if action not in {"copy", "move"}:
            raise OperationError("분류 작업은 복사 또는 이동이어야 합니다.")
        if action == "move" and any(parent.name.casefold().endswith(".app") for parent in source.parents):
            raise OperationError("앱 번들의 내부 파일은 개별 이동하지 않습니다.")
        relative = str(route["relative_target"]).replace("\\", "/")
        parts = relative.split("/")
        if not relative or relative.startswith("/") or any(part in {"", ".", ".."} or re.search(r'[<>:"|?*\x00-\x1f]', part) or part.casefold() == STATE_DIRECTORY.casefold() or part.endswith((" ", ".")) for part in parts):
            raise OperationError("분류 대상은 안전한 상대 경로여야 합니다.")
        if any(re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I) for part in parts):
            raise OperationError("Windows 예약 장치 이름은 분류 경로에 사용할 수 없습니다.")
        target = target_root.joinpath(*parts)
        if source == target:
            plan["warnings"].append(f"원본과 대상이 같아 건너뜀: {source.relative_to(directory)}")
            continue
        target = _unique(target, reserved)
        fingerprint = _fingerprint(source)
        plan["items"].append({"source": str(source), "target": str(target), "action": action,
                              "rule": str(route.get("rule", "사용자 규칙"))[:200], **fingerprint})
        _progress(progress_detail, "preview", index, len(routes), f"분류 검증: {source.relative_to(directory)}")
    if plan["items"]:
        _capacity(plan["items"])
    plan["signature"] = _sign(plan, _PLAN_KEY)
    return plan


def _duplicate_guard(item: dict, root: Path) -> None:
    guard = item.get("duplicate_keeper")
    if not guard:
        return
    keeper = _source(guard["path"], root)
    source = _source(item["source"], root)
    if keeper == source:
        raise OperationError("중복 사본과 보존 원본은 다른 파일이어야 합니다.")
    if item.get("snapshot_reuse") is True and guard.get("snapshot_reuse") is True:
        _snapshot_guard(keeper, guard)
        _snapshot_guard(source, item)
        return
    _verify(keeper, guard)
    _verify(source, item)
    with keeper.open("rb") as left, source.open("rb") as right:
        while True:
            a, b = left.read(_CHUNK), right.read(_CHUNK)
            if a != b:
                raise OperationError("보존 원본과 사본의 실제 내용이 달라 중복 정리를 중단했습니다.")
            if not a:
                break
    _verify(keeper, guard)
    _verify(source, item)


def _snapshot_guard(path: Path, item: dict) -> None:
    """Reuse process-authenticated content proof only for the same descriptor snapshot."""
    expected_ctime = item.get("snapshot_ctime_ns")
    if type(expected_ctime) is not int:
        raise OperationError("현재 중복 검사 스냅샷을 확인할 수 없습니다. 다시 검사하세요.")
    _safe_path(path, require=True)
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same(before, item) or before.st_ctime_ns != expected_ctime:
            raise OperationError("중복 검사 이후 파일이 변경되었습니다. 다시 검사하세요.")
        _safe_path(path, require=True)
        named = path.lstat()
        after = os.fstat(descriptor)
        if not _same(named, item) or not _same(after, item) or after.st_ctime_ns != expected_ctime:
            raise OperationError("중복 검사 이후 파일 경로 또는 내용이 변경되었습니다.")
    finally:
        os.close(descriptor)


def _duplicate_preflight(items: list[dict], root: Path) -> None:
    selected = {str(_absolute(item["source"])).casefold() for item in items}
    targets = {str(_absolute(item["target"])).casefold() for item in items}
    conflicts = selected | targets
    physical = {(item["device"], item["inode"]) for item in items if item["inode"]}
    for item in items:
        guard = item.get("duplicate_keeper")
        if guard:
            if (str(_absolute(guard["path"])).casefold() in conflicts
                    or (guard["inode"] and (guard["device"], guard["inode"]) in physical)):
                raise OperationError("보존 원본 또는 그 하드링크가 다른 정리 대상에 포함되어 있습니다.")
            _duplicate_guard(item, root)


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
    # A final compact recipe must never stay active once this working JSON can
    # describe a newer mutation. Invalidation is durable and only writes once
    # for a final-to-working transition, rather than on every file checkpoint.
    try:
        recovery_metadata.invalidate(path, Path(journal["root"]), key)
    except recovery_metadata.MetadataError as exc:
        raise OperationError(str(exc)) from exc
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


def _execution_event(event: str, **metadata) -> None:
    # Even a broken logging backend must never alter file-operation results.
    try:
        execution_log.record_execution(event, **metadata)
    except Exception:
        pass


def _move_no_replace(source: Path, target: Path) -> None:
    """Same-volume atomic rename without replacing a pre-existing destination."""
    _safe_path(source, require=True)
    _safe_path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"대상이 이미 있습니다. 덮어쓰지 않습니다: {target}")
    if os.name == "nt":
        # Windows rename refuses an existing target; no replace flag is used.
        _rename_anchored(source, target)
        return
    with _directory_descriptor(source.parent) as left, _directory_descriptor(target.parent) as right:
        if left is not None and right is not None:
            import ctypes
            import errno
            function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
            if function is not None:
                function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
                function.restype = ctypes.c_int
                result = function(left, os.fsencode(source.name), right, os.fsencode(target.name), 1)
                if result == 0:
                    _sync_directory(source.parent)
                    _sync_directory(target.parent)
                    return
                code = ctypes.get_errno()
                if code not in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                    raise OSError(code, os.strerror(code), str(source))
        raise OperationError("이 파일시스템에서 덮어쓰기 없는 원자적 이동을 확인하지 못했습니다. 원래 파일을 유지합니다.")


def _execute_direct_delete(item: dict, root: Path, journal: dict, path: Path, key: bytes) -> None:
    source, target = _source(item["source"], root), _absolute(item["target"])
    if item.get("action") != "move" or target.parent != root / DELETE_DIRECTORY:
        raise OperationError("삭제 보관 대상은 선택 폴더의 delete_files여야 합니다.")
    if item.get("snapshot_reuse") is True:
        _snapshot_guard(source, item)
    else:
        _verify(source, item)
    _duplicate_guard(item, root)
    _safe_path(target)
    _mkdir(target.parent)
    item["state"] = "moving"
    item["source_removed"] = False
    _write_journal(path, journal, key)  # durable intent precedes the rename
    moved = False
    try:
        _move_no_replace(source, target)
        moved = True
        info = target.lstat()
        item.update(target_created=True, target_device=info.st_dev, target_inode=info.st_ino,
                    source_removed=True, deletion_path=str(target))
        _write_journal(path, journal, key)
        _safe_path(target, require=True)
        if item.get("snapshot_reuse") is True:
            # Rename changes ctime itself. The authenticated pre-rename content
            # proof is reused; captured identity/size/mtime must remain exact.
            if not stat.S_ISREG(info.st_mode) or not _same(info, item):
                raise OperationError("이동하는 동안 파일이 바뀌었습니다. 변경된 파일을 보존합니다.")
        else:
            _verify(target, item)
        guard = item.get("duplicate_keeper")
        if guard:
            keeper = _source(guard["path"], root)
            if item.get("snapshot_reuse") is True and guard.get("snapshot_reuse") is True:
                _snapshot_guard(keeper, guard)
            else:
                _verify(keeper, guard)
        if source.exists() or source.is_symlink():
            item["source_reappeared"] = True
    except BaseException:
        if moved:
            try:
                _move_no_replace(target, source)
            except (OSError, OperationError):
                item.update(source_removed=True, deletion_path=str(target), target_created=True)
            else:
                item.update(source_removed=False, target_created=False, deletion_returned=True)
        raise


def execute_plan(plan: dict, *, progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Execute a preview and retain bounded, nonfatal execution receipts."""
    identifier = plan.get("id", "") if isinstance(plan, dict) else ""
    action = plan.get("mode", "unknown") if isinstance(plan, dict) else "unknown"
    _execution_event("execution.started", execution_id=identifier, action=action,
                     status="started", message="파일 정리 실행을 시작합니다.")
    try:
        result = _execute_plan(plan, progress_detail=progress_detail)
    except Exception as exc:
        _execution_event("execution.failed", execution_id=identifier, action=action,
                         status="failed", message="파일 정리 실행이 중단되었습니다.", error=str(exc))
        raise
    _execution_event("execution.finished", execution_id=identifier, action=action,
                     status=result["status"], message="파일 정리 처리 결과입니다.",
                     completed=sum(item["state"] == "completed" for item in result["items"]),
                     total=len(result["items"]))
    return result


def _execute_plan(plan: dict, *, progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Execute an unchanged preview. Individual failures never discard originals."""
    _check_signature(plan, _PLAN_KEY)
    directory = _root(plan["root"])
    if not re.fullmatch(r"[0-9a-f]{32}", plan.get("id", "")):
        raise OperationError("유효하지 않은 작업 ID입니다.")
    if not plan["items"]:
        raise OperationError("실행할 파일이 없습니다. 미리보기의 안내를 확인하세요.")
    if plan.get("mode") in {"rules", "duplicate_auto"} and not all(item.get("deletion_storage") == "delete_files" for item in plan["items"]):
        _capacity(plan["items"])
    # Reject the entire automatic duplicate preview before any journal or copy
    # when any designated keeper has changed or could itself be moved.
    _duplicate_preflight(plan["items"], directory)
    deletion_policy = None
    if any(item.get("deletion_storage") == "delete_files" for item in plan["items"]):
        # Probe Windows/NAS policy once per operation, not for every file. The
        # current backend chooses a recoverable folder unless recycling itself
        # has been verified; an observed NAS trash folder is not such proof.
        try:
            selection = deletion.deletion_destination(directory)
            if selection.get("volume_kind") == "network":
                # Probe only a validated, accessible operation root. Merely
                # parsing an arbitrary UNC path must not contact that server.
                selection.update(deletion.nas_recycle_observation(directory))
                if selection.get("nas_recycle_detected"):
                    selection["reason"] = "NAS의 #recycle 폴더를 찾았지만 파일별 휴지통 정책을 확인할 수 없어 delete_files에 보관합니다."
            deletion_policy = {str(name)[:80]: value[:2048] if isinstance(value, str) else value
                               for name, value in selection.items()
                               if value is None or isinstance(value, (str, int, float, bool))}
        except Exception as exc:
            deletion_policy = {"kind": "delete_files", "reason": "휴지통 보장을 확인하지 못해 delete_files를 사용합니다.",
                               "probe_error": str(exc)[:300], "recycle_only_verified": False}
    _progress(progress_detail, "execute", 0, len(plan["items"]), "검증된 작업 시작")
    state = _state(directory)
    with _lock(state):
        key = _key(state)
        journals = state / "history"
        _mkdir(journals)
        journal_path = journals / f"{plan['id']}.json"
        journal = {"version": 1, "id": plan["id"], "root": str(directory),
                   "mode": plan["mode"], "created_at": plan["created_at"],
                   "status": "running", "items": [], "journal_path": str(journal_path)}
        if plan.get("deletion_storage") == "delete_files":
            journal["deletion_storage"] = "delete_files"
        if deletion_policy is not None:
            journal["deletion_destination"] = deletion_policy
        for item in plan["items"]:
            journal["items"].append({**item, "state": "pending", "error": None,
                                     "target_created": False})
        _write_journal(journal_path, journal, key, initial=True)
        for index, item in enumerate(journal["items"], 1):
            source, target = Path(item["source"]), Path(item["target"])
            try:
                if item.get("deletion_storage") == "delete_files":
                    _execute_direct_delete(item, directory, journal, journal_path, key)
                    item["state"] = "completed"
                else:
                    _source(source, directory)
                    _verify(source, item)
                    _duplicate_guard(item, directory)
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
                        _duplicate_guard(item, directory)
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
            _execution_event("execution.item", execution_id=journal["id"], action=journal["mode"],
                             status=item["state"], message="파일별 정리 결과입니다.",
                             source=str(source), target=str(target), error=item.get("error"),
                             completed=index, total=len(journal["items"]))
            _progress(progress_detail, "execute", index, len(journal["items"]), f"작업 처리: {source.name}")
        successes = sum(item["state"] == "completed" for item in journal["items"])
        journal["status"] = "completed" if successes == len(journal["items"]) else "partial" if successes else "failed"
        _write_journal(journal_path, journal, key)
        return _with_recovery_metadata(journal, recovery_metadata.write_compact(journal_path, directory, key))


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


def _read_restore_journal(path: Path, root: Path, key: bytes) -> tuple[dict, dict]:
    try:
        journal, info = recovery_metadata.read_preferred(path, root, key)
    except recovery_metadata.LegacyIntegrityError as exc:
        raise OperationError(str(exc)) from exc
    if journal is None:
        if not path.exists() and not path.is_symlink():
            raise OperationError("유효한 기존 복원 기록 또는 활성 압축 메타데이터가 없습니다. 보관된 원본 파일은 지우지 마세요.")
        try:
            journal = _read_journal(path, root, key)
        except FileNotFoundError as exc:
            raise OperationError("유효한 기존 복원 기록 또는 활성 압축 메타데이터가 없습니다. 보관된 원본 파일은 지우지 마세요.") from exc
        info["uncompressed_bytes"] = path.stat().st_size
    return journal, info


def _with_recovery_metadata(journal: dict, info: dict) -> dict:
    # Display-only transport information must not contaminate the signed data
    # used for subsequent checkpoints or be treated as authority to restore.
    result = {**journal, "recovery_metadata": info}
    if info.get("reason"):
        result["recovery_metadata_warning"] = info["reason"]
    return result


def _private_recovery_path(journal: dict, item: dict, field: str, root: Path) -> Path:
    path = _absolute(item[field])
    identifier = journal["id"]
    if field == "target":
        prefix = root / STATE_DIRECTORY / "quarantine" / identifier
        current_delete = item.get("deletion_storage") == "delete_files" and path.parent == root / DELETE_DIRECTORY
        legacy_delete = journal.get("mode") in {"quarantine", "duplicate_auto"} and _inside(path, prefix)
        if not current_delete and not legacy_delete:
            raise OperationError("보이는 대상 파일은 복원 데이터 최적화로 옮기거나 지우지 않습니다.")
    else:
        anchor = Path(item["source"] if field == "original_backup" else item["target"]).parent
        prefixes = (root / STATE_DIRECTORY / "retained" / identifier,
                    anchor / STATE_DIRECTORY / "retained" / identifier)
        if not any(_inside(path, prefix) for prefix in prefixes):
            raise OperationError("이 작업의 비공개 원본 보관본이 아닙니다.")
    _safe_path(path)
    return path


def _recovery_candidates(journal: dict, root: Path) -> tuple[list[dict], list[str]]:
    candidates, warnings, seen = [], [], set()
    for index, item in enumerate(journal["items"]):
        fields = [field for field in ("original_backup", "undo_backup") if item.get(field)]
        if (item.get("deletion_storage") == "delete_files" or journal.get("mode") in {"quarantine", "duplicate_auto"}) and item.get("target_created"):
            fields.append("target")
        for field in fields:
            try:
                original = _private_recovery_path(journal, item, field, root)
                path = original
                if not path.exists():
                    capture = item.get(field + "_compaction", {})
                    holding = capture.get("holding") if isinstance(capture, dict) else None
                    if not holding:
                        continue
                    path = _absolute(holding)
                    if path.parent != original.parent or not path.name.startswith(".compact-"):
                        raise OperationError("보관본 캡처 경로가 올바르지 않습니다.")
                    _safe_path(path, require=True)
                if str(path) in seen:
                    continue
                expected = _fingerprint(path)
                if expected["size"] != item["size"] or expected["sha256"] != item["sha256"]:
                    raise OperationError("원본 보관본의 내용이 바뀌어 최적화하지 않습니다.")
                seen.add(str(path))
                candidates.append({"item_index": index, "field": field, "path": str(path),
                                   "original_path": str(original), "fingerprint": expected})
            except (OSError, OperationError) as exc:
                warnings.append(f"보관본 유지: {item.get(field, field)} · {exc}")
    return candidates, warnings


def compaction_preview(root: str | Path, operation_id: str, *, progress_detail=None) -> dict:
    """Read-only, signed estimate for optimizing only private recovery copies."""
    directory = _root(root)
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise OperationError("유효하지 않은 작업 ID입니다.")
    state = directory / STATE_DIRECTORY
    _safe_path(state / "journal.key", require=True)
    key = _key(state)
    journal, _metadata_info = _read_restore_journal(state / "history" / f"{operation_id}.json", directory, key)
    _progress(progress_detail, "compact", 0, None, "비공개 원본 보관본을 확인합니다")
    candidates, warnings = _recovery_candidates(journal, directory)
    unique = {(candidate["fingerprint"]["sha256"], candidate["fingerprint"]["size"])
              for candidate in candidates}
    raw_bytes = sum(candidate["fingerprint"]["size"] for candidate in candidates)
    unique_bytes = sum(size for _digest, size in unique)
    plan = {"version": 1, "id": uuid.uuid4().hex, "operation_id": operation_id,
            "root": str(directory), "journal_signature": journal["signature"],
            "candidates": candidates, "candidate_count": len(candidates), "raw_bytes": raw_bytes,
            "unique_content_bytes": unique_bytes, "duplicate_bytes": raw_bytes - unique_bytes,
            "warnings": warnings, "metadata_only": False,
            "notice": "내용을 다시 검증한 비공개 보관본만 무손실 압축·중복 저장 제거합니다. 실제 절약량은 파일 내용·하드링크·파일시스템에 따라 달라집니다."}
    plan["signature"] = _sign(plan, _PLAN_KEY)
    _progress(progress_detail, "compact", len(candidates), len(candidates), "복원 데이터 최적화 미리보기 완료")
    return plan


def _remove_verified_private(path: Path, fingerprint: dict) -> int:
    """Retire an exclusively captured app-owned raw copy after verification."""
    _verify(path, fingerprint)
    info = path.lstat()
    with _directory_descriptor(path.parent) as parent:
        _safe_path(path, require=True)
        if parent is not None:
            os.unlink(path.name, dir_fd=parent)
        else:
            path.unlink()
    _sync_directory(path.parent)
    # Shared hardlinks do not free a second physical file when one name goes.
    return fingerprint["size"] if info.st_nlink == 1 else 0


def compact_operation(root: str | Path, operation_id: str, *, plan: dict | None = None,
                      progress_detail=None) -> dict:
    """Explicitly compact private recovery bytes; retain raw copies on failure.

    A signed descriptor and immutable verified chunks become durable before a
    private raw file is captured. Changed captures remain available; they are
    never silently removed or replaced with a live duplicate keeper.
    """
    from . import recovery_payload
    directory = _root(root)
    plan = compaction_preview(directory, operation_id, progress_detail=progress_detail) if plan is None else plan
    _check_signature(plan, _PLAN_KEY)
    if plan.get("root") != str(directory) or plan.get("operation_id") != operation_id or plan.get("version") != 1:
        raise OperationError("최적화 미리보기의 폴더·작업 번호가 일치하지 않습니다.")
    state = directory / STATE_DIRECTORY
    with _lock(state):
        key = _key(state)
        journal_path = state / "history" / f"{operation_id}.json"
        journal, _info = _read_restore_journal(journal_path, directory, key)
        if journal["signature"] != plan["journal_signature"]:
            raise OperationError("미리보기 이후 작업 기록이 바뀌었습니다. 다시 확인하세요.")
        if not journal_path.exists():
            recovery_metadata.materialize_checkpoint(journal_path, directory, key)
        summary = {"status": "running", "candidate_count": plan["candidate_count"], "processed": 0,
                   "skipped": 0, "raw_bytes": plan["raw_bytes"], "retired_raw_bytes": 0,
                   "new_object_bytes": 0, "saved_bytes": 0, "warnings": list(plan["warnings"])}
        journal["recovery_compaction"] = summary
        _write_journal(journal_path, journal, key)
        _execution_event("execution.started", execution_id=operation_id, action="compact", status="started",
                         message="비공개 복원 보관본 최적화를 시작합니다.", total=plan["candidate_count"])
        duplicates = {}
        for candidate in plan["candidates"]:
            fingerprint = candidate["fingerprint"]
            content = (fingerprint["sha256"], fingerprint["size"])
            duplicates[content] = duplicates.get(content, 0) + 1
        if plan["raw_bytes"] < 4096:
            summary.update(status="unchanged", skipped=plan["candidate_count"])
            summary["warnings"].append("4 KiB 미만의 작은 보관본은 처리 비용을 줄이기 위해 기존 방식으로 유지합니다.")
        else:
            credited_bytes = 0
            for number, candidate in enumerate(plan["candidates"], 1):
                item = journal["items"][candidate["item_index"]]
                field = candidate["field"]
                source = Path(candidate["path"])
                fingerprint = candidate["fingerprint"]
                holding = source.parent / (".compact-" + uuid.uuid4().hex + "-" + source.name[-80:])
                captured = False
                try:
                    original = _private_recovery_path(journal, item, field, directory)
                    if str(original) != candidate["original_path"] or source.parent != original.parent:
                        raise OperationError("최적화 대상의 비공개 경로가 바뀌었습니다.")
                    _verify(source, fingerprint)
                    content = (fingerprint["sha256"], fingerprint["size"])
                    def prepared(descriptor):
                        item[field + "_payload"] = descriptor
                        item[field + "_compaction"] = {"holding": str(holding), "state": "packing", "fingerprint": fingerprint}
                        _write_journal(journal_path, journal, key)
                    descriptor = recovery_payload.pack_file(directory, source, fingerprint,
                        progress_detail=progress_detail, min_savings_ratio=0.0 if duplicates[content] > 1 else .10,
                        on_prepared=prepared)
                    recovery_payload.verify_payload(directory, descriptor)
                    summary["new_object_bytes"] += descriptor["stats"]["new_bytes"]
                    item[field + "_payload"] = descriptor
                    item[field + "_compaction"] = {"holding": str(holding), "state": "prepared", "fingerprint": fingerprint}
                    _write_journal(journal_path, journal, key)  # durable verified recovery recipe
                    if holding.exists() or holding.is_symlink():
                        raise OperationError("캡처 경로 충돌이 있어 기존 보관본을 유지합니다.")
                    _rename_anchored(source, holding)
                    captured = True
                    _verify(holding, fingerprint)  # preserve unexpected post-capture changes
                    recovery_payload.verify_payload(directory, descriptor)
                    item[field + "_packed"] = True
                    item[field + "_compaction"]["state"] = "retire_pending"
                    _write_journal(journal_path, journal, key)
                    recovery_payload.verify_payload(directory, descriptor)
                    credited_bytes += _remove_verified_private(holding, fingerprint)
                    captured = False
                    item[field + "_compaction"]["state"] = "packed"
                    summary["processed"] += 1
                    summary["retired_raw_bytes"] += fingerprint["size"]
                    _execution_event("execution.item", execution_id=operation_id, action="compact", status="completed",
                                     message="검증한 보관본을 복원 객체로 최적화했습니다.", source=str(source),
                                     completed=number, total=plan["candidate_count"])
                except (OSError, OperationError, recovery_payload.PayloadError) as exc:
                    if captured:
                        _return_retained(holding, source)
                    item[field + "_packed"] = False
                    summary["skipped"] += 1
                    summary["warnings"].append(f"보관본 유지: {source} · {exc}")
                    _execution_event("execution.item", execution_id=operation_id, action="compact", status="unchanged",
                                     message="기존 보관본을 유지합니다.", source=str(source), error=str(exc),
                                     completed=number, total=plan["candidate_count"])
                _write_journal(journal_path, journal, key)
                _progress(progress_detail, "compact", number, plan["candidate_count"], "복원 보관본 최적화")
            summary["saved_bytes"] = max(0, credited_bytes - summary["new_object_bytes"])
            summary["status"] = "completed" if summary["processed"] and not summary["skipped"] else "partial" if summary["processed"] else "unchanged"
        _write_journal(journal_path, journal, key)
        result = _with_recovery_metadata(journal, recovery_metadata.write_compact(journal_path, directory, key))
        _execution_event("execution.finished", execution_id=operation_id, action="compact", status=summary["status"],
                         message="복원 데이터 최적화 결과입니다.", completed=summary["processed"], total=plan["candidate_count"])
        return result


def list_history(root: str | Path, *, progress_detail: Callable[[dict], None] | None = None) -> list[dict]:
    """List valid durable journals, raising on tampered records instead of trusting them."""
    directory = _root(root)
    state = directory / STATE_DIRECTORY
    _safe_path(state)
    history = state / "history"
    _safe_path(history)
    if not history.exists():
        return []
    key = _key(state)
    paths = list(history.glob("*.json"))
    known = {path.stem for path in paths}
    # Only final gzip recipes may survive an accidentally removed legacy JSON.
    # read_preferred still requires their authenticated active receipt.
    for compact in history.glob("*.restore.json.gz"):
        identifier = compact.name.removesuffix(".restore.json.gz")
        if re.fullmatch(r"[0-9a-f]{32}", identifier) and identifier not in known:
            paths.append(history / f"{identifier}.json")
            known.add(identifier)
    result = []
    _progress(progress_detail, "history", 0, len(paths), "작업 기록 확인")
    for index, path in enumerate(paths, 1):
        journal, info = _read_restore_journal(path, directory, key)
        result.append(_with_recovery_metadata(journal, info))
        _progress(progress_detail, "history", index, len(paths), "작업 기록 확인")
    return sorted(result, key=lambda item: item["created_at"], reverse=True)


def undo_operation(root: str | Path, operation_id: str, *,
                   progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Restore a journalled operation with separate, nonfatal execution receipts."""
    _execution_event("execution.started", execution_id=operation_id, action="undo",
                     status="started", message="작업 되돌리기를 시작합니다.")
    try:
        result = _undo_operation(root, operation_id, progress_detail=progress_detail)
    except Exception as exc:
        _execution_event("execution.failed", execution_id=operation_id, action="undo",
                         status="failed", message="작업 되돌리기가 중단되었습니다.", error=str(exc))
        raise
    _execution_event("execution.finished", execution_id=operation_id, action="undo",
                     status=result["status"], message="작업 되돌리기 처리 결과입니다.",
                     completed=sum(item["state"] in {"restored", "unchanged"} for item in result["items"]),
                     total=len(result["items"]))
    return result


def _undo_direct_delete(item: dict, root: Path, journal: dict, path: Path, key: bytes) -> None:
    if (not item.get("target_created") and not item.get("source_removed")
            and item.get("state") in {"failed", "pending"}):
        # A preflight failure or safely returned capture did not retire a file.
        # Its subsequently edited source is not part of this undo operation.
        item.update(state="unchanged", error=None)
        return
    source, target = _absolute(item["source"]), _absolute(item["target"])
    if (not _inside(source, root) or STATE_DIRECTORY in source.relative_to(root).parts
            or source.relative_to(root).parts[0].casefold() == DELETE_DIRECTORY.casefold()
            or target.parent != root / DELETE_DIRECTORY):
        raise OperationError("삭제 보관본의 원본·대상 경로가 올바르지 않습니다.")
    _safe_path(source)
    _safe_path(target)
    if item.get("target_packed") is True:
        _private_recovery_path(journal, item, "target", root)
        if target.exists() or target.is_symlink():
            raise OperationError("최적화 이후 delete_files의 이전 위치에 파일이 생겼습니다. 보관본을 유지합니다.")
        if source.exists() or source.is_symlink():
            expected_restore = item.get("restore_fingerprint")
            if item.get("restore_state") != "moving_back_packed" or not isinstance(expected_restore, dict):
                raise OperationError("원래 위치에 파일이 있어 압축 보관본을 덮어쓰지 않습니다.")
            _verify(source, expected_restore)
        else:
            materialized, fingerprint = _materialize_recovery(journal, item, "target", root, path, key)
            _mkdir(source.parent)
            item.update(restore_state="moving_back_packed", restore_fingerprint=fingerprint)
            _write_journal(path, journal, key)
            _move_no_replace(materialized, source)
            _verify(source, fingerprint)
            item["recovery_materialized"]["target"]["state"] = "restored_to_source"
        item.update(state="restored", error=None, target_created=False, source_removed=False, restore_state="restored")
        return
    if not target.exists():
        if source.exists():
            _verify(source, item)
            item.update(state="restored" if item.get("source_removed") or item.get("restore_state") else "unchanged",
                        target_created=False, source_removed=False, error=None)
            return
        raise OperationError("delete_files 보관본과 원래 파일이 없습니다. 기존 기록을 유지합니다.")
    _verify(target, item)
    if source.exists() or source.is_symlink():
        raise OperationError("원래 위치에 파일이 있어 덮어쓰지 않습니다. delete_files 보관본을 유지합니다.")
    else:
        _mkdir(source.parent)
        item["restore_state"] = "moving_back"
        _write_journal(path, journal, key)
        _move_no_replace(target, source)
        item.update(target_created=False, source_removed=False)
        _verify(source, item)
    item.update(state="restored", error=None, target_created=False, source_removed=False,
                restore_state="restored", restore_fingerprint=_fingerprint(source))


def _materialize_recovery(journal: dict, item: dict, field: str, root: Path,
                          path: Path, key: bytes) -> tuple[Path, dict]:
    from . import recovery_payload
    descriptor = item.get(field + "_payload")
    if not isinstance(descriptor, dict):
        raise OperationError("복원할 압축 데이터의 서명된 참조가 없습니다.")
    _private_recovery_path(journal, item, field, root)
    materialized = item.setdefault("recovery_materialized", {})
    previous = materialized.get(field)
    prefix = root / STATE_DIRECTORY / "materialized" / journal["id"]
    if isinstance(previous, dict):
        old_path = _absolute(previous.get("path", ""))
        if _inside(old_path, prefix) and old_path.exists():
            try:
                fingerprint = _verify(old_path, previous["fingerprint"])
                if fingerprint["size"] == item["size"] and fingerprint["sha256"] == item["sha256"]:
                    return old_path, fingerprint
            except (OSError, OperationError, KeyError):
                # Keep unexpected edited bytes; create a different owned file.
                pass
    _mkdir(prefix)
    target = prefix / f"{uuid.uuid4().hex}-{field}"
    materialized[field] = {"path": str(target), "state": "creating"}
    _write_journal(path, journal, key)
    try:
        fingerprint = recovery_payload.materialize(root, descriptor, target)
    except recovery_payload.PayloadError as exc:
        raise OperationError(str(exc)) from exc
    if fingerprint["size"] != item["size"] or fingerprint["sha256"] != item["sha256"]:
        raise OperationError("복원한 데이터의 내용이 작업 기록과 다릅니다. 만든 파일과 보관 데이터를 유지합니다.")
    materialized[field].update(state="ready", fingerprint=fingerprint)
    _write_journal(path, journal, key)
    return target, fingerprint


def _cleanup_recovery_materialized(journal: dict, root: Path) -> None:
    prefix = root / STATE_DIRECTORY / "materialized" / journal["id"]
    for item in journal["items"]:
        if item.get("state") != "restored":
            continue
        for saved in item.get("recovery_materialized", {}).values():
            if not isinstance(saved, dict) or saved.get("state") != "ready":
                continue
            target = _absolute(saved["path"])
            if not _inside(target, prefix):
                continue
            try:
                _remove_verified_private(target, saved["fingerprint"])
            except (OSError, OperationError) as exc:
                saved["warning"] = "변경되었거나 확인할 수 없는 임시 복원본을 유지합니다: " + str(exc)[:300]
            else:
                saved["state"] = "retired"


def _undo_operation(root: str | Path, operation_id: str, *,
                   progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Restore moved files or remove unchanged copies; conflicts remain recoverable."""
    directory = _root(root)
    if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        raise OperationError("유효하지 않은 작업 ID입니다.")
    state = directory / STATE_DIRECTORY
    _safe_path(state, require=True)
    with _lock(state):
        key = _key(state)
        journal_path = state / "history" / f"{operation_id}.json"
        journal, _metadata_info = _read_restore_journal(journal_path, directory, key)
        if not journal_path.exists():
            # Recreate the exact authenticated baseline first. A crash here
            # keeps an active matching recipe; the next checkpoint durably
            # invalidates it before any actual source/target changes.
            try:
                recovery_metadata.materialize_checkpoint(journal_path, directory, key)
            except recovery_metadata.MetadataError as exc:
                raise OperationError(str(exc)) from exc
        _write_journal(journal_path, journal, key)
        total = len(journal["items"])
        _progress(progress_detail, "undo", 0, total, "복원 작업 시작")
        for index, item in enumerate(reversed(journal["items"]), 1):
            _progress(progress_detail, "undo", index - 1, total, f"복원 확인: {Path(item['source']).name}")
            if item["state"] in {"restored", "unchanged"}:
                _execution_event("execution.item", execution_id=journal["id"], action="undo",
                                 status="unchanged", message="이미 되돌렸거나 변경할 내용이 없습니다.",
                                 source=item["source"], target=item["target"], completed=index, total=total)
                continue
            if item.get("deletion_storage") == "delete_files":
                try:
                    _undo_direct_delete(item, directory, journal, journal_path, key)
                except (OSError, OperationError) as exc:
                    item["state"], item["error"] = "undo_failed", str(exc)
                _write_journal(journal_path, journal, key)
                _execution_event("execution.item", execution_id=journal["id"], action="undo",
                                 status=item["state"], message="delete_files 보관본 복원 결과입니다.",
                                 source=item["source"], target=item["target"], error=item.get("error"),
                                 completed=index, total=total)
                continue
            if not item.get("target_created"):
                item["state"] = "unchanged"
                _execution_event("execution.item", execution_id=journal["id"], action="undo",
                                 status="unchanged", message="이전 작업에서 바뀌지 않은 파일입니다.",
                                 source=item["source"], target=item["target"], completed=index, total=total)
                continue
            source, target = Path(item["source"]), Path(item["target"])
            try:
                if not _inside(source, directory) or STATE_DIRECTORY in source.relative_to(directory).parts:
                    raise OperationError("복원 원본 경로가 선택 폴더 밖입니다.")
                _safe_path(source)
                packed_target = item.get("target_packed") is True
                if packed_target:
                    _private_recovery_path(journal, item, "target", directory)
                    if target.exists() or target.is_symlink():
                        raise OperationError("압축 최적화 이후 기존 보관 경로에 파일이 생겼습니다. 덮어쓰거나 이동하지 않습니다.")
                    target, target_fingerprint = _materialize_recovery(journal, item, "target", directory, journal_path, key)
                else:
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
                    if packed_target:
                        recovery, recovery_fingerprint = target, target_fingerprint
                    else:
                        recovery = Path(item.get("original_backup", str(target)))
                        if not recovery.exists() and isinstance(item.get("original_backup_payload"), dict):
                            recovery, recovery_fingerprint = _materialize_recovery(journal, item, "original_backup", directory, journal_path, key)
                        else:
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
                if packed_target:
                    # The independent immutable payload remains the undo
                    # backup. Do not create a second uncompressed private copy.
                    item["undo_backup_payload"] = item["target_payload"]
                else:
                    _retain_verified(target, target_fingerprint, directory, journal["id"], item,
                                     "undo_backup", lambda: _write_journal(journal_path, journal, key))
                item["state"] = "restored"
                item["error"] = None
            except (OSError, OperationError) as exc:
                item["state"] = "undo_failed"
                item["error"] = str(exc)
            _write_journal(journal_path, journal, key)
            _execution_event("execution.item", execution_id=journal["id"], action="undo",
                             status=item["state"], message="파일별 되돌리기 결과입니다.",
                             source=str(source), target=str(target), error=item.get("error"),
                             completed=index, total=total)
        journal["status"] = "undone" if all(item["state"] in {"restored", "unchanged"} for item in journal["items"]) else "partial_undo"
        _write_journal(journal_path, journal, key)
        _cleanup_recovery_materialized(journal, directory)
        _write_journal(journal_path, journal, key)
        _progress(progress_detail, "undo", total, total, "복원 처리 완료")
        return _with_recovery_metadata(journal, recovery_metadata.write_compact(journal_path, directory, key))
