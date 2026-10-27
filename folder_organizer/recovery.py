"""Bounded, non-destructive integrity checks and best-effort recovery copies.

This module does not undelete disk sectors. It never changes the source. A
successful rewrite is a structurally checked copy, not a promise that all of the
original document's content survived.
"""
from __future__ import annotations

import io
import os
import stat
import struct
import warnings
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
from typing import Any
from xml.etree import ElementTree

MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_COMPRESSION_RATIO = 500
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_TOTAL_PIXELS = 80_000_000
MAX_IMAGE_FRAMES = 1_000
MAX_PDF_PAGES = 2_000
MAX_XML_BYTES = 8 * 1024 * 1024

_ARCHIVES = {".zip", ".docx", ".xlsx", ".pptx", ".docm", ".xlsm", ".pptm"}
_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico"}
_OFFICE_MAIN = {
    ".docx": "word/document.xml", ".docm": "word/document.xml",
    ".xlsx": "xl/workbook.xml", ".xlsm": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml", ".pptm": "ppt/presentation.xml",
}


class RecoveryError(ValueError):
    """Unsafe or changed input; no source mutation is permitted."""


class UnsupportedFile(RecoveryError):
    """A format or resource requirement is outside supported bounds."""


def _no_symlinks(path: Path) -> None:
    """Reject links in every currently existing path component."""
    for component in (path, *path.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise RecoveryError("심볼릭 링크·정션 경로는 복구 대상이나 출력 폴더로 사용할 수 없습니다.")
        try:
            attributes = getattr(component.stat(follow_symlinks=False), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise RecoveryError("Windows 재분석 지점은 복구 경로로 사용할 수 없습니다.")


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _source_snapshot(path: str | Path) -> tuple[Path, bytes, tuple[int, ...]]:
    source = Path(os.path.abspath(os.fspath(path)))
    _no_symlinks(source)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryError("일반 파일만 검사할 수 있습니다.")
        if before.st_size > MAX_INPUT_BYTES:
            raise UnsupportedFile("안전한 메모리 사용을 위해 128 MiB 이하 파일만 복구 검사합니다.")
        data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise UnsupportedFile("복구 검사 크기 제한을 초과했습니다.")
        after = os.fstat(stream.fileno())
    fingerprint = _signature(before)
    if fingerprint != _signature(after) or len(data) != before.st_size:
        raise RecoveryError("검사 중 원본이 변경되었습니다. 다시 검사해 주세요.")
    _unchanged(source, fingerprint)
    return source, data, fingerprint


def _unchanged(source: Path, fingerprint: tuple[int, ...]) -> None:
    _no_symlinks(source)
    # Compare metadata through the same API used for the original snapshot.
    # CPython 3.12 on Windows exposes creation time as Path.stat().st_ctime,
    # while os.fstat().st_ctime contains FILE_BASIC_INFO.ChangeTime. Comparing
    # those unlike timestamps falsely rejects unchanged files. Reopening and
    # fstat'ing preserves both identity and the actual change-time guard.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(source, flags)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or _signature(current) != fingerprint:
            raise RecoveryError("검사 이후 원본이 변경되어 작업을 중단했습니다.")
    finally:
        os.close(descriptor)
    _no_symlinks(source)


def _archive_name(name: str) -> bool:
    if not name or "\x00" in name or "\\" in name or ":" in name:
        return False
    parts = PurePosixPath(name).parts
    return not name.startswith("/") and bool(parts) and all(part not in {"..", "."} for part in parts)


def _entry_limit(size: int, compressed: int) -> None:
    if size < 0 or compressed < 0 or size > MAX_ENTRY_BYTES:
        raise UnsupportedFile("압축 항목 크기 제한을 초과했습니다.")
    if size > max(compressed, 1) * MAX_COMPRESSION_RATIO:
        raise UnsupportedFile("비정상적으로 높은 압축률이 감지되었습니다.")


def _xml_problem(name: str, payload: bytes) -> str | None:
    if not name.lower().endswith((".xml", ".rels")):
        return None
    if len(payload) > MAX_XML_BYTES:
        return f"XML 크기 제한 초과: {name}"
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        return f"외부/내부 엔터티가 포함된 XML은 처리하지 않음: {name}"
    try:
        ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return f"XML 구문 손상: {name}"
    return None


def _office_problems(suffix: str, entries: dict[str, bytes]) -> list[str]:
    if suffix not in _OFFICE_MAIN:
        return []
    required = {"[Content_Types].xml", "_rels/.rels", _OFFICE_MAIN[suffix]}
    issues = [f"필수 Office 항목 누락: {name}" for name in sorted(required - entries.keys())]
    for name, payload in entries.items():
        problem = _xml_problem(name, payload)
        if problem:
            issues.append(problem)
    return issues


def _read_archive(data: bytes) -> tuple[dict[str, bytes], list[str], bool]:
    entries: dict[str, bytes] = {}
    issues: list[str] = []
    encrypted = False
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise UnsupportedFile("압축 항목 수 제한을 초과했습니다.")
        seen: set[str] = set()
        for info in infos:
            # ZipInfo normalizes Windows separators and truncates NULs in
            # filename. Validate the original archive spelling as well, so the
            # same unsafe member is rejected on Windows and POSIX.
            if not _archive_name(info.orig_filename) or not _archive_name(info.filename):
                issues.append(f"안전하지 않은 압축 경로 제외: {info.orig_filename[:200]}")
                continue
            folded = info.filename.casefold()
            if folded in seen:
                issues.append(f"중복 압축 항목 제외: {info.filename[:200]}")
                continue
            seen.add(folded)
            if info.flag_bits & 1:
                encrypted = True
                issues.append(f"암호화 항목은 비밀번호 없이 검사할 수 없음: {info.filename[:200]}")
                continue
            _entry_limit(info.file_size, info.compress_size)
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise UnsupportedFile("전체 압축 해제 크기 제한을 초과했습니다.")
            if info.is_dir():
                entries[info.filename] = b""
                continue
            try:
                with archive.open(info) as stream:
                    payload = stream.read(MAX_ENTRY_BYTES + 1)
                if len(payload) > MAX_ENTRY_BYTES or len(payload) != info.file_size:
                    raise RecoveryError("압축 해제 크기 불일치")
                entries[info.filename] = payload
            except (OSError, RuntimeError, ValueError, EOFError, zipfile.BadZipFile, zlib.error, NotImplementedError) as exc:
                issues.append(f"읽을 수 없는 압축 항목 제외: {info.filename[:200]} ({type(exc).__name__})")
    return entries, issues, encrypted


def _salvage_local_headers(data: bytes) -> tuple[dict[str, bytes], list[str]]:
    """Recover only local entries whose explicit size and CRC can be verified.

    Streaming/data-descriptor entries, encrypted data, and unknown compression
    methods are deliberately skipped; guessing their boundaries risks bad data.
    """
    entries: dict[str, bytes] = {}
    issues = ["중앙 디렉터리가 손상되어 크기·CRC를 검증한 로컬 항목만 복구했습니다."]
    offset = 0
    examined = 0
    total = 0
    seen: set[str] = set()
    while examined < MAX_ENTRIES:
        found = data.find(b"PK\x03\x04", offset)
        if found < 0 or found + 30 > len(data):
            break
        examined += 1
        offset = found + 4
        try:
            _, _version, flags, method, _time, _date, crc, compressed, size, name_len, extra_len = struct.unpack_from("<4s5H3I2H", data, found)
            start = found + 30 + name_len + extra_len
            end = start + compressed
            if not name_len or name_len > 4096 or start > len(data) or end > len(data):
                continue
            raw_name = data[found + 30 : found + 30 + name_len]
            name = raw_name.decode("utf-8" if flags & 0x800 else "cp437")
            if not _archive_name(name) or name.casefold() in seen:
                continue
            if flags & 9 or method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                issues.append(f"경계를 안전하게 확인할 수 없는 항목 제외: {name[:200]}")
                continue
            _entry_limit(size, compressed)
            if total + size > MAX_TOTAL_BYTES:
                raise UnsupportedFile("복구 데이터 총 크기 제한을 초과했습니다.")
            payload = data[start:end]
            if method == zipfile.ZIP_DEFLATED:
                decoder = zlib.decompressobj(-15)
                payload = decoder.decompress(payload, MAX_ENTRY_BYTES + 1)
                if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                    continue
            if len(payload) != size or (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
                continue
            total += size
            entries[name] = payload
            seen.add(name.casefold())
            offset = end
        except (UnicodeError, struct.error, ValueError, zlib.error):
            continue
    if examined >= MAX_ENTRIES and data.find(b"PK\x03\x04", offset) >= 0:
        issues.append("로컬 헤더 검사 개수 제한으로 나머지 데이터는 처리하지 않았습니다.")
    return entries, issues


def _image_impl(data: bytes, *, recover: bool = False) -> tuple[bytes | None, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise UnsupportedFile("이미지 검사에 Pillow 설치가 필요합니다.") from exc
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        if not recover:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    raise UnsupportedFile("이미지 픽셀 수 제한을 초과했습니다.")
                image.verify()
        with Image.open(io.BytesIO(data)) as image:
            frames = getattr(image, "n_frames", 1)
            if frames > MAX_IMAGE_FRAMES:
                raise UnsupportedFile("이미지 프레임 수 제한을 초과했습니다.")
            pixels = 0
            first_frame = None
            for index in range(frames):
                image.seek(index)
                pixels += image.width * image.height
                if image.width * image.height > MAX_IMAGE_PIXELS or pixels > MAX_IMAGE_TOTAL_PIXELS:
                    raise UnsupportedFile("이미지 해제 픽셀 수 제한을 초과했습니다.")
                image.load()
                if index == 0 and recover:
                    first_frame = image.copy()
            if recover:
                assert first_frame is not None
                if first_frame.mode not in {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}:
                    first_frame = first_frame.convert("RGBA")
                output = io.BytesIO()
                first_frame.save(output, format="PNG")
                if output.tell() > MAX_TOTAL_BYTES:
                    raise UnsupportedFile("복구 이미지 크기 제한을 초과했습니다.")
                return output.getvalue(), frames
    return None, frames


def _image(data: bytes, *, recover: bool = False) -> tuple[bytes | None, int]:
    try:
        from PIL import Image
    except ImportError as exc:
        raise UnsupportedFile("이미지 검사에 Pillow 설치가 필요합니다.") from exc
    try:
        return _image_impl(data, recover=recover)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise UnsupportedFile("이미지 해제 크기가 안전 제한을 초과했습니다.") from exc


def _pdf(data: bytes, *, recover: bool = False) -> bytes | None:
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise UnsupportedFile("PDF 검사에 pypdf 설치가 필요합니다.") from exc
    reader = PdfReader(io.BytesIO(data), strict=not recover)
    if reader.is_encrypted:
        raise UnsupportedFile("암호화 PDF는 비밀번호 없이 검사·복구할 수 없습니다.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise UnsupportedFile("PDF 페이지 수 제한을 초과했습니다.")
    for page in reader.pages:
        _ = page.mediabox
        # Resolve content streams so broken references/compression do not pass
        # merely because the page tree is readable. pypdf also limits inflation.
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_ENTRY_BYTES:
            raise UnsupportedFile("PDF 페이지 콘텐츠 크기 제한을 초과했습니다.")
    if recover:
        writer = PdfWriter()
        writer.clone_document_from_reader(reader)
        output = io.BytesIO()
        writer.write(output)
        if output.tell() > MAX_TOTAL_BYTES:
            raise UnsupportedFile("복구 PDF 크기 제한을 초과했습니다.")
        result = output.getvalue()
        _pdf(result)
        return result
    return None


def _inspect_data(data: bytes, suffix: str) -> dict[str, Any]:
    if suffix in _ARCHIVES:
        try:
            entries, issues, encrypted = _read_archive(data)
        except zipfile.BadZipFile:
            return {"status": "damaged", "format": "ZIP/Office", "details": ["ZIP 중앙 디렉터리를 읽을 수 없습니다. 복구는 일부 항목에 한정될 수 있습니다."]}
        issues.extend(_office_problems(suffix, entries))
        status = "unsupported" if encrypted else "damaged" if issues else "healthy"
        details = issues or ["압축 항목의 크기와 CRC 검사를 통과했습니다." if suffix == ".zip" else "Office 패키지의 CRC, 필수 항목, XML 구문 검사를 통과했습니다. 문서 의미·서식 전체를 보장하지는 않습니다."]
        return {"status": status, "format": "ZIP/Office", "details": details, "entries": len(entries)}
    if suffix in _IMAGES:
        _image(data)
        return {"status": "healthy", "format": "image", "details": ["이미지 구조 확인과 모든 지원 프레임의 픽셀 해석을 통과했습니다."]}
    if suffix == ".pdf":
        _pdf(data)
        return {"status": "healthy", "format": "PDF", "details": ["PDF 구조, 페이지 참조, 콘텐츠 스트림을 읽었습니다. 화면 표시·원문 완전성 전체를 보장하지는 않습니다."]}
    raise UnsupportedFile("지원 형식: ZIP, DOCX/XLSX/PPTX(매크로 형식 포함), PNG/JPEG/GIF/BMP/TIFF/WebP/ICO, PDF. 실행 파일·디스크 삭제 복구는 지원하지 않습니다.")


def inspect_file(path: str | Path) -> dict[str, Any]:
    """Inspect a supported regular file without changing it."""
    result: dict[str, Any] = {"path": str(path), "status": "error", "format": Path(path).suffix.lower().lstrip(".") or "unknown", "details": []}
    try:
        source, data, fingerprint = _source_snapshot(path)
        result["path"] = str(source)
        try:
            result.update(_inspect_data(data, source.suffix.lower()))
        except UnsupportedFile:
            raise
        except Exception as exc:
            result.update(status="damaged", details=[f"파일 구조를 읽을 수 없습니다: {type(exc).__name__}: {str(exc)[:250]}"])
        _unchanged(source, fingerprint)
    except UnsupportedFile as exc:
        result.update(status="unsupported", details=[str(exc)])
    except (OSError, RecoveryError) as exc:
        result.update(status="error", details=[str(exc)])
    return result


@contextmanager
def _output_directory(directory: Path):
    """On POSIX, create/open every ancestor without following links.

    A held directory descriptor also prevents a concurrent rename/link swap
    from redirecting an exclusive output write or its cleanup elsewhere.
    Windows uses explicit link/junction/reparse-point checks instead.
    """
    _no_symlinks(directory)
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_DIRECTORY"):
        directory.mkdir(parents=True, exist_ok=True)
        _no_symlinks(directory)
        if not directory.is_dir():
            raise RecoveryError("복구 출력 위치는 폴더여야 합니다.")
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(directory.anchor, flags)
    try:
        for part in directory.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _output_file(destination: str | Path, filename: str, payload: bytes, source: Path, fingerprint: tuple[int, ...]) -> Path:
    directory = Path(os.path.abspath(os.fspath(destination)))
    _unchanged(source, fingerprint)
    filename_path = Path(filename)
    with _output_directory(directory) as directory_fd:
        for sequence in range(1, 10_001):
            suffix = "" if sequence == 1 else f" ({sequence})"
            candidate = directory / f"{filename_path.stem}{suffix}{filename_path.suffix}"
            _no_symlinks(directory)
            relative = candidate.name if directory_fd is not None else candidate
            kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            try:
                descriptor = os.open(relative, flags, 0o600, **kwargs)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                _unchanged(source, fingerprint)
                _no_symlinks(directory)
                if directory_fd is not None:
                    current = directory.stat()
                    held = os.fstat(directory_fd)
                    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
                        raise RecoveryError("출력 폴더 경로가 변경되어 복구를 중단했습니다.")
                return candidate
            except BaseException:
                try:
                    os.unlink(relative, **kwargs)
                except FileNotFoundError:
                    pass
                raise
    raise RecoveryError("사용 가능한 복구 파일 이름을 만들 수 없습니다.")


def repair_file(path: str | Path, destination: str | Path) -> dict[str, Any]:
    """Write a new recovery copy into ``destination``; never overwrite anything."""
    result: dict[str, Any] = {"path": str(path), "status": "failed", "output": None, "details": []}
    try:
        source, data, fingerprint = _source_snapshot(path)
        suffix = source.suffix.lower()
        details: list[str] = []
        partial = False
        if suffix in _ARCHIVES:
            try:
                entries, issues, _encrypted = _read_archive(data)
            except zipfile.BadZipFile:
                entries, issues = _salvage_local_headers(data)
            issues.extend(_office_problems(suffix, entries))
            if not any(not name.endswith("/") for name in entries):
                raise RecoveryError("CRC와 크기를 검증할 수 있는 압축 항목이 없어 복구 파일을 만들지 않았습니다.")
            partial = bool(issues)
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for name, payload in entries.items():
                    archive.writestr(name, payload)
            payload = output.getvalue()
            details.extend(issues)
            details.append("정상적으로 읽은 항목만 새 압축 파일에 저장했습니다. 원본은 변경하지 않았습니다.")
            result["recovered_entries"] = len(entries)
            output_suffix = ".zip" if partial and suffix in _OFFICE_MAIN else suffix
            filename = f"{source.stem}.recovered{output_suffix}"
            # Check the new ZIP's CRC even if its Office content remains partial.
            new_entries, new_issues, _ = _read_archive(payload)
            if new_issues or len(new_entries) != len(entries):
                raise RecoveryError("생성한 압축 복구본 검증에 실패했습니다.")
            if partial and suffix in _OFFICE_MAIN:
                details.append("Office 패키지가 완전하지 않을 수 있어 검토용 ZIP으로 저장했습니다.")
        elif suffix in _IMAGES:
            payload, frames = _image(data, recover=True)
            assert payload is not None
            _image(payload)
            filename = f"{source.stem}.recovered.png"
            partial = frames > 1
            details.append("해석 가능한 첫 프레임의 픽셀을 새 PNG에 저장했습니다. 메타데이터·애니메이션·원본 압축 형식은 보존하지 않습니다.")
            if frames > 1:
                details.append(f"원본 {frames}개 프레임 중 첫 프레임만 복구하여 부분 복구로 표시합니다.")
        elif suffix == ".pdf":
            payload = _pdf(data, recover=True)
            assert payload is not None
            filename = f"{source.stem}.recovered.pdf"
            details.append("읽을 수 있는 PDF 구조를 다시 저장하고 재검사했습니다. 누락된 원문·손상된 페이지 내용을 재생성하지는 못합니다.")
        else:
            raise UnsupportedFile("이 형식의 손상 복구는 지원하지 않습니다. 실행 파일·디스크에서 삭제된 파일은 복구할 수 없습니다.")
        _unchanged(source, fingerprint)
        output_path = _output_file(destination, filename, payload, source, fingerprint)
        result.update(status="partial" if partial else "repaired", output=str(output_path), details=details)
    except UnsupportedFile as exc:
        result.update(status="unsupported", details=[str(exc)])
    except Exception as exc:
        result.update(status="failed", details=[f"복구본을 만들지 못했습니다: {type(exc).__name__}: {str(exc)[:250]}"])
    return result
