"""Compare read-only Python/Rust results and fresh-process performance.

Only newly generated disposable files are analyzed. At least five runs per
engine alternate their order. The operating-system cache is not flushed; these
are local synthetic measurements, not cold-cache, SSD, NAS, or GUI claims.

Example on a native Windows release runner:
  python scripts/benchmark_editions_v070.py --require-windows \
    --python-command '["dist/FolderOrganizer/FolderOrganizer.exe", "--cli"]' \
    --rust-command '["dist/FolderOrganizerRust/FolderOrganizerRustCLI.exe"]' \
    --output editions-benchmark.json
"""
from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MIB = 1024 * 1024


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def json_command(value: str) -> list[str]:
    command = json.loads(value)
    if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
        raise argparse.ArgumentTypeError("Command must be a nonempty JSON array of argument strings")
    return command


def relative(value: str, root: Path) -> str:
    # Resolving both sides handles Windows TEMP 8.3 aliases and verbatim paths.
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved, base = str(path.resolve()), str(root.resolve())
    if os.name == "nt":
        def ordinary(text: str) -> str:
            if text.startswith("\\\\?\\UNC\\"):
                return "\\\\" + text[8:]
            return text[4:] if text.startswith("\\\\?\\") else text
        resolved, base = ordinary(resolved), ordinary(base)
    result = os.path.relpath(resolved, base).replace("\\", "/")
    if result == ".." or result.startswith("../"):
        raise ValueError("Report contains a path outside the disposable fixture")
    return result


def _timestamp(entry: dict, nanosecond_keys: tuple[str, ...]) -> int | None:
    for key in nanosecond_keys:
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lstrip("-").isdigit():
            return int(value)
    return None


def normalize(report: dict, root: Path) -> dict:
    """Read the versioned cross-engine schema without lossy float date parsing."""
    if report.get("schema") != "folder-organizer.common.v1":
        raise ValueError("Expected the versioned folder-organizer.common.v1 report")
    expected_version = json.loads((ROOT / "version.json").read_text(encoding="utf-8"))["version"]
    if report.get("app_version") != expected_version or report.get("action_authority") is not False:
        raise ValueError("Compiled version differs from the release marker or report claims action authority")
    scan = report["inventory"]
    files = scan.get("files", report.get("files"))
    if not isinstance(files, list):
        raise ValueError("Report has no file inventory")
    inventory = {}
    for entry in files:
        name = relative(entry.get("path") or entry["relative_path"], root)
        if name in inventory:
            raise ValueError(f"Repeated inventory member: {name}")
        inventory[name] = {
            "size": entry["size"], "kind": entry["kind"],
            "extension": entry["extension"],
            "modified_ns": _timestamp(entry, ("mtime_ns", "modified_ns")),
            "created_ns": _timestamp(entry, ("created_ns",)),
        }
    groups = []
    raw_groups = report.get("duplicates", [])
    if isinstance(raw_groups, dict):
        raw_groups = raw_groups.get("groups", [])
    for group in raw_groups:
        paths = tuple(sorted(relative(path, root) for path in group["paths"]))
        if len(paths) < 2 or len(set(paths)) != len(paths):
            raise ValueError("Duplicate group contains missing or repeated members")
        groups.append(paths)
    if len(groups) != len(set(groups)):
        raise ValueError("Duplicate report repeats an exact group")
    analysis = report.get("duplicate_analysis") or report.get("duplicate_diagnostics") or {}
    if not analysis and isinstance(report.get("duplicates"), dict):
        analysis = report["duplicates"]
    metrics = analysis.get("metrics") or report.get("duplicate_metrics") or {}
    warnings = list(scan.get("warnings", [])) + list(analysis.get("warnings", []))
    if scan.get("complete") is not True or analysis.get("requested") is not True or analysis.get("complete") is not True or warnings:
        raise ValueError(f"Synthetic input returned a partial analysis or warnings: {warnings!r}")
    return {"inventory": inventory, "groups": sorted(groups), "duplicate_metrics": metrics,
            "app_version": report.get("app_version"),
            "schema_version": report["schema"], "scope": report.get("scope")}


@dataclass
class Fixture:
    root: Path
    expected: dict[str, tuple[int, str]]
    digests: dict[str, str]

    def manifest_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.digests, sort_keys=True).encode()).hexdigest()

    def exact_groups(self) -> list[tuple[str, ...]]:
        buckets: dict[tuple[int, str], list[str]] = {}
        for name, (size, _kind) in self.expected.items():
            buckets.setdefault((size, self.digests[name]), []).append(name)
        return sorted(tuple(sorted(names)) for names in buckets.values() if len(names) > 1)

    def verify_unchanged(self) -> None:
        actual = {path.relative_to(self.root).as_posix(): sha256(path)
                  for path in self.root.rglob("*") if path.is_file()}
        if actual != self.digests:
            raise ValueError("Read-only analysis changed the disposable source tree")


def create_fixture(parent: Path, kind: str, small_files: int, large_files: int, large_mib: int) -> Fixture:
    root = parent / kind
    root.mkdir()
    expected = {}

    def write(name: str, data: bytes) -> Path:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        expected[name] = (len(data), "application" if path.suffix.lower() == ".exe" else "file")
        return path

    if kind == "small":
        for index in range(small_files):
            body = hashlib.sha256(f"small-{index}".encode()).digest() * 32
            write(f"자료-{index % 16:02d}/파일-{index:06d}.dat", body)
        write("동일/첫 파일.txt", b"read-only exact small duplicate\n")
        write("동일/사본.txt", b"read-only exact small duplicate\n")
        write("빈 파일 A.txt", b"")
        write("빈 파일 B.txt", b"")
        write("실행 예시.exe", b"Synthetic fixture only; never executed.\n")
        write(".숨김 자료.txt", b"include-hidden contract\n")
    else:
        size = large_mib * MIB
        for index in range(large_files):
            block = hashlib.sha256(f"large-{index}".encode()).digest() * (MIB // 32)
            write(f"동크기 비중복/자료-{index:03d}.bin", block * large_mib)
        exact = write("동일/대용량 원본.bin", b"D" * size)
        copy = root / "동일/대용량 사본.bin"
        shutil.copyfile(exact, copy)
        expected[copy.relative_to(root).as_posix()] = (size, "file")
        # This pair has identical first/middle/last 64KiB samples but different
        # primary bytes away from those samples. Sampling must never certify it.
        write("샘플 충돌/동일 샘플 A.bin", b"S" * size)
        other = write("샘플 충돌/동일 샘플 B.bin", b"S" * size)
        with other.open("r+b") as stream:
            stream.seek(128 * 1024)
            stream.write(b"different unsampled primary bytes")
    ignored = {
        "의도적 제외.skip": b"excluded by explicit glob",
        "#recycle/서버 휴지통.txt": b"excluded system recycle directory",
        "delete_files/삭제 보관.txt": b"excluded deletion storage",
        ".folder-organizer/history/state.json": b"excluded Python journal",
        ".folder-organizer-rust/history/state.json": b"excluded Rust journal",
    }
    for name, body in ignored.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    digests = {path.relative_to(root).as_posix(): sha256(path)
               for path in root.rglob("*") if path.is_file()}
    return Fixture(root, expected, digests)


class WindowsCounters:
    """Query a retained real process handle, including final kernel counters."""
    def __init__(self, process: subprocess.Popen):
        from ctypes import wintypes
        self.handle = wintypes.HANDLE(int(process._handle))
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi", use_last_error=True)

        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]
        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in (
                    "peak_working", "working", "quota_peak_paged", "quota_paged",
                    "quota_peak_nonpaged", "quota_nonpaged", "pagefile", "peak_pagefile", "private")]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_operations", "write_operations", "other_operations", "read_bytes", "write_bytes", "other_bytes")]
        self.FileTime, self.Memory, self.Io = FileTime, Memory, Io
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FileTime)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(Io)]
        self.kernel.GetProcessIoCounters.restype = wintypes.BOOL
        self.psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    def read(self) -> dict:
        result = {}
        times = [self.FileTime() for _ in range(4)]
        if self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
            result["cpu_seconds"] = sum((value.high << 32) + value.low for value in times[2:]) / 10_000_000
        memory = self.Memory()
        memory.cb = ctypes.sizeof(memory)
        if self.psapi.GetProcessMemoryInfo(self.handle, ctypes.byref(memory), memory.cb):
            result["peak_private_commit_bytes"] = memory.peak_pagefile
            result["peak_working_set_bytes"] = memory.peak_working
            result["private_bytes_last_observed"] = memory.private
        io = self.Io()
        if self.kernel.GetProcessIoCounters(self.handle, ctypes.byref(io)):
            result["process_read_transfer_bytes"] = io.read_bytes
            result["process_write_transfer_bytes"] = io.write_bytes
        return result


def measure(command: list[str], output: Path, timeout: int) -> dict:
    cpu_before = None
    if os.name != "nt":
        import resource
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_before = before.ru_utime + before.ru_stime
    started = time.perf_counter()
    with output.with_suffix(".stdout.txt").open("wb") as stdout, output.with_suffix(".stderr.txt").open("wb") as stderr:
        process = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
        counters = WindowsCounters(process) if os.name == "nt" else None
        observed = {}
        try:
            while True:
                if counters:
                    for key, value in counters.read().items():
                        observed[key] = max(observed.get(key, 0), value)
                if time.perf_counter() - started > timeout:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=0.02)
                    break
                except subprocess.TimeoutExpired:
                    continue
            if counters:
                for key, value in counters.read().items():
                    observed[key] = max(observed.get(key, 0), value)
        except BaseException:
            process.kill()
            process.wait()
            raise
    wall = time.perf_counter() - started
    if process.returncode:
        text = output.with_suffix(".stderr.txt").read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(f"Engine failed ({process.returncode}): {text[-6000:]}")
    if cpu_before is not None:
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        observed["cpu_seconds"] = max(0.0, after.ru_utime + after.ru_stime - cpu_before)
    return {"wall_seconds": wall, **observed,
            "cpu_method": "GetProcessTimes" if counters else "RUSAGE_CHILDREN delta; sequential children",
            "memory_io_method": "GetProcessMemoryInfo/GetProcessIoCounters" if counters else "not collected on this platform"}


def check_result(result: dict, fixture: Fixture, reference: dict | None) -> dict:
    simple = {name: (entry["size"], entry["kind"]) for name, entry in result["inventory"].items()}
    if simple != fixture.expected:
        raise ValueError(f"Inventory contract differs: expected {len(fixture.expected)} entries, received {len(simple)}")
    if result["groups"] != fixture.exact_groups():
        raise ValueError("Byte-verified duplicate groups differ from generated SHA-256 fixture truth")
    dates = {"created_ns_compared": 0, "created_ns_availability_differences": 0,
             "created_ns_unavailable_both": 0}
    if any(entry["modified_ns"] is None for entry in result["inventory"].values()):
        raise ValueError("Common report lost exact modification nanoseconds")
    if reference is None:
        return dates
    if result["scope"] != reference["scope"]:
        raise ValueError("Engines disagree on the requested scan scope")
    if set(result["inventory"]) != set(reference["inventory"]):
        raise ValueError("Engines disagree on inventory members")
    for name, item in result["inventory"].items():
        other = reference["inventory"][name]
        if item["modified_ns"] != other["modified_ns"]:
            raise ValueError(f"Engines disagree on exact modified_ns: {name}")
        if item["extension"] != other["extension"]:
            raise ValueError(f"Engines disagree on the common extension: {name}")
        left, right = item["created_ns"], other["created_ns"]
        if left is not None and right is not None:
            if left != right:
                raise ValueError(f"Engines disagree on exact created_ns: {name}")
            dates["created_ns_compared"] += 1
        elif (left is None) != (right is None):
            dates["created_ns_availability_differences"] += 1
        else:
            dates["created_ns_unavailable_both"] += 1
    return dates


def source_receipt(commands: dict[str, list[str]]) -> dict:
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    files = ["folder_organizer/scanner.py", "folder_organizer/matching.py", "folder_organizer/cli.py",
             "folder_organizer/reporting.py", "rust-app/src/core.rs", "rust-app/src/cli.rs",
             "rust-app/src/reporting.rs", "rust-app/Cargo.lock", "scripts/benchmark_editions_v070.py"]
    return {"git_commit": commit, "source_sha256": {name: sha256(ROOT / name) for name in files},
            "commands": commands,
            "executable_sha256": {engine: sha256(Path(command[0]).resolve())
                                  for engine, command in commands.items() if Path(command[0]).is_file()}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-command", type=json_command,
                        default=[sys.executable, "-m", "folder_organizer", "--cli"])
    parser.add_argument("--rust-command", type=json_command,
                        default=[str(ROOT / "rust-app/target/release" / ("FolderOrganizerRustCLI.exe" if os.name == "nt" else "FolderOrganizerRustCLI"))])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--small-files", type=int, default=1000)
    parser.add_argument("--large-files", type=int, default=8)
    parser.add_argument("--large-mib", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--require-windows", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.require_windows and os.name != "nt":
        parser.error("This delivery check requires a native Windows process")
    if args.repeats < 5 or args.small_files < 2 or args.large_files < 2 or args.large_mib < 1 or args.timeout < 1:
        parser.error("Use >=5 runs, >=2 files per distribution, >=1MiB large files, and a positive timeout")
    if args.small_files * 1024 + (args.large_files + 4) * args.large_mib * MIB > 100 * MIB:
        parser.error("Disposable fixture input must stay within the 100MiB benchmark budget")
    if args.output.exists():
        parser.error("Output already exists; benchmark evidence is never overwritten")
    commands = {"Python": args.python_command, "Rust": args.rust_command}
    receipt = source_receipt(commands)
    rows = []
    with tempfile.TemporaryDirectory(prefix="organizer-cross-engine-v070-") as temporary:
        scratch = Path(temporary)
        for distribution in ("small", "large"):
            fixture = create_fixture(scratch, distribution, args.small_files, args.large_files, args.large_mib)
            runs, reference = [], None
            for repeat in range(args.repeats):
                order = ("Python", "Rust") if repeat % 2 == 0 else ("Rust", "Python")
                for position, engine in enumerate(order):
                    report_path = scratch / f"{distribution}-{repeat}-{engine}.json"
                    command = commands[engine] + [str(fixture.root), "--duplicates", "--hidden",
                                                  "--exclude", "*.skip", "--json-common", str(report_path)]
                    measurement = measure(command, report_path, args.timeout)
                    report = normalize(json.loads(report_path.read_text(encoding="utf-8")), fixture.root)
                    date_comparison = check_result(report, fixture, reference)
                    if reference is None:
                        reference = report
                    runs.append({"engine": engine, "repeat": repeat + 1, "position": position + 1,
                                 **measurement, "duplicate_metrics": report["duplicate_metrics"],
                                 "timestamp_comparison": date_comparison,
                                 "app_version": report["app_version"], "schema_version": report["schema_version"]})
                    print(f"{distribution} {repeat + 1}/{args.repeats} {engine}: {measurement['wall_seconds']:.6f}s", flush=True)
            fixture.verify_unchanged()
            medians = {}
            for engine in commands:
                selected = [run for run in runs if run["engine"] == engine]
                numeric = ("wall_seconds", "cpu_seconds", "peak_private_commit_bytes", "peak_working_set_bytes",
                           "process_read_transfer_bytes", "process_write_transfer_bytes")
                medians[engine] = {key: statistics.median(run[key] for run in selected)
                                   for key in numeric if all(key in run for run in selected)}
            rows.append({"distribution": distribution, "inventory_files": len(fixture.expected),
                         "source_files_including_excluded": len(fixture.digests),
                         "inventory_bytes": sum(size for size, _kind in fixture.expected.values()),
                         "expected_exact_groups": fixture.exact_groups(), "fixture_manifest_sha256": fixture.manifest_hash(),
                         "source_bytes_unchanged": True, "runs": runs, "medians": medians})
    if source_receipt(commands) != receipt:
        raise ValueError("Source or executable changed during benchmark; discard mixed evidence")
    result = {"suite": "cross-engine-semantics-fresh-process-v070", "platform": platform.platform(),
              "native_windows": os.name == "nt", "python_driver": platform.python_version(),
              "logical_cpus": os.cpu_count(), "repeats_per_engine_per_distribution": args.repeats,
              "scope": "fresh CLI processes including startup, inventory, exact detection and JSON serialization; synthetic local files only",
              "limitations": ["OS page cache and background activity are uncontrolled; fixture generation and integrity hashes warm it",
                              "No cold-cache, physical disk, SSD, NAS, GUI responsiveness, or universal speed claim",
                              "Process I/O transfer bytes include runtime/report I/O and are not physical storage bytes",
                              "Memory values are maximum observed API high-water counters at 20ms polling; unavailable final counters are not invented",
                              "Windows peak private commit is PeakPagefileUsage; peak working set is a different metric",
                              "Algorithm sample/full-hash byte counters are requested reads, separate from OS process I/O",
                              "Dates use exact integer nanoseconds; creation time is compared only where both engines expose it, availability differences are counted"],
              "source": receipt, "results": rows}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    print(f"Cross-engine semantic contract passed; evidence written to {args.output}", flush=True)


if __name__ == "__main__":
    main()
