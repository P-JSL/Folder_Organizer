//! New review regressions; previously passed operation suites are not repeated.
use folder_organizer_rust::{
    core::{self, ScanOptions},
    operations::{self, TransferKind},
};
use std::{fs, path::PathBuf, sync::atomic::AtomicBool};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0; 16];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-operation-review-v070-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn cancelled_transfer_preserves_later_user_target(kind: TransferKind) {
    let fixture = Fixture::new();
    let root = fixture.0.join("source");
    let destination = fixture.0.join("destination");
    fs::create_dir(&root).unwrap();
    fs::create_dir(&destination).unwrap();
    let source = root.join("original.txt");
    fs::write(&source, b"user data must survive cancellation").unwrap();
    let running = AtomicBool::new(false);
    let scan = core::scan(&root, &ScanOptions::default(), &running, None).unwrap();
    let plan = operations::plan_files(&scan, &[source.clone()], Some(&destination), kind).unwrap();
    let receipt = operations::execute(&plan, &AtomicBool::new(true), None).unwrap();
    assert!(receipt.cancelled);
    assert!(!receipt.items[0].source_removed);
    let target = receipt.items[0].target.clone();
    assert!(!target.exists());
    // A user creates this alias after cancellation; it was never published by
    // the app and must not be captured into hidden undo storage.
    fs::hard_link(&source, &target).unwrap();
    let before = core::snapshot(&target).unwrap();
    let restored = operations::undo(&root, &receipt.id, &running, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    assert_eq!(core::snapshot(&target).unwrap(), before);
    assert_eq!(
        fs::read(&target).unwrap(),
        b"user data must survive cancellation"
    );
    assert_eq!(
        fs::read(&source).unwrap(),
        b"user data must survive cancellation"
    );
}

#[test]
fn undo_cancelled_copy_preserves_a_later_user_created_source_hardlink() {
    cancelled_transfer_preserves_later_user_target(TransferKind::Copy);
}

#[test]
fn undo_cancelled_move_preserves_a_later_user_created_source_hardlink() {
    cancelled_transfer_preserves_later_user_target(TransferKind::Move);
}
