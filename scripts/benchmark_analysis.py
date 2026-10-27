"""Reproducible read-only analysis benchmark using disposable synthetic files.

Run from the repository after installing its dependencies. ``--revision`` loads
matching.py from an earlier local Git revision to compare identical fixtures.
Times depend on hardware and OS cache; byte counts describe reads requested by
the algorithm, not physical disk traffic. Fixture generation is not timed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types

from folder_organizer import matching
from folder_organizer.scanner import scan_folder


def run_benchmark(*, count=64, size=2 * 1024 * 1024, repeats=3, revision=None):
    module = matching
    if revision:
        code = subprocess.check_output(
            ["git", "show", f"{revision}:folder_organizer/matching.py"], text=True,
            encoding="utf-8",
        )
        module = types.ModuleType("folder_organizer._benchmark_matching")
        module.__package__ = "folder_organizer"
        sys.modules[module.__name__] = module
        exec(compile(code, "<benchmark matching revision>", "exec"), module.__dict__)
    with tempfile.TemporaryDirectory(prefix="organizer-analysis-benchmark-") as temporary:
        root = Path(temporary)
        for index in range(count):
            block = hashlib.sha256(str(index).encode("ascii")).digest()
            (root / f"file-{index:04d}.bin").write_bytes((block * ((size + 31) // 32))[:size])
        duplicate_count = min(4, count)
        for index in range(duplicate_count):
            shutil.copyfile(root / f"file-{index:04d}.bin", root / f"copy-{index:04d}.bin")
        scan = scan_folder(root)
        original_open = module._open_snapshot
        requested = 0

        class CountedReader:
            def __init__(self, stream):
                self.stream = stream

            def read(self, *args):
                nonlocal requested
                chunk = self.stream.read(*args)
                requested += len(chunk)
                return chunk

            def __getattr__(self, name):
                return getattr(self.stream, name)

        @contextmanager
        def counted_open(entry):
            with original_open(entry) as stream:
                yield CountedReader(stream)

        module._open_snapshot = counted_open
        durations, reads = [], []
        try:
            for _ in range(repeats):
                requested = 0
                started = time.perf_counter()
                groups = module.find_duplicates(scan.files)
                durations.append(time.perf_counter() - started)
                reads.append(requested)
                actual = {frozenset(Path(path).name for path in group.paths) for group in groups}
                expected = {frozenset((f"file-{i:04d}.bin", f"copy-{i:04d}.bin")) for i in range(duplicate_count)}
                assert actual == expected, "The benchmark did not preserve exact duplicate results"
        finally:
            module._open_snapshot = original_open
        return {"revision": revision or "working-tree", "fixture": "equal-size distinct files with four duplicate pairs",
                "files": len(scan.files), "file_bytes": size, "repeats": repeats,
                "duplicate_groups": duplicate_count,
                "median_seconds": round(statistics.median(durations), 6),
                "requested_read_bytes_per_run": reads,
                "timing_scope": "analysis only; synthetic local files; OS cache may be warm"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision")
    parser.add_argument("--count", type=int, default=64)
    parser.add_argument("--size", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--repeats", type=int, default=3)
    options = parser.parse_args()
    if not 4 <= options.count <= 1000 or not 1024 <= options.size <= 16 * 1024 * 1024 or not 1 <= options.repeats <= 10:
        parser.error("count: 4..1000, size: 1024..16777216, repeats: 1..10")
    print(json.dumps(run_benchmark(**vars(options)), ensure_ascii=False, indent=2))
