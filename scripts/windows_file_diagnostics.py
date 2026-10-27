"""Expose non-secret file metadata differences when Windows CI fails."""
import json
import os
from pathlib import Path
import tempfile
import zipfile

from folder_organizer import matching, recovery, scanner


def details(info):
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_birthtime_ns", "st_file_attributes")
    return {key: getattr(info, key, None) for key in keys}


def main():
    with tempfile.TemporaryDirectory(prefix="organizer-metadata-") as directory:
        source = Path(directory) / "fixture.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("one.txt", b"data")
        with source.open("rb") as stream:
            opened = details(os.fstat(stream.fileno()))
        with os.scandir(directory) as children:
            cached = details(next(children).stat(follow_symlinks=False))
        result = scanner.scan_folder(directory)
        entry = result.files[0]
        report = {"path_stat": details(source.stat()), "handle_stat": opened,
                  "direntry_stat": cached, "scan": entry.to_dict(),
                  "inspection": recovery.inspect_file(source)}
        try:
            with matching._open_snapshot(entry) as stream:
                report["matching_read_bytes"] = len(stream.read())
        except OSError as exc:
            report["matching_error"] = str(exc)
        encoded = json.dumps(report).replace("%", "%25").replace("\n", "%0A")
        print("::notice title=Windows file snapshot diagnostics::" + encoded)


if __name__ == "__main__":
    main()
