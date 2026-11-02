"""Reuse one exact formal build and two passed native cases after fixture fixes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "P-JSL/Folder_Organizer"
WORKFLOW = ".github/workflows/extended-release.yml"
BUILD_RUN = "41365825550"
BUILD_SOURCE = "ccdbe6a2c6c59817fd807c1d3d6107992db10ff4"
FAILED_CASE = "native_exclusive_handle_partial_then_hash_cancel_preserve_both_real_sources"
PASSED_CASES = (
    "native_long_unicode_overlaps_keep_membership_and_128_bit_hardlink_identity",
    "native_cross_root_sampling_uses_change_time_when_mtime_and_file_id_stay_same",
)
NATIVE_PATH = "rust-app/tests/windows_multiroot_v080.rs"
ALLOWED_PATHS = {
    WORKFLOW, "scripts/publish_extended_release.py", "scripts/reuse_extended_release.py",
    "scripts/release_v080_fixtures.py", "scripts/windows_release_v080.py", NATIVE_PATH,
    "docs/RELEASE_NOTES_0.8.0.md", "docs/RELEASE_NOTES.md",
}
OLD_CALLBACK = '        if event.phase == "duplicates_hash" && event.completed == 0 {'
NEW_CALLBACK = ('        // The shared kernel reports each completed hash as "duplicates";\n'
                '        // stop before the second file can finish and publish an exact group.\n'
                '        if event.root_id.is_empty() && event.phase == "duplicates" && event.completed == 1 {')


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT).decode("utf-8")


def api(path: str):
    return json.loads(command("gh", "api", path))


def verify_reused_source(compiled: str, validation: str) -> list[str]:
    if compiled != BUILD_SOURCE or not re.fullmatch(r"[0-9a-f]{40}", validation):
        raise ValueError("Reuse is limited to the exact successful 0.8.0 build")
    subprocess.run(["git", "merge-base", "--is-ancestor", compiled, validation], cwd=ROOT, check=True)
    paths = [value for value in command("git", "diff", "--name-only", "--no-renames", "-z",
                                       compiled, validation).split("\0") if value]
    if not set(paths) <= ALLOWED_PATHS:
        raise ValueError("Compiled application/build/signing inputs changed: " +
                         ", ".join(sorted(set(paths) - ALLOWED_PATHS)))
    before = command("git", "show", f"{compiled}:{NATIVE_PATH}")
    after = command("git", "show", f"{validation}:{NATIVE_PATH}")
    if before.count(OLD_CALLBACK) != 1 or after != before.replace(OLD_CALLBACK, NEW_CALLBACK, 1):
        raise ValueError("The native target changed beyond the failed case's callback correction")
    return sorted(paths)


def jobs_for(run_id: str) -> list[dict]:
    response = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    if response.get("total_count") != len(response["jobs"]):
        raise ValueError("Prior job inventory is incomplete")
    return response["jobs"]


def one_job(jobs: list[dict], name: str, conclusion: str) -> dict:
    found = [job for job in jobs if job.get("name") == name]
    if len(found) != 1 or found[0].get("status") != "completed" or found[0].get("conclusion") != conclusion:
        raise ValueError("Prior gate status differs: " + name)
    return found[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.run_id != BUILD_RUN:
        raise ValueError("Only the known successful build may be reused")
    manifest = json.loads((ROOT / "release-build/BUILD_INFO.json").read_text(encoding="utf-8"))
    if (manifest.get("source_commit") != BUILD_SOURCE or manifest.get("version") != "0.8.0"
            or str(manifest.get("workflow_run")) != BUILD_RUN):
        raise ValueError("Downloaded build receipt differs from the known formal build")
    validation = command("git", "rev-parse", "HEAD").strip()
    changes = verify_reused_source(BUILD_SOURCE, validation)
    execution = api(f"repos/{REPOSITORY}/actions/runs/{BUILD_RUN}")
    if (execution.get("repository", {}).get("full_name") != REPOSITORY
            or execution.get("head_sha") != BUILD_SOURCE or execution.get("path") != WORKFLOW
            or execution.get("event") != "workflow_dispatch" or execution.get("status") != "completed"
            or execution.get("conclusion") != "failure"):
        raise ValueError("Prior execution identity or completion state differs")
    jobs = jobs_for(BUILD_RUN)
    build = one_job(jobs, "Formal Windows builds", "success")
    native = one_job(jobs, "New Windows contracts", "failure")
    failed = [step for step in native.get("steps", []) if step.get("conclusion") == "failure"]
    if len(failed) != 1 or failed[0].get("name") != "Run only new native multi-folder cases":
        raise ValueError("Native failure was outside the known target invocation")
    log = command("gh", "api", "--allow-escape-sequences",
                  f"repos/{REPOSITORY}/actions/jobs/{native['id']}/logs")
    log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log)
    statuses = dict(re.findall(r"\btest ([A-Za-z_][A-Za-z_0-9]*) \.\.\. (ok|FAILED)\b", log))
    expected = {name: "ok" for name in PASSED_CASES} | {FAILED_CASE: "FAILED"}
    if statuses != expected or "2 passed; 1 failed" not in log:
        raise ValueError("The prior native output does not prove exactly the two unchanged passed cases")
    relevant = [line for line in log.splitlines() if any(name in line for name in (*PASSED_CASES, FAILED_CASE))
                or "test result:" in line or "assertion failed:" in line]
    output = ROOT / "reuse-results"
    output.mkdir(exist_ok=False)
    receipt = {"schema": "folder-organizer.build-reuse.v080", "version": "0.8.0", "status": "passed",
               "build_run": BUILD_RUN, "compiled_source": BUILD_SOURCE, "validation_source": validation,
               "original_build_job": build["id"], "original_native_job": native["id"],
               "reused_native_cases": list(PASSED_CASES), "required_native_case": FAILED_CASE,
               "allowed_source_changes": changes, "formal_build_repeated": False,
               "previously_passed_native_cases_repeated": False}
    (output / "BUILD_REUSE_V080.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (output / "PRIOR_NATIVE_V080.log").write_text("\n".join(relevant) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
