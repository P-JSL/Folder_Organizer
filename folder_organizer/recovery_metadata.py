"""Bounded compressed restore recipes, never a replacement for retained bytes.

The legacy signed JSON checkpoint remains available. A small HMAC receipt binds
an active gzip recipe to that exact checkpoint and is invalidated before a new
checkpoint can describe a filesystem mutation. Thus an old compact recipe cannot
be used after an interrupted undo has begun.
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
import zlib

MAX_RAW_BYTES = 8 * 1024 * 1024
MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 4096
MAX_DEPTH = 64
MAX_ITEMS = 100_000
MIN_RAW_BYTES = 1024
COMPRESSION_BUDGET_SECONDS = .250
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class MetadataError(ValueError):
    pass


class LegacyIntegrityError(MetadataError):
    """Existing JSON changed; never bypass that evidence with a gzip copy."""


def paths(journal_path: Path) -> tuple[Path, Path]:
    identifier = journal_path.stem
    if not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise MetadataError("복원 메타데이터 작업 번호가 올바르지 않습니다.")
    return (journal_path.with_name(identifier + ".restore.json.gz"),
            journal_path.with_name(identifier + ".restore.receipt"))


def _safe(path: Path, *, require=False) -> None:
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if require and component == path:
                raise
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MetadataError("링크·재분석 지점의 복원 메타데이터는 사용하지 않습니다.")
        if component != path and not stat.S_ISDIR(info.st_mode):
            raise MetadataError("복원 메타데이터의 상위 경로가 폴더가 아닙니다.")
        if component == path and not stat.S_ISREG(info.st_mode):
            raise MetadataError("복원 메타데이터가 일반 파일이 아닙니다.")


def _read(path: Path, limit: int, *, digest_only=False):
    _safe(path, require=True)
    descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY | _NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise MetadataError("복원 메타데이터의 크기 또는 파일 형식이 올바르지 않습니다.")
        if digest_only:
            digest, count = hashlib.sha256(), 0
            while chunk := stream.read(64 * 1024):
                count += len(chunk)
                if count > limit:
                    raise MetadataError("복원 메타데이터 크기 제한을 넘었습니다.")
                digest.update(chunk)
            raw = (digest.hexdigest(), count)
        else:
            raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if not digest_only and len(raw) > limit:
        raise MetadataError("복원 메타데이터 크기 제한을 넘었습니다.")
    _safe(path, require=True)
    named = path.lstat()
    fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
    if (any(getattr(before, field) != getattr(after, field) or
            getattr(after, field) != getattr(named, field) for field in fields)
            or before.st_ctime_ns != after.st_ctime_ns):
        raise MetadataError("읽는 동안 복원 메타데이터가 바뀌었습니다.")
    return raw


def _canonical(value: dict) -> bytes:
    return json.dumps({key: item for key, item in value.items() if key != "signature"},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _signature(value: dict, key: bytes) -> str:
    return hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def _authenticate(value: dict, key: bytes) -> None:
    signature = value.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, _signature(value, key)):
        raise MetadataError("복원 메타데이터의 서명을 확인할 수 없습니다.")


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise MetadataError("복원 메타데이터에 중복 항목이 있습니다.")
        result[key] = value
    return result


def _document(raw: bytes) -> dict:
    # Bound nesting before json.loads, which otherwise recurses on hostile input.
    depth, quoted, escaped = 0, False, False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > MAX_DEPTH:
                raise MetadataError("복원 메타데이터의 중첩 제한을 넘었습니다.")
        elif char in (93, 125):
            depth -= 1
    def invalid_constant(_value):
        raise MetadataError("복원 메타데이터의 숫자 형식이 올바르지 않습니다.")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=invalid_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MetadataError("복원 메타데이터를 읽을 수 없습니다.") from exc
    if not isinstance(value, dict):
        raise MetadataError("복원 메타데이터가 객체 형식이 아닙니다.")
    return value


def _sync(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, raw: bytes, *, exclusive=False) -> None:
    _safe(path)
    temporary = path if exclusive else path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            _safe(path)
            os.replace(temporary, path)
        _sync(path.parent)
    finally:
        if not exclusive and temporary.exists():
            temporary.unlink()


def _receipt(journal_path: Path, root: Path, key: bytes) -> dict:
    _gzip_path, receipt_path = paths(journal_path)
    value = _document(_read(receipt_path, MAX_RECEIPT_BYTES))
    _authenticate(value, key)
    if (type(value.get("version")) is not int or value.get("version") != 1 or value.get("id") != journal_path.stem
            or value.get("root") != str(root)
            or not isinstance(value.get("state"), str) or value.get("state") not in {"final", "working", "legacy"}):
        raise MetadataError("복원 메타데이터의 폴더·작업 번호·형식이 일치하지 않습니다.")
    return value


def _write_receipt(journal_path, root, key, state, **values):
    _gzip_path, receipt_path = paths(journal_path)
    receipt = {"version": 1, "id": journal_path.stem, "root": str(root), "state": state, **values}
    receipt["signature"] = _signature(receipt, key)
    _write(receipt_path, json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def invalidate(journal_path: Path, root: Path, key: bytes) -> None:
    """Durably deactivate an older gzip before a working checkpoint changes."""
    _gzip_path, receipt_path = paths(journal_path)
    if not receipt_path.exists() and not receipt_path.is_symlink():
        return
    try:
        if _receipt(journal_path, root, key)["state"] == "working":
            return
    except (OSError, MetadataError):
        # Replace malformed *regular* metadata only after _safe rejects links.
        pass
    _write_receipt(journal_path, root, key, "working")


def _info(format="legacy", *, uncompressed_bytes=0, compressed_bytes=0, reason="", compression_ms=0):
    return {"format": format, "uncompressed_bytes": uncompressed_bytes,
            "compressed_bytes": compressed_bytes, "reason": reason,
            "compression_ms": compression_ms, "metadata_only": True,
            "requires_retained_data": True}


def write_compact(journal_path: Path, root: Path, key: bytes) -> dict:
    """Compress one final signed checkpoint; failure leaves legacy undo intact."""
    raw_size = 0
    elapsed = 0
    try:
        _safe(journal_path, require=True)
        raw_size = journal_path.stat().st_size
        if raw_size < MIN_RAW_BYTES:
            reason = "작은 기록은 압축 비용을 줄이기 위해 기존 방식으로 저장했습니다."
        elif raw_size > MAX_RAW_BYTES:
            reason = "복원 메타데이터 크기 제한을 넘어 기존 방식으로 저장했습니다."
        else:
            raw = _read(journal_path, MAX_RAW_BYTES)
            record = _document(raw)
            _authenticate(record, key)
            if record.get("id") != journal_path.stem or record.get("root") != str(root) or record.get("version") != 1:
                raise MetadataError("압축할 작업 기록의 폴더·작업 번호가 다릅니다.")
            if not isinstance(record.get("items"), list) or len(record["items"]) > MAX_ITEMS:
                raise MetadataError("압축할 작업 기록의 파일 수 제한을 넘었습니다.")
            started = time.perf_counter()
            compressed = gzip.compress(raw, compresslevel=1, mtime=0)
            elapsed = round((time.perf_counter() - started) * 1000, 3)
            if elapsed > COMPRESSION_BUDGET_SECONDS * 1000:
                reason = "압축 시간 기준을 넘어 기존 방식으로 저장했습니다."
            elif len(compressed) > MAX_COMPRESSED_BYTES or len(compressed) >= len(raw) * .9:
                reason = "압축 효과가 작아 기존 방식으로 저장했습니다."
            else:
                gzip_path, _receipt_path = paths(journal_path)
                _write(gzip_path, compressed)
                if _read(gzip_path, MAX_COMPRESSED_BYTES) != compressed:
                    raise MetadataError("저장한 압축 복원 메타데이터가 일치하지 않습니다.")
                _write_receipt(journal_path, root, key, "final", codec="gzip",
                               json_sha256=hashlib.sha256(raw).hexdigest(),
                               gzip_sha256=hashlib.sha256(compressed).hexdigest(),
                               journal_signature=record["signature"], uncompressed_bytes=len(raw),
                               compressed_bytes=len(compressed), compression_ms=elapsed)
                return _info("gzip", uncompressed_bytes=len(raw), compressed_bytes=len(compressed), compression_ms=elapsed)
        _write_receipt(journal_path, root, key, "legacy", reason=reason, uncompressed_bytes=raw_size)
        return _info(uncompressed_bytes=raw_size, reason=reason, compression_ms=elapsed)
    except Exception as exc:
        # The caller already saved a durable signed JSON checkpoint. Neither
        # compression nor an unavailable optional receipt may invalidate it.
        return _info(uncompressed_bytes=raw_size, reason="압축 메타데이터를 저장하지 못해 기존 복원 기록을 유지합니다: " + str(exc)[:300], compression_ms=elapsed)


def _legacy_matches(journal_path: Path, receipt: dict) -> None:
    try:
        digest, count = _read(journal_path, MAX_RAW_BYTES, digest_only=True)
        if (count != receipt["uncompressed_bytes"] or
                not hmac.compare_digest(digest, receipt["json_sha256"])):
            raise LegacyIntegrityError("기존 복원 기록이 압축 메타데이터 생성 이후 변경되었습니다. 변경된 기록을 우회하지 않습니다.")
    except (OSError, MetadataError) as exc:
        if isinstance(exc, LegacyIntegrityError):
            raise
        raise LegacyIntegrityError("기존 복원 기록을 안전하게 확인할 수 없습니다. 압축 기록으로 우회하지 않습니다.") from exc


def _active(journal_path: Path, root: Path, key: bytes) -> tuple[dict, dict, bytes]:
    receipt = _receipt(journal_path, root, key)
    if receipt["state"] != "final" or receipt.get("codec") != "gzip":
        raise MetadataError("현재 작업에는 활성 압축 복원 메타데이터가 없습니다.")
    for field, limit in (("uncompressed_bytes", MAX_RAW_BYTES), ("compressed_bytes", MAX_COMPRESSED_BYTES)):
        value = receipt.get(field)
        if type(value) is not int or not 0 < value <= limit:
            raise MetadataError("압축 복원 메타데이터의 크기 제한을 넘었습니다.")
    for field in ("gzip_sha256", "json_sha256", "journal_signature"):
        if not isinstance(receipt.get(field), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[field]):
            raise MetadataError("압축 복원 메타데이터의 검증 값이 올바르지 않습니다.")
    if journal_path.exists() or journal_path.is_symlink():
        _legacy_matches(journal_path, receipt)
    gzip_path, _receipt_path = paths(journal_path)
    compressed = _read(gzip_path, MAX_COMPRESSED_BYTES)
    if len(compressed) != receipt["compressed_bytes"] or not hmac.compare_digest(hashlib.sha256(compressed).hexdigest(), receipt["gzip_sha256"]):
        raise MetadataError("압축 복원 메타데이터가 변경되거나 손상되었습니다.")
    try:
        decoder = zlib.decompressobj(wbits=31)
        raw = decoder.decompress(compressed, receipt["uncompressed_bytes"] + 1)
        if (len(raw) != receipt["uncompressed_bytes"] or not decoder.eof
                or decoder.unused_data or decoder.unconsumed_tail):
            raise MetadataError("압축 복원 메타데이터의 해제 크기·형식이 올바르지 않습니다.")
    except zlib.error as exc:
        raise MetadataError("압축 복원 메타데이터를 해제하지 못했습니다.") from exc
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), receipt["json_sha256"]):
        raise MetadataError("해제한 복원 메타데이터의 검증 값이 다릅니다.")
    record = _document(raw)
    _authenticate(record, key)
    if (record.get("signature") != receipt["journal_signature"] or record.get("version") != 1
            or record.get("id") != journal_path.stem or record.get("root") != str(root)
            or not isinstance(record.get("items"), list) or len(record["items"]) > MAX_ITEMS):
        raise MetadataError("압축 복원 기록의 서명·폴더·파일 수가 일치하지 않습니다.")
    info = _info("gzip", uncompressed_bytes=len(raw), compressed_bytes=len(compressed),
                 compression_ms=receipt.get("compression_ms", 0))
    return record, info, raw


def read_preferred(journal_path: Path, root: Path, key: bytes) -> tuple[dict | None, dict]:
    """Return authenticated gzip data or a reason for using signed legacy JSON."""
    try:
        record, info, _raw = _active(journal_path, root, key)
        return record, info
    except LegacyIntegrityError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        gzip_path, receipt_path = paths(journal_path)
        reason = ""
        try:
            receipt = _receipt(journal_path, root, key)
            if receipt["state"] == "legacy":
                return None, _info(uncompressed_bytes=receipt.get("uncompressed_bytes", 0), reason=str(receipt.get("reason", "기존 복원 기록을 사용합니다.")))
            if receipt["state"] == "working":
                reason = "진행·중단된 작업은 최신 기존 복원 기록을 사용합니다. 이전 압축 기록은 사용하지 않습니다."
        except (OSError, ValueError, RecursionError):
            pass
        if not reason and (gzip_path.exists() or receipt_path.exists() or gzip_path.is_symlink() or receipt_path.is_symlink()):
            reason = "압축 메타데이터를 확인하지 못해 기존 복원 기록을 사용합니다: " + str(exc)[:300]
        return None, _info(reason=reason)


def materialize_checkpoint(journal_path: Path, root: Path, key: bytes) -> None:
    """Recreate the exact authenticated baseline before invalidating gzip undo.

    Creating the same bytes first ensures that a crash before invalidation still
    has a matching legacy checkpoint; after invalidation only that JSON is used.
    """
    _record, _metadata, raw = _active(journal_path, root, key)
    _write(journal_path, raw, exclusive=True)
