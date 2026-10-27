from pathlib import Path
from threading import Event

import pytest

from folder_organizer.matching import find_duplicates, find_similar
from folder_organizer.scanner import scan_folder


def write(root: Path, name: str, value: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def test_duplicate_groups_require_matching_bytes_and_not_just_sizes(tmp_path):
    write(tmp_path, "a.txt", b"same content")
    write(tmp_path, "b.txt", b"same content")
    write(tmp_path, "c.txt", b"DIFF content")
    groups = find_duplicates(scan_folder(tmp_path).files)
    assert len(groups) == 1
    assert {Path(p).name for p in groups[0].paths} == {"a.txt", "b.txt"}
    assert groups[0].kind == "duplicate"


def test_hash_collision_still_requires_byte_comparison(tmp_path, monkeypatch):
    import folder_organizer.matching as matching
    write(tmp_path, "a", b"aaaa")
    write(tmp_path, "b", b"bbbb")
    monkeypatch.setattr(matching, "_digest", lambda *_: "fabricated collision")
    assert find_duplicates(scan_folder(tmp_path).files) == []


def test_changed_missing_and_symlink_replacements_are_not_duplicates(tmp_path):
    first = write(tmp_path, "a.txt", b"same")
    second = write(tmp_path, "b.txt", b"same")
    files = scan_folder(tmp_path).files
    second.write_bytes(b"different content")
    assert find_duplicates(files) == []
    second.unlink()
    assert find_duplicates(files) == []
    try:
        second.symlink_to(first)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    assert find_duplicates(files) == []


def test_unreadable_files_cannot_generate_an_empty_digest(tmp_path, monkeypatch):
    import folder_organizer.matching as matching
    write(tmp_path, "a", b"content")
    write(tmp_path, "b", b"content")
    real_open = matching._open_snapshot

    def denied(entry):
        if entry.name == "b":
            raise PermissionError("denied")
        return real_open(entry)

    monkeypatch.setattr(matching, "_open_snapshot", denied)
    messages = []
    assert find_duplicates(scan_folder(tmp_path).files, progress=messages.append) == []
    assert any("denied" in message for message in messages)


def test_hardlink_alias_does_not_claim_reclaimable_duplicate(tmp_path):
    import os
    original = write(tmp_path, "a", b"same")
    try:
        os.link(original, tmp_path / "hardlink")
    except OSError:
        pytest.skip("Hardlinks unavailable")
    assert find_duplicates(scan_folder(tmp_path).files) == []


def test_empty_files_are_verified_exact_matches(tmp_path):
    write(tmp_path, "a", b"")
    write(tmp_path, "b", b"")
    groups = find_duplicates(scan_folder(tmp_path).files)
    assert len(groups) == 1
    assert "0 bytes" in groups[0].reason


def test_cancelled_matching_returns_no_partial_suggestions(tmp_path):
    write(tmp_path, "report.txt", b"same")
    write(tmp_path, "report copy.txt", b"same")
    event = Event()
    files = scan_folder(tmp_path).files
    assert find_duplicates(files, cancel=event, progress=lambda _: event.set()) == []
    assert find_similar(files, cancel=event) == []


def test_name_similarity_is_explicitly_only_a_candidate(tmp_path):
    write(tmp_path, "invoice.txt", b"one")
    write(tmp_path, "invoice copy.txt", b"two")
    write(tmp_path, "invoice.exe", b"three")
    groups = find_similar(scan_folder(tmp_path).files)
    assert len(groups) == 1
    assert groups[0].kind == "similar_name"
    assert "NOT established" in groups[0].reason
    assert len(groups[0].paths) == 2


def test_text_similarity_checks_content_with_different_filenames(tmp_path):
    words = "This document contains a long enough paragraph for comparing actual text content safely. "
    write(tmp_path, "apple.txt", words.encode())
    write(tmp_path, "zebra.txt", (words + "Updated.").encode())
    groups = find_similar(scan_folder(tmp_path).files)
    assert len(groups) == 1
    assert groups[0].kind == "similar_text"


def test_similarity_limit_is_bounded_and_reported(tmp_path):
    for index in range(4):
        write(tmp_path, f"report ({index}).txt", str(index).encode())
    messages = []
    groups = find_similar(scan_folder(tmp_path).files, max_pairs=1, progress=messages.append)
    assert len(groups) <= 1
    assert any("limit reached" in message for message in messages)


def test_similarity_threshold_validation(tmp_path):
    with pytest.raises(ValueError):
        find_similar([], threshold=.1)
    with pytest.raises(ValueError):
        find_similar([], max_pairs=0)


def test_solid_different_images_are_not_false_visual_matches(tmp_path):
    from PIL import Image
    Image.new("RGB", (40, 40), "black").save(tmp_path / "apple.png")
    Image.new("RGB", (40, 40), "white").save(tmp_path / "zebra.png")
    assert find_similar(scan_folder(tmp_path).files) == []


def test_resized_same_image_is_a_visual_candidate(tmp_path):
    from PIL import Image
    image = Image.new("RGB", (40, 40), "blue")
    for x in range(40):
        for y in range(40):
            image.putpixel((x, y), (x * 5, y * 5, (x + y) * 2))
    image.save(tmp_path / "apple.png")
    image.resize((80, 80)).save(tmp_path / "zebra.jpg")
    groups = find_similar(scan_folder(tmp_path).files)
    assert len(groups) == 1
    assert groups[0].kind == "similar_image"
