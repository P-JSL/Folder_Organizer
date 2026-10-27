"""Read-only automation interface. File changes use the desktop confirmation UI."""
from __future__ import annotations

import argparse
import csv
import json
import sys
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
    args = parser.parse_args(argv)
    from .scanner import scan_folder
    from .matching import find_duplicates, find_similar
    try:
        result = scan_folder(args.folder, recursive=not args.no_recursive, include_hidden=args.hidden)
        output = {
            "root": result.root,
            "total_size": result.total_size,
            "files": [entry.to_dict() for entry in result.files],
            "warnings": result.warnings,
            "cancelled": result.cancelled,
        }
        if args.duplicates:
            output["duplicates"] = [group.to_dict() for group in find_duplicates(result.files)]
        if args.similar:
            output["similar"] = [group.to_dict() for group in find_similar(result.files)]
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
        serialized = json.dumps(output, ensure_ascii=False, indent=2)
        if args.json_path:
            with args.json_path.open("x", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
        if sys.stdout is not None:
            print(serialized)
        return 0
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
