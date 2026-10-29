"""Native Korean desktop interface. All filesystem work runs outside the GUI thread."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QObject, QSortFilterProxyModel, Qt,
    QThread, QUrl, QSettings, QSize, QTimer, Signal, Slot,
)
from PySide6.QtGui import QAction, QActionGroup, QColor, QDesktopServices, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter, QTableView, QTabWidget,
    QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import __version__
from . import updater
from .matching import AnalysisCache, find_duplicates, find_similar
from .models import FileEntry, MatchGroup, ScanResult
from .operations import build_plan, execute_plan, list_history, undo_operation
from .recovery import inspect_file, repair_file
from .scanner import scan_folder, normalize_exclude_patterns
from .comparison import compare_files
from .retention import choose_duplicate_copies
from .reporting import build_report, write_report
from .diagnostics import configure_logging, install_exception_hook, log_event
from .history_view import HistoryDialog, restore_effect
from .log_view import LogViewerDialog
from .progress import ProgressTracker
from .rules import default_rules, validate_rules, build_classification_plan, build_duplicate_plan
from .rule_view import ClassificationDialog
from .help_view import HelpCenterDialog
from .themes import apply_theme, stylesheet
from .execution_log import configure_execution_logging, record_execution
from .execution_log_view import ExecutionLogDialog
from .merging import execute_merge
from .merge_view import MergeDialog
from .inventory_summary import summarize_inventory
from .overview_view import InventoryOverviewDialog


STYLE = stylesheet('dark')


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value):,} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return "0 B"


def format_date(value: float | None) -> str:
    if value is None:
        return "제공되지 않음"
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "날짜 범위 초과"


ANALYSIS_DEFAULTS = {
    "profile": "balanced", "methods": ["name", "text", "image"],
    "name_threshold": .82, "text_threshold": .90, "image_threshold": .90,
    "exclude_patterns": [], "min_size": 0, "max_size": None,
    "retention_rule": "first", "preferred_folder": "", "protected_folders": [],
    "search_mode": "auto", "max_candidates_per_file": 32,
}
PROFILE_LABELS = {
    "balanced": "전체 기준 비교", "photos": "사진 검토", "documents": "문서 버전 검토", "names": "이름만 비교",
}
RETENTION_LABELS = {
    "first": "경로순 첫 파일 보존", "newest": "가장 최근 수정 파일 보존",
    "oldest": "가장 오래된 수정 파일 보존", "shortest_path": "짧은 경로 파일 보존",
    "preferred_folder": "선호 폴더 파일 우선 보존",
}


def analysis_preferences(settings: QSettings) -> dict:
    """Read a bounded local preference document; invalid values use safe defaults."""
    result = {**ANALYSIS_DEFAULTS, "methods": list(ANALYSIS_DEFAULTS["methods"]),
              "exclude_patterns": [], "protected_folders": []}
    try:
        raw = settings.value("analysis/preferences", "")
        saved = json.loads(raw) if isinstance(raw, str) and len(raw) < 100_000 else {}
        if not isinstance(saved, dict):
            return result
        if saved.get("profile") in PROFILE_LABELS:
            result["profile"] = saved["profile"]
        methods = saved.get("methods")
        if isinstance(methods, list) and methods and all(value in {"name", "text", "image"} for value in methods):
            result["methods"] = list(dict.fromkeys(methods))
        for key in ("name_threshold", "text_threshold", "image_threshold"):
            value = saved.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and .5 <= value <= 1:
                result[key] = float(value)
        for key in ("exclude_patterns", "protected_folders"):
            value = saved.get(key)
            if isinstance(value, list) and len(value) <= 100 and all(isinstance(item, str) and len(item) <= 1024 for item in value):
                result[key] = value
        try:
            result["exclude_patterns"] = list(normalize_exclude_patterns(result["exclude_patterns"]))
        except ValueError:
            result["exclude_patterns"] = []
        low, high = saved.get("min_size", 0), saved.get("max_size")
        if isinstance(low, int) and not isinstance(low, bool) and low >= 0:
            result["min_size"] = low
        if high is None or isinstance(high, int) and not isinstance(high, bool) and high >= result["min_size"]:
            result["max_size"] = high
        if saved.get("retention_rule") in RETENTION_LABELS:
            result["retention_rule"] = saved["retention_rule"]
        if saved.get("search_mode") in {"auto", "complete", "indexed"}:
            result["search_mode"] = saved["search_mode"]
        preferred = saved.get("preferred_folder", "")
        if isinstance(preferred, str) and len(preferred) < 4096:
            result["preferred_folder"] = preferred
    except (ValueError, TypeError):
        pass
    return result


def evidence_scores(group: MatchGroup) -> str:
    if group.kind == "duplicate":
        return "내용 완전 일치"
    evidence = getattr(group, "evidence", {}) or {}
    labels = (("name_score", "이름"), ("text_score", "텍스트"), ("image_score", "이미지"))
    metrics = [f"{label} {evidence[key]:.2f}" for key, label in labels if isinstance(evidence.get(key), (int, float))]
    return " · ".join(metrics) or f"기준 점수 {group.score:.2f}"


class AnalysisSettingsDialog(QDialog):
    """Saved comparison profiles, scan scope and conservative retention rules."""
    def __init__(self, values: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("검사 설정 · 보존 규칙")
        self.resize(760, 620)
        self.values = dict(values)
        outer = QVBoxLayout(self)
        intro = QLabel("설정은 이 컴퓨터에 저장됩니다. 검사 범위 변경은 다시 분석한 뒤 적용됩니다.\n보존 규칙은 바이트가 같은 중복 그룹에서만 선택을 돕고, 파일을 직접 변경하지 않습니다.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        compare = QWidget()
        form = QFormLayout(compare)
        self.profile = QComboBox()
        for key, label in PROFILE_LABELS.items():
            self.profile.addItem(label, key)
        self.profile.setCurrentIndex(self.profile.findData(values["profile"]))
        form.addRow("검사 프로필", self.profile)
        self.search_mode = QComboBox()
        for label, mode in (("자동 · 폴더 크기에 맞게 (권장)", "auto"),
                            ("전체 쌍 비교 · 큰 폴더는 느릴 수 있음", "complete"),
                            ("빠른 후보 검색 · 일부 유사 파일을 놓칠 수 있음", "indexed")):
            self.search_mode.addItem(label, mode)
        self.search_mode.setCurrentIndex(self.search_mode.findData(values.get("search_mode", "auto")))
        self.search_mode.setToolTip("정확한 중복 검사는 항상 전체 파일을 검증합니다. 이 설정은 유사 후보를 고르는 범위만 바꿉니다. 전체 쌍 비교도 안전 한도에 도달하면 일부 결과입니다.")
        form.addRow("유사 후보 검색 방식", self.search_mode)
        self.methods = {}
        self.thresholds = {}
        for method, label in (("name", "파일 이름"), ("text", "UTF-8 텍스트"), ("image", "이미지 구조·색·투명도")):
            row = QWidget()
            horizontal = QHBoxLayout(row)
            horizontal.setContentsMargins(0, 0, 0, 0)
            enabled = QCheckBox("비교")
            enabled.setChecked(method in values["methods"])
            threshold = QDoubleSpinBox()
            threshold.setRange(.50, 1.00)
            threshold.setSingleStep(.01)
            threshold.setDecimals(2)
            threshold.setValue(values[method + "_threshold"])
            threshold.setToolTip("이 기준 이상을 검토 후보로 표시합니다. 같은 파일일 확률을 뜻하지 않습니다.")
            horizontal.addWidget(enabled)
            horizontal.addWidget(QLabel("후보 기준"))
            horizontal.addWidget(threshold)
            horizontal.addStretch()
            self.methods[method] = enabled
            self.thresholds[method] = threshold
            form.addRow(label, row)
        hint = QLabel("정확한 중복: 크기 → SHA-256 → 실제 바이트 비교\n이름은 내용의 증거가 아닙니다. 텍스트와 이미지는 서로 다른 방식의 점수이며, 점수 1.00도 바이트 동일을 보장하지 않습니다.\n사진은 회전·자르기·의미를 이해하는 AI 비교가 아닙니다. 텍스트는 지원되는 UTF-8 파일과 제한된 앞부분을 비교합니다.")
        hint.setWordWrap(True)
        form.addRow(hint)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        tabs.addTab(compare, "비교 기준")
        scope = QWidget()
        scope_form = QFormLayout(scope)
        self.exclusions = QPlainTextEdit("\n".join(values["exclude_patterns"]))
        self.exclusions.setPlaceholderText("예: Backup/**\n*.tmp\nnode_modules")
        self.exclusions.setMaximumHeight(150)
        scope_form.addRow("제외 경로·이름 패턴", self.exclusions)
        self.minimum = QDoubleSpinBox()
        self.maximum = QDoubleSpinBox()
        for spin in (self.minimum, self.maximum):
            spin.setRange(0, 16_777_216)
            spin.setDecimals(3)
            spin.setSuffix(" MiB")
        self.minimum.setValue(values["min_size"] / 1024 ** 2)
        self.maximum.setValue((values["max_size"] or 0) / 1024 ** 2)
        self._minimum_edited = False
        self._maximum_edited = False
        self.minimum.valueChanged.connect(lambda _value: setattr(self, "_minimum_edited", True))
        self.maximum.valueChanged.connect(lambda _value: setattr(self, "_maximum_edited", True))
        self.minimum.setToolTip(f"현재 정확한 기준: {values['min_size']:,}바이트. 이 필드를 바꾸지 않으면 기존 바이트 기준을 유지합니다.")
        self.maximum.setToolTip("0은 제한 없음입니다. 이 필드를 바꾸지 않으면 기존 바이트 기준을 유지합니다.")
        scope_form.addRow("최소 파일 크기", self.minimum)
        scope_form.addRow("최대 파일 크기 (0=제한 없음)", self.maximum)
        note = QLabel("세미콜론 또는 줄바꿈으로 구분합니다. 예: *.tmp, Cache, Backup/**. 제외한 폴더는 하위 파일도 검사하지 않습니다. 숨김 파일과 하위 폴더 포함 여부는 메인 화면에서 선택합니다.\n화면 검색·종류 필터는 표시만 바꾸며 검사 범위는 바꾸지 않습니다.")
        note.setWordWrap(True)
        scope_form.addRow(note)
        tabs.addTab(scope, "검사 범위")
        retention = QWidget()
        keep_form = QFormLayout(retention)
        self.rule = QComboBox()
        for key, label in RETENTION_LABELS.items():
            self.rule.addItem(label, key)
        self.rule.setCurrentIndex(self.rule.findData(values["retention_rule"]))
        keep_form.addRow("기본 보존 규칙", self.rule)
        preferred_row = QWidget()
        preferred_layout = QHBoxLayout(preferred_row)
        preferred_layout.setContentsMargins(0, 0, 0, 0)
        self.preferred = QLineEdit(values["preferred_folder"])
        self.preferred.setPlaceholderText("비워두면 지정하지 않음")
        preferred_layout.addWidget(self.preferred, 1)
        browse = QPushButton("선택")
        browse.clicked.connect(self._choose_preferred)
        preferred_layout.addWidget(browse)
        keep_form.addRow("선호 폴더", preferred_row)
        self.protected = QPlainTextEdit("\n".join(values["protected_folders"]))
        self.protected.setMaximumHeight(130)
        keep_form.addRow("보호 폴더 (한 줄에 하나)", self.protected)
        add_protected = QPushButton("보호 폴더 추가")
        add_protected.clicked.connect(self._add_protected)
        keep_form.addRow(add_protected)
        keep_note = QLabel("보호 폴더는 보존 규칙의 자동 선택에서 제외하며, 각 정확한 중복 그룹에 최소 한 파일을 남깁니다. 파일 목록에서 직접 체크한 항목은 별도 미리보기로 처리할 수 있습니다. 새 파일이 항상 더 좋은 원본이라는 뜻은 아닙니다. 프로그램 버전·품질을 판단하거나 유사 후보를 자동 정리하지 않습니다.")
        keep_note.setWordWrap(True)
        keep_form.addRow(keep_note)
        tabs.addTab(retention, "보존·보호")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("설정 저장")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _profile_changed(self):
        profile = self.profile.currentData()
        enabled = {"balanced": {"name", "text", "image"}, "photos": {"image"},
                   "documents": {"name", "text"}, "names": {"name"}}[profile]
        for method, control in self.methods.items():
            control.setChecked(method in enabled)
        for method, value in (("name", .82), ("text", .90), ("image", .90)):
            self.thresholds[method].setValue(value)

    def _choose_preferred(self):
        path = QFileDialog.getExistingDirectory(self, "우선 보존할 폴더 선택", self.preferred.text())
        if path:
            self.preferred.setText(path)

    def _add_protected(self):
        path = QFileDialog.getExistingDirectory(self, "항상 보존할 보호 폴더 선택")
        if path:
            current = self.protected.toPlainText().strip()
            self.protected.setPlainText((current + "\n" if current else "") + path)

    def _save(self):
        methods = [key for key, check in self.methods.items() if check.isChecked()]
        if not methods:
            QMessageBox.information(self, "비교 기준 필요", "유사 비교에 사용할 기준을 하나 이상 선택하세요.")
            return
        minimum = round(self.minimum.value() * 1024 ** 2) if self._minimum_edited else self.values["min_size"]
        maximum = (round(self.maximum.value() * 1024 ** 2) or None) if self._maximum_edited else self.values["max_size"]
        if maximum is not None and maximum < minimum:
            QMessageBox.information(self, "크기 범위 확인", "최대 크기는 최소 크기보다 작을 수 없습니다.")
            return
        patterns = [item.strip() for item in re.split(r"[;\n]+", self.exclusions.toPlainText()) if item.strip()]
        if len(patterns) > 100 or any(len(item) > 512 for item in patterns):
            QMessageBox.information(self, "제외 패턴 확인", "제외 패턴은 100개 이하, 각각 512자 이하로 입력하세요.")
            return
        try:
            patterns = list(normalize_exclude_patterns(patterns))
        except ValueError as exc:
            QMessageBox.information(self, "제외 패턴 확인", str(exc))
            return
        self.values = {**self.values, "profile": self.profile.currentData(), "methods": methods,
                       "search_mode": self.search_mode.currentData(),
                       **{key + "_threshold": spin.value() for key, spin in self.thresholds.items()},
                       "exclude_patterns": patterns, "min_size": minimum, "max_size": maximum,
                       "retention_rule": self.rule.currentData(), "preferred_folder": self.preferred.text().strip(),
                       "protected_folders": list(dict.fromkeys(line.strip() for line in self.protected.toPlainText().splitlines() if line.strip()))}
        self.accept()


_DETAIL_LABELS = {
    "id": "작업 번호", "root": "선택 폴더", "mode": "작업 방식", "created_at": "작업 시각",
    "status": "상태", "state": "처리 상태", "items": "파일별 결과", "results": "결과",
    "source": "원본", "target": "대상", "path": "파일", "output": "생성한 파일", "action": "작업",
    "error": "오류", "errors": "오류 목록", "warnings": "주의 사항", "details": "상세 내용",
    "format": "형식", "size": "파일 크기", "recovered_entries": "복구한 항목 수",
    "journal_path": "작업 기록 위치", "undo_status": "복원 상태", "undone_at": "복원 시각",
    "skipped": "건너뛴 항목", "complete": "완료", "partial": "부분 완료", "failed": "실패",
    "classification_summary": "분류 요약", "matched": "규칙 일치", "rules": "규칙 수", "rule": "적용 규칙",
    "retention_rule": "보존 규칙", "kept": "보존 파일", "selected": "처리할 사본",
    "output_created": "새 파일 생성", "output_verified": "새 파일 검증",
    "source_count": "병합한 원본 수", "removed_units": "겹친 문단·행 수",
    "archive_journal": "원본 안전 보관 기록", "move_originals": "병합 후 원본 이동",
    "recovery_metadata": "복원 정보 보관 방식", "format": "형식", "uncompressed_bytes": "압축 전 정보 바이트",
    "compressed_bytes": "압축 후 정보 바이트", "recovery_metadata_warning": "복원 정보 안내",
    "recovery_compaction": "복원 데이터 용량 최적화", "candidate_count": "보관 사본 수",
    "processed": "최적화한 사본 수", "raw_bytes": "기존 보관 용량", "retired_raw_bytes": "줄인 보관 사본 용량",
    "new_object_bytes": "새 복원 데이터 용량", "saved_bytes": "줄어든 보관 데이터 (파일 바이트)", "reason": "보관 방식 안내",
    "metadata_only": "경로·검증 정보만 포함", "requires_retained_data": "보관된 파일 데이터 필요",
}
_DETAIL_VALUES = {
    "healthy": "정상", "damaged": "손상 감지", "unsupported": "지원하지 않는 형식 또는 크기",
    "error": "오류", "repaired": "복구본 생성", "partial": "부분 처리 · 결과 확인 필요",
    "failed": "실패", "completed": "완료", "complete": "완료", "done": "완료",
    "pending": "처리 대기", "undone": "실행 취소 완료", "conflict": "충돌 · 원본 보존",
    "skipped": "건너뜀", "copy": "복사", "move": "이동", "quarantine": "안전 보관",
    "date": "날짜별 정리", "extension": "확장자별 정리", "ok": "정상",
    "rules": "규칙별 분류", "duplicate_auto": "검증된 중복 자동 분류",
    "unchanged": "기존 보관 방식 유지", "gzip": "압축 복원 정보", "legacy": "기존 작업 기록",
}
_INTERNAL_DETAIL_FIELDS = {"signature", "sha256", "mtime_ns", "device", "inode", "version", "token", "fingerprint"}
_INTERNAL_DETAIL_FIELDS.update({"target_payload", "original_backup_payload", "undo_backup_payload", "chunks",
                                "target_compaction", "original_backup_compaction", "undo_backup_compaction",
                                "restore_fingerprint", "snapshot_ctime_ns", "snapshot_reuse", "target_device", "target_inode"})


def readable_details(value, indent: int = 0) -> str:
    if indent >= 6:
        return "  " * indent + "[상세 항목 표시 생략]"
    """Present actionable results without exposing internal plan fields."""
    prefix = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in list(value.items())[:100]:
            if key in _INTERNAL_DETAIL_FIELDS:
                continue
            label = _DETAIL_LABELS.get(key, key)
            if isinstance(item, (list, dict)):
                lines.append(f"{prefix}{label}")
                lines.append(readable_details(item, indent + 1) or f"{prefix}  없음")
            elif key in {"size", "raw_bytes", "uncompressed_bytes", "compressed_bytes", "retired_raw_bytes", "new_object_bytes", "saved_bytes"} and isinstance(item, int):
                lines.append(f"{prefix}{label}: {format_size(item)}")
            else:
                if isinstance(item, bool):
                    display = "예" if item else "아니요"
                elif isinstance(item, str):
                    display = _DETAIL_VALUES.get(item, item[:4096])
                else:
                    display = "없음" if item is None else str(item)
                lines.append(f"{prefix}{label}: {display}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        text = "\n\n".join(f"{prefix}[{i}]\n{readable_details(item, indent + 1)}"
                           for i, item in enumerate(value[:200], 1))
        return text + (f"\n{prefix}[나머지 {len(value) - 200:,}개 표시 생략]" if len(value) > 200 else "")
    return prefix + str(value)[:4096]


class Worker(QThread):
    progress = Signal(str)
    progress_detail = Signal(object)

    def __init__(self, function: Callable[["Worker"], object], parent: QObject):
        super().__init__(parent)
        self.function = function
        self.cancel_event = threading.Event()
        self.result: object = None
        self.error: str | None = None
        self._last_report = 0.0
        self._last_detail_report = 0.0
        self._last_detail_phase = None

    def report(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_report >= 0.07:
            self.progress.emit(str(message))
            self._last_report = now

    def report_detail(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        now = time.monotonic()
        phase = event.get("phase")
        if (event.get("terminal") or phase != self._last_detail_phase
                or now - self._last_detail_report >= .07):
            self.progress_detail.emit(dict(event))
            self._last_detail_phase = phase
            self._last_detail_report = now

    def run(self) -> None:
        try:
            self.result = self.function(self)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"


class ImagePreviewLabel(QLabel):
    """Keep the entire bounded preview visible when the dialog is resized."""
    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self._original = QPixmap.fromImage(image)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(100, 100)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._fit_image()

    def _fit_image(self):
        if not self._original.isNull():
            bounds = QSize(min(450, max(1, self.width())), min(450, max(1, self.height())))
            self.setPixmap(self._original.scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_image()


class FileTableModel(QAbstractTableModel):
    selectionChanged = Signal()
    HEADERS = ("선택", "파일 이름", "종류", "용량", "수정한 날짜", "만든 날짜", "상대 경로")

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.entries: list[FileEntry] = []
        self.checked: set[str] = set()
        self.path_rows: dict[str, int] = {}
        self.search_text: list[str] = []
        self.sort_text: list[tuple[str, str, str]] = []
        self._display_cache = OrderedDict()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.entries):
            return None
        entry = self.entries[index.row()]
        column = index.column()
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            return Qt.CheckState.Checked if entry.path in self.checked else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return ""
            if column == 1:
                return entry.name
            if column == 2:
                return "응용 프로그램" if entry.kind == "application" else (entry.suffix.upper().lstrip(".") or "파일")
            if column == 6:
                return entry.relative_path
            key = index.row(), column
            if key not in self._display_cache:
                self._display_cache[key] = format_size(entry.size) if column == 3 else format_date(entry.modified if column == 4 else entry.created)
                if len(self._display_cache) > 4096:
                    self._display_cache.popitem(last=False)
            self._display_cache.move_to_end(key)
            return self._display_cache[key]
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == 5:
                return f"생성일 정보: {entry.created_source}. Linux 등에서 실제 생성일을 제공하지 않으면 표시하지 않습니다."
            return entry.path
        if role == Qt.ItemDataRole.TextAlignmentRole and column == 3:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def flags(self, index):
        flags = super().flags(index)
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if index.isValid() and index.column() == 0 and role == Qt.ItemDataRole.CheckStateRole:
            path = self.entries[index.row()].path
            if value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value):
                self.checked.add(path)
            else:
                self.checked.discard(path)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            self.selectionChanged.emit()
            return True
        return False

    def set_entries(self, entries: list[FileEntry]) -> None:
        self.beginResetModel()
        self.entries = list(entries)
        self.path_rows = {item.path: i for i, item in enumerate(self.entries)}
        self.search_text = [(item.relative_path + " " + item.name).casefold() for item in self.entries]
        self.sort_text = [(item.name.casefold(), ("응용 프로그램" if item.kind == "application" else (item.suffix.lstrip(".") or "파일")).casefold(),
                           item.relative_path.casefold()) for item in self.entries]
        self._display_cache.clear()
        self.checked.clear()
        self.endResetModel()
        self.selectionChanged.emit()

    def set_checked(self, paths: list[str] | set[str], replace: bool = True) -> None:
        paths = set(paths).intersection(self.path_rows)
        if replace:
            self.checked = paths
        else:
            self.checked.update(paths)
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.ItemDataRole.CheckStateRole])
        self.selectionChanged.emit()

    def sort_value(self, row: int, column: int):
        entry = self.entries[row]
        if column == 0:
            return entry.path in self.checked
        if column == 3:
            return entry.size
        if column == 4:
            return entry.modified
        if column == 5:
            return entry.created if entry.created is not None else float("-inf")
        return self.sort_text[row][{1: 0, 2: 1, 6: 2}[column]]


class FileFilter(QSortFilterProxyModel):
    IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic"}
    DOCUMENT = {".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf", ".hwp"}
    ARCHIVE = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ""
        self.kind = "all"
        self.quick = "all"
        self.age_cutoff = time.time() - 365 * 24 * 60 * 60

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        entry = model.entries[row]
        if self.query and self.query not in model.search_text[row]:
            return False
        if (self.quick == "large" and entry.size < 100 * 1024 ** 2
                or self.quick == "empty" and entry.size != 0
                or self.quick == "old" and entry.modified >= self.age_cutoff):
            return False
        suffix = entry.suffix.casefold()
        return (
            self.kind == "all" or
            (self.kind == "application" and entry.kind == "application") or
            (self.kind == "file" and entry.kind != "application") or
            (self.kind == "image" and suffix in self.IMAGE) or
            (self.kind == "document" and suffix in self.DOCUMENT) or
            (self.kind == "archive" and suffix in self.ARCHIVE)
        )

    def lessThan(self, left, right):
        model = self.sourceModel()
        return model.sort_value(left.row(), left.column()) < model.sort_value(right.row(), right.column())


class PlanTableModel(QAbstractTableModel):
    def __init__(self, items: list[dict], parent=None):
        super().__init__(parent)
        self.items = items

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 4

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return ("작업", "원본", "대상", "적용 규칙")[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            item = self.items[index.row()]
            value = str(item.get(("action", "source", "target", "rule")[index.column()], ""))
            return _DETAIL_VALUES.get(value, value)
        return None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Folder Organizer · {__version__}")
        self.setMinimumSize(1100, 700)
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        self.resize(min(1360, max(1100, available.width() - 60)) if available else 1280,
                    min(860, max(700, available.height() - 70)) if available else 800)
        self.root: str | None = None
        self.scan_result: ScanResult | None = None
        self.scan_complete = False
        self.groups: list[MatchGroup] = []
        self.match_kind: str | None = None
        self._worker: Worker | None = None
        self._success: Callable | None = None
        self._task_label = ""
        self._cancellable = False
        self._failure: Callable[[str], None] | None = None
        self._pending_update: dict | None = None
        self._update_worker: Worker | None = None
        self._active_update_manual = False
        self._close_requested = False
        self._update_diagnostic_notice = ""
        self._update_retry_requested = False
        self._deferred_update_manual = False
        self._task_id = ""
        self._task_started = 0.0
        self._progress_snapshot = None
        self._progress_tracker = ProgressTracker()
        self._analysis_cache = AnalysisCache()
        self._history_dialog = None
        self._log_dialog = None
        self._help_dialog = None
        self._execution_log_dialog = None
        self._merge_dialog = None
        self._merge_execute_pending = False
        self._overview_dialog = None
        self._match_items = {}
        self._last_tree_checked = set()
        self._fast_removal_completed = False
        self._guidance_action = "folder"
        self._is_busy = False
        self.settings = QSettings("P-JSL", "FolderOrganizer")
        self.analysis_options = analysis_preferences(self.settings)
        self.match_report: dict = {}
        self.last_retention: dict | None = None
        self._work_controls: list[QWidget] = []
        self._build_ui()
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(500)
        self.progress_timer.timeout.connect(self._refresh_progress_time)
        self.set_theme(self.settings.value("appearance/theme", "dark"), persist=False)
        self.set_busy(False)
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(6 * 60 * 60 * 1000)
        self.update_timer.timeout.connect(self.request_update_check)
        self.update_timer.start()
        self.initial_update_timer = QTimer(self)
        self.initial_update_timer.setSingleShot(True)
        self.initial_update_timer.timeout.connect(self._initial_update_check)
        self.initial_update_timer.start(5000)
        self.deferred_update_timer = QTimer(self)
        self.deferred_update_timer.setSingleShot(True)
        self.deferred_update_timer.timeout.connect(self._resume_deferred_update)

    def _button(self, text, callback, *, primary=False, caution=False, work=True):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
        if caution:
            button.setObjectName("caution")
        button.clicked.connect(callback)
        if work:
            self._work_controls.append(button)
        return button

    def _menu_action(self, menu, text, callback, *, shortcut=None, work=True):
        action = menu.addAction(text)
        action.triggered.connect(lambda _checked=False: callback())
        if shortcut:
            action.setShortcut(shortcut)
        if work:
            self._work_controls.append(action)
        return action

    def _build_header_menus(self):
        bar = self.menuBar()
        bar.setNativeMenuBar(False)
        self.file_menu = bar.addMenu("파일")
        self._menu_action(self.file_menu, "폴더 선택…", self.choose_folder, shortcut="Ctrl+O")
        self.recent_menu = self.file_menu.addMenu("최근 폴더")
        self._refresh_recent_menu()
        self._work_controls.append(self.recent_menu.menuAction())
        self.scan_button = self._menu_action(self.file_menu, "다시 분석", self.start_scan, shortcut="F5")
        self.file_menu.addSeparator()
        self._menu_action(self.file_menu, "표시된 파일 모두 체크", self.select_visible, shortcut="Ctrl+A")
        self._menu_action(self.file_menu, "전체 체크 해제", self.clear_selection, shortcut="Ctrl+Shift+A")
        self._menu_action(self.file_menu, "파일 위치 열기", self.reveal_selected)
        self.export_menu = self.file_menu.addMenu("내보내기")
        self._menu_action(self.export_menu, "표시된 파일 목록 · CSV", self.export_csv)
        self._menu_action(self.export_menu, "분석 보고서 · HTML / JSON", self.export_match_report)
        self.file_menu.addSeparator()
        self._menu_action(self.file_menu, "종료", self.close, work=False)

        self.analysis_menu = bar.addMenu("검사")
        self.duplicates_button = self._menu_action(self.analysis_menu, "중복 검사 · 완전히 같은 내용", self.analyze_duplicates, shortcut="Ctrl+D")
        self.similar_button = self._menu_action(self.analysis_menu, "유사 비교 · 비슷한 이름과 내용", self.analyze_similar, shortcut="Ctrl+Shift+D")
        self.overview_action = self._menu_action(self.analysis_menu, "용량·정리 요약…", self.show_overview)
        self.analysis_menu.addSeparator()
        self._menu_action(self.analysis_menu, "검사 범위 · 보존 설정…", self.show_analysis_settings)

        self.organize_menu = bar.addMenu("정리")
        self.classify_button = self._menu_action(self.organize_menu, "규칙으로 자동 분류…", self.show_classification, shortcut="Ctrl+R")
        self.duplicate_classification_action = self._menu_action(self.organize_menu, "검증된 중복 자동 분류…", self.classify_duplicates)
        self.keep_first_delete_action = self._menu_action(self.organize_menu, "첫 번째만 남기고 나머지 삭제", self.keep_first_delete)
        self.organize_menu.addSeparator()
        self.merge_action = self._menu_action(self.organize_menu, "체크한 파일 내용 병합…", self.show_merge, shortcut="Ctrl+M")
        self.merge_action.setToolTip("텍스트·Markdown·CSV·TSV에서 겹치는 내용을 합쳐 새 파일로 만듭니다. 원본 이동은 선택 사항입니다.")
        self.manual_menu = self.organize_menu.addMenu("체크한 파일 수동 정리")
        manual_menu = self.manual_menu
        for label, mode, basis in (("안전 보관…", "quarantine", "modified"),
                                   ("선택 폴더에 복사…", "copy", "modified"),
                                   ("선택 폴더로 이동…", "move", "modified")):
            self._menu_action(manual_menu, label, lambda mode=mode, basis=basis: self._manual_action(mode, basis))
        self.organize_menu.addSeparator()
        self.assume_duplicates_button = self._menu_action(self.organize_menu, "선택 항목을 중복으로 간주…", self.assume_duplicates)
        self.assume_duplicates_button.setToolTip("중복 검증 없이 사용자가 직접 지정한 파일을 안전 보관합니다. 남길 원본을 직접 확인하세요.")

        self.records_menu = bar.addMenu("기록")
        self._menu_action(self.records_menu, "실행 로그…", self.show_execution_logs, work=False)
        self._menu_action(self.records_menu, "작업 기록 · 복원…", self.show_history)
        self.records_menu.addSeparator()
        self._menu_action(self.records_menu, "활동 · 진단 로그…", self.show_logs, work=False)

        self.view_menu = bar.addMenu("보기")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        self.theme_actions = {}
        for mode, label in (("dark", "다크 모드 (기본)"), ("light", "라이트 모드")):
            action = self.view_menu.addAction(label)
            action.setCheckable(True)
            theme_group.addAction(action)
            action.triggered.connect(lambda _checked=False, mode=mode: self.set_theme(mode))
            self.theme_actions[mode] = action

        self.tools_menu = bar.addMenu("도구")
        self._menu_action(self.tools_menu, "선택 파일 손상 검사", self.inspect_selected)
        self._menu_action(self.tools_menu, "원본을 보존하고 복구본 만들기", self.repair_selected)
        self.tools_menu.addSeparator()
        self.update_button = self._menu_action(self.tools_menu, "업데이트 확인", self.manual_update_check, work=False)
        self.auto_update_check = self.tools_menu.addAction("자동 업데이트")
        self.auto_update_check.setCheckable(True)
        self.auto_update_check.setChecked(self.settings.value("updates/enabled", True, type=bool))
        self.auto_update_check.setToolTip("Windows 배포 버전에서 새 릴리스를 준비하고 앱 종료 후 적용합니다.")
        self.auto_update_check.toggled.connect(self._auto_update_changed)
        self.update_status = QLabel("종료 후 새 버전 적용" if updater.is_auto_update_supported() else "내 PC에서 처리 · 소스 실행")
        self.update_status.setObjectName("muted")
        self.update_status.setToolTip("파일은 서버에 업로드하지 않습니다. 자동 업데이트는 Windows 배포 버전에서 사용할 수 있습니다.")

        help_menu = bar.addMenu("도움말")
        self._menu_action(help_menu, "기능 안내…", self.show_help, shortcut="F1", work=False)
        self._menu_action(help_menu, "중복과 유사의 차이", self.show_comparison_guide, work=False)
        self.header_corner = QWidget(bar)
        corner = self.header_corner
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(12, 0, 16, 0)
        brand = QLabel(f"FOLDER ORGANIZER   ·   v{__version__}")
        brand.setObjectName("brand")
        corner_layout.addWidget(brand)
        self.help_button = QToolButton()
        self.help_button.setText("?")
        self.help_button.setToolTip("기능 안내 (F1)")
        self.help_button.clicked.connect(self.show_help)
        corner_layout.addWidget(self.help_button)
        bar.setCornerWidget(corner)

    def set_theme(self, mode, *, persist=True):
        self.theme_mode = apply_theme(self, mode)
        if persist:
            self.settings.setValue("appearance/theme", self.theme_mode)
        for key, action in self.theme_actions.items():
            action.setChecked(key == self.theme_mode)

    def _manual_action(self, mode, basis):
        # Date and extension classification share the rule editor. Keep legacy
        # callers working without exposing duplicate, less flexible workflows.
        if mode in {"date", "extension"}:
            preset = "created_date" if mode == "date" and basis == "created" else mode
            self.show_classification(preset=preset)
            return
        index = next((index for index in range(self.action_combo.count())
                      if self.action_combo.itemData(index) == (mode, basis)), -1)
        if index < 0:
            raise ValueError("지원하지 않는 수동 정리 방식입니다.")
        self.action_combo.setCurrentIndex(index)
        self.preview_selected()

    def _recent_folders(self):
        try:
            raw = self.settings.value("navigation/recentFolders", "[]")
            values = json.loads(raw) if isinstance(raw, str) and len(raw) <= 50_000 else []
            if not isinstance(values, list):
                return []
            return list(dict.fromkeys(path for path in values if isinstance(path, str)
                                      and 0 < len(path) <= 4096 and "\0" not in path and Path(path).is_absolute()))[:10]
        except (ValueError, TypeError):
            return []

    def _refresh_recent_menu(self):
        self.recent_menu.clear()
        paths = self._recent_folders()
        for path in paths:
            action = self.recent_menu.addAction(path.replace("&", "&&"))
            action.setToolTip("현재 검사 범위로 다시 분석합니다. 이전 결과를 그대로 사용하지 않습니다.")
            action.triggered.connect(lambda _checked=False, path=path: self._open_folder(path))
        if paths:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("최근 목록 지우기 · 파일은 유지", self._clear_recent_folders)
        else:
            self.recent_menu.addAction("아직 최근 폴더가 없습니다").setEnabled(False)

    def _clear_recent_folders(self):
        self.settings.setValue("navigation/recentFolders", "[]")
        self._refresh_recent_menu()

    def _remember_folder(self, path):
        paths = [path] + [value for value in self._recent_folders() if value != path]
        self.settings.setValue("navigation/recentFolders", json.dumps(paths[:10], ensure_ascii=False))
        self._refresh_recent_menu()

    def show_overview(self):
        if not self._require_scan():
            return
        if self._overview_dialog is not None and self._overview_dialog.isVisible():
            self._overview_dialog.raise_()
            return
        scan = self.scan_result
        self._start_task("파일 용량과 정리 대상을 요약하고 있습니다…",
                         lambda _worker: summarize_inventory(scan.files, scan.root, top_limit=20),
                         lambda summary: self._show_overview(scan, summary))

    def _show_overview(self, scan, summary):
        dialog = InventoryOverviewDialog(scan, self, summary=summary)
        self._overview_dialog = dialog
        def choose(paths):
            # Selection alone never performs an operation. Clear display-only
            # filters so all newly checked paths can be reviewed immediately.
            self.search_input.clear()
            self.type_filter.setCurrentIndex(0)
            self.quick_filter.setCurrentIndex(0)
            self.views.setCurrentIndex(0)
            self.model.set_checked(paths)
            self.status_label.setText("기존 체크를 요약에서 고른 파일로 바꿨습니다. 정리 방식을 고르고 미리보기에서 확인하세요.")
        def closed(_result):
            if self._overview_dialog is dialog:
                self._overview_dialog = None
            dialog.deleteLater()
        dialog.files_requested.connect(choose)
        dialog.finished.connect(closed)
        dialog.open()

    def _clear_match_tree(self):
        self._match_items.clear()
        self._last_tree_checked.clear()
        self.match_tree.clear()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self._build_header_menus()

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(28, 22, 28, 14)
        layout.setSpacing(6)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel("어떤 파일을 정리할까요?")
        self.guidance_title = title
        title.setObjectName("title")
        titles.addWidget(title)
        subtitle = QLabel("먼저 폴더를 선택하세요. 내용을 살펴보는 동안 파일은 바뀌지 않습니다.")
        self.guidance_text = subtitle
        subtitle.setWordWrap(True)
        subtitle.setObjectName("subtitle")
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.next_step_button = self._button("폴더 선택", self._run_next_step, primary=True)
        header.addWidget(self.next_step_button)
        self.folder_button = self._button("폴더 선택", self.choose_folder, primary=True)
        self.folder_button.setToolTip("분석할 개인 폴더 선택 (Ctrl+O)")
        header.addWidget(self.folder_button)
        layout.addLayout(header)

        folder_row = QHBoxLayout()
        self.folder_input = QLineEdit()
        self.folder_input.setReadOnly(True)
        self.folder_input.setPlaceholderText("개인 컴퓨터의 폴더를 선택하세요")
        folder_row.addWidget(self.folder_input, 1)
        layout.addLayout(folder_row)
        options = QHBoxLayout()
        self.recursive_check = QCheckBox("하위 폴더 포함")
        self.recursive_check.setChecked(self.settings.value("analysis/recursive", True, type=bool))
        self.hidden_check = QCheckBox("숨김 파일 포함")
        self.hidden_check.setChecked(self.settings.value("analysis/hidden", False, type=bool))
        self.recursive_check.toggled.connect(self._inventory_option_changed)
        self.hidden_check.toggled.connect(self._inventory_option_changed)
        options.addWidget(self.recursive_check)
        options.addWidget(self.hidden_check)
        options.addStretch()
        self.scope_label = QLabel()
        self.scope_label.setObjectName("muted")
        options.addWidget(self.scope_label)
        options.setContentsMargins(0, 0, 0, 0)
        self.scope_panel = QWidget()
        self.scope_panel.setLayout(options)
        layout.addWidget(self.scope_panel)
        self._work_controls.extend((self.recursive_check, self.hidden_check))

        cards = QHBoxLayout()
        self.count_label = self._card(cards, "분석한 파일", "—")
        self.size_label = self._card(cards, "전체 용량", "—")
        self.selected_label = self._card(cards, "작업 대상 · 체크한 파일", "0개")
        self.reclaim_label = self._card(cards, "중복 사본 용량 · 즉시 확보 아님", "—")
        cards.setContentsMargins(0, 0, 0, 0)
        self.summary_panel = QWidget()
        self.summary_panel.setLayout(cards)
        layout.addWidget(self.summary_panel)

        filters = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("파일 이름 또는 경로 검색")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._filter_changed)
        filters.addWidget(self.search_input, 1)
        self.type_filter = QComboBox()
        for label, value in (("모든 종류", "all"), ("일반 파일", "file"), ("응용 프로그램", "application"),
                             ("이미지", "image"), ("문서", "document"), ("압축 파일", "archive")):
            self.type_filter.addItem(label, value)
        self.type_filter.currentIndexChanged.connect(self._filter_changed)
        filters.addWidget(self.type_filter)
        self.quick_filter = QComboBox()
        for label, key in (("전체 파일", "all"), ("대용량 (100 MiB 이상)", "large"),
                           ("빈 파일 (0 B)", "empty"), ("오래된 파일 (수정 후 1년)", "old")):
            self.quick_filter.addItem(label, key)
        self.quick_filter.setToolTip("목록 표시만 바꿉니다. 오래된 파일은 수정일 기준이며 빈 파일도 필요한 파일일 수 있습니다. 검사 범위와 체크 상태는 유지합니다.")
        self.quick_filter.currentIndexChanged.connect(self._filter_changed)
        filters.addWidget(self.quick_filter)
        filters.setContentsMargins(0, 0, 0, 0)
        self.filter_panel = QWidget()
        self.filter_panel.setLayout(filters)
        layout.addWidget(self.filter_panel)

        self.model = FileTableModel(self)
        self.proxy = FileFilter(self)
        self.proxy.setSourceModel(self.model)
        self.model.selectionChanged.connect(self._selection_changed)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(35)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((50, 220, 110, 90, 155, 155, 240)):
            self.table.setColumnWidth(column, width)
        self.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.table.doubleClicked.connect(self._reveal_index)

        self.match_panel = QWidget()
        matches_layout = QVBoxLayout(self.match_panel)
        matches_layout.setContentsMargins(0, 7, 0, 0)
        match_header = QHBoxLayout()
        self.match_label = QLabel("중복 · 유사 파일 비교 결과")
        self.match_label.setObjectName("section")
        self.match_label.setWordWrap(True)
        match_header.addWidget(self.match_label, 1)
        match_header.addWidget(self._button("결과 읽는 법", lambda: self.show_feature_help(
            "duplicates" if self.match_kind == "duplicate" else "similar"), work=False))
        self.select_duplicates_button = self._button("첫 파일 보존 선택", self.select_duplicate_candidates)
        self.select_duplicates_button.setToolTip("경로순 첫 파일을 보존하는 규칙입니다. 보호 폴더와 현재 내용을 다시 검증한 뒤 사본을 선택합니다. 파일 변경은 별도 미리보기에서 확인합니다.")
        # The retention selector below provides the same first-file policy.
        # Keep one visible route for choosing keepers, alongside direct removal.
        self.select_duplicates_button.setParent(self)
        self.select_duplicates_button.hide()
        self.keep_first_delete_button = self._button("첫 번째 파일만 남기고 나머지 삭제", self.keep_first_delete, caution=True)
        self.keep_first_delete_button.setToolTip("현재 검사 결과의 첫 파일과 보호 파일을 남기고 즉시 실행합니다. 전체 중복 검사를 반복하지 않으며 실행 중 변경된 파일은 처리하지 않습니다. 나머지는 복원 가능한 delete_files 폴더로 이동합니다.")
        match_header.addWidget(self.keep_first_delete_button)
        matches_layout.addLayout(match_header)
        self.keep_first_hint = QLabel("클릭하면 전체 중복 그룹의 첫 파일·보호 파일을 남기고 나머지를 delete_files로 이동합니다. 추가 전체 검사 없이 실행 · 복원 가능")
        self.keep_first_hint.setObjectName("muted")
        self.keep_first_hint.setWordWrap(True)
        matches_layout.addWidget(self.keep_first_hint)
        compare_controls = QHBoxLayout()
        self.match_filter = QComboBox()
        for label, key in (("모든 근거", "all"), ("이름만 유사", "name_only"), ("내용 근거 있음", "content"),
                           ("텍스트 근거", "text"), ("이미지 근거", "image")):
            self.match_filter.addItem(label, key)
        self.match_filter.currentIndexChanged.connect(self._render_match_tree)
        compare_controls.addWidget(self.match_filter)
        self.compare_button = self._button("두 파일 나란히 비교", self.compare_current_group)
        compare_controls.addWidget(self.compare_button)
        self.merge_group_button = self._button("선택 후보 내용 병합…", self.merge_current_group)
        self.merge_group_button.setToolTip("같거나 비슷한 제목이어도 직접 내용을 확인합니다. 지원되는 텍스트·CSV 후보만 병합할 수 있습니다.")
        compare_controls.addWidget(self.merge_group_button)
        self.retention_rule = QComboBox()
        for key, label in RETENTION_LABELS.items():
            self.retention_rule.addItem(label, key)
        self.retention_rule.setCurrentIndex(self.retention_rule.findData(self.analysis_options["retention_rule"]))
        self.retention_rule.currentIndexChanged.connect(self._retention_rule_changed)
        self.retention_rule.setToolTip("정확한 중복 그룹에서 보존할 파일의 규칙입니다. 유사 후보에는 적용하지 않습니다.")
        compare_controls.addWidget(self.retention_rule)
        self.retention_button = self._button("보존 규칙으로 선택", self.select_by_retention)
        compare_controls.addWidget(self.retention_button)
        compare_controls.addStretch()
        matches_layout.addLayout(compare_controls)
        self.match_explanation = QLabel("중복은 내용이 완전히 같은 파일입니다. 유사는 직접 확인할 후보입니다.")
        self.match_explanation.setObjectName("muted")
        self.match_explanation.setWordWrap(True)
        matches_layout.addWidget(self.match_explanation)
        self.match_tree = QTreeWidget()
        self.match_tree.setHeaderLabels(("그룹 / 파일", "판별 근거", "각 기준 점수"))
        self.match_tree.setColumnWidth(0, 410)
        self.match_tree.setColumnWidth(1, 400)
        self.match_tree.itemChanged.connect(self._match_selection_changed)
        self.match_tree.itemDoubleClicked.connect(lambda *_: self.compare_current_group())
        self.match_tree.setMinimumHeight(86)
        matches_layout.addWidget(self.match_tree, 1)
        self.views = QTabWidget()
        self.file_page = QWidget()
        file_layout = QVBoxLayout(self.file_page)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.empty_state = QLabel("먼저 폴더를 선택하면 파일 목록을 보여드려요\n\n"
                                 "중복 검사  ·  이름이 달라도 내용이 완전히 같은 복사본 찾기\n\n"
                                 "유사 비교  ·  비슷한 사진·문서·이름을 찾아 직접 비교하기\n\n"
                                 "자동 분류  ·  종류·날짜·내 규칙에 따라 폴더 나누기\n\n"
                                 "검사만으로 파일은 바뀌지 않습니다. 기능 안내(F1)에서 예시를 볼 수 있어요.")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setObjectName("muted")
        self.empty_state.setWordWrap(True)
        file_layout.addWidget(self.empty_state, 1)
        file_layout.addWidget(self.table, 1)
        self.table.hide()
        self.views.addTab(self.file_page, "전체 파일")
        self.views.addTab(self.match_panel, "비교 결과")
        self.views.setTabVisible(1, False)
        layout.addWidget(self.views, 1)

        self.similarity_threshold = QDoubleSpinBox(self)
        self.similarity_threshold.setRange(0.50, 1.00)
        self.similarity_threshold.setSingleStep(0.01)
        self.similarity_threshold.setDecimals(2)
        self.similarity_threshold.setValue(self.analysis_options["name_threshold"])
        self.similarity_threshold.valueChanged.connect(self._name_threshold_changed)
        self.similarity_threshold.hide()
        self._work_controls.append(self.similarity_threshold)

        actions = QHBoxLayout()
        self.selection_panel = QWidget()
        selected_layout = QVBoxLayout(self.selection_panel)
        selected_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_hint = QLabel()
        self.selection_hint.setObjectName("muted")
        self.selection_hint.setWordWrap(True)
        selected_layout.addWidget(self.selection_hint)
        self.action_combo = QComboBox()
        for label, mode, basis in (("삭제 · delete_files로 이동 (복원 가능)", "quarantine", "modified"),
                                   ("선택 폴더에 복사", "copy", "modified"),
                                   ("선택 폴더로 이동", "move", "modified")):
            self.action_combo.addItem(label, (mode, basis))
        self.action_combo.currentIndexChanged.connect(self._refresh_action_hint)
        actions.addWidget(self.action_combo, 1)
        self._work_controls.append(self.action_combo)
        self.preview_button = self._button("작업 미리보기", self.preview_selected, primary=True)
        actions.addWidget(self.preview_button)
        selected_layout.addLayout(actions)
        layout.addWidget(self.selection_panel)

        self.visible_label = QLabel("분석 대기 중")
        self.visible_label.setObjectName("muted")
        filters.addWidget(self.visible_label)
        self.warning_label = QLabel()
        self.warning_label.setObjectName("warning")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        footer = QHBoxLayout()
        self.status_label = QLabel("폴더를 선택하면 파일 용량과 날짜를 분석합니다.")
        self.status_label.setObjectName("muted")
        self.status_label.setWordWrap(True)
        footer.addWidget(self.status_label, 1)
        self.progress_time_label = QLabel("")
        self.progress_time_label.setObjectName("muted")
        self.progress_time_label.hide()
        footer.addWidget(self.progress_time_label)
        self.cancel_button = self._button("분석 중단", self.cancel_task, work=False)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.update_status)
        layout.addLayout(footer)
        shell.addWidget(content, 1)
        self._work_controls.append(self.table)
        self._work_controls.extend((self.match_filter, self.match_tree, self.retention_rule))
        self._refresh_scope_label()
        self._refresh_action_hint()
        self.views.currentChanged.connect(self._refresh_guidance)
        self.match_tree.currentItemChanged.connect(self._refresh_guidance)

    def _hidden_selected_count(self, paths=None) -> int:
        if not hasattr(self, "proxy"):
            return 0
        return sum(not self.proxy.mapFromSource(self.model.index(self.model.path_rows[path], 0)).isValid()
                   for path in (self.model.checked if paths is None else paths)
                   if path in self.model.path_rows)

    def _refresh_action_hint(self, *_args):
        if not hasattr(self, "selection_hint") or not hasattr(self, "action_combo"):
            return
        mode, _basis = self.action_combo.currentData() or ("quarantine", "modified")
        effects = {
            "quarantine": "삭제 파일을 delete_files로 옮기고 목록에서 제외합니다. 작업 기록에서 복원할 수 있습니다.",
            "copy": "복사: 원본을 남기고 대상 폴더에 사본을 만듭니다.",
            "move": "이동: 대상 폴더로 옮기며 보호 사본과 복원 기록을 남깁니다.",
            "date": "날짜별 정리: 선택한 날짜 기준으로 연월 폴더에 옮깁니다.",
            "extension": "확장자별 정리: 파일 종류별 폴더로 옮깁니다.",
        }
        count = len(self.model.checked) if hasattr(self, "model") else 0
        hidden = self._hidden_selected_count()
        selection = f"수동 정리 · 체크 {count:,}개" if count else "아래 수동 정리는 체크한 파일만 대상입니다. 행 클릭은 체크가 아닙니다."
        if hidden:
            selection += f" · 목록 필터로 숨겨진 {hidden:,}개 포함"
        self.selection_hint.setText(selection + "  |  " + effects[mode])
        self.selection_panel.setVisible(bool(count))
        self.preview_button.setText(f"체크한 {count:,}개 미리보기" if count else "작업 미리보기")
        self.preview_button.setToolTip("현재 목록에서 보이지 않는 체크 항목도 포함됩니다. 다음 화면에서 전체 원본·대상 경로를 확인하세요.")
        self._refresh_guidance()

    def _refresh_guidance(self, *_args):
        if not hasattr(self, "next_step_button"):
            return
        comparison_view = self.views.currentIndex() == 1 and self.match_kind is not None
        for panel in (self.scope_panel, self.summary_panel, self.filter_panel):
            panel.setVisible(not comparison_view)
        if self._is_busy:
            title, detail, action, button = "작업을 진행하고 있어요", "아래 진행도와 경과 시간을 확인하세요. 기능 안내는 작업 중에도 볼 수 있습니다.", "none", "진행 중"
        elif not self.root:
            title, detail, action, button = "어떤 파일을 정리할까요?", "먼저 폴더를 선택하세요. 내용을 살펴보는 동안 파일은 바뀌지 않습니다.", "folder", "폴더 선택"
        elif not self.scan_complete:
            title, detail, action, button = "폴더 분석을 완료해 주세요", "검사 범위가 바뀌었거나 분석이 중단됐습니다. 다시 분석한 뒤 비교·정리할 수 있습니다.", "scan", "다시 분석"
        elif not self.model.entries:
            title, detail, action, button = "조건에 맞는 파일이 없어요", "검사 설정의 제외 조건을 확인하거나 다른 폴더를 선택해 보세요.", "settings", "검사 조건 확인"
        elif self._fast_removal_completed:
            title, detail, action, button = "중복 사본을 안전하게 정리했어요", "각 그룹의 첫 파일과 보호 파일은 남겼습니다. 작업 기록에서 원래 위치로 복원할 수 있습니다.", "overview", "남은 파일 요약"
        elif self.model.checked:
            title, detail, action, button = f"체크한 파일 {len(self.model.checked):,}개를 확인하세요", "아래에서 정리 방식을 고르세요. 미리보기에서 전체 경로를 확인해야 실행됩니다.", "preview", "변경 미리보기"
        elif self.match_kind == "duplicate" and self.groups:
            title, detail, action, button = "내용이 같은 파일을 찾았어요", "보존 기준을 고른 뒤 나머지 사본을 체크하세요. 첫 파일이 최신 파일을 뜻하지는 않습니다.", "retention", "남길 파일 정하기"
        elif self.match_kind == "similar" and self.groups:
            title, detail, action, button = "비슷한 파일은 직접 비교하세요", "후보를 선택해 두 파일을 나란히 보세요. 높은 점수만으로 같은 파일이라고 판단하지 않습니다.", "compare", "선택 후보 비교" if self.match_tree.currentItem() else "첫 후보 비교"
            if not self.match_tree.topLevelItemCount():
                title, detail, action, button = "현재 결과 필터에는 후보가 없어요", "전체 후보를 다시 보거나 다른 근거 필터를 골라 보세요.", "results", "전체 후보 보기"
        elif self.match_kind:
            title, detail, action, button = "현재 결과에 검토할 후보가 없어요", "검사 조건을 바꾸거나 종류·날짜 규칙으로 폴더를 나눠 보세요. 읽기 제한은 아래 주의 내용을 확인하세요.", "rules", "규칙으로 분류"
        else:
            title, detail, action, button = f"파일 {len(self.model.entries):,}개를 살펴봤어요", "같은 내용의 복사본을 찾거나, 자동 분류로 종류·날짜별 폴더를 나눌 수 있습니다.", "duplicates", "같은 파일 찾기"
        self.guidance_title.setText(title)
        self.guidance_text.setText(detail)
        self._guidance_action = action
        self.next_step_button.setText(button)
        self.next_step_button.setVisible(action not in {"folder", "none"})
        self.next_step_button.setEnabled(not self._is_busy)
        self.folder_button.setText("폴더 변경" if self.root else "폴더 선택")
        folder_style = "" if self.root else "primary"
        if self.folder_button.objectName() != folder_style:
            self.folder_button.setObjectName(folder_style)
            self.folder_button.style().unpolish(self.folder_button)
            self.folder_button.style().polish(self.folder_button)
        ready = self.scan_complete and bool(self.model.entries) and not self._is_busy
        for control in (self.duplicates_button, self.similar_button, self.classify_button):
            control.setEnabled(ready)
        self.merge_action.setEnabled(ready and len(self.model.checked) >= 2)
        self.keep_first_delete_action.setEnabled(ready and self.match_kind == "duplicate" and bool(self.groups) and not self._fast_removal_completed)
        self.keep_first_delete_button.setEnabled(self.keep_first_delete_action.isEnabled())
        has_pairs = any(len(group.paths) >= 2 for group in self.groups)
        self.compare_button.setEnabled(ready and has_pairs)
        self.merge_group_button.setEnabled(ready and has_pairs)
        if self._fast_removal_completed:
            self.keep_first_delete_button.hide()
            self.keep_first_hint.setText("첫 파일과 보호 파일만 남았습니다. 삭제한 항목은 목록에서 제외했고 작업 기록에서 복원할 수 있습니다.")
            self.retention_rule.hide()
            self.retention_button.hide()
        self.overview_action.setEnabled(ready)
        for control in (self.preview_button, self.assume_duplicates_button):
            control.setEnabled(ready and bool(self.model.checked))

    def _run_next_step(self):
        if self._is_busy:
            return
        if self._guidance_action == "results":
            self.match_filter.setCurrentIndex(0)
            self.views.setCurrentIndex(1)
            return
        if self._guidance_action == "compare":
            self.views.setCurrentIndex(1)
            if self.match_tree.currentItem() is None and self.match_tree.topLevelItemCount():
                self.match_tree.setCurrentItem(self.match_tree.topLevelItem(0))
        elif self._guidance_action == "retention":
            self.views.setCurrentIndex(1)
        actions = {"folder": self.choose_folder, "scan": self.start_scan,
                   "settings": self.show_analysis_settings, "preview": self.preview_selected,
                   "retention": self.select_by_retention, "compare": self.compare_current_group,
                   "overview": self.show_overview,
                   "rules": self.show_classification, "duplicates": self.analyze_duplicates}
        if not self._is_busy and self._guidance_action in actions:
            actions[self._guidance_action]()

    def show_feature_help(self, topic="start"):
        if self._help_dialog is not None and self._help_dialog.isVisible():
            self._help_dialog.select_topic(topic)
            self._help_dialog.raise_()
            self._help_dialog.activateWindow()
            return
        dialog = HelpCenterDialog(self, topic=topic)
        self._help_dialog = dialog
        def closed(_result):
            if self._help_dialog is dialog:
                self._help_dialog = None
            dialog.deleteLater()
        dialog.finished.connect(closed)
        dialog.show()

    def _card(self, parent_layout, title, value):
        card = QFrame()
        card.setObjectName("card")
        card.setFixedHeight(52)
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 3, 14, 3)
        box.setSpacing(0)
        title_widget = QLabel(title)
        title_widget.setObjectName("muted")
        box.addWidget(title_widget)
        value_widget = QLabel(value)
        value_widget.setObjectName("cardValue")
        box.addWidget(value_widget)
        parent_layout.addWidget(card, 1)
        return value_widget

    def set_busy(self, busy: bool, *, cancellable: bool = False):
        self._is_busy = busy
        for control in self._work_controls:
            control.setEnabled(not busy)
        self.progress_bar.setVisible(busy)
        self.progress_time_label.setVisible(busy)
        if busy:
            self.progress_timer.start()
        else:
            self.progress_timer.stop()
        self.cancel_button.setVisible(busy and cancellable)
        self.cancel_button.setEnabled(True)
        if not busy:
            self.select_duplicates_button.setEnabled(self.match_kind == "duplicate" and bool(self.groups) and self.scan_complete)
            self.keep_first_delete_button.setEnabled(self.match_kind == "duplicate" and bool(self.groups) and self.scan_complete)
            self.retention_button.setEnabled(self.match_kind == "duplicate" and bool(self.groups) and self.scan_complete)
            self.retention_rule.setEnabled(self.match_kind == "duplicate" and bool(self.groups) and self.scan_complete)
            self.compare_button.setEnabled(bool(self.groups) and self.scan_complete)
        self._refresh_guidance()

    def _start_task(self, label: str, function: Callable[[Worker], object], success: Callable,
                    *, cancellable: bool = False, failure: Callable[[str], None] | None = None):
        if self._worker is not None:
            return
        self._task_label = label
        self._task_id = uuid.uuid4().hex
        self._task_started = time.monotonic()
        self._progress_tracker.reset()
        self._progress_snapshot = None
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("진행 중")
        self.progress_time_label.setText("경과 0초")
        log_event("task.started", label, operation_id=self._task_id)
        self._success = success
        self._failure = failure
        self._cancellable = cancellable
        self.status_label.setText(label)
        self.set_busy(True, cancellable=cancellable)
        worker = Worker(function, self)
        self._worker = worker
        worker.progress.connect(self.status_label.setText)
        worker.progress_detail.connect(self._on_progress_detail)
        worker.finished.connect(self._on_worker_finished)
        worker.start()

    @Slot()
    def _on_worker_finished(self):
        worker = self._worker
        if worker is None:
            return
        callback = self._success
        failure = self._failure
        error = worker.error
        result = worker.result
        cancelled = worker.cancel_event.is_set()
        self._worker = None
        self._success = None
        self._failure = None
        worker.deleteLater()
        self.set_busy(False)
        elapsed = max(0.0, time.monotonic() - self._task_started)
        log_event("task.cancelled" if cancelled else "task.failed" if error else "task.completed",
                  error or self._task_label, level="ERROR" if error and not cancelled else "INFO",
                  operation_id=self._task_id, details={"elapsed_seconds": round(elapsed, 2)})
        if self._close_requested:
            if error:
                QMessageBox.warning(self, "종료 전 작업 결과", error)
            if self._update_worker is None:
                QTimer.singleShot(0, self.close)
            return
        if error:
            if cancelled:
                self.status_label.setText("작업을 중단했습니다. 파일을 변경하지 않았습니다.")
                self._resume_deferred_update()
                return
            self.status_label.setText(f"{self._task_label} 실패")
            if failure:
                failure(error)
            else:
                QMessageBox.warning(self, "작업을 완료하지 못했습니다", error)
            self._resume_deferred_update()
            return
        try:
            if callback:
                callback(result)
            if cancelled:
                self.status_label.setText("분석을 중단했습니다. 부분 결과는 정리에 사용할 수 없습니다. 다시 분석하세요.")
        except Exception as exc:
            log_event("task.display_failed", f"{type(exc).__name__}: {exc}", level="ERROR", operation_id=self._task_id)
            QMessageBox.warning(self, "결과 표시 오류", f"{type(exc).__name__}: {exc}")
        self._resume_deferred_update()

    @Slot(object)
    def _on_progress_detail(self, event):
        if self._worker is None or not isinstance(event, dict):
            return
        try:
            previous = self._progress_snapshot
            snapshot = self._progress_tracker.update(event)
            self._progress_snapshot = snapshot
            if snapshot.fraction is None:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, 1000)
                self.progress_bar.setValue(round(snapshot.fraction * 1000))
            units = {"files": "파일", "bytes": "바이트", "pairs": "비교 쌍", "items": "항목", "groups": "그룹"}
            unit = units.get(snapshot.unit, snapshot.unit)
            count = f"{snapshot.completed:,} / {snapshot.total:,} {unit}" if snapshot.total is not None else f"{snapshot.completed:,} {unit} 확인"
            percent = f" · {round(snapshot.fraction * 100):d}%" if snapshot.fraction is not None else ""
            phase_key = snapshot.phase.rsplit("_", 1)[-1]
            phase_names = {"scan": "폴더 탐색", "index": "파일 크기 분류", "sample": "빠른 내용 비교", "hash": "전체 내용 검증", "verify": "중복 확인", "pairs": "유사 후보 비교", "inventory": "파일 준비", "rules": "분류 규칙 확인", "preview": "작업 계획 검증", "execute": "파일 처리", "undo": "파일 복원", "history": "기록 읽기", "complete": "검사 완료", "cancelled": "검사 중단", "limit": "비교 한도 도달", "preflight": "변경 전 재검증", "copy": "사본 검증", "retention": "보존 파일 확인"}
            self.progress_bar.setFormat(phase_names.get(phase_key, "파일 처리") + " · " + count + percent)
            if snapshot.message:
                self.status_label.setText(snapshot.message)
            if previous is None or previous.phase != snapshot.phase:
                log_event("task.phase", snapshot.message or snapshot.phase, operation_id=self._task_id,
                          details={"phase": snapshot.phase, "total": snapshot.total, "unit": snapshot.unit})
            self._refresh_progress_time()
        except (TypeError, ValueError, AttributeError):
            # A progress display must never abort a verified file operation.
            log_event("progress.invalid", "진행도 정보를 표시하지 못했습니다", level="WARNING", operation_id=self._task_id)

    @Slot()
    def _refresh_progress_time(self):
        if self._worker is None:
            return
        elapsed = max(0, int(time.monotonic() - self._task_started))
        elapsed_text = f"{elapsed // 60}분 {elapsed % 60}초" if elapsed >= 60 else f"{elapsed}초"
        text = "경과 " + elapsed_text
        snapshot = self._progress_snapshot
        if snapshot is not None and snapshot.total is None:
            unit = {"files": "파일", "bytes": "바이트", "pairs": "비교 쌍", "items": "항목", "groups": "그룹"}.get(snapshot.unit, snapshot.unit)
            phase = "폴더 탐색" if snapshot.phase == "scan" else "처리 중"
            text = f"{phase} · {snapshot.completed:,} {unit} 확인 · " + text
        if snapshot is not None and snapshot.eta_seconds is not None and snapshot.fraction not in (None, 1):
            remaining = max(1, round(snapshot.eta_seconds))
            text += " · 현재 단계 약 " + (f"{remaining // 60}분" if remaining >= 60 else f"{remaining}초") + " 남음"
        self.progress_time_label.setText(text)

    def cancel_task(self):
        if self._worker is not None and self._cancellable:
            self._worker.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("진행 중인 파일 처리가 끝나면 분석을 중단합니다…")

    def closeEvent(self, event):
        file_busy = self._merge_execute_pending or (self._worker is not None and self._worker.isRunning())
        update_busy = self._update_worker is not None and self._update_worker.isRunning()
        merge_preview = self._merge_dialog._worker if self._merge_dialog is not None else None
        if merge_preview is not None:
            self._close_requested = True
            self._merge_dialog.setEnabled(False)
            if not getattr(self, "_merge_close_waiting", False):
                self._merge_close_waiting = True
                merge_preview.finished.connect(self.close, Qt.ConnectionType.QueuedConnection)
            self.status_label.setText("병합 미리보기를 안전하게 마친 뒤 종료합니다. 원본 파일은 변경하지 않습니다.")
            event.ignore()
            return
        if file_busy or update_busy:
            self._close_requested = True
            if self._worker is not None and self._cancellable:
                self._worker.cancel_event.set()
            if update_busy:
                self._update_worker.cancel_event.set()
            self.set_busy(True)
            self.status_label.setText("진행 중인 작업을 안전하게 마친 뒤 자동으로 종료합니다. 업데이트 다운로드는 중단합니다.")
            event.ignore()
            return
        if self._pending_update is not None:
            try:
                updater.start_update_helper(self._pending_update)
                log_event("update.apply_requested", "앱 종료 후 업데이트 적용 요청")
            except Exception as exc:
                log_event("update.apply_failed", str(exc), level="ERROR")
                self._close_requested = False
                self.set_busy(False)
                self.update_status.setText("업데이트 적용 시작 실패")
                QMessageBox.warning(self, "업데이트를 시작하지 못했습니다",
                                    f"현재 버전과 준비한 파일은 유지됩니다. 다시 종료하면 재시도합니다.\n"
                                    f"업데이트 없이 종료하려면 자동 업데이트 체크를 해제하세요.\n\n{exc}")
                event.ignore()
                return
            self._pending_update = None
        for dialog in (self._help_dialog, self._history_dialog, self._log_dialog, self._execution_log_dialog, self._merge_dialog, self._overview_dialog):
            if dialog is not None:
                dialog.close()
        event.accept()

    def _initial_update_check(self):
        # A bounded local diagnostic; never use any returned path for mutations.
        try:
            result = updater.last_update_result()
        except Exception:
            result = None
        if isinstance(result, dict):
            status = str(result.get("status", ""))
            version = str(result.get("version", ""))
            details = str(result.get("details", ""))[:2000]
            fingerprint = hashlib.sha256(repr((status, version, details)).encode("utf-8")).hexdigest()
            if self.settings.value("updates/lastDiagnostic", "") != fingerprint:
                self.settings.setValue("updates/lastDiagnostic", fingerprint)
                if status == "failed":
                    self._update_diagnostic_notice = "지난 업데이트를 적용하지 못했습니다. 현재 버전을 유지합니다. " + details
                    self.warning_label.setText(self._update_diagnostic_notice)
                    self.warning_label.show()
                    self.update_status.setText("이전 업데이트 실패 · 현재 버전 유지")
                    self.update_status.setToolTip(details)
                elif status == "updated":
                    self.update_status.setText(f"v{version} 업데이트 완료")
        self.request_update_check()

    def _auto_update_changed(self, enabled: bool):
        self.settings.setValue("updates/enabled", enabled)
        if not enabled:
            self._pending_update = None
            self._update_retry_requested = False
            self._deferred_update_manual = False
            if self._update_worker is not None:
                self._update_worker.cancel_event.set()
            self.update_status.setText("자동 업데이트 꺼짐")
        else:
            self.update_status.setText("자동 업데이트 켜짐")
            self.request_update_check()

    def manual_update_check(self):
        self.request_update_check(manual=True)

    def request_update_check(self, manual: bool = False):
        if self._close_requested:
            return
        if not manual and not self.auto_update_check.isChecked():
            return
        if not updater.is_auto_update_supported():
            self.update_status.setText("소스 실행 · 자동 적용 제외")
            if manual:
                QMessageBox.information(self, "업데이트 안내",
                    "자동 업데이트는 Windows 배포 파일로 실행할 때 사용할 수 있습니다.\n"
                    "소스 실행 버전은 GitHub Releases에서 새 버전을 받아주세요.\n"
                    "https://github.com/P-JSL/Folder_Organizer/releases")
            return
        if self._pending_update is not None:
            self.update_status.setText("새 버전 준비 완료 · 종료 후 적용")
            if manual:
                QMessageBox.information(self, "업데이트 준비 완료", "앱의 파일 작업을 마친 후 종료하면 새 버전을 적용합니다.")
            return
        if self._update_worker is not None:
            return
        if self._worker is not None or QApplication.activeModalWidget() is not None:
            self._update_retry_requested = True
            self._deferred_update_manual |= manual
            self.update_status.setText("현재 작업 완료 후 업데이트 확인")
            if self._worker is None:
                self.deferred_update_timer.start(2000)
            return
        self.update_status.setText("새 릴리스 확인 중…")
        log_event("update.check_started", "새 정식 버전 확인")
        def task(worker):
            release = updater.check_for_update(__version__, cancel=worker.cancel_event)
            if release is None:
                return None
            worker.report("새 버전 다운로드 · 검증 중…")
            log_event("update.download_started", "새 버전 다운로드와 검증 시작", details={"version": release.get("version")})
            plan = updater.prepare_update(release, updater.install_directory(), cancel=worker.cancel_event)
            return release, plan
        self._active_update_manual = manual
        self.update_button.setEnabled(False)
        worker = Worker(task, self)
        self._update_worker = worker
        worker.progress.connect(self.update_status.setText)
        worker.finished.connect(self._on_update_worker_finished)
        worker.start()

    @Slot()
    def _on_update_worker_finished(self):
        worker = self._update_worker
        if worker is None:
            return
        result, error, cancelled = worker.result, worker.error, worker.cancel_event.is_set()
        manual = self._active_update_manual
        self._update_worker = None
        worker.deleteLater()
        self.update_button.setEnabled(True)
        if cancelled:
            log_event("update.cancelled", "업데이트 중단 · 현재 버전 유지")
            self.update_status.setText("업데이트 중단 · 현재 버전 유지")
        elif error:
            self._update_failed(error, manual)
        else:
            self._update_ready(result, manual)
        if self._close_requested and self._worker is None:
            QTimer.singleShot(0, self.close)

    def _update_ready(self, result, manual: bool):
        if result is None:
            log_event("update.current", "현재 버전이 최신 버전입니다")
            self.update_status.setText("최신 버전입니다")
            if self._worker is None:
                self.status_label.setText("업데이트 확인 완료 · 최신 버전입니다.")
            if manual:
                QMessageBox.information(self, "업데이트 확인", "현재 사용 중인 버전이 최신 버전입니다.")
            return
        release, plan = result
        if not manual and not self.auto_update_check.isChecked():
            self.update_status.setText("자동 업데이트 꺼짐 · 현재 버전 유지")
            return
        self._pending_update = plan
        log_event("update.ready", "새 버전 검증 완료 · 종료 후 적용", details={"version": release.get("version")})
        self.update_status.setText(f"v{release.get('version', '새 버전')} 준비 완료\n종료 후 적용합니다")
        if self._worker is None:
            self.status_label.setText("새 버전 다운로드와 검증을 마쳤습니다. 파일 작업을 마치고 종료하면 적용합니다.")
        if manual:
            QMessageBox.information(self, "업데이트 준비 완료", "새 버전을 준비했습니다. 앱 종료 후 적용하며 현재 파일 작업은 그대로 이어갈 수 있습니다.")

    def _update_failed(self, error: str, manual: bool):
        log_event("update.failed", error, level="ERROR")
        self.update_status.setText("업데이트 확인 실패 · 현재 버전 유지")
        if self._worker is None:
            self.status_label.setText("업데이트 서버에 연결하지 못했거나 패키지 검증에 실패했습니다. 기존 버전으로 계속 사용할 수 있습니다.")
        self.update_status.setToolTip(error)
        if manual:
            QMessageBox.warning(self, "업데이트 실패", error)

    def _resume_deferred_update(self):
        if self._update_retry_requested and self._worker is None and not self._close_requested:
            if QApplication.activeModalWidget() is not None:
                self.deferred_update_timer.start(2000)
                return
            manual = self._deferred_update_manual
            self._update_retry_requested = False
            self._deferred_update_manual = False
            self.request_update_check(manual=manual)

    def _persist_analysis_options(self):
        self.settings.setValue("analysis/preferences", json.dumps(self.analysis_options, ensure_ascii=False))

    def _name_threshold_changed(self, value):
        self.analysis_options["name_threshold"] = value
        self._persist_analysis_options()
        self._refresh_scope_label()
        if self.match_kind == "similar":
            self._clear_match_results()
            self.status_label.setText("이름 기준을 변경했습니다. 새 기준으로 유사 검사를 다시 실행하세요.")

    def _inventory_option_changed(self):
        self._analysis_cache.clear()
        self.settings.setValue("analysis/recursive", self.recursive_check.isChecked())
        self.settings.setValue("analysis/hidden", self.hidden_check.isChecked())
        if self.scan_result is not None:
            self.scan_complete = False
            self._clear_match_results()
            self.status_label.setText("검사 범위를 변경했습니다. 다시 분석한 뒤 비교·정리하세요.")

    def _refresh_scope_label(self):
        if not hasattr(self, "scope_label"):
            return
        options = self.analysis_options
        methods = {"name": "이름", "text": "텍스트", "image": "이미지"}
        scores = " · ".join(f"{methods[key]} {options[key + '_threshold']:.2f}" for key in options["methods"])
        size = f"{format_size(options['min_size'])} 이상"
        if options["max_size"] is not None:
            size += f" ~ {format_size(options['max_size'])}"
        size_note = " · 크기 조건 있음" if options["min_size"] or options["max_size"] is not None else ""
        self.scope_label.setText(f"검사 설정: {PROFILE_LABELS[options['profile']]} · 제외 조건 {len(options['exclude_patterns'])}개{size_note}")
        self.scope_label.setToolTip(f"유사 비교 기준: {scores}\n크기 범위: {size}\n검사 범위는 전체 검사에 적용됩니다. 목록 검색은 화면 표시만 바꿉니다.\n" + "\n".join(options["exclude_patterns"]))

    def show_analysis_settings(self):
        dialog = AnalysisSettingsDialog(self.analysis_options, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        previous = self.analysis_options
        self.analysis_options = dialog.values
        self._persist_analysis_options()
        self.similarity_threshold.blockSignals(True)
        self.similarity_threshold.setValue(self.analysis_options["name_threshold"])
        self.similarity_threshold.blockSignals(False)
        self.retention_rule.blockSignals(True)
        self.retention_rule.setCurrentIndex(self.retention_rule.findData(self.analysis_options["retention_rule"]))
        self.retention_rule.blockSignals(False)
        self._refresh_scope_label()
        scope_changed = any(previous[key] != self.analysis_options[key] for key in ("exclude_patterns", "min_size", "max_size"))
        self._clear_match_results()
        if scope_changed and self.root:
            self._analysis_cache.clear()
            self.start_scan()
        else:
            self.status_label.setText("검사 설정을 저장했습니다. 변경한 기준으로 중복 또는 유사 검사를 실행하세요.")

    def _clear_match_results(self):
        self._fast_removal_completed = False
        self.groups = []
        self.match_kind = None
        self.match_report = {}
        self.last_retention = None
        self.model.set_checked([])
        self.reclaim_label.setText("—")
        self._clear_match_tree()
        self.views.setCurrentIndex(0)
        self.views.setTabVisible(1, False)
        self.select_duplicates_button.setEnabled(False)
        self.keep_first_delete_button.setEnabled(False)
        self.retention_button.setEnabled(False)
        self.retention_rule.setEnabled(False)
        self.compare_button.setEnabled(False)

    def show_comparison_guide(self):
        self.show_feature_help("duplicates")

    def choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, "분석할 폴더 선택", self.root or str(Path.home()))
        if path:
            self._open_folder(path)

    def _open_folder(self, path):
        if self._is_busy or self._close_requested:
            return
        self._analysis_cache.clear()
        self._remember_next_scan = True
        self.root = str(Path(path).expanduser().absolute())
        self.folder_input.setText(self.root)
        self.model.set_entries([])
        self.scan_result = None
        self.scan_complete = False
        self.groups = []
        self.match_kind = None
        self._clear_match_tree()
        self.views.setCurrentIndex(0)
        self.views.setTabVisible(1, False)
        self.start_scan()

    def start_scan(self):
        if not self.root:
            self.choose_folder()
            return
        root = self.root
        recursive = self.recursive_check.isChecked()
        hidden = self.hidden_check.isChecked()
        self.scan_complete = False
        options = dict(self.analysis_options)
        self._start_task("폴더를 분석하고 있습니다…", lambda worker: scan_folder(
            root, recursive=recursive, include_hidden=hidden,
            cancel=worker.cancel_event, progress=worker.report, progress_detail=worker.report_detail,
            exclude_patterns=tuple(options["exclude_patterns"]), min_size=options["min_size"],
            max_size=options["max_size"], sort_results=False), self.load_scan_result, cancellable=True)

    def load_scan_result(self, result: ScanResult):
        """Load a completed (or explicitly partial) scan; useful for integration tests."""
        previous_root = self.root
        self._fast_removal_completed = False
        self.scan_result = result
        self.root = result.root
        self.folder_input.setText(result.root)
        self.scan_complete = not result.cancelled
        if self.scan_complete and getattr(self, "_remember_next_scan", False):
            self._remember_next_scan = False
            self._remember_folder(result.root)
        if previous_root is not None and previous_root != result.root:
            self._analysis_cache.clear()
        self.model.set_entries(result.files)
        self.table.setVisible(bool(result.files))
        self.empty_state.setVisible(not result.files)
        if not result.files:
            self.empty_state.setText("표시할 파일이 없습니다\n\n검사 설정의 제외 패턴·크기 조건을 확인하거나 다른 폴더를 선택하세요.")
        self.groups = []
        self.match_kind = None
        self.match_report = {}
        self.last_retention = None
        self._clear_match_tree()
        self.views.setCurrentIndex(0)
        self.views.setTabVisible(1, False)
        self.views.setTabText(1, "비교 결과")
        self.count_label.setText(f"{len(result.files):,}개")
        metrics = getattr(result, "scan_metrics", {})
        self.count_label.setToolTip(f"폴더 탐색 {metrics.get('elapsed_seconds', 0):.3f}초 · 파일 내용은 읽지 않고 목록을 조회합니다. 표시 순서 정렬과 내용 검사는 별도 단계입니다.")
        self.size_label.setText(format_size(result.total_size))
        self.reclaim_label.setText("—")
        self.select_duplicates_button.setEnabled(False)
        self.keep_first_delete_button.setEnabled(False)
        notices = [self._update_diagnostic_notice] if self._update_diagnostic_notice else []
        if result.cancelled:
            notices.append("중단된 분석입니다. 정리하려면 분석을 완료하세요.")
        if result.warnings:
            notices.append(f"읽을 수 없거나 제외된 항목 {len(result.warnings):,}건: " + " / ".join(result.warnings[:2]))
        self.warning_label.setText("\n".join(notices))
        self.warning_label.setToolTip("\n".join(result.warnings))
        self.warning_label.setVisible(bool(notices))
        filtered = getattr(result, "filtered_files", 0)
        folders = getattr(result, "excluded_directories", 0)
        self.status_label.setText(f"분석 완료 · 파일 {len(result.files):,}개 · {format_size(result.total_size)} · 조건으로 건너뛴 파일 {filtered:,}개 / 폴더 {folders:,}곳. 목록 검색은 검사 범위를 바꾸지 않습니다.")
        self._filter_changed()

    def _filter_changed(self):
        if not hasattr(self, "proxy"):
            return
        self.views.setCurrentIndex(0)
        self.proxy.beginFilterChange()
        self.proxy.query = self.search_input.text().strip().casefold()
        self.proxy.kind = self.type_filter.currentData() or "all"
        self.proxy.quick = self.quick_filter.currentData() or "all"
        self.proxy.age_cutoff = time.time() - 365 * 24 * 60 * 60
        self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        self.visible_label.setText(f"표시 {self.proxy.rowCount():,} / 전체 {len(self.model.entries):,}개")
        self._refresh_action_hint()

    def selected_paths(self) -> list[str]:
        return sorted(self.model.checked, key=self.model.path_rows.__getitem__)

    def select_visible(self):
        self.model.set_checked([self.model.entries[self.proxy.mapToSource(self.proxy.index(row, 0)).row()].path
                                for row in range(self.proxy.rowCount())], replace=False)

    def clear_selection(self):
        self.model.set_checked([])

    def _selection_changed(self):
        if not hasattr(self, "selected_label"):
            return
        paths = self.model.checked
        self.selected_label.setText(f"{len(paths):,}개")
        self.selected_label.setToolTip(f"선택 용량 {format_size(sum(self.model.entries[self.model.path_rows[path]].size for path in paths))}. 검색으로 숨긴 항목도 선택 상태를 유지합니다.")
        if hasattr(self, "match_tree"):
            self.match_tree.blockSignals(True)
            for path in paths.symmetric_difference(self._last_tree_checked):
                for child in self._match_items.get(path, ()):
                    child.setCheckState(0, Qt.CheckState.Checked if path in paths else Qt.CheckState.Unchecked)
            self._last_tree_checked = set(paths)
            self.match_tree.blockSignals(False)
        self._refresh_action_hint()

    def _require_scan(self, selection=False) -> bool:
        if not self.scan_result or not self.scan_complete:
            QMessageBox.information(self, "폴더 분석 필요", "폴더를 선택하고 분석을 완료한 뒤 사용하세요.")
            return False
        if selection and not self.selected_paths():
            QMessageBox.information(self, "파일 선택 필요", "파일 목록의 체크 상자로 처리할 파일을 선택하세요.")
            return False
        return True

    def analyze_duplicates(self):
        if not self._require_scan():
            return
        files = list(self.model.entries)
        def task(worker):
            report = {}
            groups = find_duplicates(files, cancel=worker.cancel_event, progress=worker.report, progress_detail=worker.report_detail, report=report)
            return groups, worker.cancel_event.is_set(), report
        self._start_task("SHA-256과 실제 내용을 비교하고 있습니다…", task,
                         lambda result: self._load_groups(result[0], "duplicate", result[2]) if not result[1] else None,
                         cancellable=True)

    def analyze_similar(self):
        if not self._require_scan():
            return
        files = list(self.model.entries)
        options = dict(self.analysis_options)
        def task(worker):
            report = {}
            groups = find_similar(files, cancel=worker.cancel_event, progress=worker.report, progress_detail=worker.report_detail, cache=self._analysis_cache,
                                  methods=tuple(options["methods"]),
                                  name_threshold=options["name_threshold"], text_threshold=options["text_threshold"],
                                  image_threshold=options["image_threshold"], report=report,
                                  search_mode=options["search_mode"], max_candidates_per_file=options["max_candidates_per_file"])
            return groups, worker.cancel_event.is_set(), report
        self._start_task("선택한 이름 · 텍스트 · 이미지 기준으로 검토 후보를 찾고 있습니다…", task,
                         lambda result: self._load_groups(result[0], "similar", result[2]) if not result[1] else None,
                         cancellable=True)

    def _load_groups(self, groups: list[MatchGroup], kind: str, report: dict | None = None):
        self._fast_removal_completed = False
        self.keep_first_hint.setText("클릭하면 전체 중복 그룹의 첫 파일·보호 파일을 남기고 나머지를 delete_files로 이동합니다. 추가 전체 검사 없이 실행 · 복원 가능")
        self.groups = groups
        self.match_kind = kind
        self.keep_first_delete_button.setEnabled(kind == "duplicate" and bool(groups) and self.scan_complete)
        self.match_report = dict(report or {})
        self.last_retention = None
        self.model.set_checked([])
        self.match_filter.blockSignals(True)
        self.match_filter.setCurrentIndex(0)
        self.match_filter.blockSignals(False)
        self._render_match_tree()
        self.views.setTabVisible(1, True)
        self.views.setTabText(1, f"비교 결과 · {len(groups):,}그룹")
        self.views.setCurrentIndex(1)
        exact = kind == "duplicate"
        title = "내용이 완전히 같은 파일" if exact else "직접 확인할 유사 후보 · 완전 중복 제외"
        self.match_label.setText(f"{title} · {len(groups):,}그룹")
        self.select_duplicates_button.setEnabled(exact and bool(groups) and self.scan_complete)
        self.retention_button.setEnabled(exact and bool(groups) and self.scan_complete)
        self.retention_rule.setEnabled(exact and bool(groups) and self.scan_complete)
        self.compare_button.setEnabled(bool(groups) and self.scan_complete)
        if exact:
            sizes = {entry.path: entry.size for entry in self.model.entries}
            reclaim = sum(sum(sizes.get(path, 0) for path in group.paths[1:]) for group in groups)
            self.reclaim_label.setText(format_size(reclaim))
            self.match_explanation.setText("보존 기준으로 남길 파일을 정하세요. ‘나머지 삭제’는 다음 화면에서 확인 후 안전 보관하며 작업 기록에서 복원할 수 있습니다.")
            self.status_label.setText(f"정확한 중복 {len(groups):,}그룹 확인 · 안전 보관과 보호 사본은 디스크 공간을 즉시 반환하지 않습니다.")
        else:
            compared = self.match_report.get("compared_pairs", 0)
            excluded = self.match_report.get("exact_pairs_excluded", 0)
            self.match_explanation.setText(f"이름·텍스트·이미지는 서로 다른 기준이며 점수는 동일할 확률이 아닙니다. 직접 비교 {compared:,}쌍 · 정확한 중복 {excluded:,}쌍 제외. 유사 후보는 자동 선택하지 않습니다.")
            self.reclaim_label.setText("—")
            self.status_label.setText(f"유사 검토 후보 {len(groups):,}그룹 · 두 파일을 나란히 보고 내용과 보존 여부를 직접 결정하세요.")
        notices = [self._update_diagnostic_notice] if self._update_diagnostic_notice else []
        if self.scan_result and self.scan_result.warnings:
            notices.append(f"폴더 분석 주의 {len(self.scan_result.warnings):,}건")
        if exact and not self.match_report.get("complete", True):
            notices.append(f"중복 검사 일부 미완료 · 건너뛴 파일 {self.match_report.get('metrics', {}).get('skipped_files', 0):,}개. 확인된 그룹만 표시합니다. 전체 폴더에 중복이 없다는 뜻은 아닙니다.")
        if self.match_report.get("effective_search_mode") == "indexed":
            notices.append("빠른 후보 검색 결과입니다. 모든 파일을 살펴보되 모든 쌍을 비교하지 않아 일부 유사 파일을 놓칠 수 있습니다. 검사 설정에서 전체 쌍 비교를 선택할 수 있습니다.")
        if self.match_report.get("limit_reached"):
            notices.append(f"유사 비교 한도 {self.match_report.get('max_pairs', 25000):,}쌍에 도달했습니다. 결과는 일부 범위이며 폴더·크기를 좁혀 다시 검사하세요.")
        if self.match_report.get("skipped_count"):
            notices.append(f"파일 변경·읽기 제한 등으로 건너뛴 비교 {self.match_report['skipped_count']:,}건. 결과 보고서에서 확인하세요.")
        self.warning_label.setText("\n".join(notices))
        self.warning_label.setToolTip("\n".join(
            f"{value.get('path')}: {value.get('message')}" if isinstance(value, dict) else str(value)
            for value in self.match_report.get("warnings", [])))
        self.warning_label.setVisible(bool(notices))
        self.match_filter.setVisible(not exact)
        self.retention_rule.setVisible(exact)
        self.retention_button.setVisible(exact)
        self.keep_first_delete_button.setVisible(exact)
        self.keep_first_hint.setVisible(exact)
        self._refresh_guidance()

    def _render_match_tree(self, *_args):
        if not hasattr(self, "match_tree"):
            return
        filter_kind = self.match_filter.currentData() or "all"
        self.match_tree.blockSignals(True)
        self._clear_match_tree()
        displayed = 0
        for index, group in enumerate(self.groups):
            evidence = getattr(group, "evidence", {}) or {}
            signals = set(evidence.get("matched_signals", []))
            if not signals:
                signals = {"name" if group.kind == "similar_name" else "text" if group.kind == "similar_text" else "image" if group.kind == "similar_image" else "exact" if group.kind == "duplicate" else "unknown"}
            if filter_kind == "name_only" and signals != {"name"}:
                continue
            if filter_kind == "content" and not signals.intersection({"text", "image", "exact"}):
                continue
            if filter_kind in {"text", "image"} and filter_kind not in signals:
                continue
            displayed += 1
            label = f"그룹 {index + 1} · {len(group.paths)}개"
            reason = {
                "duplicate": "내용이 완전히 같음 · 이름·날짜가 달라도 중복",
                "similar_name": "이름만 유사 · 내용이 다를 수 있음",
                "similar_text": "텍스트 어구 유사 · 문서 버전 직접 검토",
                "similar_image": "이미지 구조·색·투명도 유사 · 품질·의미 보장 안 함",
            }.get(group.kind, group.reason)
            parent = QTreeWidgetItem((label, reason, evidence_scores(group)))
            parent.setData(0, Qt.ItemDataRole.UserRole + 1, index)
            limitation = "\n".join(map(str, evidence.get("limitations", [])))
            parent.setToolTip(1, reason + "\n" + group.reason + ("\n" + limitation if limitation else ""))
            parent.setToolTip(2, "각 기준의 점수는 같은 파일일 확률이 아닙니다. 두 파일 비교에서 바이트 동일 여부를 별도로 확인하세요.")
            self.match_tree.addTopLevelItem(parent)
            for position, path in enumerate(group.paths):
                relative = str(Path(path).relative_to(self.root)) if self.root and Path(path).is_relative_to(self.root) else path
                item = QTreeWidgetItem((relative, "첫 파일 · 기본 보존 후보" if group.kind == "duplicate" and position == 0 else "내용 확인 후 체크", ""))
                item.setToolTip(0, path)
                item.setData(0, Qt.ItemDataRole.UserRole, path)
                item.setData(0, Qt.ItemDataRole.UserRole + 1, index)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if path in self.model.checked else Qt.CheckState.Unchecked)
                parent.addChild(item)
                self._match_items.setdefault(path, []).append(item)
            parent.setExpanded(displayed <= 6)
        self._last_tree_checked = set(self.model.checked)
        self.match_tree.blockSignals(False)
        self.match_tree.setToolTip(f"표시 {displayed:,} / 전체 {len(self.groups):,}그룹. 근거 필터는 표시만 바꾸며 보존 규칙은 전체 중복 그룹에 적용됩니다.")
        self._refresh_guidance()

    def _match_selection_changed(self, item, column):
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if not path:
            return
        checked = set(self.model.checked)
        if item.checkState(0) == Qt.CheckState.Checked:
            checked.add(path)
        else:
            checked.discard(path)
        self.model.set_checked(checked)

    def select_duplicate_candidates(self):
        self._choose_by_retention("first")

    def _retention_rule_changed(self):
        self.analysis_options["retention_rule"] = self.retention_rule.currentData() or "first"
        self._persist_analysis_options()

    def select_by_retention(self):
        self._choose_by_retention(self.retention_rule.currentData() or "first")

    def _choose_by_retention(self, rule: str):
        if self.match_kind != "duplicate" or not self._require_scan():
            return
        groups = list(self.groups)
        files = list(self.model.entries)
        options = dict(self.analysis_options)
        self._start_task("정확한 중복과 보호 폴더를 다시 확인하고 있습니다…", lambda worker: choose_duplicate_copies(
            groups, files, rule=rule, preferred_folder=options["preferred_folder"] or None,
            protected_folders=list(options["protected_folders"]), cancel=worker.cancel_event,
            progress=worker.report), self._retention_done, cancellable=True)

    def _retention_done(self, result: dict):
        self.last_retention = result
        self.model.set_checked(result.get("selected", []))
        kept = len(result.get("kept", []))
        selected = len(self.selected_paths())
        self.status_label.setText(f"보존 규칙 검토 완료 · {kept:,}개 보존 · {selected:,}개 선택. 파일은 아직 변경하지 않았습니다. 작업 미리보기에서 전체 경로를 확인하세요.")
        warnings = result.get("warnings", [])
        if warnings:
            self.warning_label.setText(f"보존 규칙 주의 {len(warnings):,}건: " + " / ".join(map(str, warnings[:3])))
            self.warning_label.setToolTip("\n".join(map(str, warnings)))
            self.warning_label.show()

    def match_report_payload(self) -> dict:
        """Metadata/evidence only; raw file contents and rendered previews stay local."""
        if self.scan_result is None:
            raise ValueError("폴더를 먼저 분석하세요.")
        return build_report(self.scan_result,
                            duplicates=self.groups if self.match_kind == "duplicate" else None,
                            similar=self.groups if self.match_kind == "similar" else None,
                            comparison_report=self.match_report,
                            duplicate_analysis=self.match_report if self.match_kind == "duplicate" else None,
                            selection={"paths": self.selected_paths(), "retention": self.last_retention})

    def export_match_report(self):
        if not self._require_scan():
            return
        path, selected_filter = QFileDialog.getSaveFileName(self, "새 분석 결과 보고서 저장", "folder-organizer-report.html", "HTML 보고서 (*.html);;JSON 보고서 (*.json)")
        if not path:
            return
        if not Path(path).suffix:
            path += ".json" if selected_filter.startswith("JSON") else ".html"
        payload = self.match_report_payload()
        def task(_worker):
            write_report(path, payload)
            return path
        self._start_task("파일 내용 없이 비교 근거와 범위를 저장하고 있습니다…", task,
                         lambda saved: self.status_label.setText(f"비교 보고서 저장 완료 · {saved}"))

    def compare_current_group(self):
        if not self._require_scan():
            return
        item = self.match_tree.currentItem()
        group_index = item.data(0, Qt.ItemDataRole.UserRole + 1) if item else None
        if group_index is None:
            if len(self.groups) == 1:
                group_index = 0
            else:
                QMessageBox.information(self, "비교 그룹 선택", "비교 결과에서 그룹이나 파일을 선택하세요.")
                return
        group = self.groups[group_index]
        if len(group.paths) < 2:
            return
        left, right = group.paths[:2]
        if len(group.paths) > 2:
            picker = QDialog(self)
            picker.setWindowTitle("비교할 두 파일 선택")
            box = QVBoxLayout(picker)
            first, second = QComboBox(), QComboBox()
            for path in group.paths:
                first.addItem(str(Path(path).relative_to(self.root)) if self.root and Path(path).is_relative_to(self.root) else path, path)
                second.addItem(first.itemText(first.count() - 1), path)
            second.setCurrentIndex(1)
            box.addWidget(QLabel("같은 그룹 안에서 직접 검토할 두 파일을 선택하세요."))
            box.addWidget(first)
            box.addWidget(second)
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(picker.accept)
            buttons.rejected.connect(picker.reject)
            box.addWidget(buttons)
            if picker.exec() != QDialog.DialogCode.Accepted:
                return
            left, right = first.currentData(), second.currentData()
            if left == right:
                QMessageBox.information(self, "다른 파일 선택", "서로 다른 두 파일을 선택하세요.")
                return
        self.compare_paths(left, right)

    def compare_paths(self, left: str, right: str):
        if left == right:
            return
        entries = {entry.path: entry for entry in self.model.entries}
        if left not in entries or right not in entries:
            QMessageBox.information(self, "다시 분석 필요", "비교할 파일이 현재 분석 결과에 없습니다. 폴더를 다시 분석하세요.")
            return
        self._start_task("두 파일의 바이트·메타데이터·지원 미리보기를 읽고 있습니다…",
                         lambda worker: compare_files(entries[left], entries[right], cancel=worker.cancel_event),
                         self._show_pair_comparison, cancellable=True)

    def _show_pair_comparison(self, result: dict):
        dialog = QDialog(self)
        dialog.setWindowTitle("두 파일 나란히 비교 · 검토 후 결정")
        dialog.resize(1040, 690)
        outer = QVBoxLayout(dialog)
        self.last_pair_result = result
        verdict = QLabel(result.get("conclusion") or ("실제 바이트가 같습니다." if result.get("byte_identical") else "실제 바이트가 다릅니다. 유사 점수와 별도로 검토하세요."))
        verdict.setWordWrap(True)
        verdict.setObjectName("section")
        outer.addWidget(verdict)
        scores = []
        for key, label in (("name_score", "이름"), ("text_score", "텍스트"), ("image_score", "이미지")):
            value = result.get(key)
            scores.append(f"{label} {value:.2f}" if isinstance(value, (int, float)) else f"{label} 비교 미지원/제한")
        score_label = QLabel(" · ".join(scores) + "\n각 점수는 동일할 확률이 아닙니다. 원본의 가치·품질·프로그램 버전을 판단하지 않습니다.")
        score_label.setWordWrap(True)
        outer.addWidget(score_label)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        metadata = QWidget()
        columns = QHBoxLayout(metadata)
        for key, title in (("left", "왼쪽 파일"), ("right", "오른쪽 파일")):
            column = QVBoxLayout()
            meta = result.get(key, {})
            text = QPlainTextEdit()
            text.setReadOnly(True)
            lines = [title, str(meta.get("name", "")), str(meta.get("path", "")), "",
                     f"용량: {format_size(meta.get('size', 0))}", f"수정일: {format_date(meta.get('modified'))}",
                     f"생성일: {format_date(meta.get('created'))}", "", "SHA-256", str(meta.get("sha256", "확인 불가"))]
            image_meta = result.get("image_metadata", {}).get(key)
            if image_meta:
                lines.extend(("", "이미지 정보",
                              f"해상도: {image_meta.get('width', '?')} × {image_meta.get('height', '?')} px",
                              f"형식: {image_meta.get('format') or '확인 불가'}",
                              f"색상 모드: {image_meta.get('mode') or '확인 불가'}",
                              f"투명도: {'있음' if image_meta.get('has_alpha') else '없음'}",
                              f"다중 프레임: {'예' if image_meta.get('animated') else '아니요'}",
                              str(image_meta.get("frame_note", ""))))
            text.setPlainText("\n".join(lines))
            column.addWidget(text)
            columns.addLayout(column, 1)
        tabs.addTab(metadata, "파일 정보·전체 해시")
        if result.get("left_preview") is not None or result.get("right_preview") is not None:
            images = QWidget()
            row = QHBoxLayout(images)
            for key in ("left", "right"):
                column = QVBoxLayout()
                label = QLabel(result.get(key, {}).get("name", key))
                label.setWordWrap(True)
                column.addWidget(label)
                preview = QLabel("지원되는 이미지 미리보기가 없습니다.")
                preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
                preview.setMinimumSize(100, 100)
                data = result.get(key + "_preview")
                if data:
                    image = QImage.fromData(data, "PNG")
                    if not image.isNull():
                        preview = ImagePreviewLabel(image)
                column.addWidget(preview, 1)
                row.addLayout(column, 1)
            tabs.addTab(images, "이미지 나란히 보기")
        if result.get("left_text") is not None or result.get("right_text") is not None:
            text_pair = QWidget()
            row = QHBoxLayout(text_pair)
            for key in ("left", "right"):
                text = QPlainTextEdit()
                text.setReadOnly(True)
                text.setPlainText(result.get(key + "_text") or "텍스트 미리보기를 지원하지 않습니다.")
                row.addWidget(text, 1)
            tabs.addTab(text_pair, "텍스트 나란히 보기")
        if result.get("text_diff"):
            difference = QPlainTextEdit()
            difference.setReadOnly(True)
            difference.setPlainText("\n".join(result["text_diff"]) + ("\n[차이 미리보기 일부 생략]" if result.get("text_diff_truncated") else ""))
            tabs.addTab(difference, "텍스트 차이")
        limitations = result.get("limitations", [])
        if limitations:
            note = QLabel("\n".join(map(str, limitations)))
            note.setWordWrap(True)
            note.setObjectName("warning")
            note.setMaximumHeight(100)
            note.setToolTip(note.text())
            outer.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        outer.addWidget(buttons)
        dialog.exec()

    def _classification_rules(self):
        try:
            saved = self.settings.value("classification/rules", "")
            if isinstance(saved, str) and saved and len(saved) <= 128 * 1024:
                return validate_rules(json.loads(saved))
        except (ValueError, TypeError):
            log_event("rules.settings_invalid", "저장된 분류 규칙을 읽지 못해 기본 규칙을 사용합니다", level="WARNING")
        return default_rules("type")

    def show_classification(self, *, preset=None):
        if not self._require_scan():
            return
        dialog = ClassificationDialog(default_rules(preset) if preset else self._classification_rules(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values
        self.settings.setValue("classification/rules", json.dumps(values["rules"], ensure_ascii=False))
        scan = self.scan_result
        protected = tuple(self.analysis_options["protected_folders"])
        self._start_task(
            "규칙에 맞는 파일과 안전한 대상 경로를 검증하고 있습니다…",
            lambda worker: build_classification_plan(
                scan, values["rules"], destination=values.get("destination"), action=values["action"],
                protected_folders=protected, progress_detail=worker.report_detail),
            lambda plan: self._show_preview(plan, require_confirmation=True))

    def keep_first_delete(self):
        if self._fast_removal_completed or self._is_busy:
            return
        if not self._require_scan():
            return
        if self.match_kind != "duplicate" or not self.groups:
            QMessageBox.information(self, "완전 중복 검사 필요", "중복 검사를 완료한 뒤 완전 중복 결과에서 사용하세요. 유사 후보에는 적용할 수 없습니다.")
            return
        scan = self.scan_result
        groups = list(self.groups)
        protected = list(self.analysis_options["protected_folders"])
        def task(worker):
            from .rules import build_existing_duplicate_plan
            plan = build_existing_duplicate_plan(scan, groups, protected_folders=protected,
                                                 progress_detail=worker.report_detail)
            if not plan["items"]:
                return {"status": "unchanged", "items": [], "warnings": plan.get("warnings", [])}
            return execute_plan(plan, progress_detail=worker.report_detail)
        self._start_task(
            "현재 중복 결과에서 첫 파일을 남기고 나머지를 안전 보관하고 있습니다…",
            task, self._keep_first_done, failure=self._keep_first_failed)

    def _keep_first_failed(self, error):
        self.scan_complete = False
        self.warning_label.setText("검사 이후 파일 상태가 달라졌거나 현재 결과를 사용할 수 없어 정리를 중단했습니다. 파일을 다시 분석해 주세요.")
        self.warning_label.setToolTip(str(error))
        self.warning_label.show()
        self._refresh_guidance()

    def _keep_first_done(self, result):
        removed = {item["source"] for item in result.get("items", []) if item.get("state") == "completed" or item.get("source_removed") is True}
        kept_groups = [MatchGroup(group.kind, [path for path in group.paths if path not in removed],
                                  group.reason, group.score, group.evidence)
                       for group in self.groups]
        previous_report = dict(self.match_report)
        if self.scan_result is not None:
            # Use the completed operation receipt instead of traversing the
            # folder or running the duplicate detector again.
            self.scan_result.files = [entry for entry in self.scan_result.files if entry.path not in removed]
            self.load_scan_result(self.scan_result)
            self._load_groups([group for group in kept_groups if group.paths], "duplicate", previous_report)
        self._analysis_cache.clear()
        self._fast_removal_completed = True
        success = result.get("status") in {"completed", "unchanged"}
        if not success:
            self.scan_complete = False
            self.warning_label.setText("일부 파일은 변경·충돌 등의 이유로 처리하지 않았습니다. 실행 로그와 작업 기록을 확인하세요. 다음 정리는 다시 분석한 뒤 진행할 수 있습니다.")
            self.warning_label.show()
        self.reclaim_label.setText("—")
        self.match_label.setText(f"정리 결과 · {len(removed):,}개 delete_files로 이동 · 첫 파일·보호 파일 유지")
        self.match_explanation.setText("현재 검사 결과로 실행했으며 전체 재검사를 하지 않았습니다. 삭제 파일은 delete_files에 보관하고 작업 기록에서 복원할 수 있습니다.")
        self.status_label.setText(f"중복 정리 {'완료' if success else '일부 처리'} · {len(removed):,}개 delete_files로 이동 · 전체 재검사 없이 결과를 갱신했습니다.")
        self.retention_rule.setEnabled(False)
        self.retention_button.setEnabled(False)
        self._refresh_guidance()

    def classify_duplicates(self):
        if not self._require_scan():
            return
        scan = self.scan_result
        options = dict(self.analysis_options)

        def task(worker):
            groups = find_duplicates(list(scan.files), cancel=worker.cancel_event,
                                     progress=worker.report, progress_detail=worker.report_detail)
            if worker.cancel_event.is_set():
                raise ValueError("중복 자동 분류를 중단했습니다.")
            plan = build_duplicate_plan(
                scan, groups, retention_rule=options["retention_rule"],
                preferred_folder=options["preferred_folder"] or None,
                protected_folders=tuple(options["protected_folders"]),
                progress_detail=worker.report_detail)
            if worker.cancel_event.is_set():
                raise ValueError("중복 자동 분류를 중단했습니다.")
            return plan

        self._start_task("완전 중복을 확인하고 원본 보존 계획을 만들고 있습니다…", task,
                         lambda plan: self._show_preview(plan, require_confirmation=True), cancellable=True)

    def preview_selected(self):
        if self._require_scan(selection=True):
            mode, basis = self.action_combo.currentData()
            self._build_preview(mode, date_basis=basis)

    def assume_duplicates(self):
        if not self._require_scan(selection=True):
            return
        answer = QMessageBox.warning(
            self, "중복으로 간주하고 안전 보관",
            f"선택한 {len(self.selected_paths()):,}개 파일은 중복 여부를 검증하지 않습니다.\n"
            "선택한 파일 모두가 원래 위치에서 안전 보관 폴더로 이동합니다.\n"
            "남길 원본이 선택되지 않았는지 확인하세요. 다음 화면에서 경로를 검토합니다.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Ok:
            self._build_preview("quarantine", assumed=True)

    def _build_preview(self, mode: str, *, date_basis="modified", assumed=False):
        paths = self.selected_paths()
        root = self.root
        destination = None
        if mode in ("copy", "move"):
            destination = QFileDialog.getExistingDirectory(self, "복사할 폴더 선택" if mode == "copy" else "이동할 폴더 선택", root or "")
            if not destination:
                return
        self._start_task("변경 계획을 검증하고 있습니다…", lambda worker: build_plan(
            root, paths, mode, destination=destination, date_basis=date_basis, progress_detail=worker.report_detail),
            lambda plan: self._show_preview(plan, assumed=assumed))

    def _show_preview(self, plan: dict, *, assumed=False, require_confirmation=False, first_only=False):
        items = plan.get("items", [])
        if not items:
            self._show_details("변경할 파일이 없습니다", plan)
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("작업 미리보기 · 확인 전에는 파일을 변경하지 않습니다")
        dialog.resize(1000, 580)
        layout = QVBoxLayout(dialog)
        text = f"총 {len(items):,}개 · {format_size(sum(item.get('size', 0) for item in items))}"
        mode = plan.get("mode")
        if mode in {"quarantine", "duplicate_auto"}:
            text += "\n실행 결과: 삭제 파일을 delete_files로 옮깁니다. 작업 기록에서 원래 위치로 복원할 수 있습니다."
        elif all(item.get("action") == "copy" for item in items):
            text += "\n실행 결과: 원본은 그대로 남기고 대상 폴더에 사본을 만듭니다."
        else:
            text += "\n실행 결과: 대상 폴더로 옮기며, 원본 보호 사본과 복원 기록을 남깁니다."
        if mode == "rules":
            text += "\n작업 범위: 분석한 전체 파일 중 규칙에 맞는 항목입니다. 목록의 체크·검색 필터와는 별개입니다."
        elif mode == "duplicate_auto":
            text += "\n작업 범위: 검사한 전체 완전 중복 그룹 중 보존 조건을 통과한 사본입니다. 목록 체크·검색·결과 필터와는 별개입니다."
        else:
            text += "\n작업 범위: 파일 목록에서 체크한 항목 중 아래 표에 표시된 파일입니다."
        hidden = self._hidden_selected_count([item.get("source") for item in items])
        if hidden:
            text += f"\n확인할 점: 현재 목록 필터로 숨겨진 파일 {hidden:,}개도 이 작업에 포함됩니다."
        if assumed:
            text += "\n사용자 지정: 중복 검증 없이 선택 항목 모두를 안전 보관합니다."
        if first_only:
            text += "\n각 완전 중복 그룹에 표시된 첫 파일과 보호 폴더의 파일을 남깁니다.\n나머지는 원래 위치에서 제거하고 안전 보관합니다. 영구 삭제가 아니며 용량은 확보되지 않습니다."
        intro = QLabel(text + "\n파일을 덮어쓰지 않으며 작업 기록에서 실행 취소할 수 있습니다.")
        intro.setTextFormat(Qt.TextFormat.PlainText)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        if require_confirmation:
            intro.setText(text + "\n1. 이 화면에서 원본·대상과 규칙을 확인합니다.\n2. 실행 시 파일을 다시 검증하고, 검증 사본과 복원 기록을 남깁니다. 덮어쓰지 않습니다.")
        if mode == "duplicate_auto":
            kept = list(dict.fromkeys(path for decision in plan.get("decisions", [])
                                      for path in decision.get("kept", [])))
            if kept:
                kept_view = QPlainTextEdit()
                kept_view.setReadOnly(True)
                kept_view.setMaximumHeight(78)
                kept_view.setPlainText(f"원래 위치에 남기는 파일 {len(kept):,}개\n" + "\n".join(kept[:200])
                                       + ("\n처음 200개만 표시합니다. 나머지도 선택한 보존 기준과 보호 폴더 설정에 따라 보존합니다." if len(kept) > 200 else ""))
                layout.addWidget(kept_view)
        if plan.get("warnings"):
            warnings = QLabel("\n".join(map(str, plan["warnings"][:8])))
            warnings.setTextFormat(Qt.TextFormat.PlainText)
            warnings.setWordWrap(True)
            warnings.setObjectName("warning")
            warnings.setMaximumHeight(78)
            warnings.setToolTip(warnings.text())
            layout.addWidget(warnings)
        table = QTableView()
        table.setModel(PlanTableModel(items, table))
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.setColumnWidth(0, 90)
        table.setColumnWidth(1, 310)
        table.setColumnWidth(2, 360)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Apply)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.button(QDialogButtonBox.StandardButton.Apply).setText("확인한 사본 안전 보관" if first_only else "확인한 작업 실행")
        confirmation = QCheckBox("원본·대상 경로와 보존 규칙을 확인했습니다")
        if require_confirmation:
            layout.addWidget(confirmation)
            buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled(False)
            confirmation.toggled.connect(buttons.button(QDialogButtonBox.StandardButton.Apply).setEnabled)
        buttons.rejected.connect(dialog.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and (not require_confirmation or confirmation.isChecked()):
            self._start_task("파일을 처리하고 작업 기록을 저장하고 있습니다…", lambda worker: execute_plan(plan, progress_detail=worker.report_detail), self._operation_done)
        else:
            self.status_label.setText("작업을 취소했습니다. 파일을 변경하지 않았습니다.")

    def _operation_done(self, result: dict):
        log_event("operation.result", "파일 작업 결과", operation_id=str(result.get("id", self._task_id)),
                  level="ERROR" if result.get("status") == "failed" else "WARNING" if result.get("status") in {"partial", "partial_undo"} else "INFO",
                  details={"status": result.get("status"), "items": len(result.get("items", []))})
        self._analysis_cache.clear()
        self.scan_complete = False
        self._show_details("파일 작업 결과", result)
        self.start_scan()

    def _reveal_index(self, index):
        if index.isValid() and index.column() != 0:
            source = self.proxy.mapToSource(index)
            self._reveal_path(self.model.entries[source.row()].path)

    def _reveal_path(self, path):
        parent = Path(path).parent
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(parent))):
            QMessageBox.information(self, "폴더 위치", str(parent))

    def reveal_selected(self):
        paths = self.selected_paths()
        if not paths and self.table.currentIndex().isValid():
            row = self.proxy.mapToSource(self.table.currentIndex()).row()
            paths = [self.model.entries[row].path]
        if paths:
            self._reveal_path(paths[0])
        elif self.root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.root))
        else:
            QMessageBox.information(self, "위치 열기", "먼저 폴더를 선택하세요.")

    def export_csv(self):
        if not self._require_scan():
            return
        path, _ = QFileDialog.getSaveFileName(self, "새 파일로 목록 저장 (덮어쓰기 없음)", str(Path.home() / "folder-organizer.csv"), "CSV 파일 (*.csv)")
        if not path:
            return
        entries = [self.model.entries[self.proxy.mapToSource(self.proxy.index(row, 0)).row()]
                   for row in range(self.proxy.rowCount())]
        def task(worker):
            def safe(value):
                value = str(value)
                return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            with open(path, "x", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("이름", "상대 경로", "종류", "바이트", "수정일", "생성일", "생성일 출처"))
                for entry in entries:
                    writer.writerow((safe(entry.name), safe(entry.relative_path), safe(entry.kind), entry.size,
                                     format_date(entry.modified), format_date(entry.created), safe(entry.created_source)))
            return len(entries)
        self._start_task("표시된 파일 목록을 저장하고 있습니다…", task,
                         lambda count: self.status_label.setText(f"CSV 저장 완료 · {count:,}개 · {path}"))

    def inspect_selected(self):
        if not self._require_scan(selection=True):
            return
        paths = self.selected_paths()
        def task(worker):
            results = []
            for path in paths:
                if worker.cancel_event.is_set():
                    break
                worker.report(f"손상 검사: {Path(path).name}")
                try:
                    results.append(inspect_file(path))
                except Exception as exc:
                    results.append({"path": path, "status": "error", "details": str(exc)})
            return {"검사 수": len(results), "선택 수": len(paths), "중단": worker.cancel_event.is_set(), "results": results}
        self._start_task("선택한 파일의 무결성을 검사하고 있습니다…", task,
                         lambda results: self._show_details("손상 검사 결과", results), cancellable=True)

    def repair_selected(self):
        if not self._require_scan(selection=True):
            return
        paths = self.selected_paths()
        destination = QFileDialog.getExistingDirectory(self, "복구본을 저장할 폴더 선택", self.root or "")
        if not destination:
            return
        answer = QMessageBox.information(
            self, "원본을 보존하고 복구본 만들기",
            "원본 파일은 유지하고 새 파일로 저장합니다.\n"
            "지원 형식은 이미지, ZIP·일부 Office 문서, PDF입니다.\n"
            "남아 있는 데이터만 복구할 수 있으며 완전 복구를 보장하지 않습니다.\n"
            "디스크에서 이미 삭제한 파일을 찾아내는 기능은 포함하지 않습니다.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Ok:
            return
        def task(worker):
            results = []
            execution_id = uuid.uuid4().hex
            record_execution("execution.started", execution_id=execution_id, action="repair", status="started",
                             message="원본을 보존하고 복구본 만들기", target=destination, completed=0, total=len(paths))
            for path in paths:
                worker.report(f"복구본 생성: {Path(path).name}")
                try:
                    result = {"path": path, **repair_file(path, destination)}
                except Exception as exc:
                    result = {"path": path, "status": "failed", "details": str(exc)}
                results.append(result)
                record_execution("execution.item", execution_id=execution_id, action="repair", status=str(result.get("status", "failed")),
                                 message="복구본 생성 결과", source=path, target=result.get("output"),
                                 error="; ".join(map(str, result.get("details", []))) if result.get("status") in {"failed", "error", "unsupported", "partial"} else None,
                                 completed=len(results), total=len(paths))
            statuses = {result.get("status") for result in results}
            status = "completed" if statuses <= {"repaired", "healthy"} else "partial" if statuses.intersection({"repaired", "partial"}) else "failed"
            record_execution("execution.finished", execution_id=execution_id, action="repair", status=status,
                             message="복구본 생성 작업 완료", target=destination, completed=len(results), total=len(paths))
            return {"저장 폴더": destination, "results": results}
        self._start_task("원본을 보존하며 복구 가능한 내용을 새 파일에 저장하고 있습니다…", task,
                         self._repair_done)

    def _repair_done(self, results):
        self._show_details("복구본 생성 결과", results)
        self.start_scan()

    def show_history(self):
        if self._history_dialog is not None and self._history_dialog.isVisible():
            self._history_dialog.raise_()
            self._history_dialog.activateWindow()
            return
        if not self.root:
            QMessageBox.information(self, "작업 기록", "기록을 확인할 원래 폴더를 선택하세요.")
            return
        root = self.root
        self._start_task("작업 기록을 읽고 있습니다…",
                         lambda worker: list_history(root, progress_detail=worker.report_detail),
                         self._show_history)

    def _show_history(self, history: list[dict]):
        # A journal contains filesystem identifiers that may exceed QVariant's
        # signed 64-bit range on Windows. Keep records in the Python model only.
        dialog = HistoryDialog(history, self)
        self._history_dialog = dialog
        root = self.root

        def request_undo(operation_id: str):
            record = next((record for record in history if record.get("id") == operation_id), None)
            answer = QMessageBox.question(
                dialog, "원래 위치로 복원", restore_effect(record) + "\n\n선택한 작업을 되돌릴까요?\n"
                "충돌하거나 변경된 파일은 덮어쓰지 않고 결과에 표시합니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
            dialog.accept()
            log_event("operation.undo_requested", "기록에서 복원 요청", operation_id=operation_id)
            # Let the dialog's event handlers unwind before starting another job.
            QTimer.singleShot(0, lambda: self._start_task(
                "파일을 원래 위치로 복원하고 있습니다…",
                lambda worker: undo_operation(root, operation_id, progress_detail=worker.report_detail),
                self._operation_done))

        def request_compact(operation_id: str):
            dialog.accept()
            def start():
                from .operations import compaction_preview
                self._start_task("복원용 보관 사본의 최적화 범위를 확인하고 있습니다…",
                                 lambda worker: compaction_preview(root, operation_id, progress_detail=worker.report_detail),
                                 self._show_compaction_preview)
            QTimer.singleShot(0, start)

        def closed(_result):
            if self._history_dialog is dialog:
                self._history_dialog = None
            dialog.deleteLater()

        dialog.undo_requested.connect(request_undo)
        dialog.compact_requested.connect(request_compact)
        dialog.finished.connect(closed)
        dialog.open()

    def _show_compaction_preview(self, plan):
        if not plan.get("candidate_count"):
            self.status_label.setText("이 작업에는 최적화할 비공개 보관 사본이 없습니다.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("복원 데이터 용량 최적화 · 실행 전 확인")
        dialog.resize(860, 580)
        layout = QVBoxLayout(dialog)
        intro = QLabel(f"비공개 보관 사본 {plan['candidate_count']:,}개 · 논리 용량 {format_size(plan['raw_bytes'])}\n"
                       f"내용이 같은 중복분 {format_size(plan['duplicate_bytes'])} · 실제 압축률은 실행 후 확인할 수 있습니다.\n\n"
                       "1. 같은 데이터 조각은 한 번만 보관하고, 압축이 유리한 조각은 무손실 압축합니다.\n"
                       "2. 전체 내용을 다시 복원해 검증하고 복원 기록을 저장한 뒤, 중복된 비공개 사본만 줄입니다.\n"
                       "3. 실제 문서와 이동 대상 폴더의 파일은 바꾸지 않습니다. 오류·변경·압축 이득 부족이면 기존 보관 방식을 유지합니다.\n\n"
                       "복원 데이터는 앱의 보관함에 남습니다. 메타데이터만으로 원문을 재생성하거나 다른 위치의 원본에 의존하지 않습니다.")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(intro)
        paths = QPlainTextEdit()
        paths.setReadOnly(True)
        paths.setMaximumHeight(110)
        candidates = plan.get("candidates", [])
        paths.setPlainText("최적화 대상 보관 경로\n" + "\n".join(str(item.get("path", "")) for item in candidates[:100])
                           + (f"\n나머지 {len(candidates) - 100:,}개 표시 생략" if len(candidates) > 100 else ""))
        layout.addWidget(paths)
        if plan.get("warnings"):
            warning = QLabel("\n".join(map(str, plan["warnings"][:8])))
            warning.setWordWrap(True)
            warning.setObjectName("warning")
            layout.addWidget(warning)
        layout.addStretch()
        confirm = QCheckBox("최적화 대상이 앱의 복원용 보관 사본임을 확인했습니다")
        layout.addWidget(confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Cancel)
        apply = buttons.button(QDialogButtonBox.StandardButton.Apply)
        apply.setText("검증 후 보관 용량 최적화")
        apply.setEnabled(False)
        confirm.toggled.connect(apply.setEnabled)
        apply.clicked.connect(dialog.accept)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted and confirm.isChecked():
            from .operations import compact_operation
            self._start_task("복원 데이터를 중복 제거·압축하고 검증하고 있습니다…",
                             lambda worker: compact_operation(plan["root"], plan["operation_id"], plan=plan,
                                                              progress_detail=worker.report_detail), self._compaction_done)

    def _compaction_done(self, result):
        summary = result.get("recovery_compaction", {})
        self._show_details("복원 데이터 최적화 결과", {
            "id": result.get("id"), "status": summary.get("status"), "recovery_compaction": summary,
            "recovery_metadata": result.get("recovery_metadata", {}),
        })
        self.status_label.setText(f"복원 데이터 최적화 · 절약량 {format_size(summary.get('saved_bytes', 0))} · 작업 기록에서 복원할 수 있습니다.")

    def show_logs(self):
        if self._log_dialog is not None and self._log_dialog.isVisible():
            self._log_dialog.refresh_logs()
            self._log_dialog.raise_()
            self._log_dialog.activateWindow()
            return
        dialog = LogViewerDialog(self)
        self._log_dialog = dialog

        def closed(_result):
            if self._log_dialog is dialog:
                self._log_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()

    def show_execution_logs(self):
        if self._execution_log_dialog is not None and self._execution_log_dialog.isVisible():
            self._execution_log_dialog.refresh_logs()
            self._execution_log_dialog.raise_()
            self._execution_log_dialog.activateWindow()
            return
        dialog = ExecutionLogDialog(self)
        self._execution_log_dialog = dialog

        def closed(_result):
            if self._execution_log_dialog is dialog:
                self._execution_log_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(closed)
        dialog.open()

    def merge_current_group(self):
        if not self._require_scan():
            return
        item = self.match_tree.currentItem()
        index = item.data(0, Qt.ItemDataRole.UserRole + 1) if item else None
        if index is None:
            if len(self.groups) == 1:
                index = 0
            else:
                QMessageBox.information(self, "병합 후보 선택", "비교 결과에서 병합할 그룹을 선택하세요. 텍스트·CSV 등 지원 형식만 병합할 수 있습니다.")
                return
        if isinstance(index, int) and 0 <= index < len(self.groups):
            self.show_merge(list(self.groups[index].paths))

    def show_merge(self, paths=None):
        if not self._require_scan():
            return
        paths = self.selected_paths() if paths is None else paths
        if len(paths) < 2:
            QMessageBox.information(self, "내용 병합", "파일 목록에서 병합할 파일을 두 개 이상 체크하세요. 또는 비교 결과의 그룹을 선택하세요.")
            return
        if self._merge_dialog is not None and self._merge_dialog.isVisible():
            self._merge_dialog.raise_()
            return
        dialog = MergeDialog(self.root, paths, self)
        self._merge_dialog = dialog

        def execute(plan):
            # Leave the dialog event stack before starting filesystem work.
            self._merge_execute_pending = True
            def start():
                self._merge_execute_pending = False
                self._start_task("겹치는 내용을 병합하고 새 파일을 검증하고 있습니다…",
                                 lambda worker: execute_merge(plan, progress_detail=worker.report_detail), self._merge_done)
            QTimer.singleShot(0, start)

        def closed(_result):
            if self._merge_dialog is dialog:
                self._merge_dialog = None
            dialog.deleteLater()

        dialog.plan_ready.connect(execute)
        dialog.finished.connect(closed)
        dialog.open()

    def _merge_done(self, result):
        self._analysis_cache.clear()
        self.scan_complete = False
        self._show_details("내용 병합 결과", result)
        self.start_scan()

    def _show_details(self, title: str, result):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(880, 570)
        layout = QVBoxLayout(dialog)
        label = QLabel("상태와 파일별 결과를 확인하세요. 실패·충돌·부분 복구 항목은 처리되지 않은 내용이 있을 수 있습니다.")
        label.setWordWrap(True)
        layout.addWidget(label)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(readable_details(result))
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        self.status_label.setText(title + " · 세부 결과 확인 완료")

    def show_help(self):
        self.show_feature_help("start")


def launch() -> int:
    configure_logging()
    configure_execution_logging()
    install_exception_hook()
    log_event("application.started", f"Folder Organizer {__version__}")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Folder Organizer")
    app.setOrganizationName("Folder Organizer")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    return app.exec()
