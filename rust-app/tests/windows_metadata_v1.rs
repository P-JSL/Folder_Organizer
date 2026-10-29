#![cfg(windows)]
//! New NTFS alternate-stream preservation contract. This target does not repeat
//! earlier Linux or Windows operation tests.
use folder_organizer_rust::{
    core::{self, ScanOptions},
    operations::{self, TransferKind},
};
use std::{fs, path::PathBuf, sync::atomic::AtomicBool};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut bytes = [0; 16];
        getrandom::getrandom(&mut bytes).unwrap();
        let id: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-ntfs-preservation-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn stream(path: &std::path::Path, name: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(":");
    value.push(name);
    PathBuf::from(value)
}

#[test]
fn named_ntfs_streams_remain_raw_and_survive_undo_including_empty_stream() {
    let f = Fixture::new();
    let source = f.0.join("a.bin");
    let payload = vec![b'x'; 64 * 1024];
    fs::write(&source, &payload).unwrap();
    fs::write(stream(&source, "secret"), b"extra original bytes")
        .expect("Windows test fixture must support NTFS named streams");
    fs::write(stream(&source, "empty"), b"")
        .expect("Windows test fixture must support zero-byte named streams");
    let cancel = AtomicBool::new(false);
    let scan = core::scan(&f.0, &ScanOptions::default(), &cancel, None).unwrap();
    let plan =
        operations::plan_files(&scan, &[source.clone()], None, TransferKind::Delete).unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert!(receipt.completed, "{:?}", receipt.errors);
    let raw = &receipt.items[0].target;
    assert_eq!(
        fs::read(stream(raw, "secret")).unwrap(),
        b"extra original bytes"
    );
    let result = operations::compact(&f.0, &receipt.id, &cancel, None).unwrap();
    assert_eq!(result.files, 0);
    assert!(!result.warnings.is_empty());
    assert!(raw.exists());
    assert_eq!(fs::read(raw).unwrap(), payload);
    assert_eq!(
        fs::read(stream(raw, "secret")).unwrap(),
        b"extra original bytes"
    );
    assert_eq!(fs::read(stream(raw, "empty")).unwrap(), b"");
    let restored = operations::undo(&f.0, &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    assert_eq!(fs::read(&source).unwrap(), payload);
    assert_eq!(
        fs::read(stream(&source, "secret")).unwrap(),
        b"extra original bytes"
    );
    assert_eq!(fs::read(stream(&source, "empty")).unwrap(), b"");
}
