"""Merging never authorizes overwriting sources or retiring unseen content."""
from __future__ import annotations

import copy
import csv
import io
import os
from pathlib import Path

import pytest

from folder_organizer import merging as merge, operations as ops


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / "Documents"
    directory.mkdir()
    return directory


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def preview(root, *paths, **kwargs):
    return merge.build_merge_preview(root, [str(path) for path in paths], **kwargs)


def test_readonly_preview_and_merge_only_preserve_unique_paragraphs_in_order(root):
    first = write(root, "보고서.txt", b"\xef\xbb\xbfShared\r\nline\r\n\r\nFirst only\r\n")
    second = write(root, "보고서 수정.md", "Shared\nline\n\nSecond only\n\nshared\nline\n")
    before = {str(path): path.read_bytes() for path in (first, second)}
    plan = preview(root, first, second)
    assert plan["content"] == "Shared\nline\n\nFirst only\n\nSecond only\n\nshared\nline\n"
    assert plan["total_units"] == 5 and plan["retained_units"] == 4 and plan["removed_units"] == 1
    assert not Path(plan["output"]).exists() and not (root / ops.STATE_DIRECTORY).exists()
    result = merge.execute_merge(plan)
    assert result["status"] == "completed" and result["output_verified"]
    assert result["archive_journal"] is None
    assert Path(result["output"]).read_bytes() == plan["content"].encode("utf-8")
    assert {str(path): path.read_bytes() for path in (first, second)} == before


def test_merge_and_archive_has_normal_reversible_history_leaving_output(root):
    first = write(root, "a.txt", "Shared\n\nFirst\n")
    second = write(root, "b.txt", "Shared\n\nSecond\n")
    original = [path.read_bytes() for path in (first, second)]
    plan = preview(root, first, second, move_originals=True)
    result = merge.execute_merge(plan)
    assert result["status"] == "completed"
    assert not first.exists() and not second.exists()
    journal = result["archive_journal"]
    assert journal["status"] == "completed"
    assert all(Path(item["original_backup"]).exists() for item in journal["items"])
    assert ops.list_history(root)[0]["id"] == journal["id"]
    assert ops.undo_operation(root, journal["id"])["status"] == "undone"
    assert [path.read_bytes() for path in (first, second)] == original
    assert Path(result["output"]).read_text("utf-8") == plan["content"]


def test_fenced_code_paragraphs_are_not_split_or_deduplicated_against_prose(root):
    first = write(root, "a.md", "```text\n\nshared\n\n```\n\nshared\n")
    second = write(root, "b.md", "shared\n\nOther\n")
    plan = preview(root, first, second)
    assert plan["content"] == "```text\n\nshared\n\n```\n\nshared\n\nOther\n"
    assert plan["retained_units"] == 3 and plan["removed_units"] == 1


@pytest.mark.parametrize(("suffix", "delimiter"), [(".csv", ","), (".tsv", "\t")])
def test_tables_deduplicate_exact_rows_preserving_quoted_multiline_values(root, suffix, delimiter):
    def table(rows):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=delimiter, lineterminator="\r\n")
        writer.writerow(["name", "value"])
        writer.writerows(rows)
        return stream.getvalue()
    shared = ["한,글", "line1\nline2"]
    first = write(root, "one" + suffix, table([shared, ["a", "=SUM(A1:A2)"]]))
    second = write(root, "two" + suffix, table([shared, ["a", "=SUM(A1:A3)"]]))
    plan = preview(root, first, second)
    rows = list(csv.reader(io.StringIO(plan["content"], newline=""), delimiter=delimiter))
    assert rows == [["name", "value"], shared, ["a", "=SUM(A1:A2)"], ["a", "=SUM(A1:A3)"]]
    assert plan["removed_units"] == 1 and plan["unit"] == "행"


@pytest.mark.parametrize("second", ["value,name\nx,y\n", "name,value\nx,y,z\n", 'name,value\n"broken\n'])
def test_invalid_tables_rejected_without_any_output(root, second):
    first = write(root, "one.csv", "name,value\nx,y\n")
    other = write(root, "two.csv", second)
    with pytest.raises(merge.MergeError):
        preview(root, first, other)
    assert sorted(path.name for path in root.iterdir()) == ["one.csv", "two.csv"]


def test_csv_cell_newlines_are_exact_values_and_lone_carriage_returns_remain_quoted(root):
    def table(value):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
        writer.writerow(["value"])
        writer.writerow([value])
        return stream.getvalue()
    first = write(root, "a.csv", table("line1\r\nline2"))
    second = write(root, "b.csv", table("line1\nline2"))
    third = write(root, "c.csv", table("line1\rline2"))
    plan = preview(root, first, second, third)
    assert list(csv.reader(io.StringIO(plan["content"], newline=""))) == [
        ["value"], ["line1\r\nline2"], ["line1\nline2"], ["line1\rline2"]]
    assert plan["removed_units"] == 0


@pytest.mark.parametrize(("name", "content"), [("b.pdf", b"text"), ("b.txt", b"\xff"),
                                                ("b.txt", b"nul\x00byte"), ("b.csv", b"name\na\n")])
def test_unsupported_or_mixed_formats_never_guess(root, name, content):
    first = write(root, "a.txt", "original")
    other = write(root, name, content)
    with pytest.raises(merge.MergeError):
        preview(root, first, other)
    assert first.read_text() == "original" and other.read_bytes() == content


def test_source_edit_invalidates_whole_preview_before_output_or_archive(root):
    first, second = write(root, "a.txt", "old"), write(root, "b.txt", "other")
    plan = preview(root, first, second, move_originals=True)
    first.write_text("new", encoding="utf-8")
    result = merge.execute_merge(plan)
    assert result["status"] == "failed" and not result["output_created"]
    assert not Path(plan["output"]).exists() and not (root / ops.STATE_DIRECTORY).exists()
    assert first.read_text() == "new" and second.read_text() == "other"


def test_change_after_output_creation_preserves_new_output_and_all_sources(root, monkeypatch):
    first, second = write(root, "a.txt", "old"), write(root, "b.txt", "other")
    plan = preview(root, first, second, move_originals=True)
    real_write = merge._write_output
    def changing_write(path, raw, created):
        real_write(path, raw, created)
        first.write_text("newer content", encoding="utf-8")
    monkeypatch.setattr(merge, "_write_output", changing_write)
    result = merge.execute_merge(plan)
    assert result["status"] == "partial" and result["output_verified"]
    assert result["archive_journal"] is None
    assert first.read_text() == "newer content" and second.read_text() == "other"
    assert Path(result["output"]).read_text() == plan["content"]


def test_change_during_archive_preparation_is_not_accepted_as_new_preview(root, monkeypatch):
    first, second = write(root, "a.txt", "old"), write(root, "b.txt", "other")
    plan = preview(root, first, second, move_originals=True)
    real_build = ops.build_plan
    def changing_build(*args, **kwargs):
        second.write_text("new original", encoding="utf-8")
        return real_build(*args, **kwargs)
    monkeypatch.setattr(ops, "build_plan", changing_build)
    result = merge.execute_merge(plan)
    assert result["status"] == "partial" and result["archive_journal"] is None
    assert first.exists() and second.read_text() == "new original"


def test_changed_merged_output_after_archive_reports_recoverable_partial(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    plan = preview(root, first, second, move_originals=True)
    real_execute = ops.execute_plan
    def changed_output(*args, **kwargs):
        result = real_execute(*args, **kwargs)
        Path(plan["output"]).write_text("edited merged output", encoding="utf-8")
        return result
    monkeypatch.setattr(ops, "execute_plan", changed_output)
    result = merge.execute_merge(plan)
    assert result["status"] == "partial"
    assert ops.undo_operation(root, result["archive_journal"]["id"])["status"] == "undone"
    assert first.read_text() == "first" and second.read_text() == "second"


def test_partial_write_is_preserved_and_does_not_retire_originals(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    plan = preview(root, first, second, move_originals=True)
    def partial_write(path, raw, created):
        with path.open("xb") as stream:
            created()
            stream.write(raw[:2])
        raise OSError("disk full")
    monkeypatch.setattr(merge, "_write_output", partial_write)
    result = merge.execute_merge(plan)
    assert result["status"] == "partial" and not result["output_verified"]
    assert first.exists() and second.exists()
    assert Path(result["output"]).read_bytes() == plan["content"].encode()[:2]


def test_exclusive_output_collision_between_preview_and_execution_never_overwrites(root):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    plan = preview(root, first, second)
    output = Path(plan["output"])
    output.write_text("someone else's file", encoding="utf-8")
    result = merge.execute_merge(plan)
    assert result["status"] == "failed" and not result["output_created"]
    assert output.read_text() == "someone else's file"


@pytest.mark.parametrize("name", ["../outside.txt", "a.txt", "CON.txt", "a.txt.", "wrong.csv"])
def test_invalid_or_source_output_name_rejected(root, name):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    with pytest.raises(merge.MergeError):
        preview(root, first, second, output_name=name)


def test_default_collision_has_unique_new_name_and_external_destination_supported(root, tmp_path):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    existing = write(root, "a_병합.txt", "existing")
    plan = preview(root, first, second)
    assert Path(plan["output"]).name == "a_병합 (1).txt"
    destination = tmp_path / "Output"
    destination.mkdir()
    plan = preview(root, first, second, destination=destination)
    assert merge.execute_merge(plan)["status"] == "completed"
    assert (destination / "a_병합.txt").exists() and existing.read_text() == "existing"


@pytest.mark.parametrize("key,value", [("content", "tampered"), ("output", "/tmp/tampered.txt"),
                                      ("move_originals", True)])
def test_tampered_plan_never_executes(root, key, value):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    plan = copy.deepcopy(preview(root, first, second))
    plan[key] = value
    with pytest.raises(ops.OperationError):
        merge.execute_merge(plan)
    assert set(path.name for path in root.iterdir()) == {"a.txt", "b.txt"}


def test_byte_and_unit_limits_fail_before_output(root, monkeypatch):
    first, second = write(root, "a.txt", "first\n\nsecond"), write(root, "b.txt", "third")
    monkeypatch.setattr(merge, "MAX_FILE_BYTES", 4)
    with pytest.raises(merge.MergeError):
        preview(root, first, second)
    monkeypatch.setattr(merge, "MAX_FILE_BYTES", 100)
    monkeypatch.setattr(merge, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(merge.MergeError):
        preview(root, first, second)
    monkeypatch.setattr(merge, "MAX_TOTAL_BYTES", 100)
    monkeypatch.setattr(merge, "MAX_UNITS", 2)
    with pytest.raises(merge.MergeError):
        preview(root, first, second)
    assert len(list(root.iterdir())) == 2


def test_csv_column_and_cell_limits_bound_memory(root, monkeypatch):
    first = write(root, "a.csv", "one,two\na,b\n")
    second = write(root, "b.csv", "one,two\nc,d\n")
    monkeypatch.setattr(merge, "MAX_COLUMNS", 1)
    with pytest.raises(merge.MergeError, match="열"):
        preview(root, first, second)
    monkeypatch.setattr(merge, "MAX_COLUMNS", 10)
    monkeypatch.setattr(merge, "MAX_CELLS", 3)
    with pytest.raises(merge.MergeError, match="셀"):
        preview(root, first, second)


def test_link_source_and_app_bundle_retirement_are_blocked_in_preview(root):
    first, second = write(root, "a.txt", "first"), write(root, "Editor.app/b.txt", "second")
    with pytest.raises(merge.MergeError, match="번들"):
        preview(root, first, second, move_originals=True)
    assert preview(root, first, second)["move_originals"] is False
    linked = root / "linked.txt"
    try:
        linked.symlink_to(first)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(ops.OperationError):
        preview(root, linked, second)


class _ChangedTimeStat:
    """Retain all real stat fields while modelling unlike Windows time APIs."""

    def __init__(self, info, change=123_456_789):
        self.info, self.change = info, change

    def __getattr__(self, name):
        if name == "st_ctime_ns":
            return self.info.st_ctime_ns + self.change
        if name == "st_ctime":
            return self.info.st_ctime + self.change / 1_000_000_000
        return getattr(self.info, name)


def test_unchanged_sources_merge_when_pathname_ctime_differs_from_descriptor_ctime(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    real_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path, *args, **kwargs:
                        _ChangedTimeStat(real_lstat(path, *args, **kwargs)))
    plan = preview(root, first, second, move_originals=True)
    result = merge.execute_merge(plan)
    assert result["status"] == "completed" and result["output_verified"]
    assert ops.undo_operation(root, result["archive_journal"]["id"])["status"] == "undone"
    assert first.read_text() == "first" and second.read_text() == "second"


def test_descriptor_change_time_still_invalidates_preview_with_same_content_and_mtime(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    plan = preview(root, first, second, move_originals=True)
    real_fstat = os.fstat
    monkeypatch.setattr(merge.os, "fstat", lambda descriptor: _ChangedTimeStat(real_fstat(descriptor)))
    result = merge.execute_merge(plan)
    assert result["status"] == "failed" and not result["output_created"]
    assert first.read_text() == "first" and second.read_text() == "second"


def test_source_path_replacement_during_read_rejected_even_when_open_descriptor_is_unchanged(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    replacement = write(root, "replacement.txt", "first")
    if os.name == "nt":
        # Windows may deny replacing an open file. Model its pathname resolving
        # to the replacement at the no-follow reopen without needing deletion
        # sharing; the first descriptor still references the original inode.
        real_open = os.open
        opened = 0
        def replacement_on_reopen(path, flags, *args, **kwargs):
            nonlocal opened
            if Path(path) == first:
                opened += 1
                if opened > 1:
                    return real_open(replacement, flags, *args, **kwargs)
            return real_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(merge.os, "open", replacement_on_reopen)
        with pytest.raises(merge.MergeError, match="변경|교체"):
            preview(root, first, second)
        return
    real_read = os.read
    changed = False
    def replace_after_read(descriptor, size):
        nonlocal changed
        data = real_read(descriptor, size)
        if data and not changed:
            changed = True
            replacement.replace(first)
        return data
    monkeypatch.setattr(merge.os, "read", replace_after_read)
    with pytest.raises(merge.MergeError, match="변경|교체"):
        preview(root, first, second)
    assert first.read_text() == "first" and second.read_text() == "second"
    assert not (root / ops.STATE_DIRECTORY).exists()


def test_merge_only_logs_original_paths_with_shared_execution_id_only_after_execution(root, monkeypatch):
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    records = []
    monkeypatch.setattr(merge, "_record", lambda event, identifier, status, **kwargs:
                        records.append({"event": event, "id": identifier, "status": status, **kwargs}))
    plan = preview(root, first, second)
    assert not records
    result = merge.execute_merge(plan)
    assert result["status"] == "completed"
    assert records[0]["event"] == "execution.started" and records[-1]["event"] == "execution.finished"
    items = [record for record in records if record["event"] == "execution.item"]
    assert [item["source"] for item in items] == [str(first), str(second)]
    assert all(item["id"] == plan["id"] and item["target"] == plan["output"] for item in records)
    assert all("원본 유지" in item["message"] for item in items)


def test_execution_log_failure_cannot_break_merge(root, monkeypatch):
    from folder_organizer import execution_log
    first, second = write(root, "a.txt", "first"), write(root, "b.txt", "second")
    monkeypatch.setattr(execution_log, "record_execution", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(OSError("log denied")))
    result = merge.execute_merge(preview(root, first, second))
    assert result["status"] == "completed" and first.exists() and second.exists()
