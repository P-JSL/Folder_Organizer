"""Human-readable, ordered classification rules; previews remain a separate step."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from types import SimpleNamespace

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import rules as classification_rules
from .rules import default_rules, validate_rules


def _size_value(text: str, *, optional: bool = False) -> int | None:
    value = text.strip()
    if not value:
        return None if optional else 0
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?", value, re.IGNORECASE)
    if not match:
        raise ValueError("용량은 10 MB, 500 KB처럼 입력하세요. 제한이 없으면 비워 두세요.")
    units = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    try:
        amount = Decimal(match.group(1)) * units[(match.group(2) or "B").upper()]
    except InvalidOperation as exc:
        raise ValueError("파일 용량을 확인하세요.") from exc
    if amount != amount.to_integral_value():
        raise ValueError("입력한 용량은 최소 1바이트 단위여야 합니다.")
    return int(amount)


def rule_example(rule: dict, *, action: str = "copy", destination: str | None = None) -> dict[str, str]:
    """Explain a rule with fictional paths; this never inspects user files.

    The sample illustrates the destination structure, not whether a fictional
    file satisfies every wildcard/date/size condition. The real preview makes
    those decisions using the scanned files.
    """
    normalized = validate_rules([rule])[0]
    suffix = normalized["extensions"][0] if normalized["extensions"] else ".jpg"
    sample_name = "예시 파일" + suffix
    context = {"type": classification_rules._type(SimpleNamespace(suffix=suffix)),
               "year": "2026", "month": "10", "source_folder": "여행",
               "ext": classification_rules.ops._extension_folder(Path(sample_name))}
    folder = normalized["target"].format(**context)
    output = (destination.strip().rstrip("/\\") if destination and destination.strip() else "선택 폴더/정리됨")
    conditions = []
    if normalized["extensions"]:
        conditions.append("확장자 " + ", ".join(normalized["extensions"]))
    if normalized["name_pattern"] != "*":
        conditions.append("이름 " + normalized["name_pattern"])
    if normalized["folder_pattern"] != "*":
        conditions.append("원래 폴더 " + normalized["folder_pattern"])
    if normalized["min_size"] or normalized["max_size"] is not None:
        conditions.append(f"용량 {normalized['min_size']:,} B 이상" +
                          (f" · {normalized['max_size']:,} B 이하" if normalized["max_size"] is not None else ""))
    if normalized["modified_after"] or normalized["modified_before"]:
        conditions.append("수정일 " + (normalized["modified_after"] or "시작 제한 없음") +
                          " ~ " + (normalized["modified_before"] or "종료 제한 없음"))
    conditional = " · ".join(conditions) if conditions else "모든 파일"
    if not normalized["enabled"]:
        conditional = "사용을 껐으므로 이 규칙은 건너뜁니다."
    path = f"여행/{sample_name}  →  {output}/{folder}/{sample_name}"
    outcome = ("이동: 원래 위치에서 파일을 옮기고 보호 사본·복원 기록을 남깁니다."
               if action == "move" else "복사: 원래 파일은 그대로 두고 대상 폴더에 새 사본을 만듭니다.")
    date_note = " 연도·월은 예시 수정일 2026-10-15 기준이며 사진 촬영일이 아닙니다." if any(
        "{" + field + "}" in normalized["target"] for field in ("year", "month")) else ""
    return {"conditions": conditional, "path": path, "outcome": outcome,
            "note": "가상의 파일로 본 경로 구조입니다. 실제 조건 일치 여부와 대상은 다음 미리보기에서 확인합니다." + date_note}


class ClassificationDialog(QDialog):
    """Edit rules without JSON or internal plan fields in the product flow."""

    def __init__(self, rules: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("자동 분류 규칙 · 미리보기 만들기")
        self.resize(1020, 680)
        self.setMinimumSize(850, 610)
        self._rules = deepcopy(rules or default_rules("type"))
        self._current_row = -1
        self._updating = False
        self._advanced_user_toggled = False
        self.rules_value: list[dict] = []
        self.action_value = "copy"
        self.destination_value: str | None = None
        self.values: dict = {}
        outer = QVBoxLayout(self)
        title = QLabel("파일을 어떤 기준으로, 어디에 모을지 정하세요")
        title.setObjectName("section")
        outer.addWidget(title)
        guide = QLabel("① 규칙 설정 → ② 실제 파일별 미리보기 → ③ 확인 후 실행\n"
                       "분석한 전체 파일 중 규칙에 맞는 파일이 대상입니다. 목록의 체크·표시 필터는 분류 범위를 줄이지 않습니다.\n"
                       "위에서부터 처음 맞는 규칙 하나를 적용합니다. 한 규칙의 조건은 모두 맞아야 하며 검사 제외·보호 폴더는 유지합니다.")
        guide.setWordWrap(True)
        guide.setObjectName("muted")
        outer.addWidget(guide)
        presets = QHBoxLayout()
        presets.addWidget(QLabel("추천 규칙"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("추천 규칙 선택", None)
        for text, key in (("종류별 · 사진/문서/…", "type"), ("수정일별 · 2026/10/…", "date"), ("기존 폴더 구조 유지", "folder")):
            self.preset_combo.addItem(text, key)
        presets.addWidget(self.preset_combo)
        self.preset_button = QPushButton("추천 규칙 불러오기")
        self.preset_button.setToolTip("현재 편집 중인 규칙을 선택한 추천 규칙으로 바꿉니다. 아직 파일은 변경하지 않습니다.")
        self.preset_button.clicked.connect(self._load_preset)
        self.preset_button.setEnabled(False)
        self.preset_combo.currentIndexChanged.connect(lambda: self.preset_button.setEnabled(self.preset_combo.currentData() is not None))
        for key in ("type", "date", "folder"):
            if self._rules == default_rules(key):
                self.preset_combo.setCurrentIndex(self.preset_combo.findData(key))
                break
        presets.addWidget(self.preset_button)
        presets.addStretch()
        outer.addLayout(presets)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("사용", "규칙 이름", "확장자 (pdf, jpg)", "이름 조건 (*.pdf)", "저장할 하위 폴더"))
        self.table.setMinimumHeight(80)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.setColumnWidth(0, 46)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 145)
        self.table.setColumnWidth(3, 170)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.currentCellChanged.connect(self._row_changed)
        self.table.itemChanged.connect(self._refresh_example)
        outer.addWidget(self.table, 1)
        row_actions = QHBoxLayout()
        for label, callback in (("규칙 추가", self._add_rule), ("선택 규칙 삭제", self._remove_rule),
                                ("위로", lambda: self._move_rule(-1)), ("아래로", lambda: self._move_rule(1))):
            button = QPushButton(label)
            button.clicked.connect(callback)
            row_actions.addWidget(button)
        row_actions.addStretch()
        outer.addLayout(row_actions)
        self.advanced_toggle = QPushButton("폴더·용량·날짜 조건과 폴더 기호 안내 (선택) ▸")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setStyleSheet("text-align: left;")
        self.advanced_toggle.clicked.connect(lambda: setattr(self, "_advanced_user_toggled", True))
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        outer.addWidget(self.advanced_toggle)
        self.advanced_panel = QWidget()
        self.advanced_panel.setStyleSheet("QLineEdit { padding: 5px 7px; }")
        advanced_layout = QVBoxLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        details = QGridLayout()
        self.folder_pattern = QLineEdit()
        self.folder_pattern.setPlaceholderText("예: 업무/청구서 · 비우면 모든 폴더")
        self.min_size = QLineEdit()
        self.min_size.setPlaceholderText("예: 10 MB · 제한 없음")
        self.max_size = QLineEdit()
        self.max_size.setPlaceholderText("예: 2 GB · 제한 없음")
        self.modified_after = QLineEdit()
        self.modified_after.setPlaceholderText("YYYY-MM-DD · 제한 없음")
        self.modified_before = QLineEdit()
        self.modified_before.setPlaceholderText("YYYY-MM-DD · 제한 없음")
        for control in (self.min_size, self.max_size):
            control.setMaxLength(64)
        for control in (self.modified_after, self.modified_before):
            control.setMaxLength(10)
        self.folder_pattern.setMaxLength(512)
        for row, label, control, column in (
            (0, "원래 폴더 조건", self.folder_pattern, 0),
            (0, "최소 용량", self.min_size, 2), (0, "최대 용량", self.max_size, 4),
            (1, "수정일 이후", self.modified_after, 0), (1, "수정일 이전", self.modified_before, 2),
        ):
            details.addWidget(QLabel(label), row, column)
            details.addWidget(control, row, column + 1)
        advanced_layout.addLayout(details)
        help_text = QLabel("폴더 기호: {type}=종류 · {ext}=확장자 · {source_folder}=원래 하위 폴더\n"
                           "{year}/{month}=파일 수정일의 연도/월 (사진 촬영일 아님). 예: 사진/{year}/{month}/{source_folder}")
        help_text.setWordWrap(True)
        help_text.setObjectName("muted")
        advanced_layout.addWidget(help_text)
        outer.addWidget(self.advanced_panel)
        self.advanced_panel.hide()
        example_frame = QFrame()
        example_frame.setObjectName("card")
        example_layout = QVBoxLayout(example_frame)
        example_layout.setContentsMargins(10, 6, 10, 6)
        example_layout.setSpacing(3)
        self.example_heading = QLabel("선택한 규칙을 쉽게 이해하기 · 가상 파일 예시")
        self.example_heading.setObjectName("section")
        self.example_conditions = QLabel()
        self.example_path = QLabel()
        self.example_note = QLabel()
        for control in (self.example_heading, self.example_conditions, self.example_path, self.example_note):
            control.setTextFormat(Qt.TextFormat.PlainText)
            control.setWordWrap(True)
            example_layout.addWidget(control)
        self.example_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.example_note.setObjectName("muted")
        outer.addWidget(example_frame)
        output = QHBoxLayout()
        self.action_combo = QComboBox()
        self.action_combo.addItem("복사 · 원본을 그대로 유지 (권장)", "copy")
        self.action_combo.addItem("이동 · 확인된 사본과 복원 기록 유지", "move")
        output.addWidget(self.action_combo)
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("기본: 선택 폴더의 ‘정리됨’ · 다른 대상 폴더도 선택 가능")
        output.addWidget(self.destination, 1)
        self.destination_button = QPushButton("대상 폴더")
        self.destination_button.clicked.connect(self._choose_destination)
        output.addWidget(self.destination_button)
        outer.addLayout(output)
        self.safety_label = QLabel("원본을 유지하는 복사가 기본입니다. 기존 파일은 덮어쓰지 않고, 작업 결과는 기록에 남습니다.")
        self.safety_label.setObjectName("muted")
        self.safety_label.setWordWrap(True)
        self.action_combo.currentIndexChanged.connect(self._action_changed)
        self.destination.textChanged.connect(self._refresh_example)
        for control in (self.folder_pattern, self.min_size, self.max_size, self.modified_after, self.modified_before):
            control.textChanged.connect(self._refresh_example)
        outer.addWidget(self.safety_label)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("다음: 파일별 분류안 확인")
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self._save)
        outer.addWidget(self.buttons)
        self._render(0)

    def _toggle_advanced(self, visible: bool):
        self.advanced_panel.setVisible(visible)
        self.advanced_toggle.setText("폴더·용량·날짜 조건과 폴더 기호 안내 (선택) " + ("▾" if visible else "▸"))

    def _refresh_example(self, *_args):
        if self._updating or not hasattr(self, "example_path"):
            return
        row = self.table.currentRow()
        if not 0 <= row < len(self._rules):
            self.example_conditions.setText("규칙을 추가하면 저장 경로의 예시를 볼 수 있습니다.")
            self.example_path.clear()
            self.example_note.clear()
            return
        rule = deepcopy(self._rules[row])
        try:
            text = lambda column: self.table.item(row, column).text().strip()
            rule.update(name=text(1), enabled=self.table.item(row, 0).checkState() == Qt.CheckState.Checked,
                        extensions=[value.strip() for value in text(2).split(",") if value.strip()],
                        name_pattern=text(3), target=text(4), folder_pattern=self.folder_pattern.text().strip(),
                        min_size=_size_value(self.min_size.text()), max_size=_size_value(self.max_size.text(), optional=True),
                        modified_after=self.modified_after.text().strip() or None,
                        modified_before=self.modified_before.text().strip() or None)
            example = rule_example(rule, action=self.action_combo.currentData(), destination=self.destination.text())
            conditions = "적용 조건: " + example["conditions"]
            self.example_conditions.setText(conditions if len(conditions) <= 220 else conditions[:217] + "…")
            path = example["path"]
            self.example_path.setText(path if len(path) <= 200 else path[:97] + "…" + path[-98:])
            self.example_conditions.setToolTip(conditions[:2048])
            self.example_path.setToolTip(path[:2048])
            self.example_note.setText(example["note"])
            self.safety_label.setText(example["outcome"] + " 일치하지 않는 파일과 보호 대상은 남기며 결과는 작업 기록에서 복원할 수 있습니다.")
        except (ValueError, TypeError, AttributeError) as exc:
            self.example_conditions.setText("입력 중인 조건을 확인하세요: " + str(exc)[:180])
            self.example_path.clear()
            self.example_note.setText("입력을 마치면 가상의 저장 경로 예시가 표시됩니다. 파일은 다음 화면에서 확인한 뒤 실행합니다.")

    def _render(self, row: int):
        self._updating = True
        self._current_row = -1
        self.table.setRowCount(len(self._rules))
        for index, rule in enumerate(self._rules):
            enabled = QTableWidgetItem()
            enabled.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
            enabled.setCheckState(Qt.CheckState.Checked if rule.get("enabled", True) else Qt.CheckState.Unchecked)
            self.table.setItem(index, 0, enabled)
            for column, value in ((1, rule.get("name", "")), (2, ", ".join(rule.get("extensions", []))),
                                  (3, rule.get("name_pattern", "")), (4, rule.get("target", ""))):
                self.table.setItem(index, column, QTableWidgetItem(str(value or "")))
        self._updating = False
        if self._rules:
            row = min(max(row, 0), len(self._rules) - 1)
            self.table.setCurrentCell(row, 1)
            self._current_row = row
            self._load_details(row)
        else:
            self._load_details(-1)
        self._refresh_example()

    def _read_table(self):
        for index, rule in enumerate(self._rules):
            text = lambda column: self.table.item(index, column).text().strip()
            rule.update(name=text(1), enabled=self.table.item(index, 0).checkState() == Qt.CheckState.Checked,
                        extensions=[value.strip() for value in text(2).split(",") if value.strip()],
                        name_pattern=text(3), target=text(4))

    def _store_details(self):
        if 0 <= self._current_row < len(self._rules):
            rule = self._rules[self._current_row]
            rule.update(folder_pattern=self.folder_pattern.text().strip(),
                        min_size=_size_value(self.min_size.text()),
                        max_size=_size_value(self.max_size.text(), optional=True),
                        modified_after=self.modified_after.text().strip() or None,
                        modified_before=self.modified_before.text().strip() or None)

    def _load_details(self, row: int):
        rule = self._rules[row] if 0 <= row < len(self._rules) else {}
        controls = (self.folder_pattern, self.min_size, self.max_size, self.modified_after, self.modified_before)
        for control in controls:
            control.setEnabled(row >= 0)
        self.folder_pattern.setText(str(rule.get("folder_pattern", "") or ""))
        self.min_size.setText(str(rule.get("min_size", 0)) + " B" if rule.get("min_size") else "")
        self.max_size.setText(str(rule["max_size"]) + " B" if rule.get("max_size") is not None else "")
        self.modified_after.setText(str(rule.get("modified_after", "") or ""))
        self.modified_before.setText(str(rule.get("modified_before", "") or ""))
        if not self._advanced_user_toggled:
            has_filters = bool(rule.get("folder_pattern") not in (None, "", "*") or rule.get("min_size") or
                               rule.get("max_size") is not None or rule.get("modified_after") or rule.get("modified_before"))
            self.advanced_toggle.setChecked(has_filters)
        self._refresh_example()

    def _row_changed(self, current_row: int, _column: int, _previous_row: int, _previous_column: int):
        if self._updating or current_row == self._current_row:
            return
        try:
            self._store_details()
        except ValueError as exc:
            QMessageBox.warning(self, "규칙 용량 확인", str(exc))
            self._updating = True
            self.table.setCurrentCell(self._current_row, 1)
            self._updating = False
            return
        self._current_row = current_row
        self._load_details(current_row)

    def _collect(self):
        self._read_table()
        self._store_details()

    def _load_preset(self):
        if self.preset_combo.currentData() is None:
            return
        self._rules = deepcopy(default_rules(self.preset_combo.currentData()))
        self._render(0)

    def _add_rule(self):
        try:
            self._collect()
        except ValueError as exc:
            QMessageBox.warning(self, "규칙 확인", str(exc))
            return
        self._rules.append({"name": "새 규칙", "enabled": True, "extensions": [],
                            "name_pattern": "", "folder_pattern": "", "target": "기타/{source_folder}",
                            "min_size": 0, "max_size": None, "modified_after": None, "modified_before": None})
        self._render(len(self._rules) - 1)

    def _remove_rule(self):
        row = self.table.currentRow()
        if row < 0:
            return
        try:
            self._collect()
        except ValueError as exc:
            QMessageBox.warning(self, "규칙 확인", str(exc))
            return
        self._rules.pop(row)
        self._render(row)

    def _move_rule(self, delta: int):
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not 0 <= target < len(self._rules):
            return
        try:
            self._collect()
        except ValueError as exc:
            QMessageBox.warning(self, "규칙 확인", str(exc))
            return
        self._rules[row], self._rules[target] = self._rules[target], self._rules[row]
        self._render(target)

    def _choose_destination(self):
        path = QFileDialog.getExistingDirectory(self, "분류한 파일을 저장할 폴더", self.destination.text())
        if path:
            self.destination.setText(path)

    def _action_changed(self):
        moving = self.action_combo.currentData() == "move"
        self.safety_label.setText("이동은 원래 위치를 바꿉니다. 미리보기와 최종 확인 후 실행하며, 보호 사본과 복원 기록을 남깁니다."
                                  if moving else "원본을 유지하는 복사가 기본입니다. 기존 파일은 덮어쓰지 않고, 작업 결과는 기록에 남습니다.")
        self._refresh_example()

    def _save(self):
        self.values = {}
        try:
            self._collect()
            self.rules_value = validate_rules(self._rules)
            self.action_value = self.action_combo.currentData()
            self.destination_value = self.destination.text().strip() or None
            self.values = {"rules": deepcopy(self.rules_value), "action": self.action_value,
                           "destination": self.destination_value}
        except (ValueError, TypeError) as exc:
            QMessageBox.warning(self, "분류 규칙을 확인하세요", str(exc))
            return
        self.accept()
