"""Read-only selection assistance for already verified exact duplicate groups.

Choosing a copy is a proposed checkbox selection, never a file operation. The
organizer still requires its ordinary preview and confirmation before a change.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import os
from pathlib import Path
import re
from threading import Event
from typing import Callable

from .matching import _open_snapshot
from .models import FileEntry, MatchGroup

RULES = frozenset({"first", "newest", "oldest", "shortest_path", "preferred_folder"})


def _cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ValueError("보존 규칙 선택을 중단했습니다.")


def _handle_state(info: os.stat_result) -> tuple[int, ...]:
    # Always compare fstat to fstat. On Windows Path.stat().st_ctime may expose
    # creation time, while the held handle exposes the actual change time.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _verified_digest(entry: FileEntry, expected: str, cancel: Event | None) -> tuple[int, ...]:
    digest = hashlib.sha256()
    with _open_snapshot(entry) as stream:
        before = _handle_state(os.fstat(stream.fileno()))
        while True:
            _cancel(cancel)
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        verified_state = _handle_state(os.fstat(stream.fileno()))
        if before != verified_state:
            raise OSError("보존 후보의 해시를 읽는 동안 파일이 변경되었습니다.")
    if digest.hexdigest() != expected:
        raise OSError("중복 검증 이후 파일 내용이 변경되었습니다. 중복 검사를 다시 실행하세요.")
    return verified_state


def _check_verified_state(entry: FileEntry, expected: tuple[int, ...]) -> None:
    with _open_snapshot(entry) as stream:
        if _handle_state(os.fstat(stream.fileno())) != expected:
            raise OSError("보존 후보를 검증하는 동안 파일이 변경되었습니다. 중복 검사를 다시 실행하세요.")


def _path_key(value: str) -> str:
    """Lexically normalize paths without following user-controlled links."""
    return os.path.normcase(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _inside(value: str, folder: str) -> bool:
    path, root = _path_key(value), _path_key(folder)
    try:
        return path != root and os.path.commonpath((path, root)) == root
    except ValueError:  # Different Windows drives cannot contain each other.
        return False


def _tie(entry: FileEntry) -> tuple[str, str]:
    path = _path_key(entry.path)
    return path.casefold(), path


def _rank(entry: FileEntry, rule: str, preferred_folder: str | None) -> tuple:
    tie = _tie(entry)
    if rule == "newest":
        return (-entry.mtime_ns, *tie)
    if rule == "oldest":
        return (entry.mtime_ns, *tie)
    if rule == "shortest_path":
        return (len(entry.relative_path), *tie)
    if rule == "preferred_folder":
        return (not _inside(entry.path, preferred_folder), *tie)
    return tie


def choose_duplicate_copies(
    groups: list[MatchGroup],
    files: list[FileEntry],
    rule: str = "first",
    preferred_folder: str | None = None,
    protected_folders: list[str] | None = None,
    *,
    cancel: Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Propose copies to select while preserving at least one file per group.

    Only ``duplicate`` groups are eligible. Legacy groups with empty evidence
    came from the earlier byte-verifying finder and remain supported; nonempty
    evidence must explicitly confirm ``byte_identical=True``. All members are
    checked against their scanned snapshot. A stale or ambiguous group is
    skipped in full, rather than selecting around an unavailable original.

    Every protected-folder member is retained. When protected members exist,
    the rule picks the explanatory keeper among those retained files; it does
    not unnecessarily retain another unprotected copy. Overlapping eligible
    groups are all skipped because independent keep rules could conflict.
    """
    _cancel(cancel)
    if rule not in RULES:
        raise ValueError("지원하지 않는 보존 기준입니다.")
    if rule == "preferred_folder" and not preferred_folder:
        raise ValueError("우선 보존할 폴더를 선택하세요.")
    if preferred_folder is not None:
        preferred_folder = _path_key(preferred_folder)
    protected = tuple(dict.fromkeys(_path_key(path) for path in (protected_folders or [])))
    entries: dict[str, FileEntry] = {}
    conflicting_entries: set[str] = set()
    for entry in files:
        key = _path_key(entry.path)
        if key in entries and entries[key] != entry:
            conflicting_entries.add(key)
        entries[key] = entry

    result = {"selected": [], "kept": [], "decisions": [], "warnings": []}
    eligible: list[tuple[int, MatchGroup, list[str]]] = []
    candidate_keys: set[str] = set()

    def skip(index: int, group: MatchGroup, message: str) -> None:
        result["warnings"].append(f"그룹 {index}: {message}")
        result["decisions"].append({
            "index": index, "rule": rule, "status": "skipped",
            "paths": list(group.paths), "keeper": None,
            "kept": [entries[key].path for key in dict.fromkeys(_path_key(p) for p in group.paths) if key in entries],
            "selected": [], "reason": message,
        })

    for index, group in enumerate(groups, 1):
        _cancel(cancel)
        if group.kind != "duplicate":
            skip(index, group, "유사 후보에는 자동 보존·선택 기준을 적용하지 않습니다.")
            continue
        keys = list(dict.fromkeys(_path_key(path) for path in group.paths))
        candidate_keys.update(key for key in keys if key in entries)
        evidence = getattr(group, "evidence", {}) or {}
        if evidence and evidence.get("byte_identical") is not True:
            skip(index, group, "실제 바이트 일치가 확인되지 않은 그룹입니다.")
            continue
        if "sha256" in evidence and (not isinstance(evidence["sha256"], str)
                                     or not re.fullmatch(r"[0-9a-fA-F]{64}", evidence["sha256"])):
            skip(index, group, "중복 검증 해시 근거가 손상되어 그룹 전체를 건너뜁니다.")
            continue
        if len(keys) < 2:
            skip(index, group, "서로 다른 파일이 두 개 이상 필요합니다.")
            continue
        if any(key not in entries or key in conflicting_entries for key in keys):
            skip(index, group, "분석 목록에 없거나 일관되지 않은 파일 정보가 있습니다. 다시 분석하세요.")
            continue
        eligible.append((index, group, keys))

    occurrences = Counter(key for _, _, keys in eligible for key in keys)
    selected_keys: set[str] = set()
    verified_states: dict[str, tuple[int, ...]] = {}
    for index, group, keys in eligible:
        _cancel(cancel)
        if any(occurrences[key] > 1 for key in keys):
            skip(index, group, "다른 중복 그룹과 파일이 겹쳐 보존 기준이 충돌할 수 있습니다. 다시 검사하세요.")
            continue
        members = [entries[key] for key in keys]
        expected_digest = (getattr(group, "evidence", {}) or {}).get("sha256")
        try:
            for entry in members:
                _cancel(cancel)
                if progress:
                    progress(f"보존 후보 재검증: {entry.relative_path}")
                if expected_digest:
                    verified_states[_path_key(entry.path)] = _verified_digest(entry, expected_digest.lower(), cancel)
                else:
                    with _open_snapshot(entry) as stream:
                        verified_states[_path_key(entry.path)] = _handle_state(os.fstat(stream.fileno()))
            # An earlier member can change while a later (large) file hashes.
            # Reject the whole group before proposing any checkbox selection.
            for entry in members:
                _cancel(cancel)
                _check_verified_state(entry, verified_states[_path_key(entry.path)])
        except (OSError, ValueError) as exc:
            _cancel(cancel)
            skip(index, group, f"파일이 변경되었거나 읽을 수 없어 그룹 전체를 건너뜁니다: {exc}")
            continue

        protected_members = [entry for entry in members if any(_inside(entry.path, folder) for folder in protected)]
        keeper = min(protected_members or members, key=lambda entry: _rank(entry, rule, preferred_folder))
        kept_keys = {_path_key(entry.path) for entry in protected_members} | {_path_key(keeper.path)}
        copies = [entry.path for entry in members if _path_key(entry.path) not in kept_keys]
        selected_keys.update(_path_key(path) for path in copies)
        reason = "중복 그룹에서 보존 기준에 따라 한 파일을 남깁니다."
        if protected_members:
            reason = f"보호 폴더의 파일 {len(protected_members)}개를 모두 보존합니다."
        if rule == "preferred_folder" and not any(_inside(entry.path, preferred_folder) for entry in members):
            message = f"그룹 {index}: 우선 보존 폴더에 사본이 없어 경로 순서로 보존 파일을 정했습니다."
            result["warnings"].append(message)
            reason += " 우선 보존 폴더에 사본이 없어 경로 순서를 적용했습니다."
        result["decisions"].append({
            "index": index, "rule": rule, "status": "ready",
            "paths": [entry.path for entry in members], "keeper": keeper.path,
            "kept": [entry.path for entry in members if _path_key(entry.path) in kept_keys],
            "selected": copies, "reason": reason,
        })

    # Other groups may contain large files. Revalidate all ready groups after
    # that work too, so edits in an earlier group never survive as old choices.
    for decision in result["decisions"]:
        _cancel(cancel)
        if decision["status"] != "ready":
            continue
        try:
            for path in decision["paths"]:
                _cancel(cancel)
                _check_verified_state(entries[_path_key(path)], verified_states[_path_key(path)])
        except (OSError, ValueError) as exc:
            _cancel(cancel)
            message = f"다른 그룹 검증 중 파일이 변경되었거나 읽을 수 없어 그룹 전체를 건너뜁니다: {exc}"
            result["warnings"].append(f"그룹 {decision['index']}: {message}")
            selected_keys.difference_update(_path_key(path) for path in decision["paths"])
            decision.update(status="skipped", keeper=None, kept=list(decision["paths"]), selected=[], reason=message)
    result["selected"] = sorted((entries[key].path for key in selected_keys), key=_path_key)
    result["kept"] = sorted((entries[key].path for key in candidate_keys - selected_keys), key=_path_key)
    result["decisions"].sort(key=lambda decision: decision["index"])
    _cancel(cancel)
    return result
