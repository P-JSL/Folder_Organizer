"""Publish the exact 0.9 formal packages after only new main-workspace checks.

The operator prepares an immutable source tag and draft. No release creation,
old checks, old native targets, source rebuild, or asset replacement occurs here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile

from archive_review_release import ROOT, digest
from publish_extended_release import api, archive_bytes, read_json, run, verify_asset_subset

REPOSITORY = "P-JSL/Folder_Organizer"
VERSION = "0.9.0"
TAG = "v" + VERSION
WORKFLOW = ".github/workflows/workspace-release.yml"
PACKAGES = {
    "FolderOrganizer-Windows-x64.zip": ("FolderOrganizer", REPOSITORY),
    "FolderOrganizer-Rust-Windows-x64.zip": ("FolderOrganizerRust", REPOSITORY + ".Rust"),
}
PUBLICATION_ONLY_PATHS = {WORKFLOW, "scripts/publish_workspace_release.py", "README.md", "CHANGELOG.md"}


def publication_only(path: str) -> bool:
    return path in PUBLICATION_ONLY_PATHS or path.startswith("docs/")


def verify_sources(compiled: str, publication: str) -> list[str]:
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (compiled, publication)):
        raise ValueError("Immutable full source commits are required")
    subprocess.run(["git", "merge-base", "--is-ancestor", compiled, publication], cwd=ROOT, check=True)
    changes = [name for name in run("git", "diff", "--name-only", "--no-renames", "-z",
                                    compiled, publication).split("\0") if name]
    forbidden = [name for name in changes if not publication_only(name)]
    if forbidden:
        raise ValueError("Application or workspace validation inputs changed after compilation: " +
                         ", ".join(forbidden))
    return changes


def verify_workspace_evidence(manifest: dict, source: str, run_id: str) -> dict:
    receipt = read_json(ROOT / "workspace-results/WINDOWS_WORKSPACE_V090.json")
    expected_programs = {name: checksum for package in manifest["packages"].values()
                         for name, checksum in package["program_sha256"].items()}
    if (receipt.get("schema") != "folder-organizer.windows-workspace.v090"
            or receipt.get("version") != VERSION or receipt.get("status") != "passed"
            or receipt.get("source_commit") != source or receipt.get("build_source_commit") != source
            or str(receipt.get("workflow_run")) != run_id
            or receipt.get("program_sha256") != expected_programs
            or receipt.get("source_bytes_unchanged") is not True):
        raise ValueError("New main-workspace evidence differs from the exact compiled programs")
    for edition in ("Python", "Rust"):
        proof = receipt.get("gui", {}).get(edition, {})
        if (proof.get("exit_code") != 0 or proof.get("checked_folders") != 2
                or proof.get("file_rows") != 6 or proof.get("duplicate_groups") != 1
                or proof.get("main_workspace") is not True
                or proof.get("command", [])[-1:] != ["--workspace-smoke"]):
            raise ValueError("New main workspace did not finish: " + edition)
        for key, filename in (("capture", f"{edition}_WORKSPACE_GUI.png"),
                              ("report", f"{edition}_WORKSPACE_GUI.json")):
            path = ROOT / "workspace-results" / filename
            if not path.is_file() or proof.get(key, {}).get("sha256") != digest(path):
                raise ValueError("New main-workspace evidence is missing or differs: " + filename)
        capture = proof["capture"]
        if (capture.get("native_visible_window") is not True
                or capture.get("sampled_body_colors", 0) < 16):
            raise ValueError("Main workspace has no actual rendered Windows capture: " + edition)
    return receipt


def verify_gates(manifest: dict, run_id: str) -> dict:
    if not run_id.isdigit() or str(manifest.get("workflow_run")) != run_id:
        raise ValueError("Publication requires the exact successful 0.9 build execution")
    source = manifest["source_commit"]
    execution = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    if (execution.get("repository", {}).get("full_name") != REPOSITORY
            or execution.get("path") != WORKFLOW or execution.get("event") != "workflow_dispatch"
            or execution.get("head_sha") != source):
        raise ValueError("Formal build and new workspace check source identities differ")
    response = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = response["jobs"]
    if response.get("total_count") != len(jobs):
        raise ValueError("Incomplete new workspace gate job inventory")
    gates = {"compiled_source": source, "build_run": run_id, "validation_run": run_id}
    for name in ("Formal Windows builds", "New main workspace Windows checks"):
        found = [job for job in jobs if job.get("name") == name]
        if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != "success":
            raise ValueError("Required new gate did not pass: " + name)
        gates[name] = {"job_id": found[0]["id"], "conclusion": "success"}
    verify_workspace_evidence(manifest, source, run_id)
    return gates


def prepared_draft(compiled: str) -> dict:
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases?per_page=100"))
    found = [release for page in pages for release in page if release.get("tag_name") == TAG]
    if len(found) != 1:
        raise ValueError("Prepare exactly one v0.9.0 draft for the compiled source")
    release = found[0]
    if (release.get("draft") is not True or release.get("prerelease") is not False
            or not isinstance(release.get("assets"), list) or release.get("target_commitish") != compiled):
        raise ValueError("Refusing to replace published assets or a draft for another source")
    reference = api(f"repos/{REPOSITORY}/git/ref/tags/{TAG}")["object"]
    for _ in range(4):
        if reference.get("type") == "commit":
            if reference.get("sha") != compiled:
                raise ValueError("Immutable release tag differs from the compiled source")
            return release
        if reference.get("type") != "tag":
            break
        reference = api(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    raise ValueError("Release tag does not resolve to the exact compiled source")


def main() -> None:
    directory = ROOT / "release-build"
    manifest = read_json(directory / "BUILD_INFO.json")
    if manifest.get("version") != VERSION or set(manifest.get("packages", {})) != set(PACKAGES):
        raise ValueError("Only the two formal 0.9.0 packages may be published")
    source = manifest["source_commit"]
    publication = run("git", "rev-parse", "HEAD")
    changes = verify_sources(source, publication)
    for filename, (folder, identity) in PACKAGES.items():
        package = manifest["packages"][filename]
        if (package.get("version") != VERSION or package.get("directory") != folder
                or package.get("app_id") != identity or digest(directory / filename) != package.get("sha256")):
            raise ValueError("Formal ZIP identity or transfer checksum differs: " + filename)
    expected_checksums = "".join(f"{package['sha256']}  {name}\n" for name, package in manifest["packages"].items())
    if (directory / "SHA256SUMS.txt").read_text(encoding="utf-8-sig") != expected_checksums:
        raise ValueError("Shared checksum file differs from exact formal ZIPs")
    run_id = os.environ.get("PUBLISH_RUN_ID", "")
    gates = verify_gates(manifest, run_id)
    draft = prepared_draft(source)
    chain = {"version": VERSION, "compiled_source": source, "validation_run": run_id,
             "publication_source": publication, "publication_run": os.environ.get("GITHUB_RUN_ID"),
             "publication_only_changes": changes, "gates": gates, "release_draft_id": draft["id"],
             "previous_checks": "all existing tests and old CLI/native/GUI drivers skipped by user request",
             "packages": {name: package["sha256"] for name, package in manifest["packages"].items()}}
    evidence = directory / "VALIDATION_RESULTS.zip"
    with zipfile.ZipFile(evidence, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("BUILD_INFO.json", "PYTHON_PACKAGE_LAYOUT.json"):
            archive_bytes(archive, name, (directory / name).read_bytes())
        for path in sorted((ROOT / "workspace-results").rglob("*")):
            if path.is_file():
                archive_bytes(archive, f"workspace-results/{path.relative_to(ROOT / 'workspace-results').as_posix()}",
                              path.read_bytes())
        archive_bytes(archive, "SOURCE_CHAIN.json", (json.dumps(chain, indent=2) + "\n").encode("utf-8"))
    body = (ROOT / "docs/RELEASE_NOTES_0.9.0.md").read_text(encoding="utf-8")
    body += f"\n정식 빌드·새 메인 UI 확인 소스: `{source}` · 실행: `{run_id}`. 게시 소스: `{publication}`.\n"
    body += "\n기존 검사는 모두 스킵했습니다. 두 정식 배포본의 새 메인 화면에서 폴더 2개, 파일 6개, 중복 그룹 1개와 파일 정보 패널을 확인했으며 실제 Windows 화면과 로그를 `VALIDATION_RESULTS.zip`에 보존합니다.\n"
    signing = manifest["authenticode"]
    body += f"\n실제 서명 모드: `{signing['mode']}` · 공인 신뢰 검증: `{str(signing['public_trust_verified']).lower()}`.\n"
    body += "\n| 정식 배포본 | ZIP 바이트 | 비압축 바이트 | 파일 수 |\n| --- | ---: | ---: | ---: |\n"
    for package in manifest["packages"].values():
        body += f"| {package['directory']} | {package['zip_bytes']:,} | {package['unpacked_bytes']:,} | {package['files']:,} |\n"
    notes = directory / "RELEASE_BODY.md"
    notes.write_text(body, encoding="utf-8")
    assets = [directory / name for name in PACKAGES] + [directory / "SHA256SUMS.txt", evidence]
    expected = {path.name: "sha256:" + digest(path) for path in assets}
    missing = verify_asset_subset(draft, expected, complete=False)
    if missing:
        run("gh", "release", "upload", TAG, *(str(directory / name) for name in missing), "--repo", REPOSITORY)
    uploaded = api(f"repos/{REPOSITORY}/releases/{draft['id']}")
    if uploaded.get("draft") is not True or uploaded.get("target_commitish") != source:
        raise ValueError("Release state changed before transfer verification completed")
    verify_asset_subset(uploaded, expected, complete=True)
    run("gh", "release", "edit", TAG, "--repo", REPOSITORY, "--title", f"Folder Organizer {TAG} · Python / Rust",
        "--notes-file", str(notes), "--draft=false", "--latest")
    published = api(f"repos/{REPOSITORY}/releases/tags/{TAG}")
    if (published.get("id") != draft["id"] or published.get("draft") or published.get("prerelease")
            or published.get("target_commitish") != source):
        raise ValueError("Published release metadata differs from verified upload")
    verify_asset_subset(published, expected, complete=True)
    print(json.dumps({"version": VERSION, "source": source, "publication_source": publication,
                      "status": "published", "release": published["html_url"]}))


if __name__ == "__main__":
    main()
