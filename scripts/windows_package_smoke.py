"""Exercise the compiled Windows GUI, CLI and update helper on disposable copies.

The network is replaced with local release fixtures, while the real packaged
helper performs process waiting, hash verification and directory replacement.
"""
from __future__ import annotations

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

from folder_organizer import __version__, updater
from folder_organizer import updater_helper as helper


def main() -> int:
    if sys.platform != "win32":
        raise RuntimeError("This smoke check must run on Windows")
    package = Path("dist/FolderOrganizer").absolute()
    with tempfile.TemporaryDirectory(prefix="folder-organizer-native-") as temporary:
        scratch = Path(temporary)
        install = scratch / "Programs" / "FolderOrganizer"
        shutil.copytree(package, install)
        source = scratch / "sample-files"
        source.mkdir()
        for name in ("original.txt", "copy.txt"):
            (source / name).write_text("actual packaged duplicate test", encoding="utf-8")
        # Windowed PyInstaller builds do not expose stdout; exercise a CSV result.
        # The normal CLI executable is a GUI-subsystem binary on Windows, so the
        # persistent CSV is the platform-neutral functional readiness evidence.
        inventory = scratch / "inventory.csv"
        report = scratch / "inventory.json"
        subprocess.run(
            [str(install / helper.APP_EXECUTABLE), "--cli", str(source), "--duplicates", "--csv", str(inventory), "--json", str(report)],
            check=True, timeout=60,
        )
        assert inventory.is_file() and "original.txt" in inventory.read_text(encoding="utf-8-sig")
        analysis = json.loads(report.read_text(encoding="utf-8"))
        assert len(analysis["files"]) == 2 and len(analysis["duplicates"]) == 1
        assert analysis["duplicates"][0]["evidence"]["byte_identical"] is True
        review = scratch / "review-files"
        review.mkdir()
        paragraph = "A careful local review compares every supported signal while keeping the original document safe for later decisions. "
        (review / "draft.txt").write_text(paragraph, encoding="utf-8")
        (review / "draft copy.txt").write_text(paragraph + "Revision.", encoding="utf-8")
        (review / "Cache").mkdir()
        (review / "Cache" / "ignored.txt").write_text(paragraph, encoding="utf-8")
        similarity_json, similarity_html = scratch / "similarity.json", scratch / "similarity.html"
        subprocess.run([
            str(install / helper.APP_EXECUTABLE), "--cli", str(review), "--similar",
            "--similar-method", "text", "--text-threshold", "0.90", "--exclude", "Cache",
            "--json", str(similarity_json), "--html", str(similarity_html),
        ], check=True, timeout=60)
        comparison = json.loads(similarity_json.read_text(encoding="utf-8"))
        assert comparison["schema_version"] == 2
        assert comparison["excluded_directories"] == 1
        assert comparison["comparison_report"]["methods"] == ["text"]
        assert len(comparison["similar"]) == 1
        candidate = comparison["similar"][0]
        assert candidate["kind"] == "similar_text"
        assert candidate["evidence"]["byte_identical"] is False
        assert candidate["evidence"]["matched_signals"] == ["text"]
        assert "동일 확률이 아니며" in similarity_html.read_text(encoding="utf-8")
        old_process = subprocess.Popen([str(install / helper.APP_EXECUTABLE)], cwd=scratch)
        update_process = None
        try:
            time.sleep(3)
            assert old_process.poll() is None, "Packaged desktop exited at startup"
            major, minor, patch_number = helper.version_tuple(__version__)
            next_version = f"{major}.{minor}.{patch_number + 1}"
            archive = scratch / updater.WINDOWS_ASSET
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
                for file in package.rglob("*"):
                    if not file.is_file():
                        continue
                    relative = file.relative_to(package).as_posix()
                    if relative == "version.json":
                        bundle.writestr("FolderOrganizer/version.json", json.dumps({"app_id": helper.APP_ID, "version": next_version}))
                    else:
                        bundle.write(file, "FolderOrganizer/" + relative)
            digest = helper.file_hash(archive)
            checksum = f"{digest}  {updater.WINDOWS_ASSET}\n".encode()
            tag = "v" + next_version
            release = {
                "version": next_version, "tag_name": tag,
                "zip_url": f"https://github.com/{updater.REPOSITORY}/releases/download/{tag}/{updater.WINDOWS_ASSET}",
                "checksum_url": f"https://github.com/{updater.REPOSITORY}/releases/download/{tag}/{updater.CHECKSUM_ASSET}",
                "release_url": f"https://github.com/{updater.REPOSITORY}/releases/tag/{tag}",
                "zip_size": archive.stat().st_size,
            }

            def local_download(url, *, limit, target=None, **kwargs):
                if url.endswith(updater.CHECKSUM_ASSET):
                    return checksum
                assert target is not None
                shutil.copyfile(archive, target)

            appdata = scratch / "AppData"
            with patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}), patch.object(updater, "_download", local_download):
                plan = updater.prepare_update(release, install)
                plan["parent_pid"] = old_process.pid
                Path(plan["plan_path"]).write_text(json.dumps(plan), encoding="utf-8")
                helper.validate_plan(plan)
                copied_helper = scratch / "FolderOrganizerUpdater.exe"
                shutil.copyfile(install / helper.HELPER_EXECUTABLE, copied_helper)
                update_process = subprocess.Popen([
                    str(copied_helper), "--plan", plan["plan_path"], "--no-restart",
                ], cwd=scratch)
                time.sleep(1)
                assert update_process.poll() is None, "Helper did not wait for the live desktop"
                assert helper.read_marker(install)["version"] == __version__
                old_process.terminate()
                old_process.wait(timeout=15)
                exit_code = update_process.wait(timeout=120)
                diagnostic = Path(plan["plan_path"]).parent / "result.json"
                result = json.loads(diagnostic.read_text(encoding="utf-8")) if diagnostic.exists() else {}
                assert exit_code == 0, f"Native update helper failed ({exit_code}): {result}"
                assert result["status"] == "updated", result
                assert helper.read_marker(install)["version"] == next_version
                assert helper.read_marker(Path(result["backup"]))["version"] == __version__
                assert helper.file_hash(install / helper.APP_EXECUTABLE) == helper.file_hash(package / helper.APP_EXECUTABLE)
                subprocess.run([str(install / helper.APP_EXECUTABLE), "--cli", str(source)], check=True, timeout=60)
            print("Compiled Windows desktop, exact/similar evidence, scope filters, JSON/HTML exports, helper wait, version swap and retained backup verified")
        finally:
            if old_process.poll() is None:
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
        print("::error title=Compiled Windows package check::" + message)
        raise
