# Changelog

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
