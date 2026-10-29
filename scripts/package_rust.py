"""Package the three already-built native Rust programs and license notices."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tomllib

ROOT = Path(__file__).resolve().parents[1]
RUST = ROOT / "rust-app"
PROGRAMS = ("FolderOrganizerRust.exe", "FolderOrganizerRustCLI.exe", "FolderOrganizerRustUpdater.exe")


def package(binaries: Path, destination: Path) -> None:
    marker = json.loads((RUST / "version.json").read_text(encoding="utf-8"))
    version = tomllib.loads((RUST / "Cargo.toml").read_text(encoding="utf-8"))["package"]["version"]
    if marker != {"app_id": "P-JSL/Folder_Organizer.Rust", "version": version}:
        raise ValueError("Rust version marker does not match Cargo.toml")
    for name in PROGRAMS:
        source = binaries / name
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing native program: {name}")
        with source.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise ValueError(f"Not a Windows executable: {name}")
    metadata = json.loads(subprocess.check_output([
        "cargo", "metadata", "--manifest-path", str(RUST / "Cargo.toml"),
        "--locked", "--format-version", "1", "--features", "gui",
        "--filter-platform", "x86_64-pc-windows-msvc",
    ], cwd=ROOT, text=True, encoding="utf-8"))
    nodes = {node["id"]: node for node in metadata["resolve"]["nodes"]}
    pending = [metadata["resolve"]["root"]]
    included = set()
    while pending:
        identifier = pending.pop()
        if identifier in included:
            continue
        included.add(identifier)
        pending.extend(dep["pkg"] for dep in nodes[identifier]["deps"])
    dependencies = sorted((p for p in metadata["packages"] if p["id"] in included
                           and p["name"] != "folder-organizer-rust"), key=lambda p: (p["name"], p["version"]))
    destination.mkdir(parents=True, exist_ok=False)
    for name in PROGRAMS:
        shutil.copyfile(binaries / name, destination / name)
    for name in ("README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copyfile(RUST / "version.json", destination / "version.json")
    notices = destination / "licenses"
    notices.mkdir()
    lines = ["# Rust edition third-party notices", "",
             "Selected Cargo.lock dependencies for the Windows GUI/CLI/updater build.",
             "License expressions below are declared by the dependencies. Their supplied notices are included in licenses/.",
             "This inventory does not assign a license to Folder Organizer itself.", "",
             "| Package | Version | Declared license | Source |", "| --- | --- | --- | --- |"]
    total = 0
    count = 0
    for item in dependencies:
        manifest = Path(item["manifest_path"])
        source = manifest.parent.resolve()
        identifier = f"{item['name']}-{item['version']}"
        license_text = (item.get("license") or "See included license file").replace("|", "\\|")
        repository = item.get("repository") or f"https://crates.io/crates/{item['name']}/{item['version']}"
        lines.append(f"| {item['name']} | {item['version']} | {license_text} | {repository} |")
        selected = set()
        if item.get("license_file"):
            selected.add(source / item["license_file"])
        # Font and bundled-source notices may live below the crate root.
        for path in source.rglob("*"):
            relative = path.relative_to(source)
            if len(relative.parts) > 5:
                continue
            if any(token in path.name.casefold() for token in ("license", "notice", "copyright")):
                selected.add(path)
        for path in sorted(selected):
            if path.is_symlink():
                raise ValueError(f"Dependency notice is a link: {identifier}")
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(source):
                raise ValueError(f"Dependency notice leaves its crate: {identifier}")
            size = path.stat().st_size
            total += size
            count += 1
            if size > 2 * 1024 * 1024 or total > 64 * 1024 * 1024 or count > 10_000:
                raise ValueError("Dependency notice size/count exceeds the packaging limit")
            target = notices / identifier / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as original, target.open("xb") as output:
                shutil.copyfileobj(original, output)
    (destination / "THIRD_PARTY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"edition": "Rust", "version": version, "dependencies": len(dependencies),
                      "license_files": count, "license_bytes": total}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binaries", type=Path, default=RUST / "target" / "release")
    parser.add_argument("--destination", type=Path, default=ROOT / "dist" / "FolderOrganizerRust")
    arguments = parser.parse_args()
    package(arguments.binaries, arguments.destination)
