"""Read-only automation interface. File changes use the desktop confirmation UI."""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Folder Organizer 로컬 분석 (파일 변경 없음)")
    parser.add_argument("folder", type=Path, help="분석할 폴더")
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
    from .matching import find_duplicates, find_similar
    from .reporting import build_report, build_common_report, write_common_csv, write_report
    try:
        started = time.perf_counter()
        result = scan_folder(args.folder, recursive=not args.no_recursive, include_hidden=args.hidden,
                             exclude_patterns=args.exclude, min_size=args.min_size, max_size=args.max_size)
        phases = {"inventory": time.perf_counter() - started}
        duplicate_groups = None
        duplicate_diagnostics = {}
        similar_groups = None
        diagnostics = {}
        if args.duplicates:
            started = time.perf_counter()
            duplicate_groups = find_duplicates(result.files, report=duplicate_diagnostics)
            phases["duplicates"] = time.perf_counter() - started
        if args.similar:
            started = time.perf_counter()
            similar_groups = find_similar(result.files, methods=tuple(args.similar_method or ("name", "text", "image")),
                                  name_threshold=args.name_threshold, text_threshold=args.text_threshold,
                                  image_threshold=args.image_threshold, max_pairs=args.max_pairs, report=diagnostics,
                                  verified_duplicates=duplicate_groups)
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
        serialized = json.dumps(printable, ensure_ascii=False, indent=2)
        if args.json_path:
            write_report(args.json_path, output)
        if args.html_path:
            write_report(args.html_path, output)
        if sys.stdout is not None:
            # Redirected Windows pipes can retain a legacy code page even when
            # report files are UTF-8. JSON escapes preserve Unicode losslessly.
            try:
                serialized.encode(getattr(sys.stdout, "encoding", None) or "utf-8")
            except UnicodeEncodeError:
                serialized = json.dumps(printable, ensure_ascii=True, indent=2)
            print(serialized)
        return 2 if partial else 0
    except (OSError, ValueError) as exc:
        if sys.stderr is not None:
            message = f"오류: {exc}"
            encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
            print(message.encode(encoding, errors="backslashreplace").decode(encoding), file=sys.stderr)
        return 2
