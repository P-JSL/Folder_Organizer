"""New inventory optimizations retain the recorded file and scope contract."""
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from folder_organizer import scanner
from folder_organizer.inventory_summary import inventory_folder, summarize_inventory
from folder_organizer.models import FileEntry, MatchGroup


def entry(relative: str, size: int = 1) -> FileEntry:
    name = relative.replace("\\", "/").rsplit("/", 1)[-1]
    return FileEntry("/offline/" + name, relative, name, size, 1000.0, None,
                     "unavailable", "file", ".TXT", 10, 20, 2 ** 100)


def test_compact_inventory_records_keep_all_exported_fields_and_replace_support():
    item = entry("Reports/원본.txt", 123)
    exported = item.to_dict()
    assert exported == asdict(item)
    assert set(exported) == {field.name for field in fields(item)}
    assert exported["inode"] == 2 ** 100
    exported["size"] = 0
    assert item.size == 123 and replace(item, size=7).size == 7
    # There is no per-entry instance dictionary for large inventories.
    assert not hasattr(item, "__dict__")


def test_group_exports_still_copy_nested_evidence_and_exclude_authority():
    group = MatchGroup("duplicate", ["one", "two"], "same bytes",
                       evidence={"byte_identical": True, "details": ["source"]})
    group._snapshot_proof = {"private": "process-only authority"}
    exported = group.to_dict()
    assert "_snapshot_proof" not in exported
    exported["paths"].append("third")
    exported["evidence"]["details"].append("changed")
    assert group.paths == ["one", "two"]
    assert group.evidence["details"] == ["source"]


def test_scan_compiles_each_glob_once_and_prunes_matched_ancestors(tmp_path, monkeypatch):
    for relative in ("PUBLIC/deep/keep.TXT", "PUBLIC/deep/discard.tmp", "PUBLIC/secret/private.txt",
                     "Cache/child/skip.txt", "other/keep.txt", "other/discard.OLD"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
    compiled = []
    real_compile = scanner.re.compile

    def compile_once(pattern, *args, **kwargs):
        compiled.append(pattern)
        return real_compile(pattern, *args, **kwargs)

    monkeypatch.setattr(scanner.re, "compile", compile_once)
    result = scanner.scan_folder(tmp_path, sort_results=False,
                                 exclude_patterns=("cAcHe", "*.TMP", "*.old", "public/secret"))
    assert len(compiled) == 4
    assert {item.relative_path.replace("\\", "/") for item in result.files} == {
        "PUBLIC/deep/keep.TXT", "other/keep.txt",
    }
    assert result.excluded_directories == 2 and result.filtered_files == 2
    assert result.scan_metrics["exclusion_patterns_compiled"] == 4


def test_empty_scope_has_no_glob_compilation_or_root_relative_reparsing(tmp_path, monkeypatch):
    directory = tmp_path / "깊은 폴더" / "Editor.APP" / "Contents"
    directory.mkdir(parents=True)
    (directory / "설정.txt").write_bytes(b"editor settings")
    (tmp_path / "ordinary.txt").write_bytes(b"ordinary")
    monkeypatch.setattr(scanner, "_compile_exclusions", lambda *_: pytest.fail("empty scope must not compile globs"))
    monkeypatch.setattr(Path, "relative_to", lambda *_args, **_kwargs: pytest.fail("prefix is already known"))
    result = scanner.scan_folder(tmp_path, sort_results=False)
    assert result.scan_metrics["exclusion_patterns_compiled"] == 0
    by_name = {item.name: item for item in result.files}
    assert by_name["설정.txt"].kind == "application"
    assert by_name["ordinary.txt"].kind == "file"
    assert by_name["설정.txt"].relative_path.replace("\\", "/") == "깊은 폴더/Editor.APP/Contents/설정.txt"
    for item in result.files:
        metadata = Path(item.path).stat(follow_symlinks=False)
        assert (item.size, item.mtime_ns, item.device, item.inode) == (
            metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino,
        )


def test_prefix_context_does_not_follow_symlink_or_reinclude_delete_storage(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_bytes(b"outside selection")
    root = tmp_path / "selected"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    (nested / "keep.txt").write_bytes(b"inside")
    (root / "delete_files").mkdir()
    (root / "delete_files" / "deleted.txt").write_bytes(b"stored")
    try:
        (nested / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating links is not permitted on this runner")
    result = scanner.scan_folder(root, include_hidden=True, sort_results=False)
    assert [item.name for item in result.files] == ["keep.txt"]
    assert any("link" in warning for warning in result.warnings)


@pytest.mark.parametrize(("relative", "folder"), [
    ("./Reports//2026/./item.txt", "Reports"),
    ("Reports\\2026\\item.txt", "Reports"),
    ("./single.txt", "."),
    ("././", "[범위 밖 경로]"),
    ("Reports/../item.txt", "[범위 밖 경로]"),
    ("/Reports/item.txt", "[범위 밖 경로]"),
])
def test_summary_folder_parser_preserves_normalized_relative_scope(relative, folder):
    assert inventory_folder(entry(relative), "/offline") == folder


def test_largest_heap_keeps_earliest_ties_after_replacements_and_exact_others():
    items = [entry(f"folder-{number}/item.txt", size) for number, size in enumerate([5, 5, 8, 5, 8, 8, 5])]
    items.extend(entry(f"folder-{number}/item.txt", 1) for number in range(7, 80))
    result = summarize_inventory(iter(items), "/offline", top_limit=2, now=2000)
    assert [item["relative_path"] for item in result["largest_files"]] == [
        "folder-2/item.txt", "folder-4/item.txt",
    ]
    assert result["folder_count"] == 80 and result["folder_others"]["folders"] == 60
    assert sum(row["count"] for row in result["by_folder"]) + result["folder_others"]["count"] == len(items)
    assert sum(row["bytes"] for row in result["by_folder"]) + result["folder_others"]["bytes"] == sum(i.size for i in items)
    assert next(row for row in result["by_type"] if row["key"] == "documents")["count"] == len(items)


def test_rust_and_python_internal_state_are_excluded_with_hidden_files_enabled(tmp_path):
    for relative in (".folder-organizer/history/private.json",
                     ".folder-organizer-rust/history/private.json",
                     "nested/.FOLDER-ORGANIZER-RUST/objects/chunk.bin",
                     "nested/keep.txt"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    result = scanner.scan_folder(tmp_path, include_hidden=True, sort_results=False)
    assert [item.relative_path.replace("\\", "/") for item in result.files] == ["nested/keep.txt"]
