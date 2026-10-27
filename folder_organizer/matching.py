"""Conservative, bounded duplicate and similarity suggestions.

An exact duplicate is SHA-256 matched AND byte-compared. Similarity results are
review candidates only: each result describes a directly compared pair, never a
transitively inferred group. This module never moves or deletes files.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import warnings
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from threading import Event
from typing import BinaryIO, Callable, Iterator

from .models import FileEntry, MatchGroup

CHUNK_SIZE = 1024 * 1024
TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".css", ".js", ".ts", ".py", ".ini", ".log",
    ".rst", ".toml", ".sql", ".sh", ".c", ".cpp", ".h", ".java",
}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"}


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
            yield stream
            if not _snapshot_matches(entry, os.fstat(stream.fileno())):
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


def _digest(entry: FileEntry, cancel: Event | None) -> str:
    digest = hashlib.sha256()
    with _open_snapshot(entry) as stream:
        while chunk := stream.read(CHUNK_SIZE):
            _check_cancel(cancel)
            digest.update(chunk)
    _check_cancel(cancel)
    return digest.hexdigest()


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


def find_duplicates(
    files: list[FileEntry], cancel: Event | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[MatchGroup]:
    """Return exact duplicate groups; inaccessible or stale snapshots are skipped.

    Hardlink aliases are considered one physical file. Empty files can form a
    verified exact group but reclaim no bytes. No read error creates a digest.
    Cancellation returns an empty result, never an incomplete suggestion list.
    """
    groups: list[MatchGroup] = []
    try:
        _check_cancel(cancel)
        sizes: dict[int, list[FileEntry]] = defaultdict(list)
        seen_paths: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        for entry in sorted(files, key=lambda item: item.path.casefold()):
            _check_cancel(cancel)
            physical = (entry.device, entry.inode)
            if entry.path in seen_paths or (entry.inode and physical in seen_inodes):
                continue
            seen_paths.add(entry.path)
            if entry.inode:
                seen_inodes.add(physical)
            sizes[entry.size].append(entry)
        for candidates in sizes.values():
            if len(candidates) < 2:
                continue
            hashes: dict[str, list[FileEntry]] = defaultdict(list)
            for entry in candidates:
                _check_cancel(cancel)
                try:
                    _report(progress, f"Hashing {entry.relative_path}")
                    hashes[_digest(entry, cancel)].append(entry)
                except OSError as exc:
                    _report(progress, f"Skipped {entry.relative_path}: {exc}")
            for digest, matches in hashes.items():
                if len(matches) < 2:
                    continue
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
                for bucket in partitions:
                    if len(bucket) > 1:
                        groups.append(MatchGroup(
                            kind="duplicate", paths=[entry.path for entry in bucket],
                            reason=f"Same {bucket[0].size:,} bytes; SHA-256 and byte comparison verified ({digest[:12]}). Hardlink aliases excluded.",
                            score=1.0,
                        ))
    except _Cancelled:
        return []
    return groups


def _normal_name(entry: FileEntry) -> str:
    stem = Path(entry.name).stem.casefold()
    stem = re.sub(
        r"(?:[\s_-]+(?:copy|복사)(?:[\s_-]*\d+)?|\s*\((?:copy|복사)(?:\s+\d+)?\))(?=\s*(?:\(\d+\))?$)",
        " ", stem,
    )
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    return re.sub(r"[\W_]+", " ", stem, flags=re.UNICODE).strip()


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


def _image_signature(entry: FileEntry) -> tuple[int, tuple[int, ...], float] | None:
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
                image = ImageOps.exif_transpose(source).convert("RGB")
                gray = image.convert("L").resize((9, 8)).tobytes()
                bits = 0
                for y in range(8):
                    for x in range(8):
                        bits = (bits << 1) | (gray[y * 9 + x] > gray[y * 9 + x + 1])
                pixels = tuple(image.resize((16, 16)).tobytes())
                return bits, pixels, image.width / image.height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None


def find_similar(
    files: list[FileEntry], threshold: float = 0.82,
    cancel: Event | None = None, progress: Callable[[str], None] | None = None,
    *, max_pairs: int = 25_000, max_text_bytes: int = 262_144,
) -> list[MatchGroup]:
    """Suggest directly similar pairs by filename, small UTF-8 text or image.

    At most ``max_pairs`` same-extension pairs are compared, in stable path
    order. UTF-8 text is limited to ``max_text_bytes`` and images to 40MP.
    Pair-limit and skipped-file notices go to ``progress``. Name suggestions
    do not prove matching content; no suggestion is safe to delete blindly.
    """
    if not 0.5 <= threshold <= 1:
        raise ValueError("Similarity threshold must be between 0.5 and 1.")
    if max_pairs < 1 or max_text_bytes < 1:
        raise ValueError("Comparison limits must be positive.")
    results: list[MatchGroup] = []
    try:
        _check_cancel(cancel)
        buckets: dict[str, list[FileEntry]] = defaultdict(list)
        names: dict[str, str] = {}
        # Large folders must not retain every decoded file or image. A small
        # LRU is sufficient for nested pair comparisons and bounds memory.
        texts: OrderedDict = OrderedDict()
        images: OrderedDict = OrderedDict()
        seen_paths: set[str] = set()
        for entry in sorted(files, key=lambda item: item.path.casefold()):
            _check_cancel(cancel)
            if entry.path in seen_paths:
                continue
            seen_paths.add(entry.path)
            try:
                with _open_snapshot(entry):
                    pass
            except OSError as exc:
                _report(progress, f"Skipped {entry.relative_path}: {exc}")
                continue
            key = "image" if entry.suffix in IMAGE_SUFFIXES else entry.suffix
            buckets[key].append(entry)
            names[entry.path] = _normal_name(entry)
        compared = 0
        for candidates in buckets.values():
            for index, first in enumerate(candidates):
                for second in candidates[index + 1:]:
                    _check_cancel(cancel)
                    if compared >= max_pairs:
                        _report(progress, f"Similarity comparison limit reached ({max_pairs:,} pairs); later pairs were not checked. Narrow the folder or increase max_pairs.")
                        return results
                    compared += 1
                    if first.inode and (first.device, first.inode) == (second.device, second.inode):
                        continue
                    _report(progress, f"Comparing {first.relative_path} / {second.relative_path}")
                    kind, score, reason = "", 0.0, ""
                    left, right = names[first.path], names[second.path]
                    if left and right:
                        name_score = SequenceMatcher(None, left, right, autojunk=False).ratio()
                        if name_score >= threshold:
                            kind, score = "similar_name", name_score
                            reason = "Similar normalized filenames; content equivalence is NOT established."
                    try:
                        if first.suffix in TEXT_SUFFIXES and second.suffix in TEXT_SUFFIXES:
                            a = _cached(texts, first, lambda entry: _text_signature(entry, max_text_bytes))
                            b = _cached(texts, second, lambda entry: _text_signature(entry, max_text_bytes))
                            if a and b:
                                text_score = min(
                                    2 * len(a[0] & b[0]) / (len(a[0]) + len(b[0])),
                                    min(a[1], b[1]) / max(a[1], b[1]),
                                )
                                if text_score >= max(threshold, .90) and text_score >= score:
                                    kind, score = "similar_text", text_score
                                    reason = f"Similar UTF-8 text by 3-word shingle overlap (first 4,096 words; files up to {max_text_bytes:,} bytes); review both files."
                        if first.suffix in IMAGE_SUFFIXES and second.suffix in IMAGE_SUFFIXES:
                            a = _cached(images, first, _image_signature)
                            b = _cached(images, second, _image_signature)
                            if a and b and abs(math.log(a[2] / b[2])) <= .1:
                                hash_score = 1 - (a[0] ^ b[0]).bit_count() / 64
                                # Pixel colors reject the all-zero dHash false
                                # positive for solid images of different colors.
                                color_score = 1 - sum(abs(x - y) for x, y in zip(a[1], b[1])) / (255 * len(a[1]))
                                image_score = min(hash_score, color_score)
                                if image_score >= max(threshold, .90) and image_score >= score:
                                    kind, score = "similar_image", image_score
                                    reason = "Similar image structure, colors and aspect ratio; resized 16×16 comparison, not semantic recognition."
                        # Snapshots may have changed while cached content was
                        # compared, so validate both again before suggesting.
                        for entry in (first, second):
                            with _open_snapshot(entry):
                                pass
                    except (OSError, ValueError, SyntaxError) as exc:
                        _report(progress, f"Skipped pair {first.relative_path} / {second.relative_path}: {exc}")
                        continue
                    if kind:
                        results.append(MatchGroup(kind=kind, paths=[first.path, second.path], reason=reason, score=score))
    except _Cancelled:
        return []
    return results
