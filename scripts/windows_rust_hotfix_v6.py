"""Build receipts and disposable checks for the Rust-only v0.6.0 reissue.

This does not build Python, run previously passed test suites, move a release
tag, or modify a published release. Publication follows artifact verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from package_rust import PROGRAMS, package

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "dist" / "FolderOrganizerRust"
REPOSITORY = "P-JSL/Folder_Organizer"
BASE_COMMIT = "1cceb715e94aee7462f03d0c7f3d775d91eb443b"
VERSION = "0.6.0"
ARCHIVE = "FolderOrganizer-Rust-Windows-x64.zip"


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def prepare() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Formal Windows package must be prepared on Windows")
    marker = json.loads((ROOT / "rust-app/version.json").read_text(encoding="utf-8"))
    if marker != {"app_id": REPOSITORY + ".Rust", "version": VERSION}:
        raise ValueError("This hotfix must preserve the Rust v0.6.0 app marker")
    if command("git", "rev-parse", "v0.6.0^{commit}") != BASE_COMMIT:
        raise ValueError("The original v0.6.0 release tag must remain unchanged")
    unchanged_python = command(
        "git", "diff", "--name-only", BASE_COMMIT, "HEAD", "--", "folder_organizer",
        "pyproject.toml", "version.json", "desktop_entry.py", "updater_entry.py",
    )
    if unchanged_python:
        raise ValueError("Rust-only reissue cannot include changed Python application code")
    source = command("git", "rev-parse", "HEAD")
    package(ROOT / "rust-app/target/x86_64-pc-windows-msvc/release", DESTINATION)
    receipt = {
        "repository": REPOSITORY,
        "edition": "Rust",
        "version": VERSION,
        "release_tag": "v0.6.0",
        "original_release_commit": BASE_COMMIT,
        "source_commit": source,
        "build_kind": "formal-rust-only-hotfix",
        "target": "x86_64-pc-windows-msvc",
        "rust_toolchain": "1.99.1",
        "workflow": ".github/workflows/rust-hotfix.yml",
        "workflow_run": os.environ.get("GITHUB_RUN_ID"),
        "workflow_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "program_sha256": {name: digest(DESTINATION / name) for name in PROGRAMS},
    }
    write_json(DESTINATION / "BUILD_INFO.json", receipt)
    write_json(ROOT / "Rust-BUILD_INFO.json", receipt)
    print(json.dumps(receipt))


def run_capture(arguments: list[str], *, timeout: int) -> subprocess.CompletedProcess:
    result = subprocess.run(arguments, cwd=ROOT, timeout=timeout, capture_output=True,
                            text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Native package command failed ({result.returncode}): "
                           f"{arguments[0]}\n{result.stdout}\n{result.stderr}")
    return result


def verify() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Packaged identity regression must run on native Windows")
    receipt = json.loads((DESTINATION / "BUILD_INFO.json").read_text(encoding="utf-8"))
    if receipt["source_commit"] != command("git", "rev-parse", "HEAD"):
        raise ValueError("Package source differs from the current build")
    for name, expected in receipt["program_sha256"].items():
        if digest(DESTINATION / name) != expected:
            raise ValueError(f"Native program changed after packaging: {name}")
    cli = str(DESTINATION / "FolderOrganizerRustCLI.exe")
    version = run_capture([cli, "--version"], timeout=30)
    if version.stdout.strip() != "Folder Organizer Rust 0.6.0":
        raise ValueError("Rust program version changed during same-version reissue")
    # Snapshot identity now tries a provider-compatible route. This new fixture
    # exercises that changed path through the actual release-mode console binary.
    with tempfile.TemporaryDirectory(prefix="organizer-identity-hotfix-") as temporary:
        scratch = Path(temporary)
        folder = scratch / "식별 정보 검사"
        folder.mkdir()
        files = {
            "첫 원본.txt": "파일 ID 호환성 수정 검증\n".encode("utf-8"),
            "하위/동일 사본.txt": "파일 ID 호환성 수정 검증\n".encode("utf-8"),
            "다른 내용.txt": "다른 내용과 다른 크기\n".encode("utf-8"),
        }
        for name, data in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        output = scratch / "분석 결과.json"
        result = run_capture([cli, str(folder), "--duplicates", "--json", str(output)], timeout=120)
        report = json.loads(output.read_text(encoding="utf-8"))
        if report["app_version"] != VERSION or len(report["scan"]["files"]) != len(files):
            raise ValueError("Release console did not finish the changed identity scan path")
        groups = report["duplicates"]
        if len(groups) != 1 or len(groups[0]["paths"]) != 2:
            raise ValueError("Release console lost the expected exact duplicate group")
        expected = {"첫 원본.txt", "동일 사본.txt"}
        if {Path(path).name for path in groups[0]["paths"]} != expected:
            raise ValueError("Release console returned an unexpected duplicate member")
        if any((folder / name).read_bytes() != data for name, data in files.items()):
            raise ValueError("Read-only hotfix analysis modified a source file")
        if "os error 87" in result.stderr:
            raise ValueError("Provider identity error 87 remains in native package output")
    gui = run_capture([str(DESTINATION / "FolderOrganizerRust.exe"), "--gui-smoke"], timeout=60)
    verification = {
        "edition": "Rust", "version": VERSION, "source_commit": receipt["source_commit"],
        "release_mode_identity_scan": {"files": len(files), "exact_groups": len(groups),
                                        "source_bytes_unchanged": True},
        "native_gui_exit_code": gui.returncode,
        "previously_passed_suites": "skipped",
        "python_build": "skipped; published Python asset is preserved",
    }
    write_json(ROOT / "Rust-VERIFICATION.json", verification)
    print(json.dumps(verification))


def archive() -> None:
    verification = json.loads((ROOT / "Rust-VERIFICATION.json").read_text(encoding="utf-8"))
    if verification["source_commit"] != command("git", "rev-parse", "HEAD"):
        raise ValueError("Hotfix verification source is stale")
    archive_path = ROOT / ARCHIVE
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(DESTINATION.rglob("*")):
            if path.is_symlink():
                raise ValueError("Package contains an unexpected link")
            if path.is_file():
                output.write(path, path.relative_to(DESTINATION.parent).as_posix())
    checksum = digest(archive_path)
    (ROOT / "SHA256SUMS-Rust.txt").write_text(f"{checksum}  {ARCHIVE}\n", encoding="utf-8")
    print(json.dumps({"archive": ARCHIVE, "sha256": checksum, "bytes": archive_path.stat().st_size}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("package", "verify", "archive"))
    arguments = parser.parse_args()
    {"package": prepare, "verify": verify, "archive": archive}[arguments.action]()
