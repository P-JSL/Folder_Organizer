"""Conservative, bounded duplicate and similarity suggestions.

An exact duplicate is SHA-256 matched AND byte-compared. Similarity results are
review candidates only: each result describes a directly compared pair, never a
transitively inferred group. This module never moves or deletes files.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import math
import os
import re
import random
import secrets
import stat
import sys
import threading
import time
import warnings
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, Iterator

from .models import FileEntry, MatchGroup

CHUNK_SIZE = 1024 * 1024
SAMPLE_BYTES = 64 * 1024
SAMPLE_MIN_SIZE = 1024 * 1024
TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".css", ".js", ".ts", ".py", ".ini", ".log",
    ".rst", ".toml", ".sql", ".sh", ".c", ".cpp", ".h", ".java",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
AUTO_COMPLETE_FILE_LIMIT = 256
INDEX_POSTING_LIMIT = 32
INDEX_SKETCH_SIZE = 8
MAX_INDEX_KEYS = 65_536
NAME_INDEX_CACHE_ENTRIES = 128
NAME_SCORE_CACHE_ENTRIES = 4096
_DUPLICATE_PROOF_KEY = secrets.token_bytes(32)
_DUPLICATE_READS = threading.local()


def _snapshot_metadata(info):
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino,
            "snapshot_ctime_ns": info.st_ctime_ns}


def _current_snapshot(entry):
    """Read descriptor metadata only, preserving Windows ChangeTime semantics."""
    with _open_snapshot(entry) as stream:
        return _snapshot_metadata(os.fstat(stream.fileno()))


def _proof_json(proof):
    return json.dumps({key: value for key, value in proof.items() if key != "signature"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _seal_duplicate_proof(group, entries, snapshots):
    members = []
    for entry in entries:
        original = snapshots.get(entry.path)
        if original is None or original.get("sha256") != group.evidence["sha256"]:
            raise OSError("Original hash-read snapshot is unavailable; rescan first")
        current = _current_snapshot(entry)
        if any(current[key] != original[key] for key in current):
            raise OSError("File changed after its verified hash read; rescan first")
        members.append({"path": entry.path, **original})
    proof = {"version": 1, "group": group.to_dict(), "snapshots": members}
    proof["signature"] = hmac.new(_DUPLICATE_PROOF_KEY, _proof_json(proof), hashlib.sha256).hexdigest()
    # Private Python state is deliberately absent from MatchGroup.to_dict and
    # exported reports. Imported reports cannot become one-click authority.
    group._snapshot_proof = proof


def validate_duplicate_proof(group: MatchGroup) -> dict[str, dict]:
    """Authenticate an unchanged live exact result without reading file content.

    Proofs are process-local. Fresh metadata checks are performed by the plan
    builder; a missing/edited/imported result must be scanned again explicitly.
    """
    if not isinstance(group, MatchGroup):
        raise ValueError("현재 검사에서 확인한 중복 결과가 아닙니다. 중복 검사를 다시 실행하세요.")
    proof = getattr(group, "_snapshot_proof", None)
    try:
        valid = (isinstance(proof, dict) and proof.get("version") == 1
                 and isinstance(proof.get("signature"), str)
                 and hmac.compare_digest(proof["signature"], hmac.new(
                     _DUPLICATE_PROOF_KEY, _proof_json(proof), hashlib.sha256).hexdigest())
                 and proof.get("group") == group.to_dict()
                 and group.kind == "duplicate" and group.evidence.get("byte_identical") is True)
        members = proof["snapshots"] if valid else []
        if (not valid or not isinstance(members, list) or len(group.paths) < 2
                or [item["path"] for item in members] != group.paths
                or len(set(group.paths)) != len(group.paths)):
            raise ValueError("untrusted result")
        return {item["path"]: dict(item) for item in members}
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ValueError("중복 결과가 없거나 변경되었습니다. 중복 검사를 다시 실행하세요.") from exc


class _Cancelled(Exception):
    pass


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Cancelled


def _snapshot_matches(entry: FileEntry, info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode) and info.st_size == entry.size
        and info.st_mtime_ns == entry.mtime_ns
        and info.st_dev == entry.device and info.st_ino == entry.inode
    )


@contextmanager
def _open_snapshot(entry: FileEntry) -> Iterator[BinaryIO]:
    """Open a still-current regular file, rejecting final and parent symlinks.

    POSIX directory-relative opens avoid following a parent swapped for a link.
    Windows additionally checks all parents before opening and verifies fstat.
    """
    path = Path(entry.path)
    if not path.is_absolute():
        raise OSError("File snapshot path is not absolute")
    descriptor = None
    directory_fd = None
    try:
        if os.name == "posix" and os.open in os.supports_dir_fd:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            directory_fd = os.open(path.anchor, flags)
            for part in path.parts[1:-1]:
                next_fd = os.open(part, flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            descriptor = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        else:
            for candidate in (path, *path.parents):
                if candidate.is_symlink() or (
                    hasattr(candidate, "is_junction") and candidate.is_junction()
                ):
                    raise OSError("Symbolic links are not read")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
        if not _snapshot_matches(entry, os.fstat(descriptor)):
            raise OSError("File changed since the folder scan; rescan first")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            before = os.fstat(stream.fileno())
            yield stream
            after = os.fstat(stream.fileno())
            if (not _snapshot_matches(entry, after)
                    or before.st_ctime_ns != after.st_ctime_ns):
                raise OSError("File changed while being read; rescan first")
            if not _snapshot_matches(entry, os.stat(path, follow_symlinks=False)):
                raise OSError("File path changed while being read; rescan first")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory_fd is not None:
            os.close(directory_fd)


def _report(progress: Callable[[str], None] | None, message: str) -> None:
    if progress is not None:
        progress(message)


class _ProgressDetails:
    """Sample structured events before allocating their dictionaries.

    Small phases, phase transitions, the known last item and terminal events
    are always visible. Cancellation is still checked in every processing loop;
    it does not depend on this display/reporting frequency.
    """
    def __init__(self, callback):
        self.callback = callback
        self.phase = None
        self.completed = 0
        self.last_report = 0.0


def _sampled_progress(callback):
    if callback is None or isinstance(callback, _ProgressDetails):
        return callback
    return _ProgressDetails(callback)


def _detail(callback: Callable[[dict], None] | None, phase: str, completed: int,
            total: int | None, unit: str, message: str, *, terminal=False) -> None:
    if callback is not None:
        if isinstance(callback, _ProgressDetails):
            # Keep the gate in this existing call instead of adding a second
            # Python method invocation to every visited file or pair.
            if (not terminal and phase == callback.phase and completed > 10
                    and completed != total and completed - callback.completed < 100
                    and time.monotonic() - callback.last_report < .07):
                return
            callback.phase, callback.completed, callback.last_report = phase, completed, time.monotonic()
            callback = callback.callback
        callback({"phase": phase, "completed": completed, "total": total,
                  "unit": unit, "message": message, "terminal": terminal})


def _sample_digest(entry: FileEntry, cancel: Event | None) -> str:
    """Reject differing large files cheaply; samples never prove duplication."""
    digest = hashlib.sha256()
    offsets = (0, (entry.size - SAMPLE_BYTES) // 2, entry.size - SAMPLE_BYTES)
    with _open_snapshot(entry) as stream:
        for offset in offsets:
            _check_cancel(cancel)
            stream.seek(offset)
            digest.update(stream.read(SAMPLE_BYTES))
    _check_cancel(cancel)
    return digest.hexdigest()


class AnalysisCache:
    """Bounded in-memory text/image features; full duplicate hashes are not cached.

    Explicit session ownership avoids trusting a persistent cache after restart.
    Every lookup opens the current snapshot and includes its descriptor ctime,
    so even a same-size edit which restores mtime invalidates an older feature.
    """
    def __init__(self, max_bytes: int = 32 * 1024 * 1024, max_entries: int = 512):
        if type(max_bytes) is not int or type(max_entries) is not int or min(max_bytes, max_entries) < 1:
            raise ValueError("Cache limits must be positive integers")
        self.max_bytes, self.max_entries = max_bytes, max_entries
        self._values: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        self.hits = self.misses = 0

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._bytes = self.hits = self.misses = 0

    @property
    def bytes_used(self) -> int:
        return self._bytes

    @staticmethod
    def _weight(value) -> int:
        pending, seen, total = [value], set(), 0
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            total += sys.getsizeof(item)
            if isinstance(item, (tuple, frozenset)):
                pending.extend(item)
        return total

    def get(self, entry: FileEntry, namespace: str, loader: Callable, *, tokens: dict | None = None):
        with _open_snapshot(entry) as stream:
            token = os.fstat(stream.fileno()).st_ctime_ns
        key = (namespace, entry.path, entry.size, entry.mtime_ns, entry.device, entry.inode, token)
        with self._lock:
            if key in self._values:
                self.hits += 1
                self._values.move_to_end(key)
                if tokens is not None:
                    tokens[entry.path] = token
                return self._values[key][0]
            self.misses += 1
        value = loader(entry)
        # Do not label a newly changed file's feature with the old token.
        with _open_snapshot(entry) as stream:
            if os.fstat(stream.fileno()).st_ctime_ns != token:
                raise OSError("File changed while computing comparison features; rescan first")
        weight = self._weight(key) + self._weight(value) + 128
        if weight <= self.max_bytes:
            with self._lock:
                previous = self._values.pop(key, None)
                if previous is not None:
                    self._bytes -= previous[1]
                while self._values and (len(self._values) >= self.max_entries or self._bytes + weight > self.max_bytes):
                    _, (_, removed_weight) = self._values.popitem(last=False)
                    self._bytes -= removed_weight
                self._values[key] = value, weight
                self._bytes += weight
        if tokens is not None:
            tokens[entry.path] = token
        return value


def _digest(entry: FileEntry, cancel: Event | None) -> str:
    digest = hashlib.sha256()
    with _open_snapshot(entry) as stream:
        original = _snapshot_metadata(os.fstat(stream.fileno()))
        while chunk := stream.read(CHUNK_SIZE):
            _check_cancel(cancel)
            digest.update(chunk)
    _check_cancel(cancel)
    value = digest.hexdigest()
    sink = getattr(_DUPLICATE_READS, "sink", None)
    if sink is not None:
        sink[entry.path] = {**original, "sha256": value}
    return value


def _equal(first: FileEntry, second: FileEntry, cancel: Event | None) -> bool:
    equal = True
    with _open_snapshot(first) as left, _open_snapshot(second) as right:
        while True:
            _check_cancel(cancel)
            a, b = left.read(CHUNK_SIZE), right.read(CHUNK_SIZE)
            if a != b:
                equal = False
                break
            if not a:
                break
    return equal


def _ordered_files(files: list[FileEntry]) -> list[FileEntry]:
    """Avoid repeating O(n log n) ordering for an already ordered inventory."""
    previous = None
    for entry in files:
        key = entry.path.casefold()
        if previous is not None and key < previous:
            return sorted(files, key=lambda item: item.path.casefold())
        previous = key
    return files


def find_duplicates(
    files: list[FileEntry], cancel: Event | None = None,
    progress: Callable[[str], None] | None = None,
    *, progress_detail: Callable[[dict], None] | None = None,
) -> list[MatchGroup]:
    """Return exact duplicate groups; inaccessible or stale snapshots are skipped.

    Hardlink aliases are considered one physical file. Empty files can form a
    verified exact group but reclaim no bytes. No read error creates a digest.
    Cancellation returns an empty result, never an incomplete suggestion list.
    """
    groups: list[MatchGroup] = []
    progress_detail = _sampled_progress(progress_detail)
    hash_snapshots = {}
    previous_sink = getattr(_DUPLICATE_READS, "sink", None)
    _DUPLICATE_READS.sink = hash_snapshots
    try:
        _check_cancel(cancel)
        sizes: dict[int, list[FileEntry]] = defaultdict(list)
        seen_paths: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        _detail(progress_detail, "duplicate_index", 0, len(files), "files", "중복 검사 대상을 분류합니다.")
        for completed, entry in enumerate(_ordered_files(files), 1):
            _check_cancel(cancel)
            _detail(progress_detail, "duplicate_index", completed, len(files), "files", entry.relative_path)
            physical = (entry.device, entry.inode)
            if entry.path in seen_paths or (entry.inode and physical in seen_inodes):
                continue
            seen_paths.add(entry.path)
            if entry.inode:
                seen_inodes.add(physical)
            sizes[entry.size].append(entry)
        candidates_by_size = [entries for entries in sizes.values() if len(entries) > 1]
        sample_total = sum(len(entries) for entries in candidates_by_size if entries[0].size >= SAMPLE_MIN_SIZE)
        sample_completed = 0
        hash_batches: list[list[FileEntry]] = []
        _detail(progress_detail, "duplicate_sample", 0, sample_total, "files", "큰 파일의 일부를 비교해 다른 파일을 먼저 제외합니다.")
        for candidates in candidates_by_size:
            if candidates[0].size < SAMPLE_MIN_SIZE:
                hash_batches.append(candidates)
                continue
            samples: dict[str, list[FileEntry]] = defaultdict(list)
            for entry in candidates:
                _check_cancel(cancel)
                try:
                    _report(progress, f"Sampling {entry.relative_path}")
                    samples[_sample_digest(entry, cancel)].append(entry)
                except OSError as exc:
                    _report(progress, f"Skipped {entry.relative_path}: {exc}")
                sample_completed += 1
                _detail(progress_detail, "duplicate_sample", sample_completed, sample_total, "files", entry.relative_path)
            hash_batches.extend(entries for entries in samples.values() if len(entries) > 1)
        hash_total = sum(len(entries) for entries in hash_batches)
        hash_completed = verify_completed = 0
        _detail(progress_detail, "duplicate_hash", 0, hash_total, "files", "남은 파일의 전체 SHA-256을 계산합니다.")
        matching_hashes: list[tuple[str, list[FileEntry]]] = []
        for candidates in hash_batches:
            hashes: dict[str, list[FileEntry]] = defaultdict(list)
            for entry in candidates:
                _check_cancel(cancel)
                try:
                    _report(progress, f"Hashing {entry.relative_path}")
                    hashes[_digest(entry, cancel)].append(entry)
                except OSError as exc:
                    _report(progress, f"Skipped {entry.relative_path}: {exc}")
                hash_completed += 1
                _detail(progress_detail, "duplicate_hash", hash_completed, hash_total, "files", entry.relative_path)
            matching_hashes.extend((digest, matches) for digest, matches in hashes.items() if len(matches) > 1)
        verify_total = sum(len(matches) for _, matches in matching_hashes)
        _detail(progress_detail, "duplicate_verify", 0, verify_total, "files", "해시가 같은 파일의 실제 바이트를 재검증합니다.")
        for digest, matches in matching_hashes:
            # Partition by actual content as well as hash. This also makes
            # a fabricated/colliding digest unable to prove duplication.
            partitions: list[list[FileEntry]] = []
            for entry in matches:
                assigned = False
                try:
                    for bucket in partitions:
                        if _equal(bucket[0], entry, cancel):
                            bucket.append(entry)
                            assigned = True
                            break
                    if not assigned:
                        # Recheck even the first member after hashing.
                        with _open_snapshot(entry):
                            pass
                        partitions.append([entry])
                except OSError as exc:
                    _report(progress, f"Skipped {entry.relative_path}: {exc}")
                verify_completed += 1
                _detail(progress_detail, "duplicate_verify", verify_completed, verify_total, "files", entry.relative_path)
            for bucket in partitions:
                if len(bucket) > 1:
                    group = MatchGroup(
                        kind="duplicate", paths=[entry.path for entry in bucket],
                        reason=f"Same {bucket[0].size:,} bytes; SHA-256 and byte comparison verified ({digest[:12]}). Hardlink aliases excluded.",
                        score=1.0,
                        evidence={
                            "byte_identical": True,
                            "matched_signals": ["exact"],
                            "name_score": None, "text_score": None, "image_score": None,
                            "name_threshold": None, "text_threshold": None, "image_threshold": None,
                            "sha256": digest, "size_bytes": bucket[0].size,
                            "limitations": [
                                "파일 내용이 같다는 판정이며, 사용 중인 경로나 파일의 필요성을 판단하지 않습니다.",
                                "이름·수정일·생성일이 달라도 파일 내용이 모두 같으면 완전 중복입니다.",
                            ],
                        },
                    )
                    try:
                        _seal_duplicate_proof(group, bucket, hash_snapshots)
                    except OSError as exc:
                        _report(progress, f"Skipped verified group: {exc}")
                        continue
                    groups.append(group)
        _check_cancel(cancel)
        _detail(progress_detail, "duplicate_complete", len(files), len(files), "files", "중복 검사를 완료했습니다.", terminal=True)
    except _Cancelled:
        _detail(progress_detail, "duplicate_cancelled", 0, None, "files", "중복 검사를 중단했습니다.", terminal=True)
        return []
    finally:
        _DUPLICATE_READS.sink = previous_sink
    return groups


def _normal_name(entry: FileEntry) -> str:
    stem = Path(entry.name).stem.casefold()
    stem = re.sub(
        r"(?:[\s_-]+(?:copy|복사)(?:[\s_-]*\d+)?|\s*\((?:copy|복사)(?:\s+\d+)?\))(?=\s*(?:\(\d+\))?$)",
        " ", stem,
    )
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    return re.sub(r"[\W_]+", " ", stem, flags=re.UNICODE).strip()


class _NameComparisons:
    """Bounded, per-analysis reuse of pure filename comparisons.

    SequenceMatcher's right-hand index is independent of the left filename.
    Reusing that index and completed *ordered* scores keeps the original ratio
    exactly, including its direction-sensitive tie behavior. No file contents,
    metadata proof or delete authority is cached here.
    """
    def __init__(self):
        self.indexes = OrderedDict()
        self.scores = OrderedDict()
        self.identical_names = self.score_hits = self.score_misses = 0
        self.index_hits = self.index_builds = 0

    def ratio(self, left: str, right: str) -> float:
        # Truncation is the same bound used by the original comparison path.
        left, right = left[:1024], right[:1024]
        if left == right:
            self.identical_names += 1
            return 1.0
        key = left, right
        if key in self.scores:
            self.score_hits += 1
            self.scores.move_to_end(key)
            return self.scores[key]
        self.score_misses += 1
        matcher = self.indexes.get(right)
        if matcher is None:
            self.index_builds += 1
            matcher = SequenceMatcher(None, "", right, autojunk=False)
            if len(self.indexes) >= NAME_INDEX_CACHE_ENTRIES:
                self.indexes.popitem(last=False)
            self.indexes[right] = matcher
        else:
            self.index_hits += 1
            self.indexes.move_to_end(right)
        matcher.set_seq1(left)
        value = matcher.ratio()
        if len(self.scores) >= NAME_SCORE_CACHE_ENTRIES:
            self.scores.popitem(last=False)
        self.scores[key] = value
        return value

    def metrics(self) -> dict:
        return {"identical_names": self.identical_names, "score_hits": self.score_hits,
                "score_misses": self.score_misses, "sequence_index_hits": self.index_hits,
                "sequence_index_builds": self.index_builds, "score_entries": len(self.scores),
                "sequence_index_entries": len(self.indexes),
                "max_score_entries": NAME_SCORE_CACHE_ENTRIES,
                "max_sequence_index_entries": NAME_INDEX_CACHE_ENTRIES}


def _read_text(entry: FileEntry, limit: int) -> str | None:
    if entry.suffix not in TEXT_SUFFIXES or entry.size > limit:
        return None
    with _open_snapshot(entry) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    normalized = " ".join(text.split())
    return normalized if len(normalized) >= 40 else None


def _text_signature(entry: FileEntry, limit: int) -> tuple[frozenset[tuple[str, ...]], int] | None:
    text = _read_text(entry, limit)
    if text is None:
        return None
    words = text.split()[:4096]
    if len(words) < 3:
        return None
    # Shingle overlap is linear in this bounded signature size. Unlike a full
    # sequence alignment it cannot become quadratic on pathological text.
    shingles = frozenset(tuple(words[index:index + 3]) for index in range(len(words) - 2))
    return shingles, len(words)


def _cached(cache: OrderedDict, entry: FileEntry, loader: Callable):
    if entry.path in cache:
        cache.move_to_end(entry.path)
        return cache[entry.path]
    value = loader(entry)
    cache[entry.path] = value
    if len(cache) > 128:
        cache.popitem(last=False)
    return value


def _has_alpha(image) -> bool:
    return "A" in image.getbands() or "transparency" in image.info


def _visual_signature(image) -> tuple[int, tuple[int, ...], float, tuple[int, ...]]:
    """Keep RGB scoring stable and independently account for opacity changes."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    try:
        gray = rgb.convert("L").resize((9, 8)).tobytes()
        bits = 0
        for y in range(8):
            for x in range(8):
                bits = (bits << 1) | (gray[y * 9 + x] > gray[y * 9 + x + 1])
        pixels = tuple(rgb.resize((16, 16)).tobytes())
    finally:
        if rgb is not image:
            rgb.close()
    alpha = (255,) * 256
    if _has_alpha(image):
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        try:
            with rgba.getchannel("A") as channel:
                alpha = tuple(channel.resize((16, 16)).tobytes())
        finally:
            if rgba is not image:
                rgba.close()
    return bits, pixels, image.width / image.height, alpha


def _image_similarity(first, second) -> float | None:
    if abs(math.log(first[2] / second[2])) > .1:
        return None
    hash_score = 1 - (first[0] ^ second[0]).bit_count() / 64
    color_score = 1 - sum(abs(a - b) for a, b in zip(first[1], second[1])) / (255 * len(first[1]))
    alpha_score = 1 - sum(abs(a - b) for a, b in zip(first[3], second[3])) / (255 * len(first[3]))
    return min(hash_score, color_score, alpha_score)


def _image_signature(entry: FileEntry) -> tuple[int, tuple[int, ...], float, tuple[int, ...]] | None:
    if entry.suffix not in IMAGE_SUFFIXES:
        return None
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with _open_snapshot(entry) as stream, Image.open(stream) as source:
                if source.width * source.height > 40_000_000 or source.width <= 0 or source.height <= 0:
                    return None
                with ImageOps.exif_transpose(source) as image:
                    return _visual_signature(image)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def _sketch(values, count=INDEX_SKETCH_SIZE):
    """Small stable keys used only to retrieve candidates, never to prove equality."""
    hashes = {hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest() for value in values}
    return heapq.nsmallest(count, hashes)


def _candidate_keys(entry, name, enabled, features, max_text_bytes, tokens, notice):
    """Return a bounded set of approximate retrieval keys for one current file.

    Name/text sketches and image bands deliberately have imperfect recall. The
    full scoring and current-snapshot checks still decide each returned pair.
    """
    keys = []
    if "name" in enabled and name:
        short = name[:1024]
        keys.append(("name", short))
        grams = (short[index:index + 3] for index in range(max(1, len(short) - 2)))
        keys.extend(("name_gram", value) for value in _sketch(grams))
    try:
        if "text" in enabled and entry.suffix in TEXT_SUFFIXES and entry.size <= max_text_bytes:
            signature = features.get(entry, f"text:{max_text_bytes}",
                                     lambda item: _text_signature(item, max_text_bytes), tokens=tokens)
            if signature:
                keys.extend(("text", value) for value in _sketch("\0".join(words) for words in signature[0]))
        if "image" in enabled and entry.suffix in IMAGE_SUFFIXES:
            signature = features.get(entry, "image:1", _image_signature, tokens=tokens)
            if signature:
                bits, pixels, ratio, _alpha = signature
                # A high dHash score must share several unchanged byte bands.
                keys.extend(("image_hash", band, (bits >> (8 * band)) & 255) for band in range(8))
                means = tuple(sum(pixels[offset::3]) // 256 for offset in range(3))
                aspect = round(math.log(ratio) * 10)
                for shift in (0, 16):
                    color = tuple((value + shift) // 32 for value in means)
                    keys.extend(("image_color", shift, bucket, color) for bucket in (aspect - 1, aspect, aspect + 1))
    except (OSError, ValueError, SyntaxError) as exc:
        notice(f"Index content unavailable for {entry.relative_path}: {exc}", warning=True, skipped=True)
    return keys


def _indexed_pairs(buckets, names, enabled, features, max_text_bytes, max_pairs,
                   max_candidates_per_file, cancel, progress_detail, summary, notice):
    """Build bounded postings in one pass and sample the entire pair stream.

    Each lookup visits at most 32 previous entries, and each file emits at most
    K pairs. A deterministic reservoir retains at most max_pairs across *all*
    files. Thus late files are indexed even when the global score budget fills;
    the old lexicographic pair-prefix bias is not retained in fast mode.
    """
    reservoir = []
    randomizer = random.Random(0)
    total_files = sum(map(len, buckets.values()))
    completed = 0
    _detail(progress_detail, "similar_index", 0, total_files, "files", "전체 파일에서 빠른 비교 후보를 찾습니다.")
    for candidates in buckets.values():
        postings = OrderedDict()
        for index, entry in enumerate(candidates):
            _check_cancel(cancel)
            tokens = {}
            keys = _candidate_keys(entry, names[entry.path], enabled, features, max_text_bytes, tokens, notice)
            votes = defaultdict(int)
            for key in keys:
                _check_cancel(cancel)
                summary["candidate_lookups"] += 1
                previous = postings.get(key, ())
                if key in postings:
                    postings.move_to_end(key)
                for candidate in previous:
                    summary["index_postings_scanned"] += 1
                    votes[candidate] += 1
            # This sorting is over at most 31 * 32 entries (a fixed bound),
            # rather than over the complete file set or all possible pairs.
            chosen = heapq.nsmallest(max_candidates_per_file, votes,
                                    key=lambda candidate: (-votes[candidate], index - candidate))
            if len(votes) > len(chosen):
                summary["candidate_limit_reached"] = True
            for candidate in chosen:
                _check_cancel(cancel)
                summary["candidate_pairs"] += 1
                pair = (candidates[candidate], entry)
                if len(reservoir) < max_pairs:
                    reservoir.append(pair)
                else:
                    slot = randomizer.randrange(summary["candidate_pairs"])
                    if slot < max_pairs:
                        reservoir[slot] = pair
            for key in keys:
                if key not in postings:
                    if len(postings) >= MAX_INDEX_KEYS:
                        postings.popitem(last=False)
                        summary["index_evictions"] += 1
                        summary["candidate_limit_reached"] = True
                    postings[key] = []
                    summary["index_keys"] += 1
                elif len(postings[key]) == INDEX_POSTING_LIMIT:
                    del postings[key][0]
                    summary["candidate_limit_reached"] = True
                postings[key].append(index)
                postings.move_to_end(key)
            summary["index_keys_retained_peak"] = max(summary["index_keys_retained_peak"], len(postings))
            completed += 1
            summary["candidate_files_visited"] = completed
            _detail(progress_detail, "similar_index", completed, total_files, "files", entry.relative_path)
    summary["candidate_pairs_selected"] = len(reservoir)
    summary["limit_reached"] = summary["candidate_pairs"] > len(reservoir)
    # Stable scoring order improves bounded feature-cache locality. Sorting is
    # bounded by the explicit max_pairs budget, not quadratic file-pair growth.
    reservoir.sort(key=lambda pair: (pair[1].path.casefold(), pair[0].path.casefold()))
    return reservoir


def find_similar(
    files: list[FileEntry], threshold: float = 0.82,
    cancel: Event | None = None, progress: Callable[[str], None] | None = None,
    *, max_pairs: int = 25_000, max_text_bytes: int = 262_144,
    methods: tuple[str, ...] = ("name", "text", "image"),
    name_threshold: float | None = None, text_threshold: float | None = None,
    image_threshold: float | None = None,
    report: dict | None = None,
    progress_detail: Callable[[dict], None] | None = None,
    cache: AnalysisCache | None = None,
    search_mode: str = "auto", max_candidates_per_file: int = 32,
) -> list[MatchGroup]:
    """Suggest directly similar pairs by filename, small UTF-8 text or image.

    At most ``max_pairs`` same-extension pairs are compared. Auto mode checks
    all pairs for at most 256 files or when the whole pair set fits that budget;
    otherwise bounded indexes retrieve candidates across the entire inventory.
    Indexed mode is approximate and can miss similar files. Complete mode uses
    the legacy stable pair order and stops when the explicit budget is reached.
    UTF-8 text is limited to ``max_text_bytes`` and images to 40MP.
    A size/hash/byte verification pass excludes exact duplicates first. The
    pair budget bounds the following similarity stage, not this streaming exact
    pass. Hardlink aliases are represented once. Pair-limit and skipped-file
    notices go to ``progress``. Scores are independent method heuristics, not
    probabilities that a file is redundant or safe to remove.

    Explicit method thresholds are honored independently. For compatibility,
    omitted text/image thresholds default to max(threshold, .90), while the
    omitted name threshold defaults to threshold. Every result reports those
    effective thresholds and all available enabled metrics in ``evidence``.
    """
    def checked_threshold(value: float, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.5 <= value <= 1:
            raise ValueError(f"{label} threshold must be between 0.5 and 1.")
        return float(value)

    threshold = checked_threshold(threshold, "Similarity")
    cutoffs = {
        "name": checked_threshold(threshold if name_threshold is None else name_threshold, "Name"),
        "text": checked_threshold(max(threshold, .90) if text_threshold is None else text_threshold, "Text"),
        "image": checked_threshold(max(threshold, .90) if image_threshold is None else image_threshold, "Image"),
    }
    if isinstance(methods, str):
        raise ValueError("Similarity methods must be a nonempty collection of name, text and/or image.")
    try:
        enabled = set(methods)
    except TypeError as exc:
        raise ValueError("Similarity methods must be a nonempty collection of name, text and/or image.") from exc
    if not enabled or not enabled.issubset({"name", "text", "image"}):
        raise ValueError("Similarity methods must contain only name, text and/or image.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (max_pairs, max_text_bytes)):
        raise ValueError("Comparison limits must be positive.")
    if not isinstance(search_mode, str) or search_mode not in {"auto", "complete", "indexed"}:
        raise ValueError("Search mode must be auto, complete or indexed.")
    if type(max_candidates_per_file) is not int or not 1 <= max_candidates_per_file <= 128:
        raise ValueError("Candidates per file must be between 1 and 128.")
    summary = report if report is not None else {}
    summary.clear()
    summary.update({
        "methods": [method for method in ("name", "text", "image") if method in enabled],
        "thresholds": dict(cutoffs), "files_considered": 0,
        "compared_pairs": 0, "max_pairs": max_pairs, "exact_pairs_excluded": 0,
        "skipped_count": 0, "unsupported_files": 0, "limit_reached": False, "cancelled": False, "warnings": [],
        "search_mode": search_mode, "effective_search_mode": "complete", "exhaustive": True,
        "potential_pairs": 0, "candidate_pairs": 0, "candidate_pairs_selected": 0,
        "candidate_files_visited": 0, "candidate_lookups": 0, "index_postings_scanned": 0,
        "index_keys": 0, "index_keys_retained_peak": 0, "index_evictions": 0,
        "index_key_limit": MAX_INDEX_KEYS, "index_posting_limit": INDEX_POSTING_LIMIT,
        "max_candidates_per_file": max_candidates_per_file, "candidate_limit_reached": False,
    })
    warning_keys: set[str] = set()

    def notice(message: str, *, warning: bool = False, skipped: bool = False) -> None:
        if skipped:
            summary["skipped_count"] += 1
        if warning and len(warning_keys) < 100 and message not in warning_keys:
            warning_keys.add(message)
            summary["warnings"].append(message)
        _report(progress, message)

    def exact_progress(message: str) -> None:
        skipped = message.startswith("Skipped ")
        notice(message, warning=skipped, skipped=skipped)

    results: list[MatchGroup] = []
    progress_detail = _sampled_progress(progress_detail)
    name_comparisons = _NameComparisons()
    features = cache if cache is not None else AnalysisCache()
    initial_hits, initial_misses = features.hits, features.misses
    # Content-only profiles cannot match unrelated formats. Excluding these
    # metadata-proven ineligible files also avoids hashing or pairing them.
    eligible_files = files if "name" in enabled else [
        entry for entry in files
        if ("image" in enabled and entry.suffix in IMAGE_SUFFIXES)
        or ("text" in enabled and entry.suffix in TEXT_SUFFIXES and 40 <= entry.size <= max_text_bytes)
    ]
    summary["unsupported_files"] = len(files) - len(eligible_files)
    if summary["unsupported_files"]:
        notice(f"Content-only profile: {summary['unsupported_files']:,} files have no supported enabled comparison method and were not hashed or paired.", warning=True)
    try:
        _check_cancel(cancel)
        exact_groups = find_duplicates(eligible_files, cancel=cancel, progress=exact_progress,
                                       progress_detail=progress_detail)
        _check_cancel(cancel)
        exact_membership = {path: index for index, group in enumerate(exact_groups) for path in group.paths}
        if exact_groups:
            notice(f"Verified {len(exact_groups):,} exact duplicate groups; byte-identical pairs are excluded from similarity results.")
        buckets: dict[str, list[FileEntry]] = defaultdict(list)
        names: dict[str, str] = {}
        seen_paths: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        _detail(progress_detail, "similar_inventory", 0, len(eligible_files), "files", "유사 비교 대상을 확인합니다.")
        for completed, entry in enumerate(_ordered_files(eligible_files), 1):
            _check_cancel(cancel)
            _detail(progress_detail, "similar_inventory", completed, len(eligible_files), "files", entry.relative_path)
            if entry.path in seen_paths:
                continue
            seen_paths.add(entry.path)
            physical = (entry.device, entry.inode)
            if entry.inode and physical in seen_inodes:
                continue
            try:
                with _open_snapshot(entry):
                    pass
            except OSError as exc:
                notice(f"Skipped {entry.relative_path}: {exc}", warning=True, skipped=True)
                continue
            if entry.inode:
                seen_inodes.add(physical)
            key = "image" if entry.suffix in IMAGE_SUFFIXES else entry.suffix
            buckets[key].append(entry)
            names[entry.path] = _normal_name(entry)
            summary["files_considered"] += 1
        compared = 0
        pair_total = sum(len(candidates) * (len(candidates) - 1) // 2 for candidates in buckets.values())
        summary["potential_pairs"] = pair_total
        indexed = search_mode == "indexed" or (
            search_mode == "auto" and summary["files_considered"] > AUTO_COMPLETE_FILE_LIMIT and pair_total > max_pairs)
        if indexed:
            summary["effective_search_mode"] = "indexed"
            summary["exhaustive"] = False
            notice("빠른 후보 검색은 모든 쌍을 비교하지 않아 일부 유사 파일을 놓칠 수 있습니다. "
                   "전체 확인이 필요하면 범위를 좁히거나 전체 쌍 비교를 선택하세요.", warning=True)
            pair_source = _indexed_pairs(buckets, names, enabled, features, max_text_bytes, max_pairs,
                                         max_candidates_per_file, cancel, progress_detail, summary, notice)
            planned_pairs = len(pair_source)
            if summary["limit_reached"]:
                notice(f"Similarity comparison limit reached ({max_pairs:,} pairs); "
                       "candidates were sampled across the entire inventory, not only its first files.", warning=True)
        else:
            planned_pairs = min(pair_total, max_pairs)
            summary["candidate_pairs"] = pair_total
            summary["candidate_pairs_selected"] = planned_pairs
            pair_source = ((first, second) for candidates in buckets.values()
                           for index, first in enumerate(candidates) for second in candidates[index + 1:])
        _detail(progress_detail, "similar_pairs", 0, planned_pairs, "pairs", "이름·텍스트·이미지의 유사도를 비교합니다.")
        for first, second in pair_source:
            _check_cancel(cancel)
            if compared >= max_pairs:
                summary["limit_reached"] = True
                summary["exhaustive"] = False
                notice(f"Similarity comparison limit reached ({max_pairs:,} pairs); later pairs were not checked. Narrow the folder or increase max_pairs.", warning=True)
                _detail(progress_detail, "similar_limit", compared, planned_pairs, "pairs", "비교 한도에 도달했습니다. 나머지 쌍은 검사하지 않았습니다.", terminal=True)
                return results
            compared += 1
            summary["compared_pairs"] = compared
            _detail(progress_detail, "similar_pairs", compared, planned_pairs, "pairs", f"{first.relative_path} / {second.relative_path}")
            if first.inode and (first.device, first.inode) == (second.device, second.inode):
                continue
            notice(f"Comparing {first.relative_path} / {second.relative_path}")
            first_exact, second_exact = exact_membership.get(first.path), exact_membership.get(second.path)
            if first_exact is not None and first_exact == second_exact:
                summary["exact_pairs_excluded"] += 1
                continue
            metrics: dict[str, float | None] = {"name": None, "text": None, "image": None}
            content_tokens: dict[str, int] = {}
            limitations = [
                "각 점수는 해당 비교 방법의 유사도이며, 중복 확률이나 삭제해도 된다는 의미가 아닙니다.",
                "유사 후보는 두 파일을 직접 비교한 결과이며, 다른 후보와의 동일성을 추론하지 않습니다.",
            ]
            if indexed:
                limitations.append("빠른 후보 검색에서 직접 비교한 쌍입니다. 모든 파일 쌍을 검사하지 않아 다른 유사 파일을 놓칠 수 있습니다.")
            left, right = names[first.path], names[second.path]
            if "name" in enabled and left and right:
                # Limit adversarially long input while preserving the
                # full path separately in the result. Typical OS names
                # are much shorter than this bound.
                metrics["name"] = name_comparisons.ratio(left, right)
            try:
                if "text" in enabled and first.suffix in TEXT_SUFFIXES and second.suffix in TEXT_SUFFIXES:
                    a = features.get(first, f"text:{max_text_bytes}", lambda entry: _text_signature(entry, max_text_bytes), tokens=content_tokens)
                    b = features.get(second, f"text:{max_text_bytes}", lambda entry: _text_signature(entry, max_text_bytes), tokens=content_tokens)
                    if a and b:
                        metrics["text"] = min(
                            2 * len(a[0] & b[0]) / (len(a[0]) + len(b[0])),
                            min(a[1], b[1]) / max(a[1], b[1]),
                        )
                    else:
                        limitations.append(f"텍스트 비교 불가: UTF-8·최소 40문자/3단어·최대 {max_text_bytes:,}바이트 범위만 지원합니다.")
                        notice(f"Text content unsupported or outside limits: {first.relative_path} / {second.relative_path}", warning=True)
                elif "text" in enabled:
                    limitations.append("이 형식의 텍스트 내용은 비교하지 않았습니다.")
                if "image" in enabled and first.suffix in IMAGE_SUFFIXES and second.suffix in IMAGE_SUFFIXES:
                    limitations.append("이미지의 첫 프레임만 비교하며, GIF·WebP 등의 나머지 프레임과 움직임은 비교하지 않습니다.")
                    a = features.get(first, "image:1", _image_signature, tokens=content_tokens)
                    b = features.get(second, "image:1", _image_signature, tokens=content_tokens)
                    if a and b and (image_score := _image_similarity(a, b)) is not None:
                        metrics["image"] = image_score
                    else:
                        limitations.append("이미지 비교 불가: 해석 불가·40MP 초과·종횡비 차이 등의 조건을 확인하세요.")
                        notice(f"Image content unsupported or outside limits: {first.relative_path} / {second.relative_path}", warning=True)
                elif "image" in enabled:
                    limitations.append("이 형식의 이미지 내용은 비교하지 않았습니다.")
            except (OSError, ValueError, SyntaxError) as exc:
                # A meaningful name candidate may survive an unsupported
                # or corrupt content format; never invent its score.
                limitations.append(f"내용 비교를 완료하지 못했습니다: {type(exc).__name__}: {str(exc)[:160]}")
                notice(f"Content unavailable for {first.relative_path} / {second.relative_path}: {exc}", warning=True, skipped=True)
            matched = [method for method in ("name", "text", "image")
                       if method in enabled and metrics[method] is not None and metrics[method] >= cutoffs[method]]
            if not matched:
                continue
            # Content evidence takes priority over filename evidence.
            # A normalized filename score of 1 must never hide a useful
            # text/image score or imply identical file contents.
            primary = "image" if "image" in matched else "text" if "text" in matched else "name"
            kind = "similar_" + primary
            score = float(metrics[primary])
            if primary == "image":
                reason = "Similar image structure, colors, opacity and aspect ratio; resized 16×16 comparison, not semantic recognition."
                limitations.append("이미지는 EXIF 방향 보정 후 구조·색·투명도·종횡비를 비교하며, 의미 인식·모든 회전/자르기/연사 판별은 지원하지 않습니다.")
            elif primary == "text":
                reason = f"Similar UTF-8 text by 3-word shingle overlap (first 4,096 words; files up to {max_text_bytes:,} bytes); review both files."
                limitations.append("텍스트 점수는 공백 정규화 후 앞 4,096단어의 3단어 어구 겹침과 길이 비율을 비교합니다. 문서 의미나 모든 변경을 보장하지 않습니다.")
            else:
                reason = "Similar normalized filenames; content equivalence is NOT established."
                limitations.append("이름 유사만으로 내용·버전·설치 프로그램의 대체 가능성을 판단할 수 없습니다.")
            for method, label in (("text", "텍스트"), ("image", "이미지")):
                if method not in enabled:
                    limitations.append(f"{label} 내용 비교를 선택하지 않았습니다.")
                elif metrics[method] is not None and metrics[method] < cutoffs[method]:
                    limitations.append(f"{label} 유사도 {metrics[method]:.1%}는 선택한 기준 {cutoffs[method]:.1%} 미만입니다.")
            byte_identical: bool | None = False if first.size != second.size else None
            if byte_identical is None:
                try:
                    if _equal(first, second, cancel):
                        summary["exact_pairs_excluded"] += 1
                        notice(f"Byte-identical pair excluded from similarity: {first.relative_path} / {second.relative_path}")
                        continue
                    byte_identical = False
                except OSError as exc:
                    limitations.append("바이트 비교를 완료하지 못해 완전 중복 여부는 확인되지 않았습니다.")
                    notice(f"Byte comparison unavailable for {first.relative_path} / {second.relative_path}: {exc}", warning=True, skipped=True)
            try:
                # Snapshots may have changed while cached content was
                # compared, so validate both again before suggesting.
                for entry in (first, second):
                    with _open_snapshot(entry) as stream:
                        if (entry.path in content_tokens
                                and os.fstat(stream.fileno()).st_ctime_ns != content_tokens[entry.path]):
                            raise OSError("File changed after its comparison features were read; rescan first")
            except (OSError, ValueError, SyntaxError) as exc:
                notice(f"Skipped pair {first.relative_path} / {second.relative_path}: {exc}", warning=True, skipped=True)
                continue
            results.append(MatchGroup(
                kind=kind, paths=[first.path, second.path], reason=reason, score=score,
                evidence={"byte_identical": byte_identical, "matched_signals": matched,
                          **{method + "_score": metrics[method] for method in ("name", "text", "image")},
                          **{method + "_threshold": cutoffs[method] for method in ("name", "text", "image")},
                          "limitations": limitations},
            ))
        _check_cancel(cancel)
        _detail(progress_detail, "similar_complete", compared, planned_pairs, "pairs", "유사 비교를 완료했습니다.", terminal=True)
    except _Cancelled:
        summary["cancelled"] = True
        summary["exhaustive"] = False
        _detail(progress_detail, "similar_cancelled", summary["compared_pairs"], None, "pairs", "유사 비교를 중단했습니다.", terminal=True)
        return []
    finally:
        summary["name_comparison_cache"] = name_comparisons.metrics()
        if summary["skipped_count"]:
            summary["exhaustive"] = False
        summary["feature_cache"] = {"hits": features.hits - initial_hits, "misses": features.misses - initial_misses,
                                    "bytes_used": features.bytes_used, "max_bytes": features.max_bytes}
    return results
