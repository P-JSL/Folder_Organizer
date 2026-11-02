"""Publish exact 0.8.0 formal artifacts after only the new Windows gates.

Creation of the tag and empty release draft belongs to the authenticated release
operator. This program uploads without clobbering and never moves an old tag.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

from archive_review_release import ROOT, digest

REPOSITORY = "P-JSL/Folder_Organizer"
VERSION = "0.8.0"
TAG = "v" + VERSION
WORKFLOW = ".github/workflows/extended-release.yml"
PRIOR_CHECK_SOURCE = "3c63f82dafcc7710af30b83a5b6223bf56622abf"
PACKAGES = {
    "FolderOrganizer-Windows-x64.zip": ("FolderOrganizer", REPOSITORY),
    "FolderOrganizer-Rust-Windows-x64.zip": ("FolderOrganizerRust", REPOSITORY + ".Rust"),
}
PUBLICATION_ONLY_PATHS = {
    WORKFLOW, "scripts/publish_extended_release.py",
    "docs/RELEASE_NOTES_0.8.0.md", "docs/RELEASE_NOTES.md",
}


def run(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def api(path: str):
    return json.loads(run("gh", "api", path))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def verify_sources(compiled: str, publication: str) -> list[str]:
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (compiled, publication)):
        raise ValueError("A full immutable source commit is required")
    subprocess.run(["git", "merge-base", "--is-ancestor", compiled, publication], cwd=ROOT, check=True)
    changes = [name for name in run("git", "diff", "--name-only", "--no-renames", "-z",
                                    compiled, publication).split("\0") if name]
    if not set(changes) <= PUBLICATION_ONLY_PATHS:
        raise ValueError("Compiled application or gate inputs changed after validation: " +
                         ", ".join(sorted(set(changes) - PUBLICATION_ONLY_PATHS)))
    return changes


def verify_gates(manifest: dict, run_id: str) -> dict:
    if not run_id.isdigit() or str(manifest.get("workflow_run")) != run_id:
        raise ValueError("Artifact source and selected workflow run differ")
    source = manifest["source_commit"]
    execution = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    if (execution.get("repository", {}).get("full_name") != REPOSITORY
            or execution.get("head_sha") != source or execution.get("path") != WORKFLOW
            or execution.get("event") != "workflow_dispatch"):
        raise ValueError("Artifact workflow identity differs from the formal source")
    response = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = response["jobs"]
    if response.get("total_count") != len(jobs):
        raise ValueError("The formal gate job inventory is incomplete")
    gates = {}
    for name in ("Formal Windows builds", "New Windows contracts", "New frozen feature checks"):
        found = [job for job in jobs if job.get("name") == name]
        if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != "success":
            raise ValueError("Required new gate did not pass: " + name)
        gates[name] = {"job_id": found[0]["id"], "conclusion": "success"}
    native = read_json(ROOT / "native-results/WINDOWS_NATIVE_V080.json")
    feature = read_json(ROOT / "review-results/WINDOWS_RELEASE_V080.json")
    for receipt in (native, feature):
        if (receipt.get("version") != VERSION or receipt.get("source_commit") != source
                or receipt.get("status") != "passed"):
            raise ValueError("New Windows evidence does not match the exact compiled source")
    if (native.get("schema") != "folder-organizer.native-windows.v080"
            or native.get("exit_code") != 0 or str(native.get("workflow_run")) != run_id):
        raise ValueError("The new native contract target did not complete successfully")
    expected_programs = {name: checksum for package in manifest["packages"].values()
                         for name, checksum in package["program_sha256"].items()}
    if (feature.get("schema") != "folder-organizer.windows-release.v080"
            or feature.get("program_sha256") != expected_programs):
        raise ValueError("Frozen feature checks did not exercise the exact five release executables")
    for edition in ("Python", "Rust"):
        if feature.get("gui", {}).get(edition, {}).get("exit_code") != 0:
            raise ValueError("The new frozen multi-folder window did not complete: " + edition)
    return gates


def prepared_draft(compiled: str) -> dict:
    # The by-tag REST endpoint may omit a draft; the authenticated inventory does
    # include drafts. No creation or deletion is attempted by this publisher.
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/releases?per_page=100"))
    found = [release for page in pages for release in page if release.get("tag_name") == TAG]
    if len(found) != 1:
        raise ValueError("Prepare exactly one empty v0.8.0 draft for the compiled source before publication")
    release = found[0]
    if (release.get("draft") is not True or release.get("prerelease") is not False
            or not isinstance(release.get("assets"), list) or release.get("target_commitish") != compiled):
        raise ValueError("Refusing to replace published assets or a draft for another source")
    reference = api(f"repos/{REPOSITORY}/git/ref/tags/{TAG}")["object"]
    for _ in range(4):
        if reference.get("type") == "commit":
            if reference.get("sha") != compiled:
                raise ValueError("The immutable release tag does not point at the compiled source")
            return release
        if reference.get("type") != "tag":
            break
        reference = api(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    raise ValueError("The release tag cannot be resolved to the compiled source")


def verify_asset_subset(release: dict, expected: dict[str, str], *, complete: bool) -> list[str]:
    assets = release["assets"]
    actual = {asset["name"]: asset for asset in assets}
    if (len(actual) != len(assets) or not set(actual) <= set(expected)
            or any(asset.get("state") != "uploaded" or asset.get("digest") != expected[name]
                   for name, asset in actual.items())):
        raise ValueError("Refusing to overwrite foreign, unfinished or different draft assets")
    missing = [name for name in expected if name not in actual]
    if complete and missing:
        raise ValueError("The exact release payload set is incomplete")
    return missing


def archive_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    # Artifact extraction times differ across runners. Fixed ZIP entry metadata
    # makes a failed-job retry reproduce the same evidence asset without clobber.
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(entry, data, compresslevel=6)


def main() -> None:
    directory = ROOT / "release-build"
    manifest = read_json(directory / "BUILD_INFO.json")
    if manifest.get("version") != VERSION or set(manifest.get("packages", {})) != set(PACKAGES):
        raise ValueError("Only the two formal 0.8.0 packages may be published")
    source = manifest["source_commit"]
    publication = run("git", "rev-parse", "HEAD")
    changes = verify_sources(source, publication)
    for filename, (folder, identity) in PACKAGES.items():
        package = manifest["packages"][filename]
        if (package.get("version") != VERSION or package.get("directory") != folder
                or package.get("app_id") != identity or digest(directory / filename) != package.get("sha256")):
            raise ValueError("Formal ZIP identity or transfer checksum differs: " + filename)
    expected_checksums = "".join(f"{manifest['packages'][name]['sha256']}  {name}\n" for name in manifest["packages"])
    if (directory / "SHA256SUMS.txt").read_text(encoding="utf-8-sig") != expected_checksums:
        raise ValueError("The shared checksum file differs from the exact verified ZIPs")
    run_id = os.environ.get("PUBLISH_RUN_ID", "")
    gates = verify_gates(manifest, run_id)
    draft = prepared_draft(source)
    chain = {"version": VERSION, "compiled_source": source, "validation_run": run_id,
             "publication_source": publication, "publication_run": os.environ.get("GITHUB_RUN_ID"),
             "publication_only_changes": changes, "gates": gates,
             "release_draft_id": draft["id"], "previous_checks": {"unique_cases": 52,
                 "source_commit": PRIOR_CHECK_SOURCE, "status": "previously passed; not repeated",
                 "document": "docs/MULTIROOT_ANALYSIS.md"},
             "packages": {name: package["sha256"] for name, package in manifest["packages"].items()}}
    evidence = directory / "VALIDATION_RESULTS.zip"
    with zipfile.ZipFile(evidence, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("BUILD_INFO.json", "PYTHON_PACKAGE_LAYOUT.json"):
            archive_bytes(archive, name, (directory / name).read_bytes())
        for folder in ("native-results", "review-results"):
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file():
                    archive_bytes(archive, f"{folder}/{path.relative_to(ROOT / folder).as_posix()}", path.read_bytes())
        archive_bytes(archive, "SOURCE_CHAIN.json", (json.dumps(chain, indent=2) + "\n").encode("utf-8"))
    body = (ROOT / "docs/RELEASE_NOTES_0.8.0.md").read_text(encoding="utf-8")
    body += f"\n정식 빌드·새 Windows 검사 소스: `{source}`. 검사 실행: `{run_id}`. 게시 소스: `{publication}`.\n"
    body += "\n기존에 통과한 확장 개선 검사 52건은 재실행하지 않았습니다. 이번 새 Windows 계약·실제 배포 CLI·다중 폴더 GUI와 화면 증거는 `VALIDATION_RESULTS.zip`에 보존합니다.\n"
    signing = manifest["authenticode"]
    body += f"\n실제 서명 모드: `{signing['mode']}` · 공인 신뢰 검증: `{str(signing['public_trust_verified']).lower()}`. 자체 서명은 공인 게시자 인증을 의미하지 않습니다.\n"
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
    # Automatic update clients see the new version only after all four exact
    # asset digests have been checked while the release is still unpublished.
    run("gh", "release", "edit", TAG, "--repo", REPOSITORY, "--title", f"Folder Organizer {TAG} · Python / Rust",
        "--notes-file", str(notes), "--draft=false", "--latest")
    published = api(f"repos/{REPOSITORY}/releases/tags/{TAG}")
    if (published.get("id") != draft["id"] or published.get("draft") or published.get("prerelease")
            or published.get("target_commitish") != source):
        raise ValueError("Published release metadata differs from the verified upload")
    verify_asset_subset(published, expected, complete=True)
    print(json.dumps({"version": VERSION, "source": source, "publication_source": publication,
                      "status": "published", "release": published["html_url"]}))


if __name__ == "__main__":
    main()
