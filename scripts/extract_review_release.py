"""Extract only the two exact build artifacts for new native validation."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import stat
import zipfile

from archive_review_release import ROOT, digest


def main() -> None:
    source = ROOT / "release-build"
    manifest = json.loads((source / "BUILD_INFO.json").read_text(encoding="utf-8"))
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=False)
    for name, package in manifest["packages"].items():
        archive_path = source / name
        if digest(archive_path) != package["sha256"]:
            raise ValueError("Downloaded artifact differs from the exact formal build")
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            seen = set()
            if len(entries) > 15_000 or sum(item.file_size for item in entries) > 2 * 1024**3:
                raise ValueError("Unexpected archive size")
            for entry in entries:
                raw = entry.orig_filename.rstrip("/")
                path = PurePosixPath(raw)
                if (path.is_absolute() or not path.parts or path.parts[0] != package["directory"]
                        or ".." in path.parts or any(character in raw for character in ("\\", ":", "\0"))
                        or raw.casefold() in seen or stat.S_IFMT(entry.external_attr >> 16) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError("Invalid release archive member")
                seen.add(raw.casefold())
            archive.extractall(destination)
    print("Exact formal artifacts extracted; previous application suites were not invoked.")


if __name__ == "__main__":
    main()
