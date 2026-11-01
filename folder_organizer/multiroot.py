"""Read-only inventory and exact matching across independently selected roots.

Root journals and destructive plans deliberately remain outside this module.
Results contain no live duplicate proof and cannot authorize a file operation.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import stat
from threading import Event, Lock
import time
from typing import Callable, Iterable

from . import __version__
from .matching import find_duplicates
from .models import FileEntry, ScanResult
from .scanner import normalize_exclude_patterns, scan_folder

MAX_ROOTS = 64
MAX_WORKERS = 4


@dataclass
class RootResult:
    root_id: str
    requested_path: str
    path: str | None = None
    state: str = "waiting"
    volume_group: str = "unknown"
    scan: ScanResult | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0
    scan_options: dict = field(default_factory=dict)

    @property
    def file_count(self) -> int:
        return len(self.scan.files) if self.scan is not None else 0

    @property
    def total_size(self) -> int:
        return self.scan.total_size if self.scan is not None else 0


@dataclass(frozen=True)
class ReadOnlyDuplicateGroup:
    paths: tuple[str, ...]
    size: int
    sha256: str
    root_ids: tuple[str, ...]
    action_authority: bool = field(default=False, init=False)


@dataclass
class MultiScanResult:
    roots: list[RootResult] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    memberships: dict[str, list[str]] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    groups: list[ReadOnlyDuplicateGroup] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    duplicate_inputs: list[dict] = field(default_factory=list)
    duplicate_analysis: dict = field(default_factory=dict)
    cancelled: bool = False
    complete: bool = False
    elapsed_seconds: float = 0.0
    scan_seconds: float = 0.0
    duplicate_seconds: float = 0.0
    max_workers: int = 1
    action_authority: bool = field(default=False, init=False)

    @property
    def inventory_complete(self) -> bool:
        # Cancelling the later exact-analysis phase does not undo a finished
        # inventory. The overall result still remains cancelled/incomplete.
        return bool(self.roots) and all(root.state == "complete" for root in self.roots)


def _path_key(path: str | Path) -> str:
    # The scanner already supplies absolute, canonical-root paths without
    # following child links. Avoid an extra resolve/stat for every file.
    # Preserve case: Windows directories can explicitly enable case-sensitive
    # names. Actual root aliases are collapsed by their physical identity.
    return os.path.normpath(str(path))


def _volume_group(path: Path, info: os.stat_result) -> str:
    value = str(path).replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    if value.startswith("\\\\") and not value.startswith("\\\\?\\"):
        server = value[2:].split("\\", 1)[0].casefold()
        return "server:" + server if server else "unknown"
    # Volume identity is a conservative scheduling hint, not a claim that
    # different partitions necessarily correspond to different physical disks.
    if os.name == "nt":
        return "unknown-local"
    return f"volume:{info.st_dev}" if info.st_dev else "unknown"


def scan_folders(
    roots: Iterable[str | Path], *, recursive: bool = True,
    include_hidden: bool = False, exclude_patterns: tuple[str, ...] | list[str] = (),
    min_size: int = 0, max_size: int | None = None, workers: int = 2,
    duplicates: bool = True, cancel: Event | None = None,
    progress_detail: Callable[[dict], None] | None = None,
) -> MultiScanResult:
    """Inventory selected folders and optionally compare their exact contents.

    Same-volume and same-server roots are scanned sequentially, with at most
    four independent volume groups active. Exact matching is a single serial
    pass over unique paths. Overlapping roots retain their own relative filter
    semantics; shared files are inventoried once in the global result and
    preserve every observed root membership. No source file is modified.
    """
    if isinstance(roots, (str, bytes, Path)):
        raise ValueError("폴더 경로 목록을 전달하세요.")
    requested = list(roots)
    if not 1 <= len(requested) <= MAX_ROOTS:
        raise ValueError(f"폴더는 1개부터 {MAX_ROOTS}개까지 선택하세요.")
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"동시 탐색 수는 1부터 {MAX_WORKERS}까지 설정하세요.")
    if type(duplicates) is not bool or type(recursive) is not bool or type(include_hidden) is not bool:
        raise ValueError("검사 옵션은 참 또는 거짓이어야 합니다.")
    patterns = normalize_exclude_patterns(exclude_patterns)
    if type(min_size) is not int or min_size < 0 or (max_size is not None and (type(max_size) is not int or max_size < min_size)):
        raise ValueError("파일 크기 범위가 올바르지 않습니다.")
    cancel = cancel if cancel is not None else Event()
    started = time.monotonic()
    result = MultiScanResult(max_workers=workers)
    by_canonical: dict[str, RootResult] = {}
    by_identity: dict[tuple[int, int], RootResult] = {}
    queues: OrderedDict[str, list[RootResult]] = OrderedDict()
    callback_lock = Lock()
    last_report: dict[str, float] = {}

    def emit(root: RootResult | None, event: dict, *, terminal: bool = False) -> None:
        if progress_detail is None:
            return
        key = root.root_id if root is not None else "global"
        now = time.monotonic()
        with callback_lock:
            if not terminal and now - last_report.get(key, 0.0) < .07:
                return
            last_report[key] = now
            progress_detail({**event, "root_id": root.root_id if root else None,
                             "requested_path": root.requested_path if root else None,
                             "root_path": root.path if root else None,
                             "state": root.state if root else "duplicates",
                             "terminal": terminal or bool(event.get("terminal"))})

    requested_options = {"recursive": recursive, "include_hidden": include_hidden,
                         "exclude_patterns": list(patterns), "min_size": min_size, "max_size": max_size}
    for raw in requested:
        root = RootResult(f"root-{len(result.roots) + 1:04d}", str(raw), scan_options=dict(requested_options))
        try:
            if not isinstance(raw, (str, Path)) or not str(raw).strip() or "\0" in str(raw):
                raise ValueError("유효한 폴더 경로가 아닙니다.")
            path = Path(os.path.abspath(os.path.expanduser(str(raw))))
            initial = path.lstat()
            if stat.S_ISLNK(initial.st_mode) or (getattr(initial, "st_file_attributes", 0) & 0x400):
                raise ValueError("링크 또는 재분석 지점을 루트로 선택할 수 없습니다.")
            path = path.resolve(strict=True)
            info = path.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("선택한 경로가 폴더가 아닙니다.")
            key = _path_key(path)
            identity = (info.st_dev, info.st_ino)
            previous = by_canonical.get(key) or (by_identity.get(identity) if info.st_ino else None)
            if previous is not None:
                result.duplicate_inputs.append({"requested_path": str(raw), "root_id": previous.root_id})
                continue
            root.path = str(path)
            root.volume_group = _volume_group(path, info)
            by_canonical[key] = root
            if info.st_ino:
                by_identity[identity] = root
            queues.setdefault(root.volume_group, []).append(root)
        except (OSError, ValueError, RuntimeError) as exc:
            root.state, root.error = "failed", str(exc)
        result.roots.append(root)

    valid = [root for root in result.roots if root.path is not None]
    for index, left in enumerate(valid):
        for right in valid[index + 1:]:
            a, b = Path(left.path), Path(right.path)
            if a.is_relative_to(b) or b.is_relative_to(a):
                result.notes.append(f"{left.root_id} / {right.root_id}: 경로가 겹칩니다. 루트별 범위를 확인하고 전역 목록·내용 검사는 중복 경로를 한 번만 반영합니다.")
    if result.duplicate_inputs:
        result.notes.append(f"같은 폴더 입력 {len(result.duplicate_inputs)}개를 기존 루트에 연결했습니다.")

    def scan_queue(group: list[RootResult]) -> None:
        for root in group:
            if cancel.is_set():
                root.state = "cancelled"
                emit(root, {"phase": "scan", "completed": 0, "message": "취소됨"}, terminal=True)
                continue
            root.state = "scanning"
            emit(root, {"phase": "scan", "completed": 0, "message": "폴더를 탐색합니다."})
            root_started = time.monotonic()
            try:
                root.scan = scan_folder(root.path, recursive, include_hidden, cancel=cancel,
                    exclude_patterns=patterns, min_size=min_size, max_size=max_size,
                    sort_results=False, progress_detail=lambda event, current=root: emit(current, event))
                root.state = "cancelled" if root.scan.cancelled else "partial" if root.scan.warnings else "complete"
            except (OSError, ValueError, RuntimeError) as exc:
                root.state, root.error = "failed", str(exc)
            finally:
                root.elapsed_seconds = time.monotonic() - root_started
                emit(root, {"phase": "scan", "completed": root.file_count,
                            "message": root.error or root.state}, terminal=True)

    scan_started = time.monotonic()
    if len(queues) == 1 or workers == 1:
        for queue in queues.values():
            scan_queue(queue)
    elif queues:
        with ThreadPoolExecutor(max_workers=min(workers, len(queues)), thread_name_prefix="folder-inventory") as executor:
            futures = [executor.submit(scan_queue, queue) for queue in queues.values()]
            for future in futures:
                future.result()
    result.scan_seconds = time.monotonic() - scan_started

    unique: dict[str, FileEntry] = {}
    rejected_paths: set[str] = set()
    roots_by_id = {root.root_id: root for root in result.roots}
    for root in result.roots:
        for entry in root.scan.files if root.scan is not None else ():
            key = _path_key(entry.path)
            if key in rejected_paths:
                continue
            previous = unique.get(key)
            if previous is not None and any(getattr(previous, name) != getattr(entry, name)
                    for name in ("size", "mtime_ns", "device", "inode", "created_ns")):
                message = f"겹친 폴더를 조회하는 동안 파일이 변경되어 통합 목록에서 제외했습니다: {entry.path}"
                involved = [*result.memberships.pop(previous.path), root.root_id]
                for root_id in involved:
                    affected = roots_by_id[root_id]
                    affected.state = "partial"
                    if affected.scan is not None:
                        affected.scan.warnings.append(message)
                result.owners.pop(previous.path, None)
                unique.pop(key)
                rejected_paths.add(key)
                continue
            selected = unique.setdefault(key, entry)
            result.memberships.setdefault(selected.path, []).append(root.root_id)
            result.owners.setdefault(selected.path, root.root_id)
    result.files = list(unique.values())
    result.cancelled = cancel.is_set() or any(root.state == "cancelled" for root in result.roots)
    result.duplicate_analysis = {"requested": duplicates, "complete": None, "cancelled": False,
                                 "warnings": [], "metrics": {}}
    if duplicates and not result.cancelled:
        duplicate_started = time.monotonic()
        analysis: dict = {}
        groups = find_duplicates(result.files, cancel=cancel, report=analysis,
            progress_detail=lambda event: emit(None, event, terminal=bool(event.get("terminal"))))
        result.duplicate_seconds = time.monotonic() - duplicate_started
        result.duplicate_analysis = {"requested": True, **analysis}
        result.cancelled = result.cancelled or bool(analysis.get("cancelled"))
        by_path = {entry.path: entry for entry in result.files}
        # Copy only public values into a different type, deliberately dropping
        # process-local MatchGroup proofs before returning multi-root results.
        result.groups = [ReadOnlyDuplicateGroup(tuple(sorted(group.paths)), by_path[group.paths[0]].size,
            group.evidence["sha256"], tuple(sorted({root_id for path in group.paths
                                                   for root_id in result.memberships[path]})))
            for group in groups]
        result.groups.sort(key=lambda group: group.paths[0])
        if not result.inventory_complete:
            result.duplicate_analysis["complete"] = False
            for root in result.roots:
                messages = ([root.error] if root.error else []) + (root.scan.warnings if root.scan else [])
                for message in messages:
                    result.duplicate_analysis["warning_count"] = result.duplicate_analysis.get("warning_count", 0) + 1
                    if len(result.duplicate_analysis["warnings"]) < 1000:
                        result.duplicate_analysis["warnings"].append({"path": root.path or root.requested_path,
                            "phase": "inventory", "message": message})
                    else:
                        result.duplicate_analysis["warnings_truncated"] = True
    elif duplicates:
        result.duplicate_analysis.update(complete=False, cancelled=True)
    result.cancelled = result.cancelled or cancel.is_set()
    result.complete = not result.cancelled and result.inventory_complete and (not duplicates or result.duplicate_analysis.get("complete") is True)
    result.elapsed_seconds = time.monotonic() - started
    return result


def build_multiroot_report(result: MultiScanResult) -> dict:
    """Return the additive, explicitly non-authoritative multi-root schema."""
    roots = [{"root_id": root.root_id, "requested_path": root.requested_path, "path": root.path,
              "state": root.state, "volume_group": root.volume_group,
              "file_count": root.file_count, "bytes": root.total_size,
              "warnings": list(root.scan.warnings) if root.scan else [], "error": root.error,
              "elapsed_seconds": root.elapsed_seconds,
              "scan_options": dict(root.scan_options)}
             for root in result.roots]
    paths = {root.root_id: Path(root.path) for root in result.roots if root.path}
    files = [{"path": entry.path, "root_ids": result.memberships[entry.path],
              "owner_root_id": result.owners[entry.path],
              "relative_paths": {root_id: Path(entry.path).relative_to(paths[root_id]).as_posix()
                                 for root_id in result.memberships[entry.path]},
              "size": entry.size, "kind": entry.kind, "extension": entry.suffix,
              "modified_ns": str(entry.mtime_ns),
              "created_ns": str(entry.created_ns) if entry.created_ns is not None else None}
             for entry in result.files]
    return {"schema": "folder-organizer.multiroot.v1", "app_version": __version__,
            "action_authority": False, "complete": result.complete,
            "inventory_complete": result.inventory_complete, "cancelled": result.cancelled,
            "roots": roots, "files": files, "notes": list(result.notes),
            "duplicate_inputs": list(result.duplicate_inputs),
            "duplicates": {**result.duplicate_analysis, "groups": [
                {"paths": list(group.paths), "size": group.size, "sha256": group.sha256,
                 "root_ids": list(group.root_ids), "group_source_count": len(group.root_ids),
                 "action_authority": False} for group in result.groups]},
            "timings": {"elapsed_seconds": result.elapsed_seconds, "scan_seconds": result.scan_seconds,
                        "duplicate_seconds": result.duplicate_seconds},
            "scheduling": {"max_workers": result.max_workers,
                           "policy": "same-volume/server sequential; independent groups bounded; exact hashing serial"}}


def write_multiroot_report(result: MultiScanResult, destination: str | Path) -> None:
    """Create a new report; never replace a source or an existing report."""
    target = Path(destination)
    # The entire selected scope is read-only, including unindexed new paths.
    resolved = target.resolve(strict=False)
    if any(root.path and resolved.is_relative_to(Path(root.path)) for root in result.roots):
        raise ValueError("보고서는 분석한 폴더 밖의 새 경로에 저장하세요.")
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(build_multiroot_report(result), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
