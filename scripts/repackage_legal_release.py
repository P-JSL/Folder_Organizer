"""Add application terms to published 0.6.1 ZIPs without rebuilding or running apps.

Reads the pinned published archives from legal-source/ and writes new archives,
checksums and a content-preservation receipt to legal-dist/. Never writes to a
GitHub release. Every non-document payload byte must remain unchanged.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

from package_legal import LEGAL_FILES, bundled_document

ROOT = Path(__file__).resolve().parents[1]
BASELINES = {
    "FolderOrganizer-Windows-x64.zip": (
        "FolderOrganizer", "94939d3bcfc874565b642cac6410f00f73ffd5cd0f7cb2a099ed18687ea14d30"
    ),
    "FolderOrganizer-Rust-Windows-x64.zip": (
        "FolderOrganizerRust", "aae0341023bef08fac7d3235ae039f98f72817774ed5ef212434284128b101c3"
    ),
}
BUFFER = 1024 * 1024


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def checksums(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        checksum, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        require(bool(re.fullmatch(r"[0-9a-fA-F]{64}", checksum)), "Invalid source checksum")
        require(name not in result, "Duplicate source checksum entry")
        result[name] = checksum.lower()
    return result


def entries(archive: zipfile.ZipFile, directory: str) -> list[zipfile.ZipInfo]:
    items = archive.infolist()
    require(0 < len(items) <= 15_000, "Unexpected package entry count")
    require(sum(item.file_size for item in items) <= 2 * 1024**3, "Package is too large")
    seen = set()
    for item in items:
        raw = item.orig_filename.rstrip("/")
        path = PurePosixPath(raw)
        require(not path.is_absolute() and bool(path.parts) and path.parts[0] == directory
                and ".." not in path.parts and not any(c in raw for c in ("\\", ":", "\0")),
                "Invalid package entry path")
        require(raw.casefold() not in seen, "Duplicate package entry path")
        seen.add(raw.casefold())
        require(item.file_size <= 512 * 1024**2, "Package entry is too large")
        require(stat.S_IFMT(item.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR)
                and not item.external_attr & 0x400 and not item.flag_bits & 1,
                "Unsupported package entry type")
    return items


def content_digest(values: dict[str, str]) -> str:
    payload = "".join(f"{name}\0{checksum}\n" for name, checksum in sorted(values.items()))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def repackage(source: Path, target: Path, directory: str) -> dict:
    documents = {
        relative: bundled_document(relative, (ROOT / filename).read_bytes())
        for relative, filename in LEGAL_FILES.items()
    }
    documents.update({
        name: bundled_document(name, (ROOT / name).read_bytes())
        for name in ("README.md", "CHANGELOG.md")
    })
    if directory == "FolderOrganizer":
        documents["THIRD_PARTY.md"] = bundled_document(
            "THIRD_PARTY.md", (ROOT / "docs/THIRD_PARTY.md").read_bytes()
        )
    # Rust's generated third-party dependency matrix stays exactly as shipped.
    overlay = {f"{directory}/{name}": data for name, data in documents.items()}
    old_payload = {}
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(
        target, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as output:
        items = entries(original, directory)
        require({f"{directory}/README.md", f"{directory}/CHANGELOG.md"}
                <= {item.filename for item in items}, "Source lacks packaged documentation")
        output.comment = original.comment
        added = set(overlay) - {item.filename for item in items}
        for item in items:
            info = copy.copy(item)
            if item.filename in overlay:
                output.writestr(info, overlay[item.filename])
            elif item.is_dir():
                output.writestr(info, b"")
            else:
                checksum = hashlib.sha256()
                with original.open(item) as reader, output.open(info, "w", force_zip64=True) as writer:
                    while chunk := reader.read(BUFFER):
                        writer.write(chunk)
                        checksum.update(chunk)
                old_payload[item.filename] = checksum.hexdigest()
        for name in sorted(added):
            output.writestr(name, overlay[name], compress_type=zipfile.ZIP_DEFLATED)

    new_payload = {}
    with zipfile.ZipFile(target) as output:
        for item in entries(output, directory):
            if item.is_dir():
                continue
            with output.open(item) as reader:
                checksum = hashlib.file_digest(reader, "sha256").hexdigest()
            if item.filename in overlay:
                require(checksum == hashlib.sha256(overlay[item.filename]).hexdigest(),
                        "Bundled legal document differs from the current source")
            else:
                new_payload[item.filename] = checksum
    require(old_payload == new_payload, "A non-documentation payload byte changed")
    binaries = {name: checksum for name, checksum in old_payload.items()
                if name.lower().endswith(".exe")}
    require(bool(binaries), "Source package has no executables")
    return {
        "archive": target.name,
        "before_sha256": digest(source),
        "after_sha256": digest(target),
        "repackaged_bytes": target.stat().st_size,
        "non_document_payload_unchanged": True,
        "unchanged_payload_files": len(old_payload),
        "unchanged_payload_sha256": content_digest(old_payload),
        "unchanged_executable_count": len(binaries),
        "unchanged_executable_sha256": binaries,
        "document_sha256": {name: hashlib.sha256(data).hexdigest()
                            for name, data in sorted(documents.items())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "legal-source")
    parser.add_argument("--destination", type=Path, default=ROOT / "legal-dist")
    arguments = parser.parse_args()
    provided = checksums(arguments.source / "SHA256SUMS.txt")
    require(set(provided) == set(BASELINES), "Source checksum list must contain both editions")
    for name, (_, expected) in BASELINES.items():
        require(provided[name] == expected and digest(arguments.source / name) == expected,
                f"Source does not match the published 0.6.1 package: {name}")
    arguments.destination.mkdir(parents=True, exist_ok=False)
    packages = {
        name: repackage(arguments.source / name, arguments.destination / name, directory)
        for name, (directory, _) in BASELINES.items()
    }
    (arguments.destination / "SHA256SUMS.txt").write_text(
        "".join(f"{package['after_sha256']}  {name}\n" for name, package in packages.items()),
        encoding="utf-8",
    )
    commit = os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    receipt = {"version": "0.6.1", "kind": "legal-documents-only", "source_commit": commit,
               "builds": "not run", "application_tests": "not run", "packages": packages}
    (arguments.destination / "LEGAL_REPACKAGE.json").write_text(
        json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps({"kind": receipt["kind"], "version": receipt["version"],
                      "archives": [{key: package[key] for key in (
                          "archive", "after_sha256", "unchanged_payload_files"
                      )} for package in packages.values()]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
