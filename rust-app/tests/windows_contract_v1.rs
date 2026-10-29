#![cfg(windows)]
//! New Windows-only contracts; Linux-passed test cases are not rerun here.
use folder_organizer_rust::{
    core::{self, ScanOptions},
    operations::{self, TransferKind},
};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::AtomicBool,
};
struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut b = [0; 16];
        getrandom::getrandom(&mut b).unwrap();
        let id: String = b.iter().map(|n| format!("{n:02x}")).collect();
        let p = std::env::temp_dir().join(format!("organizer-windows-contract-{id}"));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let p = self.0.join(name);
        fs::write(&p, bytes).unwrap();
        p
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
fn every_local_drive_letter_and_verbatim_form_is_a_drive_root_not_a_nas() {
    for letter in 'A'..='Z' {
        assert!(operations::is_local_drive_root(Path::new(&format!(
            "{letter}:\\"
        ))));
        assert!(operations::is_local_drive_root(Path::new(&format!(
            "\\\\?\\{letter}:\\"
        ))));
        assert!(!operations::is_local_drive_root(Path::new(&format!(
            "{letter}:\\Users\\sample"
        ))));
    }
    assert!(!operations::is_local_drive_root(Path::new(
        r"\\server\share\"
    )));
    assert!(!operations::is_local_drive_root(Path::new(
        r"\\?\UNC\server\share\"
    )));
}

#[test]
fn windows_handle_identity_change_time_and_creation_date_are_distinct() {
    use std::os::windows::fs::MetadataExt;
    let f = Fixture::new();
    let path = f.write("a.txt", b"same length");
    let file = core::safe_open(&path).unwrap();
    let opened = core::snapshot_open_file(&file).unwrap();
    let before = core::snapshot(&path).unwrap();
    assert_eq!(opened, before);
    assert_ne!(before.inode, 0);
    let created = fs::metadata(&path).unwrap().creation_time();
    let modified = fs::metadata(&path).unwrap().last_write_time();
    assert!(created > 0 && modified > 0);
    drop(file);
    std::thread::sleep(std::time::Duration::from_millis(5));
    fs::write(&path, b"new content").unwrap();
    let after = core::snapshot(&path).unwrap();
    assert_eq!(before.device, after.device);
    assert_eq!(before.inode, after.inode);
    assert_ne!(before.changed_ns, after.changed_ns);
    assert_eq!(fs::metadata(&path).unwrap().creation_time(), created);
}

#[test]
fn windows_movefile_capture_and_undo_preserve_verified_first_file() {
    let f = Fixture::new();
    f.write("a.txt", b"native Windows duplicate");
    f.write("b.txt", b"native Windows duplicate");
    let scan = f.scan();
    let cancel = AtomicBool::new(false);
    let groups = core::find_duplicates(&scan, &cancel, None).unwrap();
    assert_eq!(groups.len(), 1);
    let keeper = groups[0].paths[0].clone();
    let receipt = operations::keep_first(&scan, &groups, &[], &cancel, None).unwrap();
    assert!(receipt.completed, "{:?}", receipt.errors);
    assert!(keeper.exists());
    assert_eq!(receipt.items.len(), 1);
    assert!(!receipt.items[0].source.exists());
    assert!(receipt.items[0].target.exists());
    let undo = operations::undo(&f.0, &receipt.id, &cancel, None).unwrap();
    assert!(undo.completed, "{:?}", undo.errors);
    assert_eq!(
        fs::read(&receipt.items[0].source).unwrap(),
        b"native Windows duplicate"
    );
    assert!(keeper.exists());
    assert!(!f.0.join(".folder-organizer-rust/quarantine").exists());
    assert!(!f.0.join(".folder-organizer-rust/retained").exists());
}

#[test]
fn windows_exclusive_move_does_not_replace_a_raced_in_target() {
    let f = Fixture::new();
    let source = f.write("a.txt", b"original bytes");
    let cancel = AtomicBool::new(false);
    let plan =
        operations::plan_files(&f.scan(), &[source.clone()], None, TransferKind::Delete).unwrap();
    let target = plan.items[0].target.clone();
    fs::create_dir_all(target.parent().unwrap()).unwrap();
    fs::write(&target, b"raced-in user data").unwrap();
    let receipt = operations::execute(&plan, &cancel, None).unwrap();
    assert!(!receipt.completed && !receipt.errors.is_empty());
    assert_eq!(fs::read(source).unwrap(), b"original bytes");
    assert_eq!(fs::read(target).unwrap(), b"raced-in user data");
}

struct ChildGuard(std::process::Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
#[test]
fn python_msvcrt_byte_zero_lock_blocks_native_rust_without_truncation() {
    let f = Fixture::new();
    let source = f.write("a.txt", b"unchanged original");
    let plan =
        operations::plan_files(&f.scan(), &[source.clone()], None, TransferKind::Delete).unwrap();
    let app = f.0.join(".folder-organizer");
    fs::create_dir(&app).unwrap();
    let lock = app.join("operations.lock");
    fs::write(&lock, b"preserved shared lock contents").unwrap();
    let code="import msvcrt,sys\nf=open(sys.argv[1],'r+b');f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)\nprint('locked',flush=True)\nsys.stdin.buffer.read(1)\nf.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)\nf.close()\n";
    let child = Command::new("python")
        .args(["-c", code])
        .arg(&lock)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut child = ChildGuard(child);
    let mut ready = String::new();
    BufReader::new(child.0.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert_eq!(ready.trim(), "locked");
    assert!(operations::execute(&plan, &AtomicBool::new(false), None).is_err());
    assert_eq!(fs::read(&source).unwrap(), b"unchanged original");
    assert_eq!(fs::read(&lock).unwrap(), b"preserved shared lock contents");
    child.0.stdin.take().unwrap().write_all(b"x").unwrap();
    assert!(child.0.wait().unwrap().success());
    assert!(
        operations::execute(&plan, &AtomicBool::new(false), None)
            .unwrap()
            .completed
    );
    assert_eq!(fs::read(lock).unwrap(), b"preserved shared lock contents");
}
