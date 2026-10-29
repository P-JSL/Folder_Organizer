"""Publish only the built ZIPs that completed the new changed-path gates."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import zipfile

from archive_review_release import ROOT, digest
from reuse_review_release import BUILD_COMMIT, PRIOR_RUN, WORKFLOW, check_source_changes

REPOSITORY = "P-JSL/Folder_Organizer"
COMPLETED_RUN = 40944704868
COMPLETED_REVIEW_COMMIT = "9e27008ecb9e4e175d8cc619f11b798ef7fc42a9"


def run(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT, text=True, encoding="utf-8").strip()


def api(path: str) -> dict:
    return json.loads(run("gh", "api", path))


def verify_completed_review(manifest: dict, results: Path, commit: str, run_id: str) -> dict:
    """Validate already-passed evidence without re-running either application."""
    if (run_id != str(COMPLETED_RUN) or manifest.get("source_commit") != BUILD_COMMIT
            or str(manifest.get("workflow_run")) != str(PRIOR_RUN)
            or manifest.get("version") != "0.7.0"):
        raise ValueError("Publication reuse is limited to the exact completed 0.7.0 build and review")
    check_source_changes(BUILD_COMMIT, commit)
    subprocess.run(["git", "merge-base", "--is-ancestor", COMPLETED_REVIEW_COMMIT, commit],
                   cwd=ROOT, check=True)
    changed = {name for name in run("git", "diff", "--name-only", "--no-renames", "-z",
                                   COMPLETED_REVIEW_COMMIT, commit).split("\0") if name}
    if not changed <= {WORKFLOW, "scripts/publish_review_release.py"}:
        raise ValueError("Completed review source changed beyond publication bookkeeping")
    prior = api(f"repos/{REPOSITORY}/actions/runs/{COMPLETED_RUN}")
    if (prior.get("repository", {}).get("full_name") != REPOSITORY
            or prior.get("head_sha") != COMPLETED_REVIEW_COMMIT or prior.get("path") != WORKFLOW
            or prior.get("event") != "workflow_dispatch" or prior.get("status") != "completed"
            or prior.get("conclusion") != "failure"):
        raise ValueError("Completed review run identity or completion state differs")
    response = api(f"repos/{REPOSITORY}/actions/runs/{COMPLETED_RUN}/jobs?filter=latest&per_page=100")
    jobs = response["jobs"]
    if response.get("total_count") != len(jobs):
        raise ValueError("Completed review job list is incomplete")
    for name in ("Preserve passed build and checks", "New artifact checks"):
        selected = [job for job in jobs if job.get("name") == name]
        if (len(selected) != 1 or selected[0].get("status") != "completed"
                or selected[0].get("conclusion") != "success"):
            raise ValueError("The reused review gate did not pass: " + name)
    failed = [job for job in jobs if job.get("conclusion") == "failure"]
    if len(failed) != 1 or failed[0].get("name") != "Publish reviewed release":
        raise ValueError("A prior failure occurred outside publication")
    benchmark = json.loads((results / "EDITIONS_BENCHMARK_0.7.0.json").read_text(encoding="utf-8-sig"))
    benchmark_source = benchmark.get("source", {})
    if (benchmark.get("suite") != "cross-engine-semantics-fresh-process-v070"
            or benchmark.get("native_windows") is not True
            or benchmark_source.get("git_commit") != COMPLETED_REVIEW_COMMIT):
        raise ValueError("Downloaded benchmark belongs to another review source")
    expected_programs = {
        "Python": manifest["packages"]["FolderOrganizer-Windows-x64.zip"]["program_sha256"]["FolderOrganizer.exe"],
        "Rust": manifest["packages"]["FolderOrganizer-Rust-Windows-x64.zip"]["program_sha256"]["FolderOrganizerRustCLI.exe"],
    }
    if benchmark_source.get("executable_sha256") != expected_programs:
        raise ValueError("Benchmark did not exercise the exact signed release executables")
    rows = benchmark.get("results", [])
    if ({row.get("distribution") for row in rows} != {"small", "large"} or len(rows) != 2
            or benchmark.get("repeats_per_engine_per_distribution", 0) < 5):
        raise ValueError("Completed benchmark distributions or repetitions are missing")
    for row in rows:
        runs = row.get("runs", [])
        if (row.get("source_bytes_unchanged") is not True
                or len(runs) != 2 * benchmark["repeats_per_engine_per_distribution"]
                or any(item.get("app_version") != "0.7.0" for item in runs)
                or any(sum(item.get("engine") == engine for item in runs)
                       != benchmark["repeats_per_engine_per_distribution"] for engine in expected_programs)):
            raise ValueError("Downloaded benchmark lacks the completed 0.7.0 observations")
    gui = json.loads((results / "RUST_GUI_CHECK.json").read_text(encoding="utf-8-sig"))
    package = json.loads((results / "PYTHON_PACKAGE_CHECK.json").read_text(encoding="utf-8-sig"))
    if (gui.get("version") != "0.7.0" or gui.get("native_gui_rendered") is not True
            or package.get("checks_reused") is not True or package.get("source_run") != PRIOR_RUN
            or package.get("build_commit") != BUILD_COMMIT
            or package.get("review_commit") != COMPLETED_REVIEW_COMMIT):
        raise ValueError("Downloaded package or GUI completion receipts differ")
    return {"completed_run": COMPLETED_RUN, "build_commit": BUILD_COMMIT,
            "review_commit": COMPLETED_REVIEW_COMMIT, "publication_commit": commit,
            "application_checks_rerun": False, "publication_only": True}


def existing_release(tag: str) -> dict | None:
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"],
                            cwd=ROOT, text=True, encoding="utf-8", capture_output=True)
    if result.returncode == 0:
        return json.loads(result.stdout)
    if "HTTP 404" in result.stderr:
        # GitHub's by-tag endpoint can omit an unpublished draft even for its
        # authorized owner. The authenticated release inventory includes it.
        pages = json.loads(run("gh", "api", "--paginate", "--slurp",
                               f"repos/{REPOSITORY}/releases?per_page=100"))
        found = [release for page in pages for release in page if release.get("tag_name") == tag]
        if len(found) > 1:
            raise ValueError("Multiple releases unexpectedly use the requested tag")
        return found[0] if found else None
    raise RuntimeError("Unable to inspect the existing release: " + result.stderr.strip())


def verify_empty_draft(release: dict, tag: str, build_commit: str) -> None:
    if (release.get("tag_name") != tag or release.get("draft") is not True
            or release.get("prerelease") is not False or release.get("assets") != []):
        raise ValueError("Existing release must be an empty stable-release draft")
    if release.get("target_commitish") == build_commit:
        return
    reference = api(f"repos/{REPOSITORY}/git/ref/tags/{tag}")["object"]
    for _ in range(4):
        if reference.get("type") == "commit":
            if reference.get("sha") != build_commit:
                raise ValueError("Existing release tag points to another compiled source")
            return
        if reference.get("type") != "tag":
            break
        reference = api(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    raise ValueError("Existing release target does not resolve to the compiled source")


def main() -> None:
    source = ROOT / "release-build"
    results = ROOT / "review-results"
    manifest = json.loads((source / "BUILD_INFO.json").read_text(encoding="utf-8"))
    commit = run("git", "rev-parse", "HEAD")
    if manifest["source_commit"] != commit:
        check_source_changes(manifest["source_commit"], commit)
    completed_run = os.environ.get("PUBLISH_COMPLETED_RUN_ID", "")
    publication_reuse = verify_completed_review(manifest, results, commit, completed_run) if completed_run else None
    if publication_reuse:
        (results / "PUBLICATION_REUSE.json").write_text(
            json.dumps(publication_reuse, indent=2) + "\n", encoding="utf-8")
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
    review_commit = COMPLETED_REVIEW_COMMIT if publication_reuse else commit
    body += f"\n정식 빌드 소스: `{manifest['source_commit']}`. 배포 검사 소스: `{review_commit}`. 게시 작업 소스: `{commit}`. 변경 경로의 새 Windows 검사·실제 패키지 실행·같은 입력 비교 자료는 `VALIDATION_RESULTS.zip`에 포함합니다.\n"
    if publication_reuse:
        body += f"\n완료한 검사 실행 `{COMPLETED_RUN}`의 결과와 빌드를 재사용해 게시만 진행했습니다. 이미 통과한 프로그램 검사는 반복하지 않았습니다.\n"
    if manifest["source_commit"] != commit:
        body += "\n빌드와 완료한 검사는 그대로 재사용했습니다. 최초 Python 패키지 확인의 마지막 콘솔 출력 인코딩 오류만 수정했으며, 해당 실행의 화면 파일은 보존되지 않아 첨부하지 않습니다. 재사용 근거는 첨부 기록에 포함합니다.\n"
    signing = manifest["authenticode"]
    body += f"\n실제 실행 파일 서명 모드: `{signing['mode']}`. 공인 신뢰 검증 완료: `{str(signing['public_trust_verified']).lower()}`. 자체 서명은 공인 게시자 인증을 의미하지 않습니다.\n"
    body += "\n| 실제 배포본 | ZIP 바이트 | 비압축 바이트 | 파일 수 |\n| --- | ---: | ---: | ---: |\n"
    for name, package in manifest["packages"].items():
        body += f"| {package['directory']} | {package['zip_bytes']:,} | {package['unpacked_bytes']:,} | {package['files']:,} |\n"
    notes = source / "RELEASE_BODY.md"
    notes.write_text(body, encoding="utf-8")
    tag = "v" + version
    assets = [*(str(source / name) for name in manifest["packages"]), str(source / "SHA256SUMS.txt"), str(evidence)]
    draft = existing_release(tag)
    if draft is None:
        run("gh", "release", "create", tag, *assets,
            "--repo", REPOSITORY, "--draft", "--target", manifest["source_commit"],
            "--title", f"Folder Organizer {tag} · Python / Rust", "--notes-file", str(notes))
    else:
        verify_empty_draft(draft, tag, manifest["source_commit"])
        # Deliberately omit --clobber: never replace or remove existing assets.
        run("gh", "release", "upload", tag, *assets, "--repo", REPOSITORY)
        run("gh", "release", "edit", tag, "--repo", REPOSITORY,
            "--title", f"Folder Organizer {tag} · Python / Rust", "--notes-file", str(notes))
    run("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest")
    print(json.dumps({"version": version, "source": manifest["source_commit"], "review_source": review_commit,
                      "publication_source": commit, "status": "published", "release":
                      f"https://github.com/{REPOSITORY}/releases/tag/{tag}"}))


if __name__ == "__main__":
    main()
