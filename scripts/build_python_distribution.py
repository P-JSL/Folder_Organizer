"""Build the Widgets-only Python portable package using dependency-aware hooks.

Do not collect the entire PySide6 wheel: its QML, Designer/tools and unrelated
resources are not used by this QtCore/QtGui/QtWidgets application. PyInstaller's
module hooks retain linked Qt libraries, platforms, image plugins, translations
for the imported modules and Windows' opengl32sw software fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

from package_legal import include_legal_documents

ROOT = Path(__file__).resolve().parents[1]
UNUSED_QT_MODULES = (
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtUiTools", "PySide6.QtTest",
)


def package_layout(destination: Path) -> dict:
    files = {path.relative_to(destination).as_posix(): path.stat().st_size
             for path in destination.rglob("*") if path.is_file()}
    categories = {}
    for name, size in files.items():
        folded = name.casefold()
        if folded.startswith("_internal/pyside6/"):
            category = "qt"
        elif folded.startswith("_internal/pil/"):
            category = "pillow"
        elif folded.startswith("_internal/"):
            category = "python-runtime-and-dependencies"
        elif folded.startswith("licenses/"):
            category = "license-notices"
        else:
            category = "programs-and-documents"
        value = categories.setdefault(category, {"files": 0, "bytes": 0})
        value["files"] += 1
        value["bytes"] += size
    return {"edition": "Python", "version": json.loads((destination / "version.json").read_text())["version"],
            "policy": "imported Qt module hooks; no collect-all-data and no post-build DLL deletion",
            "files": len(files), "unpacked_bytes": sum(files.values()), "categories": categories,
            "qt_files": sorted(name for name in files if name.casefold().startswith("_internal/pyside6/")),
            "pillow_codec_policy": "unchanged; all installed Pillow plugins retained"}


def main() -> None:
    if sys.platform != "win32":
        raise RuntimeError("The release package must be built on native Windows")
    common = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
              "--specpath", str(ROOT / "build/pyinstaller-specs"), "--distpath", str(ROOT / "dist")]
    desktop = common + ["--onedir", "--name", "FolderOrganizer", "--workpath", str(ROOT / "build/python-desktop")]
    for name in ("PySide6-Essentials", "shiboken6", "Pillow", "pypdf"):
        desktop += ["--copy-metadata", name]
    for module in UNUSED_QT_MODULES:
        desktop += ["--exclude-module", module]
    desktop.append(str(ROOT / "desktop_entry.py"))
    subprocess.run(desktop, cwd=ROOT, check=True)
    helper = common + ["--onefile", "--name", "FolderOrganizerUpdater", "--workpath",
                       str(ROOT / "build/python-updater"), str(ROOT / "updater_entry.py")]
    subprocess.run(helper, cwd=ROOT, check=True)
    destination = ROOT / "dist/FolderOrganizer"
    for name in ("version.json", "README.md", "CHANGELOG.md"):
        shutil.copyfile(ROOT / name, destination / name)
    shutil.copyfile(ROOT / "dist/FolderOrganizerUpdater.exe", destination / "FolderOrganizerUpdater.exe")
    shutil.copyfile(ROOT / "docs/THIRD_PARTY.md", destination / "THIRD_PARTY.md")
    shutil.copytree(ROOT / "docs/licenses", destination / "licenses", dirs_exist_ok=True)
    include_legal_documents(destination)
    layout = package_layout(destination)
    (ROOT / "dist/PYTHON_PACKAGE_LAYOUT.json").write_text(
        json.dumps(layout, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in layout.items() if key != "qt_files"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
