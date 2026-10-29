"""Stage both formal builds in a draft after all new native checks pass."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "P-JSL/Folder_Organizer"


def run(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def main() -> None:
    python_marker = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))
    rust_marker = json.loads((ROOT / "rust-app/version.json").read_text(encoding="utf-8"))
    version = python_marker["version"]
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    assert rust_marker == {"app_id": "P-JSL/Folder_Organizer.Rust", "version": version}
    assert python_marker["app_id"] == REPOSITORY
    assert tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"] == version
    assert tomllib.loads((ROOT / "rust-app/Cargo.toml").read_text(encoding="utf-8"))["package"]["version"] == version
    tag = "v" + version
    commit = run("git", "rev-parse", "HEAD")
    entries = json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases?per_page=100"))
    found = [entry for entry in entries if entry["tag_name"] == tag]
    assert len(found) <= 1
    if found:
        release = found[0]
        assert release["draft"] is True and release["prerelease"] is False, "Refusing to replace a published version"
        assert not release["assets"], "Refusing to overwrite existing release assets"
        assert release["target_commitish"] == commit, "Draft source differs from this formal build"
    else:
        run("gh", "release", "create", tag, "--repo", REPOSITORY, "--draft", "--target", commit,
            "--title", f"Folder Organizer {tag} · Python / Rust",
            "--notes-file", str(ROOT / f"docs/RELEASE_NOTES_{version}.md"))
    run("gh", "release", "upload", tag, "FolderOrganizer-Windows-x64.zip",
        "FolderOrganizer-Rust-Windows-x64.zip", "SHA256SUMS.txt", "--repo", REPOSITORY)
    print(json.dumps({"tag": tag, "commit": commit, "assets": [
        "FolderOrganizer-Windows-x64.zip", "FolderOrganizer-Rust-Windows-x64.zip", "SHA256SUMS.txt"],
        "status": "formal build assets staged; publication follows final transfer verification"}))


if __name__ == "__main__":
    main()
