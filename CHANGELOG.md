# Changelog

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
