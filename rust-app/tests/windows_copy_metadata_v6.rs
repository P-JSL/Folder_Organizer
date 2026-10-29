#![cfg(windows)]
//! Copy/move must not silently discard metadata outside the streamed payload.
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
        let id: String = random.iter().map(|b| format!("{b:02x}")).collect();
        let root = std::env::temp_dir().join(format!("organizer-copy-metadata-v6-{id}"));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn ads(path: &std::path::Path) -> PathBuf {
    let mut path = path.as_os_str().to_os_string();
    path.push(":original-data");
    PathBuf::from(path)
}

fn refuses_lossy_metadata(kind: TransferKind) {
    for contents in [b"extra original bytes".as_slice(), b"".as_slice()] {
        let fixture = Fixture::new();
        let root = fixture.0.join("source");
        let destination = fixture.0.join("destination");
        fs::create_dir(&root).unwrap();
        fs::create_dir(&destination).unwrap();
        let path = root.join("원본.txt");
        fs::write(&path, b"main original bytes").unwrap();
        fs::write(ads(&path), contents).expect("Windows fixture must support named streams");
        let cancel = AtomicBool::new(false);
        let scan = core::scan(&root, &ScanOptions::default(), &cancel, None).unwrap();
        let error = operations::plan_files(&scan, &[path.clone()], Some(&destination), kind)
            .expect_err("Copy/move must not publish an incomplete metadata representation");
        assert!(error.contains("추가 데이터 스트림"), "{error}");
        assert_eq!(fs::read(&path).unwrap(), b"main original bytes");
        assert_eq!(fs::read(ads(&path)).unwrap(), contents);
        assert!(fs::read_dir(&destination).unwrap().next().is_none());
        assert!(!root.join(".folder-organizer-rust").exists());
    }
}

#[test]
fn copying_named_or_empty_stream_is_refused_without_losing_original_data() {
    refuses_lossy_metadata(TransferKind::Copy);
}

#[test]
fn moving_named_or_empty_stream_is_refused_without_retiring_original() {
    refuses_lossy_metadata(TransferKind::Move);
}

fn ordinary_streams_still_work(kind: TransferKind) {
    let fixture = Fixture::new();
    let root = fixture.0.join("source");
    let destination = fixture.0.join("destination");
    fs::create_dir(&root).unwrap();
    fs::create_dir(&destination).unwrap();
    let first = root.join("일반 파일.txt");
    let empty = root.join("빈 파일.txt");
    fs::write(&first, b"ordinary bytes remain supported").unwrap();
    fs::write(&empty, b"").unwrap();
    let cancel = AtomicBool::new(false);
    let scan = core::scan(&root, &ScanOptions::default(), &cancel, None).unwrap();
    let plan = operations::plan_files(
        &scan,
        &[first.clone(), empty.clone()],
        Some(&destination),
        kind,
    )
    .unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert!(receipt.completed, "{:?}", receipt.errors);
    assert_eq!(
        fs::read(destination.join("일반 파일.txt")).unwrap(),
        b"ordinary bytes remain supported"
    );
    assert_eq!(fs::read(destination.join("빈 파일.txt")).unwrap(), b"");
    assert_eq!(first.exists(), kind == TransferKind::Copy);
    let restored = operations::undo(&root, &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    assert_eq!(fs::read(first).unwrap(), b"ordinary bytes remain supported");
    assert_eq!(fs::read(empty).unwrap(), b"");
    assert!(!destination.join("일반 파일.txt").exists());
    assert!(!destination.join("빈 파일.txt").exists());
}

#[test]
fn ordinary_and_empty_files_can_still_be_copied_and_undone() {
    ordinary_streams_still_work(TransferKind::Copy);
}

#[test]
fn ordinary_and_empty_files_can_still_be_moved_and_undone() {
    ordinary_streams_still_work(TransferKind::Move);
}
