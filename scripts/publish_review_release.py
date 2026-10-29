"""Publish only the built ZIPs that completed the new changed-path gates."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zipfile

from archive_review_release import ROOT, digest

REPOSITORY = "P-JSL/Folder_Organizer"


def run(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def main() -> None:
    source = ROOT / "release-build"
    results = ROOT / "review-results"
    manifest = json.loads((source / "BUILD_INFO.json").read_text(encoding="utf-8"))
    commit = run("git", "rev-parse", "HEAD")
    if manifest["source_commit"] != commit:
        raise ValueError("Release source and actual compiled artifacts differ")
    version = manifest["version"]
    for name, package in manifest["packages"].items():
        if digest(source / name) != package["sha256"]:
            raise ValueError("Release archive changed after its native gates")
    evidence = source / "VALIDATION_RESULTS.zip"
    with zipfile.ZipFile(evidence, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(results.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(results).as_posix())
        archive.write(source / "BUILD_INFO.json", "BUILD_INFO.json")
        archive.write(source / "PYTHON_PACKAGE_LAYOUT.json", "PYTHON_PACKAGE_LAYOUT.json")
    body = (ROOT / f"docs/RELEASE_NOTES_{version}.md").read_text(encoding="utf-8")
    body += f"\n정식 빌드 소스: `{commit}`. 변경 경로의 새 Windows 검사·실제 패키지 실행·같은 입력 비교 자료는 `VALIDATION_RESULTS.zip`에 포함합니다.\n"
    body += "\n| 실제 배포본 | ZIP 바이트 | 비압축 바이트 | 파일 수 |\n| --- | ---: | ---: | ---: |\n"
    for name, package in manifest["packages"].items():
        body += f"| {package['directory']} | {package['zip_bytes']:,} | {package['unpacked_bytes']:,} | {package['files']:,} |\n"
    notes = source / "RELEASE_BODY.md"
    notes.write_text(body, encoding="utf-8")
    tag = "v" + version
    run("gh", "release", "create", tag, *(str(source / name) for name in manifest["packages"]),
        str(source / "SHA256SUMS.txt"), str(evidence), "--repo", REPOSITORY, "--draft", "--target", commit,
        "--title", f"Folder Organizer {tag} · Python / Rust", "--notes-file", str(notes))
    run("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest")
    print(json.dumps({"version": version, "source": commit, "status": "published", "release":
                      f"https://github.com/{REPOSITORY}/releases/tag/{tag}"}))


if __name__ == "__main__":
    main()
