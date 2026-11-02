#![cfg(windows)]
//! New Windows-native multi-root contracts; previously passed suites are skipped.
use folder_organizer_rust::{core, multiroot};
use std::{
    fs::{self, FileTimes, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::windows::{ffi::OsStrExt, fs::OpenOptionsExt, io::AsRawHandle},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};
use windows_sys::Win32::Storage::FileSystem::{
    FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut bytes = [0_u8; 12];
        getrandom::getrandom(&mut bytes).unwrap();
        let id: String = bytes.iter().map(|v| format!("{v:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-windows-multi-v080-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
    fn directory(&self, name: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::create_dir_all(&path).unwrap();
        path
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn file_id(path: &Path) -> (u64, u128) {
    let file = core::safe_open(path).unwrap();
    let mut native: FILE_ID_INFO = unsafe { std::mem::zeroed() };
    assert_ne!(
        unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle() as _,
                FileIdInfo,
                &mut native as *mut _ as _,
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        },
        0
    );
    let expected = (
        native.VolumeSerialNumber,
        u128::from_le_bytes(native.FileId.Identifier),
    );
    let snapshot = core::snapshot_open_file(&file).unwrap();
    assert_eq!((snapshot.device, snapshot.inode), expected);
    expected
}

#[test]
fn native_long_unicode_overlaps_keep_membership_and_128_bit_hardlink_identity() {
    let fixture = Fixture::new();
    assert!(fixture.0.to_string_lossy().starts_with(r"\\?\"));
    let mut left = fixture.0.join("a-작업");
    for part in 0..7 {
        left = left.join(format!(
            "긴폴더-{part}-abcdefghijklmnopqrstuvwxyz0123456789"
        ));
    }
    let nested = left.join("하위");
    fs::create_dir_all(&nested).unwrap();
    assert!(left.as_os_str().encode_wide().count() > 260);
    let right = fixture.directory("b-보관");
    let original = nested.join("원본.bin");
    let copy = right.join("복사본.bin");
    let alias = right.join("하드링크.bin");
    let raw = b"independent copy, alias, and overlapping roots are different concepts\n";
    fs::write(&original, raw).unwrap();
    fs::write(&copy, raw).unwrap();
    fs::hard_link(&original, &alias).unwrap();
    assert_eq!(file_id(&original), file_id(&alias));
    assert_ne!(file_id(&original), file_id(&copy));
    let report = multiroot::analyze(
        &[left.clone(), right.clone(), nested.clone(), left.clone()],
        &core::ScanOptions::default(),
        true,
        4,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(report.complete && report.inventory_complete && !report.cancelled);
    assert!(!report.action_authority);
    assert_eq!(report.roots.len(), 3);
    assert_eq!(report.duplicate_inputs.len(), 1);
    assert!(report
        .roots
        .iter()
        .all(|root| root.volume_group == "unknown-local"));
    assert_eq!(report.files.len(), 3);
    assert_eq!(report.duplicates.metrics.hardlink_aliases, 1);
    assert_eq!(report.duplicates.groups.len(), 1);
    let group = &report.duplicates.groups[0];
    assert_eq!(group.paths.len(), 2);
    assert!(!group.action_authority);
    let record = report
        .files
        .iter()
        .find(|file| file.path == original)
        .unwrap();
    assert_eq!(record.root_ids, vec!["root-0001", "root-0003"]);
    assert_eq!(record.owner_root_id, "root-0001");
    assert_eq!(record.relative_paths["root-0003"], "원본.bin");
    assert!(record.created_ns.is_some());
    for path in [&original, &copy, &alias] {
        assert_eq!(fs::read(path).unwrap(), raw);
    }
}

#[test]
fn native_cross_root_sampling_uses_change_time_when_mtime_and_file_id_stay_same() {
    const SIZE: usize = 2 * 1024 * 1024 + 31;
    let fixture = Fixture::new();
    let left = fixture.directory("a-left");
    let right = fixture.directory("b-right");
    let healthy = left.join("healthy.bin");
    let healthy_copy = right.join("healthy-copy.bin");
    let other = left.join("other.bin");
    let victim = right.join("changed-after-sample.bin");
    fs::write(&healthy, vec![31_u8; SIZE]).unwrap();
    fs::write(&healthy_copy, vec![31_u8; SIZE]).unwrap();
    fs::write(&other, vec![61_u8; SIZE]).unwrap();
    fs::write(&victim, vec![91_u8; SIZE]).unwrap();
    let before = core::snapshot(&victim).unwrap();
    let modified = fs::metadata(&victim).unwrap().modified().unwrap();
    let changed = AtomicBool::new(false);
    let on_progress = |event: multiroot::MultiProgress| {
        if event.phase == "duplicates_sample"
            && event.completed == 4
            && !changed.swap(true, Ordering::Relaxed)
        {
            let mut file = OpenOptions::new().write(true).open(&victim).unwrap();
            file.seek(SeekFrom::Start(128 * 1024)).unwrap();
            file.write_all(b"same size and mtime, new primary bytes")
                .unwrap();
            file.set_times(FileTimes::new().set_modified(modified))
                .unwrap();
            file.sync_all().unwrap();
        }
    };
    let report = multiroot::analyze(
        &[left, right],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        Some(&on_progress),
    )
    .unwrap();
    assert!(changed.load(Ordering::Relaxed));
    let after = core::snapshot(&victim).unwrap();
    assert_eq!(
        (before.device, before.inode, before.size, before.modified_ns),
        (after.device, after.inode, after.size, after.modified_ns)
    );
    assert_ne!(
        before.changed_ns, after.changed_ns,
        "NTFS ChangeTime must expose this edit"
    );
    assert!(report.inventory_complete && !report.complete && !report.cancelled);
    assert_eq!(report.duplicates.groups.len(), 1);
    assert_eq!(
        report.duplicates.groups[0].paths,
        vec![healthy.clone(), healthy_copy.clone()]
    );
    assert!(report
        .duplicates
        .warnings
        .iter()
        .any(|warning| warning.path == victim && warning.phase == "final_snapshot"));
    assert_eq!(fs::read(healthy).unwrap(), vec![31_u8; SIZE]);
    assert_eq!(fs::read(healthy_copy).unwrap(), vec![31_u8; SIZE]);
    assert_eq!(fs::read(other).unwrap(), vec![61_u8; SIZE]);
}

#[test]
fn native_exclusive_handle_partial_then_hash_cancel_preserve_both_real_sources() {
    let fixture = Fixture::new();
    let left = fixture.directory("left");
    let right = fixture.directory("right");
    let locked = left.join("exclusive.bin");
    let healthy = left.join("healthy.txt");
    let copy = right.join("copy.txt");
    let healthy_bytes = b"healthy exact source remains unchanged";
    // Equal sizes force the locked entry through the new global exact stage
    // even when Windows permits metadata-only enumeration of the open file.
    let locked_bytes = vec![b'L'; healthy_bytes.len()];
    fs::write(&locked, &locked_bytes).unwrap();
    fs::write(&healthy, healthy_bytes).unwrap();
    fs::write(&copy, healthy_bytes).unwrap();
    let mut held = OpenOptions::new()
        .read(true)
        .share_mode(0)
        .open(&locked)
        .unwrap();
    let before = core::snapshot_open_file(&held).unwrap();
    let partial = multiroot::analyze(
        &[left.clone(), right.clone()],
        &core::ScanOptions::default(),
        true,
        2,
        &AtomicBool::new(false),
        None,
    )
    .unwrap();
    assert!(!partial.complete && !partial.cancelled);
    assert_eq!(partial.duplicates.groups.len(), 1);
    assert_eq!(
        partial.duplicates.groups[0].paths,
        vec![healthy.clone(), copy.clone()]
    );
    assert!(partial.duplicates.warning_count > 0);
    assert!(
        partial
            .duplicates
            .warnings
            .iter()
            .any(|warning| warning.path == locked)
            || partial.roots.iter().any(|root| root
                .warnings
                .iter()
                .any(|warning| warning.contains("exclusive.bin")))
    );
    let cancel = AtomicBool::new(false);
    let on_progress = |event: multiroot::MultiProgress| {
        // The shared kernel reports each completed hash as "duplicates";
        // stop before the second file can finish and publish an exact group.
        if event.root_id.is_empty() && event.phase == "duplicates" && event.completed == 1 {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    let report = multiroot::analyze(
        &[left, right],
        &core::ScanOptions::default(),
        true,
        2,
        &cancel,
        Some(&on_progress),
    )
    .unwrap();
    assert!(cancel.load(Ordering::Relaxed));
    assert!(report.cancelled && !report.complete);
    assert!(report.duplicates.cancelled && report.duplicates.groups.is_empty());
    assert!(report.duplicates.warning_count > 0);
    let after = core::snapshot_open_file(&held).unwrap();
    assert_eq!(
        (before.device, before.inode, before.size, before.modified_ns),
        (after.device, after.inode, after.size, after.modified_ns)
    );
    let mut bytes = Vec::new();
    held.read_to_end(&mut bytes).unwrap();
    assert_eq!(bytes, locked_bytes);
    drop(held);
    assert_eq!(fs::read(healthy).unwrap(), healthy_bytes);
    assert_eq!(fs::read(copy).unwrap(), healthy_bytes);
}
