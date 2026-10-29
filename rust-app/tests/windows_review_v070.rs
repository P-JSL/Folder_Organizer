#![cfg(windows)]
//! New native contracts for the changed 0.7 sampling and undo publication paths.
use folder_organizer_rust::{
    core::{self, ScanOptions, ScanResult},
    operations::{self, TransferKind},
};
use std::{
    fs::{self, FileTimes, OpenOptions},
    os::windows::{ffi::OsStrExt, io::AsRawHandle},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};
use windows_sys::Win32::Storage::FileSystem::{
    FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO,
};

const SIZE: usize = 2 * 1024 * 1024 + 47;
const SAMPLE_BLOCK: u64 = 64 * 1024;

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0_u8; 12];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-windows-v070-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path.canonicalize().unwrap())
    }
    fn scan(root: &Path) -> ScanResult {
        let mut scan =
            core::scan(root, &ScanOptions::default(), &AtomicBool::new(false), None).unwrap();
        scan.files
            .sort_by(|left, right| left.relative_path.cmp(&right.relative_path));
        scan
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn native_identity(path: &Path) -> (u64, u128) {
    let file = core::safe_open(path).unwrap();
    let mut identity: FILE_ID_INFO = unsafe { std::mem::zeroed() };
    assert_ne!(
        unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle() as _,
                FileIdInfo,
                &mut identity as *mut _ as _,
                std::mem::size_of::<FILE_ID_INFO>() as u32,
            )
        },
        0,
        "The native Windows test volume must expose FileIdInfo"
    );
    let expected = (
        identity.VolumeSerialNumber,
        u128::from_le_bytes(identity.FileId.Identifier),
    );
    let snapshot = core::snapshot_open_file(&file).unwrap();
    assert_eq!((snapshot.device, snapshot.inode), expected);
    expected
}

#[test]
fn native_verbatim_unicode_long_paths_sample_all_three_ranges_without_full_hashing() {
    let fixture = Fixture::new();
    assert!(fixture.0.as_os_str().to_string_lossy().starts_with(r"\\?\"));
    let mut root = fixture.0.clone();
    for id in 0..7 {
        root = root.join(format!("긴경로-{id}-abcdefghijklmnopqrstuvwxyz0123456789"));
    }
    fs::create_dir_all(&root).unwrap();
    assert!(root.as_os_str().encode_wide().count() > 260);
    let first = root.join("첫번째-큰파일.bin");
    let second = root.join("두번째-큰파일.bin");
    let left = vec![17_u8; SIZE];
    let right = vec![93_u8; SIZE];
    fs::write(&first, &left).unwrap();
    fs::write(&second, &right).unwrap();
    assert_ne!(native_identity(&first), native_identity(&second));
    let scan = Fixture::scan(&root);
    let analysis = core::analyze_duplicates(&scan, &AtomicBool::new(false), None).unwrap();
    assert!(analysis.complete && analysis.warnings.is_empty());
    assert!(analysis.groups.is_empty());
    assert_eq!(analysis.metrics.sampled_files, 2);
    assert_eq!(analysis.metrics.sample_bytes, 2 * 3 * SAMPLE_BLOCK);
    assert_eq!(analysis.metrics.full_hash_files, 0);
    assert_eq!(analysis.metrics.full_hash_bytes, 0);
    assert_eq!(fs::read(first).unwrap(), left);
    assert_eq!(fs::read(second).unwrap(), right);
}

#[test]
fn native_same_mtime_replacement_after_sampling_warns_and_retains_the_healthy_group() {
    let fixture = Fixture::new();
    let first = fixture.0.join("a-정상원본.bin");
    let second = fixture.0.join("b-정상사본.bin");
    let other = fixture.0.join("c-다른파일.bin");
    let victim = fixture.0.join("z-교체대상.bin");
    for path in [&first, &second] {
        fs::write(path, vec![31_u8; SIZE]).unwrap();
    }
    fs::write(&other, vec![61_u8; SIZE]).unwrap();
    fs::write(&victim, vec![91_u8; SIZE]).unwrap();
    let original_id = native_identity(&victim);
    let held_original = core::safe_open(&victim).unwrap();
    let original_mtime = fs::metadata(&victim).unwrap().modified().unwrap();
    let saved_original = fixture.0.join("원래파일-보존.bin");
    let scan = Fixture::scan(&fixture.0);
    let replaced = AtomicBool::new(false);
    let callback = |progress: core::Progress| {
        if progress.phase == "duplicates_sample"
            && progress.completed == 4
            && !replaced.swap(true, Ordering::Relaxed)
        {
            // Windows' held file ID remains alive while the same pathname now
            // contains a different file with identical size and restored mtime.
            fs::rename(&victim, &saved_original).unwrap();
            fs::write(&victim, vec![121_u8; SIZE]).unwrap();
            OpenOptions::new()
                .write(true)
                .open(&victim)
                .unwrap()
                .set_times(FileTimes::new().set_modified(original_mtime))
                .unwrap();
        }
    };
    let analysis =
        core::analyze_duplicates(&scan, &AtomicBool::new(false), Some(&callback)).unwrap();
    assert!(replaced.load(Ordering::Relaxed));
    assert!(!analysis.complete);
    assert_eq!(analysis.groups.len(), 1);
    assert_eq!(
        analysis.groups[0].paths,
        vec![first.clone(), second.clone()]
    );
    core::validate_group(&analysis.groups[0]).unwrap();
    assert!(analysis
        .warnings
        .iter()
        .any(|warning| warning.path == victim && warning.phase == "final_snapshot"));
    assert_eq!(analysis.metrics.full_hash_files, 2);
    assert_ne!(native_identity(&victim), original_id);
    assert_eq!(native_identity(&saved_original), original_id);
    assert_eq!(
        fs::metadata(&victim).unwrap().modified().unwrap(),
        original_mtime
    );
    assert_eq!(fs::read(&victim).unwrap(), vec![121_u8; SIZE]);
    assert_eq!(fs::read(saved_original).unwrap(), vec![91_u8; SIZE]);
    drop(held_original);
}

#[test]
fn native_cancel_after_first_copy_publication_preserves_a_later_external_hardlink_on_undo() {
    let fixture = Fixture::new();
    let root = fixture.0.join("정리대상");
    let destination = fixture.0.join("복사대상");
    fs::create_dir(&root).unwrap();
    fs::create_dir(&destination).unwrap();
    let first = root.join("첫번째.txt");
    let second = root.join("두번째.txt");
    fs::write(&first, b"first verified copy").unwrap();
    fs::write(&second, b"later user hardlink remains user owned").unwrap();
    let scan = Fixture::scan(&root);
    let plan = operations::plan_files(
        &scan,
        &[first.clone(), second.clone()],
        Some(&destination),
        TransferKind::Copy,
    )
    .unwrap();
    let cancel = AtomicBool::new(false);
    let callback = |progress: core::Progress| {
        if progress.phase == "execute" && progress.completed == 1 {
            cancel.store(true, Ordering::Relaxed);
        }
    };
    let receipt = operations::execute(&plan, &cancel, Some(&callback)).unwrap();
    assert!(receipt.cancelled && !receipt.completed);
    assert!(receipt.items[0].target.exists());
    assert!(!receipt.items[1].target.exists());
    let later_target = receipt.items[1].target.clone();
    fs::hard_link(&second, &later_target).unwrap();
    assert_eq!(native_identity(&second), native_identity(&later_target));
    let before = core::snapshot(&later_target).unwrap();
    cancel.store(false, Ordering::Relaxed);
    let restored = operations::undo(&scan.root, &receipt.id, &cancel, None).unwrap();
    assert!(restored.completed, "{:?}", restored.errors);
    assert!(!receipt.items[0].target.exists());
    assert_eq!(core::snapshot(&later_target).unwrap(), before);
    assert_eq!(
        fs::read(&later_target).unwrap(),
        b"later user hardlink remains user owned"
    );
    assert_eq!(fs::read(first).unwrap(), b"first verified copy");
    assert_eq!(
        fs::read(second).unwrap(),
        b"later user hardlink remains user owned"
    );
}
