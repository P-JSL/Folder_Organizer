"""Exercise only changed 0.8 Windows paths in the two exact frozen packages.

The already-passed extended suites, old image fixtures and old benchmarks are
not repeated. All CLI reports, rendered GUI captures and process logs remain
available when a later assertion fails.
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

from release_v080_fixtures import create_fixture, normalize_report, run_cli_checks, verify_sources
from windows_python_bundle_v070 import capture_window, offline_updates

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.0"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def artifact(path: Path) -> dict:
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}


def save(path: Path, record: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".v080-evidence-",
                                         suffix=".tmp", encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(record, stream, indent=2, ensure_ascii=False)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def produced_artifacts(output: Path, receipt: Path) -> list[dict]:
    return [{**artifact(path), "file": path.relative_to(output).as_posix()}
            for path in sorted(output.rglob("*"))
            if path.is_file() and path != receipt and not path.name.startswith(".v080-evidence-")]


def window_for_process(process: subprocess.Popen, prefix: str, timeout: float = 15) -> int:
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.EnumWindows.restype = wintypes.BOOL
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Actual GUI process exited before capture ({process.returncode})")
        found = []
        @callback_type
        def visit(hwnd, _parameter):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process.pid and user.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(1024)
                user.GetWindowTextW(hwnd, title, len(title))
                if title.value.startswith(prefix):
                    found.append(int(hwnd))
            return True
        user.EnumWindows(visit, 0)
        if found:
            return found[0]
        time.sleep(.05)
    raise RuntimeError(f"The actual GUI has no visible expected multi-root window: {prefix}")


def body_colors(image: Image.Image) -> int:
    # Exclude the title bar and borders: a black GPU surface must not pass just
    # because Windows painted antialiased caption text and close buttons.
    body = image.crop((16, 64, image.width - 16, image.height - 16))
    colors = body.resize((160, 100)).getcolors(16000)
    return len(colors) if colors is not None else 16001


def capture(hwnd: int, destination: Path) -> dict:
    try:
        result = capture_window(hwnd, destination)
        with Image.open(destination) as image:
            count = body_colors(image)
        if count < 16:
            raise RuntimeError("PrintWindow lacks rendered multi-folder body content")
        return {**result, "sampled_body_colors": count, "method": "PrintWindow"}
    except (OSError, RuntimeError):
        # GPU swapchains can omit pixels from PrintWindow. Capture the visible
        # composed window instead, still using its verified process-owned HWND.
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        user.BringWindowToTop.argtypes = [wintypes.HWND]
        user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user.BringWindowToTop(hwnd); user.SetForegroundWindow(hwnd)
        time.sleep(.3)
        rectangle = wintypes.RECT()
        if not user.GetWindowRect(hwnd, ctypes.byref(rectangle)):
            raise ctypes.WinError(ctypes.get_last_error())
        bbox = (rectangle.left, rectangle.top, rectangle.right, rectangle.bottom)
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        count = body_colors(image)
        if image.width < 640 or image.height < 400 or count < 16:
            raise RuntimeError("Composed GPU window capture lacks rendered multi-folder content")
        image.save(destination)
        return {"width": image.width, "height": image.height, "sampled_body_colors": count,
                "capture": destination.name, "native_visible_window": True,
                "method": "composed desktop window rectangle"}


def run_gui(engine: str, program: Path, fixture, output: Path, reference: dict,
            record: dict, receipt: Path) -> None:
    report = output / f"{engine}_MULTIROOT_GUI.json"
    screenshot = output / f"{engine}_MULTIROOT_GUI.png"
    command = [str(program), "--multiroot-smoke"]
    environment = {**os.environ, "QT_QPA_PLATFORM": "windows",
        "APPDATA": str(fixture.base.parent / f"{engine}-appdata"),
        "LOCALAPPDATA": str(fixture.base.parent / f"{engine}-localappdata"),
        "FOLDER_ORGANIZER_MULTI_SMOKE_ROOTS": json.dumps([str(path) for path in fixture.roots], ensure_ascii=False),
        "FOLDER_ORGANIZER_MULTI_SMOKE_EXCLUDES": '["*.skip"]',
        "FOLDER_ORGANIZER_MULTI_SMOKE_REPORT": str(report)}
    proof = {"command": command, "exit_code": None}
    record["gui"][engine] = proof
    save(receipt, record)
    stdout_path, stderr_path = output / f"{engine}_GUI.stdout.txt", output / f"{engine}_GUI.stderr.txt"
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
                    raise RuntimeError(f"{engine} real multi-folder GUI failed ({process.returncode})")
                time.sleep(.05)
            if value is None:
                raise RuntimeError(f"{engine} multi-folder GUI did not export its real worker result")
            if (value.get("app_version") != VERSION or value.get("schema") != "folder-organizer.multiroot.v1"
                    or value.get("action_authority") is not False or value.get("complete") is not True):
                raise ValueError(f"{engine} actual GUI did not complete the expected read-only analysis")
            normalized = normalize_report(value, fixture)
            for key in ("files", "groups"):
                if normalized[key] != reference[key]:
                    raise ValueError(f"{engine} GUI {key} differs from the verified packaged CLI fixture")
            prefix = "여러 폴더 통합 분석" if engine == "Python" else "Folder Organizer Rust"
            hwnd = window_for_process(process, prefix)
            time.sleep(.6)
            proof["capture"] = {**capture(hwnd, screenshot), **artifact(screenshot)}
            proof["report"] = artifact(report)
            save(receipt, record)
            Path(str(report) + ".capture-done").write_text("actual native window retained\n", encoding="utf-8")
            process.wait(timeout=15)
            if process.returncode:
                raise RuntimeError(f"{engine} real multi-folder GUI exit failed ({process.returncode})")
        finally:
            if process.poll() is None:
                process.terminate(); process.wait(timeout=15)
            proof["exit_code"] = process.returncode
    proof["stdout"] = artifact(stdout_path); proof["stderr"] = artifact(stderr_path)
    save(receipt, record)


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("0.8 package validation requires native Windows")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "review-results")
    args = parser.parse_args()
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    receipt = output / "WINDOWS_RELEASE_V080.json"
    if any(output.iterdir()):
        raise ValueError("Refusing to mix or overwrite prior Windows package evidence")
    record = {"schema": "folder-organizer.windows-release.v080", "version": VERSION,
              "status": "running", "source_commit": os.environ.get("GITHUB_SHA"),
              "validation_source_commit": os.environ.get("GITHUB_SHA"),
              "started_at": datetime.now(timezone.utc).isoformat(), "program_sha256": {},
              "gui": {}, "previously_passed_checks": "skipped; no full old suites or old packaging fixtures"}
    save(receipt, record)
    try:
        manifest = json.loads((ROOT / "release-build/BUILD_INFO.json").read_text(encoding="utf-8"))
        expected_source = os.environ.get("FOLDER_ORGANIZER_BUILD_SOURCE", os.environ.get("GITHUB_SHA"))
        if manifest.get("version") != VERSION or manifest.get("source_commit") != expected_source:
            raise ValueError("Extracted release receipt has another version or compiled source")
        record["build_source_commit"] = manifest["source_commit"]
        for package in manifest["packages"].values():
            directory = ROOT / "dist" / package["directory"]
            marker = json.loads((directory / "version.json").read_text(encoding="utf-8"))
            if marker.get("version") != VERSION or marker.get("app_id") != package["app_id"]:
                raise ValueError("The actual package marker differs from its formal build")
            for name, expected_hash in package["program_sha256"].items():
                actual_hash = digest(directory / name)
                if actual_hash != expected_hash:
                    raise ValueError("An extracted executable differs from the exact formal ZIP")
                record["program_sha256"][name] = actual_hash
        expected_names = {"FolderOrganizer.exe", "FolderOrganizerUpdater.exe", "FolderOrganizerRust.exe",
                          "FolderOrganizerRustCLI.exe", "FolderOrganizerRustUpdater.exe"}
        if set(record["program_sha256"]) != expected_names:
            raise ValueError("Both formal editions must contain the expected five programs")
        save(receipt, record)
        with tempfile.TemporaryDirectory(prefix="organizer-formal-v080-") as temporary:
            fixture = create_fixture(Path(temporary))
            commands = {"Python": [str(ROOT / "dist/FolderOrganizer/FolderOrganizer.exe"), "--cli"],
                        "Rust": [str(ROOT / "dist/FolderOrganizerRust/FolderOrganizerRustCLI.exe")]}
            record["cli"] = run_cli_checks(commands, output, fixture)
            reference_path = output / record["cli"]["reference_report"]
            reference = normalize_report(json.loads(reference_path.read_text(encoding="utf-8")), fixture)
            save(receipt, record)
            with offline_updates():
                for engine, executable in (("Python", ROOT / "dist/FolderOrganizer/FolderOrganizer.exe"),
                                           ("Rust", ROOT / "dist/FolderOrganizerRust/FolderOrganizerRust.exe")):
                    run_gui(engine, executable, fixture, output, reference, record, receipt)
            verify_sources(fixture)
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
