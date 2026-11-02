"""Reuse the completed 45 workspace cases without executing tests or captures.

This records immutable GitHub execution evidence and proves the application is
unchanged apart from the six 0.9.1 version markers. No application is launched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.9.1"
REPOSITORY = "P-JSL/Folder_Organizer"
REVIEW_WORKFLOW = ".github/workflows/workspace-actions-review.yml"
BASE_SOURCE = "899171286e9d12291568d742afe4866ca7555d0f"
RETRY_SOURCE = "089c81f5655794f6fd93e86e73fa143ce981063e"
BASE_RUN = "41525952508"
RETRY_RUN = "41528577873"
SCHEMA = "folder-organizer.workspace-check-reuse.v091"
VERSION_PATTERNS = {
    "folder_organizer/__init__.py": (b'__version__ = "0.9.0"', b'__version__ = "0.9.1"'),
    "pyproject.toml": (b'\nversion = "0.9.0"\n', b'\nversion = "0.9.1"\n'),
    "version.json": (b'"version":"0.9.0"', b'"version":"0.9.1"'),
    "rust-app/version.json": (b'"version":"0.9.0"', b'"version":"0.9.1"'),
    "rust-app/Cargo.toml": (b'\nversion = "0.9.0"\n', b'\nversion = "0.9.1"\n'),
    "rust-app/Cargo.lock": (
        b'name = "folder-organizer-rust"\nversion = "0.9.0"\n',
        b'name = "folder-organizer-rust"\nversion = "0.9.1"\n'),
}
ALLOWED_PATHS = {
    "README.md", "CHANGELOG.md", REVIEW_WORKFLOW,
    ".github/workflows/workspace-actions-release.yml",
    "tests/test_workspace_flow_next.py",
    "scripts/publish_workspace_actions_release.py",
    "scripts/reuse_workspace_actions_checks.py",
}
FAILED_FLOW_CASES = {
    "tests/test_workspace_flow_next.py::test_next_workspace_real_worker_analysis_to_keep_first_and_per_root_restore[single-folder]",
    "tests/test_workspace_flow_next.py::test_next_workspace_real_worker_analysis_to_keep_first_and_per_root_restore[multiple-folders]",
}


def command(*arguments: str) -> str:
    return subprocess.check_output(arguments, cwd=ROOT).decode("utf-8")


def api(path: str):
    return json.loads(command("gh", "api", path))


def git_bytes(source: str, path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{source}:{path}"], cwd=ROOT)


def verify_reused_source(source: str) -> list[str]:
    """Allow exactly version metadata, release plumbing and documented changes."""
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise ValueError("A complete immutable build source is required")
    subprocess.run(["git", "merge-base", "--is-ancestor", RETRY_SOURCE, source], cwd=ROOT, check=True)
    changes = [path for path in command("git", "diff", "--name-only", "--no-renames", "-z",
                                        BASE_SOURCE, source).split("\0") if path]
    forbidden = [path for path in changes if path not in ALLOWED_PATHS
                 and path not in VERSION_PATTERNS and not path.startswith("docs/")]
    if forbidden:
        raise ValueError("Application or build inputs changed after the passed cases: " + ", ".join(forbidden))
    for path, (before, after) in VERSION_PATTERNS.items():
        original = git_bytes(BASE_SOURCE, path)
        current = git_bytes(source, path)
        if original.count(before) != 1 or current != original.replace(before, after, 1):
            raise ValueError("A version file changed beyond its one 0.9.1 marker: " + path)
    # These are the exact flow wait fix and dispatch definition whose two
    # formerly failed cases passed. Neither is part of a packaged application.
    for path in ("tests/test_workspace_flow_next.py", REVIEW_WORKFLOW):
        if git_bytes(source, path) != git_bytes(RETRY_SOURCE, path):
            raise ValueError("Passed workspace retry inputs changed: " + path)
    return sorted(changes)


def execution(run_id: str, source: str, conclusion: str) -> dict:
    value = api(f"repos/{REPOSITORY}/actions/runs/{run_id}")
    if (value.get("repository", {}).get("full_name") != REPOSITORY
            or value.get("head_sha") != source or value.get("path") != REVIEW_WORKFLOW
            or value.get("event") != "workflow_dispatch" or value.get("status") != "completed"
            or value.get("conclusion") != conclusion):
        raise ValueError("Previously completed workspace execution identity differs: " + run_id)
    return value


def jobs_for(run_id: str) -> list[dict]:
    value = api(f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    jobs = value.get("jobs", [])
    if value.get("total_count") != len(jobs):
        raise ValueError("Previously completed workspace job list is incomplete")
    return jobs


def one_job(jobs: list[dict], name: str, conclusion: str, expected_id: int | None = None) -> dict:
    found = [job for job in jobs if job.get("name") == name]
    if (len(found) != 1 or found[0].get("status") != "completed"
            or found[0].get("conclusion") != conclusion
            or expected_id is not None and found[0].get("id") != expected_id):
        raise ValueError("Previously completed workspace job differs: " + name)
    return found[0]


def job_log(job: dict) -> str:
    raw = command("gh", "api", "--allow-escape-sequences",
                  f"repos/{REPOSITORY}/actions/jobs/{job['id']}/logs")
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)


def record_prior_evidence(output: Path, source: str) -> dict:
    changes = verify_reused_source(source)
    execution(BASE_RUN, BASE_SOURCE, "failure")
    execution(RETRY_RUN, RETRY_SOURCE, "success")
    base_jobs, retry_jobs = jobs_for(BASE_RUN), jobs_for(RETRY_RUN)
    python = one_job(base_jobs, "New Python action workspace on Windows", "failure", 125553606525)
    rust = one_job(base_jobs, "New Rust action workspace on Windows", "success", 125553606951)
    retry = one_job(retry_jobs, "New Python action workspace on Windows", "success")
    one_job(retry_jobs, "New Rust action workspace on Windows", "skipped")
    python_log, rust_log, retry_log = job_log(python), job_log(rust), job_log(retry)
    if not re.search(r"\b2 failed, 26 passed in [\d.]+s\b", python_log):
        raise ValueError("Original Python output does not prove exactly 26 passed and two failed")
    failures = set(re.findall(r"\bFAILED (tests/\S+)\s+-", python_log))
    if failures != FAILED_FLOW_CASES:
        raise ValueError("The originally failed Python nodes differ from the two corrected flows")
    if not re.search(r"\b2 passed in [\d.]+s\b", retry_log):
        raise ValueError("Corrected Windows worker output does not prove both flows passed")
    statuses = re.findall(r"\btest ([A-Za-z_][A-Za-z_0-9:]*) \.\.\. (ok|FAILED)\b", rust_log)
    names = {name for name, status in statuses if status == "ok"}
    if (len(statuses) != 17 or len(names) != 17 or any(status != "ok" for _, status in statuses)
            or sum(name.startswith("workspace_") for name in names) != 12
            or sum(name.startswith("gui::next_workspace_ui_tests::") for name in names) != 5
            or "test result: ok. 12 passed; 0 failed" not in rust_log
            or "test result: ok. 5 passed; 0 failed" not in rust_log
            or not re.search(r"Finished `dev` profile", rust_log)):
        raise ValueError("Original Rust output does not prove 12 action cases, five UI cases and compilation")
    output.mkdir(parents=True, exist_ok=False)
    logs = {}
    for name, body, job, run_id in (
            ("PYTHON_26_PASSED.log", python_log, python, BASE_RUN),
            ("RUST_17_PASSED.log", rust_log, rust, BASE_RUN),
            ("PYTHON_2_FIXED_PASSED.log", retry_log, retry, RETRY_RUN)):
        raw = body.encode("utf-8")
        (output / name).write_bytes(raw)
        logs[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                      "job_id": job["id"], "run_id": run_id, "conclusion": job["conclusion"]}
    receipt = {
        "schema": SCHEMA, "version": VERSION, "status": "passed", "source_commit": source,
        "workflow_run": os.environ.get("GITHUB_RUN_ID"),
        "base_source": BASE_SOURCE, "retry_source": RETRY_SOURCE,
        "base_review_run": BASE_RUN, "corrected_flow_run": RETRY_RUN,
        "application_code_unchanged": True, "version_markers_only": list(VERSION_PATTERNS),
        "allowed_source_changes": changes,
        "total_reused_cases": 45, "python_reused_cases": 28, "rust_reused_cases": 17,
        "source_commands": {
            "python_original": "python -m pytest -q tests/test_workspace_actions_next.py tests/test_workspace_actions_ui_next.py tests/test_workspace_flow_next.py --junitxml=workspace-python-new.xml",
            "rust_actions": "cargo +1.99.1 test --manifest-path rust-app/Cargo.toml --locked --test workspace_actions_next",
            "rust_ui": "cargo +1.99.1 test --manifest-path rust-app/Cargo.toml --locked --features gui --lib next_workspace",
            "rust_compile": "cargo +1.99.1 check --manifest-path rust-app/Cargo.toml --locked --features gui --bins",
            "python_corrected": "python -m pytest -q tests/test_workspace_flow_next.py --junitxml=workspace-python-new.xml",
        },
        "reused_rust_case_names": sorted(names), "corrected_python_nodes": sorted(FAILED_FLOW_CASES),
        "retry_reason": "Qt qWait retained the GIL and starved the Python worker; the wait now yields via time.sleep, with the 15-second deadline and data guards unchanged",
        "logs": logs, "tests_rerun": False, "captures": False,
        "scope": "Read-only retrieval of already completed native Windows evidence; formal binaries are built separately",
    }
    (output / "REUSED_CHECKS_V091.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reuse-results")
    arguments = parser.parse_args()
    source = command("git", "rev-parse", "HEAD").strip()
    if os.environ.get("GITHUB_SHA", source) != source:
        raise ValueError("Checked-out source differs from the current workflow source")
    receipt = record_prior_evidence(arguments.output, source)
    print(json.dumps({"schema": receipt["schema"], "version": VERSION, "source_commit": source,
                      "total_reused_cases": 45, "tests_rerun": False, "captures": False}))


if __name__ == "__main__":
    main()
