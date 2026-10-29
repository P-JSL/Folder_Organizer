#![cfg(windows)]
//! Targeted regressions for the 0.6.0 Windows file-ID compatibility hotfix.
use folder_organizer_rust::core::{self, ScanOptions};
use std::{
    fs::{self, FileTimes, OpenOptions},
    os::windows::io::AsRawHandle,
    path::PathBuf,
    sync::atomic::AtomicBool,
};
use windows_sys::Win32::Storage::FileSystem::{
    FileBasicInfo, FileIdInfo, GetFileInformationByHandle, GetFileInformationByHandleEx,
    BY_HANDLE_FILE_INFORMATION, FILE_BASIC_INFO, FILE_ID_INFO,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let mut random = [0; 16];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
        let path = std::env::temp_dir().join(format!("organizer-identity-v6-{id}"));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn native_snapshot_keeps_supported_128_bit_id_and_real_change_time() {
    let fixture = Fixture::new();
    let path = fixture.0.join("파일 식별.txt");
    fs::write(&path, b"native ID compatibility").unwrap();
    let file = core::safe_open(&path).unwrap();
    let actual = core::snapshot_open_file(&file).unwrap();
    let handle = file.as_raw_handle() as _;
    let mut extended: FILE_ID_INFO = unsafe { std::mem::zeroed() };
    let supported = unsafe {
        GetFileInformationByHandleEx(
            handle,
            FileIdInfo,
            &mut extended as *mut _ as _,
            std::mem::size_of_val(&extended) as u32,
        )
    } != 0;
    let expected = if supported {
        (
            extended.VolumeSerialNumber,
            u128::from_le_bytes(extended.FileId.Identifier),
        )
    } else {
        let error = std::io::Error::last_os_error();
        assert!(matches!(
            error.raw_os_error(),
            Some(1 | 50 | 87 | 120 | 124)
        ));
        let mut legacy: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
        assert_ne!(
            unsafe { GetFileInformationByHandle(handle, &mut legacy) },
            0
        );
        (
            u64::from(legacy.dwVolumeSerialNumber),
            u128::from(u64::from(legacy.nFileIndexHigh) << 32 | u64::from(legacy.nFileIndexLow)),
        )
    };
    assert_eq!((actual.device, actual.inode), expected);
    let mut basic: FILE_BASIC_INFO = unsafe { std::mem::zeroed() };
    assert_ne!(
        unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileBasicInfo,
                &mut basic as *mut _ as _,
                std::mem::size_of_val(&basic) as u32,
            )
        },
        0
    );
    assert!(basic.ChangeTime > 0);
    assert_eq!(actual.changed_ns, i128::from(basic.ChangeTime) * 100);
}

#[test]
fn same_size_and_mtime_replacement_cannot_reuse_authenticated_duplicate_proof() {
    let fixture = Fixture::new();
    let first = fixture.0.join("a.txt");
    let second = fixture.0.join("b.txt");
    for path in [&first, &second] {
        fs::write(path, b"verified duplicate content").unwrap();
    }
    let cancel = AtomicBool::new(false);
    let scan = core::scan(&fixture.0, &ScanOptions::default(), &cancel, None).unwrap();
    let groups = core::find_duplicates(&scan, &cancel, None).unwrap();
    assert_eq!(groups.len(), 1);
    let victim = &groups[0].paths[1];
    let before = core::snapshot(victim).unwrap();
    let modified = fs::metadata(victim).unwrap().modified().unwrap();
    // Keep the original alive so the provider cannot recycle its file ID.
    fs::rename(victim, fixture.0.join("held original.txt")).unwrap();
    fs::write(victim, b"verified duplicate content").unwrap();
    OpenOptions::new()
        .write(true)
        .open(victim)
        .unwrap()
        .set_times(FileTimes::new().set_modified(modified))
        .unwrap();
    let after = core::snapshot(victim).unwrap();
    assert_eq!(before.size, after.size);
    assert_eq!(before.modified_ns, after.modified_ns);
    assert_ne!((before.device, before.inode), (after.device, after.inode));
    assert!(core::validate_group(&groups[0]).is_err());
    assert_eq!(fs::read(victim).unwrap(), b"verified duplicate content");
    assert!(!fixture.0.join("delete_files").exists());
}
