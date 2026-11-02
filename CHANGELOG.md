# Changelog

## 미배포 — 단일·다중 폴더 검사·정리 통합

- v0.9.0 원본 ZIP·체크섬·배포 기록을 `DOWNLOADS/v0.9.0`에 보관하고 최신 공개 릴리즈를 v0.7.0으로 롤백했습니다. 이전 태그와 앱 버전 표식은 바꾸지 않습니다.
- 단일·다중 폴더를 같은 작업 공간에서 분석·검사·정리하도록 연결하고 리본 도구 모음, 검색 폴더·필터, 결과, 파일 정보, 종류별 요약으로 화면을 구성합니다.
- 실제 폴더별 작업 계획·잠금·복원 기록을 사용하며 내보낸 읽기 전용 보고서에 파일 변경 권한을 부여하지 않습니다.
- 기존 검사는 모두 스킵하고 새 변경 사례만 확인합니다. 화면 캡처와 새 공개 릴리즈는 진행하지 않습니다.

## 0.9.0 — 2026-11-02 · 메인 화면의 복수 폴더 작업 공간

- v0.8.0 원래 Windows 배포 ZIP·체크섬·증거를 `DOWNLOADS/v0.8.0`에 보관하고, 새 버전 준비 전에 최신 릴리즈를 v0.7.0으로 되돌렸습니다. v0.8.0 소스와 이전 태그는 유지합니다.
- Python·Rust의 메인 화면에 체크 가능한 폴더 목록, 통합 파일·완전 중복 결과, 선택한 파일의 스냅샷 정보를 배치합니다. 별도 창을 먼저 열 필요 없이 폴더를 추가하고 검사를 시작·취소합니다.
- 통합 분석은 읽기 전용을 유지합니다. 실제 폴더 하나를 명시적으로 선택해 기존 단일 폴더 정리로 이동하며 통합 그룹에 파일 변경 권한을 부여하지 않습니다.
- 목록은 가상 행을 사용하고 파일 정보는 분석 메타데이터에서 표시합니다. 화면을 그릴 때 파일 내용을 읽지 않습니다.
- 이전 검사·CLI 계약·분석 엔진·복원 검사를 모두 스킵합니다. 새 메인 화면 연결과 새 UI 사례만 확인하고 두 판을 정식 Windows 배포 파일로 각각 빌드합니다.

## 0.8.0 — 2026-11-02 · 여러 폴더 통합 분석

- Python·Rust에 여러 폴더 선택·읽기 전용 통합 파일/완전 중복 분석과 별도 JSON 계약을 추가했습니다. 중복·중첩 루트, 하드링크, 접근 실패·취소와 조회 범위를 보존하며 기존 단일 폴더 정리·복원 API와 분리합니다.
- Python은 한 분석의 인증된 다른 해시를 재사용해 유사 후보의 불필요한 바이트 재읽기를 줄이고 최대 16,384개로 제한합니다. 파일 스냅샷 검증과 실제 중복의 SHA-256·바이트 증명은 유지합니다.
- Rust는 Unicode 이름 특징의 반복 계산과 미지원 형식의 텍스트 캐시 점유를 줄이고, 단일·다중 폴더에 같은 검증 코어와 GUI 작업 실행기를 재사용합니다.
- 새 분석 창의 취소·닫기·다시 열기와 부분 결과를 보완하고, 향후 Windows 패키지 확인의 JSON·PNG·명령·해시를 실패 후에도 보존합니다.
- 보고서의 확인된 사실·설계 제안·정확도 해석 오류를 문서화했습니다. 기존 공개 v0.7.0 태그·배포 파일은 그대로 유지합니다.

## 0.7.0 — 2026-10-29 · Report-driven review

- Add Rust's large-file sample prefilter while retaining full SHA-256, byte proof and snapshot checks; record actual sample/hash/comparison reads and explicit partial-file failures.
- Strengthen bounded Rust similarity candidates with actual text/image features and expose individual methods, thresholds and pair limits through the CLI.
- Add opt-in `folder-organizer.common.v1` JSON and shared CSV columns; preserve native report formats, exact integer dates, error visibility and process-local action authority.
- Reuse authenticated Python duplicate results during combined CLI analyses instead of repeating the full duplicate pass; retain complete/partial diagnostics in reports and the GUI.
- Fix cancelled-copy undo touching a later external hardlink and support only the precisely identified historical Rust build receipt in newer updater validation.
- Trim Python distribution data through imported Qt Widgets hooks and keep runtime plugins, software rendering, codecs and legal notices. Measure the final packages rather than inferring runtime speed from ZIP size.
- Run only new checks for changed paths. Existing passed checks and untouched functionality are skipped; Windows synthetic measurements do not establish physical NAS or cold-cache performance.

## 0.6.1 — 2026-10-29 · Python / Rust

- Add a proprietary authorized-use-only application license, detailed terms, local-data/privacy notice and permission-request guidance. Preserve third-party licenses and existing lawful grants; include legal notices in both packages without changing the already released executables.
- Release both Windows editions as 0.6.1 so existing 0.6.0 installations can recognize a newer update. Preserve the previously passed Python implementation and Rust identity, metadata-preservation and classification fixes.
- Change version and release metadata only. Skip previously passed tests and application execution checks as requested; build the version-stamped release packages without new feature changes.

## 0.6.0 — 2026-10-29 · Python / Rust

- Reissue only the Rust Windows package at the same version: handle unsupported extended file-ID queries through verified same-handle legacy identities, refuse lossy Windows ADS/EFS transfers, and continue to later rules when a creation date is unavailable. Preserve the original release tag and Python asset; embed exact hotfix source/build receipts.
- Add a separate native Rust desktop, console interface and independent update helper covering inventory, exact/similar review, recoverable operations, rules, merges, history, compact recovery data, health checks and reports.
- Separate edition package identifiers, update assets and recovery journals; exclude both editions' metadata from inventories.
- Reduce Python inventory path/pattern work, per-record memory and serialization overhead; optimize metadata summaries and bounded name-comparison/progress caches without weakening exact duplicate verification.
- Consolidate duplicated date/extension controls into the shared rule editor and add an extension preset; preserve existing operation and recovery compatibility.
- Document official-source 1.0 readiness research, feature decisions and measured performance scope. Reuse previous verification records; run only new or changed-path checks.

## 0.5.0 — 2026-10-28

- Reuse process-authenticated duplicate results for immediate keep-first deletion with no rescan or extra confirmation; update the displayed inventory from operation receipts.
- Move new deletions into one recoverable delete_files folder without quarantine/retained copies, skip trash folders during analysis, and retain legacy undo compatibility.
- Add explicit, verified chunk deduplication and adaptive lossless compression for recovery payloads.
- Add read-only storage summaries, largest/empty file review, quick display filters and freshly scanned recent folders.
- Stream folder enumeration without per-directory sorting, allow unsorted core inventory for GUI use and throttle detailed progress before event allocation.
- Add bounded indexed similarity candidates across the full inventory, with automatic/small exhaustive modes and explicit incomplete-coverage reporting; preserve full byte-verified duplicate checks.
- Add authenticated compressed restore metadata for suitable completed operations, retain original bytes and legacy checkpoints, and fall back to valid legacy records when compression is unsuitable or unavailable.
- Explain storage review, search limits and metadata-based restore in the interface and help; add a reproducible scaling benchmark and measured performance notes.

## 0.4.0 — 2026-10-28

- Move navigation to header dropdown menus and keep the main screen focused on the current step.
- Add saved dark and light themes, with dark as the default, across the main window and dialogs.
- Expand folder mappings with Korean and English tokens, rule names, date formats and explicit modified/created dates; keep copy as the default and warn when creation dates are unavailable.
- Add a separate searchable execution log for actual file operations while retaining activity diagnostics and signed undo history.
- Add previewed, non-overwriting UTF-8 text/Markdown paragraph and CSV/TSV row merges; preserve original files by default and offer verified output followed by recoverable original archiving.
- Make the exact-duplicate keep-first action easier to find and explain its retained files, confirmation and safe-storage behavior.
- Update feature help, user guides and reproducible screenshots for the new menus, rules, logs and merge workflow.

## 0.3.1 — 2026-10-27

- Guide the next action from the current folder, analysis, result and checkbox state.
- Add searchable read-only help with fourteen practical topics and optional technical criteria.
- Explain checkbox selection, hidden checked files, automatic classification scope and operation effects before execution.
- Give comparison results more space and keep ordinary file-list tools on their own tab.
- Show live fictional routing examples in the rule editor, with optional advanced conditions that retain their values.
- Distinguish undo from diagnostic logs and present readable activity details before optional support JSON.
- Add reproducible captures of the real GUI using disposable example files and settings.

## 0.3.0 — 2026-10-27

- Replace recursive journal-to-Qt container conversion with a flat, bounded history model; defer restore tasks outside history callbacks.
- Show per-phase file counts, determinate progress when totals are known, elapsed time and estimated remaining time where applicable.
- Add bounded rotating local logs, a searchable log viewer and explicit log export.
- Add guided, ordered classification rules and exact-duplicate quarantine plans with retained originals, protected folders and keeper revalidation.
- Add a separate keep-first removal preview that always retains the first displayed exact duplicate and all protected files; copies remain recoverable in quarantine.
- Reduce large equal-size nonduplicate reads with a sampled prefilter while preserving full SHA-256 and byte verification; cache bounded text/image signatures in memory with change checks.
- Refresh the desktop workflow and keep preview, verified copies, conflict-aware undo and exit-time updates.

## 0.2.0 — 2026-10-27

- Separate byte-verified duplicates from name/text/image similarity, preserving every enabled signal and its threshold.
- Exclude verified exact pairs from similarity and expose bounded comparison diagnostics and unknown-byte states.
- Add side-by-side metadata, full hashes, bounded text differences and image previews.
- Add exact-only retention rules, preferred/protected folders and digest revalidation before selecting copies.
- Save comparison profiles and scope preferences; filter excluded paths and file-size ranges before analysis.
- Export inspectable JSON and readable local HTML reports with evidence, scope, warnings and safe escaping.
- Document paid-product research from official Duplicate Cleaner Pro and TreeSize pages, with source and capability limits.
- Keep previewed operations, retained backups, undo, recovery and verified exit-time automatic updates in the same workflow.

## 0.1.1 — 2026-10-27

- Use stable Windows file identities for duplicate/similar matching.
- Compare recovery snapshots with consistent Windows change-time semantics.
- Validate raw ZIP filenames before platform-specific normalization.
- Include Linux headless Qt prerequisites and actionable CI diagnostics.
- Verify compiled Windows inventory and the independent updater's process wait, replacement and retained backup before publication.

## 0.1.0 — 2026-10-27

- Korean local desktop interface for folder inventory and sorting.
- Exact duplicate verification, bounded name/text/image similarity review.
- Previewed copy/move/date/extension organization, separate assumed-duplicate quarantine.
- Recoverable quarantine and operation history with conflict-aware undo.
- Non-destructive ZIP/Office, image and PDF health checks and best-effort recovery.
- Read-only CLI, CSV inventory, safety regression tests and Windows packaging workflow.
- Windows portable automatic update checks, verified staging and exit-time replacement with rollback.

Initial release. Unsupported or unrecoverable damage is reported explicitly; this release does not recover deleted disk sectors or uninstall installed applications.
