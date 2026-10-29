"""Publish documentation-only 0.6.1 packages with the existing native payloads."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

from repackage_legal_release import BASELINES, digest

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "P-JSL/Folder_Organizer"
TAG = "v0.6.1"
ORIGINAL_COMMIT = "4c05dfb18934b5edf14f501e328cb8fab4d7a7ec"
RELEASE_ID = 427584935


def run(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def release() -> dict:
    return json.loads(run("gh", "api", f"repos/{REPOSITORY}/releases/tags/{TAG}"))


def main() -> None:
    output = ROOT / "legal-dist"
    receipt = json.loads((output / "LEGAL_REPACKAGE.json").read_text(encoding="utf-8"))
    commit = run("git", "rev-parse", "HEAD")
    assert receipt["source_commit"] == commit and receipt["kind"] == "legal-documents-only"
    assert receipt["version"] == "0.6.1" and set(receipt["packages"]) == set(BASELINES)
    assert run("git", "rev-parse", f"{TAG}^{{commit}}") == ORIGINAL_COMMIT
    current = release()
    assert current["id"] == RELEASE_ID and current["target_commitish"] == ORIGINAL_COMMIT
    assert not current["draft"] and not current["prerelease"]
    assets = {item["name"]: item for item in current["assets"]}
    assert set(assets) == set(BASELINES) | {"SHA256SUMS.txt"}
    for name, (_, baseline) in BASELINES.items():
        package = receipt["packages"][name]
        assert assets[name]["digest"] == "sha256:" + baseline
        assert package["before_sha256"] == baseline and package["non_document_payload_unchanged"] is True
        assert digest(output / name) == package["after_sha256"]

    # Use the GitHub runner's release token. No rebuild or application/test run.
    run("gh", "release", "upload", TAG, *(str(output / name) for name in BASELINES),
        str(output / "SHA256SUMS.txt"), "--clobber", "--repo", REPOSITORY)
    body = (ROOT / "docs/RELEASE_NOTES_0.6.1.md").read_text(encoding="utf-8")
    body += (f"\n이용 조건·배포 문서 소스: [`{commit[:12]}`]"
             f"(https://github.com/{REPOSITORY}/commit/{commit}). "
             "실행 파일은 기존 v0.6.1과 동일하며 원래 버전 태그는 유지합니다.\n")
    notes = output / "RELEASE_BODY.md"
    notes.write_text(body, encoding="utf-8")
    run("gh", "release", "edit", TAG, "--repo", REPOSITORY, "--notes-file", str(notes))
    published = release()
    published_assets = {item["name"]: item for item in published["assets"]}
    assert published["id"] == RELEASE_ID and published["target_commitish"] == ORIGINAL_COMMIT
    assert set(published_assets) == set(BASELINES) | {"SHA256SUMS.txt"}
    for name, package in receipt["packages"].items():
        assert published_assets[name]["digest"] == "sha256:" + package["after_sha256"]
    assert published_assets["SHA256SUMS.txt"]["digest"] == "sha256:" + digest(output / "SHA256SUMS.txt")
    assert commit in published["body"]
    print(json.dumps({"release": published["html_url"], "version": "0.6.1",
                      "legal_source": commit, "native_payloads": "unchanged",
                      "builds": "not run", "application_tests": "not run"}))


if __name__ == "__main__":
    main()
