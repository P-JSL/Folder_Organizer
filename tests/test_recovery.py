from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest

from folder_organizer import recovery


def make_zip(path: Path, entries: dict[str, bytes], compression=zipfile.ZIP_STORED) -> bytes:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return path.read_bytes()


def test_healthy_archive_and_unique_non_destructive_outputs(tmp_path):
    source = tmp_path / "source.zip"
    before = make_zip(source, {"a.txt": b"hello", "nested/b.txt": b"world"})
    inspection = recovery.inspect_file(source)
    assert inspection["status"] == "healthy", inspection
    first = recovery.repair_file(source, tmp_path / "out")
    second = recovery.repair_file(source, tmp_path / "out")
    assert first["status"] == second["status"] == "repaired"
    assert first["output"] != second["output"]
    assert source.read_bytes() == before
    assert recovery.inspect_file(first["output"])["status"] == "healthy"


def test_corrupt_entry_salvages_only_intact_members(tmp_path):
    source = tmp_path / "bad.zip"
    data = bytearray(make_zip(source, {"bad.txt": b"broken payload", "good.txt": b"keep me"}))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("bad.txt")
        name_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
        offset = info.header_offset + 30 + name_len + extra_len
    data[offset] ^= 0xFF
    source.write_bytes(data)
    inspection = recovery.inspect_file(source)
    assert inspection["status"] == "damaged", inspection
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    assert result["recovered_entries"] == 1
    with zipfile.ZipFile(result["output"]) as archive:
        assert archive.namelist() == ["good.txt"]
        assert archive.read("good.txt") == b"keep me"
    assert source.read_bytes() == data


def test_missing_central_directory_uses_crc_verified_local_headers(tmp_path):
    source = tmp_path / "truncated.zip"
    data = make_zip(source, {"one.txt": b"first", "two.txt": b"second"}, zipfile.ZIP_DEFLATED)
    source.write_bytes(data[:data.index(b"PK\x01\x02")])
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    assert result["recovered_entries"] == 2
    assert recovery.inspect_file(result["output"])["status"] == "healthy"


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "C:/windows.txt", "nested\\escape.txt"])
def test_unsafe_archive_names_are_never_copied(tmp_path, name):
    source = tmp_path / "unsafe.zip"
    make_zip(source, {name: b"unsafe", "safe.txt": b"safe"})
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    with zipfile.ZipFile(result["output"]) as archive:
        assert archive.namelist() == ["safe.txt"]
    assert not (tmp_path.parent / "escape.txt").exists()


def test_office_missing_structure_is_not_claimed_repaired_document(tmp_path):
    source = tmp_path / "broken.docx"
    make_zip(source, {"word/document.xml": b"<document/>"})
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    assert result["output"].endswith(".zip")


def test_healthy_office_and_invalid_xml(tmp_path):
    source = tmp_path / "document.docx"
    make_zip(source, {"[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<Relationships/>", "word/document.xml": b"<document/>"})
    assert recovery.inspect_file(source)["status"] == "healthy"
    make_zip(source, {"[Content_Types].xml": b"<Types>", "_rels/.rels": b"<Relationships/>", "word/document.xml": b"<document/>"})
    assert recovery.inspect_file(source)["status"] == "damaged"


def test_archive_bomb_limit_is_reported_without_output(tmp_path, monkeypatch):
    source = tmp_path / "bomb.zip"
    make_zip(source, {"huge.txt": b"A" * 100_000}, zipfile.ZIP_DEFLATED)
    monkeypatch.setattr(recovery, "MAX_COMPRESSION_RATIO", 10)
    assert recovery.inspect_file(source)["status"] == "unsupported"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "unsupported"
    assert result["output"] is None
    assert not (tmp_path / "out").exists()


def test_unsupported_and_missing_files(tmp_path):
    source = tmp_path / "application.exe"
    source.write_bytes(b"MZ")
    assert recovery.inspect_file(source)["status"] == "unsupported"
    assert recovery.repair_file(source, tmp_path / "out")["status"] == "unsupported"
    assert recovery.inspect_file(tmp_path / "missing.zip")["status"] == "error"


def test_symlinks_rejected_on_source_and_destination(tmp_path):
    source = tmp_path / "source.zip"
    make_zip(source, {"good.txt": b"safe"})
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    assert recovery.inspect_file(link)["status"] == "error"
    target = tmp_path / "actual"
    target.mkdir()
    out_link = tmp_path / "out"
    out_link.symlink_to(target, target_is_directory=True)
    assert recovery.repair_file(source, out_link)["status"] == "failed"
    assert not list(target.iterdir())


def test_source_changed_before_output_is_never_written(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    make_zip(source, {"good.txt": b"safe"})
    original = recovery._read_archive
    changed = False
    def change_after_read(data):
        nonlocal changed
        result = original(data)
        if not changed:
            source.write_bytes(b"changed by another program")
            changed = True
        return result
    monkeypatch.setattr(recovery, "_read_archive", change_after_read)
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "failed"
    assert result["output"] is None
    assert source.read_bytes() == b"changed by another program"
    assert not (tmp_path / "out").exists()


def test_input_limit(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    make_zip(source, {"safe.txt": b"safe"})
    monkeypatch.setattr(recovery, "MAX_INPUT_BYTES", 10)
    assert recovery.inspect_file(source)["status"] == "unsupported"


def test_png_missing_end_marker_can_be_reencoded_without_source_mutation(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = tmp_path / "broken.png"
    Image.new("RGB", (8, 8), "red").save(source)
    source.write_bytes(source.read_bytes()[:-12])
    before = source.read_bytes()
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "repaired"
    assert source.read_bytes() == before
    assert recovery.inspect_file(result["output"])["status"] == "healthy"


def test_multiframe_image_recovery_is_explicitly_partial(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    source = tmp_path / "animation.gif"
    Image.new("RGB", (8, 8), "red").save(source, save_all=True, append_images=[Image.new("RGB", (8, 8), "blue")], duration=100)
    assert recovery.inspect_file(source)["status"] == "healthy"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    assert recovery.inspect_file(result["output"])["status"] == "healthy"


def test_pdf_strict_inspection_and_lenient_rewrite(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "document.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with source.open("wb") as stream:
        writer.write(stream)
    assert recovery.inspect_file(source)["status"] == "healthy"
    source.write_bytes(source.read_bytes().replace(b"%PDF", b"%PXF", 1))
    before = source.read_bytes()
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "repaired"
    assert recovery.inspect_file(result["output"])["status"] == "healthy"
    assert source.read_bytes() == before


def test_existing_output_symlink_cannot_overwrite_another_file(tmp_path):
    source = tmp_path / "source.zip"
    make_zip(source, {"safe.txt": b"safe"})
    output = tmp_path / "out"
    output.mkdir()
    victim = tmp_path / "keep.txt"
    victim.write_text("preserve")
    try:
        (output / "source.recovered.zip").symlink_to(victim)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    result = recovery.repair_file(source, output)
    assert result["status"] == "repaired"
    assert result["output"].endswith("source.recovered (2).zip")
    assert victim.read_text() == "preserve"


def test_xml_entity_payload_is_not_treated_as_healthy_office(tmp_path):
    source = tmp_path / "document.docx"
    make_zip(source, {"[Content_Types].xml": b'<!DOCTYPE root [<!ENTITY x "payload">]><Types>&x;</Types>', "_rels/.rels": b"<Relationships/>", "word/document.xml": b"<document/>"})
    assert recovery.inspect_file(source)["status"] == "damaged"
    result = recovery.repair_file(source, tmp_path / "out")
    assert result["status"] == "partial"
    assert result["output"].endswith(".zip")


def test_encrypted_pdf_is_unsupported_not_mislabelled_damaged(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    source = tmp_path / "locked.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("private-password")
    with source.open("wb") as stream:
        writer.write(stream)
    before = source.read_bytes()
    assert recovery.inspect_file(source)["status"] == "unsupported"
    assert recovery.repair_file(source, tmp_path / "out")["status"] == "unsupported"
    assert source.read_bytes() == before
    assert not (tmp_path / "out").exists()
