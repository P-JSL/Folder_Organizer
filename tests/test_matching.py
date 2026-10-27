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


def test_duplicate_evidence_proves_bytes_independent_of_name_or_metadata(tmp_path):
    import hashlib
    import os
    first = write(tmp_path, "original.bin", b"same original bytes")
    second = write(tmp_path, "renamed.txt", first.read_bytes())
    os.utime(first, (1000, 1000))
    os.utime(second, (2000, 2000))
    group, = find_duplicates(scan_folder(tmp_path).files)
    assert group.evidence["byte_identical"] is True
    assert group.evidence["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert group.evidence["matched_signals"] == ["exact"]
    assert group.evidence["name_score"] is None
    assert group.to_dict()["evidence"] == group.evidence


def test_exact_copies_are_separate_from_similarity_even_with_similar_names(tmp_path):
    write(tmp_path, "invoice.txt", b"identical bytes")
    write(tmp_path, "invoice copy.txt", b"identical bytes")
    files = scan_folder(tmp_path).files
    assert len(find_duplicates(files)) == 1
    report = {}
    assert find_similar(files, report=report) == []
    assert report["exact_pairs_excluded"] == 1
    assert report["compared_pairs"] == 1
    assert report["files_considered"] == 2


def test_identical_encoded_image_under_different_extensions_is_not_merely_similar(tmp_path):
    from PIL import Image
    Image.new("RGB", (30, 30), "red").save(tmp_path / "photo.png")
    (tmp_path / "photo copy.jpg").write_bytes((tmp_path / "photo.png").read_bytes())
    files = scan_folder(tmp_path).files
    assert len(find_duplicates(files)) == 1
    assert find_similar(files) == []


def test_normalized_name_score_one_does_not_hide_text_evidence(tmp_path):
    original = "This document contains a long enough paragraph for comparing actual text content safely. "
    write(tmp_path, "report.txt", original.encode())
    write(tmp_path, "report copy.txt", (original + "Updated.").encode())
    group, = find_similar(scan_folder(tmp_path).files)
    assert group.kind == "similar_text"
    assert group.evidence["name_score"] == 1
    assert .9 <= group.evidence["text_score"] < 1
    assert group.score == group.evidence["text_score"]
    assert group.evidence["matched_signals"] == ["name", "text"]
    assert group.evidence["byte_identical"] is False


def test_normalized_name_score_one_does_not_hide_image_evidence(tmp_path):
    from PIL import Image
    image = Image.new("RGB", (40, 40), (20, 20, 20))
    image.save(tmp_path / "photo.png")
    Image.new("RGB", (40, 40), (25, 25, 25)).save(tmp_path / "photo copy.png")
    group, = find_similar(scan_folder(tmp_path).files)
    assert group.kind == "similar_image"
    assert group.evidence["name_score"] == 1
    assert .9 <= group.evidence["image_score"] < 1
    assert group.score == group.evidence["image_score"]
    assert group.evidence["matched_signals"] == ["name", "image"]
    assert group.evidence["byte_identical"] is False


def test_same_name_unrelated_images_show_name_only_and_low_content_score(tmp_path):
    from PIL import Image
    Image.new("RGB", (40, 40), "black").save(tmp_path / "photo.png")
    Image.new("RGB", (40, 40), "white").save(tmp_path / "photo copy.png")
    group, = find_similar(scan_folder(tmp_path).files)
    assert group.kind == "similar_name"
    assert group.evidence["name_score"] == 1
    assert group.evidence["image_score"] == 0
    assert group.evidence["matched_signals"] == ["name"]
    assert group.evidence["byte_identical"] is False
    assert any("기준" in item and "미만" in item for item in group.evidence["limitations"])


def test_same_name_executables_do_not_claim_program_equivalence(tmp_path):
    write(tmp_path, "Setup.exe", b"different program A")
    write(tmp_path, "Setup copy.exe", b"another program B")
    group, = find_similar(scan_folder(tmp_path).files)
    assert group.kind == "similar_name"
    assert group.evidence["text_score"] is None
    assert group.evidence["image_score"] is None
    assert group.evidence["byte_identical"] is False
    assert any("대체 가능성" in item for item in group.evidence["limitations"])


def test_content_only_mode_does_not_return_name_only_candidates(tmp_path):
    write(tmp_path, "report.txt", b"short first version")
    write(tmp_path, "report copy.txt", b"short second version")
    files = scan_folder(tmp_path).files
    assert len(find_similar(files, methods=("name",))) == 1
    assert find_similar(files, methods=("text",)) == []


def test_explicit_text_threshold_is_independent_and_not_silently_floored(tmp_path):
    original = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon"
    revision = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi newer changed revised words now added"
    write(tmp_path, "apple.txt", original.encode())
    write(tmp_path, "zebra.txt", revision.encode())
    files = scan_folder(tmp_path).files
    assert find_similar(files, methods=("text",)) == []
    group, = find_similar(files, methods=("text",), text_threshold=.6, name_threshold=.99)
    assert group.kind == "similar_text"
    assert .6 <= group.evidence["text_score"] < .9
    assert group.evidence["text_threshold"] == .6
    assert group.evidence["name_score"] is None


def test_explicit_image_threshold_is_independent_and_not_silently_floored(tmp_path):
    from PIL import Image
    Image.new("RGB", (40, 40), (0, 0, 0)).save(tmp_path / "apple.png")
    Image.new("RGB", (40, 40), (30, 30, 30)).save(tmp_path / "zebra.png")
    files = scan_folder(tmp_path).files
    assert find_similar(files, methods=("image",)) == []
    group, = find_similar(files, methods=("image",), image_threshold=.85)
    assert group.kind == "similar_image"
    assert .85 <= group.score < .9
    assert group.evidence["image_threshold"] == .85


@pytest.mark.parametrize("methods", [(), ("unknown",), "name", None])
def test_invalid_similarity_methods_rejected(methods):
    with pytest.raises(ValueError):
        find_similar([], methods=methods)


@pytest.mark.parametrize("value", [float("nan"), .49, 1.1, True, "0.9"])
def test_invalid_independent_threshold_rejected(value):
    with pytest.raises(ValueError):
        find_similar([], image_threshold=value)


def test_report_persists_effective_thresholds_and_incomplete_pair_budget(tmp_path):
    for index in range(4):
        write(tmp_path, f"report ({index}).txt", str(index).encode())
    report = {"stale": "old run"}
    groups = find_similar(scan_folder(tmp_path).files, methods=("name",), max_pairs=1,
                          name_threshold=.8, text_threshold=.91, image_threshold=.92, report=report)
    assert len(groups) == 1
    assert report["methods"] == ["name"]
    assert report["thresholds"] == {"name": .8, "text": .91, "image": .92}
    assert report["compared_pairs"] == report["max_pairs"] == 1
    assert report["limit_reached"] is True
    assert any("limit reached" in warning for warning in report["warnings"])
    assert "stale" not in report


def test_warning_report_is_bounded_even_when_many_text_pairs_are_unsupported(tmp_path):
    for index in range(21):
        write(tmp_path, f"report ({index}).txt", str(index).encode())
    report = {}
    find_similar(scan_folder(tmp_path).files, report=report)
    assert report["compared_pairs"] == 210
    assert len(report["warnings"]) == 100
    assert report["limit_reached"] is False


def test_same_size_unavailable_byte_comparison_stays_unknown(tmp_path, monkeypatch):
    import folder_organizer.matching as matching
    write(tmp_path, "report.txt", b"aaa")
    write(tmp_path, "report copy.txt", b"bbb")

    def denied(*args):
        raise PermissionError("read temporarily unavailable")

    monkeypatch.setattr(matching, "_equal", denied)
    report = {}
    group, = find_similar(scan_folder(tmp_path).files, methods=("name",), report=report)
    assert group.evidence["byte_identical"] is None
    assert any("바이트 비교" in item for item in group.evidence["limitations"])
    assert report["skipped_count"] == 1


def test_late_byte_verification_still_excludes_copies_if_exact_pass_missed_them(tmp_path, monkeypatch):
    import folder_organizer.matching as matching
    write(tmp_path, "report.txt", b"same bytes")
    write(tmp_path, "report copy.txt", b"same bytes")
    monkeypatch.setattr(matching, "find_duplicates", lambda *args, **kwargs: [])
    report = {}
    assert find_similar(scan_folder(tmp_path).files, methods=("name",), report=report) == []
    assert report["exact_pairs_excluded"] == 1


def test_similarity_cancel_report_does_not_return_partial_suggestions(tmp_path):
    write(tmp_path, "report.txt", b"same")
    write(tmp_path, "report copy.txt", b"same")
    event = Event()
    report = {}
    assert find_similar(scan_folder(tmp_path).files, cancel=event, report=report,
                        progress=lambda _: event.set()) == []
    assert report["cancelled"] is True


def test_similar_pairs_are_not_transitively_merged(tmp_path):
    write(tmp_path, "abcdef.txt", b"a")
    write(tmp_path, "abcdefghij.txt", b"b")
    write(tmp_path, "efghij.txt", b"c")
    groups = find_similar(scan_folder(tmp_path).files, threshold=.70, methods=("name",))
    assert len(groups) == 2
    assert all(len(group.paths) == 2 for group in groups)
    assert not any({Path(path).name for path in group.paths} == {"abcdef.txt", "efghij.txt"} for group in groups)


def test_text_score_one_for_bounded_prefix_is_not_a_byte_duplicate(tmp_path):
    prefix = "word " * 4096
    write(tmp_path, "apple.txt", (prefix + "first distinct ending").encode())
    write(tmp_path, "zebra.txt", (prefix + "second changed ending").encode())
    group, = find_similar(scan_folder(tmp_path).files, methods=("text",))
    assert group.kind == "similar_text"
    assert group.score == 1
    assert group.evidence["byte_identical"] is False
    assert any("4,096" in item for item in group.evidence["limitations"])


def test_exif_oriented_equivalent_pixels_are_similar_without_byte_identity(tmp_path):
    from PIL import Image
    source = Image.new("RGB", (40, 20))
    for x in range(40):
        for y in range(20):
            source.putpixel((x, y), (x * 5, y * 10, (x + y) * 3))
    source.save(tmp_path / "apple.png")
    rotated = source.transpose(Image.Transpose.ROTATE_90)
    orientation = Image.Exif()
    orientation[274] = 6
    rotated.save(tmp_path / "zebra.png", exif=orientation)
    group, = find_similar(scan_folder(tmp_path).files, methods=("image",))
    assert group.kind == "similar_image"
    assert group.score == 1
    assert group.evidence["byte_identical"] is False


def test_hardlink_aliases_are_omitted_from_similarity_counts(tmp_path):
    import os
    first = write(tmp_path, "invoice.txt", b"first version")
    write(tmp_path, "invoice copy.txt", b"second version")
    try:
        os.link(first, tmp_path / "invoice alias.txt")
    except OSError:
        pytest.skip("Hardlinks unavailable")
    report = {}
    groups = find_similar(scan_folder(tmp_path).files, methods=("name",), report=report)
    assert report["files_considered"] == 2
    assert report["compared_pairs"] == 1
    assert len(groups) <= 1


def test_file_changed_during_content_comparison_is_not_suggested(tmp_path):
    write(tmp_path, "report.txt", b"one version")
    second = write(tmp_path, "report copy.txt", b"two version")
    files = scan_folder(tmp_path).files

    def change_on_compare(message):
        if message.startswith("Comparing "):
            second.write_bytes(b"a new larger live edit")

    report = {}
    assert find_similar(files, methods=("name",), report=report, progress=change_on_compare) == []
    assert report["skipped_count"] >= 1


def test_same_first_gif_frame_is_explicitly_not_whole_animation_identity(tmp_path):
    from PIL import Image
    first_frame = Image.new("RGB", (20, 20), "red")
    for name, later_color in (("apple.gif", "blue"), ("zebra.gif", "green")):
        first_frame.save(tmp_path / name, save_all=True,
                         append_images=[Image.new("RGB", (20, 20), later_color)], duration=50, loop=0)
    group, = find_similar(scan_folder(tmp_path).files, methods=("image",))
    assert group.kind == "similar_image" and group.score == 1
    assert group.evidence["byte_identical"] is False
    assert any("첫 프레임" in item and "움직임" in item for item in group.evidence["limitations"])
