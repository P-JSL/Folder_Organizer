"""Ordered classification rules and byte-proven duplicate quarantine previews.

Rules only create a signed preview. The ordinary journalled executor performs
the confirmed operation with exclusive targets and retained originals.
"""
from __future__ import annotations

from datetime import date, datetime
from fnmatch import fnmatchcase
import math
import os
from pathlib import Path
import re
import uuid
from string import Formatter
from types import SimpleNamespace
from typing import Callable

from . import operations as ops
from .models import MatchGroup, ScanResult
from .matching import _current_snapshot, validate_duplicate_proof
from .deletion import DELETE_DIRECTORY
from .retention import choose_duplicate_copies

RULE_GUIDELINES = (
    "규칙은 위에서 아래로 검사하고 처음 일치한 규칙 하나만 적용합니다.\n"
    "먼저 복사로 작은 폴더를 시험하세요. 이동은 검증된 복사본과 원본 보호 사본을 남깁니다.\n"
    "확장자는 pdf,jpg처럼 쉼표로 입력하고, 이름에는 *.pdf 같은 와일드카드를 사용할 수 있습니다.\n"
    "폴더 조건은 상대 경로입니다. 예: 청구서 또는 업무/청구서 — 해당 폴더 아래 파일에도 적용합니다.\n"
    "날짜 조건은 선택한 수정일·생성일 기준이며 시작일·종료일을 모두 포함합니다.\n"
    "대상 폴더에는 {종류}, {날짜}, {규칙}, {확장자}, {원래폴더}와 영어 표기를 사용할 수 있습니다. "
    "예: {수정일:%Y/%m/%d}, {created:%Y-%m}, {year}/{quarter}분기. 생성일이 없으면 생성일 규칙은 건너뜁니다.\n"
    "{source_folder}를 넣으면 기존 하위 폴더 구조를 유지합니다. 파일명은 그대로 붙습니다.\n"
    "보호 폴더·이미 분류된 출력 폴더·일치하지 않는 파일·앱 번들 내부는 자동 처리하지 않습니다.\n"
    "완전 중복 자동 분류는 원본을 최소 하나 남기고 나머지를 격리합니다. 유사 후보는 제외합니다.\n"
    "미리보기 확인과 실행 직전 재검증이 모두 필요합니다. 결과는 작업 기록에서 되돌릴 수 있습니다."
)

_DATE_PARTS = {"year", "month", "day", "date", "weekday", "quarter", "week", "hour", "minute", "second"}
_ALIASES = {
    "종류": "type", "확장자": "ext", "원래폴더": "source_folder", "규칙": "rule", "이름": "name",
    "연도": "year", "년": "year", "월": "month", "일": "day", "날짜": "date",
    "요일": "weekday", "분기": "quarter", "주차": "week", "시": "hour", "분": "minute", "초": "second",
    "수정일": "modified", "생성일": "created", "stem": "name",
}
for _basis, _korean in (("modified", "수정"), ("created", "생성")):
    for _part, _label in (("year", "연도"), ("month", "월"), ("day", "일"), ("date", "날짜"),
                          ("weekday", "요일"), ("quarter", "분기"), ("week", "주차"),
                          ("hour", "시"), ("minute", "분"), ("second", "초")):
        # 수정일/생성일 are date values, so explicit day aliases use 수정날/생성날.
        _ALIASES[_korean + ("날" if _part == "day" else _label)] = _basis + "_" + _part
_FIELDS = {"type", "ext", "source_folder", "rule", "name", "modified", "created"} | _DATE_PARTS | {
    f"{basis}_{part}" for basis in ("modified", "created") for part in _DATE_PARTS
}
_ALIASES.update({field: field for field in _FIELDS})
_DATE_DIRECTIVES = {"Y", "m", "d", "H", "M", "S", "j", "u", "V", "G", "%"}
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
        "extension": ("확장자별 분류", "{ext}/{source_folder}"),
        "date": ("수정 연도·월별 분류", "{year}/{month}/{source_folder}"),
        "folder": ("기존 폴더 구조 보존", "폴더별/{source_folder}"),
        "daily": ("수정 날짜별 분류", "{date}/{source_folder}"),
        "created_date": ("생성 연도·월별 분류", "{created:%Y/%m}/{source_folder}"),
        "quarter": ("수정 분기별 분류", "{year}/{quarter}분기/{source_folder}"),
        "rule": ("규칙 이름별 분류", "{rule}/{type}/{source_folder}"),
    }
    if preset not in presets:
        raise ValueError("지원하지 않는 분류 프리셋입니다.")
    name, target = presets[preset]
    return validate_rules([{"name": name, "target": target,
                            "date_basis": "created" if preset == "created_date" else "modified"}])


def mapping_tokens() -> list[dict[str, str]]:
    """Describe supported folder mappings without reading files or evaluating code."""
    entries = [
        ("{종류}", "파일 종류", "사진", "사진·문서·동영상 등 파일 종류"),
        ("{확장자}", "확장자", "jpg", "점 없는 파일 확장자"),
        ("{원래폴더}", "원래 하위 폴더", "업무/청구서", "원래 상대 폴더 구조를 유지합니다. 루트 파일은 ‘루트’입니다."),
        ("{규칙}", "규칙 이름", "회사 청구서", "현재 적용된 규칙 이름을 안전한 폴더 이름으로 사용합니다."),
        ("{이름}", "파일 이름", "보고서", "확장자를 뺀 원래 파일 이름입니다. 실제 파일 이름은 바꾸지 않습니다."),
        ("{연도}", "기준 연도", "2026", "선택한 날짜 기준의 연도. 기본은 수정일입니다."),
        ("{월}", "기준 월", "10", "선택한 날짜 기준의 두 자리 월"),
        ("{일}", "기준 일", "15", "선택한 날짜 기준의 두 자리 일"),
        ("{날짜}", "기준 날짜", "2026-10-15", "선택한 날짜 기준의 연-월-일"),
        ("{요일}", "기준 요일", "목", "월·화·수·목·금·토·일"),
        ("{분기}", "기준 분기", "4", "1~4분기 번호. 예: {연도}/{분기}분기"),
        ("{주차}", "기준 주차", "42", "ISO 주차 01~53. 연도 경계에서는 날짜 연도와 다를 수 있습니다."),
        ("{수정일:%Y-%m-%d}", "수정일 날짜 형식", "2026-10-15", "수정일을 지정한 날짜 형식으로 매핑합니다."),
        ("{수정일:%Y/%m}", "수정 연도/월 폴더", "2026/10", "수정 연도와 월을 서로 다른 폴더로 만듭니다."),
        ("{생성일:%Y-%m-%d}", "생성일 날짜 형식", "2025-03-07", "실제 생성일이 제공될 때만 사용합니다. 없으면 파일을 건너뜁니다."),
        ("{created:%Y/%m}", "생성 연도/월 폴더", "2025/03", "생성일 영어 표기. 수정일로 대신하지 않습니다."),
        ("{modified_year}", "명시적 수정 연도", "2026", "선택한 날짜 기준과 관계없이 수정일 연도입니다."),
        ("{created_year}", "명시적 생성 연도", "2025", "선택한 날짜 기준과 관계없이 실제 생성일 연도입니다."),
        ("{type}", "종류 · 영어", "사진", "{종류}와 같습니다."),
        ("{ext}", "확장자 · 영어", "jpg", "{확장자}와 같습니다."),
        ("{source_folder}", "원래 폴더 · 영어", "업무/청구서", "{원래폴더}와 같습니다."),
        ("{rule}", "규칙 이름 · 영어", "회사 청구서", "{규칙}과 같습니다."),
        ("{name}", "파일 이름 · 영어", "보고서", "{이름}과 같습니다."),
        ("{stem}", "파일 이름 · 확장자 제외", "보고서", "{이름}, {name}과 같습니다. 실제 파일 이름은 바꾸지 않습니다."),
        ("{year}", "기준 연도 · 영어", "2026", "{연도}와 같습니다. 기존 규칙은 수정일 기준을 유지합니다."),
        ("{month}", "기준 월 · 영어", "10", "{월}과 같습니다."),
        ("{day}", "기준 일 · 영어", "15", "{일}과 같습니다."),
        ("{date}", "기준 날짜 · 영어", "2026-10-15", "{날짜}와 같습니다."),
    ]
    return [{"token": token, "label": label, "example": example, "description": description}
            for token, label, example, description in entries]


def _template_parts(template: str):
    if not isinstance(template, str) or not template.strip() or len(template) > 512:
        raise ValueError("대상 폴더 템플릿은 1~512자여야 합니다.")
    parts = list(Formatter().parse(template))
    for _literal, field, spec, conversion in parts:
        if field is None:
            continue
        canonical = _ALIASES.get(field)
        if canonical is None or conversion:
            raise ValueError("지원하는 {규칙} 기호만 사용하세요. 속성·인덱스·형식 변환은 사용할 수 없습니다.")
        if spec:
            if canonical not in {"modified", "created"} or len(spec) > 100 or "{" in spec or "}" in spec:
                raise ValueError("날짜 형식은 {수정일:%Y-%m-%d}, {생성일:%Y/%m}처럼 입력하세요.")
            index = 0
            while index < len(spec):
                if spec[index] == "%":
                    if index + 1 == len(spec) or spec[index + 1] not in _DATE_DIRECTIVES:
                        raise ValueError("날짜 형식은 %Y·%m·%d·%H·%M·%S·%j·%u·%V·%G·%%만 사용할 수 있습니다.")
                    index += 2
                else:
                    index += 1
    return parts


def _safe_component(value: str, fallback: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not value:
        return fallback
    if _RESERVED.fullmatch(value) or value.casefold() == ops.STATE_DIRECTORY.casefold():
        value = "_" + value
    return value[:100].rstrip(" .") or fallback


def _moment(entry, basis: str) -> datetime:
    value = getattr(entry, basis, None)
    label = "생성일" if basis == "created" else "수정일"
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}을 제공하지 않아 해당 날짜 규칙을 적용할 수 없습니다.")
    try:
        return datetime.fromtimestamp(value)
    except (OSError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label}을 사용할 수 있는 날짜로 변환할 수 없습니다.") from exc


def _date_value(moment: datetime, part: str) -> str:
    values = {"year": f"{moment.year:04d}", "month": f"{moment.month:02d}", "day": f"{moment.day:02d}",
              "date": moment.date().isoformat(), "weekday": "월화수목금토일"[moment.weekday()],
              "quarter": str((moment.month - 1) // 3 + 1), "week": f"{moment.isocalendar().week:02d}",
              "hour": f"{moment.hour:02d}", "minute": f"{moment.minute:02d}", "second": f"{moment.second:02d}"}
    return values[part]


def _format_date(moment: datetime, spec: str) -> str:
    """Render the small documented format set identically on Windows and Unix.

    Platform strftime implementations differ for ISO weeks and early years.
    Parsing has already rejected unsupported directives and modifiers.
    """
    iso = moment.isocalendar()
    directives = {"Y": f"{moment.year:04d}", "m": f"{moment.month:02d}", "d": f"{moment.day:02d}",
                  "H": f"{moment.hour:02d}", "M": f"{moment.minute:02d}", "S": f"{moment.second:02d}",
                  "j": f"{moment.timetuple().tm_yday:03d}", "u": str(iso.weekday),
                  "V": f"{iso.week:02d}", "G": f"{iso.year:04d}", "%": "%"}
    return re.sub(r"%([YmdHMSjuVG%])", lambda match: directives[match.group(1)], spec)


def render_target(rule: dict, entry, relative: Path) -> str:
    """Render a validated relative folder; never eval expressions or rename files.

    Rules may be raw or normalized. Template validation uses this same renderer
    with representative metadata, so GUI examples and real plans share semantics.
    """
    basis = rule.get("date_basis", "modified")
    if basis not in {"modified", "created"}:
        raise ValueError("날짜 기준은 수정일 또는 생성일이어야 합니다.")
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("원래 폴더는 안전한 상대 경로여야 합니다.")
    metadata = {"type": _type(entry), "ext": ops._extension_folder(Path(entry.name)),
                "source_folder": relative.parent.as_posix() if relative.parent != Path(".") else "루트",
                "rule": _safe_component(rule.get("name", "규칙"), "규칙"),
                "name": _safe_component(Path(entry.name).stem, "파일")}
    moments = {}
    rendered = []
    for literal, field, spec, _conversion in _template_parts(rule.get("target", "{type}/{source_folder}")):
        rendered.append(literal)
        if field is None:
            continue
        key = _ALIASES[field]
        if key in metadata:
            rendered.append(metadata[key])
            continue
        selected_basis, part = basis, key
        if key in {"modified", "created"}:
            selected_basis, part = key, "date"
        elif key.startswith(("modified_", "created_")):
            selected_basis, part = key.split("_", 1)
        if selected_basis not in moments:
            moments[selected_basis] = _moment(entry, selected_basis)
        rendered.append(_format_date(moments[selected_basis], spec) if spec else _date_value(moments[selected_basis], part))
    return _relative_path("".join(rendered))


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
        basis = rule.get("date_basis", "modified")
        if not isinstance(basis, str) or basis not in {"modified", "created"}:
            raise ValueError("날짜 기준은 수정일 또는 생성일이어야 합니다.")
        target = rule.get("target", "{type}/{source_folder}")
        if not isinstance(target, str):
            raise ValueError("대상 폴더 템플릿은 문자열이어야 합니다.")
        target = target.replace("\\", "/").strip()
        try:
            # Validation and the editor use the exact renderer used by plans.
            sample = SimpleNamespace(name="예시.txt", suffix=".txt",
                                     modified=datetime(2026, 10, 15, 12, 30, 45).timestamp(),
                                     created=datetime(2025, 3, 7, 9, 15, 20).timestamp())
            render_target({"name": name.strip(), "target": target, "date_basis": basis},
                          sample, Path("업무/문서/예시.txt"))
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"대상 폴더 템플릿이 올바르지 않습니다: {exc}") from exc
        result.append({"name": name.strip(), "enabled": enabled, "extensions": list(dict.fromkeys(normalized)),
                       "name_pattern": name_pattern, "folder_pattern": folder_pattern,
                       "min_size": low, "max_size": high, "modified_after": after,
                       "modified_before": before, "target": target, "date_basis": basis})
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
    if not (rule["modified_after"] or rule["modified_before"]):
        return True
    day = _moment(entry, rule["date_basis"]).date().isoformat()
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
            folder = render_target(rule, entry, relative)
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
    plan["destination"] = str(Path(scan.root) / DELETE_DIRECTORY)
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
    if plan["items"] and plan.get("deletion_storage") != "delete_files":
        ops._capacity(plan["items"])
    plan["classification_summary"] = {"matched": len(plan["items"]), "kept": len(choice["kept"]), "action": "quarantine"}
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    return plan


def build_existing_duplicate_plan(scan: ScanResult, groups: list[MatchGroup], *,
                                  protected_folders: list[str] | None = None,
                                  progress_detail: Callable[[dict], None] | None = None) -> dict:
    """Keep each displayed first file using authenticated live byte-proof state.

    This dedicated one-click path performs metadata/descriptor ChangeTime checks
    only. It never rescans, hashes files, changes checkboxes or quietly falls
    back to a slower analysis. The executor rechecks the keeper snapshot and atomically moves owned
    files into delete_files. Missing/stale/imported proof rejects the request.
    """
    if not isinstance(scan, ScanResult) or scan.cancelled:
        raise ValueError("완료된 현재 폴더 분석이 필요합니다. 중복 검사를 다시 실행하세요.")
    directory = ops._root(scan.root)
    if not isinstance(groups, list) or not groups:
        raise ValueError("현재 화면에 확인된 중복 그룹이 없습니다. 중복 검사를 실행하세요.")
    if protected_folders is not None and (not isinstance(protected_folders, list)
            or len(protected_folders) > 100 or any(not isinstance(path, str) or not path for path in protected_folders)):
        raise ValueError("보호 폴더 설정이 올바르지 않습니다.")
    protected = protected_folders or []
    entries = {}
    for entry in scan.files:
        if entry.path in entries and entries[entry.path] != entry:
            raise ValueError("분석한 파일 정보가 일관되지 않습니다. 다시 분석하세요.")
        entries[entry.path] = entry
    identifier = uuid.uuid4().hex
    target_root = directory / DELETE_DIRECTORY
    plan = {"id": identifier, "root": str(directory), "mode": "duplicate_auto",
            "destination": str(target_root), "date_basis": "modified",
            "created_at": datetime.now().astimezone().isoformat(),
            "preserve_first": True, "reuse_verified_results": True, "deletion_storage": "delete_files",
            "items": [], "decisions": [], "warnings": [],
            "safety": ["현재 검사 근거 인증", "변경 시각·파일 식별자 재확인", "첫 항목·보호 폴더 보존", "검증 복사·복원 기록"]}
    all_snapshots, seen_paths, seen_physical, reserved = {}, set(), set(), set()
    target_counters = {}
    total = sum(len(group.paths) for group in groups if isinstance(group, MatchGroup))
    checked = 0
    for index, group in enumerate(groups, 1):
        snapshots = validate_duplicate_proof(group)
        for path in group.paths:
            if path not in entries:
                raise ValueError("중복 파일이 현재 분석 목록에 없습니다. 다시 검사하세요.")
            source = ops._source(path, directory)
            normalized = str(source).casefold()
            expected = snapshots[path]
            entry = entries[path]
            if (normalized in seen_paths or any(expected[key] != getattr(entry, key) for key in
                                               ("size", "mtime_ns", "device", "inode"))):
                raise ValueError("중복 그룹이 겹치거나 분석 정보가 변경되었습니다. 다시 검사하세요.")
            physical = (expected["device"], expected["inode"])
            if expected["inode"] and physical in seen_physical:
                raise ValueError("중복 그룹에 같은 물리 파일 또는 하드링크가 겹칩니다. 다시 검사하세요.")
            current = _current_snapshot(entry)
            if any(current[key] != expected[key] for key in current):
                raise ValueError("중복 검사 이후 파일이 변경되었습니다. 중복 검사를 다시 실행하세요.")
            seen_paths.add(normalized)
            if expected["inode"]:
                seen_physical.add(physical)
            all_snapshots[path] = expected
            checked += 1
            if progress_detail:
                progress_detail({"phase": "duplicate_reuse", "completed": checked, "total": total,
                                 "unit": "files", "message": f"기존 검사 근거 확인: {entry.relative_path}"})
        first = group.paths[0]
        kept = [path for path in group.paths if path == first or any(_within(path, folder) for folder in protected)]
        retained = set(kept)
        selected = [path for path in group.paths if path not in retained]
        plan["decisions"].append({"index": index, "rule": "first", "status": "ready",
                                  "paths": list(group.paths), "keeper": first, "kept": kept,
                                  "selected": selected, "reason": "현재 결과의 첫 항목과 보호 폴더 파일을 보존합니다."})
        for path in selected:
            source = Path(path)
            if any(parent.name.casefold().endswith(".app") for parent in source.parents):
                raise ValueError("앱 번들의 내부 파일은 개별 정리하지 않습니다.")
            target = ops._deletion_target(directory, source, reserved, counters=target_counters)
            fingerprint = {key: value for key, value in snapshots[path].items() if key != "path"}
            keeper = {**snapshots[first], "snapshot_reuse": True}
            plan["items"].append({"source": path, "target": str(target), "action": "move",
                                  "rule": "완전 중복 · 현재 첫 항목과 보호 폴더 보존",
                                  **fingerprint, "snapshot_reuse": True, "duplicate_keeper": keeper,
                                  "deletion_storage": "delete_files"})
    # Earlier keepers may change while later groups are checked. Never replace
    # their original ChangeTime token with a fresh one from that gap.
    for path, expected in all_snapshots.items():
        current = _current_snapshot(entries[path])
        if any(current[key] != expected[key] for key in current):
            raise ValueError("다른 그룹 확인 중 파일이 변경되었습니다. 중복 검사를 다시 실행하세요.")
    plan["classification_summary"] = {"matched": len(plan["items"]),
                                       "kept": sum(len(item["kept"]) for item in plan["decisions"]),
                                       "action": "quarantine"}
    plan["signature"] = ops._sign(plan, ops._PLAN_KEY)
    return plan
