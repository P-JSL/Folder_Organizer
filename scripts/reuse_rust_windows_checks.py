"""Reuse actual successful Windows steps only when their source is unchanged."""
import json
import subprocess
import tomllib

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
changed_paths = {path.decode('utf-8') for path in changed.split(b'\0') if path}
assert changed_paths <= {"rust-app/src/gui.rs", "rust-app/src/main.rs",
                         "rust-app/Cargo.toml", "rust-app/Cargo.lock"}, \
    "Rust backend changed: prior Windows data checks cannot be reused"
old_manifest = tomllib.loads(command("git", "show", f"{COMMIT}:rust-app/Cargo.toml"))
new_manifest = tomllib.loads(command("git", "show", "HEAD:rust-app/Cargo.toml"))
old_features = set(old_manifest['dependencies']['eframe']['features'])
new_features = set(new_manifest['dependencies']['eframe']['features'])
assert old_features <= new_features <= old_features | {'wgpu'}
old_manifest['dependencies']['eframe']['features'] = new_manifest['dependencies']['eframe']['features']
windows_dependencies = new_manifest['target']['cfg(windows)']['dependencies']
gpu = windows_dependencies.pop('wgpu', None)
gui_features = new_manifest['features']['gui']
if 'wgpu' in new_features - old_features:
    assert gpu == {'version': '24', 'default-features': False, 'features': ['dx12'], 'optional': True}
    assert gui_features.count('dep:wgpu') == 1
    new_manifest['features']['gui'] = [feature for feature in gui_features if feature != 'dep:wgpu']
else:
    assert gpu is None and 'dep:wgpu' not in gui_features
assert old_manifest == new_manifest, "Only the added GUI renderer feature may reuse backend checks"
old_lock = tomllib.loads(command("git", "show", f"{COMMIT}:rust-app/Cargo.lock"))
new_lock = tomllib.loads(command("git", "show", "HEAD:rust-app/Cargo.lock"))
new_packages = {(p['name'], p['version'], p.get('source')): p.get('checksum') for p in new_lock['package']}
for package in old_lock['package']:
    identity = (package['name'], package['version'], package.get('source'))
    assert identity in new_packages and new_packages[identity] == package.get('checksum'), \
        "Previously verified dependency versions/checksums must stay pinned"
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
cli_run_id = 40809917824
cli_commit = "575b7cb64dd955b33f3ba7eb64363131563228de"
cli_run = json.loads(command("gh", "api", f"repos/{REPOSITORY}/actions/runs/{cli_run_id}"))
assert cli_run['head_sha'] == cli_commit and cli_run['head_repository']['full_name'] == REPOSITORY
assert cli_run['path'] == '.github/workflows/dual-release.yml' and cli_run['status'] == 'completed'
logs = command('gh', 'run', 'view', str(cli_run_id), '--repo', REPOSITORY, '--log-failed')
cli_results = {}
for line in logs.splitlines():
    start = line.find('{"edition":')
    if start >= 0:
        result = json.loads(line[start:])
        cli_results[result['edition']] = result
assert cli_results == {edition: {'edition': edition, 'files': 5, 'exact_groups': 1, 'version': '0.6.0'}
                       for edition in ('Python', 'Rust')}
assert 'Native Rust GUI startup failed' in logs, 'Only GUI startup remained after passed CLI assertions'
print(json.dumps({"reused_run": RUN, "rust_commit": COMMIT,
                  "python_run": python_run_id, "python_commit": python_commit,
                  "packaged_cli_run": cli_run_id,
                  "passed_steps": sorted(required | python_steps),
                  "status": "unchanged verified data backend; new GUI startup checked separately"}))
