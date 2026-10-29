"""Package version-only Windows builds without running tests or applications."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile

from package_legal import include_legal_documents

ROOT = Path(__file__).resolve().parents[1]
RUST_DIRECTORY = "FolderOrganizerRust"
RUST_PROGRAMS = (
    "FolderOrganizerRust.exe", "FolderOrganizerRustCLI.exe", "FolderOrganizerRustUpdater.exe",
)


def main() -> None:
    destination = ROOT / "dist" / RUST_DIRECTORY
    destination.mkdir(parents=True, exist_ok=False)
    # Dependencies are unchanged. Reuse their already shipped notices, without
    # repeating the previous dependency/native-import audits or application checks.
    with zipfile.ZipFile(ROOT / "reuse/FolderOrganizer-Rust-Windows-x64.zip") as original:
        for entry in original.infolist():
            path = PurePosixPath(entry.filename)
            if not path.parts or path.parts[0] != RUST_DIRECTORY:
                continue
            relative = PurePosixPath(*path.parts[1:])
            if relative.as_posix() != "THIRD_PARTY.md" and not relative.parts[:1] == ("licenses",):
                continue
            # Application terms come from this release, rather than an older
            # package; dependency notices are reused without changing them.
            if relative.parts[:2] == ("licenses", "FolderOrganizer"):
                continue
            if entry.is_dir():
                continue
            if relative.is_absolute() or ".." in relative.parts or any(
                character in entry.filename for character in ("\\", ":", "\0")
            ):
                raise ValueError("Invalid notice destination in the prior package")
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with original.open(entry) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
    for name in RUST_PROGRAMS:
        shutil.copyfile(ROOT / "rust-app/target/x86_64-pc-windows-msvc/release" / name,
                        destination / name)
    shutil.copyfile(ROOT / "rust-app/version.json", destination / "version.json")
    for name in ("README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, destination / name)
    include_legal_documents(destination)

    checksums = []
    for directory, archive_name in (
        (ROOT / "dist/FolderOrganizer", "FolderOrganizer-Windows-x64.zip"),
        (destination, "FolderOrganizer-Rust-Windows-x64.zip"),
    ):
        archive_path = ROOT / archive_name
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as archive:
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(directory.parent).as_posix())
        with archive_path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{checksum}  {archive_name}")
        print(json.dumps({"archive": archive_name, "bytes": archive_path.stat().st_size,
                          "sha256": checksum, "tests": "not run"}))
    (ROOT / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
