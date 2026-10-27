"""GitHub stable-release updater for the Windows portable distribution.

No personal folders or credentials are sent. HTTPS and SHA-256 verification stay
enabled. Development/source installs never replace their own files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from . import updater_helper as helper

UpdateError = helper.UpdateError
REPOSITORY = "P-JSL/Folder_Organizer"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
WINDOWS_ASSET = "FolderOrganizer-Windows-x64.zip"
CHECKSUM_ASSET = "SHA256SUMS.txt"
MAX_ARCHIVE_BYTES = 768 * 1024 * 1024
MAX_API_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 64 * 1024
MAX_ENTRY_BYTES = 512 * 1024 * 1024
ALLOWED_HOSTS = frozenset({"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"})


def _safe_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 16_384:
        raise UpdateError("업데이트 다운로드 주소가 올바르지 않습니다.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("업데이트 주소를 읽을 수 없습니다.") from exc
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.fragment
            or port not in {None, 443}):
        raise UpdateError("GitHub의 검증된 HTTPS 주소에서만 업데이트할 수 있습니다.")
    if parsed.hostname == "api.github.com" and not parsed.path.startswith(f"/repos/{REPOSITORY}/releases/"):
        raise UpdateError("다른 저장소의 업데이트 API는 사용할 수 없습니다.")
    if parsed.hostname == "github.com" and not parsed.path.startswith(f"/{REPOSITORY}/releases/"):
        raise UpdateError("다른 저장소의 배포 파일은 사용할 수 없습니다.")
    return value


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, *, limit: int, target: Path | None = None,
              cancel: threading.Event | None = None) -> bytes | None:
    helper.check_cancel(cancel)
    _safe_url(url)
    request = Request(url, headers={"User-Agent": "FolderOrganizer-Updater/1", "Accept": "application/vnd.github+json" if url == LATEST_RELEASE_API else "application/octet-stream"})
    opener = build_opener(_SafeRedirect())  # inherited proxy and verified CA trust
    deadline = time.monotonic() + 240
    output = None
    try:
        with opener.open(request, timeout=15) as response:
            helper.check_cancel(cancel)
            _safe_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > limit):
                raise UpdateError("업데이트 다운로드 크기 제한을 초과했습니다.")
            if target is not None:
                helper.plain_path(target, must_exist=False)
                output = target.open("xb")
            chunks = []
            count = 0
            while True:
                helper.check_cancel(cancel)
                if time.monotonic() > deadline:
                    raise UpdateError("업데이트 다운로드 대기 시간을 초과했습니다.")
                # read1 returns after one socket read rather than waiting to fill
                # a large buffer, making cancel/deadline checks responsive even
                # if a server sends data very slowly.
                read_chunk = getattr(response, "read1", response.read)
                chunk = read_chunk(64 * 1024)
                helper.check_cancel(cancel)
                if not chunk:
                    break
                count += len(chunk)
                if count > limit or time.monotonic() > deadline:
                    raise UpdateError("업데이트 다운로드 제한 또는 대기 시간을 초과했습니다.")
                if output:
                    output.write(chunk)
                else:
                    chunks.append(chunk)
            if length and count != int(length):
                raise UpdateError("업데이트 다운로드가 완전하지 않습니다.")
            if output:
                output.flush()
                os.fsync(output.fileno())
                return None
            return b"".join(chunks)
    except HTTPError as exc:
        if exc.code == 404 and url == LATEST_RELEASE_API:
            return None
        raise UpdateError(f"업데이트 서버에 접근할 수 없습니다 (HTTP {exc.code}). 네트워크 또는 GitHub 제한을 확인하세요.") from exc
    except (URLError, OSError) as exc:
        raise UpdateError("업데이트 파일을 받지 못했습니다. 네트워크 연결과 저장 공간을 확인하세요.") from exc
    finally:
        if output:
            output.close()


def _asset_url(tag: str, asset: str) -> str:
    helper.version_tuple(tag)
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{asset}"


def _validate_release(release: dict) -> dict:
    if not isinstance(release, dict):
        raise UpdateError("업데이트 정보가 올바르지 않습니다.")
    tag = release.get("tag_name")
    version = release.get("version")
    helper.version_tuple(tag)
    helper.version_tuple(version)
    if not tag.startswith("v") or version != tag[1:]:
        raise UpdateError("안정 버전 태그가 일치하지 않습니다.")
    for key, name in (("zip_url", WINDOWS_ASSET), ("checksum_url", CHECKSUM_ASSET)):
        if release.get(key) != _asset_url(tag, name):
            raise UpdateError("이 저장소의 올바른 배포 파일 주소가 아닙니다.")
    if release.get("release_url") != f"https://github.com/{REPOSITORY}/releases/tag/{tag}":
        raise UpdateError("업데이트 릴리스 주소가 일치하지 않습니다.")
    size = release.get("zip_size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise UpdateError("업데이트 압축 파일 크기가 올바르지 않습니다.")
    return release


def check_for_update(current_version: str, cancel: threading.Event | None = None) -> dict | None:
    """Return a newer stable release with both required Windows assets, or None."""
    helper.check_cancel(cancel)
    current = helper.version_tuple(current_version)
    options = {"cancel": cancel} if cancel is not None else {}
    data = _download(LATEST_RELEASE_API, limit=MAX_API_BYTES, **options)
    helper.check_cancel(cancel)
    if data is None:
        return None
    try:
        release = json.loads(data)
    except (ValueError, TypeError) as exc:
        raise UpdateError("업데이트 서버의 응답을 읽을 수 없습니다.") from exc
    if not isinstance(release, dict):
        raise UpdateError("업데이트 서버의 응답 형식이 올바르지 않습니다.")
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = release.get("tag_name")
    try:
        latest = helper.version_tuple(tag)
    except UpdateError:
        return None
    if not tag.startswith("v") or latest <= current:
        return None
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("새 버전에 배포 파일 목록이 없습니다.")
    selected = {}
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") not in {WINDOWS_ASSET, CHECKSUM_ASSET}:
            continue
        name = asset["name"]
        if name in selected:
            raise UpdateError("이름이 중복된 업데이트 배포 파일입니다.")
        if asset.get("browser_download_url") != _asset_url(tag, name):
            raise UpdateError("안전하지 않은 업데이트 배포 파일 주소입니다.")
        size = asset.get("size")
        limit = MAX_ARCHIVE_BYTES if name == WINDOWS_ASSET else MAX_CHECKSUM_BYTES
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= limit:
            raise UpdateError("배포 파일 크기를 확인할 수 없습니다.")
        selected[name] = asset
    if set(selected) != {WINDOWS_ASSET, CHECKSUM_ASSET}:
        # A release may have been published while its CI artifacts are uploading.
        # Never fall back to an unchecked source archive or another platform.
        return None
    return _validate_release({
        "version": tag[1:], "tag_name": tag,
        "zip_url": selected[WINDOWS_ASSET]["browser_download_url"],
        "checksum_url": selected[CHECKSUM_ASSET]["browser_download_url"],
        "release_url": f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
        "zip_size": selected[WINDOWS_ASSET]["size"],
    })


def _expected_checksum(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise UpdateError("체크섬 파일을 읽을 수 없습니다.") from exc
    matches = []
    for line in text.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, name = parts
        if name.startswith("*"):
            name = name[1:]
        if name == WINDOWS_ASSET:
            if not helper._DIGEST.fullmatch(digest.lower()):
                raise UpdateError("Windows 배포 파일 체크섬이 올바르지 않습니다.")
            matches.append(digest.lower())
    if len(matches) != 1:
        raise UpdateError("Windows 배포 파일의 유일한 SHA-256 체크섬이 필요합니다.")
    return matches[0]


def _extract_archive(archive: Path, destination: Path, cancel: threading.Event | None = None) -> None:
    helper.check_cancel(cancel)
    total = 0
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if not entries or len(entries) > helper.MAX_MANIFEST_FILES:
                raise UpdateError("업데이트 압축 파일의 항목 수가 올바르지 않습니다.")
            for item in entries:
                helper.check_cancel(cancel)
                raw_name = item.orig_filename
                if item.is_dir():
                    raw_name = raw_name.rstrip("/")
                relative = helper.safe_relative(raw_name)
                if relative.parts[0] != helper.APP_DIRECTORY:
                    raise UpdateError("압축 파일의 최상위 앱 폴더가 올바르지 않습니다.")
                key = raw_name.casefold()
                if key in seen:
                    raise UpdateError("대소문자 중복 압축 항목은 사용할 수 없습니다.")
                seen.add(key)
                mode = item.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                        or item.external_attr & 0x400 or item.flag_bits & 1):
                    raise UpdateError("링크, 암호화 또는 특수 파일은 업데이트에 포함할 수 없습니다.")
                total += item.file_size
                if item.file_size > MAX_ENTRY_BYTES or total > helper.MAX_UNPACKED_BYTES:
                    raise UpdateError("업데이트 압축 해제 크기 제한을 초과했습니다.")
                if item.file_size > 1024 * 1024 and item.file_size / max(item.compress_size, 1) > 250:
                    raise UpdateError("비정상적인 압축 비율의 업데이트 파일입니다.")
                target = helper.plain_path(destination / relative, must_exist=False)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                with source.open(item) as input_stream, target.open("xb") as output:
                    while True:
                        helper.check_cancel(cancel)
                        chunk = input_stream.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > item.file_size or count > MAX_ENTRY_BYTES:
                            raise UpdateError("압축 파일 항목 크기가 일치하지 않습니다.")
                        output.write(chunk)
                if count != item.file_size:
                    raise UpdateError("압축 파일 항목이 완전하지 않습니다.")
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError("업데이트 압축 파일을 안전하게 해제하지 못했습니다.") from exc


def prepare_update(release: dict, install_dir: str | Path,
                   cancel: threading.Event | None = None) -> dict:
    """Download, verify and stage a complete update without touching the app."""
    helper.check_cancel(cancel)
    _validate_release(release)
    install = helper.plain_path(Path(install_dir).absolute())
    current = helper.read_marker(install)
    helper.validate_install_contents(install)
    if helper.version_tuple(release["version"]) <= helper.version_tuple(current["version"]):
        raise UpdateError("설치된 버전보다 새로운 업데이트가 아닙니다.")
    if not os.access(install.parent, os.W_OK):
        raise UpdateError("앱의 상위 폴더에 쓰기 권한이 없습니다. portable 앱을 쓰기 가능한 위치로 옮기세요.")
    root = helper.plain_path(helper.storage_root(), must_exist=False)
    if install == root or install in root.parents or root in install.parents:
        raise UpdateError("앱 폴더와 업데이트 저장소가 겹칩니다.")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = root / uuid.uuid4().hex
    work.mkdir(mode=0o700)
    options = {"cancel": cancel} if cancel is not None else {}
    checksum_data = _download(release["checksum_url"], limit=MAX_CHECKSUM_BYTES, **options)
    helper.check_cancel(cancel)
    expected = _expected_checksum(checksum_data)
    archive = work / WINDOWS_ASSET
    _download(release["zip_url"], limit=MAX_ARCHIVE_BYTES, target=archive, **options)
    helper.check_cancel(cancel)
    if archive.stat().st_size != release["zip_size"] or helper.file_hash(archive, cancel=cancel) != expected:
        raise UpdateError("업데이트 SHA-256 또는 파일 크기가 일치하지 않습니다. 기존 앱은 유지됩니다.")
    payload = work / "payload"
    payload.mkdir()
    _extract_archive(archive, payload, cancel=cancel)
    helper.check_cancel(cancel)
    stage = payload / helper.APP_DIRECTORY
    marker = helper.read_marker(stage)
    if marker["version"] != release["version"]:
        raise UpdateError("압축 파일의 앱 버전이 릴리스 버전과 다릅니다.")
    plan = {
        "schema": 1, "app_id": helper.APP_ID, "id": work.name,
        "version": release["version"], "original_version": current["version"],
        "install_dir": str(install), "stage_dir": str(stage),
        "plan_path": str(work / "plan.json"), "parent_pid": os.getpid(),
        "files": helper.manifest(stage, cancel=cancel), "archive_sha256": expected,
        "created_at": time.time(),
    }
    helper.check_cancel(cancel)
    with (work / "plan.json").open("x", encoding="utf-8") as stream:
        json.dump(plan, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    helper.validate_plan(plan, cancel=cancel)
    helper.check_cancel(cancel)
    return plan


def install_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).absolute().parent
    return Path(__file__).absolute().parent.parent


def is_auto_update_supported() -> bool:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        location = install_directory()
        helper.read_marker(location)
        return os.access(location.parent, os.W_OK)
    except (UpdateError, OSError):
        return False


def start_update_helper(plan: dict) -> None:
    """Detach a trusted installed helper outside the folder it will replace."""
    if not is_auto_update_supported():
        raise UpdateError("Windows portable 배포본에서만 자동 업데이트를 적용합니다.")
    install, _stage, work = helper.validate_plan(plan)
    if install != install_directory():
        raise UpdateError("현재 실행 중인 앱의 업데이트 계획이 아닙니다.")
    source = helper.plain_path(install / helper.HELPER_EXECUTABLE)
    # A failed process launch must not poison future close/retry attempts. Each
    # attempt owns an exclusive fresh directory; no existing helper is replaced.
    launch_directory = helper.plain_path(work / ("launch-" + uuid.uuid4().hex), must_exist=False)
    launch_directory.mkdir(mode=0o700)
    target = launch_directory / helper.HELPER_EXECUTABLE
    try:
        with source.open("rb") as input_stream, target.open("xb") as output:
            shutil.copyfileobj(input_stream, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if helper.file_hash(source) != helper.file_hash(target):
            raise UpdateError("업데이트 도우미 복사 확인에 실패했습니다.")
        subprocess.Popen(
            [str(target), "--plan", plan["plan_path"]], cwd=work, close_fds=True,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        raise UpdateError("업데이트 도우미를 시작하지 못했습니다. 앱을 유지하고 나중에 다시 시도하세요.") from exc


def last_update_result() -> dict | None:
    """Read diagnostic status, never a source of executable paths or commands."""
    try:
        path = helper.storage_root() / "last-result.json"
        helper.plain_path(path)
        result = helper._read_json(path)
        if result.get("status") in {"updated", "failed"}:
            return result
    except (UpdateError, OSError):
        pass
    return None
