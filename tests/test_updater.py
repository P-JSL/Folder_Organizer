from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import threading
import zipfile

import pytest

from folder_organizer import updater as updates
from folder_organizer import updater_helper as helper


def app_files(version="0.2.0"):
    return {
        "FolderOrganizer.exe": b"new executable" if version != "0.1.0" else b"old executable",
        "FolderOrganizerUpdater.exe": b"helper executable",
        "_internal/library.dll": b"runtime library",
        "version.json": json.dumps({"app_id": helper.APP_ID, "version": version}).encode(),
    }


def write_app(path, version="0.1.0"):
    for relative, content in app_files(version).items():
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def archive_bytes(files=None):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (files or app_files()).items():
            archive.writestr("FolderOrganizer/" + name, content)
    return result.getvalue()


def release_json(version="0.2.0", size=100):
    tag = "v" + version
    return {
        "tag_name": tag, "draft": False, "prerelease": False,
        "assets": [
            {"name": updates.WINDOWS_ASSET, "size": size, "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{updates.WINDOWS_ASSET}"},
            {"name": updates.CHECKSUM_ASSET, "size": 100, "browser_download_url": f"https://github.com/{updates.REPOSITORY}/releases/download/{tag}/{updates.CHECKSUM_ASSET}"},
        ],
    }


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "user-updates"
    monkeypatch.setattr(helper, "storage_root", lambda: root)
    install = tmp_path / "Programs" / "FolderOrganizer"
    write_app(install)
    return install, root


def mock_release(monkeypatch, payload=None, checksum=None):
    payload = payload or archive_bytes()
    remote = release_json(size=len(payload))
    expected = checksum or hashlib.sha256(payload).hexdigest()
    checksums = f"{expected}  {updates.WINDOWS_ASSET}\n".encode()

    def download(url, *, limit, target=None):
        if url == updates.LATEST_RELEASE_API:
            return json.dumps(remote).encode()
        if url.endswith(updates.CHECKSUM_ASSET):
            return checksums
        if target:
            target.write_bytes(payload)
            return None
        raise AssertionError(url)

    monkeypatch.setattr(updates, "_download", download)
    return updates.check_for_update("0.1.0")


def test_source_install_does_not_auto_update():
    assert not updates.is_auto_update_supported()


@pytest.mark.parametrize("version", ["0.1.0", "0.0.9", "0.2.0-beta", "02.0.0", "x", "999"])
def test_no_downgrade_or_unstable_update(version, monkeypatch):
    monkeypatch.setattr(updates, "_download", lambda *args, **kwargs: json.dumps(release_json(version)).encode())
    assert updates.check_for_update("0.1.0") is None


def test_prerelease_and_incomplete_release_are_ignored(monkeypatch):
    value = release_json()
    value["prerelease"] = True
    monkeypatch.setattr(updates, "_download", lambda *args, **kwargs: json.dumps(value).encode())
    assert updates.check_for_update("0.1.0") is None
    value["prerelease"] = False
    value["assets"].pop()
    assert updates.check_for_update("0.1.0") is None


def test_new_release_uses_only_this_repo_assets(monkeypatch):
    value = release_json()
    monkeypatch.setattr(updates, "_download", lambda *args, **kwargs: json.dumps(value).encode())
    result = updates.check_for_update("0.1.0")
    assert result["version"] == "0.2.0"
    value["assets"][0]["browser_download_url"] = "https://github.com/evil/repo/releases/download/v0.2.0/" + updates.WINDOWS_ASSET
    with pytest.raises(updates.UpdateError):
        updates.check_for_update("0.1.0")


@pytest.mark.parametrize("url", [
    "http://github.com/P-JSL/Folder_Organizer/releases/download/v0.2.0/a.zip",
    "https://github.com.evil.example/x", "https://user:pass@github.com/P-JSL/Folder_Organizer/releases/x",
    "file:///C:/app.exe", "https://api.github.com/repos/evil/repo/releases/latest",
    "https://github.com:444/P-JSL/Folder_Organizer/releases/x",
])
def test_disallowed_url_schemes_hosts_and_repositories(url):
    with pytest.raises(updates.UpdateError):
        updates._safe_url(url)


def test_redirect_to_untrusted_host_is_rejected():
    with pytest.raises(updates.UpdateError):
        updates._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/app.zip")


def test_staging_does_not_modify_installed_files(environment, monkeypatch):
    install, root = environment
    before = helper.manifest(install)
    release = mock_release(monkeypatch)
    plan = updates.prepare_update(release, install)
    assert helper.manifest(install) == before
    assert Path(plan["plan_path"]).is_file()
    assert helper.load_plan(plan["plan_path"]) == plan
    assert helper.read_marker(Path(plan["stage_dir"]))["version"] == "0.2.0"
    assert Path(plan["stage_dir"]).is_relative_to(root)


def test_hash_mismatch_never_changes_install(environment, monkeypatch):
    install, _ = environment
    before = helper.manifest(install)
    release = mock_release(monkeypatch, checksum="0" * 64)
    with pytest.raises(updates.UpdateError, match="SHA-256"):
        updates.prepare_update(release, install)
    assert helper.manifest(install) == before


@pytest.mark.parametrize("name", ["../escape.exe", "/absolute.exe", "C:/escape.exe", "FolderOrganizer/../../escape.exe", "FolderOrganizer/CON.txt", "FolderOrganizer/trailing. ", "FolderOrganizer\\evil.exe", "FolderOrganizer/question?.exe", "FolderOrganizer/a<b.exe"])
def test_unsafe_zip_paths_are_rejected(tmp_path, name):
    path = tmp_path / "download.zip"
    with zipfile.ZipFile(path, "w") as archive:
        # ZipInfo normalizes backslashes while writing on Windows. Preserve the
        # same malicious raw local/central filename on every test platform.
        archive.writestr(name.replace("\\", "/"), b"not executed")
    if "\\" in name:
        path.write_bytes(path.read_bytes().replace(name.replace("\\", "/").encode(), name.encode()))
    destination = tmp_path / "payload"
    destination.mkdir()
    with pytest.raises(updates.UpdateError):
        updates._extract_archive(path, destination)
    assert not (tmp_path / "escape.exe").exists()


def test_zip_symlink_and_case_collision_are_rejected(tmp_path):
    path = tmp_path / "download.zip"
    with zipfile.ZipFile(path, "w") as archive:
        item = zipfile.ZipInfo("FolderOrganizer/symlink")
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(item, "../escape")
    destination = tmp_path / "payload"
    destination.mkdir()
    with pytest.raises(updates.UpdateError):
        updates._extract_archive(path, destination)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("FolderOrganizer/A.exe", b"one")
        archive.writestr("FolderOrganizer/a.exe", b"two")
    with pytest.raises(updates.UpdateError):
        updates._extract_archive(path, destination)


def test_zip_size_bomb_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "download.zip"
    path.write_bytes(archive_bytes())
    monkeypatch.setattr(updates, "MAX_ENTRY_BYTES", 2)
    with pytest.raises(updates.UpdateError):
        updates._extract_archive(path, tmp_path)


def test_staged_version_must_match_release(environment, monkeypatch):
    install, _ = environment
    release = mock_release(monkeypatch, payload=archive_bytes(app_files("0.3.0")))
    with pytest.raises(updates.UpdateError, match="버전"):
        updates.prepare_update(release, install)
    assert helper.read_marker(install)["version"] == "0.1.0"


def test_complete_swap_retains_previous_app_and_reports_status(environment, monkeypatch):
    install, root = environment
    original = helper.manifest(install)
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    result = helper.apply_update(plan, wait=False, restart=False)
    assert result["status"] == "updated"
    assert helper.read_marker(install)["version"] == "0.2.0"
    assert helper.manifest(Path(result["backup"])) == original
    assert updates.last_update_result()["status"] == "updated"
    assert (root / plan["id"] / "result.json").is_file()


def test_activation_failure_rolls_back_original_app(environment, monkeypatch):
    install, _ = environment
    original = helper.manifest(install)
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    real_rename = helper._rename_with_retry

    def rename(source, target, attempts=20):
        if source.name.startswith("."):
            raise PermissionError("simulated Windows lock")
        real_rename(source, target, attempts)

    monkeypatch.setattr(helper, "_rename_with_retry", rename)
    with pytest.raises(updates.UpdateError, match="simulated"):
        helper.apply_update(plan, wait=False, restart=False)
    assert helper.manifest(install) == original
    assert updates.last_update_result()["status"] == "failed"


def test_changed_staged_file_is_rejected_before_swap(environment, monkeypatch):
    install, _ = environment
    original = helper.manifest(install)
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    (Path(plan["stage_dir"]) / "_internal/library.dll").write_bytes(b"tampered")
    with pytest.raises(updates.UpdateError, match="변경"):
        helper.apply_update(plan, wait=False, restart=False)
    assert helper.manifest(install) == original


def test_plan_tampering_and_unknown_personal_files_are_rejected(environment, monkeypatch):
    install, _ = environment
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    changed = dict(plan, install_dir=str(install.parent))
    with pytest.raises(updates.UpdateError, match="일치"):
        helper.apply_update(changed, wait=False, restart=False)
    (install / "personal.txt").write_text("keep me")
    with pytest.raises(updates.UpdateError, match="개인 파일"):
        helper.apply_update(plan, wait=False, restart=False)
    assert (install / "personal.txt").read_text() == "keep me"


def test_existing_backup_is_not_overwritten(environment, monkeypatch):
    install, _ = environment
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    backup = install.with_name(install.name + ".previous-" + plan["id"])
    backup.mkdir()
    (backup / "keep.txt").write_text("other data")
    with pytest.raises(updates.UpdateError, match="이미"):
        helper.apply_update(plan, wait=False, restart=False)
    assert (backup / "keep.txt").read_text() == "other data"
    assert helper.read_marker(install)["version"] == "0.1.0"


def test_process_wait_failure_preserves_current_app(environment, monkeypatch):
    install, _ = environment
    original = helper.manifest(install)
    plan = updates.prepare_update(mock_release(monkeypatch), install)

    def timeout(_pid):
        raise helper.UpdateError("앱 종료 대기 시간 초과")

    monkeypatch.setattr(helper, "wait_for_process", timeout)
    with pytest.raises(updates.UpdateError, match="대기 시간"):
        helper.apply_update(plan, wait=True, restart=False)
    assert helper.manifest(install) == original


def test_symlinked_install_is_rejected(environment, tmp_path):
    install, _ = environment
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(install, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not available")
    with pytest.raises(updates.UpdateError, match="링크"):
        helper.read_marker(linked)


def test_checksum_requires_unique_exact_asset_name():
    digest = "a" * 64
    assert updates._expected_checksum(f"{digest} *{updates.WINDOWS_ASSET}\n".encode()) == digest
    with pytest.raises(updates.UpdateError):
        updates._expected_checksum(f"{digest}  other.zip\n".encode())
    with pytest.raises(updates.UpdateError):
        updates._expected_checksum((f"{digest}  {updates.WINDOWS_ASSET}\n" * 2).encode())


def test_shipped_notices_and_license_directory_are_valid(environment, monkeypatch):
    install, _ = environment
    (install / "README.md").write_text("read me")
    files = app_files()
    files.update({"README.md": b"new readme", "CHANGELOG.md": b"changes", "THIRD_PARTY.md": b"third-party notices", "licenses/Qt-LGPL.txt": b"license text"})
    plan = updates.prepare_update(mock_release(monkeypatch, payload=archive_bytes(files)), install)
    result = helper.apply_update(plan, wait=False, restart=False)
    assert result["status"] == "updated"
    assert (install / "licenses/Qt-LGPL.txt").read_text() == "license text"


def test_helper_launch_failure_can_retry_with_fresh_owned_executable(environment, monkeypatch):
    install, _ = environment
    plan = updates.prepare_update(mock_release(monkeypatch), install)
    monkeypatch.setattr(updates, "is_auto_update_supported", lambda: True)
    monkeypatch.setattr(updates, "install_directory", lambda: install)
    monkeypatch.setattr(updates.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(updates.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    launched = []

    def launch(args, **kwargs):
        launched.append(args[0])
        if len(launched) == 1:
            raise OSError("simulated process creation failure")
        return object()

    monkeypatch.setattr(updates.subprocess, "Popen", launch)
    with pytest.raises(updates.UpdateError, match="시작하지"):
        updates.start_update_helper(plan)
    updates.start_update_helper(plan)
    assert len(launched) == 2
    assert launched[0] != launched[1]
    assert all(Path(item).is_file() for item in launched)
    assert helper.read_marker(install)["version"] == "0.1.0"


def test_windows_wait_closes_process_handle(monkeypatch):
    calls = []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class Kernel:
        OpenProcess = Function(lambda *args: 42)
        WaitForSingleObject = Function(lambda *args: 0)
        CloseHandle = Function(lambda value: calls.append(value))

    monkeypatch.setattr(helper.sys, "platform", "win32")
    monkeypatch.setattr(helper.ctypes, "WinDLL", lambda *args, **kwargs: Kernel(), raising=False)
    helper.wait_for_process(12345, timeout=1)
    assert calls == [42]


def test_restart_failure_keeps_successful_update(environment, monkeypatch):
    install, _ = environment
    plan = updates.prepare_update(mock_release(monkeypatch), install)

    def fail(_install):
        raise OSError("cannot relaunch")

    monkeypatch.setattr(helper, "_launch_app", fail)
    result = helper.apply_update(plan, wait=False, restart=True)
    assert result["status"] == "updated"
    assert "직접 실행" in result["details"]
    assert helper.read_marker(install)["version"] == "0.2.0"


def test_cancelled_update_check_does_not_make_request(monkeypatch):
    cancel = threading.Event()
    cancel.set()

    def unexpected_request(*args, **kwargs):
        pytest.fail("A cancelled update should not make a network request")

    monkeypatch.setattr(updates, "_download", unexpected_request)
    with pytest.raises(updates.UpdateError, match="취소"):
        updates.check_for_update("0.1.0", cancel=cancel)


def test_cancelled_preparation_preserves_install_and_creates_no_stage(environment, monkeypatch):
    install, root = environment
    release = mock_release(monkeypatch)
    before = helper.manifest(install)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(updates.UpdateError, match="취소"):
        updates.prepare_update(release, install, cancel=cancel)
    assert helper.manifest(install) == before
    assert not root.exists()


def test_cancel_during_download_closes_partial_file_and_never_applies(environment, monkeypatch):
    install, root = environment
    before = helper.manifest(install)
    target = root / "partial-download.zip"
    root.mkdir()
    cancel = threading.Event()
    closed = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def geturl(self):
            return updates._asset_url("v0.2.0", updates.WINDOWS_ASSET)

        def read1(self, size):
            cancel.set()
            return b"incomplete data"

        read = read1

    class Opener:
        def open(self, request, timeout):
            assert timeout == 15
            return Response()

    monkeypatch.setattr(updates, "build_opener", lambda *args: Opener())
    with pytest.raises(updates.UpdateError, match="취소"):
        updates._download(updates._asset_url("v0.2.0", updates.WINDOWS_ASSET), limit=100, target=target, cancel=cancel)
    assert closed == [True]
    assert helper.manifest(install) == before
    assert not list(root.rglob("plan.json"))
    # A closed partial file may be cleaned up by future maintenance or the user.
    target.unlink()


def test_cancel_after_download_does_not_create_actionable_plan(environment, monkeypatch):
    install, root = environment
    release = mock_release(monkeypatch)
    real_download = updates._download
    cancel = threading.Event()

    def cancel_download(url, *, limit, target=None, cancel=None):
        result = real_download(url, limit=limit, target=target)
        if target is not None:
            cancel.set()
        return result

    monkeypatch.setattr(updates, "_download", cancel_download)
    before = helper.manifest(install)
    with pytest.raises(updates.UpdateError, match="취소"):
        updates.prepare_update(release, install, cancel=cancel)
    assert helper.manifest(install) == before
    assert not list(root.rglob("plan.json"))


def test_manifest_cancellation_stops_before_reading_app_files(environment):
    install, _ = environment
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(updates.UpdateError, match="취소"):
        helper.manifest(install, cancel=cancel)


@pytest.mark.parametrize("no_restart", [False, True])
def test_helper_cli_no_restart_retains_normal_wait_behavior(monkeypatch, no_restart):
    called = []
    plan = {"test": "validated by mocked loader"}
    monkeypatch.setattr(helper, "load_plan", lambda path: plan)

    def apply(received_plan, *, wait=True, restart=True):
        called.append((received_plan, wait, restart))

    monkeypatch.setattr(helper, "apply_update", apply)
    args = ["--plan", "mock-plan.json"]
    if no_restart:
        args.append("--no-restart")
    assert helper.main(args) == 0
    assert called == [(plan, True, not no_restart)]
