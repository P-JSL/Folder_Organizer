//! Read-only multi-folder inventory. Global groups have no operation proof.
//!
//! Each real root keeps its own scan scope. The shared exact kernel receives
//! entries, not an invented common root, and all authenticated groups are
//! consumed into records that cannot be passed to write/recovery APIs.
use crate::core::{
    self, AppResult, DuplicateMetrics, DuplicateWarning, FileEntry, Progress, ScanOptions,
};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::Instant;

const MAX_ROOTS: usize = 64;

#[derive(Clone, Debug, Serialize)]
pub struct MultiProgress {
    pub root_id: String,
    pub requested_path: Option<PathBuf>,
    pub phase: String,
    pub completed: u64,
    pub total: Option<u64>,
    pub message: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct RootRecord {
    pub root_id: String,
    pub requested_path: PathBuf,
    pub path: Option<PathBuf>,
    pub state: String,
    pub volume_group: String,
    pub file_count: u64,
    pub bytes: u64,
    pub warnings: Vec<String>,
    pub error: Option<String>,
    pub elapsed_seconds: f64,
    pub scan_options: ScanOptions,
}

#[derive(Clone, Debug, Serialize)]
pub struct MultiFile {
    pub path: PathBuf,
    pub root_ids: Vec<String>,
    pub owner_root_id: String,
    pub relative_paths: BTreeMap<String, String>,
    pub size: u64,
    pub kind: String,
    pub extension: String,
    // Decimal strings preserve Windows nanoseconds in ordinary JSON clients.
    pub modified_ns: String,
    pub created_ns: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct ReadOnlyGroup {
    pub paths: Vec<PathBuf>,
    pub size: u64,
    pub sha256: String,
    pub root_ids: Vec<String>,
    pub group_source_count: usize,
    pub action_authority: bool,
}

#[derive(Clone, Debug, Serialize)]
pub struct MultiDuplicates {
    pub requested: bool,
    pub complete: Option<bool>,
    pub cancelled: bool,
    pub warnings: Vec<DuplicateWarning>,
    pub warning_count: u64,
    pub warnings_truncated: bool,
    pub metrics: DuplicateMetrics,
    pub groups: Vec<ReadOnlyGroup>,
}

#[derive(Clone, Debug, Serialize)]
pub struct DuplicateInput {
    pub requested_path: PathBuf,
    pub root_id: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct Timings {
    pub elapsed_seconds: f64,
    pub scan_seconds: f64,
    pub duplicate_seconds: f64,
}

#[derive(Clone, Debug, Serialize)]
pub struct Scheduling {
    pub max_workers: usize,
    pub policy: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct MultiAnalysis {
    pub schema: String,
    pub app_version: String,
    pub action_authority: bool,
    pub complete: bool,
    pub inventory_complete: bool,
    pub cancelled: bool,
    pub roots: Vec<RootRecord>,
    pub files: Vec<MultiFile>,
    pub notes: Vec<String>,
    pub duplicate_inputs: Vec<DuplicateInput>,
    pub duplicates: MultiDuplicates,
    pub timings: Timings,
    pub scheduling: Scheduling,
}

fn emit(
    callback: Option<&(dyn Fn(MultiProgress) + Sync)>,
    root: &str,
    requested_path: Option<&Path>,
    progress: Progress,
) {
    if let Some(callback) = callback {
        callback(MultiProgress {
            root_id: root.into(),
            requested_path: requested_path.map(Path::to_path_buf),
            phase: progress.phase,
            completed: progress.completed,
            total: progress.total,
            message: progress.message,
        });
    }
}

fn volume_group(path: &Path, metadata: &fs::Metadata) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = path;
        if metadata.dev() == 0 {
            "unknown".into()
        } else {
            format!("volume:{}", metadata.dev())
        }
    }
    #[cfg(windows)]
    {
        let _ = metadata;
        let raw = path.to_string_lossy().replace('/', "\\");
        let ordinary = if let Some(tail) = raw.strip_prefix(r"\\?\UNC\") {
            format!(r"\\{tail}")
        } else {
            raw.strip_prefix(r"\\?\").unwrap_or(&raw).to_string()
        };
        if let Some(server) = ordinary
            .strip_prefix(r"\\")
            .and_then(|tail| tail.split('\\').next())
        {
            return format!("server:{}", server.to_lowercase());
        }
        // Drive letters do not prove physical independence. Keep local volumes
        // serialized until a reliable physical-device mapping is available.
        "unknown-local".into()
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = (path, metadata);
        "unknown".into()
    }
}

fn root_identity(path: &Path, metadata: &fs::Metadata) -> String {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = path;
        format!("{}:{}", metadata.dev(), metadata.ino())
    }
    #[cfg(not(unix))]
    {
        let _ = metadata;
        // canonicalize resolves aliases without merging names in a Windows
        // directory that explicitly enables case-sensitive names.
        path.to_string_lossy().into_owned()
    }
}

// Retain a bounded set of forwarded details without cloning every original
// per-root warning; the total stays exact even after the detail budget is full.
fn inventory_warning(
    details: &mut Vec<DuplicateWarning>,
    total: &mut u64,
    path: &Path,
    message: &str,
) {
    *total = total.saturating_add(1);
    if details.len() < core::DUPLICATE_WARNING_LIMIT {
        details.push(DuplicateWarning {
            path: path.to_path_buf(),
            phase: "inventory".into(),
            message: message.into(),
        });
    }
}

/// Scan selected roots using at most four independent volume groups. Roots on
/// the same known device/server (and unknown Windows local devices) are serial.
/// Global hashing remains serial and retains full byte/snapshot verification.
pub fn analyze(
    requested: &[PathBuf],
    options: &ScanOptions,
    duplicates: bool,
    max_workers: usize,
    cancel: &AtomicBool,
    progress: Option<&(dyn Fn(MultiProgress) + Sync)>,
) -> AppResult<MultiAnalysis> {
    if requested.is_empty() || requested.len() > MAX_ROOTS || !(1..=4).contains(&max_workers) {
        return Err("폴더 1~64개와 동시 탐색 수 1~4를 지정하세요.".into());
    }
    let started = Instant::now();
    let mut roots: Vec<RootRecord> = Vec::new();
    let mut duplicate_inputs = Vec::new();
    let mut identities: HashMap<String, String> = HashMap::new();
    for input in requested {
        let validation = fs::symlink_metadata(input)
            .map_err(|error| error.to_string())
            .and_then(|metadata| {
                if !metadata.is_dir() || core::is_link_or_reparse(&metadata) {
                    return Err("링크가 아닌 실제 폴더를 선택하세요.".into());
                }
                input
                    .canonicalize()
                    .map(|path| (path, metadata))
                    .map_err(|error| error.to_string())
            });
        let (path, group, state, error) = match validation {
            Ok((path, metadata)) => {
                let identity = root_identity(&path, &metadata);
                if let Some(existing) = identities.get(&identity) {
                    duplicate_inputs.push(DuplicateInput {
                        requested_path: input.clone(),
                        root_id: existing.clone(),
                    });
                    continue;
                }
                let root_id = format!("root-{:04}", roots.len() + 1);
                identities.insert(identity, root_id);
                let group = volume_group(&path, &metadata);
                (Some(path), group, "waiting", None)
            }
            Err(error) => (None, "unknown".into(), "failed", Some(error)),
        };
        roots.push(RootRecord {
            root_id: format!("root-{:04}", roots.len() + 1),
            requested_path: input.clone(),
            path,
            state: state.into(),
            volume_group: group,
            file_count: 0,
            bytes: 0,
            warnings: vec![],
            error,
            elapsed_seconds: 0.0,
            scan_options: options.clone(),
        });
    }
    let mut notes = Vec::new();
    for (index, root) in roots.iter().enumerate() {
        let Some(path) = root.path.as_ref() else {
            continue;
        };
        for other in &roots[..index] {
            if let Some(other_path) = other.path.as_ref() {
                if path.starts_with(other_path) || other_path.starts_with(path) {
                    notes.push(format!("{} / {}: 선택 범위가 겹칩니다. 각 범위의 제외 규칙은 유지하고 동일 경로는 전체 목록에서 한 번 집계합니다.", other.requested_path.display(), root.requested_path.display()));
                }
            }
        }
    }
    let mut grouped: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, root) in roots.iter().enumerate() {
        if root.path.is_some() {
            grouped
                .entry(root.volume_group.clone())
                .or_default()
                .push(index);
        }
    }
    let queue = Mutex::new(grouped.into_values().collect::<VecDeque<_>>());
    let results = Mutex::new(Vec::new());
    let workers = max_workers.min(queue.lock().unwrap_or_else(|e| e.into_inner()).len());
    std::thread::scope(|scope| {
        let mut handles = Vec::new();
        for _ in 0..workers {
            let roots = &roots;
            let queue = &queue;
            let results = &results;
            handles.push(scope.spawn(move || loop {
                let group = queue.lock().unwrap_or_else(|e| e.into_inner()).pop_front();
                let Some(group) = group else {
                    break;
                };
                for index in group {
                    if cancel.load(Ordering::Relaxed) {
                        break;
                    }
                    let root = &roots[index];
                    let root_started = Instant::now();
                    let callback =
                        |event| emit(progress, &root.root_id, Some(&root.requested_path), event);
                    emit(
                        progress,
                        &root.root_id,
                        Some(&root.requested_path),
                        Progress {
                            phase: "scan_start".into(),
                            completed: 0,
                            total: None,
                            message: root.requested_path.display().to_string(),
                        },
                    );
                    let result = core::scan(
                        root.path.as_ref().expect("validated root"),
                        options,
                        cancel,
                        Some(&callback),
                    );
                    results.lock().unwrap_or_else(|e| e.into_inner()).push((
                        index,
                        result,
                        root_started.elapsed().as_secs_f64(),
                    ));
                }
            }));
        }
        for handle in handles {
            let _ = handle.join();
        }
    });
    let mut entries: BTreeMap<PathBuf, (FileEntry, MultiFile)> = BTreeMap::new();
    let mut rejected = HashSet::new();
    let mut conflict_roots = HashSet::new();
    let mut inventory_warnings = Vec::new();
    let mut total_inventory_warning_count = 0_u64;
    let mut finished = results.into_inner().unwrap_or_else(|e| e.into_inner());
    finished.sort_unstable_by_key(|(index, _, _)| *index);
    for (index, result, elapsed) in finished {
        let root = &mut roots[index];
        root.elapsed_seconds = elapsed;
        match result {
            Ok(scan) => {
                root.file_count = scan.files.len() as u64;
                root.bytes = scan.total_size();
                root.state = if scan.cancelled {
                    "cancelled"
                } else if scan.warnings.is_empty() {
                    "complete"
                } else {
                    "partial"
                }
                .into();
                root.warnings = scan.warnings;
                for entry in scan.files {
                    if rejected.contains(&entry.path) {
                        continue;
                    }
                    if let Some((previous, record)) = entries.get_mut(&entry.path) {
                        if previous.size != entry.size
                            || previous.modified_ns != entry.modified_ns
                            || previous.identity != entry.identity
                        {
                            let path = entry.path.clone();
                            rejected.insert(path.clone());
                            conflict_roots.extend(record.root_ids.iter().cloned());
                            conflict_roots.insert(root.root_id.clone());
                            inventory_warning(&mut inventory_warnings, &mut total_inventory_warning_count, &path,
                                "겹친 폴더를 조회하는 동안 같은 경로의 파일이 바뀌어 전체 결과에서 제외했습니다.");
                            continue;
                        }
                        record.root_ids.push(root.root_id.clone());
                        record
                            .relative_paths
                            .insert(root.root_id.clone(), entry.relative_path.replace('\\', "/"));
                    } else {
                        let record = MultiFile {
                            path: entry.path.clone(),
                            root_ids: vec![root.root_id.clone()],
                            owner_root_id: root.root_id.clone(),
                            relative_paths: BTreeMap::from([(
                                root.root_id.clone(),
                                entry.relative_path.replace('\\', "/"),
                            )]),
                            size: entry.size,
                            kind: entry.kind.clone(),
                            extension: entry.extension.clone(),
                            modified_ns: entry.modified_ns.to_string(),
                            created_ns: entry.created_ns.map(|n| n.to_string()),
                        };
                        entries.insert(entry.path.clone(), (entry, record));
                    }
                }
            }
            Err(error) => {
                root.state = "failed".into();
                root.error = Some(error);
            }
        }
    }
    let cancelled =
        cancel.load(Ordering::Relaxed) || roots.iter().any(|root| root.state == "cancelled");
    for root in &mut roots {
        if conflict_roots.contains(&root.root_id) {
            if root.state == "complete" {
                root.state = "partial".into();
            }
            root.warnings.push(
                "겹친 폴더의 동일 경로가 탐색 사이에 변경되어 전체 목록에서 제외했습니다.".into(),
            );
        }
        if root.state == "waiting" {
            root.state = if cancelled { "cancelled" } else { "failed" }.into();
            if !cancelled {
                root.error = Some("탐색 작업이 결과를 반환하지 않았습니다.".into());
            }
        }
        let path = root.path.as_ref().unwrap_or(&root.requested_path);
        if let Some(error) = &root.error {
            inventory_warning(
                &mut inventory_warnings,
                &mut total_inventory_warning_count,
                path,
                error,
            );
        }
        total_inventory_warning_count =
            total_inventory_warning_count.saturating_add(root.warnings.len() as u64);
        let available = core::DUPLICATE_WARNING_LIMIT.saturating_sub(inventory_warnings.len());
        inventory_warnings.extend(root.warnings.iter().take(available).map(|message| {
            DuplicateWarning {
                path: path.clone(),
                phase: "inventory".into(),
                message: message.clone(),
            }
        }));
    }
    for path in &rejected {
        entries.remove(path);
    }
    let inventory_complete = !cancelled
        && total_inventory_warning_count == 0
        && roots.iter().all(|root| root.state == "complete");
    let (inventory, files): (Vec<FileEntry>, Vec<MultiFile>) = entries.into_values().unzip();
    let scan_seconds = started.elapsed().as_secs_f64();
    let mut exact = MultiDuplicates {
        requested: duplicates,
        complete: None,
        cancelled: false,
        warnings: vec![],
        warning_count: 0,
        warnings_truncated: false,
        metrics: DuplicateMetrics::default(),
        groups: vec![],
    };
    let hash_started = Instant::now();
    if duplicates {
        if cancelled {
            exact.cancelled = true;
            exact.complete = Some(false);
            exact.warnings = inventory_warnings;
            exact.warning_count = total_inventory_warning_count;
            exact.warnings_truncated = exact.warning_count > exact.warnings.len() as u64;
        } else {
            let callback = |event| emit(progress, "", None, event);
            match core::analyze_duplicate_files(
                &inventory,
                inventory_warnings,
                total_inventory_warning_count,
                cancel,
                Some(&callback),
            ) {
                Ok(analysis) => {
                    exact.complete = Some(analysis.complete && inventory_complete);
                    exact.warnings = analysis.warnings;
                    exact.warning_count = analysis.warning_count;
                    exact.warnings_truncated = analysis.warnings_truncated;
                    exact.metrics = analysis.metrics;
                    let membership: HashMap<_, _> = files
                        .iter()
                        .map(|file| (&file.path, &file.root_ids))
                        .collect();
                    exact.groups = analysis
                        .groups
                        .into_iter()
                        .map(|group| {
                            let mut root_ids: Vec<_> = group
                                .paths
                                .iter()
                                .filter_map(|path| membership.get(path))
                                .flat_map(|ids| ids.iter().cloned())
                                .collect();
                            root_ids.sort_unstable();
                            root_ids.dedup();
                            ReadOnlyGroup {
                                paths: group.paths,
                                size: group.size,
                                sha256: group.sha256,
                                group_source_count: root_ids.len(),
                                root_ids,
                                action_authority: false,
                            }
                        })
                        .collect();
                }
                Err(error) => {
                    exact.complete = Some(false);
                    exact.cancelled = cancel.load(Ordering::Relaxed);
                    exact.warnings.push(DuplicateWarning {
                        path: PathBuf::new(),
                        phase: "duplicates".into(),
                        message: error,
                    });
                    exact.warning_count = total_inventory_warning_count.saturating_add(1);
                    exact.warnings_truncated = exact.warning_count > exact.warnings.len() as u64;
                }
            }
        }
    }
    let duplicate_seconds = if duplicates {
        hash_started.elapsed().as_secs_f64()
    } else {
        0.0
    };
    let cancelled = cancelled || exact.cancelled || cancel.load(Ordering::Relaxed);
    let complete = inventory_complete && !cancelled && exact.complete.unwrap_or(true);
    Ok(MultiAnalysis {
        schema: "folder-organizer.multiroot.v1".into(),
        app_version: env!("CARGO_PKG_VERSION").into(),
        action_authority: false,
        complete,
        inventory_complete,
        cancelled,
        roots,
        files,
        notes,
        duplicate_inputs,
        duplicates: exact,
        timings: Timings {
            elapsed_seconds: started.elapsed().as_secs_f64(),
            scan_seconds,
            duplicate_seconds,
        },
        scheduling: Scheduling {
            max_workers,
            policy:
                "same-volume/server sequential; independent groups bounded; exact hashing serial"
                    .into(),
        },
    })
}
