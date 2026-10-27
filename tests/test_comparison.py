from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
from threading import Event

from PIL import Image
import pytest

from folder_organizer import comparison
from folder_organizer.scanner import scan_folder


def entries(root: Path):
    return {item.name: item for item in scan_folder(root).files}


def test_exact_pair_streams_hashes_and_proves_actual_bytes(tmp_path):
    data = b"important original content\x00\x01"
    (tmp_path / "original.bin").write_bytes(data)
    (tmp_path / "different-name.bin").write_bytes(data)
    files = entries(tmp_path)
    result = comparison.compare_files(files["original.bin"], files["different-name.bin"])
    assert result["byte_identical"] is True
    assert result["left"]["sha256"] == result["right"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["left"]["size"] == len(data)
    assert result["left"]["created_source"] == files["original.bin"].created_source
    assert "완전 중복" in result["conclusion"]
    assert result["text_score"] is None and result["image_score"] is None
    assert result["left_preview"] is None


def test_same_normalized_executable_name_does_not_establish_equivalence(tmp_path):
    first, second = tmp_path / "Installer.exe", tmp_path / "Installer copy.exe"
    first.write_bytes(b"version one")
    second.write_bytes(b"version two")
    before = first.read_bytes(), second.read_bytes()
    files = entries(tmp_path)
    result = comparison.compare_files(files[first.name], files[second.name])
    assert result["byte_identical"] is False and result["name_score"] == 1
    assert result["text_score"] is None and result["image_score"] is None
    assert "바이트가 다릅니다" in result["conclusion"]
    assert any("중복 확률" in value for value in result["limitations"])
    assert (first.read_bytes(), second.read_bytes()) == before


def test_matching_digest_still_requires_actual_byte_comparison(tmp_path, monkeypatch):
    (tmp_path / "first.bin").write_bytes(b"aaaa")
    (tmp_path / "second.bin").write_bytes(b"bbbb")
    files = entries(tmp_path)
    monkeypatch.setattr(comparison, "_digest", lambda *args: "fabricated collision")
    assert comparison.compare_files(files["first.bin"], files["second.bin"])["byte_identical"] is False


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_text_diff_preserves_line_changes_and_reports_bounded_similarity(tmp_path, newline):
    first = newline.join(("계약서 초안입니다.", "This is a sufficiently long shared paragraph with several words.", "amount = 100", ""))
    second = first.replace("100", "150")
    # Write exact bytes so the host platform cannot translate LF to CRLF.
    (tmp_path / "draft.txt").write_bytes(first.encode("utf-8"))
    (tmp_path / "revision.txt").write_bytes(second.encode("utf-8"))
    files = entries(tmp_path)
    result = comparison.compare_files(files["draft.txt"], files["revision.txt"])
    assert result["byte_identical"] is False
    assert result["left_text"] == first and result["right_text"] == second
    assert "-amount = 100" in result["text_diff"] and "+amount = 150" in result["text_diff"]
    assert 0 < result["text_score"] < 1
    assert not result["text_diff_truncated"]


def test_line_ending_difference_is_not_an_exact_duplicate(tmp_path):
    text = "This is a sufficiently long shared paragraph with several words.\namount = 100\n"
    (tmp_path / "unix.txt").write_bytes(text.encode("utf-8"))
    windows_text = text.replace("\n", "\r\n")
    (tmp_path / "windows.txt").write_bytes(windows_text.encode("utf-8"))
    files = entries(tmp_path)
    result = comparison.compare_files(files["unix.txt"], files["windows.txt"])
    assert result["byte_identical"] is False
    assert result["left"]["sha256"] != result["right"]["sha256"]
    assert result["left_text"] == text and result["right_text"] == windows_text
    assert result["text_score"] == 1
    assert result["text_diff"] == []


def test_short_text_keeps_diff_but_matches_discovery_score_cutoff(tmp_path):
    from folder_organizer.matching import _text_signature
    (tmp_path / "first.txt").write_text("one two three four", encoding="utf-8")
    (tmp_path / "second.txt").write_text("one two three five", encoding="utf-8")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.txt"], files["second.txt"])
    assert _text_signature(files["first.txt"], comparison.MAX_TEXT_BYTES) is None
    assert result["text_score"] is None
    assert result["text_diff"] and result["left_text"]
    assert any("40문자 미만" in value for value in result["limitations"])


def test_long_text_discovery_and_pair_review_use_same_4096_word_limit(tmp_path):
    from folder_organizer.matching import find_similar
    common = " ".join(f"word{number}" for number in range(4100))
    (tmp_path / "first.txt").write_text(common + " original tail", encoding="utf-8")
    (tmp_path / "second.txt").write_text(common + " changed tail", encoding="utf-8")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.txt"], files["second.txt"])
    group, = find_similar(list(files.values()), methods=("text",))
    assert group.evidence["text_score"] == result["text_score"] == 1
    assert result["byte_identical"] is False
    assert any("생략" in value for value in result["limitations"])


def test_hardlink_pair_is_same_underlying_file_not_two_reclaimable_copies(tmp_path):
    import os
    first = tmp_path / "first.bin"
    first.write_bytes(b"one physical file")
    try:
        os.link(first, tmp_path / "alias.bin")
    except OSError:
        pytest.skip("Hardlinks unavailable")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.bin"], files["alias.bin"])
    assert result["byte_identical"] is True and result["same_physical_file"] is True
    assert "별도 중복 사본" in result["conclusion"]


def test_large_or_non_utf8_text_has_explicit_unsupported_reason(tmp_path, monkeypatch):
    (tmp_path / "first.txt").write_bytes(b"a" * 30)
    (tmp_path / "second.txt").write_bytes(b"b" * 30)
    files = entries(tmp_path)
    monkeypatch.setattr(comparison, "MAX_TEXT_BYTES", 20)
    result = comparison.compare_files(files["first.txt"], files["second.txt"])
    assert result["text_score"] is None and result["left_text"] is None
    assert any("20바이트 이하" in value for value in result["limitations"])
    assert result["byte_identical"] is False
    (tmp_path / "first.txt").write_bytes(b"\xff")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.txt"], files["second.txt"])
    assert any("UTF-8로 해석할 수 없어" in value for value in result["limitations"])


def test_text_preview_and_diff_limits_mark_omissions(tmp_path, monkeypatch):
    (tmp_path / "first.txt").write_text("line one\nline two\nline three\n", encoding="utf-8")
    (tmp_path / "second.txt").write_text("changed one\nchanged two\nchanged three\n", encoding="utf-8")
    files = entries(tmp_path)
    monkeypatch.setattr(comparison, "MAX_TEXT_PREVIEW_CHARS", 10)
    monkeypatch.setattr(comparison, "MAX_TEXT_DIFF_LINES", 3)
    result = comparison.compare_files(files["first.txt"], files["second.txt"])
    assert len(result["left_text"]) <= 10 and len(result["text_diff"]) == 3
    assert result["text_preview_truncated"] and result["text_diff_truncated"]
    assert any("생략" in issue for issue in result["limitations"])


def test_image_preview_is_bounded_and_exif_oriented(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (1000, 600), "blue").save(tmp_path / "first.jpg", exif=exif)
    Image.new("RGB", (600, 1000), "blue").save(tmp_path / "second.png")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.jpg"], files["second.png"])
    assert result["byte_identical"] is False
    assert result["image_metadata"]["left"]["width"] == 600
    assert result["image_metadata"]["left"]["height"] == 1000
    assert result["image_metadata"]["left"]["format"] == "JPEG"
    assert result["image_score"] > .99
    for key in ("left_preview", "right_preview"):
        with Image.open(io.BytesIO(result[key])) as preview:
            assert preview.format == "PNG" and max(preview.size) <= 512
    assert any("100%" in issue for issue in result["limitations"])


def test_uniform_images_different_colors_do_not_get_perfect_score(tmp_path):
    Image.new("RGB", (30, 30), "black").save(tmp_path / "first.png")
    Image.new("RGB", (30, 30), "white").save(tmp_path / "second.png")
    files = entries(tmp_path)
    result = comparison.compare_files(files["first.png"], files["second.png"])
    assert result["image_score"] == 0
    assert result["byte_identical"] is False


def test_corrupt_and_oversized_images_are_not_unrestricted_decodes(tmp_path, monkeypatch):
    (tmp_path / "broken.jpg").write_bytes(b"not an image")
    Image.new("RGB", (100, 100), "blue").save(tmp_path / "valid.png")
    files = entries(tmp_path)
    monkeypatch.setattr(comparison, "MAX_IMAGE_PIXELS", 5_000)
    result = comparison.compare_files(files["broken.jpg"], files["valid.png"])
    assert result["byte_identical"] is False
    assert result["left_preview"] is None and result["right_preview"] is None
    assert any("ピクセル" in issue or "픽셀" in issue for issue in result["limitations"])
    assert any("미리보기를 만들 수 없습니다" in issue for issue in result["limitations"])


def test_animated_image_reports_first_frame_only_without_counting_all_frames(tmp_path):
    first, second = Image.new("RGB", (20, 20), "red"), Image.new("RGB", (20, 20), "blue")
    first.save(tmp_path / "moving.gif", save_all=True, append_images=[second], duration=100, loop=0)
    first.save(tmp_path / "still.png")
    files = entries(tmp_path)
    result = comparison.compare_files(files["moving.gif"], files["still.png"])
    assert result["image_metadata"]["left"]["animated"]
    assert result["image_metadata"]["left"]["frames"] is None
    assert any("첫 프레임만" in issue for issue in result["limitations"])


def test_stale_replaced_or_linked_snapshot_is_not_compared(tmp_path):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"original")
    second.write_bytes(b"original")
    files = entries(tmp_path)
    first.write_bytes(b"changed content")
    with pytest.raises(OSError):
        comparison.compare_files(files[first.name], files[second.name])
    files = entries(tmp_path)
    first.unlink()
    try:
        first.symlink_to(second)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    with pytest.raises(OSError):
        comparison.compare_files(files[first.name], files[second.name])


def test_cancellation_during_hash_never_returns_partial_evidence(tmp_path, monkeypatch):
    (tmp_path / "first.bin").write_bytes(b"stream this content")
    (tmp_path / "second.bin").write_bytes(b"stream this content")
    files = entries(tmp_path)
    event = Event()
    real_open = comparison._open_snapshot

    @contextmanager
    def cancellable_open(entry):
        with real_open(entry) as stream:
            class Stream:
                def read(self, size):
                    result = stream.read(size)
                    event.set()
                    return result
            yield Stream()

    monkeypatch.setattr(comparison, "_open_snapshot", cancellable_open)
    with pytest.raises(comparison.ComparisonCancelled):
        comparison.compare_files(files["first.bin"], files["second.bin"], cancel=event)


def test_change_during_preview_invalidates_whole_result(tmp_path, monkeypatch):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"one two three four original text")
    second.write_bytes(b"one two three four changed text")
    files = entries(tmp_path)
    real_image = comparison._image

    def image_then_change(entry, cancel):
        result = real_image(entry, cancel)
        if entry.name == second.name:
            first.write_bytes(b"edited while comparison running")
        return result

    monkeypatch.setattr(comparison, "_image", image_then_change)
    with pytest.raises(OSError):
        comparison.compare_files(files[first.name], files[second.name])
