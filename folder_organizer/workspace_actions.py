"""Live single/multi-folder actions with real-root journals and shared guards.

Serialized inventory reports remain read-only. Only a session issued by a fresh
in-process workspace analysis can preview or execute workspace operations.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import datetime as dt
import hmac
from pathlib import Path
import uuid
from threading import Lock

from . import operations as ops
from .matching import _current_snapshot, _seal_duplicate_proof, validate_duplicate_proof
from .models import MatchGroup, ScanResult
from .multiroot import MultiScanResult, scan_folders
from .rules import build_classification_plan

_ISSUER = object()


class WorkspaceSession:
    __slots__ = ("_result", "_files", "_groups", "_roots", "_owners", "_id", "_signature", "_consumed", "_complete", "_execution_lock")

    def __init__(self, result, groups, *, _issuer=None):
        if _issuer is not _ISSUER or not isinstance(result, MultiScanResult):
            raise ValueError("현재 앱에서 분석한 폴더 결과가 필요합니다.")
        self._result = result
        self._files = tuple(result.files)
        self._groups = tuple(groups)
        self._roots = tuple(root.path for root in result.roots if root.path is not None)
        root_paths = {root.root_id: root.path for root in result.roots if root.path is not None}
        self._owners = tuple((entry.path, root_paths[result.owners[entry.path]]) for entry in self._files)
        self._id = uuid.uuid4().hex
        self._consumed = False
        self._complete = result.complete and not result.cancelled
        self._execution_lock = Lock()
        self._signature = ops._sign(self._payload(), ops._PLAN_KEY)

    @property
    def result(self):
        return self._result

    @property
    def files(self):
        return self._files

    @property
    def groups(self):
        return self._groups

    @property
    def roots(self):
        return self._roots

    @property
    def consumed(self):
        return self._consumed

    @property
    def actionable(self):
        return self._complete and not self._consumed

    def invalidate(self):
        self._consumed = True

    def _payload(self):
        return {"id": self._id, "roots": self._roots, "owners": self._owners,
                "complete": self._complete, "groups": [group.to_dict() for group in self._groups],
                "files": [entry.to_dict() for entry in self._files]}


def analyze_workspace(roots, **options) -> WorkspaceSession:
    """Analyze once and retain private exact proofs without exporting them."""
    if "_live_groups" in options:
        raise ValueError("내부 검사 근거 옵션을 지정할 수 없습니다.")
    groups = []
    result = scan_folders(roots, **options, _live_groups=groups)
    # The report's displayed order is lexical path order. Derive a fresh live
    # proof with exactly that order from the original verified proof, rather
    # than changing paths on a sealed group or silently retaining another file.
    entries = {entry.path: entry for entry in result.files}
    ordered = []
    for group in groups:
        snapshots = validate_duplicate_proof(group)
        data = group.to_dict()
        data["paths"] = sorted(group.paths)
        canonical = MatchGroup(**data)
        _seal_duplicate_proof(canonical, [entries[path] for path in canonical.paths], snapshots)
        ordered.append(canonical)
    ordered.sort(key=lambda group: group.paths[0])
    return WorkspaceSession(result, ordered, _issuer=_ISSUER)


def _session(session):
    if not isinstance(session, WorkspaceSession):
        raise ValueError("보고서로 정리할 수 없습니다. 현재 앱에서 폴더를 분석하세요.")
    if not hmac.compare_digest(session._signature, ops._sign(session._payload(), ops._PLAN_KEY)):
        raise ValueError("분석 범위가 변경되었습니다. 다시 분석하세요.")
    if session._consumed:
        raise ValueError("이미 사용한 분석 결과입니다. 새 검사를 실행하세요.")
    if not session._complete:
        raise ValueError("완료된 폴더 분석이 필요합니다. 실패·취소 범위를 확인하세요.")
    # Inventory-only roots may be read-only/protected locations. Validate write
    # roots when building/locking their actual plans, not every registered root.
    roots = session.roots
    entries = {entry.path: entry for entry in session.files}
    owners = dict(session._owners)
    if len(entries) != len(session.files) or len(owners) != len(entries):
        raise ValueError("분석한 파일 소속이 일관되지 않습니다.")
    for path, root in owners.items():
        if root not in roots or not ops._inside(Path(path), Path(root)):
            raise ValueError("파일이 실제 분석 폴더에 속하지 않습니다.")
    return entries, owners


def _entry_snapshot(entry, fingerprint):
    if any(fingerprint[name] != getattr(entry, name) for name in ("size", "mtime_ns", "device", "inode")):
        raise ValueError("분석 이후 선택 파일이 변경되었습니다. 다시 분석하세요.")


def _base(session, mode, plans, *, warnings=None, decisions=None):
    items = [item for plan in plans for item in plan["items"]]
    result = {"id": uuid.uuid4().hex, "mode": mode, "schema": "folder-organizer.workspace-plan.v1",
              "session_id": session._id, "roots": list(session.roots),
              "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "plans": plans,
              "items": items, "warnings": list(warnings or []), "decisions": list(decisions or [])}
    result["signature"] = ops._sign(result, ops._PLAN_KEY)
    return result


def _reserve_targets(plans, all_sources):
    # Reserve across all roots, including unprocessed sources. A common output
    # folder cannot make two plans overwrite each other or retire another input.
    reserved = {str(Path(path)).casefold() for path in all_sources}
    for plan in plans:
        for item in plan["items"]:
            candidate = Path(item["target"])
            target = ops._unique(candidate, reserved)
            item["target"] = str(target)
        plan["signature"] = ops._sign(plan, ops._PLAN_KEY)


def build_workspace_plan(session, paths, mode, destination=None, date_basis="modified", *,
                         rules=None, action="copy", protected_folders=None, progress_detail=None):
    """Preview selected files using separate signed plans for actual roots."""
    entries, owners = _session(session)
    if mode not in {"copy", "move", "date", "extension", "quarantine", "rules"}:
        raise ValueError("지원하지 않는 통합 정리 방식입니다.")
    chosen = list(dict.fromkeys(paths if paths is not None else entries))
    if not chosen:
        raise ValueError("정리할 파일을 체크하세요.")
    if any(path not in entries for path in chosen):
        raise ValueError("현재 분석 목록 밖의 파일은 정리할 수 없습니다.")
    by_root = defaultdict(list)
    for path in chosen:
        by_root[owners[path]].append(path)
    plans, warnings = [], []
    for root, selected in by_root.items():
        if mode == "rules":
            if not rules:
                raise ValueError("분류 규칙을 지정하세요.")
            files = [replace(entries[path], relative_path=str(Path(path).relative_to(root))) for path in selected]
            plan = build_classification_plan(ScanResult(root, files), rules, destination=destination,
                action=action, protected_folders=protected_folders, progress_detail=progress_detail)
        else:
            plan = ops.build_plan(root, selected, mode, destination=destination,
                                  date_basis=date_basis, progress_detail=progress_detail)
        for item in plan["items"]:
            _entry_snapshot(entries[item["source"]], item)
        warnings.extend(plan.get("warnings", []))
        if plan["items"]:
            plans.append(plan)
    _reserve_targets(plans, entries)
    batch = _base(session, mode, plans, warnings=warnings)
    _preflight(session, batch, snapshots=False)
    return batch


def build_workspace_keep_first_plan(session, groups=None, *, protected_folders=None, progress_detail=None):
    """Keep the global displayed first once; metadata only, never rehash/rescan."""
    entries, owners = _session(session)
    groups = tuple(session.groups if groups is None else groups)
    if not groups:
        raise ValueError("완전 중복 검사를 먼저 실행하세요.")
    issued = {id(group) for group in session.groups}
    if any(id(group) not in issued for group in groups):
        raise ValueError("현재 분석 세션의 중복 그룹만 정리할 수 있습니다.")
    protected = tuple(Path(folder).absolute() for folder in (protected_folders or []))
    seen_paths, seen_physical, snapshots, decisions = set(), set(), {}, []
    roots_items = defaultdict(list)
    counters = defaultdict(dict)
    reserved = set()
    total = sum(len(group.paths) for group in groups)
    checked = 0
    for index, group in enumerate(groups, 1):
        proof = validate_duplicate_proof(group)
        for path in group.paths:
            if path not in entries or path in seen_paths:
                raise ValueError("중복 그룹이 현재 분석 범위를 벗어나거나 서로 겹칩니다.")
            expected = proof[path]
            entry = entries[path]
            _entry_snapshot(entry, expected)
            identity = (expected["device"], expected["inode"])
            if expected["inode"] and identity in seen_physical:
                raise ValueError("같은 물리 파일·하드링크를 중복 사본으로 처리하지 않습니다.")
            current = _current_snapshot(entry)
            if any(current[name] != expected[name] for name in current):
                raise ValueError("중복 검사 이후 파일이 변경되었습니다. 다시 검사하세요.")
            ops._source(path, Path(owners[path]))
            seen_paths.add(path)
            if expected["inode"]:
                seen_physical.add(identity)
            snapshots[path] = expected
            checked += 1
            ops._progress(progress_detail, "duplicate_reuse", checked, total, f"현재 검사 근거 확인: {entry.name}")
        first = group.paths[0]
        kept = [path for path in group.paths if path == first or any(Path(path).is_relative_to(folder) for folder in protected)]
        selected = [path for path in group.paths if path not in set(kept)]
        decisions.append({"index": index, "keeper": first, "paths": list(group.paths), "kept": kept,
                          "selected": selected, "status": "ready", "rule": "first"})
        for path in selected:
            source = Path(path)
            if any(parent.name.casefold().endswith(".app") for parent in source.parents):
                raise ValueError("앱 번들 내부 파일은 개별 정리하지 않습니다.")
            root = owners[path]
            target = ops._deletion_target(Path(root), source, reserved, counters=counters[root])
            roots_items[root].append({"source": path, "target": str(target), "action": "move",
                **{name: value for name, value in snapshots[path].items() if name != "path"},
                "snapshot_reuse": True, "deletion_storage": "delete_files",
                "duplicate_keeper": {**snapshots[first], "snapshot_reuse": True, "root": owners[first]},
                "rule": "완전 중복 · 통합 결과의 첫 항목과 보호 폴더 보존"})
    # Validate earlier members once more after later metadata checks, preserving
    # their original ChangeTime token rather than replacing it with a new one.
    for path, expected in snapshots.items():
        current = _current_snapshot(entries[path])
        if any(current[name] != expected[name] for name in current):
            raise ValueError("다른 그룹 확인 중 파일이 변경되었습니다. 다시 검사하세요.")
    plans = []
    for root, items in roots_items.items():
        plan = {"id": uuid.uuid4().hex, "root": root, "mode": "duplicate_auto",
                "destination": str(Path(root) / ops.DELETE_DIRECTORY), "date_basis": "modified",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(), "preserve_first": True,
                "reuse_verified_results": True, "deletion_storage": "delete_files",
                "items": items, "warnings": [], "decisions": decisions}
        plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
        plans.append(plan)
    batch = _base(session, "duplicate_auto", plans, decisions=decisions)
    _preflight(session, batch, snapshots=True)
    return batch


def _preflight(session, batch, *, snapshots, verify_sources=False):
    entries, owners = _session(session)
    ops._check_signature(batch, ops._PLAN_KEY)
    if batch.get("session_id") != session._id or batch.get("roots") != list(session.roots):
        raise ValueError("정리 계획이 현재 분석 범위와 다릅니다.")
    flattened = [item for plan in batch["plans"] for item in plan["items"]]
    if batch["items"] != flattened:
        raise ValueError("통합 미리보기의 파일과 실제 계획이 다릅니다.")
    selected = set()
    physical = set()
    targets = set()
    keepers = []
    for plan in batch["plans"]:
        ops._check_signature(plan, ops._PLAN_KEY)
        if plan["root"] not in session.roots:
            raise ValueError("분석한 폴더 밖의 정리 계획입니다.")
        for item in plan["items"]:
            source, target = item["source"], item["target"]
            if source not in entries or owners[source] != plan["root"]:
                raise ValueError("파일의 실제 소속 폴더와 계획이 다릅니다.")
            _entry_snapshot(entries[source], item)
            key, target_key = source.casefold(), target.casefold()
            identity = (item["device"], item["inode"])
            if key in selected or target_key in targets:
                raise ValueError("여러 폴더의 파일·대상 경로가 겹칩니다.")
            selected.add(key)
            targets.add(target_key)
            if item["inode"]:
                physical.add(identity)
            if target_key == key:
                raise ValueError("원본과 대상 경로가 같습니다.")
            guard = item.get("duplicate_keeper")
            if guard:
                if guard.get("root") not in session.roots or owners.get(guard["path"]) != guard["root"]:
                    raise ValueError("보존 원본이 현재 분석 범위에 속하지 않습니다.")
                keepers.append((item, Path(plan["root"]), guard))
            elif snapshots:
                raise ValueError("보존 원본의 검사 근거가 없습니다.")
            elif verify_sources:
                # Validate every participating root before the first mutation,
                # rather than discovering a stale later root after earlier
                # roots have already been changed.
                ops._source(source, Path(plan["root"]))
                ops._verify(Path(source), item)
    if selected & targets:
        raise ValueError("대상 경로가 다른 폴더의 선택 원본과 충돌합니다.")
    for item, root, guard in keepers:
        if (guard["path"].casefold() in selected | targets or
                guard["inode"] and (guard["device"], guard["inode"]) in physical):
            raise ValueError("다른 폴더의 보존 원본·하드링크가 정리 대상에 포함되어 있습니다.")
        ops._duplicate_guard(item, root)


def execute_workspace_plan(session, plan, *, progress_detail=None, cancel=None):
    """Run all root journals under sorted locks, with a shared global preflight."""
    _session(session)
    operations, errors, cancelled = [], [], False
    with session._execution_lock:
        _session(session)
        try:
            _preflight(session, plan, snapshots=plan.get("mode") == "duplicate_auto")
            if not plan["items"]:
                return {"id": plan["id"], "status": "unchanged", "items": [], "operations": [],
                        "warnings": plan["warnings"], "requires_new_analysis": True}
            if cancel is not None and cancel.is_set():
                cancelled = True
            else:
                lock_roots = {root_plan["root"] for root_plan in plan["plans"] if root_plan["items"]}
                lock_roots.update(item["duplicate_keeper"]["root"] for item in plan["items"]
                                  if item.get("duplicate_keeper"))
                with ops._workspace_locks(lock_roots):
                    _preflight(session, plan, snapshots=plan.get("mode") == "duplicate_auto", verify_sources=True)
                    session._consumed = True
                    for root_plan in plan["plans"]:
                        if cancel is not None and cancel.is_set():
                            cancelled = True
                            break
                        try:
                            result = ops.execute_plan(root_plan, progress_detail=progress_detail, cancel=cancel)
                        except (OSError, ValueError) as exc:
                            errors.append({"root": root_plan["root"], "error": str(exc)})
                            break
                        operations.append(result)
                        cancelled = result["status"] == "cancelled" or (cancel is not None and cancel.is_set())
                        if result["status"] != "completed" or cancelled:
                            break
        finally:
            # A failed preflight or cancelled execution cannot leave another
            # queued preview authorised to reuse this attempted session.
            session._consumed = True
    items = [item for operation in operations for item in operation["items"]]
    completed = sum(item.get("state") == "completed" for item in items)
    status = ("cancelled" if cancelled else "completed" if not errors and completed == len(plan["items"])
              else "partial" if completed else "failed")
    return {"id": plan["id"], "mode": plan["mode"], "status": status, "items": items,
            "operations": operations, "errors": errors, "warnings": plan["warnings"],
            "source_roots": list(session.roots), "requires_new_analysis": True, "cancelled": cancelled}


def _verify_workspace_merge_sources(session, plan, entries, owners):
    from . import merging

    seen = set()
    for item in plan["sources"]:
        path = item["path"]
        if path in seen or path not in entries or item.get("root") != owners[path]:
            raise merging.MergeError("병합 원본이 현재 분석한 실제 폴더 범위와 다릅니다.")
        seen.add(path)
        _entry_snapshot(entries[path], item)
        source = ops._source(path, ops._root(owners[path]))
        ops._verify(source, item)
        merging._check_source_metadata(source, item)
    if len(seen) != plan["source_count"] or not 2 <= len(seen) <= merging.MAX_FILES:
        raise merging.MergeError("병합 원본 개수가 올바르지 않습니다.")


def build_workspace_merge_preview(session, paths, *, destination=None, output_name=None,
                                   move_originals=False, progress_detail=None):
    """Bounded content merge over real source roots, without staging or moves."""
    from . import merging

    entries, owners = _session(session)
    selected = list(dict.fromkeys(map(str, paths)))
    if any(path not in entries for path in selected):
        raise merging.MergeError("현재 분석 목록의 파일만 병합할 수 있습니다.")
    plan = merging._build_scoped_merge_preview(selected, owners, destination=destination,
        output_name=output_name, move_originals=move_originals, progress_detail=progress_detail)
    if plan["output"].casefold() in {path.casefold() for path in entries}:
        raise merging.MergeError("병합본은 현재 분석 파일과 다른 새 경로에 저장해야 합니다.")
    _verify_workspace_merge_sources(session, plan, entries, owners)
    archive = build_workspace_plan(session, selected, "quarantine", progress_detail=progress_detail) if move_originals else None
    if archive:
        originals = {item["path"]: item for item in plan["sources"]}
        for item in archive["items"]:
            if any(item[field] != originals[item["source"]][field] for field in merging._FINGERPRINT_FIELDS):
                raise merging.MergeError("병합 내용을 확인한 뒤 원본이 변경되었습니다.")
    plan.update(schema="folder-organizer.workspace-merge.v1", session_id=session._id,
                archive_plan=archive)
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    return plan


def execute_workspace_merge(session, plan, *, progress_detail=None, cancel=None):
    """Create a verified new output, then optionally retire real-root originals."""
    import hashlib
    from contextlib import nullcontext
    from . import merging

    entries, owners = _session(session)
    result = {"id": plan.get("id", ""), "status": "failed", "output": plan.get("output", ""),
              "output_created": False, "output_verified": False, "source_count": plan.get("source_count", 0),
              "removed_units": plan.get("removed_units", 0), "archive_journal": None,
              "operations": [], "items": [], "error": None, "requires_new_analysis": True}
    with session._execution_lock:
        _session(session)
        try:
            ops._check_signature(plan, ops._PLAN_KEY)
            if (plan.get("schema") != "folder-organizer.workspace-merge.v1"
                    or plan.get("session_id") != session._id):
                raise merging.MergeError("현재 분석 세션의 병합 미리보기가 아닙니다.")
            if cancel is not None and cancel.is_set():
                result["status"] = "cancelled"
                return result
            output = Path(plan["output"])
            raw = plan["content"].encode("utf-8")
            if (len(raw) != plan["output_size"] or len(raw) > merging.MAX_TOTAL_BYTES
                    or hashlib.sha256(raw).hexdigest() != plan["output_sha256"]):
                raise merging.MergeError("병합본 내용 검증에 실패했습니다.")
            archive = plan.get("archive_plan")
            if bool(archive) != plan["move_originals"]:
                raise merging.MergeError("병합 원본 처리 방식과 정리 미리보기가 다릅니다.")
            participating = {owners[item["path"]] for item in plan["sources"]}
            with (ops._workspace_locks(participating) if archive else nullcontext()):
                _verify_workspace_merge_sources(session, plan, entries, owners)
                if archive:
                    _preflight(session, archive, snapshots=False, verify_sources=True)
                    if set(item["source"] for item in archive["items"]) != {item["path"] for item in plan["sources"]}:
                        raise merging.MergeError("병합 원본과 보관할 원본 목록이 다릅니다.")
                ops._safe_path(output)
                if output.exists() or output.is_symlink():
                    raise merging.MergeError("미리보기 이후 병합본 경로에 파일이 생겼습니다.")
                ops._capacity([{"target": str(output), "size": len(raw)}])
                if cancel is not None and cancel.is_set():
                    result["status"] = "cancelled"
                    return result
                session._consumed = True
                merging._progress(progress_detail, "merge", 0, plan["source_count"], "새 병합본 저장 및 검증")
                merging._write_output(output, raw, lambda: result.update(output_created=True))
                fingerprint = ops._fingerprint(output)
                if fingerprint["size"] != len(raw) or fingerprint["sha256"] != plan["output_sha256"]:
                    raise merging.MergeError("저장된 병합본의 검증에 실패했습니다. 원본을 유지합니다.")
                result["output_verified"] = True
                if archive:
                    _verify_workspace_merge_sources(session, plan, entries, owners)
                    for root_plan in archive["plans"]:
                        if cancel is not None and cancel.is_set():
                            result["status"] = "cancelled"
                            break
                        ops._verify(output, fingerprint)
                        receipt = ops.execute_plan(root_plan, progress_detail=progress_detail, cancel=cancel)
                        result["operations"].append(receipt)
                        result["items"].extend(receipt["items"])
                        ops._verify(output, fingerprint)
                        if receipt["status"] != "completed":
                            result["status"] = "cancelled" if receipt["status"] == "cancelled" else "partial"
                            result["error"] = "병합본은 보존했으며 일부 원본 보관을 완료하지 못했습니다. 폴더별 기록을 확인하세요."
                            break
                    else:
                        result["status"] = "completed"
                else:
                    result["status"] = "completed"
                merging._progress(progress_detail, "merge", plan["source_count"], plan["source_count"], "병합 작업 처리 완료")
        except (OSError, ValueError) as exc:
            result["status"] = "partial" if result["output_created"] else "failed"
            result["error"] = str(exc)
        finally:
            session._consumed = True
    return result
