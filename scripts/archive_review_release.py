"""Archive both built editions and record the exact release payloads."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> None:
    output = ROOT / "release-build"
    output.mkdir(exist_ok=False)
    packages = {}
    for folder, name, programs in (
        ("FolderOrganizer", "FolderOrganizer-Windows-x64.zip", ("FolderOrganizer.exe", "FolderOrganizerUpdater.exe")),
        ("FolderOrganizerRust", "FolderOrganizer-Rust-Windows-x64.zip", ("FolderOrganizerRust.exe", "FolderOrganizerRustCLI.exe", "FolderOrganizerRustUpdater.exe")),
    ):
        directory = ROOT / "dist" / folder
        marker = json.loads((directory / "version.json").read_text(encoding="utf-8"))
        archive_path = output / name
        files = [path for path in sorted(directory.rglob("*")) if path.is_file()]
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                if path.is_symlink():
                    raise ValueError("Release payload must not contain symbolic links")
                archive.write(path, path.relative_to(directory.parent).as_posix())
        packages[name] = {"directory": folder, "version": marker["version"], "app_id": marker["app_id"],
                          "zip_bytes": archive_path.stat().st_size, "sha256": digest(archive_path),
                          "files": len(files), "unpacked_bytes": sum(path.stat().st_size for path in files),
                          "program_sha256": {program: digest(directory / program) for program in programs}}
    versions = {package["version"] for package in packages.values()}
    if len(versions) != 1:
        raise ValueError("Both release editions must have the same version")
    manifest = {"source_commit": os.environ["GITHUB_SHA"], "workflow_run": os.environ["GITHUB_RUN_ID"],
                "version": versions.pop(), "packages": packages,
                "authenticode": "not configured; no signing certificate supplied to this build",
                "verification_scope": "new changed-path checks only; previously passed cases are not rerun"}
    (output / "BUILD_INFO.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "SHA256SUMS.txt").write_text("".join(f"{value['sha256']}  {name}\n" for name, value in packages.items()), encoding="utf-8")
    shutil.copyfile(ROOT / "dist/PYTHON_PACKAGE_LAYOUT.json", output / "PYTHON_PACKAGE_LAYOUT.json")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
