"""Promote an already tested Windows artifact; never publish a release.

Only the stable README/CHANGELOG may replace files inside the tested package.
The application, updater, dependencies and every other payload byte stay intact.
No dependency installation, test suite or native rebuild is needed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from folder_organizer import updater  # stdlib-only modules
from folder_organizer import updater_helper as helper

REPOSITORY = "P-JSL/Folder_Organizer"
APPROVED_DOCUMENTS = {
    "README.md", "CHANGELOG.md", "docs/RELEASE_NOTES.md",
    "docs/RELEASE_NOTES_0.5.0.md", "docs/GUI_GUIDE.md",
    "docs/BUILD_REVIEW_0.5.0.md",
}
REQUIRED_STEPS = {
    "Run python -m pytest -q --junitxml=test-results.xml",
    "Build portable desktop app", "Build independent update helper",
    "Include update marker", "Package and checksum",
    "Verify compiled GUI, inventory and automatic update helper",
    "Verify installed v0.4.0 upgrades to this release",
}
ARTIFACT_NAME = "FolderOrganizer-Windows-x64"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def command(*args: str, output=None) -> bytes:
    result = subprocess.run(args, cwd=ROOT, stdout=output or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=240)
    if result.returncode:
        # GitHub artifact errors may contain signed redirect URLs.
        detail = re.sub(r"https?://[^\s]+", "[URL]", result.stderr.decode(errors="replace"))
        raise RuntimeError(f"{args[0]} failed: {detail[:500]}")
    return result.stdout or b""


def api(suffix: str):
    return json.loads(command("gh", "api", f"repos/{REPOSITORY}/{suffix}"))


def remote_tag_commit(tag: str) -> str:
    obj = api(f"git/ref/tags/{tag}")["object"]
    for _ in range(4):
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Release ref is not a commit or annotated tag")
        obj = api(f"git/tags/{obj['sha']}")["object"]
    raise RuntimeError("Too many nested release tags")


def draft_release(tag: str) -> dict:
    # The tag endpoint may hide drafts even from this task's GitHub binding.
    # List releases using the same authenticated CLI and require one exact match.
    releases = api("releases?per_page=100")
    matches = [entry for entry in releases if entry["tag_name"] == tag]
    require(len(matches) == 1, "Pinned release draft is not visible")
    release = matches[0]
    require(release["tag_name"] == tag and release["draft"] is True
            and release["prerelease"] is False, "An existing stable draft release is required")
    require(not release["assets"], "Draft already has assets; refusing to overwrite them")
    return release


def provenance(config: dict) -> dict:
    tag, release_sha, candidate_sha = (config[k] for k in ("tag", "release_sha", "candidate_sha"))
    require(bool(re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)), "Invalid stable version tag")
    require(all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (release_sha, candidate_sha)), "Full commit SHAs are required")
    require(all(re.fullmatch(r"[1-9][0-9]*", config[k]) for k in ("candidate_run_id", "artifact_id")), "Numeric run and artifact IDs are required")
    require(remote_tag_commit(tag) == release_sha, "Remote release tag changed")
    local = command("git", "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}").decode().strip()
    require(local == release_sha, "Local release tag does not match the pinned commit")
    require(command("git", "rev-parse", "HEAD:folder_organizer") == command(
        "git", "rev-parse", f"{release_sha}:folder_organizer"),
        "Promotion's archive validation helpers differ from the pinned release")
    changed = command("git", "diff", "--name-only", "-z", candidate_sha, release_sha, "--")
    changed_files = {p.decode("utf-8") for p in changed.split(b"\0") if p}
    require(changed_files <= APPROVED_DOCUMENTS, "Release changed files outside the approved documentation")
    run = api(f"actions/runs/{config['candidate_run_id']}")
    require(run["id"] == int(config["candidate_run_id"]) and run["head_sha"] == candidate_sha,
            "Candidate run does not match its pinned commit")
    require(run["repository"]["full_name"] == REPOSITORY
            and run["head_repository"]["full_name"] == REPOSITORY,
            "Candidate run belongs to another repository")
    require(run["status"] == "completed" and run["conclusion"] == "success"
            and run["event"] == "workflow_dispatch"
            and run["path"] == ".github/workflows/release.yml", "Candidate was not a successful review build")
    jobs = api(f"actions/runs/{config['candidate_run_id']}/jobs?per_page=100")
    require(jobs["total_count"] <= 100, "Candidate job list is incomplete")
    verified = any(job["conclusion"] == "success" and REQUIRED_STEPS <= {
        step["name"] for step in job.get("steps", [])
        if step["status"] == "completed" and step["conclusion"] == "success"
    } for job in jobs["jobs"])
    require(verified, "Candidate lacks successful required tests and packaged execution checks")
    artifact_list = api(f"actions/runs/{config['candidate_run_id']}/artifacts?per_page=100")
    require(artifact_list["total_count"] <= 100, "Candidate artifact list is incomplete")
    selected = [entry for entry in artifact_list["artifacts"] if entry["id"] == int(config["artifact_id"])]
    require(len(selected) == 1, "Pinned artifact is not present in the candidate run")
    artifact = selected[0]
    require(artifact["id"] == int(config["artifact_id"]) and artifact["name"] == ARTIFACT_NAME
            and artifact["expired"] is False, "Candidate artifact is missing, expired or unexpected")
    require(artifact["workflow_run"]["id"] == run["id"]
            and artifact["workflow_run"]["head_sha"] == candidate_sha
            and artifact["workflow_run"]["repository_id"] == run["repository"]["id"],
            "Artifact does not belong to the pinned candidate run")
    require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", ""))), "Artifact has no SHA-256 provenance digest")
    require(0 < artifact["size_in_bytes"] <= updater.MAX_ARCHIVE_BYTES + 128 * 1024,
            "Artifact size is outside the package limit")
    release = draft_release(tag)
    return {"artifact": artifact, "release_id": release["id"], "changed_documents": sorted(changed_files)}


def validate_package(archive: Path, destination: Path, version: str) -> dict[str, str]:
    updater._extract_archive(archive, destination)
    install = destination / helper.APP_DIRECTORY
    require(helper.read_marker(install)["version"] == version, "Package version differs from the release tag")
    helper.validate_install_contents(install)
    return helper.manifest(install)


def promote(config: dict, evidence: dict) -> None:
    output = ROOT / "release-promotion"
    require(not output.exists(), "Promotion output already exists")
    output.mkdir()
    with tempfile.TemporaryDirectory(prefix="organizer-promote-") as temporary:
        scratch = Path(temporary)
        wrapper = scratch / "artifact.zip"
        with wrapper.open("xb") as stream:
            command("gh", "api", f"repos/{REPOSITORY}/actions/artifacts/{config['artifact_id']}/zip", output=stream)
        artifact = evidence["artifact"]
        require(wrapper.stat().st_size == artifact["size_in_bytes"], "Artifact download size changed")
        require("sha256:" + helper.file_hash(wrapper) == artifact["digest"], "Artifact provenance digest mismatch")
        with zipfile.ZipFile(wrapper) as source:
            require(len(source.infolist()) == 2 and set(source.namelist()) == {
                updater.WINDOWS_ASSET, updater.CHECKSUM_ASSET,
            }, "Artifact wrapper contains unexpected entries")
            require(source.getinfo(updater.CHECKSUM_ASSET).file_size <= updater.MAX_CHECKSUM_BYTES,
                    "Checksum file is oversized")
            require(source.getinfo(updater.WINDOWS_ASSET).file_size <= updater.MAX_ARCHIVE_BYTES,
                    "Candidate package is oversized")
            expected = updater._expected_checksum(source.read(updater.CHECKSUM_ASSET))
            original = scratch / updater.WINDOWS_ASSET
            with source.open(updater.WINDOWS_ASSET) as reader, original.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
        require(helper.file_hash(original) == expected, "Original candidate checksum mismatch")
        version = config["tag"][1:]
        original_files = validate_package(original, scratch / "original", version)
        replacements = {
            f"{helper.APP_DIRECTORY}/{name}": command("git", "show", f"{config['release_sha']}:{name}")
            for name in ("README.md", "CHANGELOG.md")
        }
        final = output / updater.WINDOWS_ASSET
        with zipfile.ZipFile(original) as source, zipfile.ZipFile(final, "x") as target:
            require(replacements.keys() <= set(source.namelist()), "Candidate lacks bundled stable documentation")
            for item in source.infolist():
                info = copy.copy(item)
                if item.filename in replacements:
                    target.writestr(info, replacements[item.filename])
                elif item.is_dir():
                    target.writestr(info, b"")
                else:
                    with source.open(item) as reader, target.open(info, "w", force_zip64=True) as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
        final_files = validate_package(final, scratch / "final", version)
        document_names = {"README.md", "CHANGELOG.md"}
        require({k: v for k, v in original_files.items() if k not in document_names} == {
            k: v for k, v in final_files.items() if k not in document_names
        }, "A non-documentation payload byte changed")
        for name in document_names:
            require(final_files[name] == hashlib.sha256(replacements[f"{helper.APP_DIRECTORY}/{name}"]).hexdigest(),
                    "Stable bundled documentation differs from the pinned tag")
        final_digest = helper.file_hash(final)
        (output / updater.CHECKSUM_ASSET).write_text(f"{final_digest}  {updater.WINDOWS_ASSET}\n", encoding="utf-8")
        proof = {**config, "artifact_digest": artifact["digest"], "original_zip_sha256": expected,
                 "release_zip_sha256": final_digest, "unchanged_payload_files": len(final_files) - 2,
                 "changed_documents": evidence["changed_documents"], "reused_passed_checks": sorted(REQUIRED_STEPS)}
        (output / "VERIFICATION.json").write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
        fresh = provenance(config)
        require(fresh["artifact"]["digest"] == artifact["digest"] and fresh["release_id"] == evidence["release_id"],
                "Candidate provenance or target draft changed")
        command("gh", "release", "upload", config["tag"], str(final), str(output / updater.CHECKSUM_ASSET),
                "--repo", REPOSITORY)
        print(json.dumps(proof, ensure_ascii=False))
        print("Verified assets uploaded to draft only; release publication remains a separate action.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-provenance-only", action="store_true")
    args = parser.parse_args()
    config = {key: os.environ.get("PROMOTION_" + key.upper(), "") for key in (
        "tag", "release_sha", "candidate_run_id", "candidate_sha", "artifact_id",
    )}
    evidence = provenance(config)
    if args.check_provenance_only:
        print(json.dumps({**config, "artifact_digest": evidence["artifact"]["digest"],
                          "changed_documents": evidence["changed_documents"], "draft_release_id": evidence["release_id"]}))
    else:
        promote(config, evidence)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        message = re.sub(r"https?://[^\s]+", "[URL]", str(error))
        print(f"Verified promotion stopped: {message[:800]}", file=sys.stderr)
        raise SystemExit(1)
