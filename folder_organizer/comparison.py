"""Bounded, read-only evidence for a directly compared pair of files.

Name, text and image scores are descriptive signals, never a deletion decision
or a probability that two files are interchangeable. Only matching bytes prove
an exact duplicate. No application, macro or external viewer is executed here.
"""
from __future__ import annotations

import difflib
import hashlib
import io
from threading import Event
import warnings

from .matching import CHUNK_SIZE, IMAGE_SUFFIXES, TEXT_SUFFIXES, _has_alpha, _image_similarity, _normal_name, _open_snapshot, _visual_signature
from .models import FileEntry

MAX_TEXT_BYTES = 262_144
MAX_TEXT_WORDS = 4_096
MAX_TEXT_PREVIEW_CHARS = 48_000
MAX_TEXT_DIFF_INPUT_LINES = 1_000
MAX_TEXT_DIFF_LINES = 300
MAX_TEXT_DIFF_LINE_CHARS = 500
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
PREVIEW_SIZE = (512, 512)


class ComparisonCancelled(ValueError):
    """The user cancelled comparison; no incomplete proof should be displayed."""


def _cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ComparisonCancelled("파일 비교를 중단했습니다.")


def _digest(entry: FileEntry, cancel: Event | None) -> str:
    digest = hashlib.sha256()
    with _open_snapshot(entry) as stream:
        while True:
            _cancel(cancel)
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    _cancel(cancel)
    return digest.hexdigest()


def _same_bytes(first: FileEntry, second: FileEntry, cancel: Event | None) -> bool:
    with _open_snapshot(first) as left, _open_snapshot(second) as right:
        while True:
            _cancel(cancel)
            a, b = left.read(CHUNK_SIZE), right.read(CHUNK_SIZE)
            if a != b:
                return False
            if not a:
                return True


def _metadata(entry: FileEntry, digest: str) -> dict:
    return {
        "path": entry.path, "name": entry.name, "relative_path": entry.relative_path,
        "size": entry.size, "modified": entry.modified, "created": entry.created,
        "created_source": entry.created_source, "suffix": entry.suffix,
        "kind": entry.kind, "sha256": digest,
    }


def _text(entry: FileEntry, cancel: Event | None) -> tuple[str | None, str | None]:
    if entry.suffix.casefold() not in TEXT_SUFFIXES:
        return None, "지원되는 UTF-8 텍스트 형식이 아닙니다."
    if entry.size > MAX_TEXT_BYTES:
        return None, f"텍스트 내용 비교는 {MAX_TEXT_BYTES:,}바이트 이하에 한정합니다."
    with _open_snapshot(entry) as stream:
        _cancel(cancel)
        raw = stream.read(MAX_TEXT_BYTES + 1)
    if len(raw) > MAX_TEXT_BYTES:
        return None, "텍스트 읽기 제한을 초과했습니다."
    if b"\0" in raw:
        return None, "널 바이트가 있어 텍스트로 해석하지 않았습니다."
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeDecodeError:
        return None, "UTF-8로 해석할 수 없어 내용 유사도를 계산하지 않았습니다."


def _text_score(left: str, right: str) -> tuple[float | None, bool]:
    a, b = left.split(), right.split()
    truncated = len(a) > MAX_TEXT_WORDS or len(b) > MAX_TEXT_WORDS
    if len(" ".join(a)) < 40 or len(" ".join(b)) < 40:
        return None, truncated
    a, b = a[:MAX_TEXT_WORDS], b[:MAX_TEXT_WORDS]
    if len(a) < 3 or len(b) < 3:
        return None, truncated
    a_shingles = {tuple(a[index:index + 3]) for index in range(len(a) - 2)}
    b_shingles = {tuple(b[index:index + 3]) for index in range(len(b) - 2)}
    score = min(2 * len(a_shingles & b_shingles) / (len(a_shingles) + len(b_shingles)),
                min(len(a), len(b)) / max(len(a), len(b)))
    return score, truncated


def _text_diff(left: str, right: str, cancel: Event | None) -> tuple[list[str], bool]:
    a, b = left.splitlines(), right.splitlines()
    truncated = len(a) > MAX_TEXT_DIFF_INPUT_LINES or len(b) > MAX_TEXT_DIFF_INPUT_LINES
    a, b = a[:MAX_TEXT_DIFF_INPUT_LINES], b[:MAX_TEXT_DIFF_INPUT_LINES]
    lines = []
    # Line alignment is bounded independently of the bytes and display limits.
    for line in difflib.unified_diff(a, b, fromfile="왼쪽", tofile="오른쪽", lineterm="", n=3):
        _cancel(cancel)
        if len(lines) >= MAX_TEXT_DIFF_LINES:
            truncated = True
            break
        if len(line) > MAX_TEXT_DIFF_LINE_CHARS:
            line = line[:MAX_TEXT_DIFF_LINE_CHARS] + " … [행 일부 생략]"
            truncated = True
        lines.append(line)
    return lines, truncated


def _image(entry: FileEntry, cancel: Event | None) -> tuple[bytes | None, dict | None, tuple | None, str | None]:
    if entry.suffix.casefold() not in IMAGE_SUFFIXES:
        return None, None, None, "지원되는 이미지 형식이 아닙니다."
    if entry.size > MAX_IMAGE_BYTES:
        return None, None, None, "이미지 미리보기는 64 MiB 이하 파일에 한정합니다."
    from PIL import Image, ImageOps
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with _open_snapshot(entry) as stream, Image.open(stream) as source:
                if source.width <= 0 or source.height <= 0 or source.width * source.height > MAX_IMAGE_PIXELS:
                    return None, None, None, "이미지 픽셀 수 제한(4,000만)을 초과했습니다."
                _cancel(cancel)
                animated = bool(getattr(source, "is_animated", False))
                with ImageOps.exif_transpose(source) as image:
                    _cancel(cancel)
                    metadata = {"width": image.width, "height": image.height,
                                "format": source.format, "mode": image.mode,
                                "has_alpha": _has_alpha(image), "animated": animated,
                                "frames": None if animated else 1,
                                "frame_note": "첫 프레임만 비교하며 총 프레임 수를 순회하지 않습니다." if animated else "단일 이미지"}
                    signature = _visual_signature(image)
                    with image.copy() as preview:
                        preview.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
                        output = io.BytesIO()
                        with preview.convert("RGBA" if metadata["has_alpha"] else "RGB") as encoded:
                            encoded.save(output, format="PNG")
                    _cancel(cancel)
                    return output.getvalue(), metadata, signature, None
    except ComparisonCancelled:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, ValueError, SyntaxError) as exc:
        # Snapshot failures must not be mistaken for unsupported image content.
        # Re-open below (and once more before return) to surface changed paths.
        with _open_snapshot(entry):
            pass
        return None, None, None, f"이미지 미리보기를 만들 수 없습니다: {type(exc).__name__}."


def compare_files(first: FileEntry, second: FileEntry, cancel: Event | None = None) -> dict:
    """Compare scanned file snapshots without modifying or executing either file.

    Hashing always streams the whole files. Text and image previews are bounded
    and explicitly report skipped or omitted data. Missing, replaced or changed
    snapshots raise OSError. Cancellation raises ComparisonCancelled.
    """
    _cancel(cancel)
    left_digest, right_digest = _digest(first, cancel), _digest(second, cancel)
    byte_identical = (first.size == second.size and left_digest == right_digest
                      and _same_bytes(first, second, cancel))
    name_left, name_right = _normal_name(first), _normal_name(second)
    name_score = difflib.SequenceMatcher(None, name_left[:1024], name_right[:1024], autojunk=False).ratio() if name_left and name_right else None
    same_physical = bool(first.inode and (first.device, first.inode) == (second.device, second.inode))
    result = {
        "left": _metadata(first, left_digest), "right": _metadata(second, right_digest),
        "byte_identical": byte_identical, "same_physical_file": same_physical, "name_score": name_score,
        "text_score": None, "image_score": None, "left_text": None, "right_text": None,
        "text_diff": [], "text_diff_truncated": False, "text_preview_truncated": False,
        "left_preview": None, "right_preview": None,
        "image_metadata": {"left": None, "right": None},
        "limitations": [],
        "conclusion": "크기·SHA-256·실제 바이트가 모두 일치하는 완전 중복입니다." if byte_identical else "파일의 실제 바이트가 다릅니다. 유사한 근거가 있어도 같은 파일은 아닙니다.",
    }
    if same_physical:
        result["conclusion"] = "같은 물리적 파일을 가리키는 경로입니다. 바이트가 같아도 별도 중복 사본으로 계산하지 않습니다."
        result["limitations"].append("동일 inode·device를 참조하는 하드링크 또는 같은 경로입니다. 경로 하나를 제거해도 별도 사본 한 개만큼의 공간을 확보하지 않습니다.")
    _cancel(cancel)
    left_text, left_issue = _text(first, cancel)
    right_text, right_issue = _text(second, cancel)
    if left_text is not None and right_text is not None:
        score, truncated_words = _text_score(left_text, right_text)
        result["text_score"] = score
        result["left_text"], result["right_text"] = left_text[:MAX_TEXT_PREVIEW_CHARS], right_text[:MAX_TEXT_PREVIEW_CHARS]
        result["text_preview_truncated"] = len(left_text) > MAX_TEXT_PREVIEW_CHARS or len(right_text) > MAX_TEXT_PREVIEW_CHARS
        result["text_diff"], result["text_diff_truncated"] = _text_diff(left_text, right_text, cancel)
        result["limitations"].append("텍스트 점수는 앞 4,096단어의 3단어 묶음 겹침·단어 수 비율입니다. 의미나 문서 완전성을 판정하지 않습니다.")
        if score is None:
            result["limitations"].append("공백 정규화 후 40문자 미만 또는 3단어 미만인 텍스트는 내용 유사도 점수 대신 바이트 결과와 차이를 확인하세요.")
        if truncated_words or result["text_preview_truncated"] or result["text_diff_truncated"]:
            result["limitations"].append("텍스트 일부가 비교 또는 화면 표시 제한으로 생략되었습니다. 전체 바이트 동일 여부는 별도로 검증했습니다.")
    else:
        if first.suffix.casefold() in TEXT_SUFFIXES or second.suffix.casefold() in TEXT_SUFFIXES:
            result["limitations"].append(f"텍스트 비교 미지원: 왼쪽 {left_issue or '읽기 가능'}, 오른쪽 {right_issue or '읽기 가능'}")
    left_png, left_metadata, left_signature, left_image_issue = _image(first, cancel)
    right_png, right_metadata, right_signature, right_image_issue = _image(second, cancel)
    result["left_preview"], result["right_preview"] = left_png, right_png
    result["image_metadata"] = {"left": left_metadata, "right": right_metadata}
    if left_signature and right_signature:
        a, b = left_signature, right_signature
        if (score := _image_similarity(a, b)) is not None:
            result["image_score"] = score
        else:
            result["limitations"].append("종횡비 차이가 커 이미지 유사도 점수를 표시하지 않습니다.")
        result["limitations"].append("이미지 점수는 축소된 구조·색·투명도 비교입니다. 100%여도 원본 바이트 동일이나 같은 피사체를 보증하지 않습니다.")
        if (left_metadata or {}).get("animated") or (right_metadata or {}).get("animated"):
            result["limitations"].append("애니메이션·다중 프레임 이미지는 첫 프레임만 미리보고 비교합니다.")
    elif first.suffix.casefold() in IMAGE_SUFFIXES or second.suffix.casefold() in IMAGE_SUFFIXES:
        result["limitations"].append(f"이미지 비교 미지원: 왼쪽 {left_image_issue or '읽기 가능'}, 오른쪽 {right_image_issue or '읽기 가능'}")
    result["limitations"].append("파일 이름 점수는 이름의 닮은 정도이며 중복 확률·불필요한 파일이라는 뜻이 아닙니다.")
    for entry in (first, second):
        _cancel(cancel)
        with _open_snapshot(entry):
            pass
    _cancel(cancel)
    return result
