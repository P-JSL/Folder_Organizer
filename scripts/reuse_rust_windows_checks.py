"""Reuse actual successful Windows steps only when Rust source is unchanged."""
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
print(json.dumps({"reused_run": RUN, "rust_commit": COMMIT,
                  "passed_steps": sorted(required), "status": "unchanged Rust; passed cases skipped"}))
