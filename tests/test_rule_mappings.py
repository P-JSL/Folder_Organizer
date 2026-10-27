"""Folder mappings use one safe renderer for previews and real routes."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from folder_organizer import operations as ops, rules
from folder_organizer.scanner import scan_folder


@pytest.fixture
def entry():
    return SimpleNamespace(name="보고서.txt", suffix=".txt", modified=datetime(2026, 10, 15, 12, 30, 45).timestamp(),
                           created=datetime(2025, 3, 7, 9, 15, 20).timestamp())


def test_korean_aliases_and_explicit_dates_share_core_renderer(entry):
    template = "{규칙}/{종류}/{수정일:%Y/%m/%d}/{created:%Y-%m}/{분기}분기/{요일}/{확장자}/{원래폴더}/{이름}"
    normalized = rules.validate_rules([{"name": "회사 청구서", "target": template}])[0]
    assert rules.render_target(normalized, entry, Path("업무/청구서/보고서.txt")) == (
        "회사 청구서/문서/2026/10/15/2025-03/4분기/목/txt/업무/청구서/보고서")
    assert rules.render_target({"target": "{name}/{stem}/{day}/{week}/{hour}-{minute}-{second}"}, entry,
                               Path("보고서.txt")) == "보고서/보고서/15/42/12-30-45"


def test_creation_basis_changes_only_implicit_date_values(entry):
    template = "{year}/{month}/{day}/{date}/{quarter}/{weekday}/{modified:%Y}/{created_year}"
    assert rules.render_target({"target": template, "date_basis": "created"}, entry, Path("보고서.txt")) == (
        "2025/03/07/2025-03-07/1/금/2026/2025")
    assert rules.render_target({"target": "{type}/{source_folder}"}, entry, Path("보고서.txt")) == "문서/루트"


def test_iso_week_format_is_portable_and_keeps_iso_year_boundary(entry):
    entry.modified = datetime(2021, 1, 1, 4, 5, 6).timestamp()
    assert rules.render_target({"target": "{modified:%Y-%m-%d/%G-W%V-%u/%j/%H-%M-%S/%%}"},
                               entry, Path("보고서.txt")) == "2021-01-01/2020-W53-5/001/04-05-06/%"


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), "2025-01-01"])
def test_invalid_creation_never_becomes_modified_date(entry, value):
    entry.created = value
    with pytest.raises(ValueError, match="생성일"):
        rules.render_target({"target": "{created:%Y/%m}"}, entry, Path("보고서.txt"))


def test_rule_and_filename_mapping_cannot_inject_paths_or_device_names(entry):
    rule = {"name": "../CON: 회사/작업", "target": "{rule}/{stem}"}
    assert rules.render_target(rule, entry, Path("보고서.txt")) == "_CON_ 회사_작업/보고서"
    entry.name = "CON.txt"
    assert rules.render_target({"target": "{stem}"}, entry, Path("CON.txt")) == "_CON"


@pytest.mark.parametrize("target", [
    "{year.__class__}", "{created[year]}", "{name!r}", "{modified!s:%Y}", "{modified:%s}",
    "{modified:%-d}", "{modified:%Z}", "{modified:%}", "{modified:{year}}", "{year:04d}",
    "{수정일:%Y}/../../escape", "{created:C:/%Y}", "{modified:%Y}/NUL", "{수정일:%Y}/name.",
    "{missing}", "{", "{}", "{0}", "C:/{year}", "x" * 513,
])
def test_unsafe_or_unsupported_format_rejected_before_any_file_access(target):
    with pytest.raises(ValueError):
        rules.validate_rules([{"name": "규칙", "target": target}])


@pytest.mark.parametrize("basis", ["ctime", "birthtime", None, [], True])
def test_invalid_date_basis_fails_validation(basis):
    with pytest.raises(ValueError, match="날짜 기준"):
        rules.validate_rules([{"target": "{year}", "date_basis": basis}])


def test_all_documented_tokens_and_presets_render_with_independent_values(entry):
    tokens = rules.mapping_tokens()
    assert len({token["token"] for token in tokens}) == len(tokens)
    for token in tokens:
        normalized = rules.validate_rules([{"name": "회사 청구서", "target": token["token"]}])[0]
        assert rules.render_target(normalized, entry, Path("업무/청구서/보고서.txt"))
    for preset in ("type", "date", "folder", "daily", "created_date", "quarter", "rule"):
        first, second = rules.default_rules(preset), rules.default_rules(preset)
        assert rules.validate_rules(first) == first
        assert rules.render_target(first[0], entry, Path("업무/청구서/보고서.txt"))
        first[0]["target"] = "changed"
        assert first != second


def test_created_date_filters_and_routing_use_creation_without_replacing_mtime(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("valuable data", encoding="utf-8")
    scan = scan_folder(tmp_path)
    scan.files = [replace(scan.files[0], created=datetime(2025, 3, 7, 9, 15).timestamp())]
    plan = rules.build_classification_plan(scan, [{"name": "생성일 분류", "date_basis": "created",
                                                  "modified_after": "2025-03-07", "modified_before": "2025-03-07",
                                                  "target": "{규칙}/{날짜}/{확장자}"}])
    assert len(plan["items"]) == 1
    item = plan["items"][0]
    assert Path(item["target"]).relative_to(tmp_path).as_posix() == "정리됨/생성일 분류/2025-03-07/txt/report.txt"
    assert source.read_text() == "valuable data" and not Path(item["target"]).exists()
    result = ops.execute_plan(plan)
    assert result["status"] == "completed"
    assert source.read_text() == Path(item["target"]).read_text() == "valuable data"
    assert ops.undo_operation(tmp_path, result["id"])["status"] == "undone"
    assert source.exists() and not Path(item["target"]).exists()


@pytest.mark.parametrize("target,filters", [
    ("{created:%Y/%m}", {}), ("{year}/{month}", {}),
    ("문서", {"modified_after": "2020-01-01"}),
])
def test_missing_creation_is_visible_skip_without_mtime_fallback_or_mutation(tmp_path, target, filters):
    source = tmp_path / "report.txt"
    source.write_bytes(b"valuable")
    scan = scan_folder(tmp_path)
    scan.files = [replace(scan.files[0], created=None)]
    plan = rules.build_classification_plan(scan, [{"name": "생성일", "date_basis": "created",
                                                  "target": target, **filters}])
    assert not plan["items"] and plan["classification_summary"]["skipped"] == 1
    assert any("생성일" in warning and "건너뜀" in warning for warning in plan["warnings"])
    assert source.read_bytes() == b"valuable" and not (tmp_path / ops.STATE_DIRECTORY).exists()


def test_legacy_rule_defaults_to_modified_and_new_tokens_keep_plan_signature(tmp_path):
    source = tmp_path / "report.txt"
    source.write_text("important", encoding="utf-8")
    scan = scan_folder(tmp_path)
    legacy = rules.validate_rules([{"name": "문서", "target": "{type}/{year}/{month}/{source_folder}"}])[0]
    assert legacy["date_basis"] == "modified"
    plan = rules.build_classification_plan(scan, [{"name": "문서", "target": "{rule}/{modified:%Y/%m/%d}/{name}"}])
    plan["items"][0]["target"] = str(tmp_path / "unexpected.txt")
    with pytest.raises(ops.OperationError):
        ops.execute_plan(plan)
    assert source.read_text() == "important" and not (tmp_path / "unexpected.txt").exists()
