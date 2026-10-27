"""Independent regressions for review evidence and retention decisions."""
import io
import os

from PIL import Image
import pytest

from folder_organizer import comparison, matching
from folder_organizer.retention import choose_duplicate_copies
from folder_organizer.scanner import scan_folder


def test_retention_rechecks_an_earlier_member_after_later_hashes(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"original shared content")
    second.write_bytes(first.read_bytes())
    files = scan_folder(tmp_path).files
    groups = matching.find_duplicates(files)

    def edit_earlier_file(message):
        if message.endswith("second.txt"):
            first.write_bytes(b"edited while the later file was being verified")

    selection = choose_duplicate_copies(groups, files, progress=edit_earlier_file)
    assert selection["selected"] == []
    assert set(selection["kept"]) == {str(first), str(second)}
    assert selection["warnings"]
    assert first.read_bytes() != second.read_bytes()


@pytest.mark.parametrize("rgb", [(255, 0, 0), (255, 255, 255), (127, 127, 127)])
def test_opacity_difference_is_visible_and_not_a_perfect_image_match(tmp_path, rgb):
    transparent = tmp_path / "apple.png"
    opaque = tmp_path / "zebra.png"
    Image.new("RGBA", (32, 32), (*rgb, 0)).save(transparent)
    Image.new("RGBA", (32, 32), (*rgb, 255)).save(opaque)
    files = {entry.name: entry for entry in scan_folder(tmp_path).files}
    groups = matching.find_similar(list(files.values()), methods=("image",))
    assert groups == [], "Discarding alpha made visually different files match perfectly"
    result = comparison.compare_files(files[transparent.name], files[opaque.name])
    assert result["byte_identical"] is False
    assert result["image_score"] < 0.9
    assert result["image_metadata"]["left"]["has_alpha"] is True
    assert result["left_preview"] != result["right_preview"]
    with Image.open(io.BytesIO(result["left_preview"])) as preview:
        assert preview.mode == "RGBA" and preview.getpixel((0, 0))[3] == 0


def test_ready_retention_group_is_rechecked_after_later_groups_finish(tmp_path):
    paths = {name: tmp_path / name for name in ("a1.txt", "a2.txt", "b1.txt", "b2.txt")}
    for name, path in paths.items():
        path.write_bytes(b"first duplicate group" if name.startswith("a") else b"second duplicate group")
    files = scan_folder(tmp_path).files
    groups = matching.find_duplicates(files)

    def edit_earlier_group(message):
        if message.endswith("b2.txt"):
            paths["a1.txt"].write_bytes(b"a new user edit after its group was checked")

    selection = choose_duplicate_copies(groups, files, progress=edit_earlier_group)
    assert selection["selected"] == [str(paths["b2.txt"])]
    assert {str(paths["a1.txt"]), str(paths["a2.txt"])} <= set(selection["kept"])
    assert [decision["status"] for decision in selection["decisions"]] == ["skipped", "ready"]


def test_retention_final_pass_detects_same_size_edit_with_preserved_mtime(tmp_path):
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"old verified content")
    second.write_bytes(first.read_bytes())
    files = scan_folder(tmp_path).files
    groups = matching.find_duplicates(files)
    before = first.stat()

    def edit_earlier_file(message):
        if message.endswith("second.txt"):
            first.write_bytes(b"new modified content")
            os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns))

    selection = choose_duplicate_copies(groups, files, progress=edit_earlier_file)
    assert first.stat().st_size == before.st_size
    assert first.stat().st_mtime_ns == before.st_mtime_ns
    assert selection["selected"] == []
    assert selection["warnings"]
