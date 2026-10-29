"""Reuse one completed build and its checks after a console-only receipt error.

This deliberately recognizes a single prior run, not arbitrary failed checks.
It never executes the applications or repeats their successful assertions.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "P-JSL/Folder_Organizer"
WORKFLOW = ".github/workflows/review-release.yml"
BUILD_COMMIT = "664e574a772fda1a41b28e4bad577773c4862d1f"
PRIOR_RUN = 40943399617
PACKAGE_SCRIPT = "scripts/windows_python_bundle_v070.py"
ALLOWED_CHANGES = frozenset((
    WORKFLOW, PACKAGE_SCRIPT, "scripts/reuse_review_release.py",
    "scripts/publish_review_release.py",
))
OLD_PRINT = "        print(json.dumps(proof, ensure_ascii=False))"
NEW_PRINT = "        print(json.dumps(proof, ensure_ascii=True))"


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT).decode("utf-8")


def check_source_changes(build_commit: str, current_commit: str) -> list[str]:
    """Verify that executable/build inputs remain exactly at the reused source.

    The publisher can call this without repeating any program checks or API
    queries. Only workflow/reuse/publication bookkeeping and one console-print
    encoding correction may differ from the actual compiled commit.
    """
    if build_commit != BUILD_COMMIT or not re.fullmatch(r"[0-9a-f]{40}", current_commit):
        raise ValueError("Only the known console-error build can be reused")
    subprocess.run(["git", "merge-base", "--is-ancestor", build_commit, current_commit],
                   cwd=ROOT, check=True)
    changes = [name for name in command("git", "diff", "--name-only", "--no-renames",
                                        "-z", build_commit, current_commit).split("\0") if name]
    if not set(changes) <= ALLOWED_CHANGES:
        raise ValueError("Reused build inputs changed: " + ", ".join(sorted(set(changes) - ALLOWED_CHANGES)))
    old_source = command("git", "show", f"{build_commit}:{PACKAGE_SCRIPT}")
    new_source = command("git", "show", f"{current_commit}:{PACKAGE_SCRIPT}")
    proof_write = '        (ROOT / "dist/PYTHON_PACKAGE_CHECK.json").write_text(\n'
    final_tail = OLD_PRINT + '\n\n\nif __name__ == "__main__":\n    main()\n'
    if (old_source.count(OLD_PRINT) != 1 or old_source.count(proof_write) != 1
            or old_source.index(proof_write) >= old_source.index(OLD_PRINT)
            or not old_source.endswith(final_tail)):
        raise ValueError("The prior failure was not after the final saved package proof")
    if new_source != old_source.replace(OLD_PRINT, NEW_PRINT, 1):
        raise ValueError("The package check changed beyond its final console encoding correction")
    return sorted(changes)


def api(path: str) -> dict:
    return json.loads(command("gh", "api", path))


def unique_job(jobs: list[dict], name: str) -> dict:
    found = [job for job in jobs if job.get("name") == name]
    if len(found) != 1:
        raise ValueError("Expected exactly one prior job: " + name)
    return found[0]


def narrow_traceback(log: str) -> str:
    # GitHub prefixes each line with an ISO timestamp. Discard only that prefix,
    # leaving the actual Python traceback as a small, inspectable source record.
    lines = [re.sub(r"^\d{4}-\d{2}-\d{2}T\S+\s", "", line) for line in log.splitlines()]
    starts = [index for index, line in enumerate(lines) if line == "Traceback (most recent call last):"]
    if len(starts) != 1:
        raise ValueError("Expected one final Python traceback in the prior failed job")
    start = starts[0]
    ends = [index for index in range(start, len(lines)) if lines[index].startswith("UnicodeEncodeError:")]
    if len(ends) != 1:
        raise ValueError("The prior failure was not solely a UnicodeEncodeError")
    trace = "\n".join(lines[start:ends[0] + 1]) + "\n"
    if ("windows_python_bundle_v070.py\"" not in trace
            or "print(json.dumps(proof, ensure_ascii=False))" not in trace
            or "cp1252.py" not in trace
            or "'charmap' codec can't encode" not in trace):
        raise ValueError("The prior failure does not match the known final console encoding error")
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    if args.run_id != PRIOR_RUN:
        raise ValueError("Reuse is limited to the known partially reported run")
    manifest = json.loads((ROOT / "release-build/BUILD_INFO.json").read_text(encoding="utf-8"))
    if (manifest.get("source_commit") != BUILD_COMMIT or manifest.get("version") != "0.7.0"
            or str(manifest.get("workflow_run")) != str(args.run_id)):
        raise ValueError("Downloaded build receipt differs from the known successful build")
    current_commit = command("git", "rev-parse", "HEAD").strip()
    changes = check_source_changes(manifest["source_commit"], current_commit)
    run = api(f"repos/{REPOSITORY}/actions/runs/{args.run_id}")
    if (run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("head_sha") != BUILD_COMMIT or run.get("path") != WORKFLOW
            or run.get("status") != "completed" or run.get("conclusion") != "failure"
            or run.get("event") != "workflow_dispatch"):
        raise ValueError("Prior run identity or completion state does not match the reused receipt")
    response = api(f"repos/{REPOSITORY}/actions/runs/{args.run_id}/jobs?filter=latest&per_page=100")
    jobs = response["jobs"]
    if response.get("total_count") != len(jobs):
        raise ValueError("Prior job list is incomplete")
    for name in ("Build both editions", "New Windows paths"):
        job = unique_job(jobs, name)
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            raise ValueError("The reused build/native gate did not pass: " + name)
    review = unique_job(jobs, "New artifact checks")
    if review.get("status") != "completed" or review.get("conclusion") != "failure":
        raise ValueError("Unexpected prior artifact-check job state")
    steps = review["steps"]
    failed = [step for step in steps if step.get("conclusion") == "failure"]
    if len(failed) != 1 or failed[0].get("name") != "Check changed Python packaging and visible Widgets":
        raise ValueError("A prior artifact assertion failed beyond the known console print")
    for name in ("Check changed Rust GUI startup",
                 "Compare changed exact analysis and common report with five alternating runs"):
        selected = [step for step in steps if step.get("name") == name]
        if len(selected) != 1 or selected[0].get("conclusion") != "skipped":
            raise ValueError("An unrun new gate was unexpectedly executed in the prior run: " + name)
    log = command("gh", "api", f"repos/{REPOSITORY}/actions/jobs/{review['id']}/logs")
    trace = narrow_traceback(log)
    output = ROOT / "reuse-results"
    output.mkdir(exist_ok=False)
    receipt = {
        "edition": "Python", "version": "0.7.0", "checks_reused": True,
        "source_run": args.run_id, "source_job": review["id"], "build_commit": BUILD_COMMIT,
        "review_commit": current_commit, "rerun": False,
        "basis": "All package assertions, native GUI capture/exit and proof write completed before the final console print raised UnicodeEncodeError.",
        "original_proof_retained": False, "original_screenshot_retained": False,
        "limitations": "The prior job did not upload its generated JSON or PNG; exact dimensions, sampled colors and the original image are unavailable.",
        "unchanged_build_native_gates": "reused successful jobs from the same original run",
        "allowed_source_changes": changes,
    }
    (output / "PYTHON_PACKAGE_CHECK.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (output / "PRIOR_PACKAGE_LOG.txt").write_text(trace, encoding="utf-8")
    print(json.dumps({"reused_run": args.run_id, "build_commit": BUILD_COMMIT,
                      "review_commit": current_commit, "new_application_checks_rerun": False}))


if __name__ == "__main__":
    main()
