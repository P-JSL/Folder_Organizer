"""Bundle application terms separately from third-party license notices."""
from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# These destinations stay inside the existing updater's allowed app layout.
LEGAL_FILES = {
    "LICENSE": "LICENSE",
    "licenses/FolderOrganizer/TERMS_OF_USE.md": "docs/TERMS_OF_USE.md",
    "licenses/FolderOrganizer/PRIVACY.md": "docs/PRIVACY.md",
    "licenses/FolderOrganizer/PERMISSION_REQUEST.md": "docs/PERMISSION_REQUEST.md",
}


def bundled_document(relative: str, data: bytes) -> bytes:
    if relative.startswith("licenses/FolderOrganizer/") and relative.endswith(".md"):
        # Repository docs live in docs/, bundled docs two levels below the app.
        return data.replace(b"](../LICENSE)", b"](../../LICENSE)")
    return data


def include_legal_documents(destination: Path, source_root: Path = ROOT) -> None:
    for relative, source in LEGAL_FILES.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bundled_document(relative, (source_root / source).read_bytes()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    include_legal_documents(parser.parse_args().destination)
