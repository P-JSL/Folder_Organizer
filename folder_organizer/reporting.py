"""Portable, local analysis reports without file contents or executable links."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

from . import __version__
from .models import MatchGroup, ScanResult

DEFINITIONS = {
    "duplicate": "완전 중복: 크기·SHA-256·실제 바이트가 모두 일치한 파일입니다. 이름·날짜와 무관합니다.",
    "similar": "유사 후보: 이름·텍스트 어구·축소 이미지 특징이 닮은 파일입니다. 점수는 동일 확률이 아니며 자동 정리 대상이 아닙니다.",
    "scope": "이 보고서는 선택한 검사 범위의 결과입니다. 제외 폴더의 내부 파일 수는 계산하지 않습니다.",
    "storage": "안전 보관과 보호 사본은 용량을 차지합니다. 표시된 중복 용량은 즉시 회수되는 공간이 아닙니다.",
}


def build_report(scan: ScanResult, *, duplicates: list[MatchGroup] | None = None,
                 similar: list[MatchGroup] | None = None, comparison_report: dict | None = None,
                 selection: dict | None = None) -> dict:
    report = {
        "schema_version": 2, "app_version": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": scan.root, "total_size": scan.total_size,
        "files": [entry.to_dict() for entry in scan.files],
        "warnings": list(scan.warnings), "cancelled": scan.cancelled,
        "scope": dict(scan.scan_options),
        "filtered_files": scan.filtered_files, "excluded_directories": scan.excluded_directories,
        "definitions": dict(DEFINITIONS),
    }
    if duplicates is not None:
        report["duplicates"] = [group.to_dict() for group in duplicates]
    if similar is not None:
        report["similar"] = [group.to_dict() for group in similar]
    if comparison_report is not None:
        report["comparison_report"] = dict(comparison_report)
    if selection is not None:
        report["selection"] = selection
    return report


def _cell(value) -> str:
    return escape(str(value if value is not None else "제공되지 않음"), quote=True)


def _score(value) -> str:
    return f"{value:.1%}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "미평가"


def _date(value) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "제공되지 않음"
    try:
        return datetime.fromtimestamp(value).astimezone().isoformat(sep=" ", timespec="seconds")
    except (ValueError, OSError, OverflowError):
        return "날짜 범위 초과"


def render_html_report(report: dict) -> str:
    """Render a bounded reading view; JSON remains the complete machine report."""
    parts = ["<!doctype html><html lang='ko'><head><meta charset='utf-8'>",
             "<meta name='viewport' content='width=device-width,initial-scale=1'>",
             "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">",
             "<title>Folder Organizer 분석 보고서</title><style>",
             "body{font:15px/1.6 system-ui,sans-serif;background:#101820;color:#e6edf3;margin:0;padding:32px;max-width:1400px}",
             "h1,h2{color:#85e4ca}table{border-collapse:collapse;width:100%;margin:16px 0}td,th{padding:9px;border:1px solid #3a4b59;text-align:left;overflow-wrap:anywhere}",
             "th{background:#1d2b38}section{margin:24px 0;padding:18px;background:#17232e;border-radius:10px}p,li{overflow-wrap:anywhere}",
             ".notice{color:#f4cd82}small{color:#b3c3ce}</style></head><body>",
             "<h1>Folder Organizer · 분석 보고서</h1>",
             f"<p>버전 {_cell(report.get('app_version'))} · 생성 시각 {_cell(report.get('generated_at'))} (UTC 표기)</p>",
             f"<p>분석 폴더: {_cell(report.get('root'))}</p><section><h2>판정의 의미</h2>"]
    for definition in DEFINITIONS.values():
        parts.append(f"<p>{_cell(definition)}</p>")
    parts.append("</section><section><h2>분석 범위</h2>")
    files = report.get("files", [])
    scope = report.get("scope", {})
    parts.append(f"<p>파일 {len(files):,}개 · 총 {_cell(report.get('total_size'))}바이트 · 크기/패턴 제외 {_cell(report.get('filtered_files', 0))}개 · 폴더 가지 제외 {_cell(report.get('excluded_directories', 0))}개</p>")
    parts.append(f"<p>하위 폴더: {'포함' if scope.get('recursive', True) else '제외'} · 숨김 파일: {'포함' if scope.get('include_hidden', False) else '제외'} · 크기: {_cell(scope.get('min_size', 0))}바이트 이상 / {_cell(scope.get('max_size') if scope.get('max_size') is not None else '상한 없음')}</p>")
    patterns = scope.get("exclude_patterns", [])
    parts.append(f"<p>제외 패턴: {_cell(', '.join(patterns) or '없음')}</p>")
    if report.get("cancelled"):
        parts.append("<p class='notice'>중단된 분석입니다. 전체 폴더를 검사한 결과가 아닙니다.</p>")
    diagnostics = report.get("comparison_report", {})
    if diagnostics:
        labels = {"name": "이름", "text": "텍스트", "image": "이미지"}
        methods = diagnostics.get("methods", [])
        thresholds = diagnostics.get("thresholds", {})
        parts.append("<p>비교 기준: " + " · ".join(f"{_cell(labels.get(method, method))} {_score(thresholds.get(method))}" for method in methods) + "</p>")
        parts.append(f"<p>비교 {_cell(diagnostics.get('compared_pairs', 0))}쌍 / 한도 {_cell(diagnostics.get('max_pairs', 25000))}쌍 · 완전 중복 제외 {_cell(diagnostics.get('exact_pairs_excluded', 0))}쌍 · 읽기 실패 등 건너뜀 {_cell(diagnostics.get('skipped_count', 0))}건</p>")
        if diagnostics.get("limit_reached"):
            parts.append("<p class='notice'>비교 한도에 도달했습니다. 이후 쌍은 검사하지 않은 부분 결과입니다.</p>")
        if diagnostics.get("cancelled"):
            parts.append("<p class='notice'>유사 비교를 중단했습니다. 전체 결과가 아닙니다.</p>")
        parts.extend(f"<p class='notice'>{_cell(warning)}</p>" for warning in diagnostics.get("warnings", [])[:100])
    for warning in report.get("warnings", [])[:100]:
        parts.append(f"<p class='notice'>{_cell(warning)}</p>")
    parts.append("</section>")
    for key, title in (("duplicates", "완전 중복"), ("similar", "유사 후보 · 직접 검토 필요")):
        if key not in report:
            continue
        groups = report[key]
        parts.append(f"<section><h2>{title} · {len(groups):,}그룹</h2>")
        if not groups:
            parts.append("<p>선택한 범위와 기준에서 발견되지 않았습니다.</p>")
        for index, group in enumerate(groups[:1000], 1):
            evidence = group.get("evidence", {})
            byte_result = {True: "일치 검증됨", False: "다름", None: "확인되지 않음"}.get(evidence.get("byte_identical"), "확인되지 않음")
            kind_label = {"duplicate": "완전 중복", "similar_name": "이름 유사", "similar_text": "텍스트 유사", "similar_image": "이미지 유사"}.get(group.get("kind"), "검토 후보")
            parts.append(f"<h3>그룹 {index} · {kind_label}</h3><p>바이트: {byte_result} · 이름 {_score(evidence.get('name_score'))} · 텍스트 {_score(evidence.get('text_score'))} · 이미지 {_score(evidence.get('image_score'))}</p>")
            if evidence.get("sha256"):
                parts.append(f"<p>검증 SHA-256: <code>{_cell(evidence['sha256'])}</code></p>")
            parts.append(f"<p>{_cell(group.get('reason'))}</p><ul>")
            parts.extend(f"<li>{_cell(path)}</li>" for path in group.get("paths", []))
            parts.append("</ul>")
            for issue in evidence.get("limitations", []):
                parts.append(f"<p class='notice'>{_cell(issue)}</p>")
        if len(groups) > 1000:
            parts.append("<p class='notice'>읽기용 HTML은 앞 1,000개 그룹만 표시합니다. 전체 결과는 JSON으로 내보내세요.</p>")
        parts.append("</section>")
    if report.get("selection") is not None:
        parts.append("<section><h2>보존 규칙의 선택 제안</h2><p>체크 상자 선택 제안이며 파일 변경 작업을 실행한 기록은 아닙니다.</p>")
        selection = report["selection"]
        selected = selection.get("paths", selection.get("selected", []))
        parts.append(f"<p>현재 선택한 파일 {len(selected):,}개</p><ul>")
        parts.extend(f"<li>{_cell(path)}</li>" for path in selected[:2000])
        parts.append("</ul>")
        if len(selected) > 2000:
            parts.append("<p>선택 경로는 앞 2,000개만 표시합니다.</p>")
        retention = selection.get("retention") or selection
        for decision in retention.get("decisions", [])[:1000]:
            parts.append(f"<p>그룹 {_cell(decision.get('index'))}: {_cell(decision.get('reason'))} 보존 파일: {_cell(decision.get('keeper'))}</p>")
        parts.append("</section>")
    parts.append("<section><h2>파일 목록</h2><table><tr><th>상대 경로</th><th>바이트</th><th>종류</th><th>수정일 (시차 포함)</th><th>생성일 (시차 포함)</th></tr>")
    for entry in files[:2000]:
        kind = "응용 프로그램" if entry.get("kind") == "application" else "파일"
        parts.append(f"<tr><td>{_cell(entry.get('relative_path'))}</td><td>{_cell(entry.get('size'))}</td><td>{kind}</td><td>{_cell(_date(entry.get('modified')))}</td><td>{_cell(_date(entry.get('created')))}</td></tr>")
    parts.append("</table>")
    if len(files) > 2000:
        parts.append("<p class='notice'>HTML은 앞 2,000개 파일만 표시합니다. 전체 목록은 JSON을 이용하세요.</p>")
    parts.append("</section><small>개인 파일 내용·미리보기는 포함하지 않습니다. 로컬에서 생성된 보고서입니다.</small></body></html>")
    return "".join(parts)


def write_report(path: str | Path, report: dict) -> None:
    destination = Path(path)
    if destination.suffix.casefold() not in {".json", ".html", ".htm"}:
        raise ValueError("보고서 확장자는 .json 또는 .html이어야 합니다.")
    payload = render_html_report(report) if destination.suffix.casefold() in {".html", ".htm"} else json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    with destination.open("x", encoding="utf-8") as stream:
        stream.write(payload + "\n")
