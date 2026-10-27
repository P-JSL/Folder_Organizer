"""Small, stdlib-only update installer, bundled as a separate Windows executable.

Updates are copied to a sibling of the app first, then swapped only after the old
process exits. The old directory is retained. This module never deletes a backup.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

APP_ID = "P-JSL/Folder_Organizer"
APP_DIRECTORY = "FolderOrganizer"
APP_EXECUTABLE = "FolderOrganizer.exe"
HELPER_EXECUTABLE = "FolderOrganizerUpdater.exe"
MAX_MANIFEST_FILES = 15_000
MAX_PLAN_BYTES = 5 * 1024 * 1024
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
_VERSION = re.compile(r"(?:v)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class UpdateError(RuntimeError):
    """An update could not be safely completed; the original is preserved."""


def check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateError("업데이트 확인 또는 다운로드를 취소했습니다. 기존 앱은 유지됩니다.")


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 48:
        raise UpdateError("올바르지 않은 업데이트 버전입니다.")
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError("안정 버전 X.Y.Z 형식만 업데이트할 수 있습니다.")
    return tuple(int(part) for part in match.groups())


def storage_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise UpdateError("LOCALAPPDATA 위치를 찾을 수 없습니다.")
        return Path(base).absolute() / APP_DIRECTORY / "updates"
    # Non-Windows paths support tests and manual inspection, not auto-application.
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_DIRECTORY / "updates"


def plain_path(path: str | Path, *, must_exist: bool = True) -> Path:
    """Reject links/junctions in every existing component, including parents."""
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise UpdateError("업데이트 경로는 정규화된 절대 경로여야 합니다.")
    if sys.platform == "win32" and candidate.drive.startswith("\\\\"):
        raise UpdateError("네트워크 공유 경로에는 자동 업데이트를 적용하지 않습니다.")
    for component in reversed((candidate, *candidate.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UpdateError("링크 또는 재분석 지점이 포함된 업데이트 경로입니다.")
    if must_exist and not candidate.exists():
        raise UpdateError(f"업데이트 경로가 없습니다: {candidate}")
    return candidate


def safe_relative(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise UpdateError("압축 파일의 경로가 올바르지 않습니다.")
    parts = value.split("/")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in parts:
        if (not part or part in {".", ".."} or len(part) > 240 or any(ch in part for ch in ':?*<>|"')
                or part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
                or any(ord(ch) < 32 for ch in part)):
            raise UpdateError("안전하지 않은 압축 파일 경로입니다.")
    result = Path(*parts)
    if result.is_absolute():
        raise UpdateError("절대 경로는 업데이트에 사용할 수 없습니다.")
    return result


def file_hash(path: Path, cancel: threading.Event | None = None) -> str:
    check_cancel(cancel)
    plain_path(path)
    if not path.is_file():
        raise UpdateError("업데이트 파일이 일반 파일이 아닙니다.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancel(cancel)
            digest.update(chunk)
    return digest.hexdigest()


def read_marker(directory: str | Path) -> dict:
    directory = plain_path(directory)
    marker = plain_path(directory / "version.json")
    if not marker.is_file() or marker.stat().st_size > 4096:
        raise UpdateError("앱의 version.json 표식을 확인할 수 없습니다.")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise UpdateError("앱 버전 표식이 손상되었습니다.") from exc
    if not isinstance(value, dict) or value.get("app_id") != APP_ID:
        raise UpdateError("Folder Organizer 설치 폴더가 아닙니다.")
    version_tuple(value.get("version"))
    if not plain_path(directory / APP_EXECUTABLE).is_file():
        raise UpdateError("앱 실행 파일을 찾을 수 없습니다.")
    if not plain_path(directory / HELPER_EXECUTABLE).is_file():
        raise UpdateError("업데이트 도우미를 찾을 수 없습니다.")
    if not plain_path(directory / "_internal").is_dir():
        raise UpdateError("완전한 portable 앱 폴더가 아닙니다.")
    return value


def validate_install_contents(directory: Path) -> None:
    """Only an app-only portable directory may be replaced as a whole."""
    allowed = {APP_EXECUTABLE, HELPER_EXECUTABLE, "_internal", "version.json",
               "README.md", "CHANGELOG.md", "THIRD_PARTY.md", "licenses", "LICENSE", "LICENSE.txt"}
    if {entry.name for entry in directory.iterdir()} - allowed:
        raise UpdateError("앱 폴더에 개인 파일 또는 추가 파일이 있어 자동 교체하지 않습니다. 별도 폴더의 portable 앱을 사용하세요.")


def manifest(directory: Path, cancel: threading.Event | None = None) -> dict[str, str]:
    check_cancel(cancel)
    directory = plain_path(directory)
    files: dict[str, str] = {}
    total = 0
    for parent, directories, names in os.walk(directory, followlinks=False):
        check_cancel(cancel)
        for name in directories:
            plain_path(Path(parent) / name)
        for name in names:
            path = plain_path(Path(parent) / name)
            rel = path.relative_to(directory).as_posix()
            safe_relative(rel)
            total += path.stat().st_size
            if total > MAX_UNPACKED_BYTES or len(files) >= MAX_MANIFEST_FILES:
                raise UpdateError("업데이트 파일 수 또는 크기 제한을 초과했습니다.")
            files[rel] = file_hash(path, cancel=cancel)
    return files


def verify_manifest(directory: Path, files: dict[str, str], cancel: threading.Event | None = None) -> None:
    check_cancel(cancel)
    if not isinstance(files, dict) or not files or len(files) > MAX_MANIFEST_FILES:
        raise UpdateError("업데이트 파일 목록이 올바르지 않습니다.")
    for relative, digest in files.items():
        safe_relative(relative)
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise UpdateError("업데이트 파일 해시가 올바르지 않습니다.")
    actual = manifest(directory, cancel=cancel)
    if actual != files:
        raise UpdateError("대기 중인 업데이트 파일이 변경되었습니다. 다시 다운로드해야 합니다.")


def _read_json(path: Path) -> dict:
    plain_path(path)
    if not path.is_file() or path.stat().st_size > MAX_PLAN_BYTES:
        raise UpdateError("업데이트 계획 파일이 올바르지 않습니다.")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise UpdateError("업데이트 계획 파일을 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise UpdateError("업데이트 계획이 올바르지 않습니다.")
    return value


def validate_plan(plan: dict, *, check_install: bool = True, cancel: threading.Event | None = None) -> tuple[Path, Path, Path]:
    check_cancel(cancel)
    if not isinstance(plan, dict) or plan.get("schema") != 1 or plan.get("app_id") != APP_ID:
        raise UpdateError("지원하지 않는 업데이트 계획입니다.")
    identifier = plan.get("id")
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise UpdateError("업데이트 작업 ID가 올바르지 않습니다.")
    for name in ("plan_path", "install_dir", "stage_dir"):
        if not isinstance(plan.get(name), str):
            raise UpdateError("업데이트 계획 경로가 올바르지 않습니다.")
    expected_root = plain_path(storage_root(), must_exist=True)
    work = plain_path(expected_root / identifier)
    plan_file = plain_path(Path(plan.get("plan_path", "")))
    if plan_file != work / "plan.json" or _read_json(plan_file) != plan:
        raise UpdateError("저장된 업데이트 계획과 일치하지 않습니다.")
    install = plain_path(Path(plan.get("install_dir", "")))
    stage = plain_path(Path(plan.get("stage_dir", "")))
    if stage != work / "payload" / APP_DIRECTORY:
        raise UpdateError("업데이트 대기 폴더가 올바르지 않습니다.")
    if install == expected_root or install in expected_root.parents or expected_root in install.parents:
        raise UpdateError("앱과 업데이트 저장소 경로가 겹칩니다.")
    if install.parent == install or not install.is_dir():
        raise UpdateError("앱 설치 위치가 올바르지 않습니다.")
    old = version_tuple(plan.get("original_version"))
    new = version_tuple(plan.get("version"))
    if new <= old:
        raise UpdateError("이전 버전 또는 같은 버전으로 자동 교체하지 않습니다.")
    pid = plan.get("parent_pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise UpdateError("앱 프로세스 정보가 올바르지 않습니다.")
    if not isinstance(plan.get("archive_sha256"), str) or not _DIGEST.fullmatch(plan["archive_sha256"]):
        raise UpdateError("업데이트 압축 파일 해시가 올바르지 않습니다.")
    if read_marker(stage)["version"] != plan["version"]:
        raise UpdateError("대기 중인 앱 버전이 일치하지 않습니다.")
    if check_install and read_marker(install)["version"] != plan["original_version"]:
        raise UpdateError("설치 버전이 이미 바뀌었습니다. 업데이트를 다시 확인하세요.")
    validate_install_contents(install)
    validate_install_contents(stage)
    verify_manifest(stage, plan.get("files"), cancel=cancel)
    return install, stage, work


def load_plan(path: str | Path) -> dict:
    path = plain_path(path)
    result = _read_json(path)
    validate_plan(result)
    return result


def wait_for_process(pid: int, timeout: float = 180.0) -> None:
    """Wait without terminating the app. A timeout leaves the installed app alone."""
    deadline = time.monotonic() + timeout
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no PID
                return
            raise UpdateError("실행 중인 앱의 종료를 확인할 수 없습니다.")
        try:
            while time.monotonic() < deadline:
                outcome = kernel.WaitForSingleObject(handle, 500)
                if outcome == 0:
                    return
                if outcome != 258:
                    raise UpdateError("앱 프로세스 대기 중 오류가 발생했습니다.")
        finally:
            kernel.CloseHandle(handle)
    else:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError as exc:
                raise UpdateError("앱 프로세스 종료를 확인할 권한이 없습니다.") from exc
            time.sleep(0.25)
    raise UpdateError("앱이 종료되지 않아 업데이트를 적용하지 않았습니다.")


def _rename_with_retry(source: Path, target: Path, attempts: int = 20) -> None:
    for number in range(attempts):
        plain_path(source)
        plain_path(target, must_exist=False)
        if target.exists():
            raise UpdateError("교체 대상 경로가 이미 존재합니다.")
        try:
            source.rename(target)
            return
        except (PermissionError, OSError):
            if number == attempts - 1:
                raise
            time.sleep(0.5)


def _write_result(work: Path, result: dict) -> None:
    # A diagnostic only. No paths from this result are used for future mutations.
    for path in (work / "result.json", storage_root() / "last-result.json"):
        try:
            plain_path(path, must_exist=False)
            temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
            plain_path(temporary, must_exist=False)
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except (OSError, UpdateError):
            pass


def _launch_app(install: Path) -> None:
    read_marker(install)
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(install / APP_EXECUTABLE)], cwd=install, close_fds=True, **options)


def apply_update(plan: dict, *, wait: bool = True, restart: bool = True) -> dict:
    """Apply a verified staged app. Unit tests use wait=False, restart=False."""
    install, stage, work = validate_plan(plan)
    backup = install.with_name(install.name + ".previous-" + plan["id"])
    incoming = install.with_name("." + install.name + ".update-" + plan["id"])
    plain_path(backup, must_exist=False)
    plain_path(incoming, must_exist=False)
    if backup.exists() or incoming.exists():
        raise UpdateError("이 작업의 백업 또는 임시 교체 폴더가 이미 있습니다.")
    if not os.access(install.parent, os.W_OK):
        raise UpdateError("앱 폴더에 쓰기 권한이 없습니다. 쓰기 가능한 위치에 압축을 푸세요.")
    old_moved = False
    activated = False
    try:
        if wait:
            wait_for_process(plan["parent_pid"])
        validate_plan(plan)
        # Copy to the same volume first, then hash again, allowing AppData and app
        # to reside on different volumes without moving away the only good copy.
        shutil.copytree(stage, incoming, symlinks=False)
        verify_manifest(incoming, plan["files"])
        if read_marker(incoming)["version"] != plan["version"]:
            raise UpdateError("교체 파일의 버전이 일치하지 않습니다.")
        if read_marker(install)["version"] != plan["original_version"]:
            raise UpdateError("설치 버전이 바뀌어 업데이트를 중단했습니다.")
        validate_install_contents(install)
        _rename_with_retry(install, backup)
        old_moved = True
        try:
            _rename_with_retry(incoming, install)
            activated = True
        except BaseException:
            _rename_with_retry(backup, install)
            old_moved = False
            raise
        result = {"status": "updated", "version": plan["version"], "backup": str(backup), "details": "업데이트를 적용했습니다. 이전 앱 폴더는 백업으로 보관했습니다."}
        _write_result(work, result)
        if restart:
            try:
                _launch_app(install)
            except (OSError, UpdateError):
                result["details"] += " 자동 재시작에 실패했습니다. 앱을 직접 실행하세요."
                _write_result(work, result)
        return result
    except Exception as exc:
        result = {"status": "failed", "version": plan["version"], "details": str(exc), "backup": str(backup) if old_moved else None}
        _write_result(work, result)
        if old_moved and not activated:
            result["details"] += f" 이전 앱은 {backup}에 보관되어 있습니다."
        raise UpdateError(result["details"]) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply a verified Folder Organizer update")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--no-restart", action="store_true", help="Apply the update without relaunching the app (validation/managed use)")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        apply_update(plan, restart=not args.no_restart)
        return 0
    except (UpdateError, OSError, ValueError) as exc:
        # Frozen windowed builds have no console. Persist an actionable error if
        # the expected managed plan folder is available, never arbitrary paths.
        try:
            path = Path(args.plan)
            root = plain_path(storage_root())
            if path.name == "plan.json" and path.parent.parent == root and _ID.fullmatch(path.parent.name):
                _write_result(plain_path(path.parent), {"status": "failed", "details": str(exc)})
        except (UpdateError, OSError):
            pass
        if sys.stderr:
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
