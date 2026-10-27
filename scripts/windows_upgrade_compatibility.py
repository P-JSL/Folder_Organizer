"""Verify the published 0.2.0 updater can install the current Windows ZIP.

Only disposable copies are started or replaced. The old executable and helper
come from the public, checksum-verified release; the new payload is the exact
PowerShell ZIP that this job will publish. No release assets are modified here.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
import zipfile

from PySide6.QtCore import QSettings

from folder_organizer import __version__, updater
from folder_organizer import updater_helper as helper

PREVIOUS_VERSION = "0.2.0"


@contextmanager
def disabled_fixture_updates():
    """Keep the old GUI from starting an unrelated real update during a rerun."""
    settings = QSettings("P-JSL", "FolderOrganizer")
    key = "updates/enabled"
    existed, original = settings.contains(key), settings.value(key)
    settings.setValue(key, False)
    settings.sync()
    try:
        yield
    finally:
        if existed:
            settings.setValue(key, original)
        else:
            settings.remove(key)
        settings.sync()


def release_info(version: str, size: int) -> dict:
    tag = "v" + version
    base = f"https://github.com/{updater.REPOSITORY}/releases"
    return {
        "version": version, "tag_name": tag,
        "zip_url": f"{base}/download/{tag}/{updater.WINDOWS_ASSET}",
        "checksum_url": f"{base}/download/{tag}/{updater.CHECKSUM_ASSET}",
        "release_url": f"{base}/tag/{tag}", "zip_size": size,
    }


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("This compatibility check must run on Windows")
    if helper.version_tuple(__version__) <= helper.version_tuple(PREVIOUS_VERSION):
        print("Previous-release upgrade check is not applicable to this version")
        return 0

    archive = Path(updater.WINDOWS_ASSET).absolute()
    checksum_file = Path(updater.CHECKSUM_ASSET).absolute()
    expected_new = updater._expected_checksum(checksum_file.read_bytes())
    assert helper.file_hash(archive) == expected_new, "Current package checksum does not match"
    current_release = release_info(__version__, archive.stat().st_size)

    with tempfile.TemporaryDirectory(prefix="organizer-previous-upgrade-") as temporary:
        scratch = Path(temporary)
        previous = release_info(PREVIOUS_VERSION, 1)
        checksum = updater._download(previous["checksum_url"], limit=updater.MAX_CHECKSUM_BYTES)
        expected_old = updater._expected_checksum(checksum)
        old_archive = scratch / "previous-release.zip"
        updater._download(previous["zip_url"], limit=updater.MAX_ARCHIVE_BYTES, target=old_archive)
        assert helper.file_hash(old_archive) == expected_old, "Published previous release checksum does not match"
        install_parent = scratch / "Programs"
        install_parent.mkdir()
        updater._extract_archive(old_archive, install_parent)
        install = install_parent / helper.APP_DIRECTORY
        assert helper.read_marker(install)["version"] == PREVIOUS_VERSION
        helper.validate_install_contents(install)

        # Keep an independent copy of the actual OLD helper. Updating its parent
        # directory must not implicitly substitute the new helper under test.
        old_helper = scratch / "PreviousFolderOrganizerUpdater.exe"
        shutil.copyfile(install / helper.HELPER_EXECUTABLE, old_helper)
        original_helper_hash = helper.file_hash(old_helper)
        assert original_helper_hash == helper.file_hash(install / helper.HELPER_EXECUTABLE)
        with zipfile.ZipFile(archive) as bundle:
            with bundle.open(f"{helper.APP_DIRECTORY}/{helper.APP_EXECUTABLE}") as executable:
                import hashlib
                expected_executable = hashlib.file_digest(executable, "sha256").hexdigest()

        old_process = None
        update_process = None
        appdata = scratch / "AppData"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata), "QT_QPA_PLATFORM": "offscreen"}), disabled_fixture_updates():
            try:
                old_process = subprocess.Popen([str(install / helper.APP_EXECUTABLE)], cwd=scratch)
                time.sleep(3)
                assert old_process.poll() is None, "Published previous desktop exited at startup"

                def local_download(url, *, limit, target=None, **kwargs):
                    if url == current_release["checksum_url"]:
                        content = checksum_file.read_bytes()
                        assert len(content) <= limit and target is None
                        return content
                    if url == current_release["zip_url"]:
                        assert target is not None and archive.stat().st_size <= limit
                        # Match the updater's exclusive-write semantics.
                        with archive.open("rb") as source, Path(target).open("xb") as output:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                        return None
                    raise AssertionError("Unexpected release fixture request")

                with patch.object(updater, "_download", local_download):
                    plan = updater.prepare_update(current_release, install)
                plan["parent_pid"] = old_process.pid
                plan_path = Path(plan["plan_path"])
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                helper.validate_plan(plan)
                update_process = subprocess.Popen(
                    [str(old_helper), "--plan", str(plan_path), "--no-restart"], cwd=scratch,
                )
                time.sleep(1)
                assert update_process.poll() is None, "Previous helper did not wait for the live desktop"
                assert helper.read_marker(install)["version"] == PREVIOUS_VERSION
                old_process.terminate()
                old_process.wait(timeout=15)
                exit_code = update_process.wait(timeout=180)
                diagnostic = plan_path.parent / "result.json"
                result = json.loads(diagnostic.read_text(encoding="utf-8")) if diagnostic.exists() else {}
                assert exit_code == 0, f"Previous helper failed ({exit_code}): {result}"
                assert result.get("status") == "updated", result
                assert helper.read_marker(install)["version"] == __version__
                backup = Path(result["backup"])
                assert helper.read_marker(backup)["version"] == PREVIOUS_VERSION
                assert helper.file_hash(backup / helper.HELPER_EXECUTABLE) == original_helper_hash
                assert helper.file_hash(install / helper.APP_EXECUTABLE) == expected_executable

                samples = scratch / "sample-files"
                samples.mkdir()
                for name in ("original.txt", "copy.txt"):
                    (samples / name).write_bytes(b"previous-version upgrade verification")
                report_path = scratch / "updated-analysis.json"
                subprocess.run([
                    str(install / helper.APP_EXECUTABLE), "--cli", str(samples), "--duplicates", "--json", str(report_path),
                ], check=True, timeout=60)
                analysis = json.loads(report_path.read_text(encoding="utf-8"))
                assert analysis["app_version"] == __version__
                assert len(analysis["files"]) == 2 and len(analysis["duplicates"]) == 1
                assert analysis["duplicates"][0]["evidence"]["byte_identical"] is True
                print(f"Published {PREVIOUS_VERSION} helper upgraded a live desktop to {__version__}; backup and new executable analysis verified")
            finally:
                if old_process is not None and old_process.poll() is None:
                    old_process.terminate()
                    old_process.wait(timeout=15)
                if update_process is not None and update_process.poll() is None:
                    update_process.terminate()
                    update_process.wait(timeout=15)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        message = traceback.format_exc().replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print("::error title=Previous Windows updater compatibility::" + message)
        raise
