"""Ordered classification rules and byte-proven duplicate quarantine previews.

Rules only create a signed preview. The ordinary journalled executor performs
the confirmed operation with exclusive targets and retained originals.
"""
from __future__ import annotations

from datetime import date, datetime
from fnmatch import fnmatchcase
import os
from pathlib import Path
import re
from string import Formatter
from typing import Callable

from . import operations as ops
from .models import MatchGroup, ScanResult
from .retention import choose_duplicate_copies

RULE_GUIDELINES = (
    "규칙은 위에서 아래로 검사하고 처음 일치한 규칙 하나만 적용합니다.\n"
    "먼저 복사로 작은 폴더를 시험하세요. 이동은 검증된 복사본과 원본 보호 사본을 남깁니다.\n"
    "확장자는 pdf,jpg처럼 쉼표로 입력하고, 이름에는 *.pdf 같은 와일드카드를 사용할 수 있습니다.\n"
    "폴더 조건은 상대 경로입니다. 예: 청구서 또는 업무/청구서 — 해당 폴더 아래 파일에도 적용합니다.\n"
    "날짜 조건은 수정일 기준이며 시작일·종료일을 모두 포함합니다.\n"
    "대상 폴더에는 {type}, {year}, {month}, {ext}, {source_folder}를 사용할 수 있습니다. "
    "{source_folder}를 넣으면 기존 하위 폴더 구조를 유지합니다. 파일명은 그대로 붙습니다.\n"
    "보호 폴더·이미 분류된 출력 폴더·일치하지 않는 파일·앱 번들 내부는 자동 처리하지 않습니다.\n"
    "완전 중복 자동 분류는 원본을 최소 하나 남기고 나머지를 격리합니다. 유사 후보는 제외합니다.\n"
    "미리보기 확인과 실행 직전 재검증이 모두 필요합니다. 결과는 작업 기록에서 되돌릴 수 있습니다."
)

_FIELDS = {"type", "year", "month", "ext", "source_folder"}
_INVALID = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.I)
_TYPES = {
    "사진": {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic"},
    "문서": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".md", ".csv", ".hwp", ".hwpx", ".odt", ".rtf"},
    "동영상": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv"},
    "음악": {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg"},
    "압축 파일": {".zip", ".7z", ".rar", ".tar", ".gz"},
    "설치 파일": {".exe", ".msi", ".msix", ".appx", ".dmg", ".pkg", ".deb", ".rpm"},
}


def default_rules(preset: str = "type") -> list[dict]:
    presets = {
        "type": ("파일 종류별 분류", "{type}/{source_folder}"),
        "date": ("수정 연도·월별 분류", "{year}/{month}/{source_folder}"),
        "folder": ("기존 폴더 구조 보존", "폴더별/{source_folder}"),
    }
    if preset not in presets:
        raise ValueError("지원하지 않는 분류 프리셋입니다.")
    name, target = presets[preset]
    return validate_rules([{"name": name, "target": target}])


def _relative_path(value: str, *, allow_glob: bool = False) -> str:
    value = value.replace("\\", "/").strip()
    if (not value or len(value) > 512 or value.startswith("/") or re.match(r"^[A-Za-z]:", value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(part.casefold() == ops.STATE_DIRECTORY.casefold() for part in value.split("/"))):
        raise ValueError("대상·폴더 조건은 '..' 없는 상대 경로여야 합니다.")
    for part in value.split("/"):
        invalid = re.search(r'[<>:"|\x00-\x1f]', part) if allow_glob else _INVALID.search(part)
        if invalid or part.endswith((" ", ".")) or _RESERVED.fullmatch(part):
            raise ValueError("Windows에서 사용할 수 없는 경로 이름입니다.")
    return value


def _day(value, field: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field}은 YYYY-MM-DD 형식이어야 합니다.") from exc


def validate_rules(rules: list[dict]) -> list[dict]:
    if not isinstance(rules, list) or not rules or len(rules) > 100:
        raise ValueError("분류 규칙은 1~100개가 필요합니다.")
    result = []
    for index, rule in enumerate(rules, 1):
        if not isinstance(rule, dict):
            raise ValueError("각 규칙은 이름과 조건을 가진 항목이어야 합니다.")
        name = rule.get("name", f"규칙 {index}")
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError("규칙 이름은 1~100자여야 합니다.")
        enabled = rule.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError("규칙 사용 여부는 참 또는 거짓이어야 합니다.")
        extensions = rule.get("extensions", [])
        if not isinstance(extensions, (list, tuple)) or len(extensions) > 100:
            raise ValueError("확장자는 최대 100개의 목록이어야 합니다.")
        normalized = []
        for extension in extensions:
            if not isinstance(extension, str) or not re.fullmatch(r"\.?[A-Za-z0-9][A-Za-z0-9._-]{0,39}", extension.strip()):
                raise ValueError("확장자는 pdf 또는 .jpg처럼 입력하세요.")
            normalized.append("." + extension.strip().lstrip(".").casefold())
        name_pattern = rule.get("name_pattern") or "*"
        folder_pattern = rule.get("folder_pattern") or "*"
        if not isinstance(name_pattern, str) or len(name_pattern) > 256 or re.search(r'[/\\\x00-\x1f]', name_pattern):
            raise ValueError("이름 조건은 경로가 아닌 파일 이름 패턴이어야 합니다.")
        if not isinstance(folder_pattern, str):
            raise ValueError("폴더 조건은 상대 경로 패턴이어야 합니다.")
        folder_pattern = _relative_path(folder_pattern, allow_glob=True)
        low, high = rule.get("min_size", 0), rule.get("max_size")
        if type(low) is not int or low < 0 or (high is not None and (type(high) is not int or high < low)):
            raise ValueError("크기 조건은 0 이상의 바이트이며 최대값은 최소값 이상이어야 합니다.")
        after, before = _day(rule.get("modified_after"), "시작일"), _day(rule.get("modified_before"), "종료일")
        if after and before and after > before:
            raise ValueError("날짜 종료일은 시작일 이상이어야 합니다.")
        target = rule.get("target", "{type}/{source_folder}")
        if not isinstance(target, str):
            raise ValueError("대상 폴더 템플릿은 문자열이어야 합니다.")
        try:
            for _, field, spec, conversion in Formatter().parse(target):
                if field is not None and (field not in _FIELDS or spec or conversion):
                    raise ValueError("대상 템플릿에는 type, year, month, ext, source_folder만 사용할 수 있습니다.")
            _relative_path(target.format(type="문서", year="2026", month="01", ext="txt", source_folder="업무/문서"))
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"대상 폴더 템플릿이 올바르지 않습니다: {exc}") from exc
        result.append({"name": name.strip(), "enabled": enabled, "extensions": list(dict.fromkeys(normalized)),
                       "name_pattern": name_pattern, "folder_pattern": folder_pattern,
                       "min_size": low, "max_size": high, "modified_after": after,
                       "modified_before": before, "target": target.replace("\\", "/").strip()})
    return result


def _within(path: str | Path, folder: str | Path) -> bool:
    a, b = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    try:
        return os.path.commonpath((a, b)) == b
    except ValueError:
        return False


def _folder_matches(relative: Path, pattern: str) -> bool:
    parts = relative.parent.parts
    if pattern == "*":
        return True
    pattern = pattern.casefold()
    if "/" not in pattern:
        return any(fnmatchcase(part.casefold(), pattern) for part in parts)
    return any(fnmatchcase("/".join(parts[:end]).casefold(), pattern) for end in range(1, len(parts) + 1))


def _matches(entry, relative: Path, rule: dict) -> bool:
    if not rule["enabled"] or (rule["extensions"] and entry.suffix.casefold() not in rule["extensions"]):
        return False
    if not fnmatchcase(entry.name.casefold(), rule["name_pattern"].casefold()) or not _folder_matches(relative, rule["folder_pattern"]):
        return False
    if entry.size < rule["min_size"] or (rule["max_size"] is not None and entry.size > rule["max_size"]):
        return False
    day = datetime.fromtimestamp(entry.modified).date().isoformat()
    return not ((rule["modified_after"] and day < rule["modified_after"]) or (rule["modified_before"] and day > rule["modified_before"]))


def _type(entry) -> str:
    return next((name for name, extensions in _TYPES.items() if entry.suffix.casefold() in extensions), "기타")


def build_classification_plan(scan: ScanResult, rules: list[dict], *,
                              destination: str | Path | None = None, action: str = "copy",
                              protected_folders: list[str] | None = None,
                              progress_detail: Callable[[dict], None] | None = None) -> dict:
    if scan.cancelled:
        raise ValueError("중단된 분석으로 자동 분류할 수 없습니다. 다시 분석하세요.")
    if action not in {"copy", "move"}:
        raise ValueError("자동 분류는 복사 또는 이동이어야 합니다.")
    normalized = validate_rules(rules)
    root = ops._root(scan.root)
    output = ops._absolute(destination) if destination else root / ops.ORGANIZED_DIRECTORY
    if output == root:
        raise ValueError("원래 폴더와 다른 분류 출력 폴더를 선택하세요.")
    protected = list(protected_folders or [])
    routes, warnings = [], []
    skipped = 0
    by_path = {entry.path: entry for entry in scan.files}
    for index, entry in enumerate(scan.files, 1):
        if progress_detail:
            progress_detail({"phase": "rules", "completed": index, "total": len(scan.files), "unit": "files", "message": f"규칙 확인: {entry.relative_path}"})
        source = Path(entry.path)
        try:
            relative = source.relative_to(root)
            if (_within(source, output) or any(_within(source, folder) for folder in protected)
                    or any(part.casefold() == ops.STATE_DIRECTORY.casefold() or part.casefold().endswith(".app") for part in relative.parts[:-1])):
                skipped += 1
                warnings.append(f"보호·출력 폴더 또는 앱 내부라 건너뜀: {relative}")
                continue
            rule = next((rule for rule in normalized if _matches(entry, relative, rule)), None)
            if not rule:
                skipped += 1
                continue
            current = source.stat(follow_symlinks=False)
            if (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino) != (entry.size, entry.mtime_ns, entry.device, entry.inode):
                raise ValueError("분석 후 파일 정보가 변경되었습니다. 다시 분석하세요.")
            moment = datetime.fromtimestamp(entry.modified)
            context = {"type": _type(entry), "year": moment.strftime("%Y"), "month": moment.strftime("%m"),
                       "ext": ops._extension_folder(source), "source_folder": relative.parent.as_posix() if relative.parent != Path(".") else "루트"}
            folder = _relative_path(rule["target"].format(**context))
            if any(_within(output / folder / entry.name, protected_folder) for protected_folder in protected):
                skipped += 1
                warnings.append(f"분류 대상이 보호 폴더 안이어서 건너뜀: {relative}")
                continue
            routes.append({"source": entry.path, "relative_target": f"{folder}/{entry.name}", "action": action, "rule": rule["name"]})
        except (ValueError, OSError, OverflowError) as exc:
            skipped += 1
            warnings.append(f"규칙 적용 불가로 건너뜀: {entry.relative_path}: {exc}")
    plan = ops.build_routed_plan(root, routes, destination=output, progress_detail=progress_detail)
    # Recheck that preview hashes still refer to the metadata used for rules.
    valid = []
    for item in plan["items"]:
        entry = by_path[item["source"]]
        if (item["size"], item["mtime_ns"], item["device"], item["inode"]) != (entry.size, entry.mtime_ns, entry.device, entry.inode):
            skipped += 1
            warnings.append(f"규칙 검증 중 변경되어 건너뜀: {entry.relative_path}")
        else:
            valid.append(item)
    plan["items"] = valid
    plan["warnings"].extend(warnings)
    plan["classification_summary"] = {"matched": len(valid), "skipped": skipped, "rules": len(normalized), "action": action}
    plan["rules"] = normalized
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    return plan


def build_duplicate_plan(scan: ScanResult, groups: list[MatchGroup], *,
                         retention_rule: str = "first", preferred_folder: str | None = None,
                         protected_folders: list[str] | None = None,
                         preserve_first: bool = False,
                         progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Preview quarantine of proven copies, preserving each designated keeper.

    preserve_first additionally retains the first displayed member of every
    eligible group, together with all protected members. It never converts a
    protected member into a selected copy to satisfy a single-keeper rule.
    """
    if scan.cancelled:
        raise ValueError("중단된 분석으로 중복 자동 분류할 수 없습니다.")
    if type(preserve_first) is not bool:
        raise ValueError("첫 항목 보존 여부는 참 또는 거짓이어야 합니다.")
    warnings = []
    exact = []
    for group in groups:
        evidence = group.evidence or {}
        if group.kind == "duplicate" and evidence.get("byte_identical") is True and isinstance(evidence.get("sha256"), str) and re.fullmatch(r"[0-9a-fA-F]{64}", evidence["sha256"]):
            exact.append(group)
        else:
            warnings.append("완전한 바이트·해시 근거가 없는 그룹은 중복 자동 분류에서 제외했습니다.")
    choice = choose_duplicate_copies(exact, scan.files, retention_rule, preferred_folder, protected_folders)
    if preserve_first:
        candidates = set(choice["selected"]) | set(choice["kept"])
        for decision in choice["decisions"]:
            if decision["status"] != "ready" or not decision["paths"]:
                continue
            first = decision["paths"][0]
            retained = {first} | {path for path in decision["paths"]
                                  if any(_within(path, folder) for folder in (protected_folders or []))}
            decision["selected"] = [path for path in decision["paths"] if path not in retained]
            decision["kept"] = [path for path in decision["paths"] if path in retained]
            decision["keeper"] = first
            decision["reason"] = "그룹의 첫 항목과 보호 폴더의 파일을 모두 보존합니다."
        selected = {path for decision in choice["decisions"] if decision["status"] == "ready"
                    for path in decision["selected"]}
        choice["selected"] = sorted(selected, key=lambda path: (path.casefold(), path))
        choice["kept"] = sorted(candidates - selected, key=lambda path: (path.casefold(), path))
    if choice["selected"]:
        plan = ops.build_plan(scan.root, choice["selected"], "quarantine", progress_detail=progress_detail)
    else:
        plan = ops.build_routed_plan(scan.root, [], progress_detail=progress_detail)
    plan["mode"] = "duplicate_auto"
    plan["preserve_first"] = preserve_first
    plan["destination"] = str(Path(scan.root) / ops.STATE_DIRECTORY / "quarantine" / plan["id"])
    plan["decisions"] = choice["decisions"]
    plan["warnings"].extend(warnings + choice["warnings"])
    items = {item["source"]: item for item in plan["items"]}
    valid_sources = set()
    for decision in choice["decisions"]:
        if decision["status"] != "ready" or not decision["selected"]:
            continue
        try:
            keeper = decision["keeper"]
            fingerprint = ops._fingerprint(Path(keeper))
            expected = exact[decision["index"] - 1].evidence["sha256"].lower()
            if fingerprint["sha256"] != expected:
                raise ValueError("보존 원본의 내용이 변경되었습니다.")
            for source in decision["selected"]:
                item = items[source]
                if item["sha256"] != expected or (item["device"], item["inode"]) == (fingerprint["device"], fingerprint["inode"]):
                    raise ValueError("같은 물리 파일이거나 내용이 변경되었습니다.")
                item["duplicate_keeper"] = {"path": keeper, **fingerprint}
                item["rule"] = "완전 중복 · 첫 항목과 보호 폴더 보존" if preserve_first else f"완전 중복 · {retention_rule} 원본 보존"
                ops._duplicate_guard(item, Path(scan.root))
            valid_sources.update(decision["selected"])
        except (OSError, ValueError) as exc:
            plan["warnings"].append(f"중복 그룹 최종 검증 실패로 건너뜀: {exc}")
    plan["items"] = [item for item in plan["items"] if item["source"] in valid_sources]
    if plan["items"]:
        ops._capacity(plan["items"])
    plan["classification_summary"] = {"matched": len(plan["items"]), "kept": len(choice["kept"]), "action": "quarantine"}
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    return plan
