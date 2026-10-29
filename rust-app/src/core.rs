//! Streaming inventory and authenticated, byte-verified exact duplicates.
//!
//! Inventory is read-only. A duplicate proof is process-local, never exported,
//! and cannot be restored from a report. File changes invalidate its use.
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::{self, File, Metadata, OpenOptions};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub type AppResult<T> = Result<T, String>;
const BUFFER: usize = 256 * 1024;
const CONTENT_LIMIT: u64 = 4 * 1024 * 1024;
const CONTENT_BUDGET: usize = 16 * 1024 * 1024;
type ProofMac = Hmac<Sha256>;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct FileSnapshot {
    pub size: u64,
    pub modified_ns: i128,
    pub changed_ns: i128,
    pub device: u64,
    pub inode: u128,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct FileEntry {
    pub path: PathBuf,
    pub relative_path: String,
    pub name: String,
    pub size: u64,
    pub modified_ns: i128,
    pub created_ns: Option<i128>,
    pub kind: String,
    pub extension: String,
    pub identity: Option<FileSnapshot>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ScanOptions {
    pub recursive: bool,
    pub include_hidden: bool,
    pub exclude_patterns: Vec<String>,
    pub min_size: u64,
    pub max_size: Option<u64>,
}

impl Default for ScanOptions {
    fn default() -> Self {
        Self {
            recursive: true,
            include_hidden: false,
            exclude_patterns: vec![],
            min_size: 0,
            max_size: None,
        }
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ScanMetrics {
    pub visited_entries: u64,
    pub metadata_reads: u64,
    pub directories: u64,
    pub filtered_files: u64,
    pub elapsed_ms: u128,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ScanResult {
    pub root: PathBuf,
    pub files: Vec<FileEntry>,
    pub warnings: Vec<String>,
    pub cancelled: bool,
    pub metrics: ScanMetrics,
}

impl ScanResult {
    pub fn total_size(&self) -> u64 {
        self.files
            .iter()
            .fold(0_u64, |total, file| total.saturating_add(file.size))
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Progress {
    pub phase: String,
    pub completed: u64,
    pub total: Option<u64>,
    pub message: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DuplicateGroup {
    pub paths: Vec<PathBuf>,
    pub size: u64,
    pub sha256: String,
    #[serde(skip)]
    snapshots: Vec<FileSnapshot>,
    #[serde(skip)]
    proof: Option<Vec<u8>>,
}

pub fn time_ns(value: SystemTime) -> i128 {
    match value.duration_since(UNIX_EPOCH) {
        Ok(duration) => duration.as_nanos() as i128,
        Err(error) => -(error.duration().as_nanos() as i128),
    }
}

/// Recreate a timestamp without clamping pre-1970 values or losing fractions.
pub fn time_from_ns(value: i128) -> AppResult<SystemTime> {
    let magnitude = value.unsigned_abs();
    let seconds =
        u64::try_from(magnitude / 1_000_000_000).map_err(|_| "날짜 범위를 초과했습니다.")?;
    let duration = Duration::new(seconds, (magnitude % 1_000_000_000) as u32);
    if value < 0 {
        UNIX_EPOCH.checked_sub(duration)
    } else {
        UNIX_EPOCH.checked_add(duration)
    }
    .ok_or_else(|| "운영체제가 지원하는 날짜 범위를 초과했습니다.".into())
}

pub fn is_link_or_reparse(metadata: &Metadata) -> bool {
    if metadata.file_type().is_symlink() {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        return metadata.file_attributes() & 0x400 != 0;
    }
    #[cfg(not(windows))]
    false
}

fn hidden(name: &str, metadata: &Metadata) -> bool {
    if name.starts_with('.') {
        return true;
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        return metadata.file_attributes() & 2 != 0;
    }
    #[cfg(not(windows))]
    {
        let _ = metadata;
        false
    }
}

/// Open one ordinary file without following a final-component link.
/// Mutation code also verifies every parent and its operation-root boundary.
pub fn safe_open(path: &Path) -> AppResult<File> {
    let before =
        fs::symlink_metadata(path).map_err(|error| format!("{}: {error}", path.display()))?;
    if !before.is_file() || is_link_or_reparse(&before) {
        return Err(format!(
            "일반 파일이 아니거나 링크입니다: {}",
            path.display()
        ));
    }
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        // OPEN_REPARSE_POINT prevents a raced-in link from being followed.
        options.custom_flags(0x0020_0000 | 0x0800_0000);
    }
    let file = options
        .open(path)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    let opened = file.metadata().map_err(|error| error.to_string())?;
    if !opened.is_file() || is_link_or_reparse(&opened) {
        return Err(format!(
            "읽는 동안 링크 또는 특수 파일로 바뀌었습니다: {}",
            path.display()
        ));
    }
    Ok(file)
}

/// Some SMB/NAS and older filesystem providers implement the original handle
/// identity query but reject FileIdInfo (the newer 128-bit information class).
/// Fall back only for an explicitly unsupported information class, never for
/// access/handle/I/O errors. Both queries refer to the same already-open file;
/// a path hash, creation date or truncated 128-bit ID is not a file identity.
#[cfg(any(windows, test))]
fn windows_file_identity(
    extended: impl FnOnce() -> std::io::Result<(u64, u128)>,
    legacy: impl FnOnce() -> std::io::Result<(u64, u64)>,
) -> AppResult<(u64, u128)> {
    let identity = match extended() {
        Ok(identity) => identity,
        Err(error) if matches!(error.raw_os_error(), Some(1 | 50 | 87 | 120 | 124)) => {
            let (device, inode) = legacy().map_err(|fallback| {
                format!(
                    "이 위치의 Windows 파일 ID를 읽지 못했습니다. 확장 ID 조회: {error}; 호환 ID 조회: {fallback}"
                )
            })?;
            (device, u128::from(inode))
        }
        Err(error) => {
            return Err(format!(
                "Windows 파일 ID를 읽지 못했습니다 (FileIdInfo): {error}"
            ));
        }
    };
    if identity.1 == 0 {
        return Err(
            "이 위치가 안정적인 Windows 파일 ID를 제공하지 않아 파일 작업을 중단했습니다.".into(),
        );
    }
    Ok(identity)
}

#[cfg(any(windows, test))]
fn windows_legacy_identity_filesystem(name: &str) -> std::io::Result<()> {
    // ReFS documents that its 64-bit compatibility file indices need not be
    // unique; it must keep the complete FileIdInfo identity instead.
    if name.trim().is_empty() || name.trim().eq_ignore_ascii_case("ReFS") {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "파일시스템이 고유한 64비트 파일 ID를 보장하지 않아 호환 조회를 사용할 수 없습니다.",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn windows_legacy_file_identity(file: &File) -> std::io::Result<(u64, u64)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, GetVolumeInformationByHandleW, BY_HANDLE_FILE_INFORMATION,
    };
    let handle = file.as_raw_handle() as _;
    let mut name = [0_u16; 64];
    // Query the actual opened volume, never a drive letter/path (which can
    // resolve to another provider during a mount or network reconnect).
    if unsafe {
        GetVolumeInformationByHandleW(
            handle,
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            name.as_mut_ptr(),
            name.len() as u32,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    let end = name.iter().position(|c| *c == 0).ok_or_else(|| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "파일시스템 이름이 너무 깁니다.",
        )
    })?;
    let name = String::from_utf16(&name[..end])
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
    windows_legacy_identity_filesystem(&name)?;
    let mut identity: BY_HANDLE_FILE_INFORMATION = unsafe { std::mem::zeroed() };
    if unsafe { GetFileInformationByHandle(handle, &mut identity) } == 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok((
        u64::from(identity.dwVolumeSerialNumber),
        u64::from(identity.nFileIndexHigh) << 32 | u64::from(identity.nFileIndexLow),
    ))
}

pub fn snapshot_open_file(file: &File) -> AppResult<FileSnapshot> {
    let metadata = file.metadata().map_err(|error| error.to_string())?;
    if !metadata.is_file() || is_link_or_reparse(&metadata) {
        return Err("일반 파일의 정보만 사용할 수 있습니다.".into());
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        return Ok(FileSnapshot {
            size: metadata.len(),
            modified_ns: time_ns(metadata.modified().map_err(|error| error.to_string())?),
            changed_ns: metadata.ctime() as i128 * 1_000_000_000 + metadata.ctime_nsec() as i128,
            device: metadata.dev(),
            inode: metadata.ino() as u128,
        });
    }
    #[cfg(windows)]
    {
        use std::os::windows::io::AsRawHandle;
        use windows_sys::Win32::Storage::FileSystem::{
            FileBasicInfo, FileIdInfo, GetFileInformationByHandleEx, FILE_BASIC_INFO, FILE_ID_INFO,
        };
        let handle = file.as_raw_handle() as _;
        let (device, inode) = windows_file_identity(
            || {
                let mut identity: FILE_ID_INFO = unsafe { std::mem::zeroed() };
                if unsafe {
                    GetFileInformationByHandleEx(
                        handle,
                        FileIdInfo,
                        &mut identity as *mut _ as _,
                        std::mem::size_of::<FILE_ID_INFO>() as u32,
                    )
                } == 0
                {
                    return Err(std::io::Error::last_os_error());
                }
                Ok((
                    identity.VolumeSerialNumber,
                    u128::from_le_bytes(identity.FileId.Identifier),
                ))
            },
            || windows_legacy_file_identity(file),
        )?;
        let mut basic: FILE_BASIC_INFO = unsafe { std::mem::zeroed() };
        let success = unsafe {
            GetFileInformationByHandleEx(
                handle,
                FileBasicInfo,
                &mut basic as *mut _ as _,
                std::mem::size_of::<FILE_BASIC_INFO>() as u32,
            ) != 0
        };
        if !success {
            return Err(format!(
                "Windows 변경 시각을 읽지 못했습니다 (FileBasicInfo). 안전하게 검사하려면 ChangeTime을 지원하는 위치가 필요합니다: {}",
                std::io::Error::last_os_error()
            ));
        }
        if basic.ChangeTime <= 0 {
            return Err(
                "이 위치가 Windows 변경 시각(ChangeTime)을 제공하지 않아 파일 작업을 중단했습니다."
                    .into(),
            );
        }
        return Ok(FileSnapshot {
            size: metadata.len(),
            modified_ns: time_ns(metadata.modified().map_err(|error| error.to_string())?),
            changed_ns: basic.ChangeTime as i128 * 100,
            device,
            inode,
        });
    }
    #[cfg(not(any(unix, windows)))]
    Err("이 운영체제에서는 안정적인 파일 식별을 지원하지 않습니다.".into())
}

pub fn snapshot(path: &Path) -> AppResult<FileSnapshot> {
    snapshot_open_file(&safe_open(path)?).map_err(|error| format!("{}: {error}", path.display()))
}

fn inventory_identity(metadata: &Metadata) -> Option<FileSnapshot> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        return Some(FileSnapshot {
            size: metadata.len(),
            modified_ns: time_ns(metadata.modified().ok()?),
            changed_ns: metadata.ctime() as i128 * 1_000_000_000 + metadata.ctime_nsec() as i128,
            device: metadata.dev(),
            inode: metadata.ino() as u128,
        });
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        None
    }
}

fn wildcard(pattern: &str, value: &str) -> bool {
    let pattern: Vec<char> = pattern.chars().collect();
    let value: Vec<char> = value.chars().collect();
    let (mut p, mut v, mut star, mut retry) = (0, 0, None, 0);
    while v < value.len() {
        if p < pattern.len() && (pattern[p] == '?' || pattern[p] == value[v]) {
            p += 1;
            v += 1;
        } else if p < pattern.len() && pattern[p] == '*' {
            star = Some(p);
            p += 1;
            retry = v;
        } else if let Some(position) = star {
            retry += 1;
            v = retry;
            p = position + 1;
        } else {
            return false;
        }
    }
    while p < pattern.len() && pattern[p] == '*' {
        p += 1;
    }
    p == pattern.len()
}

fn excluded(relative: &str, patterns: &[String]) -> bool {
    if patterns.is_empty() {
        return false;
    }
    let value = relative.replace('\\', "/").to_lowercase();
    patterns.iter().any(|pattern| {
        if pattern.contains('/') {
            wildcard(pattern, &value) || wildcard(pattern, &(value.clone() + "/"))
        } else {
            value.split('/').any(|part| wildcard(pattern, part))
        }
    })
}

fn notify(
    progress: Option<&dyn Fn(Progress)>,
    phase: &str,
    completed: u64,
    total: Option<u64>,
    message: String,
) {
    if let Some(callback) = progress {
        callback(Progress {
            phase: phase.into(),
            completed,
            total,
            message,
        });
    }
}

pub fn scan(
    root: &Path,
    options: &ScanOptions,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<ScanResult> {
    if options.exclude_patterns.len() > 100
        || options
            .max_size
            .is_some_and(|maximum| maximum < options.min_size)
    {
        return Err("제외 규칙 또는 파일 크기 범위가 올바르지 않습니다.".into());
    }
    let mut patterns = Vec::new();
    for pattern in &options.exclude_patterns {
        let pattern = pattern.trim().replace('\\', "/").to_lowercase();
        if pattern.len() > 512
            || pattern.contains('\0')
            || pattern.starts_with('/')
            || pattern.contains(':')
            || pattern.split('/').any(|part| part == "..")
        {
            return Err("제외 규칙은 '..' 없는 상대 경로여야 합니다.".into());
        }
        if !pattern.is_empty() {
            patterns.push(pattern.trim_end_matches('/').into());
        }
    }
    let metadata = fs::symlink_metadata(root).map_err(|error| error.to_string())?;
    if !metadata.is_dir() || is_link_or_reparse(&metadata) {
        return Err("링크가 아닌 실제 폴더를 선택하세요.".into());
    }
    let root = root.canonicalize().map_err(|error| error.to_string())?;
    let started = Instant::now();
    let mut result = ScanResult {
        root: root.clone(),
        files: vec![],
        warnings: vec![],
        cancelled: false,
        metrics: ScanMetrics {
            metadata_reads: 1,
            ..ScanMetrics::default()
        },
    };
    let mut pending = vec![root.clone()];
    let mut seen = HashSet::new();
    let mut last_event = Instant::now();
    notify(
        progress,
        "scan",
        0,
        None,
        "폴더 안의 파일을 찾고 있습니다.".into(),
    );
    while let Some(directory) = pending.pop() {
        if cancel.load(Ordering::Relaxed) {
            result.cancelled = true;
            break;
        }
        let metadata = match fs::symlink_metadata(&directory) {
            Ok(value) => value,
            Err(error) => {
                result
                    .warnings
                    .push(format!("{}: {error}", directory.display()));
                continue;
            }
        };
        result.metrics.metadata_reads += 1;
        if !metadata.is_dir() || is_link_or_reparse(&metadata) {
            result.warnings.push(format!(
                "링크 또는 바뀐 폴더 건너뜀: {}",
                directory.display()
            ));
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if !seen.insert((metadata.dev(), metadata.ino())) {
                continue;
            }
        }
        #[cfg(not(unix))]
        if !seen.insert(directory.clone()) {
            continue;
        }
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error) => {
                result
                    .warnings
                    .push(format!("{}: {error}", directory.display()));
                continue;
            }
        };
        result.metrics.directories += 1;
        for child in entries {
            if cancel.load(Ordering::Relaxed) {
                result.cancelled = true;
                break;
            }
            result.metrics.visited_entries += 1;
            let child = match child {
                Ok(value) => value,
                Err(error) => {
                    result.warnings.push(error.to_string());
                    continue;
                }
            };
            let name = child.file_name().to_string_lossy().into_owned();
            let folded = name.to_lowercase();
            if [
                ".folder-organizer",
                ".folder-organizer-rust",
                "#recycle",
                "@recycle",
                "$recycle.bin",
            ]
            .contains(&folded.as_str())
                || (directory == root && folded == "delete_files")
            {
                continue;
            }
            if !options.include_hidden && name.starts_with('.') {
                continue;
            }
            let path = child.path();
            result.metrics.metadata_reads += 1;
            let metadata = match fs::symlink_metadata(&path) {
                Ok(value) => value,
                Err(error) => {
                    result.warnings.push(format!("{}: {error}", path.display()));
                    continue;
                }
            };
            if is_link_or_reparse(&metadata) {
                result
                    .warnings
                    .push(format!("링크 건너뜀: {}", path.display()));
                continue;
            }
            if !options.include_hidden && hidden(&name, &metadata) {
                continue;
            }
            let relative = path
                .strip_prefix(&root)
                .map_err(|error| error.to_string())?
                .to_string_lossy()
                .into_owned();
            if excluded(&relative, &patterns) {
                result.metrics.filtered_files += 1;
                continue;
            }
            if metadata.is_dir() {
                if options.recursive {
                    pending.push(path);
                }
                continue;
            }
            if !metadata.is_file() {
                result
                    .warnings
                    .push(format!("특수 파일 건너뜀: {}", path.display()));
                continue;
            }
            if metadata.len() < options.min_size
                || options
                    .max_size
                    .is_some_and(|maximum| metadata.len() > maximum)
            {
                result.metrics.filtered_files += 1;
                continue;
            }
            let modified = match metadata.modified() {
                Ok(value) => value,
                Err(error) => {
                    result
                        .warnings
                        .push(format!("수정 시각 읽기 실패 {}: {error}", path.display()));
                    continue;
                }
            };
            let extension = path
                .extension()
                .map(|value| format!(".{}", value.to_string_lossy().to_lowercase()))
                .unwrap_or_default();
            let application = [
                ".exe",
                ".msi",
                ".msix",
                ".appx",
                ".bat",
                ".cmd",
                ".com",
                ".dmg",
                ".pkg",
                ".deb",
                ".rpm",
                ".appimage",
                ".desktop",
            ]
            .contains(&extension.as_str())
                || path.components().any(|part| {
                    part.as_os_str()
                        .to_string_lossy()
                        .to_lowercase()
                        .ends_with(".app")
                });
            result.files.push(FileEntry {
                path,
                relative_path: relative.clone(),
                name,
                size: metadata.len(),
                modified_ns: time_ns(modified),
                created_ns: metadata.created().ok().map(time_ns),
                kind: if application { "application" } else { "file" }.into(),
                extension,
                identity: inventory_identity(&metadata),
            });
            if result.files.len() <= 10
                || (result.files.len() % 100 == 0 && last_event.elapsed().as_millis() >= 70)
            {
                notify(progress, "scan", result.files.len() as u64, None, relative);
                last_event = Instant::now();
            }
        }
        if result.cancelled {
            break;
        }
    }
    result.metrics.elapsed_ms = started.elapsed().as_millis();
    notify(
        progress,
        "scan",
        result.files.len() as u64,
        if result.cancelled {
            None
        } else {
            Some(result.files.len() as u64)
        },
        if result.cancelled {
            "분석을 취소했습니다."
        } else {
            "파일 목록을 준비했습니다."
        }
        .into(),
    );
    Ok(result)
}

fn proof_key() -> AppResult<&'static [u8; 32]> {
    static KEY: OnceLock<Result<[u8; 32], String>> = OnceLock::new();
    KEY.get_or_init(|| {
        let mut key = [0_u8; 32];
        getrandom::getrandom(&mut key).map_err(|error| error.to_string())?;
        Ok(key)
    })
    .as_ref()
    .map_err(Clone::clone)
}

fn proof_data(group: &DuplicateGroup) -> AppResult<Vec<u8>> {
    serde_json::to_vec(&(&group.paths, group.size, &group.sha256, &group.snapshots))
        .map_err(|error| error.to_string())
}

fn seal(group: &mut DuplicateGroup) -> AppResult<()> {
    let mut mac = ProofMac::new_from_slice(proof_key()?).map_err(|error| error.to_string())?;
    mac.update(&proof_data(group)?);
    group.proof = Some(mac.finalize().into_bytes().to_vec());
    Ok(())
}

pub fn group_snapshot<'a>(group: &'a DuplicateGroup, path: &Path) -> Option<&'a FileSnapshot> {
    group
        .paths
        .iter()
        .position(|candidate| candidate == path)
        .and_then(|index| group.snapshots.get(index))
}

/// Authenticate cached evidence and inspect current metadata only. No contents
/// are read here; callers must not rehash or silently rebuild a stale group.
pub fn validate_group(group: &DuplicateGroup) -> AppResult<()> {
    if group.paths.len() < 2 || group.paths.len() != group.snapshots.len() {
        return Err("이 그룹은 현재 실행에서 검증한 중복 결과가 아닙니다. 다시 분석하세요.".into());
    }
    let proof = group
        .proof
        .as_ref()
        .ok_or("내보낸 보고서는 삭제 근거로 사용할 수 없습니다.")?;
    let mut mac = ProofMac::new_from_slice(proof_key()?).map_err(|error| error.to_string())?;
    mac.update(&proof_data(group)?);
    mac.verify_slice(proof)
        .map_err(|_| "중복 결과가 변경되었습니다. 다시 분석하세요.".to_string())?;
    for (path, expected) in group.paths.iter().zip(&group.snapshots) {
        if &snapshot(path)? != expected {
            return Err(format!("검사 후 파일이 바뀌었습니다: {}", path.display()));
        }
    }
    Ok(())
}

struct Fingerprint {
    snapshot: FileSnapshot,
    digest: String,
    content: Option<Vec<u8>>,
}

fn fingerprint_inner(
    path: &Path,
    keep_content: bool,
    cancel: Option<&AtomicBool>,
) -> AppResult<Fingerprint> {
    let mut file = safe_open(path)?;
    let before = snapshot_open_file(&file)?;
    let mut hash = Sha256::new();
    let mut content = if keep_content && before.size <= CONTENT_LIMIT {
        Some(Vec::with_capacity(before.size as usize))
    } else {
        None
    };
    let mut buffer = vec![0_u8; BUFFER];
    let mut read = 0_u64;
    loop {
        if cancel.is_some_and(|value| value.load(Ordering::Relaxed)) {
            return Err("중복 검사를 취소했습니다.".into());
        }
        let count = file.read(&mut buffer).map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        read = read.saturating_add(count as u64);
        if read > before.size {
            return Err(format!("읽는 동안 크기가 바뀌었습니다: {}", path.display()));
        }
        hash.update(&buffer[..count]);
        if let Some(content) = &mut content {
            content.extend_from_slice(&buffer[..count]);
        }
    }
    if read != before.size || snapshot_open_file(&file)? != before || snapshot(path)? != before {
        return Err(format!("읽는 동안 파일이 바뀌었습니다: {}", path.display()));
    }
    Ok(Fingerprint {
        snapshot: before,
        digest: format!("{:x}", hash.finalize()),
        content,
    })
}

pub fn fingerprint(path: &Path) -> AppResult<(FileSnapshot, String)> {
    let value = fingerprint_inner(path, false, None)?;
    Ok((value.snapshot, value.digest))
}

fn bytes_equal(
    left: &Path,
    right: &Path,
    a: &FileSnapshot,
    b: &FileSnapshot,
    cancel: &AtomicBool,
) -> AppResult<bool> {
    let mut left_file = safe_open(left)?;
    let mut right_file = safe_open(right)?;
    if &snapshot_open_file(&left_file)? != a || &snapshot_open_file(&right_file)? != b {
        return Err("비교할 파일이 바뀌었습니다.".into());
    }
    let mut left_buffer = vec![0_u8; BUFFER];
    let mut right_buffer = vec![0_u8; BUFFER];
    let mut equal = true;
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("중복 검사를 취소했습니다.".into());
        }
        let na = left_file
            .read(&mut left_buffer)
            .map_err(|error| error.to_string())?;
        let nb = right_file
            .read(&mut right_buffer)
            .map_err(|error| error.to_string())?;
        if na != nb || left_buffer[..na] != right_buffer[..nb] {
            equal = false;
            break;
        }
        if na == 0 {
            break;
        }
    }
    if &snapshot_open_file(&left_file)? != a
        || &snapshot_open_file(&right_file)? != b
        || &snapshot(left)? != a
        || &snapshot(right)? != b
    {
        return Err("내용 비교 중 파일이 바뀌었습니다.".into());
    }
    Ok(equal)
}

pub fn find_duplicates(
    scan: &ScanResult,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<Vec<DuplicateGroup>> {
    if scan.cancelled {
        return Err("완료되지 않은 목록으로 중복을 정리할 수 없습니다.".into());
    }
    let mut sizes: HashMap<u64, Vec<&FileEntry>> = HashMap::new();
    for file in &scan.files {
        sizes.entry(file.size).or_default().push(file);
    }
    let total: usize = sizes
        .values()
        .filter(|files| files.len() > 1)
        .map(Vec::len)
        .sum();
    let mut completed = 0_u64;
    let mut last_progress = Instant::now();
    let mut groups = Vec::new();
    let mut aliases = HashSet::new();
    for files in sizes.values().filter(|files| files.len() > 1) {
        struct Bucket {
            members: Vec<(PathBuf, FileSnapshot)>,
            content: Option<Vec<u8>>,
        }
        let mut hashes: HashMap<String, Vec<Bucket>> = HashMap::new();
        let mut cached: usize = 0;
        for entry in files {
            if cancel.load(Ordering::Relaxed) {
                return Err("중복 검사를 취소했습니다.".into());
            }
            // Capture identity before content reads and keep that exact version
            // throughout hashing, byte verification and proof generation.
            let current = snapshot(&entry.path)?;
            if current.inode == 0 {
                return Err(format!(
                    "안정적인 파일 식별 번호를 제공하지 않는 위치입니다: {}",
                    entry.path.display()
                ));
            }
            if current.size != entry.size
                || current.modified_ns != entry.modified_ns
                || entry
                    .identity
                    .as_ref()
                    .is_some_and(|value| value != &current)
            {
                return Err(format!(
                    "목록 분석 후 파일이 바뀌었습니다: {}",
                    entry.path.display()
                ));
            }
            if current.inode != 0 && !aliases.insert((current.device, current.inode)) {
                completed += 1;
                continue;
            }
            let fingerprint = fingerprint_inner(&entry.path, true, Some(cancel))?;
            if fingerprint.snapshot != current {
                return Err("해시 검사 직전에 파일이 바뀌었습니다.".into());
            }
            let buckets = hashes.entry(fingerprint.digest).or_default();
            let mut matched = false;
            for bucket in buckets.iter_mut() {
                let (anchor, anchor_snapshot) = &bucket.members[0];
                let equal = match (&bucket.content, &fingerprint.content) {
                    (Some(left), Some(right)) => left == right,
                    _ => bytes_equal(
                        anchor,
                        &entry.path,
                        anchor_snapshot,
                        &fingerprint.snapshot,
                        cancel,
                    )?,
                };
                if equal {
                    if snapshot(anchor)? != *anchor_snapshot
                        || snapshot(&entry.path)? != fingerprint.snapshot
                    {
                        return Err("중복 검증 후 파일이 바뀌었습니다.".into());
                    }
                    bucket
                        .members
                        .push((entry.path.clone(), fingerprint.snapshot.clone()));
                    matched = true;
                    break;
                }
            }
            if !matched {
                let content = fingerprint
                    .content
                    .filter(|value| cached.saturating_add(value.len()) <= CONTENT_BUDGET);
                cached += content.as_ref().map(Vec::len).unwrap_or(0);
                buckets.push(Bucket {
                    members: vec![(entry.path.clone(), fingerprint.snapshot)],
                    content,
                });
            }
            completed += 1;
            if completed <= 10 || completed % 64 == 0 || last_progress.elapsed().as_millis() >= 70 {
                notify(
                    progress,
                    "duplicates",
                    completed,
                    Some(total as u64),
                    entry.relative_path.clone(),
                );
                last_progress = Instant::now();
            }
        }
        for (digest, buckets) in hashes {
            for mut bucket in buckets
                .into_iter()
                .filter(|bucket| bucket.members.len() > 1)
            {
                bucket.members.sort_by(|left, right| left.0.cmp(&right.0));
                for (path, expected) in &bucket.members {
                    if &snapshot(path)? != expected {
                        return Err("결과를 준비하는 동안 파일이 바뀌었습니다.".into());
                    }
                }
                let (paths, snapshots): (Vec<_>, Vec<_>) = bucket.members.into_iter().unzip();
                let mut group = DuplicateGroup {
                    size: snapshots[0].size,
                    paths,
                    sha256: digest.clone(),
                    snapshots,
                    proof: None,
                };
                seal(&mut group)?;
                groups.push(group);
            }
        }
    }
    groups.sort_by(|left, right| left.paths[0].cmp(&right.paths[0]));
    notify(
        progress,
        "duplicates",
        total as u64,
        Some(total as u64),
        "내용이 완전히 같은 파일을 확인했습니다.".into(),
    );
    Ok(groups)
}

#[cfg(test)]
mod windows_identity_compat_tests {
    use super::windows_file_identity;
    use std::io;

    #[test]
    fn windows_identity_compat_preserves_all_128_bits_when_supported() {
        let id = (1_u128 << 123) | 0xfedc_ba98_7654_3210;
        assert_eq!(
            windows_file_identity(|| Ok((u64::MAX, id)), || panic!("no legacy query needed"))
                .unwrap(),
            (u64::MAX, id)
        );
    }

    #[test]
    fn windows_identity_compat_falls_back_for_unsupported_classes_only() {
        for code in [1, 50, 87, 120, 124] {
            assert_eq!(
                windows_file_identity(
                    || Err(io::Error::from_raw_os_error(code)),
                    || Ok((0xabcd_1234, 0xfedc_ba98_7654_3210)),
                )
                .unwrap(),
                (0xabcd_1234, 0xfedc_ba98_7654_3210)
            );
        }
    }

    #[test]
    fn windows_identity_compat_does_not_hide_access_handle_or_io_failures() {
        for code in [5, 6, 32, 1117] {
            let error = windows_file_identity(
                || Err(io::Error::from_raw_os_error(code)),
                || panic!("an operational failure must not downgrade identity"),
            )
            .unwrap_err();
            assert!(error.contains("FileIdInfo"));
        }
    }

    #[test]
    fn windows_identity_compat_reports_both_failed_queries() {
        let error = windows_file_identity(
            || Err(io::Error::from_raw_os_error(87)),
            || Err(io::Error::from_raw_os_error(5)),
        )
        .unwrap_err();
        assert!(error.contains("확장 ID 조회") && error.contains("호환 ID 조회"));
    }

    #[test]
    fn windows_identity_compat_rejects_missing_stable_identity() {
        assert!(windows_file_identity(|| Ok((7, 0)), || panic!("not unsupported")).is_err());
        assert!(
            windows_file_identity(|| Err(io::Error::from_raw_os_error(87)), || Ok((7, 0))).is_err()
        );
    }

    #[test]
    fn windows_identity_compat_filesystem_guard_refuses_refs_or_unknown_volume() {
        for name in ["ReFS", "REFS", "refs", " ReFS ", "", "   "] {
            assert!(super::windows_legacy_identity_filesystem(name).is_err());
        }
        for name in ["NTFS", "FAT", "FAT32", "exFAT", "CIFS", "BTRFS"] {
            assert!(super::windows_legacy_identity_filesystem(name).is_ok());
        }
    }

    #[cfg(windows)]
    #[test]
    fn windows_identity_compat_fallback_uses_real_held_handle_after_path_replacement() {
        use std::fs;
        struct Fixture(std::path::PathBuf);
        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = fs::remove_dir_all(&self.0);
            }
        }
        let mut random = [0_u8; 16];
        getrandom::getrandom(&mut random).unwrap();
        let id: String = random.iter().map(|n| format!("{n:02x}")).collect();
        let fixture = Fixture(std::env::temp_dir().join(format!("organizer-legacy-id-{id}")));
        fs::create_dir(&fixture.0).unwrap();
        let path = fixture.0.join("동일 경로.txt");
        fs::write(&path, b"identical content").unwrap();
        let held = super::safe_open(&path).unwrap();
        let query = |file: &std::fs::File| {
            windows_file_identity(
                || Err(io::Error::from_raw_os_error(87)),
                || super::windows_legacy_file_identity(file),
            )
            .unwrap()
        };
        let original_identity = query(&held);
        fs::rename(&path, fixture.0.join("original.txt")).unwrap();
        fs::write(&path, b"identical content").unwrap();
        let replacement = super::safe_open(&path).unwrap();
        assert_eq!(query(&held), original_identity);
        assert_ne!(query(&replacement), original_identity);
    }
}
