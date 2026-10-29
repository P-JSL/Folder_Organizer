//! Recoverable, authenticated file operations for the native Rust edition.
//!
//! Receipts use an append-only authenticated event chain: advancing one file
//! never rewrites an ever-growing operation. Publication and retirement have
//! durable intent records and exclusive renames. Ambiguous bytes are preserved.
use crate::core::{self, AppResult, DuplicateGroup, FileSnapshot, Progress, ScanResult};
use flate2::{
    read::{GzDecoder, ZlibDecoder},
    write::{GzEncoder, ZlibEncoder},
    Compression,
};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeSet, HashMap};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    OnceLock,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const APP: &str = ".folder-organizer-rust";
const MAX_FILES: usize = 25_000;
const MAX_METADATA: u64 = 128 * 1024 * 1024;
const MAX_RECIPE: u64 = 8 * 1024 * 1024;
const CHUNK: usize = 1024 * 1024;
const MAX_CHUNKS: usize = 65_536;
type HmacSha256 = Hmac<Sha256>;
#[cfg(test)]
thread_local! {
    static TEST_FAIL_SYNC:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};
    static TEST_UNDO_AFTER_RETIRE:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};
    static TEST_DURABILITY_EVENTS:std::cell::RefCell<Vec<(String,PathBuf)>>=const{std::cell::RefCell::new(Vec::new())};
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TransferKind {
    Copy,
    Move,
    Delete,
}

#[derive(Clone, Debug, Serialize)]
pub struct PlannedItem {
    pub source: PathBuf,
    pub target: PathBuf,
    pub size: u64,
    #[serde(skip)]
    snapshot: FileSnapshot,
    #[serde(skip)]
    sha256: Option<String>,
    #[serde(skip)]
    keeper: Option<(PathBuf, FileSnapshot)>,
}

#[derive(Clone, Debug, Serialize)]
pub struct OperationPlan {
    pub id: String,
    pub root: PathBuf,
    pub kind: TransferKind,
    pub items: Vec<PlannedItem>,
    pub created_at: u64,
    #[serde(skip)]
    proof: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OperationItem {
    pub source: PathBuf,
    pub target: PathBuf,
    pub size: u64,
    pub status: String,
    pub error: Option<String>,
    pub source_removed: bool,
    pub sha256: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OperationReceipt {
    pub id: String,
    pub root: PathBuf,
    pub kind: TransferKind,
    pub items: Vec<OperationItem>,
    pub errors: Vec<String>,
    pub completed: bool,
    pub cancelled: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct HistoryRecord {
    pub id: String,
    pub kind: TransferKind,
    pub status: String,
    pub created_at: u64,
    pub files: usize,
    pub bytes: u64,
    pub packed_bytes: u64,
    pub restored: bool,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CompactionResult {
    pub files: usize,
    pub original_bytes: u64,
    pub stored_bytes: u64,
    pub saved_bytes: u64,
    /// Files whose additional streams/encryption cannot be represented by the
    /// current chunk format remain raw, with an explicit explanation.
    #[serde(default)]
    pub warnings: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct LogRecord {
    pub time: u64,
    pub operation_id: String,
    pub action: String,
    pub path: String,
    pub status: String,
    pub message: String,
}

#[derive(Clone, Serialize, Deserialize)]
struct JournalItem {
    receipt: OperationItem,
    original: FileSnapshot,
    keeper: Option<(PathBuf, FileSnapshot)>,
    captured: Option<FileSnapshot>,
    staging: Option<PathBuf>,
    staged_snapshot: Option<FileSnapshot>,
    backup: Option<PathBuf>,
    backup_snapshot: Option<FileSnapshot>,
    retired_target: Option<PathBuf>,
    compact_retired: Option<PathBuf>,
    compact_snapshot: Option<FileSnapshot>,
    recipe: Option<PackedFile>,
    restored_snapshot: Option<FileSnapshot>,
}

#[derive(Clone, Serialize, Deserialize)]
struct Journal {
    schema: u8,
    id: String,
    root: PathBuf,
    kind: TransferKind,
    created_at: u64,
    items: Vec<JournalItem>,
}

#[derive(Serialize, Deserialize)]
struct Envelope<T> {
    payload: T,
    mac: String,
}

#[derive(Serialize, Deserialize)]
struct Event {
    sequence: u64,
    previous: String,
    index: usize,
    item: JournalItem,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct ChunkRef {
    sha256: String,
    raw_size: u32,
    stored_size: u32,
    compressed: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct PackedFile {
    sha256: String,
    size: u64,
    chunks: Vec<ChunkRef>,
}

struct Store {
    journal: Journal,
    directory: PathBuf,
    key: Vec<u8>,
    last_mac: String,
    sequence: u64,
    base_file: String,
    event_file: String,
    missing_raw: bool,
    poisoned: bool,
}

#[derive(Serialize, Deserialize)]
struct ChainPointer {
    id: String,
    root: PathBuf,
    base_file: String,
    event_file: String,
}
#[derive(Serialize, Deserialize)]
struct MetadataReceipt {
    id: String,
    root: PathBuf,
    sequence: u64,
    last_mac: String,
    base_file: String,
    event_file: String,
    base_sha256: String,
    event_sha256: String,
    payload_sha256: String,
    gzip_sha256: String,
    gzip_file: String,
}

fn err(context: &str, error: impl std::fmt::Display) -> String {
    format!("{context}: {error}")
}
fn now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn random_id() -> AppResult<String> {
    let mut b = [0; 16];
    getrandom::getrandom(&mut b).map_err(|e| err("난수 생성 실패", e))?;
    Ok(hex(&b))
}
fn valid_id(s: &str) -> bool {
    s.len() == 32
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn digest(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}
fn mac<T: Serialize>(key: &[u8], value: &T) -> AppResult<String> {
    let mut state = HmacSha256::new_from_slice(key).map_err(|e| err("인증 키 오류", e))?;
    state.update(&serde_json::to_vec(value).map_err(|e| err("기록 변환 실패", e))?);
    Ok(hex(&state.finalize().into_bytes()))
}
fn verify<T: Serialize>(key: &[u8], payload: &T, signature: &str) -> AppResult<()> {
    if signature.len() != 64 || !signature.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("작업 기록의 인증 정보가 올바르지 않습니다.".into());
    }
    let bytes: Result<Vec<u8>, _> = (0..64)
        .step_by(2)
        .map(|i| u8::from_str_radix(&signature[i..i + 2], 16))
        .collect();
    let bytes = bytes.map_err(|e| err("인증 정보 오류", e))?;
    let mut state = HmacSha256::new_from_slice(key).map_err(|e| err("인증 키 오류", e))?;
    state.update(&serde_json::to_vec(payload).map_err(|e| err("기록 변환 실패", e))?);
    state
        .verify_slice(&bytes)
        .map_err(|_| "작업 기록이 변경되었거나 다른 폴더의 기록입니다.".into())
}
fn plan_key() -> &'static [u8; 32] {
    static KEY: OnceLock<[u8; 32]> = OnceLock::new();
    KEY.get_or_init(|| {
        let mut k = [0; 32];
        getrandom::getrandom(&mut k).expect("secure OS randomness required");
        k
    })
}
fn plan_binding(plan: &OperationPlan) -> AppResult<Vec<u8>> {
    let guards: Vec<_> = plan
        .items
        .iter()
        .map(|i| {
            (
                &i.source,
                &i.target,
                i.size,
                &i.snapshot,
                &i.sha256,
                &i.keeper,
            )
        })
        .collect();
    serde_json::to_vec(&(&plan.id, &plan.root, plan.kind, plan.created_at, guards))
        .map_err(|e| err("계획 변환 실패", e))
}

fn is_reparse(info: &fs::Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        info.file_attributes() & 0x400 != 0
    }
    #[cfg(not(windows))]
    {
        let _ = info;
        false
    }
}
fn plain(path: &Path) -> AppResult<()> {
    let mut cursor = PathBuf::new();
    for part in path.components() {
        if matches!(part, Component::ParentDir) {
            return Err("상위 폴더 경로는 사용할 수 없습니다.".into());
        }
        cursor.push(part.as_os_str());
        // A Windows prefix is not a complete filesystem path. In particular,
        // canonical paths begin with a verbatim prefix such as `\\?\C:`,
        // which cannot be queried until RootDir completes its real anchor.
        // Every actual root and subsequent component is still checked below.
        if matches!(part, Component::Prefix(_)) {
            continue;
        }
        match fs::symlink_metadata(&cursor) {
            Ok(m) if m.file_type().is_symlink() || is_reparse(&m) => {
                return Err(format!(
                    "링크 또는 재분석 경로를 사용하지 않습니다: {}",
                    cursor.display()
                ))
            }
            Ok(_) => (),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => (),
            Err(e) => return Err(err("경로 확인 실패", e)),
        }
    }
    Ok(())
}
fn make_dirs(path: &Path) -> AppResult<()> {
    plain(path)?;
    let missing: Vec<_> = path
        .ancestors()
        .take_while(|p| !p.exists())
        .map(Path::to_path_buf)
        .collect();
    fs::create_dir_all(path).map_err(|e| err("폴더 생성 실패", e))?;
    plain(path)?;
    for directory in missing.iter().rev() {
        sync_directory(directory)?;
        if let Some(parent) = directory.parent() {
            sync_directory(parent)?;
        }
    }
    Ok(())
}
fn sync_directory(path: &Path) -> AppResult<()> {
    #[cfg(unix)]
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|e| err("복원 폴더 기록을 저장할 수 없습니다", e))?;
    #[cfg(not(unix))]
    {
        let _ = path;
    }
    #[cfg(test)]
    TEST_DURABILITY_EVENTS.with(|events| {
        events
            .borrow_mut()
            .push(("sync".into(), path.to_path_buf()))
    });
    Ok(())
}
/// Lexical Windows drive-root classification, including every drive letter and
/// verbatim form. It intentionally performs no drive/network access.
pub fn is_local_drive_root(path: &Path) -> bool {
    #[cfg(windows)]
    {
        use std::path::Prefix;
        let mut parts = path.components();
        let disk = matches!(parts.next(),Some(Component::Prefix(p)) if matches!(p.kind(),Prefix::Disk(_)|Prefix::VerbatimDisk(_)));
        disk && parts.all(|p| matches!(p, Component::RootDir))
    }
    #[cfg(not(windows))]
    {
        let _ = path;
        false
    }
}
fn root_path(root: &Path) -> AppResult<PathBuf> {
    plain(root)?;
    let path = fs::canonicalize(root).map_err(|e| err("선택 폴더 확인 실패", e))?;
    if !path.is_dir() {
        return Err("실제 폴더를 선택하세요.".into());
    }
    #[cfg(windows)]
    let unc = matches!(path.components().next(),Some(Component::Prefix(p)) if matches!(p.kind(),std::path::Prefix::UNC(_,_)|std::path::Prefix::VerbatimUNC(_,_)));
    #[cfg(not(windows))]
    let unc = false;
    if is_local_drive_root(&path) || (path.parent().is_none() && !unc) {
        return Err("디스크 전체를 정리 대상으로 사용할 수 없습니다.".into());
    }
    for name in [
        "HOME",
        "USERPROFILE",
        "WINDIR",
        "SystemRoot",
        "ProgramFiles",
        "ProgramFiles(x86)",
    ] {
        if let Some(v) = std::env::var_os(name) {
            if let Ok(p) = fs::canonicalize(PathBuf::from(v)) {
                if path == p
                    || ((name == "WINDIR"
                        || name == "SystemRoot"
                        || name.starts_with("ProgramFiles"))
                        && path.starts_with(&p))
                {
                    return Err("홈 전체·시스템·프로그램 폴더는 정리할 수 없습니다.".into());
                }
            }
        }
    }
    #[cfg(unix)]
    if [
        "/etc", "/usr", "/bin", "/sbin", "/lib", "/proc", "/sys", "/dev",
    ]
    .iter()
    .any(|p| path.starts_with(p))
    {
        return Err("시스템 폴더를 정리할 수 없습니다.".into());
    }
    Ok(path)
}
fn source_in(root: &Path, path: &Path) -> AppResult<PathBuf> {
    plain(path)?;
    let p = fs::canonicalize(path).map_err(|e| err("원본 경로 확인 실패", e))?;
    let rel = p
        .strip_prefix(root)
        .map_err(|_| "선택 폴더 밖의 파일입니다.".to_string())?;
    if rel.components().any(|c| {
        let s = c.as_os_str().to_string_lossy().to_lowercase();
        [
            APP,
            ".folder-organizer",
            "delete_files",
            "#recycle",
            "@recycle",
            "$recycle.bin",
        ]
        .contains(&s.as_str())
    }) {
        return Err("보관·휴지통·작업 기록은 새 정리 대상이 아닙니다.".into());
    }
    if !fs::symlink_metadata(&p)
        .map_err(|e| err("원본 확인 실패", e))?
        .is_file()
    {
        return Err("일반 파일만 정리할 수 있습니다.".into());
    }
    Ok(p)
}
fn same_file(a: &FileSnapshot, b: &FileSnapshot) -> bool {
    a.device == b.device && a.inode == b.inode && a.size == b.size && a.modified_ns == b.modified_ns
}
fn assert_snapshot(path: &Path, expected: &FileSnapshot) -> AppResult<()> {
    if core::snapshot(path)? != *expected {
        return Err(format!(
            "검사 후 파일이 바뀌었습니다. 다시 검사하세요: {}",
            path.display()
        ));
    }
    Ok(())
}
fn hash_file(path: &Path, cancel: &AtomicBool) -> AppResult<String> {
    let mut file = core::safe_open(path)?;
    let before = core::snapshot_open_file(&file)?;
    let mut hash = Sha256::new();
    let mut buf = vec![0; CHUNK];
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("작업을 취소했습니다.".into());
        }
        let n = file.read(&mut buf).map_err(|e| err("파일 읽기 실패", e))?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    if core::snapshot_open_file(&file)? != before || core::snapshot(path)? != before {
        return Err("읽는 동안 원본이 변경되었습니다.".into());
    }
    Ok(hex(&hash.finalize()))
}

/// Rename without replacing any existing name. Never fall back to unlink.
fn exclusive_rename(source: &Path, target: &Path) -> AppResult<()> {
    plain(source)?;
    plain(target)?;
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::ffi::OsStrExt;
        let a = std::ffi::CString::new(source.as_os_str().as_bytes())
            .map_err(|e| err("원본 경로 오류", e))?;
        let b = std::ffi::CString::new(target.as_os_str().as_bytes())
            .map_err(|e| err("대상 경로 오류", e))?;
        let code = unsafe {
            libc::renameat2(
                libc::AT_FDCWD,
                a.as_ptr(),
                libc::AT_FDCWD,
                b.as_ptr(),
                libc::RENAME_NOREPLACE,
            )
        };
        if code != 0 {
            return Err(err(
                "덮어쓰기 없는 이동 실패",
                std::io::Error::last_os_error(),
            ));
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let a: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
        let b: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        let code = unsafe {
            windows_sys::Win32::Storage::FileSystem::MoveFileExW(a.as_ptr(), b.as_ptr(), 8)
        };
        if code == 0 {
            return Err(err(
                "덮어쓰기 없는 이동 실패",
                std::io::Error::last_os_error(),
            ));
        }
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        let _ = (source, target);
        return Err("이 플랫폼에서는 안전한 원자적 이동이 지원되지 않습니다.".into());
    }
    #[cfg(test)]
    TEST_DURABILITY_EVENTS.with(|events| {
        events
            .borrow_mut()
            .push(("rename".into(), target.to_path_buf()))
    });
    if let Some(parent) = source.parent() {
        sync_directory(parent)?;
    }
    if source.parent() != target.parent() {
        if let Some(parent) = target.parent() {
            sync_directory(parent)?;
        }
    }
    Ok(())
}

fn bounded_read(path: &Path, limit: u64) -> AppResult<Vec<u8>> {
    let mut f = core::safe_open(path)?;
    if f.metadata()
        .map_err(|e| err("기록 크기 확인 실패", e))?
        .len()
        > limit
    {
        return Err("작업 기록 크기 한도를 초과했습니다.".into());
    }
    let mut bytes = Vec::new();
    Read::by_ref(&mut f)
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| err("기록 읽기 실패", e))?;
    if bytes.len() as u64 > limit {
        return Err("작업 기록 크기 한도를 초과했습니다.".into());
    }
    Ok(bytes)
}
fn write_new(path: &Path, data: &[u8]) -> AppResult<()> {
    plain(path)?;
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut f = options
        .open(path)
        .map_err(|e| err("새 파일 생성 실패", e))?;
    f.write_all(data)
        .and_then(|_| f.sync_all())
        .map_err(|e| err("파일 기록 실패", e))?;
    if let Some(parent) = path.parent() {
        sync_directory(parent)?;
    }
    Ok(())
}
fn replace_private(source: &Path, target: &Path) -> AppResult<()> {
    plain(source)?;
    plain(target)?;
    #[cfg(not(windows))]
    fs::rename(source, target).map_err(|e| err("인증된 기록 교체 실패", e))?;
    #[cfg(windows)]
    {
        use std::os::windows::ffi::OsStrExt;
        let a: Vec<_> = source.as_os_str().encode_wide().chain(Some(0)).collect();
        let b: Vec<_> = target.as_os_str().encode_wide().chain(Some(0)).collect();
        if unsafe {
            windows_sys::Win32::Storage::FileSystem::MoveFileExW(a.as_ptr(), b.as_ptr(), 1 | 8)
        } == 0
        {
            return Err(err(
                "인증된 기록 교체 실패",
                std::io::Error::last_os_error(),
            ));
        }
    }
    if let Some(parent) = target.parent() {
        sync_directory(parent)?;
    }
    Ok(())
}
fn private_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() < 128
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'.')
        && !name.starts_with('.')
}
fn journal_key(root: &Path, create: bool) -> AppResult<Vec<u8>> {
    let app = root.join(APP);
    if create {
        make_dirs(&app)?;
    } else {
        plain(&app)?;
    }
    let path = app.join("journal.key");
    if create && !path.exists() {
        let mut key = [0; 32];
        getrandom::getrandom(&mut key).map_err(|e| err("인증 키 생성 실패", e))?;
        if let Err(e) = write_new(&path, &key) {
            if !path.exists() {
                return Err(e);
            }
        }
    }
    let key = bounded_read(&path, 32)?;
    if key.len() != 32 {
        return Err("복원 인증 키가 없거나 손상되었습니다.".into());
    }
    Ok(key)
}

struct SharedLock {
    file: File,
}
impl SharedLock {
    fn acquire(root: &Path) -> AppResult<Self> {
        let directory = root.join(".folder-organizer");
        make_dirs(&directory)?;
        let path = directory.join("operations.lock");
        plain(&path)?;
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .mode(0o600);
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.custom_flags(0x0020_0000);
        }
        let file = options
            .open(&path)
            .map_err(|e| err("공유 작업 잠금 열기 실패", e))?;
        let metadata = file.metadata().map_err(|e| err("공유 잠금 확인 실패", e))?;
        if !metadata.is_file() || is_reparse(&metadata) {
            return Err("공유 작업 잠금은 일반 파일이어야 합니다.".into());
        }
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err("Python 또는 Rust 버전에서 다른 파일 작업이 진행 중입니다.".into());
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            if unsafe {
                windows_sys::Win32::Storage::FileSystem::LockFile(file.as_raw_handle(), 0, 0, 1, 0)
            } == 0
            {
                return Err("Python 또는 Rust 버전에서 다른 파일 작업이 진행 중입니다.".into());
            }
        }
        Ok(Self { file })
    }
}
impl Drop for SharedLock {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            unsafe {
                libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
            }
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            unsafe {
                windows_sys::Win32::Storage::FileSystem::UnlockFile(
                    self.file.as_raw_handle(),
                    0,
                    0,
                    1,
                    0,
                );
            }
        }
        // Never remove or truncate Python's interoperable lock file.
    }
}
struct OperationLock {
    path: PathBuf,
    nonce: String,
    _shared: SharedLock,
}
impl OperationLock {
    fn acquire(root: &Path, key: &[u8]) -> AppResult<Self> {
        let shared = SharedLock::acquire(root)?;
        let path = root.join(APP).join("operations.lock");
        let nonce = random_id()?;
        let payload = (std::process::id(), now(), nonce.clone());
        let data = serde_json::to_vec(&Envelope {
            mac: mac(key, &payload)?,
            payload,
        })
        .map_err(|e| err("잠금 기록 실패", e))?;
        match write_new(&path, &data) {
            Ok(()) => Ok(Self {
                path,
                nonce,
                _shared: shared,
            }),
            Err(e) => {
                let bytes = bounded_read(&path, 4096)?;
                let old: Envelope<(u32, u64, String)> =
                    serde_json::from_slice(&bytes).map_err(|_| {
                        "다른 작업의 잠금이 있습니다. 작업 종료 후 다시 시도하세요.".to_string()
                    })?;
                verify(key, &old.payload, &old.mac)?;
                if process_alive(old.payload.0) {
                    return Err("다른 파일 작업이 진행 중입니다.".into());
                }
                let before = core::snapshot(&path)?;
                let stale = root.join(APP).join(format!("stale-lock-{}", random_id()?));
                assert_snapshot(&path, &before)?;
                exclusive_rename(&path, &stale)?;
                if bounded_read(&stale, 4096)? != bytes {
                    return Err("잠금이 변경되어 안전을 위해 중단했습니다.".into());
                }
                write_new(&path, &data).map_err(|_| e)?;
                Ok(Self {
                    path,
                    nonce,
                    _shared: shared,
                })
            }
        }
    }
}
impl Drop for OperationLock {
    fn drop(&mut self) {
        if let Ok(bytes) = bounded_read(&self.path, 4096) {
            if let Ok(env) = serde_json::from_slice::<Envelope<(u32, u64, String)>>(&bytes) {
                if env.payload.0 == std::process::id() && env.payload.2 == self.nonce {
                    let _ = fs::remove_file(&self.path);
                }
            }
        }
    }
}
fn process_alive(pid: u32) -> bool {
    #[cfg(unix)]
    {
        unsafe {
            libc::kill(pid as i32, 0) == 0
                || std::io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
        }
    }
    #[cfg(windows)]
    {
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut std::ffi::c_void;
            fn GetExitCodeProcess(handle: *mut std::ffi::c_void, exit: *mut u32) -> i32;
            fn CloseHandle(handle: *mut std::ffi::c_void) -> i32;
            fn GetLastError() -> u32;
        }
        unsafe {
            let h = OpenProcess(0x1000, 0, pid);
            if h.is_null() {
                return GetLastError() != 87;
            }
            let mut code = 259;
            let ok = GetExitCodeProcess(h, &mut code);
            CloseHandle(h);
            ok == 0 || code == 259
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = pid;
        true
    }
}

impl Store {
    fn create(plan: &OperationPlan, key: Vec<u8>) -> AppResult<Self> {
        let directory = plan.root.join(APP).join("history").join(&plan.id);
        make_dirs(&directory)?;
        let journal = Journal {
            schema: 1,
            id: plan.id.clone(),
            root: plan.root.clone(),
            kind: plan.kind,
            created_at: plan.created_at,
            items: plan
                .items
                .iter()
                .map(|p| JournalItem {
                    receipt: OperationItem {
                        source: p.source.clone(),
                        target: p.target.clone(),
                        size: p.size,
                        status: "planned".into(),
                        error: None,
                        source_removed: false,
                        sha256: p.sha256.clone(),
                    },
                    original: p.snapshot.clone(),
                    keeper: p.keeper.clone(),
                    captured: None,
                    staging: None,
                    staged_snapshot: None,
                    backup: None,
                    backup_snapshot: None,
                    retired_target: None,
                    compact_retired: None,
                    compact_snapshot: None,
                    recipe: None,
                    restored_snapshot: None,
                })
                .collect(),
        };
        let signature = mac(&key, &journal)?;
        let data = serde_json::to_vec(&Envelope {
            payload: journal.clone(),
            mac: signature.clone(),
        })
        .map_err(|e| err("작업 기록 생성 실패", e))?;
        if data.len() as u64 > MAX_METADATA {
            return Err("한 번에 정리할 파일 수가 너무 많습니다.".into());
        }
        write_new(&directory.join("journal.json"), &data)?;
        write_new(&directory.join("events.jsonl"), &[])?;
        Ok(Self {
            journal,
            directory,
            key,
            last_mac: signature,
            sequence: 0,
            base_file: "journal.json".into(),
            event_file: "events.jsonl".into(),
            missing_raw: false,
            poisoned: false,
        })
    }
    fn load(root: &Path, id: &str) -> AppResult<Self> {
        if !valid_id(id) {
            return Err("올바른 작업 기록을 선택하세요.".into());
        }
        let key = journal_key(root, false)?;
        let directory = root.join(APP).join("history").join(id);
        plain(&directory)?;
        let pointer = directory.join("chain-pointer.json");
        let (base_file, event_file) = if pointer.exists() {
            let signed: Envelope<ChainPointer> =
                serde_json::from_slice(&bounded_read(&pointer, 4096)?)
                    .map_err(|e| err("복원 기록 위치 손상", e))?;
            verify(&key, &signed.payload, &signed.mac)?;
            if signed.payload.id != id
                || signed.payload.root != root
                || !private_name(&signed.payload.base_file)
                || !private_name(&signed.payload.event_file)
            {
                return Err("복원 기록 위치가 변경되었습니다.".into());
            }
            (signed.payload.base_file, signed.payload.event_file)
        } else {
            ("journal.json".into(), "events.jsonl".into())
        };
        let data = if directory.join(&base_file).exists() {
            Some(bounded_read(&directory.join(&base_file), MAX_METADATA)?)
        } else {
            None
        };
        let events = if directory.join(&event_file).exists() {
            Some(bounded_read(&directory.join(&event_file), MAX_METADATA)?)
        } else {
            None
        };
        if let Some((journal, sequence, last_mac)) = load_metadata(
            &directory,
            &key,
            root,
            id,
            &base_file,
            &event_file,
            data.as_deref(),
            events.as_deref(),
        )? {
            return Ok(Self {
                journal,
                directory,
                key,
                last_mac,
                sequence,
                base_file,
                event_file,
                missing_raw: data.is_none() || events.is_none(),
                poisoned: false,
            });
        }
        let data = data.ok_or_else(|| {
            "원래 복원 기록이 없고 유효한 압축 메타데이터도 없습니다.".to_string()
        })?;
        let events = events.ok_or_else(|| "복원 이벤트 기록이 없습니다.".to_string())?;
        let envelope: Envelope<Journal> =
            serde_json::from_slice(&data).map_err(|e| err("작업 기록 손상", e))?;
        verify(&key, &envelope.payload, &envelope.mac)?;
        let mut journal = envelope.payload;
        if journal.schema != 1
            || journal.id != id
            || journal.root != root
            || journal.items.len() > MAX_FILES
        {
            return Err("다른 위치·형식의 작업 기록입니다.".into());
        }
        let mut last_mac = envelope.mac;
        let mut sequence = 0;
        // A power loss can leave an incomplete final append. The preceding
        // authenticated intent remains sufficient to preserve/restore bytes.
        let usable = events
            .iter()
            .rposition(|b| *b == b'\n')
            .map_or(&[][..], |i| &events[..=i]);
        for line in usable.split(|b| *b == b'\n').filter(|l| !l.is_empty()) {
            if line.len() as u64 > MAX_RECIPE {
                return Err("복원 이벤트 크기 한도를 초과했습니다.".into());
            }
            let event: Envelope<Event> =
                serde_json::from_slice(line).map_err(|e| err("작업 이벤트 손상", e))?;
            verify(&key, &event.payload, &event.mac)?;
            if event.payload.sequence != sequence + 1
                || event.payload.previous != last_mac
                || event.payload.index >= journal.items.len()
            {
                return Err("작업 이벤트 연결이 손상되었습니다.".into());
            }
            let index = event.payload.index;
            let initial = &journal.items[index];
            let next = &event.payload.item;
            if initial.receipt.source != next.receipt.source
                || initial.receipt.target != next.receipt.target
                || initial.original != next.original
            {
                return Err("작업 원본·대상 정보가 변경되었습니다.".into());
            }
            journal.items[index] = event.payload.item;
            sequence += 1;
            last_mac = event.mac;
        }
        let torn_tail = usable.len() != events.len();
        Ok(Self {
            journal,
            directory,
            key,
            last_mac,
            sequence,
            base_file,
            event_file,
            missing_raw: torn_tail,
            poisoned: false,
        })
    }
    fn checkpoint(&mut self, index: usize, item: JournalItem) -> AppResult<()> {
        if self.poisoned {
            return Err("상태 기록 중 저장 오류가 발생했습니다. 기록을 다시 확인하기 전에는 추가 작업을 하지 않습니다.".into());
        }
        if self.missing_raw {
            self.rebase()?;
        }
        let event = Event {
            sequence: self.sequence + 1,
            previous: self.last_mac.clone(),
            index,
            item: item.clone(),
        };
        let signature = mac(&self.key, &event)?;
        let mut bytes = serde_json::to_vec(&Envelope {
            payload: event,
            mac: signature.clone(),
        })
        .map_err(|e| err("상태 기록 실패", e))?;
        if bytes.len() as u64 > MAX_RECIPE {
            return Err("복원 상태가 너무 커서 기존 파일을 그대로 보존합니다.".into());
        }
        bytes.push(b'\n');
        let path = self.directory.join(&self.event_file);
        plain(&path)?;
        let old = fs::metadata(&path).map_err(|e| err("기록 확인 실패", e))?;
        if old.len() + bytes.len() as u64 > MAX_METADATA {
            return Err("복원 기록 한도에 도달해 작업을 중단합니다.".into());
        }
        let mut f = OpenOptions::new()
            .append(true)
            .open(&path)
            .map_err(|e| err("상태 기록 열기 실패", e))?;
        if is_reparse(&f.metadata().map_err(|e| err("기록 파일 확인 실패", e))?) {
            return Err("작업 기록은 링크일 수 없습니다.".into());
        }
        // A failed write/fsync may already have appended a complete or partial
        // record. Never append again with stale sequence/MAC state in this Store.
        self.poisoned = true;
        f.write_all(&bytes)
            .and_then(|_| f.sync_all())
            .map_err(|e| err("복원 상태 저장 실패", e))?;
        #[cfg(test)]
        if TEST_FAIL_SYNC.with(|flag| flag.replace(false)) {
            return Err("injected fsync failure after a complete append".into());
        }
        self.journal.items[index] = item;
        self.sequence += 1;
        self.last_mac = signature;
        self.poisoned = false;
        Ok(())
    }
    fn rebase(&mut self) -> AppResult<()> {
        let nonce = random_id()?;
        let base_file = format!("base-{nonce}.json");
        let event_file = format!("events-{nonce}.jsonl");
        let signature = mac(&self.key, &self.journal)?;
        let bytes = serde_json::to_vec(&Envelope {
            payload: self.journal.clone(),
            mac: signature.clone(),
        })
        .map_err(|e| err("압축 복원 기록 준비 실패", e))?;
        write_new(&self.directory.join(&base_file), &bytes)?;
        write_new(&self.directory.join(&event_file), &[])?;
        let pointer = ChainPointer {
            id: self.journal.id.clone(),
            root: self.journal.root.clone(),
            base_file: base_file.clone(),
            event_file: event_file.clone(),
        };
        let bytes = serde_json::to_vec(&Envelope {
            mac: mac(&self.key, &pointer)?,
            payload: pointer,
        })
        .map_err(|e| err("복원 기록 위치 저장 실패", e))?;
        let temporary = self.directory.join(format!("pointer-{nonce}.tmp"));
        write_new(&temporary, &bytes)?;
        replace_private(&temporary, &self.directory.join("chain-pointer.json"))?;
        self.base_file = base_file;
        self.event_file = event_file;
        self.last_mac = signature;
        self.sequence = 0;
        self.missing_raw = false;
        Ok(())
    }
    fn receipt(&self, cancelled: bool) -> OperationReceipt {
        let items: Vec<_> = self
            .journal
            .items
            .iter()
            .map(|i| i.receipt.clone())
            .collect();
        let errors = items
            .iter()
            .filter_map(|i| i.error.clone())
            .collect::<Vec<_>>();
        let completed = !cancelled
            && errors.is_empty()
            && items.iter().all(|i| {
                ["copied", "moved", "deleted", "restored", "compacted"].contains(&i.status.as_str())
            });
        OperationReceipt {
            id: self.journal.id.clone(),
            root: self.journal.root.clone(),
            kind: self.journal.kind,
            items,
            errors,
            completed,
            cancelled,
        }
    }
    fn compress_metadata(&self) -> AppResult<()> {
        let payload =
            serde_json::to_vec(&self.journal).map_err(|e| err("복원 메타데이터 변환 실패", e))?;
        if payload.len() < 1024 || payload.len() as u64 > MAX_RECIPE {
            return Ok(());
        }
        let start = Instant::now();
        let mut encoder = GzEncoder::new(Vec::new(), Compression::fast());
        encoder
            .write_all(&payload)
            .map_err(|e| err("메타데이터 압축 실패", e))?;
        let bytes = encoder
            .finish()
            .map_err(|e| err("메타데이터 압축 실패", e))?;
        if start.elapsed() > Duration::from_millis(250) || bytes.len() * 10 > payload.len() * 9 {
            return Ok(());
        }
        // This is an additional checkpoint, never a substitute for the durable
        // authenticated base/event chain when compression is not worthwhile.
        let token = format!("{}-{}", self.sequence, &self.last_mac[..16]);
        let gzip_file = format!("metadata-{token}.json.gz");
        let checkpoint = self.directory.join(&gzip_file);
        if !checkpoint.exists() {
            write_new(&checkpoint, &bytes)?;
            let descriptor = MetadataReceipt {
                id: self.journal.id.clone(),
                root: self.journal.root.clone(),
                sequence: self.sequence,
                last_mac: self.last_mac.clone(),
                base_file: self.base_file.clone(),
                event_file: self.event_file.clone(),
                base_sha256: digest(&bounded_read(
                    &self.directory.join(&self.base_file),
                    MAX_METADATA,
                )?),
                event_sha256: digest(&bounded_read(
                    &self.directory.join(&self.event_file),
                    MAX_METADATA,
                )?),
                payload_sha256: digest(&payload),
                gzip_sha256: digest(&bytes),
                gzip_file,
            };
            let signed = Envelope {
                mac: mac(&self.key, &descriptor)?,
                payload: descriptor,
            };
            write_new(
                &self
                    .directory
                    .join(format!("metadata-{token}.receipt.json")),
                &serde_json::to_vec(&signed).map_err(|e| err("압축 기록 인증 실패", e))?,
            )?;
        }
        Ok(())
    }
}

fn load_metadata(
    directory: &Path,
    key: &[u8],
    root: &Path,
    id: &str,
    base_file: &str,
    event_file: &str,
    base: Option<&[u8]>,
    events: Option<&[u8]>,
) -> AppResult<Option<(Journal, u64, String)>> {
    let base_hash = base.map(digest);
    let event_hash = events.map(digest);
    let mut receipts = Vec::new();
    for entry in fs::read_dir(directory)
        .map_err(|e| err("압축 복원 기록 탐색 실패", e))?
        .take(2000)
    {
        let Ok(entry) = entry else {
            continue;
        };
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("metadata-") || !name.ends_with(".receipt.json") {
            continue;
        }
        let Ok(data) = bounded_read(&entry.path(), 4096) else {
            continue;
        };
        let Ok(signed) = serde_json::from_slice::<Envelope<MetadataReceipt>>(&data) else {
            continue;
        };
        if verify(key, &signed.payload, &signed.mac).is_err() {
            continue;
        }
        let receipt = signed.payload;
        if receipt.id != id
            || receipt.root != root
            || receipt.base_file != base_file
            || receipt.event_file != event_file
            || !private_name(&receipt.gzip_file)
        {
            continue;
        }
        if base_hash
            .as_ref()
            .is_some_and(|h| *h != receipt.base_sha256)
            || event_hash
                .as_ref()
                .is_some_and(|h| *h != receipt.event_sha256)
        {
            continue;
        }
        receipts.push(receipt);
    }
    receipts.sort_by(|a, b| b.sequence.cmp(&a.sequence));
    for receipt in receipts {
        let Ok(bytes) = bounded_read(&directory.join(&receipt.gzip_file), MAX_RECIPE) else {
            continue;
        };
        if digest(&bytes) != receipt.gzip_sha256 {
            continue;
        }
        let mut payload = Vec::new();
        let decoder = GzDecoder::new(bytes.as_slice());
        if decoder
            .take(MAX_RECIPE + 1)
            .read_to_end(&mut payload)
            .is_err()
            || payload.len() as u64 > MAX_RECIPE
            || digest(&payload) != receipt.payload_sha256
        {
            continue;
        }
        let Ok(journal) = serde_json::from_slice::<Journal>(&payload) else {
            continue;
        };
        if journal.schema != 1
            || journal.id != id
            || journal.root != root
            || journal.items.len() > MAX_FILES
        {
            continue;
        }
        return Ok(Some((journal, receipt.sequence, receipt.last_mac)));
    }
    Ok(None)
}

fn progress(
    callback: Option<&dyn Fn(Progress)>,
    phase: &str,
    index: usize,
    total: usize,
    message: String,
) {
    if let Some(cb) = callback {
        cb(Progress {
            phase: phase.into(),
            completed: index as u64,
            total: Some(total as u64),
            message,
        });
    }
}
fn log(root: &Path, operation_id: &str, action: &str, item: &OperationItem, message: &str) {
    let entry = LogRecord {
        time: now(),
        operation_id: operation_id.into(),
        action: action.into(),
        path: item.source.to_string_lossy().into_owned(),
        status: item.status.clone(),
        message: message.into(),
    };
    let path = root.join(APP).join("execution.jsonl");
    if plain(&path).is_err() {
        return;
    }
    let Ok(mut bytes) = serde_json::to_vec(&entry) else {
        return;
    };
    bytes.push(b'\n');
    if let Ok(metadata) = fs::metadata(&path) {
        if metadata.len() > 16 * 1024 * 1024 {
            let old = root.join(APP).join(format!("execution-{}.jsonl", now()));
            let _ = exclusive_rename(&path, &old);
        }
    }
    if let Ok(mut f) = OpenOptions::new().append(true).create(true).open(path) {
        let _ = f.write_all(&bytes);
    }
}
fn target_key(path: &Path) -> String {
    if cfg!(windows) {
        path.to_string_lossy().to_lowercase()
    } else {
        path.to_string_lossy().into_owned()
    }
}
fn unique_target(
    path: &Path,
    used: &mut BTreeSet<String>,
    counters: &mut HashMap<PathBuf, u64>,
) -> AppResult<PathBuf> {
    let parent = path
        .parent()
        .ok_or_else(|| "대상 폴더가 없습니다.".to_string())?;
    let name = path
        .file_name()
        .ok_or_else(|| "대상 파일 이름이 없습니다.".to_string())?;
    let count = counters.entry(path.to_path_buf()).or_insert(0);
    loop {
        let candidate = if *count == 0 {
            path.to_path_buf()
        } else {
            let stem = path.file_stem().unwrap_or(name);
            let mut n = stem.to_os_string();
            n.push(format!(" ({count})"));
            if let Some(ext) = path.extension() {
                n.push(".");
                n.push(ext);
            }
            parent.join(n)
        };
        *count += 1;
        plain(&candidate)?;
        if !candidate.exists() && used.insert(target_key(&candidate)) {
            return Ok(candidate);
        }
        if *count > 1_000_000 {
            return Err("대상 이름 충돌이 너무 많습니다.".into());
        }
    }
}
fn signed_plan(
    root: PathBuf,
    kind: TransferKind,
    items: Vec<PlannedItem>,
) -> AppResult<OperationPlan> {
    if items.is_empty() {
        return Err("정리할 파일을 선택하세요.".into());
    }
    if items.len() > MAX_FILES {
        return Err(format!("한 작업은 최대 {MAX_FILES}개 파일입니다."));
    }
    let mut plan = OperationPlan {
        id: random_id()?,
        root,
        kind,
        items,
        created_at: now(),
        proof: String::new(),
    };
    plan.proof = mac(plan_key(), &plan_binding(&plan)?)?;
    Ok(plan)
}

/// Build a read-only, authenticated preview for explicit copy/move/delete.
pub fn plan_files(
    scan: &ScanResult,
    paths: &[PathBuf],
    destination: Option<&Path>,
    kind: TransferKind,
) -> AppResult<OperationPlan> {
    if scan.cancelled {
        return Err("완료된 분석 결과에서 정리하세요.".into());
    }
    let root = root_path(&scan.root)?;
    let destination = if kind == TransferKind::Delete {
        root.join("delete_files")
    } else {
        let dest = destination.ok_or_else(|| "대상 폴더를 선택하세요.".to_string())?;
        plain(dest)?;
        fs::canonicalize(dest).map_err(|e| err("대상 폴더 확인 실패", e))?
    };
    let mappings = paths
        .iter()
        .map(|source| {
            source
                .file_name()
                .map(|name| (source.clone(), destination.join(name)))
                .ok_or_else(|| "원본 이름 오류".to_string())
        })
        .collect::<AppResult<Vec<_>>>()?;
    plan_mapped(scan, &mappings, kind)
}

/// Rules and date/extension mappings share this one execution path.
pub fn plan_mapped(
    scan: &ScanResult,
    mappings: &[(PathBuf, PathBuf)],
    kind: TransferKind,
) -> AppResult<OperationPlan> {
    if scan.cancelled {
        return Err("완료된 분석 결과에서 정리하세요.".into());
    }
    let root = root_path(&scan.root)?;
    let scanned: HashMap<_, _> = scan.files.iter().map(|f| (f.path.clone(), f)).collect();
    let mut seen = BTreeSet::new();
    let mut used = BTreeSet::new();
    let mut counters = HashMap::new();
    let mut items = Vec::new();
    for (source, requested) in mappings {
        let source = source_in(&root, source)?;
        if !seen.insert(target_key(&source)) {
            return Err("한 파일이 여러 번 선택되었습니다.".into());
        }
        let original = scanned
            .get(&source)
            .ok_or_else(|| "현재 분석에 없는 파일입니다. 다시 검사하세요.".to_string())?;
        let snapshot = core::snapshot(&source)?;
        if snapshot.size != original.size
            || snapshot.modified_ns != original.modified_ns
            || original
                .identity
                .as_ref()
                .is_some_and(|old| *old != snapshot)
        {
            return Err("검사 후 원본이 변경되었습니다.".into());
        }
        let requested = if kind == TransferKind::Delete {
            root.join("delete_files").join(source.file_name().unwrap())
        } else {
            requested.clone()
        };
        if !requested.is_absolute() || requested == source {
            return Err("원본과 다른 절대 대상 경로를 선택하세요.".into());
        }
        plain(&requested)?;
        if requested.components().any(|c| {
            [
                APP,
                ".folder-organizer",
                "#recycle",
                "@recycle",
                "$recycle.bin",
            ]
            .contains(&c.as_os_str().to_string_lossy().to_lowercase().as_str())
        }) {
            return Err("작업 기록·휴지통을 대상 폴더로 사용할 수 없습니다.".into());
        }
        let target = unique_target(&requested, &mut used, &mut counters)?;
        items.push(PlannedItem {
            source,
            target,
            size: snapshot.size,
            snapshot,
            sha256: None,
            keeper: None,
        });
    }
    signed_plan(root, kind, items)
}

/// Preserve each authenticated group's displayed first file and every protected
/// file. This function performs metadata checks only: no hash or second scan.
pub fn keep_first(
    scan: &ScanResult,
    groups: &[DuplicateGroup],
    protected: &[PathBuf],
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<OperationReceipt> {
    if scan.cancelled {
        return Err("완료된 중복 검사 결과가 필요합니다.".into());
    }
    let root = root_path(&scan.root)?;
    let scanned: BTreeSet<_> = scan.files.iter().map(|f| f.path.clone()).collect();
    let protected = protected
        .iter()
        .map(|p| {
            plain(p)?;
            fs::canonicalize(p).map_err(|e| err("보호 폴더 확인 실패", e))
        })
        .collect::<AppResult<Vec<_>>>()?;
    let mut seen = BTreeSet::new();
    let mut used = BTreeSet::new();
    let mut counters = HashMap::new();
    let mut items = Vec::new();
    for group in groups {
        core::validate_group(group)?;
        let keeper = group
            .paths
            .first()
            .ok_or_else(|| "빈 중복 그룹입니다.".to_string())?;
        let keeper_snapshot = core::group_snapshot(group, keeper)
            .ok_or_else(|| "중복 보존 정보가 없습니다.".to_string())?
            .clone();
        for path in &group.paths {
            if !scanned.contains(path) || !seen.insert(target_key(path)) {
                return Err("중복 그룹이 겹치거나 현재 분석과 다릅니다.".into());
            }
            source_in(&root, path)?;
        }
        for path in group.paths.iter().skip(1) {
            if protected.iter().any(|p| path == p || path.starts_with(p)) {
                continue;
            }
            let original = core::group_snapshot(group, path)
                .ok_or_else(|| "검증된 파일 정보가 없습니다.".to_string())?
                .clone();
            let requested = root.join("delete_files").join(
                path.file_name()
                    .ok_or_else(|| "파일 이름 오류".to_string())?,
            );
            let target = unique_target(&requested, &mut used, &mut counters)?;
            items.push(PlannedItem {
                source: path.clone(),
                target,
                size: original.size,
                snapshot: original,
                sha256: Some(group.sha256.clone()),
                keeper: Some((keeper.clone(), keeper_snapshot.clone())),
            });
        }
    }
    let plan = signed_plan(root, TransferKind::Delete, items)?;
    execute(&plan, cancel, callback)
}

fn verify_keeper(item: &JournalItem) -> AppResult<()> {
    if let Some((path, expected)) = &item.keeper {
        assert_snapshot(path, expected)?;
    }
    Ok(())
}
fn execute_delete(store: &mut Store, index: usize, cancel: &AtomicBool) -> AppResult<()> {
    let mut item = store.journal.items[index].clone();
    assert_snapshot(&item.receipt.source, &item.original)?;
    verify_keeper(&item)?;
    if item.receipt.sha256.is_none() {
        item.receipt.sha256 = Some(hash_file(&item.receipt.source, cancel)?);
    }
    let parent = item
        .receipt
        .target
        .parent()
        .ok_or_else(|| "보관 위치 오류".to_string())?;
    make_dirs(parent)?;
    assert_snapshot(&item.receipt.source, &item.original)?;
    verify_keeper(&item)?;
    item.receipt.status = "capture_intent".into();
    store.checkpoint(index, item.clone())?;
    exclusive_rename(&item.receipt.source, &item.receipt.target)?;
    let captured = core::snapshot(&item.receipt.target)?;
    if !same_file(&captured, &item.original) || verify_keeper(&item).is_err() {
        let rollback = exclusive_rename(&item.receipt.target, &item.receipt.source);
        item.receipt.source_removed = rollback.is_err();
        item.captured = Some(captured);
        item.receipt.status = "conflict".into();
        item.receipt.error = Some(if rollback.is_ok() {
            "이동 중 파일이 변경되어 원래 위치로 되돌렸습니다.".into()
        } else {
            "이동 중 변경을 발견했습니다. 파일은 보관 위치에 안전하게 남아 있습니다.".into()
        });
        store.checkpoint(index, item)?;
        return Err("정리 중 변경을 발견해 중단했습니다.".into());
    }
    item.captured = Some(captured);
    item.receipt.source_removed = true;
    item.receipt.status = "deleted".into();
    store.checkpoint(index, item)
}

fn create_staged_copy(store: &mut Store, index: usize, cancel: &AtomicBool) -> AppResult<()> {
    let mut item = store.journal.items[index].clone();
    assert_snapshot(&item.receipt.source, &item.original)?;
    let parent = item
        .receipt
        .target
        .parent()
        .ok_or_else(|| "대상 폴더 오류".to_string())?;
    make_dirs(parent)?;
    let staging = parent.join(APP).join("staging").join(&store.journal.id);
    make_dirs(&staging)?;
    let path = staging.join(format!("{index}.copy"));
    item.staging = Some(path.clone());
    item.receipt.status = "copy_intent".into();
    store.checkpoint(index, item.clone())?;
    let mut source = core::safe_open(&item.receipt.source)?;
    if core::snapshot_open_file(&source)? != item.original {
        return Err("복사 직전 원본이 변경되었습니다.".into());
    }
    let mut target = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)
        .map_err(|e| err("복사 준비 실패", e))?;
    let mut hash = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = vec![0; CHUNK];
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("작업을 취소했습니다.".into());
        }
        let count = source
            .read(&mut buffer)
            .map_err(|e| err("원본 읽기 실패", e))?;
        if count == 0 {
            break;
        }
        bytes += count as u64;
        if bytes > item.original.size {
            return Err("읽는 동안 원본 크기가 변경되었습니다.".into());
        }
        target
            .write_all(&buffer[..count])
            .map_err(|e| err("복사 쓰기 실패", e))?;
        hash.update(&buffer[..count]);
    }
    if let Ok(modified) = source.metadata().and_then(|m| m.modified()) {
        target
            .set_times(fs::FileTimes::new().set_modified(modified))
            .map_err(|e| err("복사본 수정일 보존 실패", e))?;
    }
    target.sync_all().map_err(|e| err("복사 저장 실패", e))?;
    drop(target);
    if bytes != item.original.size || core::snapshot_open_file(&source)? != item.original {
        return Err("읽는 동안 원본이 변경되었습니다.".into());
    }
    assert_snapshot(&item.receipt.source, &item.original)?;
    let sha = hex(&hash.finalize());
    if hash_file(&path, cancel)? != sha {
        return Err("복사본 내용을 검증할 수 없습니다. 원본을 유지합니다.".into());
    }
    item.receipt.sha256 = Some(sha);
    item.staged_snapshot = Some(core::snapshot(&path)?);
    item.receipt.status = "publish_intent".into();
    store.checkpoint(index, item.clone())?;
    assert_snapshot(&item.receipt.source, &item.original)?;
    exclusive_rename(&path, &item.receipt.target)?;
    let captured = core::snapshot(&item.receipt.target)?;
    if !same_file(&captured, item.staged_snapshot.as_ref().unwrap()) {
        return Err("공개한 복사본이 변경되었습니다. 원본을 유지합니다.".into());
    }
    item.captured = Some(captured);
    item.receipt.status = "copied".into();
    store.checkpoint(index, item)
}
fn execute_move(store: &mut Store, index: usize, cancel: &AtomicBool) -> AppResult<()> {
    create_staged_copy(store, index, cancel)?;
    let mut item = store.journal.items[index].clone();
    assert_snapshot(&item.receipt.source, &item.original)?;
    let backups = store
        .journal
        .root
        .join(APP)
        .join("backups")
        .join(&store.journal.id);
    make_dirs(&backups)?;
    let backup = backups.join(format!("{index}.original"));
    item.backup = Some(backup.clone());
    item.receipt.status = "retire_intent".into();
    store.checkpoint(index, item.clone())?;
    assert_snapshot(&item.receipt.source, &item.original)?;
    exclusive_rename(&item.receipt.source, &backup)?;
    let captured = core::snapshot(&backup)?;
    if !same_file(&captured, &item.original) {
        let rollback = exclusive_rename(&backup, &item.receipt.source);
        item.receipt.source_removed = rollback.is_err();
        item.backup_snapshot = Some(captured);
        store.checkpoint(index, item)?;
        return Err("이동 중 원본이 변경되었습니다. 관련 파일을 모두 보존했습니다.".into());
    }
    item.backup_snapshot = Some(captured);
    item.receipt.source_removed = true;
    item.receipt.status = "moved".into();
    store.checkpoint(index, item)
}

/// Execute one authenticated preview. Each file advances through durable intent
/// records; cancellation never deletes a partial copy or an ambiguous original.
pub fn execute(
    plan: &OperationPlan,
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<OperationReceipt> {
    verify(plan_key(), &plan_binding(plan)?, &plan.proof)?;
    if !valid_id(&plan.id) || plan.items.is_empty() || plan.items.len() > MAX_FILES {
        return Err("실행할 계획이 올바르지 않습니다.".into());
    }
    let root = root_path(&plan.root)?;
    if root != plan.root {
        return Err("선택 폴더가 변경되었습니다.".into());
    }
    let key = journal_key(&root, true)?;
    let _lock = OperationLock::acquire(&root, &key)?;
    for item in &plan.items {
        source_in(&root, &item.source)?;
        assert_snapshot(&item.source, &item.snapshot)?;
        if let Some((p, s)) = &item.keeper {
            assert_snapshot(p, s)?;
        }
    }
    let mut store = Store::create(plan, key)?;
    let total = plan.items.len();
    let mut cancelled = false;
    for index in 0..total {
        if cancel.load(Ordering::Relaxed) {
            cancelled = true;
            break;
        }
        let result = match plan.kind {
            TransferKind::Delete => execute_delete(&mut store, index, cancel),
            TransferKind::Copy => create_staged_copy(&mut store, index, cancel),
            TransferKind::Move => execute_move(&mut store, index, cancel),
        };
        if let Err(error) = result {
            let mut item = store.journal.items[index].clone();
            item.receipt.error = Some(error.clone());
            if item.receipt.status == "planned" {
                item.receipt.status = "failed".into();
            }
            store.checkpoint(index, item.clone())?;
            log(&root, &plan.id, "execute", &item.receipt, &error);
            // Continue neither a stale group nor a possibly failed storage
            // device. Previous successes remain independently recoverable.
            cancelled = cancel.load(Ordering::Relaxed);
            break;
        }
        log(
            &root,
            &plan.id,
            "execute",
            &store.journal.items[index].receipt,
            "파일 정리 완료",
        );
        progress(
            callback,
            "execute",
            index + 1,
            total,
            store.journal.items[index]
                .receipt
                .source
                .to_string_lossy()
                .into_owned(),
        );
    }
    let _ = store.compress_metadata();
    Ok(store.receipt(cancelled))
}

fn raw_recovery_path(journal: &Journal, item: &JournalItem) -> Option<PathBuf> {
    if let Some(path) = &item.compact_retired {
        if path.exists() {
            return Some(path.clone());
        }
    }
    if journal.kind == TransferKind::Delete {
        Some(item.receipt.target.clone())
    } else {
        item.backup.clone()
    }
}
fn raw_recovery_snapshot(journal: &Journal, item: &JournalItem) -> Option<FileSnapshot> {
    if item.compact_retired.as_ref().is_some_and(|p| p.exists()) {
        return item.compact_snapshot.clone();
    }
    if journal.kind == TransferKind::Delete {
        item.captured
            .clone()
            .or_else(|| Some(item.original.clone()))
    } else {
        item.backup_snapshot
            .clone()
            .or_else(|| Some(item.original.clone()))
    }
}

/// Read bounded summaries; no Rust journal is interpreted as a Python journal.
pub fn history(root: &Path) -> AppResult<Vec<HistoryRecord>> {
    let root = root_path(root)?;
    let directory = root.join(APP).join("history");
    plain(&directory)?;
    if !directory.exists() {
        return Ok(Vec::new());
    }
    let mut records = Vec::new();
    for entry in fs::read_dir(directory)
        .map_err(|e| err("기록 목록 읽기 실패", e))?
        .take(2000)
    {
        let entry = entry.map_err(|e| err("기록 읽기 실패", e))?;
        let id = entry.file_name().to_string_lossy().into_owned();
        if !valid_id(&id) {
            continue;
        }
        match Store::load(&root, &id) {
            Ok(store) => {
                let restored = store
                    .journal
                    .items
                    .iter()
                    .all(|i| i.receipt.status == "restored");
                let failed = store
                    .journal
                    .items
                    .iter()
                    .any(|i| i.receipt.error.is_some());
                let bytes = store.journal.items.iter().map(|i| i.receipt.size).sum();
                let packed_bytes = store
                    .journal
                    .items
                    .iter()
                    .filter_map(|i| i.recipe.as_ref())
                    .map(|p| p.chunks.iter().map(|c| c.stored_size as u64).sum::<u64>())
                    .sum();
                records.push(HistoryRecord {
                    id,
                    kind: store.journal.kind,
                    status: if restored {
                        "복원 완료"
                    } else if failed {
                        "일부 완료·확인 필요"
                    } else {
                        "복원 가능"
                    }
                    .into(),
                    created_at: store.journal.created_at,
                    files: store.journal.items.len(),
                    bytes,
                    packed_bytes,
                    restored,
                });
            }
            Err(error) => records.push(HistoryRecord {
                id,
                kind: TransferKind::Delete,
                status: format!("기록 검증 실패: {error}"),
                created_at: 0,
                files: 0,
                bytes: 0,
                packed_bytes: 0,
                restored: false,
            }),
        }
    }
    records.sort_by(|a, b| b.created_at.cmp(&a.created_at));
    Ok(records)
}

/// Execution logs are diagnostic only; they never authorize file restoration.
pub fn logs(root: &Path) -> AppResult<Vec<LogRecord>> {
    let root = root_path(root)?;
    let path = root.join(APP).join("execution.jsonl");
    plain(&path)?;
    if !path.exists() {
        return Ok(Vec::new());
    }
    let bytes = bounded_read(&path, 20 * 1024 * 1024)?;
    let mut records: Vec<_> = bytes
        .split(|b| *b == b'\n')
        .rev()
        .filter(|b| !b.is_empty())
        .take(10_000)
        .filter_map(|line| serde_json::from_slice::<LogRecord>(line).ok())
        .collect();
    records.reverse();
    Ok(records)
}

fn object_path(root: &Path, chunk: &ChunkRef) -> AppResult<PathBuf> {
    if chunk.sha256.len() != 64
        || !chunk
            .sha256
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        || chunk.raw_size as usize > CHUNK
        || chunk.stored_size as usize > CHUNK + 1024
    {
        return Err("압축 복원 조각의 설명이 손상되었습니다.".into());
    }
    Ok(root
        .join(APP)
        .join("objects")
        .join(&chunk.sha256[..2])
        .join(format!(
            "{}.{}",
            chunk.sha256,
            if chunk.compressed { "z" } else { "raw" }
        )))
}
fn chunk_bytes(path: &Path, chunk: &ChunkRef) -> AppResult<Vec<u8>> {
    let bytes = bounded_read(path, CHUNK as u64 + 1024)?;
    if bytes.len() != chunk.stored_size as usize {
        return Err("압축 복원 조각의 크기가 다릅니다.".into());
    }
    let raw = if chunk.compressed {
        let decoder = ZlibDecoder::new(bytes.as_slice());
        let mut raw = Vec::new();
        decoder
            .take(CHUNK as u64 + 1)
            .read_to_end(&mut raw)
            .map_err(|e| err("복원 조각 압축 해제 실패", e))?;
        raw
    } else {
        bytes
    };
    if raw.len() != chunk.raw_size as usize || digest(&raw) != chunk.sha256 {
        return Err("복원 조각의 내용 검증에 실패했습니다.".into());
    }
    Ok(raw)
}
fn materialize(
    root: &Path,
    recipe: &PackedFile,
    target: &Path,
    cancel: &AtomicBool,
) -> AppResult<FileSnapshot> {
    if recipe.chunks.len() > MAX_CHUNKS || recipe.size > MAX_CHUNKS as u64 * CHUNK as u64 {
        return Err("압축 복원 데이터 한도를 초과했습니다.".into());
    }
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)
        .map_err(|e| err("복원 준비 파일 생성 실패", e))?;
    let mut hash = Sha256::new();
    let mut total = 0;
    for chunk in &recipe.chunks {
        if cancel.load(Ordering::Relaxed) {
            return Err("복원을 취소했습니다. 기존 보관 데이터는 유지합니다.".into());
        }
        let bytes = chunk_bytes(&object_path(root, chunk)?, chunk)?;
        total += bytes.len() as u64;
        if total > recipe.size {
            return Err("압축 복원 설명의 크기가 올바르지 않습니다.".into());
        }
        hash.update(&bytes);
        output
            .write_all(&bytes)
            .map_err(|e| err("복원 데이터 쓰기 실패", e))?;
    }
    output
        .sync_all()
        .map_err(|e| err("복원 데이터 저장 실패", e))?;
    drop(output);
    if total != recipe.size || hex(&hash.finalize()) != recipe.sha256 {
        return Err("전체 복원 내용이 원본과 일치하지 않습니다.".into());
    }
    if hash_file(target, cancel)? != recipe.sha256 {
        return Err("저장한 복원 데이터 검증에 실패했습니다.".into());
    }
    core::snapshot(target)
}
fn original_modified_time(nanos: i128) -> AppResult<SystemTime> {
    let absolute = nanos
        .checked_abs()
        .ok_or_else(|| "수정일 범위를 벗어났습니다.".to_string())?;
    let seconds = u64::try_from(absolute / 1_000_000_000)
        .map_err(|_| "수정일 범위를 벗어났습니다.".to_string())?;
    let duration = Duration::new(seconds, (absolute % 1_000_000_000) as u32);
    if nanos < 0 {
        UNIX_EPOCH.checked_sub(duration)
    } else {
        UNIX_EPOCH.checked_add(duration)
    }
    .ok_or_else(|| "이 운영체제가 지원하지 않는 수정일입니다.".into())
}
fn resolve_captured(
    item: &JournalItem,
    path: &Path,
    expected: Option<&FileSnapshot>,
    cancel: &AtomicBool,
) -> AppResult<FileSnapshot> {
    let current = core::snapshot(path)?;
    if let Some(expected) = expected {
        assert_snapshot(path, expected)?;
    } else if !same_file(&current, &item.original)
        && !item
            .staged_snapshot
            .as_ref()
            .is_some_and(|s| same_file(s, &current))
    {
        return Err("중단된 작업의 보관 파일을 확인할 수 없습니다.".into());
    }
    if let Some(sha) = &item.receipt.sha256 {
        if hash_file(path, cancel)? != *sha {
            return Err("정리 후 변경된 파일은 자동 복원하지 않습니다.".into());
        }
    }
    Ok(current)
}
fn retire_published_target(store: &mut Store, index: usize, cancel: &AtomicBool) -> AppResult<()> {
    let mut item = store.journal.items[index].clone();
    if !item.receipt.target.exists() {
        return Ok(());
    }
    let current = resolve_captured(&item, &item.receipt.target, item.captured.as_ref(), cancel)?;
    let parent = item
        .receipt
        .target
        .parent()
        .ok_or_else(|| "대상 경로 오류".to_string())?;
    let backups = parent
        .join(APP)
        .join("undo-targets")
        .join(&store.journal.id);
    make_dirs(&backups)?;
    let retired = backups.join(format!("{index}.copy"));
    item.retired_target = Some(retired.clone());
    item.receipt.status = "undo_target_intent".into();
    store.checkpoint(index, item.clone())?;
    assert_snapshot(&item.receipt.target, &current)?;
    exclusive_rename(&item.receipt.target, &retired)?;
    if !same_file(&core::snapshot(&retired)?, &current) {
        return Err("복원 중 대상이 변경되었습니다. 관련 파일을 보존했습니다.".into());
    }
    store.checkpoint(index, item)
}
fn restore_item(store: &mut Store, index: usize, cancel: &AtomicBool) -> AppResult<()> {
    let mut item = store.journal.items[index].clone();
    if item.receipt.status == "restored" {
        return Ok(());
    }
    let kind = store.journal.kind;
    if item.receipt.source.exists()
        && (item.receipt.status == "restore_publish_intent" || item.restored_snapshot.is_some())
    {
        let expected = item
            .restored_snapshot
            .as_ref()
            .or(item.staged_snapshot.as_ref())
            .ok_or_else(|| "복원 게시 정보를 찾을 수 없습니다.".to_string())?;
        let actual = core::snapshot(&item.receipt.source)?;
        if item
            .restored_snapshot
            .as_ref()
            .is_some_and(|s| *s != actual)
            || !same_file(expected, &actual)
        {
            return Err("원래 경로에 다른 파일이 있습니다. 덮어쓰지 않습니다.".into());
        }
        if let Some(sha) = &item.receipt.sha256 {
            if hash_file(&item.receipt.source, cancel)? != *sha {
                return Err("복원한 원본이 변경되었습니다.".into());
            }
        }
        item.restored_snapshot = Some(actual);
        item.receipt.source_removed = false;
        store.checkpoint(index, item.clone())?;
        if kind != TransferKind::Delete {
            retire_published_target(store, index, cancel)?;
        }
        item = store.journal.items[index].clone();
        item.receipt.status = "restored".into();
        item.receipt.error = None;
        return store.checkpoint(index, item);
    }
    if kind == TransferKind::Copy {
        retire_published_target(store, index, cancel)?;
        item = store.journal.items[index].clone();
        item.receipt.status = "restored".into();
        item.receipt.error = None;
        return store.checkpoint(index, item);
    }
    let raw = raw_recovery_path(&store.journal, &item).filter(|p| p.exists());
    let needs_restore = item.receipt.source_removed
        || raw.is_some()
        || item.recipe.is_some()
        || (kind == TransferKind::Delete
            && item.receipt.target.exists()
            && item.receipt.status == "capture_intent");
    if !needs_restore {
        if kind == TransferKind::Move {
            retire_published_target(store, index, cancel)?;
        }
        item = store.journal.items[index].clone();
        item.receipt.status = "restored".into();
        item.receipt.error = None;
        return store.checkpoint(index, item);
    }
    if item.receipt.source.exists() {
        return Err("원래 경로에 파일이 있습니다. 덮어쓰지 않습니다.".into());
    }
    if kind == TransferKind::Move && item.receipt.target.exists() {
        resolve_captured(&item, &item.receipt.target, item.captured.as_ref(), cancel)?;
    }
    let parent = item
        .receipt
        .source
        .parent()
        .ok_or_else(|| "원본 경로 오류".to_string())?;
    make_dirs(parent)?;
    if let Some(raw) = raw {
        let expected = raw_recovery_snapshot(&store.journal, &item);
        let expected = expected.as_ref().filter(|_| {
            item.captured.is_some()
                || item.backup_snapshot.is_some()
                || item.compact_snapshot.is_some()
        });
        let current = resolve_captured(&item, &raw, expected, cancel)?;
        item.staging = Some(raw.clone());
        item.staged_snapshot = Some(current.clone());
        item.receipt.status = "restore_publish_intent".into();
        store.checkpoint(index, item.clone())?;
        assert_snapshot(&raw, &current)?;
        exclusive_rename(&raw, &item.receipt.source)?;
        let restored = core::snapshot(&item.receipt.source)?;
        if !same_file(&restored, &current) {
            return Err("복원 중 원본이 변경되었습니다. 파일을 보존했습니다.".into());
        }
        item.restored_snapshot = Some(restored);
        item.receipt.source_removed = false;
        store.checkpoint(index, item.clone())?;
    } else if let Some(recipe) = item.recipe.clone() {
        let staging = store
            .journal
            .root
            .join(APP)
            .join("restore-staging")
            .join(&store.journal.id);
        make_dirs(&staging)?;
        let temp = staging.join(format!("{}-{}.restore", index, random_id()?));
        item.staging = Some(temp.clone());
        item.receipt.status = "restore_prepare".into();
        store.checkpoint(index, item.clone())?;
        materialize(&store.journal.root, &recipe, &temp, cancel)?;
        let f = OpenOptions::new()
            .write(true)
            .open(&temp)
            .map_err(|e| err("복원 날짜 보존 실패", e))?;
        let modified = original_modified_time(item.original.modified_ns)?;
        f.set_times(fs::FileTimes::new().set_modified(modified))
            .map_err(|e| err("복원 수정일 설정 실패", e))?;
        let restored = core::snapshot(&temp)?;
        drop(f);
        item.staged_snapshot = Some(restored.clone());
        item.receipt.status = "restore_publish_intent".into();
        store.checkpoint(index, item.clone())?;
        assert_snapshot(&temp, &restored)?;
        exclusive_rename(&temp, &item.receipt.source)?;
        let final_snapshot = core::snapshot(&item.receipt.source)?;
        if !same_file(&restored, &final_snapshot) {
            return Err("복원 게시 중 파일이 변경되었습니다.".into());
        }
        item.restored_snapshot = Some(final_snapshot);
        item.receipt.source_removed = false;
        store.checkpoint(index, item.clone())?;
    } else {
        return Err("복원할 원본 데이터가 없습니다. 기록만으로 내용을 만들 수 없습니다.".into());
    }
    if kind == TransferKind::Move {
        retire_published_target(store, index, cancel)?;
        #[cfg(test)]
        if TEST_UNDO_AFTER_RETIRE.with(|flag| flag.replace(false)) {
            return Err("injected interruption after undo target capture".into());
        }
    }
    item = store.journal.items[index].clone();
    item.receipt.status = "restored".into();
    item.receipt.error = None;
    store.checkpoint(index, item)
}

/// Restore without overwriting a conflicting new file or changed backup.
pub fn undo(
    root: &Path,
    id: &str,
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<OperationReceipt> {
    let root = root_path(root)?;
    let key = journal_key(&root, false)?;
    let _lock = OperationLock::acquire(&root, &key)?;
    let mut store = Store::load(&root, id)?;
    let count = store.journal.items.len();
    let mut cancelled = false;
    for index in (0..count).rev() {
        if cancel.load(Ordering::Relaxed) {
            cancelled = true;
            break;
        }
        if let Err(error) = restore_item(&mut store, index, cancel) {
            let mut item = store.journal.items[index].clone();
            item.receipt.error = Some(error.clone());
            store.checkpoint(index, item.clone())?;
            log(&root, id, "undo", &item.receipt, &error);
            continue;
        }
        log(
            &root,
            id,
            "undo",
            &store.journal.items[index].receipt,
            "원래 위치로 복원 완료",
        );
        progress(
            callback,
            "restore",
            count - index,
            count,
            store.journal.items[index]
                .receipt
                .source
                .to_string_lossy()
                .into_owned(),
        );
    }
    let _ = store.compress_metadata();
    Ok(store.receipt(cancelled))
}

struct PreparedPack {
    recipe: PackedFile,
    objects: Vec<(PathBuf, ChunkRef, FileSnapshot)>,
    added_bytes: u64,
}
impl Drop for PreparedPack {
    fn drop(&mut self) {
        // Only unpublished, exclusively created private staging objects can be
        // cleaned up. Unexpected modifications remain preserved for inspection.
        for (path, _, snapshot) in &self.objects {
            if core::snapshot(path).is_ok_and(|current| current == *snapshot) {
                let _ = fs::remove_file(path);
            }
        }
    }
}
fn prepare_pack(
    root: &Path,
    path: &Path,
    expected: &FileSnapshot,
    shared: bool,
    cancel: &AtomicBool,
) -> AppResult<Option<PreparedPack>> {
    if expected.size < 4096 || expected.size > MAX_CHUNKS as u64 * CHUNK as u64 {
        return Ok(None);
    }
    let started = Instant::now();
    let stage = root.join(APP).join("pack-staging").join(random_id()?);
    make_dirs(&stage)?;
    let mut reader = core::safe_open(path)?;
    if core::snapshot_open_file(&reader)? != *expected {
        return Err("압축 전에 보관 파일이 변경되었습니다.".into());
    }
    let mut prepared = PreparedPack {
        recipe: PackedFile {
            sha256: String::new(),
            size: expected.size,
            chunks: Vec::new(),
        },
        objects: Vec::new(),
        added_bytes: 0,
    };
    let mut unique: HashMap<String, ChunkRef> = HashMap::new();
    let mut full = Sha256::new();
    let mut total = 0u64;
    let mut buffer = vec![0; CHUNK];
    loop {
        if cancel.load(Ordering::Relaxed) || started.elapsed() > Duration::from_secs(15) {
            return Ok(None);
        }
        let mut filled = 0;
        while filled < buffer.len() {
            let n = reader
                .read(&mut buffer[filled..])
                .map_err(|e| err("압축 원본 읽기 실패", e))?;
            if n == 0 {
                break;
            }
            filled += n;
        }
        if filled == 0 {
            break;
        }
        if prepared.recipe.chunks.len() >= MAX_CHUNKS {
            return Ok(None);
        }
        total += filled as u64;
        if total > expected.size {
            return Err("압축 중 원본 크기가 바뀌었습니다.".into());
        }
        let bytes = &buffer[..filled];
        full.update(bytes);
        let sha = digest(bytes);
        let chunk = if let Some(chunk) = unique.get(&sha) {
            chunk.clone()
        } else {
            let mut encoder = ZlibEncoder::new(Vec::new(), Compression::fast());
            encoder
                .write_all(bytes)
                .map_err(|e| err("복원 데이터 압축 실패", e))?;
            let packed = encoder
                .finish()
                .map_err(|e| err("복원 데이터 압축 실패", e))?;
            let compressed = packed.len() + 32 < bytes.len();
            let stored = if compressed { packed.as_slice() } else { bytes };
            let mut chunk = ChunkRef {
                sha256: sha.clone(),
                raw_size: filled as u32,
                stored_size: stored.len() as u32,
                compressed,
            };
            let object = object_path(root, &chunk)?;
            if object.exists() {
                let length = fs::metadata(&object)
                    .map_err(|e| err("기존 복원 조각 확인 실패", e))?
                    .len();
                if length > CHUNK as u64 + 1024 {
                    return Err("기존 복원 조각이 손상되었습니다.".into());
                }
                chunk.stored_size = length as u32;
                chunk_bytes(&object, &chunk)?;
            } else {
                let temp = stage.join(format!("{}.{}", sha, if compressed { "z" } else { "raw" }));
                write_new(&temp, stored)?;
                let state = core::snapshot(&temp)?;
                prepared.added_bytes += stored.len() as u64;
                prepared.objects.push((temp, chunk.clone(), state));
            }
            unique.insert(sha, chunk.clone());
            chunk
        };
        prepared.recipe.chunks.push(chunk);
    }
    if total != expected.size || core::snapshot_open_file(&reader)? != *expected {
        return Err("압축 중 원본이 변경되었습니다.".into());
    }
    assert_snapshot(path, expected)?;
    prepared.recipe.sha256 = hex(&full.finalize());
    let metadata = serde_json::to_vec(&prepared.recipe)
        .map_err(|e| err("압축 설명 기록 실패", e))?
        .len() as u64;
    if metadata > MAX_RECIPE {
        return Ok(None);
    }
    let cost = prepared.added_bytes.saturating_add(metadata);
    if !shared && cost > expected.size.saturating_mul(9) / 10 {
        return Ok(None);
    }
    // Equal full-content members can share incompressible chunks. A first
    // member may pay the object cost; subsequent members pay descriptors only.
    if shared && cost > expected.size.saturating_add(metadata) {
        return Ok(None);
    }
    Ok(Some(prepared))
}
fn verify_recipe(root: &Path, recipe: &PackedFile, cancel: &AtomicBool) -> AppResult<()> {
    let mut hash = Sha256::new();
    let mut total = 0u64;
    for chunk in &recipe.chunks {
        if cancel.load(Ordering::Relaxed) {
            return Err("압축 검증을 취소했습니다. 원본 보관본은 유지합니다.".into());
        }
        let raw = chunk_bytes(&object_path(root, chunk)?, chunk)?;
        hash.update(&raw);
        total += raw.len() as u64;
        if total > recipe.size {
            return Err("압축 조각의 합계가 원본 크기를 넘습니다.".into());
        }
    }
    if total != recipe.size || hex(&hash.finalize()) != recipe.sha256 {
        return Err("독립적인 압축 복원 데이터가 원본과 다릅니다.".into());
    }
    Ok(())
}

/// The chunk recipe represents the primary data stream only. Never retire an
/// encrypted file or any named stream (including a zero-byte stream). Failure
/// to enumerate is lack of proof, rather than evidence that no streams exist.
#[cfg(windows)]
fn packing_preservation_warning(path: &Path) -> Option<String> {
    use std::os::windows::{ffi::OsStrExt, fs::MetadataExt};
    use windows_sys::Win32::{
        Foundation::{GetLastError, ERROR_HANDLE_EOF, INVALID_HANDLE_VALUE},
        Storage::FileSystem::{
            FindClose, FindFirstStreamW, FindNextStreamW, FindStreamInfoStandard,
            WIN32_FIND_STREAM_DATA,
        },
    };
    let metadata = match fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(error) => {
            return Some(format!(
                "추가 데이터 확인 실패로 원시 보관본을 유지합니다: {error}"
            ))
        }
    };
    if metadata.file_attributes() & 0x4000 != 0 {
        return Some(
            "EFS 암호화 파일은 복원 특성을 유지하도록 압축하지 않고 원시 보관합니다.".into(),
        );
    }
    if !metadata.is_file() || metadata.file_type().is_symlink() || is_reparse(&metadata) {
        return Some("일반 파일임을 확인할 수 없어 원시 보관본을 유지합니다.".into());
    }
    let name: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
    let mut data: WIN32_FIND_STREAM_DATA = unsafe { std::mem::zeroed() };
    let handle = unsafe {
        FindFirstStreamW(
            name.as_ptr(),
            FindStreamInfoStandard,
            &mut data as *mut _ as *mut std::ffi::c_void,
            0,
        )
    };
    if handle == INVALID_HANDLE_VALUE {
        return Some(format!(
            "추가 스트림 조회를 지원하지 않거나 접근할 수 없어 원시 보관합니다 (Windows {}).",
            unsafe { GetLastError() }
        ));
    }
    struct StreamSearch(windows_sys::Win32::Foundation::HANDLE);
    impl Drop for StreamSearch {
        fn drop(&mut self) {
            unsafe {
                FindClose(self.0);
            }
        }
    }
    let _search = StreamSearch(handle);
    for _ in 0..4096 {
        let length = data
            .cStreamName
            .iter()
            .position(|c| *c == 0)
            .unwrap_or(data.cStreamName.len());
        let stream = match String::from_utf16(&data.cStreamName[..length]) {
            Ok(s) => s,
            Err(_) => return Some("추가 스트림 이름을 확인할 수 없어 원시 보관합니다.".into()),
        };
        if !stream.eq_ignore_ascii_case("::$DATA") {
            return Some(
                "NTFS 추가 데이터 스트림이 있어 해당 파일은 원시 보관본으로 유지합니다.".into(),
            );
        }
        if unsafe { FindNextStreamW(handle, &mut data as *mut _ as *mut std::ffi::c_void) } == 0 {
            let code = unsafe { GetLastError() };
            return if code == ERROR_HANDLE_EOF {
                None
            } else {
                Some(format!(
                    "추가 스트림 조회가 완료되지 않아 원시 보관합니다 (Windows {code})."
                ))
            };
        }
    }
    Some("추가 스트림 조회 한도에 도달해 원시 보관본을 유지합니다.".into())
}
#[cfg(not(windows))]
fn packing_preservation_warning(_path: &Path) -> Option<String> {
    None
}

enum PackOutcome {
    Skipped,
    KeptRaw(String),
    Packed(u64, u64),
}
fn keep_unsupported_capture(
    store: &mut Store,
    index: usize,
    mut item: JournalItem,
    raw: &Path,
    retired: &Path,
    warning: String,
) -> AppResult<PackOutcome> {
    // Clear the incomplete representation durably before trying to put the
    // complete raw file back. A failed rename cannot leave the recipe trusted.
    item.recipe = None;
    item.compact_snapshot = Some(core::snapshot(retired)?);
    item.receipt.status = "pack_raw_preserve_intent".into();
    store.checkpoint(index, item.clone())?;
    if !raw.exists() {
        exclusive_rename(retired, raw)?;
        let restored_raw = core::snapshot(raw)?;
        item.compact_retired = None;
        item.compact_snapshot = None;
        if store.journal.kind == TransferKind::Delete {
            item.captured = Some(restored_raw);
        } else {
            item.backup_snapshot = Some(restored_raw);
        }
    }
    item.receipt.status = if store.journal.kind == TransferKind::Delete {
        "deleted"
    } else {
        "moved"
    }
    .into();
    item.receipt.error = None;
    store.checkpoint(index, item)?;
    Ok(PackOutcome::KeptRaw(warning))
}
fn compact_item(
    store: &mut Store,
    index: usize,
    shared: bool,
    cancel: &AtomicBool,
) -> AppResult<PackOutcome> {
    let mut item = store.journal.items[index].clone();
    if item.receipt.status == "restored" || item.recipe.is_some() {
        return Ok(PackOutcome::Skipped);
    }
    let Some(raw) = raw_recovery_path(&store.journal, &item).filter(|p| p.exists()) else {
        return Ok(PackOutcome::Skipped);
    };
    if let Some(warning) = packing_preservation_warning(&raw) {
        return Ok(PackOutcome::KeptRaw(warning));
    }
    let expected = raw_recovery_snapshot(&store.journal, &item);
    let expected = expected
        .as_ref()
        .filter(|_| item.captured.is_some() || item.backup_snapshot.is_some());
    let before = resolve_captured(&item, &raw, expected, cancel)?;
    let Some(prepared) = prepare_pack(&store.journal.root, &raw, &before, shared, cancel)? else {
        return Ok(PackOutcome::Skipped);
    };
    if let Some(expected) = &item.receipt.sha256 {
        if *expected != prepared.recipe.sha256 {
            return Err("보관 데이터가 기존 원본 해시와 다릅니다.".into());
        }
    }
    item.receipt.sha256 = Some(prepared.recipe.sha256.clone());
    item.recipe = Some(prepared.recipe.clone());
    item.receipt.status = "pack_prepare".into();
    store.checkpoint(index, item.clone())?;
    // The descriptor is durable before any new object becomes shared.
    for (staged, chunk, state) in &prepared.objects {
        if cancel.load(Ordering::Relaxed) {
            return Err("압축을 취소했습니다. 원본 파일은 유지합니다.".into());
        }
        let object = object_path(&store.journal.root, chunk)?;
        make_dirs(object.parent().unwrap())?;
        if object.exists() {
            chunk_bytes(&object, chunk)?;
        } else {
            assert_snapshot(staged, state)?;
            exclusive_rename(staged, &object)?;
            chunk_bytes(&object, chunk)?;
        }
    }
    verify_recipe(&store.journal.root, &prepared.recipe, cancel)?;
    assert_snapshot(&raw, &before)?;
    let retired_dir = store
        .journal
        .root
        .join(APP)
        .join("packed-retirement")
        .join(&store.journal.id);
    make_dirs(&retired_dir)?;
    let retired = retired_dir.join(format!("{index}-{}.raw", random_id()?));
    item.compact_retired = Some(retired.clone());
    item.receipt.status = "pack_capture_intent".into();
    store.checkpoint(index, item.clone())?;
    assert_snapshot(&raw, &before)?;
    exclusive_rename(&raw, &retired)?;
    let captured = core::snapshot(&retired)?;
    if !same_file(&captured, &before) {
        let _ = exclusive_rename(&retired, &raw);
        return Err("압축 보관본이 변경되어 원시 데이터를 보존했습니다.".into());
    }
    item.compact_snapshot = Some(captured.clone());
    item.receipt.status = "pack_retire_intent".into();
    store.checkpoint(index, item.clone())?;
    // The random private capture is app-owned. Unexpected writes still prevent
    // retirement; live keepers are never consulted by this independent recipe.
    if let Some(warning) = packing_preservation_warning(&retired) {
        return keep_unsupported_capture(store, index, item, &raw, &retired, warning);
    }
    if hash_file(&retired, cancel)? != prepared.recipe.sha256 {
        return Err("압축 중 새 변경을 발견해 원시 보관본을 유지합니다.".into());
    }
    if let Some(warning) = packing_preservation_warning(&retired) {
        // A stream/encryption attribute may have appeared after preparation.
        // Retain all bytes. Prefer putting the capture back at its raw path;
        // if a new file now occupies that name, preserve the private capture.
        return keep_unsupported_capture(store, index, item, &raw, &retired, warning);
    }
    assert_snapshot(&retired, &captured)?;
    #[cfg(test)]
    TEST_DURABILITY_EVENTS
        .with(|events| events.borrow_mut().push(("retire".into(), retired.clone())));
    fs::remove_file(&retired).map_err(|e| err("압축 원시 보관본을 유지합니다", e))?;
    sync_directory(retired.parent().unwrap())?;
    item.receipt.status = "compacted".into();
    item.receipt.error = None;
    store.checkpoint(index, item)?;
    let recipe_bytes = serde_json::to_vec(&prepared.recipe)
        .map_err(|e| err("압축 정보 크기 확인 실패", e))?
        .len() as u64;
    Ok(PackOutcome::Packed(
        before.size,
        prepared.added_bytes + recipe_bytes,
    ))
}

/// Explicit recovery-data optimization, with an efficient raw fallback.
/// Metadata alone never substitutes for independent original byte chunks.
pub fn compact(
    root: &Path,
    id: &str,
    cancel: &AtomicBool,
    callback: Option<&dyn Fn(Progress)>,
) -> AppResult<CompactionResult> {
    let root = root_path(root)?;
    let key = journal_key(&root, false)?;
    let _lock = OperationLock::acquire(&root, &key)?;
    let mut store = Store::load(&root, id)?;
    if store.journal.kind == TransferKind::Copy {
        return Err("복사 대상은 사용자 파일입니다. 압축 최적화하지 않습니다.".into());
    }
    let mut counts: HashMap<String, usize> = HashMap::new();
    for item in &store.journal.items {
        if let Some(sha) = &item.receipt.sha256 {
            *counts.entry(sha.clone()).or_default() += 1;
        }
    }
    let total = store.journal.items.len();
    let mut result = CompactionResult::default();
    for index in 0..total {
        if cancel.load(Ordering::Relaxed) {
            break;
        }
        let shared = store.journal.items[index]
            .receipt
            .sha256
            .as_ref()
            .is_some_and(|sha| counts.get(sha).copied().unwrap_or(0) > 1);
        match compact_item(&mut store, index, shared, cancel) {
            Ok(PackOutcome::Packed(original, stored)) => {
                result.files += 1;
                result.original_bytes += original;
                result.stored_bytes += stored;
                log(
                    &root,
                    id,
                    "compact",
                    &store.journal.items[index].receipt,
                    "독립적인 복원 조각을 검증해 압축 보관 완료",
                );
            }
            Ok(PackOutcome::Skipped) => (),
            Ok(PackOutcome::KeptRaw(warning)) => {
                log(
                    &root,
                    id,
                    "compact",
                    &store.journal.items[index].receipt,
                    &warning,
                );
                result.warnings.push(format!(
                    "{}: {warning}",
                    store.journal.items[index].receipt.source.display()
                ));
            }
            Err(error) => {
                let mut item = store.journal.items[index].clone();
                item.receipt.error = Some(error.clone());
                store.checkpoint(index, item.clone())?;
                log(&root, id, "compact", &item.receipt, &error);
                return Err(error);
            }
        }
        progress(
            callback,
            "compact",
            index + 1,
            total,
            store.journal.items[index]
                .receipt
                .source
                .to_string_lossy()
                .into_owned(),
        );
    }
    result.saved_bytes = result.original_bytes.saturating_sub(result.stored_bytes);
    let _ = store.compress_metadata();
    Ok(result)
}

#[cfg(test)]
mod storage_failure_v1_unit {
    use super::*;
    struct Scratch(PathBuf);
    impl Scratch {
        fn new() -> Self {
            let p =
                std::env::temp_dir().join(format!("rust-operation-unit-{}", random_id().unwrap()));
            fs::create_dir(&p).unwrap();
            Self(p)
        }
        fn scan(&self) -> ScanResult {
            core::scan(
                &self.0,
                &core::ScanOptions::default(),
                &AtomicBool::new(false),
                None,
            )
            .unwrap()
        }
    }
    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn appended_complete_event_with_sync_failure_poison_stops_stale_chain_append() {
        let f = Scratch::new();
        let path = f.0.join("a.txt");
        fs::write(&path, b"unchanged").unwrap();
        let plan = plan_files(&f.scan(), &[path.clone()], None, TransferKind::Delete).unwrap();
        let key = journal_key(&f.0, true).unwrap();
        let mut store = Store::create(&plan, key).unwrap();
        let mut item = store.journal.items[0].clone();
        item.receipt.status = "capture_intent".into();
        TEST_FAIL_SYNC.with(|flag| flag.set(true));
        assert!(store.checkpoint(0, item.clone()).is_err());
        assert!(store.poisoned);
        let size = fs::metadata(store.directory.join(&store.event_file))
            .unwrap()
            .len();
        assert!(store.checkpoint(0, item).is_err());
        assert_eq!(
            fs::metadata(store.directory.join(&store.event_file))
                .unwrap()
                .len(),
            size
        );
        assert_eq!(fs::read(&path).unwrap(), b"unchanged");
        let mut recovered = Store::load(&f.0, &plan.id).unwrap();
        assert_eq!(recovered.sequence, 1);
        let mut item = recovered.journal.items[0].clone();
        item.receipt.status = "planned".into();
        recovered.checkpoint(0, item).unwrap();
        assert_eq!(Store::load(&f.0, &plan.id).unwrap().sequence, 2);
    }

    #[test]
    fn packed_move_undo_resumes_after_target_retirement_checkpoint() {
        let f = Scratch::new();
        let path = f.0.join("a.txt");
        let payload = vec![b'x'; 64 * 1024];
        fs::write(&path, &payload).unwrap();
        let destination = f.0.join("destination");
        fs::create_dir(&destination).unwrap();
        let cancel = AtomicBool::new(false);
        let plan = plan_files(
            &f.scan(),
            &[path.clone()],
            Some(&destination),
            TransferKind::Move,
        )
        .unwrap();
        let receipt = execute(&plan, &cancel, None).unwrap();
        assert!(receipt.completed);
        assert_eq!(compact(&f.0, &receipt.id, &cancel, None).unwrap().files, 1);
        TEST_UNDO_AFTER_RETIRE.with(|flag| flag.set(true));
        let interrupted = undo(&f.0, &receipt.id, &cancel, None).unwrap();
        assert!(!interrupted.completed);
        assert_eq!(fs::read(&path).unwrap(), payload);
        assert!(!receipt.items[0].target.exists());
        let resumed = undo(&f.0, &receipt.id, &cancel, None).unwrap();
        assert!(resumed.completed, "{:?}", resumed.errors);
        assert_eq!(fs::read(path).unwrap(), payload);
    }

    #[test]
    fn newly_published_chunk_directory_is_synced_before_original_retirement() {
        let f = Scratch::new();
        let path = f.0.join("a.txt");
        fs::write(&path, vec![b'x'; 64 * 1024]).unwrap();
        let cancel = AtomicBool::new(false);
        let plan = plan_files(&f.scan(), &[path], None, TransferKind::Delete).unwrap();
        let receipt = execute(&plan, &cancel, None).unwrap();
        assert!(receipt.completed);
        TEST_DURABILITY_EVENTS.with(|events| events.borrow_mut().clear());
        assert_eq!(compact(&f.0, &receipt.id, &cancel, None).unwrap().files, 1);
        let events = TEST_DURABILITY_EVENTS.with(|events| events.borrow().clone());
        let objects = f.0.join(APP).join("objects");
        let publication = events
            .iter()
            .position(|(kind, path)| kind == "rename" && path.starts_with(&objects))
            .unwrap();
        let retirement = events
            .iter()
            .position(|(kind, _)| kind == "retire")
            .unwrap();
        let parent = events[publication].1.parent().unwrap();
        assert!(events
            .iter()
            .enumerate()
            .any(|(i, (kind, path))| i > publication
                && i < retirement
                && kind == "sync"
                && path == parent));
    }

    #[test]
    fn packed_restore_preserves_signed_pre_epoch_modification_date() {
        let f = Scratch::new();
        let path = f.0.join("a.txt");
        fs::write(&path, vec![b'x'; 64 * 1024]).unwrap();
        let original_time = UNIX_EPOCH - Duration::new(12345, 678900000);
        let file = OpenOptions::new().write(true).open(&path).unwrap();
        file.set_times(fs::FileTimes::new().set_modified(original_time))
            .unwrap();
        drop(file);
        let expected = core::snapshot(&path).unwrap().modified_ns;
        assert!(expected < 0);
        let cancel = AtomicBool::new(false);
        let plan = plan_files(&f.scan(), &[path.clone()], None, TransferKind::Delete).unwrap();
        let receipt = execute(&plan, &cancel, None).unwrap();
        assert!(receipt.completed);
        assert_eq!(compact(&f.0, &receipt.id, &cancel, None).unwrap().files, 1);
        assert!(undo(&f.0, &receipt.id, &cancel, None).unwrap().completed);
        assert_eq!(core::snapshot(&path).unwrap().modified_ns, expected);
    }
}
