"""Reuse actual successful Windows steps only when their source is unchanged."""
import json
import subprocess

REPOSITORY = "P-JSL/Folder_Organizer"
RUN = 40807970764
COMMIT = "bcc8027e866746adcfcbdbff0dd176fe3488942b"

def command(*args):
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()

run = json.loads(command("gh", "api", f"repos/{REPOSITORY}/actions/runs/{RUN}"))
assert run["head_sha"] == COMMIT and run["head_repository"]["full_name"] == REPOSITORY
assert run["path"] == ".github/workflows/dual-release.yml" and run["status"] == "completed"
jobs = json.loads(command("gh", "api", f"repos/{REPOSITORY}/actions/runs/{RUN}/jobs"))
required = {"Build new native Rust programs", "Package native Rust edition and dependency notices",
            "Check only new Windows-specific Rust paths"}
assert any(required <= {step["name"] for step in job["steps"]
                       if step["conclusion"] == "success"} for job in jobs["jobs"])
changed = subprocess.check_output(["git", "diff", "--name-only", "-z", COMMIT, "HEAD", "--",
                                   "rust-app", ".cargo", "scripts/package_rust.py"])
assert not changed, "Rust code/build contract changed: prior Windows checks cannot be reused"
python_run_id = 40809284213
python_commit = "4a584b847feaca6d36629c7cba22f250c8e236e8"
python_run = json.loads(command("gh", "api", f"repos/{REPOSITORY}/actions/runs/{python_run_id}"))
assert python_run["head_sha"] == python_commit and python_run["head_repository"]["full_name"] == REPOSITORY
assert python_run["path"] == ".github/workflows/dual-release.yml" and python_run["status"] == "completed"
python_jobs = json.loads(command("gh", "api", f"repos/{REPOSITORY}/actions/runs/{python_run_id}/jobs"))
python_steps = {"Build changed Python edition (previous suites skipped)",
                "Check only the remaining Windows recovery case"}
assert any(python_steps <= {step["name"] for step in job["steps"]
                          if step["conclusion"] == "success"} for job in python_jobs["jobs"])
python_changed = subprocess.check_output(["git", "diff", "--name-only", "-z", python_commit, "HEAD", "--",
                                         "folder_organizer", "pyproject.toml", "version.json",
                                         "desktop_entry.py", "updater_entry.py"])
assert not python_changed, "Python code/build contract changed: prior recovery check cannot be reused"
print(json.dumps({"reused_run": RUN, "rust_commit": COMMIT,
                  "python_run": python_run_id, "python_commit": python_commit,
                  "passed_steps": sorted(required | python_steps),
                  "status": "unchanged application code; passed cases skipped"}))
