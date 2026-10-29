//! New native-operation regressions. These never touch personal originals.
use folder_organizer_rust::{
    core::{self, ScanOptions},
    operations::{self, TransferKind},
};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::atomic::AtomicBool,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0; 16];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let root = std::env::temp_dir().join(format!("organizer-rust-operations-{id}"));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
    fn root(&self) -> &Path {
        &self.0
    }
    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.0.join(name);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(&path, bytes).unwrap();
        path
    }
    fn scan(&self) -> core::ScanResult {
        core::scan(
            &self.0,
            &ScanOptions::default(),
            &AtomicBool::new(false),
            None,
        )
        .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn exact_keep_first_preserves_protected_and_undo_has_no_legacy_storage() {
    let f = Fixture::new();
    let payload = b"verified exact content";
    f.write("a.txt", payload);
    f.write("b.txt", payload);
    let protected = f.write("protected/c.txt", payload);
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let groups = core::find_duplicates(&scan, &cancel, None).unwrap();
    assert_eq!(groups.len(), 1);
    let first = groups[0].paths[0].clone();
    let receipt = operations::keep_first(
        &scan,
        &groups,
        &[protected.parent().unwrap().to_path_buf()],
        &cancel,
        None,
    )
    .unwrap();
    assert!(receipt.completed);
    assert!(first.exists());
    assert!(protected.exists());
    assert!(receipt.items.iter().all(|i| i.source_removed
        && !i.source.exists()
        && i.target.parent() == Some(f.0.join("delete_files").as_path())));
    let restored = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed);
    for path in ["a.txt", "b.txt", "protected/c.txt"] {
        assert_eq!(fs::read(f.0.join(path)).unwrap(), payload);
    }
    assert!(!f.0.join(".folder-organizer-rust/quarantine").exists());
    assert!(!f.0.join(".folder-organizer-rust/retained").exists());
}

#[test]
fn changed_exact_group_is_rejected_before_any_file_moves() {
    let f = Fixture::new();
    f.write("a.txt", b"same-content");
    f.write("b.txt", b"same-content");
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let groups = core::find_duplicates(&scan, &cancel, None).unwrap();
    fs::write(&groups[0].paths[1], b"changed-file").unwrap();
    assert!(operations::keep_first(&scan, &groups, &[], &cancel, None).is_err());
    assert!(f.0.join("a.txt").exists() && f.0.join("b.txt").exists());
    assert!(!f.0.join("delete_files").exists());
}

#[test]
fn public_preview_edits_cannot_authorize_another_target() {
    let f = Fixture::new();
    let path = f.write("a.txt", b"one");
    let scan = f.scan();
    let mut plan =
        operations::plan_files(&scan, &[path.clone()], None, TransferKind::Delete).unwrap();
    plan.items[0].target = f.0.join("unexpected.txt");
    assert!(operations::execute(&plan, &AtomicBool::new(false), None).is_err());
    assert!(path.exists());
    assert!(!f.0.join("unexpected.txt").exists());
}

#[test]
fn manual_delete_undo_refuses_conflicting_original_and_preserves_bytes() {
    let f = Fixture::new();
    let path = f.write("a.txt", b"original");
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let plan = operations::plan_files(&scan, &[path.clone()], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert!(receipt.completed);
    fs::write(&path, b"new user data").unwrap();
    let undo = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
    assert!(!undo.completed && !undo.errors.is_empty());
    assert_eq!(fs::read(&path).unwrap(), b"new user data");
    assert_eq!(fs::read(&receipt.items[0].target).unwrap(), b"original");
}

#[test]
fn copy_and_move_share_verified_nonoverwriting_undo() {
    for kind in [TransferKind::Copy, TransferKind::Move] {
        let f = Fixture::new();
        let original = f.write("source.txt", b"copy and move data");
        let destination = f.0.join("destination");
        fs::create_dir(&destination).unwrap();
        fs::write(destination.join("source.txt"), b"existing user file").unwrap();
        let scan = f.scan();
        let cancel = AtomicBool::new(false);
        let plan =
            operations::plan_files(&scan, &[original.clone()], Some(&destination), kind).unwrap();
        assert_ne!(plan.items[0].target, destination.join("source.txt"));
        let receipt = operations::execute(&plan, &cancel, None).unwrap();
        assert!(receipt.completed, "{:?}", receipt.errors);
        assert_eq!(original.exists(), kind == TransferKind::Copy);
        assert_eq!(
            fs::read(&receipt.items[0].target).unwrap(),
            b"copy and move data"
        );
        let undo = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
        assert!(undo.completed, "{:?}", undo.errors);
        assert_eq!(fs::read(&original).unwrap(), b"copy and move data");
        assert!(!receipt.items[0].target.exists());
        assert_eq!(
            fs::read(destination.join("source.txt")).unwrap(),
            b"existing user file"
        );
    }
}

#[test]
fn independent_packed_bytes_restore_after_keeper_and_raw_metadata_are_gone() {
    let f = Fixture::new();
    let payload = vec![b'a'; 2 * 1024 * 1024 + 512];
    f.write("a.bin", &payload);
    f.write("b.bin", &payload);
    f.write("c.bin", &payload);
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let groups = core::find_duplicates(&scan, &cancel, None).unwrap();
    let keeper = groups[0].paths[0].clone();
    let receipt = operations::keep_first(&scan, &groups, &[], &cancel, None).unwrap();
    assert!(receipt.completed);
    let compact = operations::compact(f.root(), &receipt.id, &cancel, None).unwrap();
    assert_eq!(compact.files, 2);
    assert!(compact.saved_bytes > 1024 * 1024);
    for item in &receipt.items {
        assert!(!item.target.exists());
    }
    fs::remove_file(keeper).unwrap();
    let journal = f.0.join(".folder-organizer-rust/history").join(&receipt.id);
    assert!(fs::read_dir(&journal).unwrap().any(|e| e
        .unwrap()
        .file_name()
        .to_string_lossy()
        .ends_with(".receipt.json")));
    fs::remove_file(journal.join("journal.json")).unwrap();
    fs::remove_file(journal.join("events.jsonl")).unwrap();
    let restored = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    for item in &receipt.items {
        assert_eq!(fs::read(&item.source).unwrap(), payload);
    }
}

#[test]
fn tampered_signed_raw_journal_is_not_hidden_by_valid_compressed_snapshot() {
    let f = Fixture::new();
    let a = f.write("a.txt", &vec![b'x'; 4096]);
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let plan = operations::plan_files(&scan, &[a], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    let journal =
        f.0.join(".folder-organizer-rust/history")
            .join(&receipt.id)
            .join("journal.json");
    let mut data = fs::read(&journal).unwrap();
    let index = data.iter().position(|b| *b == b'p').unwrap();
    data[index] = b'q';
    fs::write(&journal, data).unwrap();
    assert!(operations::undo(f.root(), &receipt.id, &cancel, None).is_err());
    assert!(receipt.items[0].target.exists());
}

#[test]
fn damaged_packed_chunk_cannot_publish_an_invented_original() {
    let f = Fixture::new();
    let a = f.write("a.txt", &vec![b'x'; 64 * 1024]);
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let plan = operations::plan_files(&scan, &[a.clone()], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert_eq!(
        operations::compact(f.root(), &receipt.id, &cancel, None)
            .unwrap()
            .files,
        1
    );
    let objects = f.0.join(".folder-organizer-rust/objects");
    let directory = fs::read_dir(objects)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let object = fs::read_dir(directory)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    fs::write(object, b"corrupted").unwrap();
    let undo = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
    assert!(!undo.completed);
    assert!(!a.exists());
}

#[test]
fn cancelled_new_operation_does_not_move_any_original() {
    let f = Fixture::new();
    let a = f.write("a.txt", b"unchanged");
    let scan = f.scan();
    let plan = operations::plan_files(&scan, &[a.clone()], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &AtomicBool::new(true), None).unwrap();
    assert!(receipt.cancelled);
    assert_eq!(fs::read(a).unwrap(), b"unchanged");
}

#[test]
fn torn_trailing_event_rebases_authenticated_prefix_before_undo_append() {
    use std::io::Write;
    let f = Fixture::new();
    let path = f.write("a.txt", b"preserve on interrupted append");
    let cancel = AtomicBool::new(false);
    let plan =
        operations::plan_files(&f.scan(), &[path.clone()], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert!(receipt.completed);
    let events =
        f.0.join(".folder-organizer-rust/history")
            .join(&receipt.id)
            .join("events.jsonl");
    let mut file = fs::OpenOptions::new().append(true).open(events).unwrap();
    file.write_all(b"{\"payload\":{\"sequence\":999").unwrap();
    file.sync_all().unwrap();
    drop(file);
    let restored = operations::undo(f.root(), &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    assert_eq!(fs::read(path).unwrap(), b"preserve on interrupted append");
    let records = operations::history(f.root()).unwrap();
    assert_eq!(records.len(), 1);
    assert!(records[0].restored);
}

#[cfg(unix)]
#[test]
fn python_compatible_os_lock_blocks_rust_without_changing_or_truncating_lock() {
    use std::os::fd::AsRawFd;
    let f = Fixture::new();
    let path = f.write("a.txt", b"unchanged");
    let scan = f.scan();
    let plan = operations::plan_files(&scan, &[path.clone()], None, TransferKind::Delete).unwrap();
    let legacy = f.0.join(".folder-organizer");
    fs::create_dir(&legacy).unwrap();
    let lock_path = legacy.join("operations.lock");
    fs::write(&lock_path, b"persistent Python lock contents").unwrap();
    let file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&lock_path)
        .unwrap();
    assert_eq!(
        unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    assert!(operations::execute(&plan, &AtomicBool::new(false), None).is_err());
    assert_eq!(fs::read(&path).unwrap(), b"unchanged");
    assert_eq!(
        fs::read(&lock_path).unwrap(),
        b"persistent Python lock contents"
    );
    assert_eq!(unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_UN) }, 0);
    drop(file);
    assert!(
        operations::execute(&plan, &AtomicBool::new(false), None)
            .unwrap()
            .completed
    );
    assert_eq!(
        fs::read(lock_path).unwrap(),
        b"persistent Python lock contents"
    );
}

#[cfg(unix)]
#[test]
fn link_source_and_link_parent_are_rejected_without_touching_targets() {
    use std::os::unix::fs::symlink;
    let f = Fixture::new();
    let outside = Fixture::new();
    let target = outside.write("secret.txt", b"keep");
    symlink(&target, f.0.join("link.txt")).unwrap();
    symlink(outside.root(), f.0.join("linked-folder")).unwrap();
    let scan = f.scan();
    assert!(
        operations::plan_files(&scan, &[f.0.join("link.txt")], None, TransferKind::Delete).is_err()
    );
    assert!(operations::plan_files(
        &scan,
        &[f.0.join("linked-folder/secret.txt")],
        None,
        TransferKind::Delete
    )
    .is_err());
    assert_eq!(fs::read(target).unwrap(), b"keep");
}
