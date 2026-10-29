"""Lossless, content-addressed recovery bytes, independent of live originals.

Only private recovery copies are candidates for compaction. This module never
removes a source: callers must durably journal a verified descriptor before
retiring their private raw copy. Shared objects are immutable and never garbage
collected here. A signed descriptor, not a filename or a hash alone, authorizes
which chunks form a file.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import zlib

CHUNK_SIZE = 1024 * 1024
MAX_CHUNKS = 65_536
MAX_FILE_BYTES = CHUNK_SIZE * MAX_CHUNKS
MAX_STORED_CHUNK = CHUNK_SIZE + 1024
FORMAT = "chunked-zlib-v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS = os.name == "nt"


class PayloadError(ValueError):
    """Recovery bytes are unsafe, changed, corrupt or outside supported bounds."""


class PayloadNotBeneficial(PayloadError):
    """The requested saving was not achieved; keep the existing raw copy."""


class PayloadMetadataUnsupported(PayloadNotBeneficial):
    """Primary-byte chunks cannot replace the original's Windows properties."""


class PayloadLimitError(PayloadError):
    """The size, cancellation or encoding-time budget requires raw fallback."""


def _windows_compaction_metadata(path: Path) -> tuple[int, tuple[str, ...]]:
    """Read attributes and the complete bounded stream list; never change data.

    Stream enumeration support varies on remote/file-system providers. Unknown,
    denied or incomplete enumeration is an error rather than evidence that no
    alternate streams exist.
    """
    import ctypes
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    attributes = kernel.GetFileAttributesW
    attributes.argtypes, attributes.restype = [wintypes.LPCWSTR], wintypes.DWORD
    first = kernel.FindFirstStreamW
    first.argtypes, first.restype = [wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(StreamData), wintypes.DWORD], wintypes.HANDLE
    following = kernel.FindNextStreamW
    following.argtypes, following.restype = [wintypes.HANDLE, ctypes.POINTER(StreamData)], wintypes.BOOL
    close = kernel.FindClose
    close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
    value = attributes(str(path))
    if value == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if value & 0x4000:  # FILE_ATTRIBUTE_ENCRYPTED: plaintext packing drops EFS.
        return value, ()
    info = StreamData()
    handle = first(str(path), 0, ctypes.byref(info), 0)  # FindStreamInfoStandard
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error == 38:  # ERROR_HANDLE_EOF: supported, complete empty listing.
            return value, ()
        raise ctypes.WinError(error)
    names = []
    try:
        while True:
            names.append(info.name)
            if len(names) > 128:
                raise OSError("데이터 스트림 수가 안전한 확인 한도를 넘었습니다.")
            if not following(handle, ctypes.byref(info)):
                error = ctypes.get_last_error()
                if error != 38:
                    raise ctypes.WinError(error)
                break
    finally:
        close(handle)
    return value, tuple(names)


def compaction_preflight(path: Path) -> None:
    """Keep raw Windows files when their metadata cannot fit this byte format.

    The current chunk format carries primary bytes, not EFS encryption or named
    streams. Deletion/undo still moves the original file; only optional data
    compaction must refuse to retire these unrepresented properties.
    """
    if not _WINDOWS:
        return
    try:
        attributes, streams = _windows_compaction_metadata(Path(path))
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise PayloadMetadataUnsupported("Windows 데이터 스트림을 완전히 확인할 수 없어 기존 보관본을 유지합니다.") from exc
    if attributes & 0x4000:
        raise PayloadMetadataUnsupported("EFS 암호화 파일의 속성을 보존하기 위해 기존 보관본을 유지합니다.")
    if any(name != "::$DATA" for name in streams):
        raise PayloadMetadataUnsupported("대체 데이터 스트림(ADS)을 보존하기 위해 기존 보관본을 유지합니다.")


def _ops():
    # Operations imports this module only at call sites, avoiding import cycles.
    from . import operations
    return operations


def _check(cancel=None, started=None, budget=None):
    if cancel is not None and cancel.is_set():
        raise PayloadLimitError("복원 데이터 최적화를 중단했습니다. 기존 보관본은 유지합니다.")
    if budget is not None and started is not None and time.monotonic() - started > budget:
        raise PayloadLimitError("압축 처리 시간 한도를 넘었습니다. 기존 보관본 방식으로 유지합니다.")


def _report(callback, completed, total, message):
    if callback:
        callback({"phase": "compact", "completed": completed, "total": total,
                  "unit": "bytes", "message": message})


def _read_input_chunk(descriptor, cancel, started, budget):
    parts, count = [], 0
    while count < CHUNK_SIZE:
        _check(cancel, started, budget)
        part = os.read(descriptor, CHUNK_SIZE - count)
        if not part:
            break
        count += len(part)
        parts.append(part)
    return b"".join(parts)


def _object_root(root: Path, *, create=False) -> Path:
    ops = _ops()
    directory = root / ops.STATE_DIRECTORY / "recovery-objects" / "v1"
    if create:
        ops._mkdir(directory)
    else:
        ops._safe_path(directory, require=True)
    return directory


def _object_path(root: Path, chunk: dict, *, create=False) -> Path:
    directory = _object_root(root, create=create) / chunk["sha256"][:2]
    if create:
        _ops()._mkdir(directory)
    else:
        _ops()._safe_path(directory, require=True)
    return directory / (chunk["sha256"] + "." + chunk["codec"])


def _metadata(info):
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns}


def _same_open_file(path: Path, info) -> None:
    """Check pathname identity without mixing Windows stat/fstat change times."""
    ops = _ops()
    ops._safe_path(path, require=True)
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or any(
            getattr(info, field) != getattr(current, field)
            for field in ("st_size", "st_mtime_ns", "st_dev", "st_ino")):
        raise PayloadError("복원 데이터의 파일 경로가 읽는 동안 변경되었습니다.")


def _read_object(root: Path, chunk: dict) -> bytes:
    ops = _ops()
    path = _object_path(root, chunk)
    ops._safe_path(path, require=True)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise PayloadError("복원 청크가 일반 파일이 아닙니다.")
    flags = os.O_RDONLY | ops._NOFOLLOW | ops._BINARY | getattr(os, "O_NONBLOCK", 0)
    with ops._directory_descriptor(path.parent) as parent:
        descriptor = os.open(path.name if parent is not None else path, flags,
                             **({"dir_fd": parent} if parent is not None else {}))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size != chunk["stored_size"]:
            raise PayloadError("복원 청크의 저장 크기가 일치하지 않습니다.")
        data = bytearray()
        while len(data) <= MAX_STORED_CHUNK:
            part = os.read(descriptor, min(64 * 1024, MAX_STORED_CHUNK + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(descriptor)
        if _metadata(before) != _metadata(after):
            raise PayloadError("읽는 동안 복원 청크가 변경되었습니다.")
        _same_open_file(path, after)
    finally:
        os.close(descriptor)
    stored = bytes(data)
    if len(stored) != chunk["stored_size"] or hashlib.sha256(stored).hexdigest() != chunk["stored_sha256"]:
        raise PayloadError("복원 청크의 저장 무결성 검증에 실패했습니다.")
    if chunk["codec"] == "raw":
        result = stored
    else:
        try:
            decoder = zlib.decompressobj()
            result = decoder.decompress(stored, chunk["size"] + 1)
            if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                raise PayloadError("복원 청크가 손상되었거나 압축 해제 제한을 넘었습니다.")
        except zlib.error as exc:
            raise PayloadError("복원 청크의 압축을 해제하지 못했습니다.") from exc
    if len(result) != chunk["size"] or hashlib.sha256(result).hexdigest() != chunk["sha256"]:
        raise PayloadError("복원 청크의 원본 바이트 검증에 실패했습니다.")
    return result


def _validate(root, descriptor):
    ops = _ops()
    directory = ops._root(root)
    required = {"version", "format", "root", "size", "sha256", "mtime_ns", "mode",
                "chunk_size", "chunks", "stats", "signature"}
    if not isinstance(descriptor, dict) or set(descriptor) != required:
        raise PayloadError("복원 데이터 설명 형식이 올바르지 않습니다.")
    if (descriptor["version"] != 1 or descriptor["format"] != FORMAT
            or descriptor["root"] != str(directory) or descriptor["chunk_size"] != CHUNK_SIZE):
        raise PayloadError("복원 데이터의 폴더 또는 버전이 일치하지 않습니다.")
    if (type(descriptor["size"]) is not int or not 0 <= descriptor["size"] <= MAX_FILE_BYTES
            or not isinstance(descriptor["sha256"], str) or not _HASH.fullmatch(descriptor["sha256"])
            or type(descriptor["mtime_ns"]) is not int or descriptor["mtime_ns"] < 0
            or type(descriptor["mode"]) is not int or not 0 <= descriptor["mode"] <= 0o777):
        raise PayloadError("복원 데이터의 크기·해시·날짜가 올바르지 않습니다.")
    chunks = descriptor["chunks"]
    if not isinstance(chunks, list) or len(chunks) > MAX_CHUNKS:
        raise PayloadLimitError("복원 청크 목록의 안전 한도를 넘었습니다.")
    if not isinstance(descriptor["stats"], dict) or len(descriptor["stats"]) > 20 or any(
            not isinstance(key, str) or type(value) not in {int, float}
            for key, value in descriptor["stats"].items()):
        raise PayloadError("복원 데이터 통계 형식이 올바르지 않습니다.")
    total = 0
    keys = {"sha256", "size", "codec", "stored_size", "stored_sha256"}
    for index, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or set(chunk) != keys:
            raise PayloadError("복원 청크 설명이 올바르지 않습니다.")
        if (not isinstance(chunk["sha256"], str) or not _HASH.fullmatch(chunk["sha256"])
                or not isinstance(chunk["stored_sha256"], str) or not _HASH.fullmatch(chunk["stored_sha256"])
                or chunk["codec"] not in {"raw", "zlib"}
                or type(chunk["size"]) is not int or not 1 <= chunk["size"] <= CHUNK_SIZE
                or index < len(chunks) - 1 and chunk["size"] != CHUNK_SIZE
                or type(chunk["stored_size"]) is not int or not 1 <= chunk["stored_size"] <= MAX_STORED_CHUNK
                or chunk["codec"] == "raw" and chunk["stored_size"] != chunk["size"]):
            raise PayloadError("복원 청크의 크기·해시·방식이 올바르지 않습니다.")
        total += chunk["size"]
    if total != descriptor["size"]:
        raise PayloadError("복원 청크의 합계가 원본 크기와 다릅니다.")
    ops._check_signature(descriptor, ops._key(directory / ops.STATE_DIRECTORY))
    return directory


def _write_object(root: Path, chunk: dict, data: bytes) -> bool:
    """Return whether this call created an object; never replace another one."""
    ops = _ops()
    path = _object_path(root, chunk, create=True)
    ops._safe_path(path)
    try:
        with ops._directory_descriptor(path.parent) as parent:
            descriptor = os.open(path.name if parent is not None else path,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | ops._NOFOLLOW | ops._BINARY,
                                 0o600, **({"dir_fd": parent} if parent is not None else {}))
    except FileExistsError:
        _read_object(root, chunk)
        return False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        ops._sync_directory(path.parent)
        _read_object(root, chunk)
    except BaseException:
        # Keep even an incomplete owned object for diagnosis. It cannot be
        # accepted on a later read because both its length and hashes are checked.
        raise
    return True


def pack_file(root, path, expected, *, progress_detail=None, cancel=None,
              time_budget=15.0, min_savings_ratio=0.0, on_prepared=None) -> dict:
    """Read one unchanged raw recovery file; create a signed lossless recipe.

    Encoding uses one bounded chunk at a time. New encoded objects are staged in
    an owned temporary spool until the complete source has been verified, so an
    encoding timeout, changed source or unhelpful compression leaves no published
    partial payload. Time limits cover encoding; durable storage can take longer
    on a slow disk. Existing raw bytes are always left untouched by this API.
    An optional callback can durably record the authenticated planned recipe
    before staged objects become public. Callback failure publishes no objects.
    """
    ops = _ops()
    directory, source = ops._root(root), ops._absolute(path)
    if type(time_budget) not in {int, float, type(None)} or time_budget is not None and not 0 < time_budget <= 3600:
        raise PayloadError("압축 시간 한도가 올바르지 않습니다.")
    if type(min_savings_ratio) not in {int, float} or not 0 <= min_savings_ratio < 1:
        raise PayloadError("압축 절감 기준이 올바르지 않습니다.")
    ops._safe_path(source, require=True)
    if not stat.S_ISREG(source.lstat().st_mode):
        raise PayloadError("복원용 일반 파일만 압축할 수 있습니다.")
    if not isinstance(expected, dict) or any(key not in expected for key in ("size", "mtime_ns", "device", "inode", "sha256")):
        raise PayloadError("원본 검증 정보가 올바르지 않습니다.")
    if type(expected["size"]) is not int or not 0 <= expected["size"] <= MAX_FILE_BYTES:
        raise PayloadLimitError("파일당 64 GiB까지 복원 데이터를 최적화할 수 있습니다.")
    compaction_preflight(source)
    state = ops._state(directory)
    key = ops._key(state)
    started, chunks, positions, unique = time.monotonic(), [], {}, {}
    digest, processed, reused = hashlib.sha256(), 0, 0
    flags = os.O_RDONLY | ops._NOFOLLOW | ops._BINARY | getattr(os, "O_NONBLOCK", 0)
    source_fd = os.open(source, flags)
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or not ops._same(before, expected):
            raise PayloadError("압축 전에 복원용 원본이 변경되었습니다.")
        with tempfile.TemporaryFile(mode="w+b", dir=state) as spool:
            _report(progress_detail, 0, before.st_size, "복원 데이터를 압축·중복 제거하고 있습니다.")
            while True:
                _check(cancel, started, time_budget)
                raw = _read_input_chunk(source_fd, cancel, started, time_budget)
                if not raw:
                    break
                processed += len(raw)
                if processed > MAX_FILE_BYTES or len(chunks) >= MAX_CHUNKS:
                    raise PayloadLimitError("복원 데이터의 크기 한도를 넘었습니다.")
                digest.update(raw)
                raw_hash = hashlib.sha256(raw).hexdigest()
                if raw_hash not in unique:
                    compressed = zlib.compress(raw, level=1)
                    stored, codec = (compressed, "zlib") if len(compressed) < len(raw) else (raw, "raw")
                    chunk = {"sha256": raw_hash, "size": len(raw), "codec": codec,
                             "stored_size": len(stored), "stored_sha256": hashlib.sha256(stored).hexdigest()}
                    object_path = _object_path(directory, chunk, create=True)
                    if object_path.exists() or object_path.is_symlink():
                        _read_object(directory, chunk)
                        positions[raw_hash] = None
                    else:
                        positions[raw_hash] = spool.tell()
                        spool.write(stored)
                    unique[raw_hash] = chunk
                chunk = unique[raw_hash]
                chunks.append(dict(chunk))
                if positions[raw_hash] is None:
                    reused += len(raw)
                _report(progress_detail, processed, before.st_size, "복원 데이터 검증 중")
            after = os.fstat(source_fd)
            if (_metadata(before) != _metadata(after) or processed != expected["size"]
                    or digest.hexdigest() != expected["sha256"]):
                raise PayloadError("압축 중 복원용 원본이 변경되었습니다. 기존 보관본은 유지합니다.")
            _same_open_file(source, after)
            _check(cancel, started, time_budget)
            # A stream/encryption property can appear after the initial probe.
            # Refuse before publishing any of this file's encoded objects.
            compaction_preflight(source)
            stored_total = sum(chunk["stored_size"] for chunk in unique.values())
            new_estimate = sum(chunk["stored_size"] for hash_, chunk in unique.items() if positions[hash_] is not None)
            if processed and min_savings_ratio and new_estimate > processed * (1 - min_savings_ratio):
                raise PayloadNotBeneficial("압축·중복 제거 효과가 작아 기존 보관본을 유지합니다.")
            descriptor = {"version": 1, "format": FORMAT, "root": str(directory),
                          "size": processed, "sha256": digest.hexdigest(),
                          "mtime_ns": before.st_mtime_ns, "mode": stat.S_IMODE(before.st_mode) & 0o777,
                          "chunk_size": CHUNK_SIZE, "chunks": chunks,
                          "stats": {"logical_bytes": processed, "stored_bytes": stored_total,
                                    "new_bytes": new_estimate, "reused_bytes": reused,
                                    "saved_bytes": max(0, processed - stored_total),
                                    "chunk_count": len(chunks), "unique_chunks": len(unique),
                                    "compressed_chunks": sum(c["codec"] == "zlib" for c in unique.values()),
                                    "elapsed_seconds": time.monotonic() - started}}
            descriptor["signature"] = ops._sign(descriptor, key)
            if on_prepared is not None:
                import copy
                on_prepared(copy.deepcopy(descriptor))
            new_bytes = 0
            for hash_, chunk in unique.items():
                if positions[hash_] is not None:
                    _check(cancel)
                    spool.seek(positions[hash_])
                    stored = spool.read(chunk["stored_size"])
                    if len(stored) != chunk["stored_size"]:
                        raise PayloadError("임시 압축 데이터가 완전하지 않습니다.")
                    if _write_object(directory, chunk, stored):
                        new_bytes += len(stored)
            descriptor["stats"].update(new_bytes=new_bytes, elapsed_seconds=time.monotonic() - started)
            descriptor["signature"] = ops._sign(descriptor, key)
            verify_payload(directory, descriptor, cancel=cancel)
            _report(progress_detail, processed, processed, "복원 데이터의 전체 원본 해시를 검증했습니다.")
            return descriptor
    finally:
        os.close(source_fd)


def verify_payload(root, descriptor, *, cancel=None) -> dict:
    """Authenticate a recipe and verify every reconstructed byte without writing."""
    directory = _validate(root, descriptor)
    digest, total = hashlib.sha256(), 0
    for chunk in descriptor["chunks"]:
        _check(cancel)
        raw = _read_object(directory, chunk)
        digest.update(raw)
        total += len(raw)
    if total != descriptor["size"] or digest.hexdigest() != descriptor["sha256"]:
        raise PayloadError("전체 복원 데이터가 원본 바이트와 일치하지 않습니다.")
    return {**descriptor["stats"], "verified_bytes": total, "sha256": digest.hexdigest()}


def materialize(root, descriptor, output, *, progress_detail=None, cancel=None) -> dict:
    """Create a verified new raw file, never replace an existing destination."""
    ops = _ops()
    directory = _validate(root, descriptor)
    # Verify all objects before creating a destination, including the last chunk.
    verify_payload(directory, descriptor, cancel=cancel)
    target = ops._absolute(output)
    ops._safe_path(target)
    ops._mkdir(target.parent)
    ops._capacity([{"target": str(target), "size": descriptor["size"]}])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | ops._NOFOLLOW | ops._BINARY
    with ops._directory_descriptor(target.parent) as parent:
        fd = os.open(target.name if parent is not None else target, flags, 0o600,
                     **({"dir_fd": parent} if parent is not None else {}))
    digest, total = hashlib.sha256(), 0
    try:
        for chunk in descriptor["chunks"]:
            _check(cancel)
            raw = _read_object(directory, chunk)
            digest.update(raw)
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("복원 데이터를 기록하지 못했습니다.")
                view = view[written:]
            total += len(raw)
            _report(progress_detail, total, descriptor["size"], "압축한 원본 바이트를 복원하고 있습니다.")
        if total != descriptor["size"] or digest.hexdigest() != descriptor["sha256"]:
            raise PayloadError("복원 중 데이터 검증에 실패했습니다.")
        os.fsync(fd)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, descriptor["mode"])
        if os.utime in os.supports_fd:
            os.utime(fd, ns=(descriptor["mtime_ns"], descriptor["mtime_ns"]))
        elif os.name == "nt":
            class Times:
                st_atime_ns = descriptor["mtime_ns"]
                st_mtime_ns = descriptor["mtime_ns"]
            ops._windows_file_times(fd, Times())
    finally:
        os.close(fd)
    ops._sync_directory(target.parent)
    fingerprint = ops._fingerprint(target)
    if fingerprint["size"] != descriptor["size"] or fingerprint["sha256"] != descriptor["sha256"]:
        raise PayloadError("저장한 복원본의 검증에 실패했습니다. 파일을 보존했습니다.")
    return fingerprint
