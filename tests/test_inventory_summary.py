"""Storage summaries use recorded metadata once and never reopen user files."""
from dataclasses import replace
from pathlib import Path

import pytest

from folder_organizer.inventory_summary import inventory_category, inventory_date_bucket, inventory_folder, summarize_inventory
from folder_organizer.models import FileEntry


def entry(relative, size=1, *, modified=1000, kind="file"):
    path = Path("/inventory") / relative
    return FileEntry(str(path), str(relative), path.name, size, modified, None, "unavailable",
                     kind, path.suffix.casefold(), 10, 20, 30)


class OnePass:
    def __init__(self, entries):
        self.entries, self.visits, self.started = entries, 0, False
    def __iter__(self):
        assert not self.started, "summary traversed inventory more than once"
        self.started = True
        for item in self.entries:
            self.visits += 1
            yield item


def test_single_pass_totals_categories_and_top_files_without_any_disk_access(monkeypatch):
    items = [entry("docs/a.pdf", 20), entry("photos/a.JPG", 40), entry("photos/b.png", 40),
             entry("empty.txt", 0), entry("bin/setup.exe", 25, kind="application")]
    monkeypatch.setattr(Path, "stat", lambda *_args, **_kwargs: pytest.fail("summary must not stat paths"))
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("summary must not read content"))
    source = OnePass(items)
    result = summarize_inventory(source, "/inventory", top_limit=2, now=1000)
    assert source.visits == result["entry_visits"] == result["total_files"] == 5
    assert result["total_bytes"] == 125 and result["zero_byte_files"] == 1
    types = {row["key"]: row for row in result["by_type"]}
    assert types["images"]["bytes"] == 80 and types["images"]["count"] == 2
    assert types["applications"]["bytes"] == 25
    assert [row["path"] for row in result["largest_files"]] == [items[1].path, items[2].path]
    assert sum(row["bytes"] for row in result["by_folder"]) == 125


def test_top_folders_are_bounded_and_others_keep_exact_counts():
    result = summarize_inventory((entry(f"folder-{number}/item.txt", number) for number in range(40)), "/inventory", top_limit=3, now=1000)
    assert result["entry_visits"] == 40 and result["folder_count"] == 40
    assert len(result["by_folder"]) == 20 and result["folders_truncated"]
    assert result["folder_others"] == {"count": 20, "bytes": sum(range(20)), "folders": 20}
    assert result["by_folder"][0]["bytes"] == 39
    assert len(result["largest_files"]) == 3
    assert sum(row["bytes"] for row in result["by_folder"]) + result["folder_others"]["bytes"] == result["total_bytes"]


def test_date_buckets_are_non_overlapping_and_do_not_use_creation_times():
    now = 40_000_000
    items = [entry("today", modified=now), entry("month", modified=now-30*86400),
             entry("year", modified=now-365*86400), entry("old", modified=now-366*86400),
             entry("future", modified=now+1), entry("unknown", modified=float("nan"))]
    result = summarize_inventory(items, "/inventory", now=now)
    assert {row["key"]: row["count"] for row in result["date_buckets"]} == {
        "recent_30": 2, "year": 1, "older": 1, "future": 1, "unknown": 1}
    assert inventory_date_bucket(items[3], now=now) == "older"


def test_empty_summary_is_complete_without_special_case_disk_queries():
    result = summarize_inventory([], "/missing", now=1000)
    assert result["total_files"] == result["total_bytes"] == result["entry_visits"] == 0
    assert not result["largest_files"] and not result["by_folder"]
    assert not result["folders_truncated"] and result["folder_others"]["bytes"] == 0


def test_selection_keys_match_windows_relative_paths_and_application_kind():
    item = replace(entry("inside.txt"), relative_path="Reports\\2026\\inside.txt")
    assert inventory_folder(item, "C:\\Inventory") == "Reports"
    assert inventory_folder(entry("direct.txt"), "/inventory") == "."
    assert inventory_folder(replace(item, relative_path="../outside.txt"), "/inventory") == "[범위 밖 경로]"
    assert inventory_category(entry("Editor.app/Contents/data.bin", kind="application")) == "applications"


def test_summary_work_doubles_for_twice_as_many_entries():
    small = OnePass(entry(f"folder/{number}.txt", number) for number in range(1000))
    large = OnePass(entry(f"folder/{number}.txt", number) for number in range(2000))
    first = summarize_inventory(small, "/inventory", now=1000)
    second = summarize_inventory(large, "/inventory", now=1000)
    assert large.visits == 2 * small.visits == second["entry_visits"]
    assert first["entry_visits"] == 1000 and second["entry_visits"] == 2000


@pytest.mark.parametrize("limit", [0, 101, True, None])
def test_summary_bounds_top_list_before_reading_entries(limit):
    with pytest.raises(ValueError):
        summarize_inventory(OnePass([]), "/inventory", top_limit=limit)
