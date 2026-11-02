"""Publish 0.9.1 formal artifacts while reusing the passed 45 workspace cases.

No test, GUI, screenshot driver, source rebuild or asset replacement runs here.
The release operator prepares an immutable source tag and a draft first.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile

from archive_review_release import ROOT, digest
from publish_extended_release import api, archive_bytes, read_json, run, verify_asset_subset
from reuse_workspace_actions_checks import verify_reused_source

REPOSITORY = "P-JSL/Folder_Organizer"
VERSION = "0.9.1"
TAG = "v" + VERSION
WORKFLOW = ".github/workflows/workspace-actions-release.yml"
PACKAGES = {
    "FolderOrganizer-Windows-x64.zip": ("FolderOrganizer", REPOSITORY),
    "FolderOrganizer-Rust-Windows-x64.zip": ("FolderOrganizerRust", REPOSITORY + ".Rust"),
}
PUBLICATION_ONLY_PATHS = {
    WORKFLOW, "scripts/publish_workspace_actions_release.py", "README.md", "CHANGELOG.md",
}


def verify_publication_source(compiled: str, publication: str) -> list[str]:
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (compiled, publication)):
        raise ValueError("Immutable full source commits are required")
    subprocess.run(["git", "merge-base", "--is-ancestor", compiled, publication], cwd=ROOT, check=True)
    changes = [name for name in run("git", "diff", "--name-only", "--no-renames", "-z",
                                    compiled, publication).split("\0") if name]
    forbidden = [name for name in changes if name not in PUBLICATION_ONLY_PATHS and not name.startswith("docs/")]
    if forbidden:
        raise ValueError("Application, packaging or reused-check inputs changed after compilation: " + ", ".join(forbidden))
    return changes


def verify_build_and_reuse(manifest: dict, run_id: str) -> dict:
    if not run_id.isdigit() or str(manifest.get("workflow_run")) != run_id:
        raise ValueError("The exact successful 0.9.1 formal build execution is required")
    source = manifest["source_commit"]
    execution = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    if (execution.get("repository", {}).get("full_name") != REPOSITORY
            or execution.get("path") != WORKFLOW or execution.get("event") != "workflow_dispatch"
            or execution.get("head_sha") != source or execution.get("status") != "completed"
            or execution.get("conclusion") != "success"):
        raise ValueError("Formal build identity or completion differs")
    response = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = response["jobs"]
    if response.get("total_count") != len(jobs):
        raise ValueError("Formal build job inventory is incomplete")
    gates = {"build_source": source, "build_run": run_id}
    for name in ("Reuse passed workspace checks", "Formal Windows builds"):
        found = [job for job in jobs if job.get("name") == name]
        if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != "success":
            raise ValueError("Required formal packaging gate did not complete: " + name)
        gates[name] = {"job_id": found[0]["id"], "conclusion": "success"}
    receipt = read_json(ROOT / "reuse-results/REUSED_CHECKS_V091.json")
    if (receipt.get("schema") != "folder-organizer.workspace-check-reuse.v091"
            or receipt.get("version") != VERSION or receipt.get("status") != "passed"
            or receipt.get("source_commit") != source or receipt.get("total_reused_cases") != 45
            or receipt.get("tests_rerun") is not False or receipt.get("captures") is not False):
        raise ValueError("The passed workspace-case reuse receipt differs")
    # Recheck the source boundary, rather than running any of those cases.
    changes = verify_reused_source(source)
    if receipt.get("allowed_source_changes") != changes:
        raise ValueError("Version-only source reuse proof differs")
    gates["reused_cases"] = 45
    gates["tests_rerun"] = False
    gates["captures"] = False
    return gates


def prepared_draft(compiled: str) -> dict:
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases?per_page=100"))
    found = [release for page in pages for release in page if release.get("tag_name") == TAG]
    if len(found) != 1:
        raise ValueError("Prepare exactly one v0.9.1 draft for the compiled source")
    release = found[0]
    if (release.get("draft") is not True or release.get("prerelease") is not False
            or release.get("target_commitish") != compiled or not isinstance(release.get("assets"), list)):
        raise ValueError("Refusing to replace a published release or use another source's draft")
    reference = api(f"repos/{REPOSITORY}/git/ref/tags/{TAG}")["object"]
    for _ in range(4):
        if reference.get("type") == "commit":
            if reference.get("sha") != compiled:
                raise ValueError("Release tag differs from exact compiled source")
            return release
        if reference.get("type") != "tag":
            break
        reference = api(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    raise ValueError("Release tag cannot be resolved to the compiled source")


def verify_package_identity(directory, manifest: dict) -> None:
    if manifest.get("version") != VERSION or set(manifest.get("packages", {})) != set(PACKAGES):
        raise ValueError("Only the two formal 0.9.1 editions may be published")
    for filename, (folder, identity) in PACKAGES.items():
        package = manifest["packages"][filename]
        path = directory / filename
        if (package.get("version") != VERSION or package.get("directory") != folder
                or package.get("app_id") != identity or digest(path) != package.get("sha256")
                or path.stat().st_size != package.get("zip_bytes")):
            raise ValueError("Formal package identity or transfer checksum differs: " + filename)
        with zipfile.ZipFile(path) as archive:
            marker = json.loads(archive.read(folder + "/version.json"))
            if marker != {"app_id": identity, "version": VERSION}:
                raise ValueError("Archived version marker differs: " + filename)
            for program, checksum in package["program_sha256"].items():
                import hashlib
                with archive.open(folder + "/" + program) as executable:
                    if hashlib.file_digest(executable, "sha256").hexdigest() != checksum:
                        raise ValueError("Archived executable transfer identity differs: " + program)
    expected = "".join(f"{package['sha256']}  {name}\n" for name, package in manifest["packages"].items())
    if (directory / "SHA256SUMS.txt").read_text(encoding="utf-8-sig") != expected:
        raise ValueError("Shared checksum file differs from exact formal ZIPs")


def main() -> None:
    directory = ROOT / "release-build"
    manifest = read_json(directory / "BUILD_INFO.json")
    verify_package_identity(directory, manifest)
    source = manifest["source_commit"]
    publication = run("git", "rev-parse", "HEAD")
    changes = verify_publication_source(source, publication)
    run_id = os.environ.get("PUBLISH_RUN_ID", "")
    gates = verify_build_and_reuse(manifest, run_id)
    draft = prepared_draft(source)
    chain = {"version": VERSION, "compiled_source": source, "build_run": run_id,
             "publication_source": publication, "publication_run": os.environ.get("GITHUB_RUN_ID"),
             "publication_only_changes": changes, "gates": gates, "release_draft_id": draft["id"],
             "test_execution": "all previously passed tests skipped; 45 existing workspace-case results reused",
             "captures": False,
             "packages": {name: package["sha256"] for name, package in manifest["packages"].items()}}
    evidence = directory / "VALIDATION_RESULTS.zip"
    with zipfile.ZipFile(evidence, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("BUILD_INFO.json", "PYTHON_PACKAGE_LAYOUT.json"):
            archive_bytes(archive, name, (directory / name).read_bytes())
        for path in sorted((ROOT / "reuse-results").rglob("*")):
            if path.is_file():
                archive_bytes(archive, "reused-checks/" + path.relative_to(ROOT / "reuse-results").as_posix(), path.read_bytes())
        archive_bytes(archive, "SOURCE_CHAIN.json", (json.dumps(chain, indent=2) + "\n").encode("utf-8"))
    body = (ROOT / "docs/RELEASE_NOTES_0.9.1.md").read_text(encoding="utf-8")
    body += f"\n정식 빌드 소스: `{source}` · 빌드 실행: `{run_id}` · 게시 소스: `{publication}`.\n"
    body += "\n이번 릴리즈에서는 앱 검사·CLI·GUI·화면 캡처를 실행하지 않았습니다. 이전에 통과한 새 작업 공간 사례 45건을 재사용했고, 같은 앱 소스와 버전 표식 변경 범위·원래 실행 결과를 `VALIDATION_RESULTS.zip`에 기록했습니다.\n"
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
        raise ValueError("Release changed before exact asset transfer verification completed")
    verify_asset_subset(uploaded, expected, complete=True)
    run("gh", "release", "edit", TAG, "--repo", REPOSITORY, "--title", f"Folder Organizer {TAG} · Python / Rust",
        "--notes-file", str(notes), "--draft=false", "--latest")
    published = api(f"repos/{REPOSITORY}/releases/tags/{TAG}")
    if (published.get("id") != draft["id"] or published.get("draft") or published.get("prerelease")
            or published.get("target_commitish") != source):
        raise ValueError("Published metadata differs from verified upload")
    verify_asset_subset(published, expected, complete=True)
    latest = api(f"repos/{REPOSITORY}/releases/latest")
    if latest.get("id") != draft["id"]:
        raise ValueError("Latest release does not identify the verified 0.9.1 publication")
    print(json.dumps({"version": VERSION, "source": source, "status": "published",
                      "reused_cases": 45, "tests_rerun": False, "captures": False,
                      "release": published["html_url"]}))


if __name__ == "__main__":
    main()
