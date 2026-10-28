"""Reproducible read-only analysis benchmark using disposable synthetic files.

Run from the repository after installing its dependencies. ``--revision`` loads
matching.py from an earlier local Git revision to compare identical fixtures.
Times depend on hardware and OS cache; byte counts describe reads requested by
the algorithm, not physical disk traffic. Fixture generation is not timed.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import types

from folder_organizer import matching
from folder_organizer.scanner import scan_folder


def _revision_modules(revision=None):
    """Load trusted local source for both stages without changing a checkout."""
    if not revision:
        from folder_organizer import scanner
        for module in (scanner, matching):
            module._benchmark_source_sha256 = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        return scanner, matching, "working-tree"
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", revision + "^{commit}"],
        text=True, encoding="utf-8",
    ).strip()
    modules = []
    for name in ("scanner", "matching"):
        code = subprocess.check_output(
            ["git", "show", f"{commit}:folder_organizer/{name}.py"], text=True,
            encoding="utf-8",
        )
        module = types.ModuleType(f"folder_organizer._benchmark_{name}_{commit[:12]}")
        module.__package__ = "folder_organizer"
        module._benchmark_source_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        sys.modules[module.__name__] = module
        exec(compile(code, f"<benchmark {name} {commit[:12]}>", "exec"), module.__dict__)
        modules.append(module)
    return *modules, commit


@contextmanager
def _count_reads(module):
    original_open = module._open_snapshot
    counts = {"requested_read_bytes": 0, "snapshot_opens": 0}

    class Reader:
        def __init__(self, stream):
            self.stream = stream

        def read(self, *args):
            chunk = self.stream.read(*args)
            counts["requested_read_bytes"] += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self.stream, name)

    @contextmanager
    def counted_open(entry):
        counts["snapshot_opens"] += 1
        with original_open(entry) as stream:
            yield Reader(stream)

    module._open_snapshot = counted_open
    try:
        yield counts
    finally:
        module._open_snapshot = original_open


def run_scaling_benchmark(*, counts=(1000, 2000), repeats=3, revision=None,
                          compare_revision=None, max_pairs=25_000):
    """Compare the same disposable fixtures, including a pair late in path order.

    This measures the GUI's inventory path: current scanners can omit core
    sorting because Qt owns display sorting; old scanners always sort results.
    It deliberately excludes Qt sort/render time and physical disk I/O claims.
    Similarity is name-only so the candidate-index work can be inspected apart
    from bounded text/image feature costs. Content is still read by the exact
    duplicate exclusion pass in both versions.
    """
    versions = [_revision_modules(compare_revision)] if compare_revision else []
    versions.append(_revision_modules(revision))
    rows = []
    with tempfile.TemporaryDirectory(prefix="organizer-scaling-benchmark-") as temporary:
        for count in counts:
            root = Path(temporary) / str(count)
            root.mkdir()
            for index in range(count):
                stem = base64.b32encode(hashlib.sha256(str(index).encode("ascii")).digest()).decode("ascii")[:24].lower()
                (root / (stem + ".txt")).write_bytes((str(index) + ": unique fixture\n").encode() * (1 + index % 7))
            late_names = ("zzzzzzzzzzzz-review.txt", "zzzzzzzzzzzz-review copy.txt")
            (root / late_names[0]).write_bytes(b"First independent version, not byte-identical.\n")
            (root / late_names[1]).write_bytes(b"Second independent version, not byte-identical.\n")
            for scanner_module, matching_module, label in versions:
                scan_durations, analysis_durations, cpu_durations = [], [], []
                measurements = []
                for _repeat in range(repeats):
                    events = 0
                    def on_progress(_event):
                        nonlocal events
                        events += 1
                    scan_options = {"progress_detail": on_progress}
                    if "sort_results" in inspect.signature(scanner_module.scan_folder).parameters:
                        scan_options["sort_results"] = False
                    started = time.perf_counter()
                    scan = scanner_module.scan_folder(root, **scan_options)
                    scan_durations.append(time.perf_counter() - started)
                    assert len(scan.files) == count + 2 and not scan.cancelled
                    report = {}
                    options = {"methods": ("name",), "name_threshold": .99,
                               "report": report, "max_pairs": max_pairs}
                    if "search_mode" in inspect.signature(matching_module.find_similar).parameters:
                        options["search_mode"] = "auto"
                    started, cpu_started = time.perf_counter(), time.process_time()
                    with _count_reads(matching_module) as reads:
                        groups = matching_module.find_similar(scan.files, **options)
                    analysis_durations.append(time.perf_counter() - started)
                    cpu_durations.append(time.process_time() - cpu_started)
                    late_found = any({Path(path).name for path in group.paths} == set(late_names) for group in groups)
                    metric_names = ("files_considered", "compared_pairs", "max_pairs", "limit_reached",
                                    "effective_search_mode", "exhaustive", "potential_pairs",
                                    "candidate_files_visited", "candidate_lookups", "index_postings_scanned",
                                    "index_keys", "index_keys_retained_peak", "index_evictions", "index_key_limit", "index_posting_limit",
                                    "candidate_pairs", "candidate_pairs_selected", "candidate_limit_reached")
                    measurements.append({"scan_metrics": dict(getattr(scan, "scan_metrics", {})),
                                         "scan_progress_callbacks": events, **reads,
                                         "late_pair_found": late_found, "result_pairs": len(groups),
                                         "comparison": {key: report[key] for key in metric_names if key in report}})
                rows.append({"revision": label, "files": count + 2, "repeats": repeats,
                             "source_sha256": {"scanner": scanner_module._benchmark_source_sha256,
                                               "matching": matching_module._benchmark_source_sha256},
                             "inventory_sorted_in_core": "sort_results" not in inspect.signature(scanner_module.scan_folder).parameters,
                             "median_inventory_seconds": round(statistics.median(scan_durations), 6),
                             "median_analysis_seconds": round(statistics.median(analysis_durations), 6),
                             "median_analysis_cpu_seconds": round(statistics.median(cpu_durations), 6),
                             "runs": measurements})
    return {"suite": "scaling", "python": platform.python_version(), "platform": platform.platform(),
            "logical_cpus": os.cpu_count(), "fixture": "unique random names plus one normalized-name pair late in path order",
            "scope": "same local files per version; creation excluded; page cache may be warm; GUI display sorting excluded; byte counters are requested reads, not physical disk traffic",
            "results": rows}


def run_gui_key_benchmark(*, counts=(1000, 2000), repeats=3, compare_revision=None):
    """Count real date/size formatting on name-sort key retrieval, without a GUI.

    This is a model hot-path benchmark, not a measurement of complete Qt sorting
    or screen rendering. Synthetic metadata is never opened as a file.
    """
    from folder_organizer import ui
    from folder_organizer.models import FileEntry
    versions = []
    if compare_revision:
        commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", compare_revision + "^{commit}"], text=True).strip()
        code = subprocess.check_output(["git", "show", f"{commit}:folder_organizer/ui.py"], text=True, encoding="utf-8")
        module = types.ModuleType("folder_organizer._benchmark_ui_" + commit[:12])
        module.__package__ = "folder_organizer"
        sys.modules[module.__name__] = module
        exec(compile(code, "<benchmark UI " + commit[:12] + ">", "exec"), module.__dict__)
        versions.append((module, commit, hashlib.sha256(code.encode()).hexdigest()))
    versions.append((ui, "working-tree", hashlib.sha256(Path(ui.__file__).read_bytes()).hexdigest()))
    rows = []
    for count in counts:
        entries = [FileEntry(path=f"/benchmark/file-{index:06d}.txt", relative_path=f"file-{index:06d}.txt",
                    name=f"file-{index:06d}.txt", size=index + 1, modified=1_700_000_000.0,
                    created=1_600_000_000.0, created_source="synthetic", kind="file", suffix=".txt",
                    mtime_ns=1_700_000_000_000_000_000, device=1, inode=index + 1) for index in range(count)]
        for module, label, digest in versions:
            build_times, key_times, runs = [], [], []
            original_date, original_size = module.format_date, module.format_size
            counters = {"date_format_calls": 0, "size_format_calls": 0}
            def date(value):
                counters["date_format_calls"] += 1
                return original_date(value)
            def size(value):
                counters["size_format_calls"] += 1
                return original_size(value)
            module.format_date, module.format_size = date, size
            try:
                for _repeat in range(repeats):
                    model = module.FileTableModel()
                    started = time.perf_counter()
                    model.set_entries(entries)
                    build_times.append(time.perf_counter() - started)
                    counters.update(date_format_calls=0, size_format_calls=0)
                    started = time.perf_counter()
                    keys = [model.sort_value(row, 1) for row in range(count)]
                    key_times.append(time.perf_counter() - started)
                    assert keys == [entry.name.casefold() for entry in entries]
                    runs.append(dict(counters))
                    model.deleteLater()
            finally:
                module.format_date, module.format_size = original_date, original_size
            rows.append({"revision": label, "ui_source_sha256": digest, "files": count, "repeats": repeats,
                         "median_model_build_seconds": round(statistics.median(build_times), 6),
                         "median_name_key_seconds": round(statistics.median(key_times), 6), "runs": runs})
    return {"suite": "gui-keys", "python": platform.python_version(), "platform": platform.platform(),
            "scope": "metadata model construction and one name-key lookup per row; no file I/O, no full Qt sorting or rendering", "results": rows}


def run_payload_benchmark(*, sizes=(1024 * 1024, 2 * 1024 * 1024), repeats=3):
    """Measure real payload encoding and independent reconstruction.

    This isolates the object-store codec from journal updates and raw retirement.
    Sources are disposable files: after the actual encoder verifies them, the
    benchmark removes them and verifies reconstruction from stored chunks alone.
    Object bytes and descriptor serialization exclude filesystem block overhead.
    """
    from folder_organizer import operations, recovery_payload
    rows = []
    entropy_chunk = random.Random(20261028).randbytes(recovery_payload.CHUNK_SIZE)
    text_chunk = (b"Folder Organizer keeps lossless recovery data for a careful review.\n" * 16384)[:recovery_payload.CHUNK_SIZE]
    text_chunk = (text_chunk * 2)[:recovery_payload.CHUNK_SIZE]
    for size in sizes:
        for fixture in ("compressible-text", "duplicate-high-entropy", "unique-high-entropy"):
            durations, restore_times, runs = [], [], []
            for repeat in range(repeats):
                with tempfile.TemporaryDirectory(prefix="organizer-payload-benchmark-") as temporary:
                    root = Path(temporary)
                    if fixture == "compressible-text":
                        payloads = [(text_chunk * ((size + len(text_chunk) - 1) // len(text_chunk)))[:size]]
                    elif fixture == "duplicate-high-entropy":
                        payload = (entropy_chunk * ((size + len(entropy_chunk) - 1) // len(entropy_chunk)))[:size]
                        payloads = [payload, payload]
                    else:
                        payloads = [random.Random(20261028 + size).randbytes(size)]
                    paths = [root / f"source-{index}.bin" for index in range(len(payloads))]
                    for path, payload in zip(paths, payloads):
                        path.write_bytes(payload)
                    fingerprints = [operations._fingerprint(path) for path in paths]
                    descriptors = []
                    started = time.perf_counter()
                    status = "packed"
                    try:
                        for path, fingerprint in zip(paths, fingerprints):
                            descriptors.append(recovery_payload.pack_file(root, path, fingerprint,
                                               min_savings_ratio=0.0 if fixture == "duplicate-high-entropy" else .10))
                    except recovery_payload.PayloadNotBeneficial:
                        status = "kept-raw"
                    durations.append(time.perf_counter() - started)
                    objects = root / operations.STATE_DIRECTORY / "recovery-objects" / "v1"
                    object_files = [path for path in objects.rglob("*") if path.is_file()] if objects.exists() else []
                    object_bytes = sum(path.stat().st_size for path in object_files)
                    recipe_bytes = sum(len(json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) for descriptor in descriptors)
                    verified = False
                    if status == "packed":
                        for path in paths:
                            path.unlink()
                        started = time.perf_counter()
                        for index, (descriptor, fingerprint) in enumerate(zip(descriptors, fingerprints)):
                            restored = recovery_payload.materialize(root, descriptor, root / f"restored-{index}.bin")
                            assert restored["sha256"] == fingerprint["sha256"] and restored["size"] == fingerprint["size"]
                        restore_times.append(time.perf_counter() - started)
                        verified = True
                    else:
                        assert all(path.read_bytes() == payload for path, payload in zip(paths, payloads))
                    runs.append({"status": status, "logical_input_bytes": sum(map(len, payloads)),
                                 "object_payload_bytes": object_bytes, "object_count": len(object_files),
                                 "serialized_descriptor_bytes": recipe_bytes,
                                 "raw_retained_bytes": sum(map(len, payloads)) if status == "kept-raw" else 0,
                                 "independent_restore_verified": verified,
                                 "encoder_stats": [descriptor["stats"] for descriptor in descriptors]})
            rows.append({"fixture": fixture, "bytes_per_file": size, "repeats": repeats,
                         "median_encode_seconds": round(statistics.median(durations), 6),
                         "median_restore_seconds": round(statistics.median(restore_times), 6) if restore_times else None,
                         "runs": runs})
    return {"suite": "recovery-payload", "python": platform.python_version(), "platform": platform.platform(),
            "payload_source_sha256": hashlib.sha256(Path(recovery_payload.__file__).read_bytes()).hexdigest(),
            "scope": "actual pack_file and materialize with temporary files; source fingerprint preflight, journal mutation, raw-retirement protocol and filesystem allocation overhead excluded",
            "results": rows}


def run_preview_benchmark(*, counts=(4, 8), size=1024 * 1024, repeats=3, compare_revision=None):
    """Measure keep-first previews after an exact scan, with no file mutation."""
    from folder_organizer import operations, rules
    versions = []
    if compare_revision:
        old_scanner, old_matching, commit = _revision_modules(compare_revision)
        loaded = {}
        for name in ("operations", "rules"):
            code = subprocess.check_output(["git", "show", f"{commit}:folder_organizer/{name}.py"], text=True, encoding="utf-8")
            module = types.ModuleType(f"folder_organizer._benchmark_preview_{name}_{commit[:12]}")
            module.__package__ = "folder_organizer"
            sys.modules[module.__name__] = module
            exec(compile(code, f"<benchmark preview {name}>", "exec"), module.__dict__)
            loaded[name] = module
        loaded["rules"].ops = loaded["operations"]
        versions.append((old_scanner, old_matching, loaded["operations"], loaded["rules"], commit, False))
    from folder_organizer import scanner
    versions.append((scanner, matching, operations, rules, "working-tree", True))
    rows = []
    with tempfile.TemporaryDirectory(prefix="organizer-preview-benchmark-") as temporary:
        for count in counts:
            root = Path(temporary) / str(count)
            root.mkdir()
            for index in range(count):
                block = hashlib.sha256(str(index).encode("ascii")).digest()
                raw = (block * ((size + 31) // 32))[:size]
                for suffix in ("a", "b"):
                    (root / f"group-{index:04d}-{suffix}.bin").write_bytes(raw)
            for scanner_module, matching_module, ops_module, rule_module, label, reuse in versions:
                scan = scanner_module.scan_folder(root)
                groups = matching_module.find_duplicates(scan.files)
                assert len(groups) == count
                durations, runs = [], []
                original = ops_module._fingerprint
                calls = input_bytes = 0
                def fingerprint(path):
                    nonlocal calls, input_bytes
                    result = original(path)
                    calls += 1
                    input_bytes += result["size"]
                    return result
                ops_module._fingerprint = fingerprint
                try:
                    for _repeat in range(repeats):
                        calls = input_bytes = 0
                        started = time.perf_counter()
                        plan = (rule_module.build_existing_duplicate_plan(scan, groups) if reuse else
                                rule_module.build_duplicate_plan(scan, groups, retention_rule="first", preserve_first=True))
                        durations.append(time.perf_counter() - started)
                        assert len(plan["items"]) == count
                        runs.append({"full_fingerprint_calls": calls, "fingerprint_input_bytes": input_bytes,
                                     "planned_copies": len(plan["items"]), "reused_verified_results": reuse})
                finally:
                    ops_module._fingerprint = original
                rows.append({"revision": label, "duplicate_groups": count, "files": count * 2,
                             "bytes_per_file": size, "repeats": repeats,
                             "median_preview_seconds": round(statistics.median(durations), 6), "runs": runs})
            assert not (root / ".folder-organizer").exists() and not (root / "delete_files").exists()
    return {"suite": "duplicate-preview", "python": platform.python_version(), "platform": platform.platform(),
            "scope": "keep-first preview after a completed exact scan; no execution; counters sum complete fingerprints requested, not physical disk I/O",
            "results": rows}


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
    parser.add_argument("--suite", choices=("duplicates", "scaling", "gui-keys", "recovery-payload", "duplicate-preview"), default="duplicates")
    parser.add_argument("--revision")
    parser.add_argument("--compare-revision", help="Scaling only: compare this local commit against the working tree on identical files")
    parser.add_argument("--counts", type=int, nargs="+", default=[1000, 2000], help="Scaling fixture sizes before adding two late-pair files")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1024 * 1024, 2 * 1024 * 1024], help="Payload fixture sizes in bytes")
    parser.add_argument("--max-pairs", type=int, default=25_000)
    parser.add_argument("--count", type=int, default=64)
    parser.add_argument("--size", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--repeats", type=int, default=3)
    options = parser.parse_args()
    if options.suite == "duplicate-preview":
        if not options.counts or len(options.counts) > 8 or any(not 1 <= count <= 64 for count in options.counts) or not 1024 <= options.size <= 16 * 1024 * 1024 or not 1 <= options.repeats <= 10 or options.revision:
            parser.error("duplicate-preview counts: 1..64 (at most 8), size: 1024..16777216, repeats: 1..10; use --compare-revision")
        print(json.dumps(run_preview_benchmark(counts=options.counts, size=options.size, repeats=options.repeats,
                         compare_revision=options.compare_revision), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if options.suite == "recovery-payload":
        if not options.sizes or len(options.sizes) > 8 or any(not 4096 <= size <= 64 * 1024 * 1024 for size in options.sizes) or not 1 <= options.repeats <= 10 or options.revision or options.compare_revision:
            parser.error("recovery-payload sizes: 4096..67108864 (at most 8), repeats: 1..10; no revision option")
        print(json.dumps(run_payload_benchmark(sizes=options.sizes, repeats=options.repeats), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if options.suite == "gui-keys":
        if not options.counts or len(options.counts) > 8 or any(not 4 <= count <= 50_000 for count in options.counts) or not 1 <= options.repeats <= 10 or options.revision:
            parser.error("gui-keys counts: 4..50000 (at most 8), repeats: 1..10; use --compare-revision for the baseline")
        print(json.dumps(run_gui_key_benchmark(counts=options.counts, repeats=options.repeats,
                         compare_revision=options.compare_revision), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if options.suite == "scaling":
        if not options.counts or len(options.counts) > 8 or any(not 4 <= count <= 50_000 for count in options.counts) or not 1 <= options.repeats <= 10 or not 1 <= options.max_pairs <= 250_000:
            parser.error("scaling counts: 4..50000 (at most 8), repeats: 1..10, max-pairs: 1..250000")
        print(json.dumps(run_scaling_benchmark(counts=options.counts, repeats=options.repeats,
                         revision=options.revision, compare_revision=options.compare_revision,
                         max_pairs=options.max_pairs), ensure_ascii=False, indent=2))
        raise SystemExit(0)
    if options.compare_revision:
        parser.error("--compare-revision requires --suite scaling")
    if not 4 <= options.count <= 1000 or not 1024 <= options.size <= 16 * 1024 * 1024 or not 1 <= options.repeats <= 10:
        parser.error("count: 4..1000, size: 1024..16777216, repeats: 1..10")
    print(json.dumps(run_benchmark(count=options.count, size=options.size,
                                  repeats=options.repeats, revision=options.revision), ensure_ascii=False, indent=2))
