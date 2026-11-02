//! New integration cases for action-capable single/multi-folder workspaces.
//! Existing operation and report regression targets are deliberately not run.
use folder_organizer_rust::{
    core::ScanOptions,
    multiroot::{self, WorkspaceSession},
    operations::{self, TransferKind},
    similarity::SimilarOptions,
    workspace_actions,
};
use std::{
    collections::HashSet,
    fs,
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        let mut id = [0_u8; 12];
        getrandom::getrandom(&mut id).unwrap();
        let name: String = id.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-workspace-actions-{name}"));
        fs::create_dir(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }

    fn folder(&self, relative: &str) -> PathBuf {
        let path = self.0.join(relative);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn write(&self, relative: &str, data: &[u8]) -> PathBuf {
        let path = self.0.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, data).unwrap();
        path
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn assert_bytes(path: &Path, expected: &[u8]) {
    assert_eq!(fs::read(path).unwrap(), expected);
}

fn analyze(roots: &[PathBuf]) -> WorkspaceSession {
    multiroot::analyze_workspace(
        roots,
        &ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap()
}

#[test]
fn workspace_global_first_survives_and_every_source_root_has_independent_undo() {
    let fixture = Fixture::new();
    let roots = [
        fixture.folder("A"),
        fixture.folder("B"),
        fixture.folder("C"),
    ];
    let bytes = b"verified copies spanning three actual roots\n";
    let sources = [
        fixture.write("A/first.txt", bytes),
        fixture.write("B/copy.txt", bytes),
        fixture.write("C/copy.txt", bytes),
    ];
    let session = analyze(&roots);
    let shared = session.clone();
    assert!(session.actionable());
    assert_eq!(session.groups().len(), 1);
    let keeper = session.groups()[0].paths[0].clone();
    let result =
        workspace_actions::keep_first(&session, &[], &AtomicBool::new(false), None).unwrap();
    assert!(result.completed && !result.cancelled && result.error.is_none());
    assert_eq!(
        result
            .receipts
            .iter()
            .map(|receipt| receipt.items.len())
            .sum::<usize>(),
        2
    );
    assert_bytes(&keeper, bytes);
    assert!(!session.actionable() && !shared.actionable());
    for receipt in &result.receipts {
        assert!(receipt.completed, "{:?}", receipt.errors);
        assert!(roots.contains(&receipt.root));
        assert!(operations::history(&receipt.root)
            .unwrap()
            .iter()
            .any(|record| record.id == receipt.id));
        for item in &receipt.items {
            assert_ne!(item.source, keeper);
            assert!(item.source.starts_with(&receipt.root));
            assert_eq!(
                item.target.parent(),
                Some(receipt.root.join("delete_files").as_path())
            );
            assert!(!item.source.exists());
            assert_bytes(&item.target, bytes);
        }
        let restored =
            operations::undo(&receipt.root, &receipt.id, &AtomicBool::new(false), None).unwrap();
        assert!(restored.completed, "{:?}", restored.errors);
    }
    for source in sources {
        assert_bytes(&source, bytes);
    }
    assert!(!fixture.0.join(".folder-organizer-rust").exists());
}

#[test]
fn workspace_stale_keeper_aborts_all_roots_before_first_source_is_moved() {
    let fixture = Fixture::new();
    let roots = [
        fixture.folder("A"),
        fixture.folder("B"),
        fixture.folder("C"),
    ];
    let sources = [
        fixture.write("A/original.txt", b"same across roots"),
        fixture.write("B/copy.txt", b"same across roots"),
        fixture.write("C/copy.txt", b"same across roots"),
    ];
    let session = analyze(&roots);
    let keeper = session.groups()[0].paths[0].clone();
    fs::write(&keeper, b"externally changed keeper with another size").unwrap();
    assert!(workspace_actions::keep_first(&session, &[], &AtomicBool::new(false), None).is_err());
    for source in sources {
        assert!(source.exists());
        if source != keeper {
            assert_bytes(&source, b"same across roots");
        }
    }
    for root in roots {
        assert!(!root.join("delete_files").exists());
    }
    assert!(!session.actionable());
}

#[test]
fn workspace_cross_root_copy_reserves_names_before_preview_and_undo_is_isolated() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let a = fixture.write("A/same.txt", b"source A bytes");
    let b = fixture.write("B/same.txt", b"source B other bytes");
    let destination = fixture.folder("destination");
    let existing = fixture.write("destination/same.txt", b"existing unrelated user file");
    let session = analyze(&roots);
    let plan = workspace_actions::plan_transfer(
        &session,
        &[a.clone(), b.clone()],
        Some(&destination),
        TransferKind::Copy,
    )
    .unwrap();
    let preview: HashSet<_> = plan
        .plans()
        .iter()
        .flat_map(|plan| plan.items.iter().map(|item| item.target.clone()))
        .collect();
    assert_eq!(plan.item_count(), 2);
    assert_eq!(preview.len(), 2);
    assert!(!preview.contains(&existing));
    let result = workspace_actions::execute(&plan, &AtomicBool::new(false), None).unwrap();
    assert!(result.completed, "{:?}", result.error);
    let written: HashSet<_> = result
        .receipts
        .iter()
        .flat_map(|receipt| receipt.items.iter().map(|item| item.target.clone()))
        .collect();
    assert_eq!(preview, written);
    assert_bytes(&a, b"source A bytes");
    assert_bytes(&b, b"source B other bytes");
    for receipt in &result.receipts {
        for item in &receipt.items {
            assert_eq!(
                fs::read(&item.target).unwrap(),
                fs::read(&item.source).unwrap()
            );
        }
    }
    let first = &result.receipts[0];
    let other_targets: Vec<_> = result.receipts[1..]
        .iter()
        .flat_map(|receipt| receipt.items.iter().map(|item| item.target.clone()))
        .collect();
    assert!(
        operations::undo(&first.root, &first.id, &AtomicBool::new(false), None)
            .unwrap()
            .completed
    );
    assert!(first.items.iter().all(|item| !item.target.exists()));
    assert!(other_targets.iter().all(|path| path.exists()));
    assert_bytes(&existing, b"existing unrelated user file");
}

#[test]
fn workspace_overlapping_roots_route_a_selected_file_to_exactly_one_real_owner() {
    let fixture = Fixture::new();
    let parent = fixture.folder("parent");
    let child = fixture.folder("parent/child");
    let selected = fixture.write("parent/child/file.txt", b"one physical source path");
    let session = analyze(&[parent.clone(), child.clone(), parent.clone()]);
    assert_eq!(session.files().len(), 1);
    let plan =
        workspace_actions::plan_transfer(&session, &[selected.clone()], None, TransferKind::Delete)
            .unwrap();
    assert_eq!(plan.item_count(), 1);
    assert_eq!(plan.plans().len(), 1);
    let owner = &plan.plans()[0].root;
    assert!(owner == &parent || owner == &child);
    assert!(selected.starts_with(owner));
    let result = workspace_actions::execute(&plan, &AtomicBool::new(false), None).unwrap();
    assert!(result.completed);
    assert_eq!(result.receipts.len(), 1);
    assert_eq!(result.receipts[0].root, *owner);
    assert!(!selected.exists());
    assert!(
        operations::undo(owner, &result.receipts[0].id, &AtomicBool::new(false), None)
            .unwrap()
            .completed
    );
    assert_bytes(&selected, b"one physical source path");
}

#[test]
fn workspace_batch_rejects_an_unregistered_source_even_when_it_is_next_to_a_root() {
    let fixture = Fixture::new();
    let root = fixture.folder("selected");
    let owned = fixture.write("selected/owned.txt", b"owned");
    let external = fixture.write("unselected/external.txt", b"never selected");
    let destination = fixture.folder("destination");
    let session = analyze(&[root]);
    assert!(workspace_actions::plan_transfer(
        &session,
        &[owned.clone(), external.clone()],
        Some(&destination),
        TransferKind::Move,
    )
    .is_err());
    assert_bytes(&owned, b"owned");
    assert_bytes(&external, b"never selected");
    assert_eq!(fs::read_dir(destination).unwrap().count(), 0);
}

#[test]
fn workspace_cancel_before_execution_consumes_shared_authority_without_writing() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let sources = [
        fixture.write("A/a.txt", b"a"),
        fixture.write("B/b.txt", b"b"),
    ];
    let session = analyze(&roots);
    let other = session.clone();
    let plan =
        workspace_actions::plan_transfer(&session, &sources, None, TransferKind::Delete).unwrap();
    let result = workspace_actions::execute(&plan, &AtomicBool::new(true), None).unwrap();
    assert!(result.cancelled && !result.completed);
    assert!(result.receipts.is_empty());
    assert!(sources.iter().all(|source| source.exists()));
    assert!(!session.actionable() && !other.actionable());
    assert!(workspace_actions::execute(&plan, &AtomicBool::new(false), None).is_err());
}

#[test]
fn workspace_cancel_between_roots_preserves_unprocessed_sources_and_completed_receipt() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let sources = [
        fixture.write("A/a.txt", b"a"),
        fixture.write("B/b.txt", b"b"),
    ];
    let session = analyze(&roots);
    let plan =
        workspace_actions::plan_transfer(&session, &sources, None, TransferKind::Delete).unwrap();
    let cancel = AtomicBool::new(false);
    let callback = |event: folder_organizer_rust::Progress| {
        if event.phase == "execute" && event.completed >= 1 {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    let result = workspace_actions::execute(&plan, &cancel, Some(&callback)).unwrap();
    assert!(cancel.load(Ordering::Relaxed));
    assert!(result.cancelled && !result.completed);
    let removed: Vec<_> = result
        .receipts
        .iter()
        .flat_map(|receipt| receipt.items.iter().filter(|item| item.source_removed))
        .collect();
    assert_eq!(removed.len(), 1);
    assert_eq!(sources.iter().filter(|source| source.exists()).count(), 1);
    for receipt in &result.receipts {
        assert!(
            operations::undo(&receipt.root, &receipt.id, &AtomicBool::new(false), None)
                .unwrap()
                .completed
        );
    }
    assert_bytes(&sources[0], b"a");
    assert_bytes(&sources[1], b"b");
}

#[test]
fn workspace_similarity_uses_retained_inventory_and_excludes_cross_root_exact_group() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let exact_a = fixture.write("A/report.txt", b"same exact report");
    let exact_b = fixture.write("B/report-copy.txt", b"same exact report");
    fixture.write("B/report-draft.txt", b"different report version");
    let session = analyze(&roots);
    let options = SimilarOptions {
        compare_names: true,
        compare_text: false,
        compare_images: false,
        name_threshold: 0.0,
        ..SimilarOptions::default()
    };
    let similar =
        workspace_actions::analyze_similar(&session, &options, &AtomicBool::new(false), None)
            .unwrap();
    assert!(similar.complete && !similar.cancelled);
    assert_eq!(similar.possible_pairs, 3);
    assert_eq!(similar.excluded_exact_pairs, 1);
    assert!(!similar
        .pairs
        .iter()
        .any(|pair| (pair.left == exact_a && pair.right == exact_b)
            || (pair.left == exact_b && pair.right == exact_a)));
    assert!(similar
        .pairs
        .iter()
        .any(|pair| pair.left.parent() != pair.right.parent()));
    assert!(session.actionable());
}

#[test]
fn workspace_participating_roots_leave_unselected_inventory_root_without_metadata() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let selected = fixture.write("A/a.txt", b"selected");
    let untouched = fixture.write("B/b.txt", b"inventory only");
    let session = analyze(&roots);
    let plan = workspace_actions::plan_transfer(&session, &[selected], None, TransferKind::Delete)
        .unwrap();
    let result = workspace_actions::execute(&plan, &AtomicBool::new(false), None).unwrap();
    assert!(result.completed);
    assert!(!roots[1].join(".folder-organizer-rust").exists());
    assert!(!roots[1].join(".folder-organizer").exists());
    assert_bytes(&untouched, b"inventory only");
}

#[test]
fn workspace_participating_protected_only_noop_creates_no_journal_directories() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    fixture.write("A/first.txt", b"same");
    fixture.write("B/copy.txt", b"same");
    let session = analyze(&roots);
    let result =
        workspace_actions::keep_first(&session, &roots, &AtomicBool::new(false), None).unwrap();
    assert!(result.completed && result.receipts.is_empty());
    for root in roots {
        assert!(!root.join(".folder-organizer-rust").exists());
        assert!(!root.join(".folder-organizer").exists());
        assert!(!root.join("delete_files").exists());
    }
}

#[test]
fn workspace_additional_rules_reserve_outputs_across_roots_and_restore_each_journal() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let sources = [
        fixture.write("A/same.txt", b"A source"),
        fixture.write("B/same.txt", b"B source"),
    ];
    let destination = fixture.folder("destination");
    let rules = vec![folder_organizer_rust::rules::Rule {
        name: "종류별".into(),
        target: "{ext}".into(),
        ..Default::default()
    }];
    let session = analyze(&roots);
    let plan = workspace_actions::plan_classification(
        &session,
        &rules,
        &destination,
        TransferKind::Move,
        &[],
    )
    .unwrap();
    assert_eq!(plan.item_count(), 2);
    let targets = plan
        .plans()
        .iter()
        .flat_map(|root| root.items.iter().map(|item| item.target.clone()))
        .collect::<HashSet<_>>();
    assert_eq!(targets.len(), 2);
    let receipt = workspace_actions::execute(&plan, &AtomicBool::new(false), None).unwrap();
    assert!(receipt.completed);
    assert!(sources.iter().all(|path| !path.exists()));
    for operation in receipt.receipts {
        assert!(operation
            .items
            .iter()
            .all(|item| targets.contains(&item.target)));
        assert!(
            operations::undo(
                &operation.root,
                &operation.id,
                &AtomicBool::new(false),
                None
            )
            .unwrap()
            .completed
        );
    }
    assert_bytes(&sources[0], b"A source");
    assert_bytes(&sources[1], b"B source");
}

#[test]
fn workspace_additional_later_root_lock_blocks_batch_before_any_source_is_retired() {
    let fixture = Fixture::new();
    let roots = [fixture.folder("A"), fixture.folder("B")];
    let sources = [
        fixture.write("A/a.txt", b"A source"),
        fixture.write("B/b.txt", b"B source"),
    ];
    let session = analyze(&roots);
    let plan =
        workspace_actions::plan_transfer(&session, &sources, None, TransferKind::Delete).unwrap();
    let lock_directory = roots[1].join(".folder-organizer");
    fs::create_dir(&lock_directory).unwrap();
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(lock_directory.join("operations.lock"))
        .unwrap();
    #[cfg(unix)]
    {
        use std::os::fd::AsRawFd;
        assert_eq!(
            unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
            0
        );
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        assert_ne!(
            unsafe {
                windows_sys::Win32::Storage::FileSystem::LockFile(lock.as_raw_handle(), 0, 0, 1, 0)
            },
            0
        );
    }
    assert!(workspace_actions::execute(&plan, &AtomicBool::new(false), None).is_err());
    assert!(!session.actionable());
    assert_bytes(&sources[0], b"A source");
    assert_bytes(&sources[1], b"B source");
    assert!(roots.iter().all(|root| !root.join("delete_files").exists()));
    drop(lock);
}
