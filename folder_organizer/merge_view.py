"""Preview-first merge configuration; execution belongs to the main worker."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
)

from .merging import build_merge_preview


class _PreviewWorker(QThread):
    ready = Signal(object)
    failed = Signal(str)
    progress = Signal(object)

    def __init__(self, root, paths, options, parent=None):
        super().__init__(parent)
        self.root, self.paths, self.options = root, paths, options

    def run(self):
        try:
            self.ready.emit(build_merge_preview(self.root, self.paths,
                                                progress_detail=self.progress.emit, **self.options))
        except Exception as exc:
            self.failed.emit(str(exc))


class MergeDialog(QDialog):
    """Keep signed plans in Python, never in QVariant-backed model roles."""

    plan_ready = Signal(object)

    def __init__(self, root, paths, parent=None):
        super().__init__(parent)
        self.setWindowTitle("내용 병합")
        self.resize(900, 700)
        self.root, self.paths = str(root), list(dict.fromkeys(map(str, paths)))
        self.plan = None
        self._worker = None
        layout = QVBoxLayout(self)
        title = QLabel("겹치는 내용은 한 번만, 다른 내용은 모두 유지")
        title.setObjectName("section")
        title.setWordWrap(True)
        layout.addWidget(title)
        intro = QLabel("제목이 같거나 비슷한 파일을 직접 선택해 병합하세요. TXT·Markdown은 완전히 같은 문단, "
                       "CSV·TSV는 같은 열 구성의 완전히 같은 행만 정리합니다. 의미가 비슷한 문장이나 서로 다른 값은 삭제하지 않습니다.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.sources = QPlainTextEdit("\n".join(f"{index}. {path}" for index, path in enumerate(self.paths, 1)))
        self.sources.setReadOnly(True)
        self.sources.setMaximumHeight(90)
        self.sources.setAccessibleName("병합할 원본 파일, 표시 순서대로 내용 유지")
        layout.addWidget(self.sources)
        form = QFormLayout()
        self.destination = QLineEdit(self.root)
        self.destination.setReadOnly(True)
        self.destination.setAccessibleName("병합본 저장 폴더")
        self.browse_button = QPushButton("폴더 선택…")
        self.browse_button.clicked.connect(self._choose_destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination, 1)
        destination_row.addWidget(self.browse_button)
        form.addRow("병합본 저장 폴더", destination_row)
        first = Path(self.paths[0]) if self.paths else Path("문서.txt")
        self.output_name = QLineEdit(f"{first.stem[:140]}_병합{first.suffix.casefold()}")
        self.output_name.setAccessibleName("새 병합본 이름")
        form.addRow("새 파일 이름", self.output_name)
        layout.addLayout(form)
        self.move_originals = QCheckBox("병합 후 원본을 안전 보관으로 이동")
        self.move_originals.setChecked(False)
        layout.addWidget(self.move_originals)
        note = QLabel("기본: 원본을 그대로 두고 새 병합본만 만듭니다. 이동 선택 시 병합본 검증 후 원본을 안전 보관합니다. "
                      "작업 기록에서 원본을 복원할 수 있으며 새 병합본은 남습니다. 파일당 8 MiB · 합계 32 MiB · 2~20개 · "
                      "문단/행 합계 200,000개 · 표 1,000열 / 데이터 셀 합계 1,000,000개까지.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        layout.addWidget(note)
        self.preview_button = QPushButton("1. 병합 내용 미리보기")
        self.preview_button.clicked.connect(self._preview)
        layout.addWidget(self.preview_button)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.summary = QLabel("미리보기는 파일을 만들거나 이동하지 않습니다.")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.summary)
        self.content = QPlainTextEdit()
        self.content.setReadOnly(True)
        self.content.setPlaceholderText("병합본의 내용이 여기에 표시됩니다.")
        self.content.setAccessibleName("새 병합본 내용 미리보기")
        layout.addWidget(self.content, 1)
        self.confirm = QCheckBox("새 병합본 내용과 원본 처리 방식을 확인했습니다.")
        self.confirm.setEnabled(False)
        self.confirm.toggled.connect(self._update_execute)
        layout.addWidget(self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        self.close_button.setText("닫기")
        buttons.rejected.connect(self.reject)
        self.execute_button = buttons.addButton("2. 새 병합본만 만들기", QDialogButtonBox.ButtonRole.ActionRole)
        self.execute_button.setObjectName("primary")
        self.execute_button.setEnabled(False)
        self.execute_button.clicked.connect(self._submit)
        layout.addWidget(buttons)
        self.output_name.textChanged.connect(self._invalidate)
        self.move_originals.toggled.connect(self._invalidate)

    def _invalidate(self, *_):
        self.plan = None
        self.confirm.setChecked(False)
        self.confirm.setEnabled(False)
        self.execute_button.setEnabled(False)
        self.execute_button.setText("2. 병합 후 원본 안전 보관" if self.move_originals.isChecked() else "2. 새 병합본만 만들기")
        self.summary.setText("설정을 바꾸었습니다. 미리보기를 다시 확인하세요.")

    def _choose_destination(self):
        directory = QFileDialog.getExistingDirectory(self, "병합본 저장 폴더 선택", self.destination.text())
        if directory:
            self.destination.setText(directory)
            self._invalidate()

    def _set_busy(self, busy):
        for control in (self.output_name, self.browse_button, self.move_originals, self.preview_button, self.close_button):
            control.setEnabled(not busy)
        self.progress.setVisible(busy)
        self.confirm.setEnabled(not busy and self.plan is not None)
        self._update_execute()

    def _preview(self):
        if self._worker is not None:
            return
        self.plan = None
        self.confirm.setChecked(False)
        self.content.clear()
        self.summary.setText("원본을 읽고 겹치는 문단·행을 확인하고 있습니다…")
        options = {"destination": self.destination.text(), "output_name": self.output_name.text(),
                   "move_originals": self.move_originals.isChecked()}
        worker = _PreviewWorker(self.root, self.paths, options, self)
        self._worker = worker
        worker.ready.connect(self._preview_ready)
        worker.failed.connect(self._preview_failed)
        worker.progress.connect(self._show_progress)
        worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        worker.start()

    def _show_progress(self, detail):
        self.progress.setRange(0, max(1, detail.get("total", 1)))
        self.progress.setValue(detail.get("completed", 0))

    def _preview_ready(self, plan):
        self.plan = plan
        effect = "검증 후 원본을 안전 보관으로 이동" if plan["move_originals"] else "원본 위치 유지"
        self.summary.setText(f"{plan['source_count']}개 파일 → {plan['retained_units']:,}개 {plan['unit']} 유지 · "
                             f"완전히 겹치는 {plan['removed_units']:,}개 {plan['unit']} 정리\n"
                             f"새 파일: {plan['output']} ({plan['output_size']:,} 바이트)\n{effect} · UTF-8 / LF 행 구분")
        preview = plan["content"][:128_000]
        if len(plan["content"]) > len(preview):
            preview += "\n\n[화면에는 처음 128,000자만 표시합니다. 저장 파일에는 전체 내용이 포함됩니다.]"
        self.content.setPlainText(preview)

    def _preview_failed(self, message):
        self.plan = None
        self.summary.setText(f"미리보기를 만들지 못했습니다: {message}\n파일은 만들거나 이동하지 않았습니다.")

    def _worker_finished(self):
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()
        self._set_busy(False)

    def _update_execute(self, *_):
        self.execute_button.setEnabled(self._worker is None and self.plan is not None and self.confirm.isChecked())

    def _submit(self):
        if self.plan is None or not self.confirm.isChecked() or self._worker is not None:
            return
        self.plan_ready.emit(self.plan)
        self.accept()

    def reject(self):
        if self._worker is None:
            super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            event.ignore()
        else:
            super().closeEvent(event)
