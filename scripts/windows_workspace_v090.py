"""Check only the new 0.9 embedded main workspace in both formal packages.

No CLI, filesystem-engine regression suite, old native target, or previous GUI
driver is executed. The Win32 bitmap helper is reused only to photograph the
new main window owned by each newly launched process.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageGrab

from windows_python_bundle_v070 import capture_window, main_window

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.0"
SCHEMA = "folder-organizer-workspace-smoke-v1"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def artifact(path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}


def save(path: Path, record: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".v090-evidence-",
                                         suffix=".tmp", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def create_workspace_fixture(base: Path) -> tuple[list[Path], dict[str, str]]:
    roots = [base / "업무 문서", base / "다운로드"]
    for root in roots:
        root.mkdir()
    shared = "2026년 분기 보고서\n동일한 원본 내용입니다.\n".encode("utf-8") * 128
    entries = {
        roots[0] / "분기 보고서.txt": shared,
        roots[1] / "분기 보고서 복사.txt": shared,
        roots[0] / "프로젝트 메모.txt": "다음 주 회의 자료\n".encode("utf-8"),
        roots[0] / "매출.csv": b"month,total\n10,2700\n",
        roots[1] / "받은 문서.txt": "검토할 문서입니다.\n".encode("utf-8"),
        roots[1] / "일정.md": "# 일정\n- 자료 정리\n".encode("utf-8"),
    }
    for path, data in entries.items():
        path.write_bytes(data)
    return roots, {str(path): digest(path) for path in entries}


def verify_workspace_report(value: dict, edition: str, roots: list[Path]) -> None:
    if (value.get("schema") != SCHEMA or value.get("app_version") != VERSION
            or str(value.get("edition", "")).casefold() != edition.casefold()
            or value.get("main_workspace") is not True
            or value.get("action_authority") is not False or value.get("finished") is not True):
        raise ValueError(f"{edition} did not finish the new read-only main workspace")
    selected = value.get("checked_roots")
    if not isinstance(selected, list) or len(selected) != len(roots):
        raise ValueError(f"{edition} main workspace did not retain both checked folders")
    # Compare only real fixture directory identity. Resolve Windows 8.3 temporary
    # aliases, without reviving the old canonical-root or CLI checks.
    expected = {os.path.normcase(str(path.resolve())) for path in roots}
    actual = {os.path.normcase(str(Path(path).resolve())) for path in selected}
    if expected != actual:
        raise ValueError(f"{edition} workspace is showing another folder selection")
    if value.get("file_rows") != 6 or value.get("duplicate_groups") != 1:
        raise ValueError(f"{edition} new workspace does not display the six files and one duplicate group")
    snapshot = value.get("selected_snapshot")
    if not isinstance(snapshot, dict) or not {"name", "path", "size", "kind"} <= set(snapshot):
        raise ValueError(f"{edition} new file-properties panel has no selected snapshot")
    if not snapshot["name"] or not snapshot["path"] or not isinstance(snapshot["size"], int):
        raise ValueError(f"{edition} new file-properties snapshot is incomplete")
    if "scope_mode" in value and value["scope_mode"] != "global":
        raise ValueError(f"{edition} did not remain in the global read-only workspace")


def body_colors(image: Image.Image) -> int:
    body = image.crop((16, 64, image.width - 16, image.height - 16))
    colors = body.resize((160, 100)).getcolors(16000)
    return len(colors) if colors is not None else 16001


def capture_main(hwnd: int, destination: Path) -> dict:
    try:
        result = capture_window(hwnd, destination)
        with Image.open(destination) as image:
            count = body_colors(image)
        if count < 16:
            raise RuntimeError("Native capture has no rendered workspace body")
        return {**result, "sampled_body_colors": count, "method": "PrintWindow"}
    except (OSError, RuntimeError):
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.BringWindowToTop.argtypes = [wintypes.HWND]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.BringWindowToTop(hwnd)
        user.SetForegroundWindow(hwnd)
        time.sleep(.3)
        rectangle = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())
        image = ImageGrab.grab(bbox=(rectangle.left, rectangle.top, rectangle.right, rectangle.bottom),
                               all_screens=True)
        count = body_colors(image)
        if image.width < 900 or image.height < 500 or count < 16:
            raise RuntimeError("Composed capture has no rendered main workspace")
        image.save(destination)
        return {"width": image.width, "height": image.height, "sampled_body_colors": count,
                "capture": destination.name, "native_visible_window": True,
                "method": "composed desktop window rectangle"}


def run_workspace(edition: str, program: Path, roots: list[Path], output: Path,
                  record: dict, receipt: Path) -> None:
    report = output / f"{edition}_WORKSPACE_GUI.json"
    screenshot = output / f"{edition}_WORKSPACE_GUI.png"
    command = [str(program), "--workspace-smoke"]
    environment = {**os.environ, "QT_QPA_PLATFORM": "windows",
        "APPDATA": str(roots[0].parent / f"{edition}-appdata"),
        "LOCALAPPDATA": str(roots[0].parent / f"{edition}-localappdata"),
        "FOLDER_ORGANIZER_WORKSPACE_SMOKE_ROOTS": json.dumps([str(path) for path in roots], ensure_ascii=False),
        "FOLDER_ORGANIZER_WORKSPACE_SMOKE_REPORT": str(report)}
    proof = {"command": command, "exit_code": None}
    record["gui"][edition] = proof
    save(receipt, record)
    stdout_path = output / f"{edition}_WORKSPACE.stdout.txt"
    stderr_path = output / f"{edition}_WORKSPACE.stderr.txt"
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(command, cwd=program.parent, env=environment, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + 25
            value = None
            while time.monotonic() < deadline:
                if report.exists():
                    try:
                        value = json.loads(report.read_text(encoding="utf-8"))
                        break
                    except (OSError, ValueError):
                        pass
                if process.poll() is not None:
                    raise RuntimeError(f"{edition} main workspace exited before its UI receipt ({process.returncode})")
                time.sleep(.05)
            if value is None:
                raise RuntimeError(f"{edition} main workspace did not render its new panels in time")
            verify_workspace_report(value, edition, roots)
            hwnd = main_window(process, timeout=5)
            time.sleep(.6)
            proof["capture"] = {**capture_main(hwnd, screenshot), **artifact(screenshot)}
            proof["report"] = artifact(report)
            proof["checked_folders"] = len(value["checked_roots"])
            proof["file_rows"] = value["file_rows"]
            proof["duplicate_groups"] = value["duplicate_groups"]
            proof["main_workspace"] = value["main_workspace"]
            save(receipt, record)
            Path(str(report) + ".capture-done").write_text("actual native main workspace captured\n", encoding="utf-8")
            process.wait(timeout=15)
            if process.returncode:
                raise RuntimeError(f"{edition} new main workspace exit failed ({process.returncode})")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
            proof["exit_code"] = process.returncode
            proof["stdout"] = artifact(stdout_path)
            proof["stderr"] = artifact(stderr_path)
            save(receipt, record)


def produced_artifacts(output: Path, receipt: Path) -> list[dict]:
    return [artifact(path) for path in sorted(output.iterdir())
            if path.is_file() and path != receipt and not path.name.startswith(".v090-evidence-")]


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("The new main-workspace package check requires native Windows")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "workspace-results")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Refusing to overwrite earlier workspace UI evidence")
    receipt = output / "WINDOWS_WORKSPACE_V090.json"
    record = {"schema": "folder-organizer.windows-workspace.v090", "version": VERSION,
              "status": "running", "source_commit": os.environ.get("GITHUB_SHA"),
              "workflow_run": os.environ.get("GITHUB_RUN_ID"),
              "started_at": datetime.now(timezone.utc).isoformat(), "program_sha256": {}, "gui": {},
              "previous_checks": "all existing tests and old CLI/native/GUI drivers skipped",
              "scope": "new embedded main-workspace GUI only; package transfer identity"}
    save(receipt, record)
    try:
        manifest = json.loads((ROOT / "release-build/BUILD_INFO.json").read_text(encoding="utf-8"))
        if manifest.get("version") != VERSION or manifest.get("source_commit") != os.environ.get("GITHUB_SHA"):
            raise ValueError("Formal build belongs to another version or source")
        record["build_source_commit"] = manifest["source_commit"]
        for package in manifest["packages"].values():
            directory = ROOT / "dist" / package["directory"]
            for name, expected in package["program_sha256"].items():
                actual = digest(directory / name)
                if actual != expected:
                    raise ValueError("Formal executable transfer checksum differs: " + name)
                record["program_sha256"][name] = actual
        save(receipt, record)
        with tempfile.TemporaryDirectory(prefix="organizer-workspace-v090-") as temporary:
            roots, original = create_workspace_fixture(Path(temporary))
            for edition, executable in (("Python", ROOT / "dist/FolderOrganizer/FolderOrganizer.exe"),
                                         ("Rust", ROOT / "dist/FolderOrganizerRust/FolderOrganizerRust.exe")):
                run_workspace(edition, executable, roots, output, record, receipt)
            current = {str(path): digest(path) for root in roots for path in root.rglob("*") if path.is_file()}
            if current != original:
                raise ValueError("A read-only main workspace changed the disposable source files")
            record["source_bytes_unchanged"] = True
        record.update(status="passed", finished_at=datetime.now(timezone.utc).isoformat(),
                      produced_artifacts=produced_artifacts(output, receipt))
        save(receipt, record)
    except BaseException as error:
        record.update(status="failed", error={"type": type(error).__name__, "message": str(error)},
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      produced_artifacts=produced_artifacts(output, receipt))
        save(receipt, record)
        raise
    print(json.dumps(record, ensure_ascii=True))


if __name__ == "__main__":
    main()
