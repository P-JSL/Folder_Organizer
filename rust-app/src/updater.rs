//! Rust-only, staged Windows updates. The independent helper retains the old
//! app directory and refuses links, personal files, stale plans and overwrites.
use crate::core::{self, AppResult, Progress};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

pub const APP_ID: &str = "P-JSL/Folder_Organizer.Rust";
pub const APP_DIRECTORY: &str = "FolderOrganizerRust";
pub const APP_EXECUTABLE: &str = "FolderOrganizerRust.exe";
pub const HELPER_EXECUTABLE: &str = "FolderOrganizerRustUpdater.exe";
pub const CLI_EXECUTABLE: &str = "FolderOrganizerRustCLI.exe";
pub const WINDOWS_ASSET: &str = "FolderOrganizer-Rust-Windows-x64.zip";
pub const CHECKSUM_ASSET: &str = "SHA256SUMS.txt";
pub const LATEST_RELEASE_API: &str =
    "https://api.github.com/repos/P-JSL/Folder_Organizer/releases/latest";
pub const MAX_ARCHIVE_BYTES: u64 = 768 * 1024 * 1024;
pub const MAX_UNPACKED_BYTES: u64 = 2 * 1024 * 1024 * 1024;
pub const MAX_ENTRY_BYTES: u64 = 512 * 1024 * 1024;
pub const MAX_MANIFEST_FILES: usize = 15_000;
const MAX_PLAN_BYTES: u64 = 5 * 1024 * 1024;
const MAX_API_BYTES: u64 = 2 * 1024 * 1024;
const MAX_CHECKSUM_BYTES: u64 = 64 * 1024;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReleaseInfo {
    pub version: String,
    pub tag_name: String,
    pub zip_url: String,
    pub checksum_url: String,
    pub release_url: String,
    pub zip_size: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct UpdatePlan {
    pub schema: u8,
    pub app_id: String,
    pub id: String,
    pub version: String,
    pub original_version: String,
    pub install_dir: PathBuf,
    pub stage_dir: PathBuf,
    pub plan_path: PathBuf,
    pub parent_pid: u32,
    pub parent_created: u64,
    pub files: BTreeMap<String, String>,
    pub archive_sha256: String,
}

fn cancelled(cancel: &AtomicBool) -> AppResult<()> {
    if cancel.load(Ordering::Relaxed) {
        Err("업데이트를 취소했습니다. 기존 앱은 유지됩니다.".into())
    } else {
        Ok(())
    }
}

fn io_error(error: impl std::fmt::Display) -> String {
    format!("업데이트 파일 처리 실패: {error}")
}
fn require(ok: bool, message: &str) -> AppResult<()> {
    if ok {
        Ok(())
    } else {
        Err(message.into())
    }
}
fn is_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn version_tuple(value: &str) -> AppResult<(u64, u64, u64)> {
    require(value.len() <= 48, "업데이트 버전이 너무 깁니다.")?;
    let value = value.strip_prefix('v').unwrap_or(value);
    let parts: Vec<_> = value.split('.').collect();
    require(
        parts.len() == 3,
        "안정 버전 X.Y.Z만 업데이트할 수 있습니다.",
    )?;
    let mut parsed = [0_u64; 3];
    for (index, part) in parts.iter().enumerate() {
        require(
            !part.is_empty()
                && part.bytes().all(|b| b.is_ascii_digit())
                && (part.len() == 1 || !part.starts_with('0')),
            "안정 버전 X.Y.Z만 업데이트할 수 있습니다.",
        )?;
        parsed[index] = part
            .parse()
            .map_err(|_| "버전 숫자가 범위를 넘었습니다.".to_string())?;
    }
    Ok((parsed[0], parsed[1], parsed[2]))
}

pub fn safe_url(value: &str) -> AppResult<()> {
    require(
        value.len() <= 16_384
            && !value
                .chars()
                .any(|c| c.is_control() || c.is_whitespace() || c == '\\'),
        "업데이트 주소가 올바르지 않습니다.",
    )?;
    let rest = value
        .strip_prefix("https://")
        .ok_or("HTTPS 업데이트 주소만 허용합니다.")?;
    let (host, path) = rest
        .split_once('/')
        .ok_or("업데이트 주소에 경로가 없습니다.")?;
    require(
        matches!(
            host,
            "api.github.com"
                | "github.com"
                | "release-assets.githubusercontent.com"
                | "objects.githubusercontent.com"
        ),
        "허용되지 않은 업데이트 서버입니다.",
    )?;
    require(
        !value.contains('#'),
        "업데이트 주소에 조각 식별자를 사용할 수 없습니다.",
    )?;
    if host == "api.github.com" {
        require(
            path.starts_with("repos/P-JSL/Folder_Organizer/releases/"),
            "다른 저장소 API에는 접근하지 않습니다.",
        )?;
    }
    Ok(())
}

fn asset_url(tag: &str, name: &str) -> String {
    format!("https://github.com/P-JSL/Folder_Organizer/releases/download/{tag}/{name}")
}

pub fn parse_release_metadata(value: &Value, current: &str) -> AppResult<Option<ReleaseInfo>> {
    let current = version_tuple(current)?;
    require(
        value.is_object(),
        "업데이트 서버 응답 형식이 올바르지 않습니다.",
    )?;
    if value.get("draft").and_then(Value::as_bool) == Some(true)
        || value.get("prerelease").and_then(Value::as_bool) == Some(true)
    {
        return Ok(None);
    }
    let Some(tag) = value.get("tag_name").and_then(Value::as_str) else {
        return Ok(None);
    };
    if !tag.starts_with('v') {
        return Ok(None);
    }
    let Ok(latest) = version_tuple(tag) else {
        return Ok(None);
    };
    if latest <= current {
        return Ok(None);
    }
    let assets = value
        .get("assets")
        .and_then(Value::as_array)
        .ok_or("배포 파일 목록을 읽을 수 없습니다.")?;
    let mut found = BTreeMap::new();
    for asset in assets {
        let Some(name) = asset.get("name").and_then(Value::as_str) else {
            continue;
        };
        if name != WINDOWS_ASSET && name != CHECKSUM_ASSET {
            continue;
        }
        require(
            !found.contains_key(name),
            "중복된 업데이트 배포 파일입니다.",
        )?;
        let url = asset
            .get("browser_download_url")
            .and_then(Value::as_str)
            .ok_or("배포 파일 주소가 없습니다.")?;
        require(
            url == asset_url(tag, name),
            "안전하지 않은 업데이트 배포 파일 주소입니다.",
        )?;
        let size = asset
            .get("size")
            .and_then(Value::as_u64)
            .ok_or("배포 파일 크기를 확인할 수 없습니다.")?;
        let maximum = if name == WINDOWS_ASSET {
            MAX_ARCHIVE_BYTES
        } else {
            MAX_CHECKSUM_BYTES
        };
        require(
            size > 0 && size <= maximum,
            "배포 파일 크기가 허용 범위를 넘었습니다.",
        )?;
        found.insert(name.to_string(), (url.to_string(), size));
    }
    let (Some(zip), Some(checksum)) = (found.get(WINDOWS_ASSET), found.get(CHECKSUM_ASSET)) else {
        return Ok(None);
    };
    Ok(Some(ReleaseInfo {
        version: tag[1..].into(),
        tag_name: tag.into(),
        zip_url: zip.0.clone(),
        checksum_url: checksum.0.clone(),
        release_url: format!("https://github.com/P-JSL/Folder_Organizer/releases/tag/{tag}"),
        zip_size: zip.1,
    }))
}

fn validate_release(release: &ReleaseInfo) -> AppResult<()> {
    version_tuple(&release.version)?;
    require(
        release.tag_name == format!("v{}", release.version)
            && release.zip_url == asset_url(&release.tag_name, WINDOWS_ASSET)
            && release.checksum_url == asset_url(&release.tag_name, CHECKSUM_ASSET)
            && release.release_url
                == format!(
                    "https://github.com/P-JSL/Folder_Organizer/releases/tag/{}",
                    release.tag_name
                )
            && release.zip_size > 0
            && release.zip_size <= MAX_ARCHIVE_BYTES,
        "업데이트 릴리스 정보가 변경되었습니다.",
    )
}

fn download(
    url: &str,
    limit: u64,
    target: Option<&Path>,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<Option<Vec<u8>>> {
    cancelled(cancel)?;
    safe_url(url)?;
    // ureq's native-tls feature supplies an adapter but does not select it.
    // Explicit selection preserves the platform's certificate trust store.
    let connector = native_tls::TlsConnector::new()
        .map_err(|_| "운영체제 인증서 저장소를 준비하지 못했습니다.")?;
    let agent = ureq::AgentBuilder::new()
        .tls_connector(std::sync::Arc::new(connector))
        .redirects(0)
        .try_proxy_from_env(true)
        .timeout_connect(Duration::from_secs(15))
        .timeout_read(Duration::from_secs(15))
        .timeout_write(Duration::from_secs(15))
        .build();
    let deadline = Instant::now() + Duration::from_secs(240);
    let mut current = url.to_string();
    let mut response = None;
    for _ in 0..10 {
        cancelled(cancel)?;
        safe_url(&current)?;
        let request = agent
            .get(&current)
            .set("User-Agent", "FolderOrganizer-Rust-Updater/1")
            .set(
                "Accept",
                if url == LATEST_RELEASE_API {
                    "application/vnd.github+json"
                } else {
                    "application/octet-stream"
                },
            );
        let received = match request.call() {
            Ok(response) => response,
            Err(ureq::Error::Status(404, _)) if url == LATEST_RELEASE_API => return Ok(None),
            Err(ureq::Error::Status(status, _)) => {
                return Err(format!(
                    "업데이트 서버에 접근할 수 없습니다 (HTTP {status})."
                ))
            }
            Err(_) => {
                return Err(
                    "업데이트 서버에 연결하지 못했습니다. 네트워크·프록시·인증서를 확인하세요."
                        .into(),
                )
            }
        };
        if (300..400).contains(&received.status()) {
            current = received
                .header("Location")
                .ok_or("업데이트 이동 주소가 없습니다.")?
                .to_string();
            safe_url(&current)?;
            continue;
        }
        require(
            received.status() == 200,
            "업데이트 서버가 완전한 파일을 반환하지 않았습니다.",
        )?;
        response = Some(received);
        break;
    }
    let response = response.ok_or("업데이트 이동 횟수 제한을 넘었습니다.")?;
    let length = response
        .header("Content-Length")
        .map(|text| {
            text.parse::<u64>()
                .map_err(|_| "업데이트 파일 크기 응답이 잘못되었습니다.")
        })
        .transpose()?;
    require(
        length.map_or(true, |bytes| bytes <= limit),
        "업데이트 다운로드 크기 제한을 넘었습니다.",
    )?;
    let mut output = if let Some(path) = target {
        plain_path(path, false)?;
        Some(
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(path)
                .map_err(io_error)?,
        )
    } else {
        None
    };
    let mut bytes = Vec::new();
    let mut reader = response.into_reader();
    let mut buffer = [0_u8; 64 * 1024];
    let mut count = 0_u64;
    let mut last_progress = Instant::now() - Duration::from_secs(1);
    loop {
        cancelled(cancel)?;
        require(
            Instant::now() <= deadline,
            "업데이트 다운로드 대기 시간이 초과됐습니다.",
        )?;
        let read = reader
            .read(&mut buffer)
            .map_err(|_| "업데이트 파일을 완전히 받지 못했습니다.")?;
        if read == 0 {
            break;
        }
        count = count
            .checked_add(read as u64)
            .ok_or("다운로드 크기가 범위를 넘었습니다.")?;
        require(count <= limit, "업데이트 다운로드 크기 제한을 넘었습니다.")?;
        if let Some(file) = output.as_mut() {
            file.write_all(&buffer[..read]).map_err(io_error)?;
        } else {
            bytes.extend_from_slice(&buffer[..read]);
        }
        if last_progress.elapsed() >= Duration::from_millis(70) {
            if let Some(callback) = progress {
                callback(Progress {
                    phase: "update_download".into(),
                    completed: count,
                    total: length,
                    message: "Rust 업데이트 파일을 받고 있습니다.".into(),
                });
            }
            last_progress = Instant::now();
        }
    }
    cancelled(cancel)?;
    require(
        length.map_or(true, |expected| expected == count),
        "다운로드 파일 크기가 응답과 다릅니다.",
    )?;
    if let Some(file) = output.as_mut() {
        file.sync_all().map_err(io_error)?;
    }
    Ok(Some(bytes))
}

pub fn check_for_update(current: &str) -> AppResult<Option<ReleaseInfo>> {
    version_tuple(current)?;
    let Some(bytes) = download(
        LATEST_RELEASE_API,
        MAX_API_BYTES,
        None,
        &AtomicBool::new(false),
        None,
    )?
    else {
        return Ok(None);
    };
    let value =
        serde_json::from_slice(&bytes).map_err(|_| "업데이트 서버 응답을 읽을 수 없습니다.")?;
    parse_release_metadata(&value, current)
}

pub fn expected_checksum(bytes: &[u8]) -> AppResult<String> {
    require(
        bytes.len() as u64 <= MAX_CHECKSUM_BYTES,
        "체크섬 파일이 너무 큽니다.",
    )?;
    let text = std::str::from_utf8(bytes)
        .map_err(|_| "체크섬 파일은 UTF-8이어야 합니다.")?
        .trim_start_matches('\u{feff}');
    let mut matches = Vec::new();
    for line in text.lines() {
        let mut fields = line.split_whitespace();
        let (Some(hash), Some(name)) = (fields.next(), fields.next()) else {
            continue;
        };
        if name.trim_start_matches('*') != WINDOWS_ASSET {
            continue;
        }
        let hash = hash.to_ascii_lowercase();
        require(
            fields.next().is_none() && is_hex(&hash, 64),
            "Rust 배포 파일의 SHA-256 체크섬이 잘못되었습니다.",
        )?;
        matches.push(hash);
    }
    require(
        matches.len() == 1,
        "Rust 배포 파일의 유일한 SHA-256 체크섬이 필요합니다.",
    )?;
    Ok(matches.remove(0))
}

pub fn plain_path(path: &Path, must_exist: bool) -> AppResult<()> {
    require(
        path.is_absolute()
            && !path
                .components()
                .any(|part| matches!(part, Component::ParentDir | Component::CurDir)),
        "업데이트 경로는 정규화된 절대 경로여야 합니다.",
    )?;
    #[cfg(windows)]
    if let Some(Component::Prefix(prefix)) = path.components().next() {
        use std::path::Prefix;
        require(
            matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_)),
            "네트워크 공유 경로에는 자동 업데이트를 적용하지 않습니다.",
        )?;
    }
    for component in path.ancestors() {
        match fs::symlink_metadata(component) {
            Ok(metadata) => require(
                !core::is_link_or_reparse(&metadata),
                "업데이트 경로에 링크나 재분석 지점이 있습니다.",
            )?,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(io_error(error)),
        }
    }
    if must_exist {
        require(path.exists(), "업데이트 경로가 없습니다.")?;
    }
    Ok(())
}

pub fn safe_relative(value: &str) -> AppResult<PathBuf> {
    require(
        !value.is_empty() && !value.contains('\\') && !value.contains('\0'),
        "압축 경로가 잘못되었습니다.",
    )?;
    let mut result = PathBuf::new();
    for part in value.split('/') {
        let base = part.split('.').next().unwrap_or("").to_ascii_uppercase();
        let reserved = matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
            || (base.len() == 4
                && (base.starts_with("COM") || base.starts_with("LPT"))
                && matches!(base.as_bytes()[3], b'1'..=b'9'));
        require(
            !part.is_empty()
                && part != "."
                && part != ".."
                && part.len() <= 240
                && !part.ends_with('.')
                && !part.ends_with(' ')
                && !reserved
                && !part
                    .chars()
                    .any(|c| c.is_control() || ":?*<>|\"".contains(c)),
            "안전하지 않은 압축 경로입니다.",
        )?;
        result.push(part);
    }
    require(
        !result.is_absolute(),
        "압축 파일에 절대 경로를 사용할 수 없습니다.",
    )?;
    Ok(result)
}

pub fn file_hash(path: &Path, cancel: &AtomicBool) -> AppResult<String> {
    cancelled(cancel)?;
    plain_path(path, true)?;
    let mut file = core::safe_open(path)?;
    let before = core::snapshot_open_file(&file)?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 256 * 1024];
    loop {
        cancelled(cancel)?;
        let read = file.read(&mut buffer).map_err(io_error)?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    require(
        core::snapshot_open_file(&file)? == before && core::snapshot(path)? == before,
        "업데이트 파일이 읽는 중 변경되었습니다.",
    )?;
    Ok(format!("{:x}", digest.finalize()))
}

fn read_json(path: &Path, maximum: u64) -> AppResult<Value> {
    plain_path(path, true)?;
    let mut file = core::safe_open(path)?;
    require(
        file.metadata().map_err(io_error)?.len() <= maximum,
        "업데이트 기록이 너무 큽니다.",
    )?;
    let mut bytes = Vec::new();
    Read::by_ref(&mut file)
        .take(maximum + 1)
        .read_to_end(&mut bytes)
        .map_err(io_error)?;
    require(
        bytes.len() as u64 <= maximum,
        "업데이트 기록이 너무 큽니다.",
    )?;
    serde_json::from_slice(&bytes).map_err(|_| "업데이트 기록이 손상되었습니다.".into())
}

pub fn read_marker(directory: &Path) -> AppResult<String> {
    plain_path(directory, true)?;
    require(directory.is_dir(), "앱 설치 위치가 폴더가 아닙니다.")?;
    let value = read_json(&directory.join("version.json"), 4096)?;
    require(
        value.get("app_id").and_then(Value::as_str) == Some(APP_ID),
        "Rust 앱 설치 폴더가 아닙니다.",
    )?;
    let version = value
        .get("version")
        .and_then(Value::as_str)
        .ok_or("앱 버전 표식이 없습니다.")?;
    version_tuple(version)?;
    for name in [APP_EXECUTABLE, HELPER_EXECUTABLE, CLI_EXECUTABLE] {
        plain_path(&directory.join(name), true)?;
        require(
            directory.join(name).is_file(),
            "Rust portable 앱 실행 파일이 빠졌습니다.",
        )?;
    }
    Ok(version.into())
}

pub fn validate_install_contents(directory: &Path) -> AppResult<()> {
    plain_path(directory, true)?;
    let allowed = [
        APP_EXECUTABLE,
        HELPER_EXECUTABLE,
        CLI_EXECUTABLE,
        "version.json",
        "README.md",
        "CHANGELOG.md",
        "THIRD_PARTY.md",
        "licenses",
        "LICENSE",
        "LICENSE.txt",
    ];
    for entry in fs::read_dir(directory).map_err(io_error)? {
        let entry = entry.map_err(io_error)?;
        require(
            entry
                .file_name()
                .to_str()
                .map_or(false, |name| allowed.contains(&name)),
            "앱 폴더에 개인 파일이 있어 자동 교체하지 않습니다. portable 앱만 별도 폴더에 두세요.",
        )?;
        plain_path(&entry.path(), true)?;
    }
    Ok(())
}

pub fn manifest(directory: &Path, cancel: &AtomicBool) -> AppResult<BTreeMap<String, String>> {
    plain_path(directory, true)?;
    let mut pending = vec![directory.to_path_buf()];
    let mut files = BTreeMap::new();
    let mut total = 0_u64;
    while let Some(parent) = pending.pop() {
        cancelled(cancel)?;
        plain_path(&parent, true)?;
        for entry in fs::read_dir(parent).map_err(io_error)? {
            cancelled(cancel)?;
            let path = entry.map_err(io_error)?.path();
            plain_path(&path, true)?;
            let metadata = fs::symlink_metadata(&path).map_err(io_error)?;
            if metadata.is_dir() {
                pending.push(path);
                continue;
            }
            require(
                metadata.is_file(),
                "특수 파일은 업데이트에 포함할 수 없습니다.",
            )?;
            total = total
                .checked_add(metadata.len())
                .ok_or("업데이트 파일 크기가 범위를 넘었습니다.")?;
            require(
                total <= MAX_UNPACKED_BYTES && files.len() < MAX_MANIFEST_FILES,
                "업데이트 파일 수 또는 크기 제한을 넘었습니다.",
            )?;
            let relative = path
                .strip_prefix(directory)
                .map_err(io_error)?
                .to_str()
                .ok_or("업데이트 경로가 Unicode가 아닙니다.")?
                .replace('\\', "/");
            safe_relative(&relative)?;
            files.insert(relative, file_hash(&path, cancel)?);
        }
    }
    require(!files.is_empty(), "업데이트 파일 목록이 비었습니다.")?;
    Ok(files)
}

pub fn extract_archive(archive: &Path, destination: &Path, cancel: &AtomicBool) -> AppResult<()> {
    plain_path(destination, false)?;
    let file = core::safe_open(archive)?;
    let mut source = zip::ZipArchive::new(file).map_err(io_error)?;
    require(
        !source.is_empty() && source.len() <= MAX_MANIFEST_FILES,
        "업데이트 압축 항목 수가 잘못되었습니다.",
    )?;
    let mut seen = HashSet::new();
    let mut total = 0_u64;
    for index in 0..source.len() {
        cancelled(cancel)?;
        let mut item = source.by_index(index).map_err(io_error)?;
        let name = std::str::from_utf8(item.name_raw())
            .map_err(|_| "업데이트 압축 경로가 UTF-8이 아닙니다.")?
            .trim_end_matches('/')
            .to_string();
        let relative = safe_relative(&name)?;
        require(
            relative.components().next().map(|c| c.as_os_str())
                == Some(std::ffi::OsStr::new(APP_DIRECTORY)),
            "Rust 압축 파일의 최상위 폴더가 다릅니다.",
        )?;
        require(
            seen.insert(name.to_lowercase()),
            "대소문자 중복 압축 항목은 사용할 수 없습니다.",
        )?;
        if let Some(mode) = item.unix_mode() {
            require(
                matches!(mode & 0o170000, 0 | 0o100000 | 0o040000),
                "링크·특수 파일은 업데이트에 포함할 수 없습니다.",
            )?;
        }
        total = total
            .checked_add(item.size())
            .ok_or("압축 해제 크기가 범위를 넘었습니다.")?;
        require(
            item.size() <= MAX_ENTRY_BYTES && total <= MAX_UNPACKED_BYTES,
            "업데이트 압축 해제 크기 제한을 넘었습니다.",
        )?;
        require(
            item.size() <= 1024 * 1024 || item.size() / item.compressed_size().max(1) <= 250,
            "비정상적인 업데이트 압축률입니다.",
        )?;
        let path = destination.join(relative);
        plain_path(&path, false)?;
        if item.is_dir() {
            fs::create_dir_all(path).map_err(io_error)?;
            continue;
        }
        fs::create_dir_all(path.parent().ok_or("압축 경로의 상위 폴더가 없습니다.")?)
            .map_err(io_error)?;
        plain_path(&path, false)?;
        let mut output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(io_error)?;
        let expected = item.size();
        let mut count = 0_u64;
        let mut buffer = [0_u8; 256 * 1024];
        loop {
            cancelled(cancel)?;
            let read = item.read(&mut buffer).map_err(io_error)?;
            if read == 0 {
                break;
            }
            count += read as u64;
            require(
                count <= expected && count <= MAX_ENTRY_BYTES,
                "압축 항목 크기가 일치하지 않습니다.",
            )?;
            output.write_all(&buffer[..read]).map_err(io_error)?;
        }
        require(count == expected, "업데이트 압축 항목이 완전하지 않습니다.")?;
        output.sync_all().map_err(io_error)?;
    }
    Ok(())
}

pub fn storage_root() -> AppResult<PathBuf> {
    #[cfg(windows)]
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .ok_or("LOCALAPPDATA를 찾을 수 없습니다.")?;
    #[cfg(not(windows))]
    let base = if let Some(value) = std::env::var_os("XDG_CACHE_HOME") {
        PathBuf::from(value)
    } else {
        PathBuf::from(std::env::var_os("HOME").ok_or("사용자 캐시 위치를 찾을 수 없습니다.")?)
            .join(".cache")
    };
    let result = base.join(APP_DIRECTORY).join("updates");
    plain_path(&result, false)?;
    Ok(result)
}

fn new_id() -> AppResult<String> {
    let mut bytes = [0_u8; 16];
    getrandom::getrandom(&mut bytes).map_err(|_| "업데이트 작업 번호를 생성하지 못했습니다.")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}

fn private_directory(path: &Path, recursive: bool) -> AppResult<()> {
    plain_path(path, false)?;
    let mut builder = fs::DirBuilder::new();
    builder.recursive(recursive);
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path).map_err(io_error)
}

fn write_json_exclusive<T: Serialize>(path: &Path, value: &T) -> AppResult<()> {
    plain_path(path, false)?;
    let bytes = serde_json::to_vec_pretty(value).map_err(io_error)?;
    require(
        bytes.len() as u64 <= MAX_PLAN_BYTES,
        "업데이트 기록이 크기 제한을 넘었습니다.",
    )?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .map_err(io_error)?;
    file.write_all(&bytes).map_err(io_error)?;
    file.sync_all().map_err(io_error)
}

fn current_process_created() -> AppResult<u64> {
    #[cfg(windows)]
    unsafe {
        return process_created(windows_sys::Win32::System::Threading::GetCurrentProcess());
    }
    #[cfg(not(windows))]
    Ok(0)
}

pub fn prepare_update(
    release: &ReleaseInfo,
    install_dir: &Path,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<UpdatePlan> {
    cancelled(cancel)?;
    validate_release(release)?;
    plain_path(install_dir, true)?;
    let current = read_marker(install_dir)?;
    validate_install_contents(install_dir)?;
    require(
        version_tuple(&release.version)? > version_tuple(&current)?,
        "설치된 버전보다 새로운 업데이트가 아닙니다.",
    )?;
    let root = storage_root()?;
    require(
        !install_dir.starts_with(&root) && !root.starts_with(install_dir),
        "앱 폴더와 업데이트 저장소가 겹칩니다.",
    )?;
    if !root.exists() {
        private_directory(&root, true)?;
    }
    let work = root.join(new_id()?);
    private_directory(&work, false)?;
    let checksum = download(
        &release.checksum_url,
        MAX_CHECKSUM_BYTES,
        None,
        cancel,
        None,
    )?
    .ok_or("체크섬을 받지 못했습니다.")?;
    let expected = expected_checksum(&checksum)?;
    let archive = work.join(WINDOWS_ASSET);
    download(
        &release.zip_url,
        MAX_ARCHIVE_BYTES,
        Some(&archive),
        cancel,
        progress,
    )?;
    require(
        fs::metadata(&archive).map_err(io_error)?.len() == release.zip_size
            && file_hash(&archive, cancel)? == expected,
        "업데이트 SHA-256 또는 파일 크기가 다릅니다. 기존 앱은 유지됩니다.",
    )?;
    let payload = work.join("payload");
    private_directory(&payload, false)?;
    extract_archive(&archive, &payload, cancel)?;
    let stage = payload.join(APP_DIRECTORY);
    require(
        read_marker(&stage)? == release.version,
        "압축 파일과 릴리스의 버전이 다릅니다.",
    )?;
    validate_install_contents(&stage)?;
    let plan = UpdatePlan {
        schema: 1,
        app_id: APP_ID.into(),
        id: work.file_name().unwrap().to_str().unwrap().into(),
        version: release.version.clone(),
        original_version: current,
        install_dir: install_dir.to_path_buf(),
        stage_dir: stage,
        plan_path: work.join("plan.json"),
        parent_pid: std::process::id(),
        parent_created: current_process_created()?,
        files: manifest(&payload.join(APP_DIRECTORY), cancel)?,
        archive_sha256: expected,
    };
    cancelled(cancel)?;
    write_json_exclusive(&plan.plan_path, &plan)?;
    validate_plan(&plan)?;
    cancelled(cancel)?;
    Ok(plan)
}

pub fn validate_plan(plan: &UpdatePlan) -> AppResult<()> {
    require(
        plan.schema == 1 && plan.app_id == APP_ID && is_hex(&plan.id, 32),
        "Rust 업데이트 계획 형식이 다릅니다.",
    )?;
    let root = storage_root()?;
    plain_path(&root, true)?;
    let work = root.join(&plan.id);
    require(
        plan.plan_path == work.join("plan.json")
            && plan.stage_dir == work.join("payload").join(APP_DIRECTORY),
        "저장된 업데이트 계획 경로가 다릅니다.",
    )?;
    let stored: UpdatePlan =
        serde_json::from_value(read_json(&plan.plan_path, MAX_PLAN_BYTES)?).map_err(io_error)?;
    require(stored == *plan, "저장된 업데이트 계획이 변경됐습니다.")?;
    plain_path(&plan.install_dir, true)?;
    plain_path(&plan.stage_dir, true)?;
    require(
        !plan.install_dir.starts_with(&root)
            && !root.starts_with(&plan.install_dir)
            && plan.install_dir.parent().is_some(),
        "앱과 업데이트 보관 경로가 겹칩니다.",
    )?;
    require(
        version_tuple(&plan.version)? > version_tuple(&plan.original_version)?
            && plan.parent_pid > 1
            && is_hex(&plan.archive_sha256, 64),
        "업데이트 버전·프로세스·해시가 잘못되었습니다.",
    )?;
    require(
        read_marker(&plan.install_dir)? == plan.original_version
            && read_marker(&plan.stage_dir)? == plan.version,
        "설치 또는 대기 앱 버전이 바뀌었습니다.",
    )?;
    validate_install_contents(&plan.install_dir)?;
    validate_install_contents(&plan.stage_dir)?;
    require(
        !plan.files.is_empty() && plan.files.len() <= MAX_MANIFEST_FILES,
        "업데이트 파일 목록이 잘못되었습니다.",
    )?;
    for (name, digest) in &plan.files {
        safe_relative(name)?;
        require(is_hex(digest, 64), "파일 해시가 잘못되었습니다.")?;
    }
    require(
        manifest(&plan.stage_dir, &AtomicBool::new(false))? == plan.files,
        "대기 업데이트 파일이 변경됐습니다. 다시 받으세요.",
    )?;
    Ok(())
}

pub fn load_plan(path: &Path) -> AppResult<UpdatePlan> {
    let plan: UpdatePlan =
        serde_json::from_value(read_json(path, MAX_PLAN_BYTES)?).map_err(io_error)?;
    require(
        plan.plan_path == path,
        "업데이트 계획의 저장 위치가 다릅니다.",
    )?;
    validate_plan(&plan)?;
    Ok(plan)
}

pub fn install_directory() -> AppResult<PathBuf> {
    std::env::current_exe()
        .map_err(io_error)?
        .parent()
        .map(Path::to_path_buf)
        .ok_or("앱 설치 위치를 찾을 수 없습니다.".into())
}

pub fn is_auto_update_supported() -> bool {
    if !cfg!(windows) {
        return false;
    }
    let Ok(executable) = std::env::current_exe() else {
        return false;
    };
    if executable.file_name().and_then(|name| name.to_str()) != Some(APP_EXECUTABLE) {
        return false;
    }
    let Some(directory) = executable.parent() else {
        return false;
    };
    read_marker(directory).is_ok() && validate_install_contents(directory).is_ok()
}

pub fn start_update_helper(plan: &UpdatePlan) -> AppResult<()> {
    require(
        is_auto_update_supported(),
        "Windows Rust portable 앱에서만 자동 업데이트를 적용합니다.",
    )?;
    validate_plan(plan)?;
    require(
        plan.install_dir == install_directory()?
            && plan.parent_pid == std::process::id()
            && plan.parent_created == current_process_created()?,
        "현재 앱의 업데이트 계획이 아닙니다.",
    )?;
    let work = plan
        .plan_path
        .parent()
        .ok_or("업데이트 작업 위치가 없습니다.")?;
    let launch = work.join(format!("launch-{}", new_id()?));
    private_directory(&launch, false)?;
    let source = plan.install_dir.join(HELPER_EXECUTABLE);
    let target = launch.join(HELPER_EXECUTABLE);
    let mut input = core::safe_open(&source)?;
    let mut output = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&target)
        .map_err(io_error)?;
    std::io::copy(&mut input, &mut output).map_err(io_error)?;
    output.sync_all().map_err(io_error)?;
    let cancel = AtomicBool::new(false);
    require(
        file_hash(&source, &cancel)? == file_hash(&target, &cancel)?,
        "업데이트 도우미 복사 확인에 실패했습니다.",
    )?;
    let mut command = Command::new(target);
    command
        .arg("--plan")
        .arg(&plan.plan_path)
        .current_dir(work)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x00000008 | 0x00000200);
    }
    command
        .spawn()
        .map_err(|_| "업데이트 도우미를 시작하지 못했습니다. 앱을 유지합니다.".to_string())?;
    Ok(())
}

#[cfg(windows)]
unsafe fn process_created(handle: windows_sys::Win32::Foundation::HANDLE) -> AppResult<u64> {
    use windows_sys::Win32::Foundation::FILETIME;
    use windows_sys::Win32::System::Threading::GetProcessTimes;
    let mut created: FILETIME = std::mem::zeroed();
    let mut exited: FILETIME = std::mem::zeroed();
    let mut kernel: FILETIME = std::mem::zeroed();
    let mut user: FILETIME = std::mem::zeroed();
    require(
        GetProcessTimes(handle, &mut created, &mut exited, &mut kernel, &mut user) != 0,
        "앱 프로세스의 생성 시각을 확인하지 못했습니다.",
    )?;
    Ok((u64::from(created.dwHighDateTime) << 32) | u64::from(created.dwLowDateTime))
}

pub fn wait_for_process(pid: u32, created: u64) -> AppResult<()> {
    #[cfg(windows)]
    unsafe {
        use windows_sys::Win32::Foundation::{
            CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, WAIT_OBJECT_0, WAIT_TIMEOUT,
        };
        use windows_sys::Win32::System::Threading::{
            OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
            PROCESS_SYNCHRONIZE,
        };
        let handle = OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            0,
            pid,
        );
        if handle.is_null() {
            if GetLastError() == ERROR_INVALID_PARAMETER {
                return Ok(());
            }
            return Err("실행 중인 앱의 종료를 확인할 수 없습니다.".into());
        }
        let result = (|| {
            require(
                created != 0 && process_created(handle)? == created,
                "프로세스 식별자가 재사용되어 업데이트를 중단했습니다.",
            )?;
            let deadline = Instant::now() + Duration::from_secs(180);
            while Instant::now() < deadline {
                let status = WaitForSingleObject(handle, 500);
                if status == WAIT_OBJECT_0 {
                    return Ok(());
                }
                require(
                    status == WAIT_TIMEOUT,
                    "앱 프로세스 대기 중 오류가 발생했습니다.",
                )?;
            }
            Err("앱이 종료되지 않아 업데이트를 적용하지 않았습니다.".into())
        })();
        CloseHandle(handle);
        return result;
    }
    #[cfg(not(windows))]
    {
        let _ = (pid, created);
        Err("자동 업데이트 설치 도우미는 Windows에서만 실행합니다.".into())
    }
}

/// A stable OS lock beside the installation, not inside a renamed app or a
/// per-plan directory. The file is never truncated or removed. Canonicalizing
/// the actual installation resolves Windows case/extended/short-path aliases.
pub struct InstallUpdateLock {
    file: File,
}

impl InstallUpdateLock {
    pub fn acquire(install: &Path) -> AppResult<Self> {
        plain_path(install, true)?;
        let canonical = fs::canonicalize(install).map_err(io_error)?;
        plain_path(&canonical, true)?;
        let parent = canonical.parent().ok_or("앱 상위 폴더가 없습니다.")?;
        let mut identity = canonical
            .to_str()
            .ok_or("앱 경로가 Unicode가 아닙니다.")?
            .replace('\\', "/");
        if cfg!(windows) {
            identity = identity.to_uppercase();
        }
        let digest = format!("{:x}", Sha256::digest(identity.as_bytes()));
        let path = parent.join(format!(
            ".folder-organizer-rust-update-{}.lock",
            &digest[..32]
        ));
        plain_path(&path, false)?;
        let mut options = OpenOptions::new();
        options.read(true).write(true).create(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            options.custom_flags(0x0020_0000);
        }
        let file = options.open(&path).map_err(io_error)?;
        let opened = core::snapshot_open_file(&file)?;
        plain_path(&path, true)?;
        require(
            core::snapshot(&path)? == opened,
            "업데이트 잠금 파일이 바뀌었습니다.",
        )?;
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            require(
                unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
                "다른 업데이트가 이 앱을 교체하고 있습니다. 기존 앱은 유지됩니다.",
            )?;
        }
        #[cfg(windows)]
        {
            use std::os::windows::io::AsRawHandle;
            use windows_sys::Win32::Storage::FileSystem::LockFile;
            require(
                unsafe { LockFile(file.as_raw_handle() as _, 0, 0, 1, 0) } != 0,
                "다른 업데이트가 이 앱을 교체하고 있습니다. 기존 앱은 유지됩니다.",
            )?;
        }
        #[cfg(not(any(unix, windows)))]
        return Err("이 운영체제는 안전한 업데이트 잠금을 지원하지 않습니다.".into());
        #[cfg(any(unix, windows))]
        Ok(Self { file })
    }
}

impl Drop for InstallUpdateLock {
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
            use windows_sys::Win32::Storage::FileSystem::UnlockFile;
            unsafe {
                UnlockFile(self.file.as_raw_handle() as _, 0, 0, 1, 0);
            }
        }
    }
}

fn rename_exclusive(source: &Path, target: &Path) -> AppResult<()> {
    for attempt in 0..20 {
        plain_path(source, true)?;
        plain_path(target, false)?;
        require(!target.exists(), "교체 대상 경로가 이미 있습니다.")?;
        #[cfg(windows)]
        let outcome = {
            use std::os::windows::ffi::OsStrExt;
            use windows_sys::Win32::Storage::FileSystem::{MoveFileExW, MOVEFILE_WRITE_THROUGH};
            let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
            let target: Vec<u16> = target.as_os_str().encode_wide().chain(Some(0)).collect();
            if unsafe { MoveFileExW(source.as_ptr(), target.as_ptr(), MOVEFILE_WRITE_THROUGH) } != 0
            {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        };
        #[cfg(target_os = "linux")]
        let outcome = {
            use std::os::unix::ffi::OsStrExt;
            let source = std::ffi::CString::new(source.as_os_str().as_bytes()).map_err(io_error)?;
            let target = std::ffi::CString::new(target.as_os_str().as_bytes()).map_err(io_error)?;
            if unsafe {
                libc::renameat2(
                    libc::AT_FDCWD,
                    source.as_ptr(),
                    libc::AT_FDCWD,
                    target.as_ptr(),
                    libc::RENAME_NOREPLACE,
                )
            } == 0
            {
                Ok(())
            } else {
                Err(std::io::Error::last_os_error())
            }
        };
        #[cfg(not(any(windows, target_os = "linux")))]
        let outcome: Result<(), std::io::Error> = Err(std::io::Error::new(
            std::io::ErrorKind::Unsupported,
            "Exclusive directory rename unavailable",
        ));
        match outcome {
            Ok(()) => return Ok(()),
            Err(error) if attempt == 19 => return Err(io_error(error)),
            Err(_) => std::thread::sleep(Duration::from_millis(500)),
        }
    }
    unreachable!()
}

fn write_result(work: &Path, result: &Value) {
    let Ok(root) = storage_root() else {
        return;
    };
    if work.parent() != Some(root.as_path())
        || !work
            .file_name()
            .and_then(|s| s.to_str())
            .map_or(false, |id| is_hex(id, 32))
    {
        return;
    }
    for target in [work.join("result.json"), root.join("last-result.json")] {
        if plain_path(&target, false).is_err() {
            continue;
        }
        let Ok(id) = new_id() else {
            continue;
        };
        let temporary = target.with_extension(format!("tmp-{id}"));
        if write_json_exclusive(&temporary, result).is_ok() {
            let _ = fs::rename(temporary, target);
        }
    }
}

pub fn last_update_result() -> Option<Value> {
    let result = read_json(
        &storage_root().ok()?.join("last-result.json"),
        MAX_PLAN_BYTES,
    )
    .ok()?;
    if matches!(
        result.get("status").and_then(Value::as_str),
        Some("updated" | "failed")
    ) {
        Some(result)
    } else {
        None
    }
}

pub fn apply_update(plan: &UpdatePlan, wait: bool, restart: bool) -> AppResult<Value> {
    validate_plan(plan)?;
    let install = &plan.install_dir;
    let parent = install.parent().ok_or("앱의 상위 폴더가 없습니다.")?;
    let name = install
        .file_name()
        .and_then(|s| s.to_str())
        .ok_or("앱 폴더 이름을 읽을 수 없습니다.")?;
    let backup = parent.join(format!("{name}.previous-{}", plan.id));
    let incoming = parent.join(format!(".{name}.update-{}", plan.id));
    plain_path(&backup, false)?;
    plain_path(&incoming, false)?;
    require(
        !backup.exists() && !incoming.exists(),
        "이 작업의 백업 또는 임시 교체 폴더가 이미 있습니다.",
    )?;
    let work = plan
        .plan_path
        .parent()
        .ok_or("업데이트 작업 위치가 없습니다.")?;
    let mut old_moved = false;
    let mut activated = false;
    let result = (|| {
        if wait {
            wait_for_process(plan.parent_pid, plan.parent_created)?;
        }
        // Revalidate under one installation-wide lock. Distinct valid plans
        // cannot both observe the old version and then replace each other.
        let _install_lock = InstallUpdateLock::acquire(install)?;
        validate_plan(plan)?;
        private_directory(&incoming, false)?;
        for relative in plan.files.keys() {
            let relative = safe_relative(relative)?;
            let source = plan.stage_dir.join(&relative);
            let target = incoming.join(relative);
            fs::create_dir_all(target.parent().ok_or("교체 파일의 상위 폴더가 없습니다.")?)
                .map_err(io_error)?;
            plain_path(&target, false)?;
            let input = core::safe_open(&source)?;
            let mut output = OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&target)
                .map_err(io_error)?;
            let copied = std::io::copy(&mut input.take(MAX_ENTRY_BYTES + 1), &mut output)
                .map_err(io_error)?;
            require(
                copied <= MAX_ENTRY_BYTES,
                "교체 파일의 크기가 바뀌었습니다.",
            )?;
            output.sync_all().map_err(io_error)?;
        }
        require(
            manifest(&incoming, &AtomicBool::new(false))? == plan.files
                && read_marker(&incoming)? == plan.version,
            "같은 디스크에 준비한 교체 파일 검증에 실패했습니다.",
        )?;
        require(
            read_marker(install)? == plan.original_version,
            "기존 앱 버전이 변경됐습니다.",
        )?;
        validate_install_contents(install)?;
        rename_exclusive(install, &backup)?;
        old_moved = true;
        if let Err(error) = rename_exclusive(&incoming, install) {
            match rename_exclusive(&backup, install) {
                Ok(()) => {
                    old_moved = false;
                    return Err(error);
                }
                Err(rollback) => {
                    return Err(format!(
                        "{error} 이전 앱은 {}에 보관되어 있습니다. 자동 되돌리기: {rollback}",
                        backup.display()
                    ))
                }
            }
        }
        activated = true;
        let mut result = json!({"status":"updated", "version":plan.version, "backup":backup,
            "details":"Rust 업데이트를 적용했습니다. 이전 앱 폴더는 백업으로 보관했습니다."});
        write_result(work, &result);
        if restart {
            let mut command = Command::new(install.join(APP_EXECUTABLE));
            command
                .current_dir(install)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                command.creation_flags(0x00000008 | 0x00000200);
            }
            if command.spawn().is_err() {
                result["details"] = json!(
                    "업데이트를 적용했지만 재시작하지 못했습니다. Rust 앱을 직접 실행하세요."
                );
                write_result(work, &result);
            }
        }
        Ok(result)
    })();
    if let Err(error) = &result {
        write_result(
            work,
            &json!({"status":"failed", "version":plan.version, "details":error,
            "backup":if old_moved { Some(&backup) } else { None }, "activated":activated}),
        );
    }
    result
}

pub fn helper_main() -> i32 {
    let mut args = std::env::args_os().skip(1);
    let mut path = None;
    let mut restart = true;
    while let Some(arg) = args.next() {
        if arg == "--plan" {
            path = args.next().map(PathBuf::from);
        } else if arg == "--no-restart" {
            restart = false;
        } else {
            return 2;
        }
    }
    let Some(path) = path else {
        return 2;
    };
    let result = load_plan(&path).and_then(|plan| apply_update(&plan, true, restart));
    match result {
        Ok(_) => 0,
        Err(error) => {
            if let Some(work) = path.parent() {
                write_result(work, &json!({"status":"failed", "details":error}));
            }
            eprintln!("{error}");
            1
        }
    }
}
