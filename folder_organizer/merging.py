"""Explicit, previewed merges of bounded UTF-8 documents and tables.

The preview is process-signed. Its sources must still have the same identity and
SHA-256 before any output is created. Outputs are exclusive, verified new files;
optional source retirement uses the normal journalled, reversible quarantine.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
from pathlib import Path
import re
import secrets
import stat
import uuid

from . import operations as ops

MAX_FILES = 20
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_UNITS = 200_000
MAX_COLUMNS = 1_000
MAX_CELLS = 1_000_000
SUPPORTED_SUFFIXES = frozenset({".txt", ".md", ".csv", ".tsv"})
_KEY = secrets.token_bytes(32)
_FINGERPRINT_FIELDS = ("size", "mtime_ns", "device", "inode", "sha256")
_METADATA_FIELDS = {"size": "st_size", "mtime_ns": "st_mtime_ns", "ctime_ns": "st_ctime_ns",
                    "device": "st_dev", "inode": "st_ino"}


class MergeError(ops.OperationError):
    """The selected documents cannot be merged without guessing or overwriting."""


def _progress(callback, phase, completed, total, message):
    if callback:
        callback({"phase": phase, "completed": completed, "total": total,
                  "unit": "files", "message": message})


def _record(event, identifier, status, **kwargs):
    # A broken/disabled log must never determine whether user bytes survive.
    try:
        from .execution_log import record_execution
        record_execution(event, execution_id=identifier, action="merge", status=status, **kwargs)
    except Exception:
        pass


def _metadata(info):
    return {key: getattr(info, field) for key, field in _METADATA_FIELDS.items()}


def _check_source_metadata(path, expected):
    """Compare change time through fstat, while also checking pathname identity.

    On CPython 3.12/Windows, pathname stat can report creation time in st_ctime
    while descriptor fstat reports ChangeTime. Comparing those two APIs would
    reject unchanged files. Reopen without following links for like-for-like
    checks; pathname stat still verifies identity, size and modification time.
    """
    ops._safe_path(path, require=True)
    descriptor = os.open(path, os.O_RDONLY | ops._NOFOLLOW | ops._BINARY)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or any(
                getattr(current, field) != expected[key] for key, field in _METADATA_FIELDS.items()):
            raise MergeError(f"내용 확인 이후 파일이 변경되거나 교체되었습니다: {path.name}")
        ops._safe_path(path, require=True)
        named = path.lstat()
        fields = ("st_size", "st_mtime_ns", "st_dev", "st_ino")
        if any(getattr(current, field) != getattr(named, field) for field in fields):
            raise MergeError(f"내용 확인 이후 파일 경로가 교체되었습니다: {path.name}")
    finally:
        os.close(descriptor)


def _read_source(path: Path) -> tuple[str, dict]:
    """Read bounded content and fingerprint the exact same opened file."""
    ops._safe_path(path, require=True)
    descriptor = os.open(path, os.O_RDONLY | ops._NOFOLLOW | ops._BINARY)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MergeError("일반 파일만 병합할 수 있습니다.")
        if before.st_size > MAX_FILE_BYTES:
            raise MergeError(f"파일당 8 MiB까지 병합할 수 있습니다: {path.name}")
        chunks, size = [], 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise MergeError(f"읽는 동안 크기 제한을 넘었습니다: {path.name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fields = ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise MergeError(f"읽는 동안 파일이 변경되었습니다: {path.name}")
        _check_source_metadata(path, _metadata(after))
        raw = b"".join(chunks)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise MergeError(f"UTF-8 텍스트만 병합할 수 있습니다: {path.name}") from exc
        if any(ord(char) < 32 and char not in "\t\n\r" for char in text):
            raise MergeError(f"텍스트에 제어 문자 또는 바이너리 내용이 있습니다: {path.name}")
        return text, {**_metadata(after), "sha256": hashlib.sha256(raw).hexdigest()}
    finally:
        os.close(descriptor)


def _text_blocks(text):
    """Yield blank-line paragraphs without splitting fenced code blocks.

    The iterator also avoids allocating millions of split paragraphs before the
    unit limit has a chance to reject a pathological document.
    """
    lines, fence = [], None
    for match in re.finditer(r"[^\n]*(?:\n|$)", text):
        if not match.group():
            continue
        line = match.group().removesuffix("\n")
        if fence:
            lines.append(line)
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*", line):
                fence = None
            continue
        if not line.strip(" \t"):
            if lines:
                yield "\n".join(lines)
                lines = []
            continue
        opening = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = (opening[1][0], len(opening[1]))
        lines.append(line)
    if lines:
        yield "\n".join(lines)


def _merge_text(documents):
    seen, unique, total = set(), [], 0
    for text in documents:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for block in _text_blocks(text):
            if not block:
                continue
            total += 1
            if total > MAX_UNITS:
                raise MergeError("문단·행은 합계 200,000개까지 병합할 수 있습니다.")
            if block not in seen:
                seen.add(block)
                unique.append(block)
    return ("\n\n".join(unique) + ("\n" if unique else "")), total, len(unique), "문단"


def _merge_table(documents, delimiter):
    header, unique, seen, total, cells = None, [], set(), 0, 0
    for text in documents:
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            current = next(reader, None)
            if not current:
                raise MergeError("표에는 비어 있지 않은 첫 번째 헤더 행이 필요합니다.")
            if len(current) > MAX_COLUMNS:
                raise MergeError("표는 1,000개 열까지 병합할 수 있습니다.")
            if header is None:
                header = current
            elif current != header:
                raise MergeError("CSV·TSV의 열 이름과 순서가 같아야 병합할 수 있습니다.")
            for row in reader:
                if len(row) != len(header):
                    raise MergeError("헤더와 열 수가 다른 행이 있습니다. 원본 표를 먼저 확인하세요.")
                cells += len(row)
                if cells > MAX_CELLS:
                    raise MergeError("표의 데이터 셀은 합계 1,000,000개까지 병합할 수 있습니다.")
                total += 1
                if total > MAX_UNITS:
                    raise MergeError("문단·행은 합계 200,000개까지 병합할 수 있습니다.")
                key = tuple(row)
                if key not in seen:
                    seen.add(key)
                    unique.append(row)
        except csv.Error as exc:
            raise MergeError(f"CSV·TSV 구문을 읽을 수 없습니다: {exc}") from exc
    stream = io.StringIO(newline="")
    # Quote every field: with LF row separators, minimal quoting would fail to
    # quote a field containing a lone CR and corrupt its exact cell boundary.
    writer = csv.writer(stream, delimiter=delimiter, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(unique)
    return stream.getvalue(), total, len(unique), "행"


def _output_name(value: str, suffix: str) -> str:
    if not isinstance(value, str) or len(value) > 180 or not value.strip():
        raise MergeError("병합본 이름은 1~180자로 입력하세요.")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.endswith((" ", ".")):
        raise MergeError("병합본 이름에 경로·예약 문자 또는 끝의 공백·마침표를 사용할 수 없습니다.")
    if re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", value, re.IGNORECASE):
        raise MergeError("Windows 예약 파일 이름은 사용할 수 없습니다.")
    if Path(value).suffix.casefold() != suffix:
        raise MergeError(f"병합본 확장자는 {suffix}이어야 합니다.")
    return value


def build_merge_preview(root, paths, *, destination=None, output_name=None,
                        move_originals=False, progress_detail=None) -> dict:
    """Build an immutable preview without creating files, directories or logs.

    Selection order defines the output order. Similar titles are a selection aid,
    never evidence for discarding nonidentical paragraphs/cells.
    """
    directory = ops._root(root)
    selected = list(dict.fromkeys(ops._source(path, directory) for path in paths))
    if not 2 <= len(selected) <= MAX_FILES:
        raise MergeError("병합할 파일을 2~20개 선택하세요.")
    if not isinstance(move_originals, bool):
        raise MergeError("원본 이동 여부가 올바르지 않습니다.")
    if move_originals and any(parent.name.casefold().endswith(".app") for path in selected for parent in path.parents):
        raise MergeError("앱 번들의 내부 파일은 이동할 수 없습니다. 새 병합본만 만들기를 선택하세요.")
    suffixes = [path.suffix.casefold() for path in selected]
    if any(suffix not in SUPPORTED_SUFFIXES for suffix in suffixes):
        raise MergeError("UTF-8의 TXT·Markdown·CSV·TSV 파일만 병합할 수 있습니다.")
    is_text = all(suffix in {".txt", ".md"} for suffix in suffixes)
    if not is_text and (len(set(suffixes)) != 1 or suffixes[0] not in {".csv", ".tsv"}):
        raise MergeError("텍스트와 표를 섞을 수 없으며 CSV·TSV는 같은 형식끼리 선택하세요.")
    target_directory = ops._root(destination) if destination else directory
    if any(path.stat().st_size > MAX_FILE_BYTES for path in selected):
        raise MergeError("파일당 8 MiB까지 병합할 수 있습니다.")
    if sum(path.stat().st_size for path in selected) > MAX_TOTAL_BYTES:
        raise MergeError("선택 파일의 합계는 32 MiB 이하여야 합니다.")
    documents, sources, total_size = [], [], 0
    for index, path in enumerate(selected, 1):
        _progress(progress_detail, "merge_preview", index - 1, len(selected), f"내용 확인: {path.name}")
        text, fingerprint = _read_source(path)
        total_size += fingerprint["size"]
        if total_size > MAX_TOTAL_BYTES:
            raise MergeError("선택 파일의 합계는 32 MiB 이하여야 합니다.")
        sources.append({"path": str(path), **fingerprint})
        documents.append(text)
    content, total, retained, unit = (_merge_text(documents) if is_text else
                                      _merge_table(documents, "," if suffixes[0] == ".csv" else "\t"))
    suffix = suffixes[0]
    name = _output_name(output_name or f"{selected[0].stem[:140]}_병합{suffix}", suffix)
    output = target_directory / name
    ops._safe_path(output)
    if str(output).casefold() in {str(path).casefold() for path in selected}:
        raise MergeError("병합본은 원본과 다른 새 이름으로 저장해야 합니다.")
    if output.exists() or output.is_symlink():
        if output_name:
            raise MergeError("같은 이름의 파일이 있습니다. 다른 새 이름을 입력하세요.")
        output = ops._unique(output, set())
    raw = content.encode("utf-8")
    if len(raw) > MAX_TOTAL_BYTES:
        raise MergeError("병합 결과는 32 MiB 이하여야 합니다.")
    plan = {"version": 1, "id": uuid.uuid4().hex, "root": str(directory),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "sources": sources, "output": str(output), "content": content,
            "output_size": len(raw), "output_sha256": hashlib.sha256(raw).hexdigest(),
            "move_originals": move_originals, "format": "text" if is_text else suffix[1:],
            "total_units": total, "retained_units": retained, "removed_units": total - retained,
            "unit": unit, "source_count": len(selected), "source_bytes": total_size,
            "warnings": ["같은 문단/행만 한 번 남깁니다. 의미가 비슷한 내용이나 서로 다른 값은 그대로 유지합니다.",
                         "병합본은 UTF-8로 저장하며 문단·행 구분은 LF를 사용합니다. 표의 셀 내부 문자는 유지하며 서식·수식 의미를 판단하거나 고치지 않습니다."]}
    if move_originals:
        plan["warnings"].append("병합본을 검증한 뒤 원본을 안전 보관으로 이동합니다. 작업 기록에서 원본을 복원해도 새 병합본은 남습니다.")
    plan["signature"] = ops._sign(plan, _KEY)
    _progress(progress_detail, "merge_preview", len(selected), len(selected), "병합 미리보기 준비 완료")
    return plan


def _verify_sources(plan):
    directory = ops._root(plan["root"])
    for item in plan["sources"]:
        path = ops._source(item["path"], directory)
        ops._verify(path, item)
        _check_source_metadata(path, item)
    return directory


def _write_output(path, raw, created):
    """Keep the directory descriptor anchored and never replace an existing file."""
    ops._safe_path(path)
    with ops._directory_descriptor(path.parent) as parent_descriptor:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | ops._NOFOLLOW | ops._BINARY
        if parent_descriptor is not None:
            descriptor = os.open(path.name, flags, 0o600, dir_fd=parent_descriptor)
        else:
            descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            created()
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        ops._sync_directory(path.parent)


def execute_merge(plan, *, progress_detail=None) -> dict:
    """Create a verified new merge, then optionally quarantine unchanged sources.

    Failures after exclusive creation report ``partial`` and retain both the new
    output (which might be incomplete on a disk error) and all surviving sources.
    A signed normal quarantine journal covers each retired original separately.
    """
    ops._check_signature(plan, _KEY)
    if plan.get("version") != 1:
        raise MergeError("지원하지 않는 병합 미리보기입니다.")
    identifier, output = plan["id"], Path(plan["output"])
    _record("execution.started", identifier, "started", message="내용 병합 시작", target=str(output), total=plan["source_count"])
    result = {"id": identifier, "status": "failed", "output": str(output),
              "output_created": False, "output_verified": False,
              "source_count": plan["source_count"], "removed_units": plan["removed_units"],
              "archive_journal": None, "error": None}
    try:
        directory = _verify_sources(plan)
        ops._safe_path(output)
        if output.exists() or output.is_symlink():
            raise MergeError("미리보기 이후 병합본 위치에 파일이 생겼습니다. 다시 미리보기를 만드세요.")
        raw = plan["content"].encode("utf-8")
        if len(raw) != plan["output_size"] or hashlib.sha256(raw).hexdigest() != plan["output_sha256"]:
            raise MergeError("병합본 내용 검증에 실패했습니다.")
        ops._capacity([{"target": str(output), "size": len(raw)}])
        _progress(progress_detail, "merge", 0, plan["source_count"], "새 병합본 저장 및 검증")
        # Mark creation separately so write failures still report preserved output.
        _write_output(output, raw, lambda: result.update(output_created=True))
        output_fingerprint = ops._fingerprint(output)
        if output_fingerprint["size"] != len(raw) or output_fingerprint["sha256"] != plan["output_sha256"]:
            raise MergeError("저장된 병합본의 검증에 실패했습니다. 원본은 이동하지 않았습니다.")
        result["output_verified"] = True
        for index, item in enumerate(plan["sources"], 1):
            _record("execution.item", identifier, "completed", source=item["path"], target=str(output),
                    message="병합본에 원본 내용 포함 · " + ("원본 이동 전" if plan["move_originals"] else "원본 유지"),
                    completed=index, total=plan["source_count"])
        if plan["move_originals"]:
            _verify_sources(plan)
            archive = ops.build_plan(directory, [item["path"] for item in plan["sources"]], "quarantine")
            # Building the archive must not silently accept changed source bytes.
            expected = {item["path"]: item for item in plan["sources"]}
            for item in archive["items"]:
                if any(item[key] != expected[item["source"]][key] for key in _FINGERPRINT_FIELDS):
                    raise MergeError("원본이 변경되어 이동하지 않았습니다. 새 병합본은 남겨두었습니다.")
            _verify_sources(plan)
            ops._verify(output, output_fingerprint)
            journal = ops.execute_plan(archive, progress_detail=progress_detail)
            result["archive_journal"] = journal
            ops._verify(output, output_fingerprint)
            if journal["status"] != "completed":
                result["status"] = "partial"
                result["error"] = "병합본은 생성했으며 일부 원본 이동을 완료하지 못했습니다. 작업 기록에서 파일별 결과를 확인하세요."
            else:
                result["status"] = "completed"
        else:
            result["status"] = "completed"
        _progress(progress_detail, "merge", plan["source_count"], plan["source_count"], "병합 작업 완료")
    except (OSError, ValueError) as exc:
        result["status"] = "partial" if result["output_created"] else "failed"
        result["error"] = str(exc)
    _record("execution.finished", identifier, result["status"], message="내용 병합 결과", target=str(output),
            error=result["error"], completed=plan["source_count"] if result["status"] == "completed" else 0,
            total=plan["source_count"])
    return result
