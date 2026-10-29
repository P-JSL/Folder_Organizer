"""New Windows integration contract for the two newly changed editions.

This deliberately does not invoke the previous test suites or v0.5 smoke tests.
Only disposable files are read and new reports are generated.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def relative(path: str, root: Path) -> str:
    # Windows TEMP may use RUNNER~1 while the apps correctly return the long
    # canonical username. Resolve both aliases before comparing relative names.
    path = str(Path(path).resolve())
    root_path = str(root.resolve())
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    if root_path.startswith("\\\\?\\UNC\\"):
        root_path = "\\\\" + root_path[8:]
    elif root_path.startswith("\\\\?\\"):
        root_path = root_path[4:]
    return os.path.relpath(path, root_path).replace("\\", "/").casefold()


def contract() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Native Windows integration must run on Windows")
    python_app = ROOT / "dist" / "FolderOrganizer" / "FolderOrganizer.exe"
    rust_app = ROOT / "dist" / "FolderOrganizerRust" / "FolderOrganizerRust.exe"
    rust_cli = rust_app.with_name("FolderOrganizerRustCLI.exe")
    for directory, app_id in ((python_app.parent, "P-JSL/Folder_Organizer"),
                              (rust_app.parent, "P-JSL/Folder_Organizer.Rust")):
        marker = json.loads((directory / "version.json").read_text(encoding="utf-8"))
        assert marker == {"app_id": app_id, "version": "0.6.0"}, marker
    if '--gui-only' in sys.argv:
        gui_startup(rust_app)
        print('Native Rust GUI rendered and exited; previously passed CLI/data checks reused.')
        return
    version = subprocess.check_output([str(rust_cli), "--version"], text=True, encoding="utf-8")
    assert "Rust 0.6.0" in version
    with tempfile.TemporaryDirectory(prefix="organizer-two-editions-") as temporary:
        scratch = Path(temporary)
        folder = scratch / "테스트 자료"
        folder.mkdir()
        data = {
            "원본.txt": "같은 내용으로 안전하게 비교합니다.\n".encode(),
            "하위/복사본.TXT": "같은 내용으로 안전하게 비교합니다.\n".encode(),
            "하위/수정본.txt": "같은 내용으로 안전하게 비교합니다.\n추가 문장입니다.".encode(),
            "빈 파일.txt": b"",
            "설치 예시.exe": b"This fixture is data and is never executed.",
            "제외.skip": b"excluded by the new common scope fixture",
            "#recycle/서버 휴지통.txt": b"never inventoried",
            "delete_files/삭제 보관.txt": b"never inventoried",
            ".folder-organizer/history/state.json": b"never inventoried",
            ".folder-organizer-rust/history/state.json": b"never inventoried",
        }
        for name, body in data.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        original = {path.relative_to(folder).as_posix(): path.read_bytes()
                    for path in folder.rglob("*") if path.is_file()}
        reports = []
        for edition, command in (("Python", [str(python_app), "--cli"]),
                                 ("Rust", [str(rust_cli)])):
            output = scratch / (edition + ".json")
            subprocess.run(command + [str(folder), "--duplicates", "--similar", "--hidden",
                                      "--exclude", "*.skip", "--json", str(output)],
                           check=True, timeout=180)
            report = json.loads(output.read_text(encoding="utf-8"))
            assert report["app_version"] == "0.6.0"
            inventory = report.get("scan", report)
            entries = {item["relative_path"].replace("\\", "/").casefold():
                       (item["size"], item["kind"]) for item in inventory["files"]}
            groups = {frozenset(relative(path, folder) for path in group["paths"])
                      for group in report["duplicates"]}
            assert set(entries) == {"원본.txt", "하위/복사본.txt", "하위/수정본.txt", "빈 파일.txt", "설치 예시.exe"}, entries
            assert groups == {frozenset({"원본.txt", "하위/복사본.txt"})}, groups
            reports.append((entries, groups))
            print(json.dumps({"edition": edition, "files": len(entries),
                              "exact_groups": len(groups), "version": report["app_version"]}, ensure_ascii=False))
        assert reports[0] == reports[1], "The editions disagree on the new read-only fixture"
        assert {path.relative_to(folder).as_posix(): path.read_bytes()
                for path in folder.rglob("*") if path.is_file()} == original
        # New native egui startup, not a repeat of the old Python GUI check.
        gui_startup(rust_app)
    print("New two-edition Windows contract passed; previous tests/build checks were not rerun.")


def gui_startup(rust_app: Path) -> None:
    smoke = subprocess.run([str(rust_app), '--gui-smoke'], timeout=60,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env={**os.environ, 'FOLDER_ORGANIZER_RUST_SMOKE': '1'})
    assert smoke.returncode == 0, f'Native Rust GUI startup failed ({smoke.returncode}): {smoke.stderr}\n{smoke.stdout}'


if __name__ == "__main__":
    contract()
