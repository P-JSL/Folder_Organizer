"""Read-only automation interface. File changes use the desktop confirmation UI."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path


def _print_json(value: dict) -> None:
    """Keep Unicode report data usable even through legacy Windows pipes."""
    if sys.stdout is None:
        return
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    try:
        serialized.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
    except UnicodeEncodeError:
        serialized = json.dumps(value, ensure_ascii=True, indent=2)
    print(serialized)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Folder Organizer 로컬 분석 (파일 변경 없음)")
    parser.add_argument("folder", type=Path, help="분석할 폴더")
    parser.add_argument("--add-folder", action="append", default=[], type=Path,
                        help="여러 폴더 읽기 전용 통합 분석에 추가할 폴더 (반복 가능)")
    parser.add_argument("--workers", type=int, default=2, choices=range(1, 5),
                        help="여러 폴더 탐색의 최대 동시 작업 수 (1~4, 같은 장치는 직렬)")
    parser.add_argument("--json-multiroot", type=Path,
                        help="읽기 전용 다중 폴더 v1 보고서 (기존 단일 폴더 형식과 구분)")
    parser.add_argument("--duplicates", action="store_true", help="SHA-256 및 실제 내용 비교")
    parser.add_argument("--similar", action="store_true", help="이름/텍스트/이미지 유사 후보")
    parser.add_argument("--hidden", action="store_true", help="숨김 파일 포함")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--csv", type=Path, help="새 CSV 파일로 내보내기 (덮어쓰기 금지)")
    parser.add_argument("--json", dest="json_path", type=Path, help="분석 결과를 새 JSON 파일로 저장")
    parser.add_argument("--html", dest="html_path", type=Path, help="읽기용 HTML 보고서를 새 파일로 저장")
    parser.add_argument("--json-common", type=Path, help="Python·Rust 공통 v1 JSON (기존 형식 유지)")
    parser.add_argument("--csv-common", type=Path, help="Python·Rust 공통 컬럼 CSV")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN", help="제외할 상대 경로/이름 패턴 (여러 번 지정 가능)")
    parser.add_argument("--min-size", type=int, default=0, metavar="BYTES")
    parser.add_argument("--max-size", type=int, default=None, metavar="BYTES")
    parser.add_argument("--similar-method", action="append", choices=("name", "text", "image"), help="유사 비교 근거 (기본 모두)")
    parser.add_argument("--name-threshold", type=float, default=0.82)
    parser.add_argument("--text-threshold", type=float, default=0.90)
    parser.add_argument("--image-threshold", type=float, default=0.90)
    parser.add_argument("--max-pairs", type=int, default=25_000)
    args = parser.parse_args(argv)
    from .scanner import scan_folder
    from .matching import AnalysisSession, find_duplicates, find_similar
    from .reporting import build_report, build_common_report, write_common_csv, write_report
    try:
        if args.add_folder or args.json_multiroot:
            if args.similar or any((args.csv, args.json_path, args.html_path, args.json_common, args.csv_common)):
                raise ValueError("다중 폴더 모드는 파일 조회·완전 중복만 지원합니다. 내보내기는 --json-multiroot를 사용하세요.")
            from .multiroot import build_multiroot_report, scan_folders, write_multiroot_report
            result = scan_folders([args.folder, *args.add_folder], recursive=not args.no_recursive,
                                  include_hidden=args.hidden, exclude_patterns=args.exclude,
                                  min_size=args.min_size, max_size=args.max_size,
                                  workers=args.workers, duplicates=args.duplicates)
            if args.json_multiroot:
                write_multiroot_report(result, args.json_multiroot)
                _print_json({"schema": "folder-organizer.multiroot.v1", "action_authority": False,
                             "roots": len(result.roots), "files": len(result.files),
                             "exact_groups": len(result.groups), "complete": result.complete,
                             "cancelled": result.cancelled})
            else:
                _print_json(build_multiroot_report(result))
            return 0 if result.complete and not result.cancelled else 2
        started = time.perf_counter()
        result = scan_folder(args.folder, recursive=not args.no_recursive, include_hidden=args.hidden,
                             exclude_patterns=args.exclude, min_size=args.min_size, max_size=args.max_size)
        phases = {"inventory": time.perf_counter() - started}
        duplicate_groups = None
        duplicate_diagnostics = {}
        similar_groups = None
        diagnostics = {}
        session = AnalysisSession()
        if args.duplicates:
            started = time.perf_counter()
            duplicate_groups = find_duplicates(result.files, report=duplicate_diagnostics, session=session)
            phases["duplicates"] = time.perf_counter() - started
        if args.similar:
            started = time.perf_counter()
            similar_groups = find_similar(result.files, methods=tuple(args.similar_method or ("name", "text", "image")),
                                  name_threshold=args.name_threshold, text_threshold=args.text_threshold,
                                  image_threshold=args.image_threshold, max_pairs=args.max_pairs, report=diagnostics,
                                  verified_duplicates=duplicate_groups, session=session)
            phases["similarity"] = time.perf_counter() - started
        native = bool(args.json_path or args.html_path or not (args.json_common or args.csv_common))
        output = build_report(result, duplicates=duplicate_groups, similar=similar_groups,
                              comparison_report=diagnostics if args.similar else None,
                              duplicate_analysis=duplicate_diagnostics if args.duplicates else None) if native else None
        if args.json_common:
            common = build_common_report(result, duplicates=duplicate_groups,
                                         duplicate_analysis=duplicate_diagnostics, similar=similar_groups,
                                         comparison_report=diagnostics, phase_seconds=phases)
            write_report(args.json_common, common)
        if args.csv_common:
            write_common_csv(args.csv_common, result)
        if args.csv:
            columns = ["relative_path", "size", "modified", "created", "created_source", "kind"]
            with args.csv.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for entry in result.files:
                    row = entry.to_dict()
                    # Spreadsheet formulas are data, even for attacker-controlled filenames.
                    if row["relative_path"].startswith(("=", "+", "-", "@", "\t", "\r")):
                        row["relative_path"] = "'" + row["relative_path"]
                    writer.writerow(row)
        partial = (result.cancelled or bool(result.warnings)
                   or (args.duplicates and not duplicate_diagnostics.get("complete", True))
                   or diagnostics.get("cancelled", False) or diagnostics.get("skipped_count", 0) > 0)
        printable = output if output is not None else {
            "files": len(result.files), "exact_groups": len(duplicate_groups or []), "partial": bool(partial)}
        if args.json_path:
            write_report(args.json_path, output)
        if args.html_path:
            write_report(args.html_path, output)
        _print_json(printable)
        return 2 if partial else 0
    except (OSError, ValueError) as exc:
        if sys.stderr is not None:
            message = f"오류: {exc}"
            encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
            print(message.encode(encoding, errors="backslashreplace").decode(encoding), file=sys.stderr)
        return 2
