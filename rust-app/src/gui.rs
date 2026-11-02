//! Native desktop presentation. Filesystem work never runs in the paint loop.
use crate::core::{self, AppResult, DuplicateGroup, Progress, ScanOptions, ScanResult};
use crate::operations::{
    self, HistoryRecord, LogRecord, OperationPlan, OperationReceipt, TransferKind,
};
use crate::{
    merging, multiroot, recovery, reporting, rules, similarity, updater, workspace_actions,
};
use chrono::{DateTime, Local};
use eframe::egui::{self, Color32, RichText};
use egui_extras::{Column, TableBuilder};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    mpsc, Arc,
};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const ACCENT: Color32 = Color32::from_rgb(68, 158, 255);
const VERSION: &str = env!("CARGO_PKG_VERSION");
const COMPACTION_SCOPE: &str = "압축 복원은 파일 내용과 수정일을 보존하며, 권한·추가 속성의 완전한 재현은 별도 확인이 필요합니다.";
const HELP: &[(&str, &str)] = &[
    ("처음 사용하기", "폴더를 선택하고 분석하세요. 파일별 크기·수정일·실제 생성일을 확인할 수 있습니다. 검사 메뉴에서 완전 중복과 유사 후보를 따로 찾습니다. 파일은 외부에 업로드하지 않습니다."),
    ("완전 중복과 유사", "완전 중복은 크기·전체 SHA-256·실제 바이트가 같은 파일입니다. 유사 후보는 이름·제한된 텍스트·이미지 특징이 가깝다는 뜻이며 삭제 근거가 아닙니다. 유사도 100%도 완전 중복을 뜻하지 않습니다."),
    ("첫 파일 제외 삭제", "중복 결과에 표시된 첫 파일과 보호 폴더의 파일을 남깁니다. 전용 버튼은 현재 검증 결과를 재사용하며 추가 확인·전체 재검사 없이 나머지를 delete_files로 이동합니다. 변경된 결과는 거부합니다. 첫 파일은 가장 좋은 원본이라는 뜻이 아닙니다."),
    ("파일 선택", "체크박스는 실제 작업 대상입니다. 화면 필터로 숨겨진 체크 파일도 선택 상태를 유지합니다. 보존 규칙으로 선택한 뒤 미리보기에서 전체 경로를 확인하세요. 보호 폴더는 자동 선택과 첫 파일 제외 정리에서 보존합니다."),
    ("삭제 파일과 NAS", "이 버전은 Windows·NAS 휴지통 이동을 추측하지 않고 delete_files에 보관합니다. #recycle 폴더가 있다는 것만으로 파일별 휴지통 복원이 보장되지는 않습니다. 새 삭제는 quarantine·retained를 만들지 않습니다."),
    ("규칙과 폴더 매핑", "분류 규칙은 위에서 아래로 첫 일치 하나를 적용합니다. {규칙}/{종류}/{수정일:%Y/%m}처럼 한글·영문 기호를 사용할 수 있습니다. 생성일이 제공되지 않는 파일은 생성일 규칙에서 건너뜁니다. 처음에는 복사로 시험하세요."),
    ("내용 병합", "UTF-8 TXT·Markdown의 같은 문단, 같은 열 제목을 가진 CSV·TSV의 같은 행을 한 번씩 남긴 새 파일을 만듭니다. 기본은 원본 유지입니다. 의미나 충돌을 자동 판단하지 않습니다. 2~20개·파일당 8 MiB 범위입니다."),
    ("작업 기록과 복원", "Rust 버전 기록은 .folder-organizer-rust에 별도로 저장합니다. Python 기록을 변환하거나 삭제하지 않습니다. 원래 경로에 다른 파일이 있으면 덮어쓰지 않습니다. delete_files와 인증 정보·보관 데이터를 함께 유지하세요."),
    ("복원 데이터 최적화", "완료 작업을 선택해 별도로 요청하면 실제 보관 데이터를 1 MiB 조각 중복 제거·무손실 압축으로 줄입니다. 검증과 기록을 마친 뒤 사본을 줄이며, 효과가 없으면 원본 저장을 유지합니다. 메타데이터만으로 잃어버린 원문을 만들 수 없습니다."),
    ("손상 검사와 새 복구본", "지원 ZIP·Office ZIP·이미지·PDF에서 읽을 수 있는 내용만 새 파일로 보존합니다. 원본을 고치거나 디스크 삭제 영역을 되살리지 않습니다. 암호화·해석 한도·부분 복구의 경고를 확인하세요."),
    ("검사 범위와 속도", "목록 탐색과 요약은 항목을 한 번씩 방문합니다. 정렬과 실제 바이트 검증 비용은 별도입니다. 큰 유사 비교는 제한된 후보를 사용하므로 일부 쌍을 놓칠 수 있습니다. 취소·권한 오류·부분 검사 경고를 확인하세요."),
    ("폴더 작업 공간", "메인 화면에서 여러 폴더를 등록하고 체크한 폴더를 함께 분석합니다. 서로 다른 폴더의 완전 중복도 실제 바이트로 확인합니다. 폴더를 한 개 또는 여러 개 분석한 뒤 같은 화면에서 파일을 체크하고 검사·정리를 실행합니다. 각 실제 폴더의 작업 기록에서 복원합니다. 중첩 경로·부분 실패·취소 안내를 확인하세요."),
    ("자동 업데이트", "Rust 배포판은 Rust ZIP·체크섬·버전 표식을 확인합니다. Python 배포판과 섞어 설치하지 않습니다. 업데이트는 파일 작업을 마친 뒤 앱이 종료된 후 별도 도우미가 적용합니다. 소스 실행에서는 지원하지 않습니다."),
];

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Settings {
    dark: bool,
    recent: Vec<PathBuf>,
    multi_roots: Vec<PathBuf>,
    protected: Vec<PathBuf>,
    preferred: Option<PathBuf>,
    retention: usize,
    scan: ScanOptions,
    similar: similarity::SimilarOptions,
    rules: Vec<rules::Rule>,
    automatic_updates: bool,
}
impl Default for Settings {
    fn default() -> Self {
        Self {
            dark: true,
            recent: vec![],
            multi_roots: vec![],
            protected: vec![],
            preferred: None,
            retention: 0,
            scan: ScanOptions::default(),
            similar: similarity::SimilarOptions::default(),
            rules: vec![rules::Rule::default()],
            automatic_updates: true,
        }
    }
}

fn settings_directory() -> PathBuf {
    if let Some(path) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(path).join("FolderOrganizerRust");
    }
    if let Some(path) = std::env::var_os("XDG_CONFIG_HOME") {
        return PathBuf::from(path).join("folder-organizer-rust");
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(std::env::temp_dir)
        .join(".config/folder-organizer-rust")
}
fn load_settings() -> Settings {
    let path = settings_directory().join("settings.json");
    if fs::symlink_metadata(&path)
        .map(|m| m.file_type().is_symlink() || m.len() > 256 * 1024)
        .unwrap_or(true)
    {
        return Settings::default();
    }
    fs::read(path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}
fn save_settings(settings: &Settings) {
    let directory = settings_directory();
    if fs::symlink_metadata(&directory).is_ok_and(|m| m.file_type().is_symlink()) {
        return;
    }
    if fs::create_dir_all(&directory).is_err() {
        return;
    }
    if let Ok(bytes) = serde_json::to_vec_pretty(settings) {
        let temporary = directory.join(format!("settings-{}.tmp", std::process::id()));
        if let Ok(mut file) = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)
        {
            if file.write_all(&bytes).is_ok() && file.sync_all().is_ok() {
                // This file contains preferences only, never restore authority.
                let target = directory.join("settings.json");
                #[cfg(windows)]
                {
                    let _ = fs::remove_file(&target);
                }
                let _ = fs::rename(&temporary, target);
            }
            let _ = fs::remove_file(temporary);
        }
    }
}

fn date_text(nanoseconds: i128) -> String {
    let Ok(seconds) = i64::try_from(nanoseconds.div_euclid(1_000_000_000)) else {
        return "날짜 범위 초과".into();
    };
    DateTime::from_timestamp(seconds, nanoseconds.rem_euclid(1_000_000_000) as u32)
        .map(|value| {
            value
                .with_timezone(&Local)
                .format("%Y-%m-%d %H:%M")
                .to_string()
        })
        .unwrap_or_else(|| "날짜 범위 초과".into())
}
fn bytes_text(size: u64) -> String {
    let units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let mut value = size as f64;
    let mut unit = 0;
    while value >= 1024.0 && unit < units.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    if unit == 0 {
        format!("{size} B")
    } else {
        format!("{value:.1} {}", units[unit])
    }
}
fn kind_text(kind: TransferKind) -> &'static str {
    match kind {
        TransferKind::Copy => "복사",
        TransferKind::Move => "이동",
        TransferKind::Delete => "삭제 보관",
    }
}
fn time_now() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[derive(Clone)]
struct RowCache {
    search: String,
    modified: String,
    created: String,
    size: String,
}
#[derive(Clone, Copy)]
enum DuplicateRow {
    Header(usize, usize),
    File(usize, usize),
}
#[derive(Clone, Copy, PartialEq)]
enum View {
    Files,
    Duplicates,
    Similar,
}
#[derive(Clone, Copy, PartialEq)]
enum Sort {
    Name,
    Size,
    Modified,
    Created,
}
#[derive(Default)]
struct Overview {
    kinds: Vec<(String, u64, usize)>,
    folders: Vec<(String, u64, usize)>,
    largest: Vec<(PathBuf, u64)>,
    empty: Vec<PathBuf>,
    old: usize,
    bytes: u64,
    files: usize,
}
struct ImagePreview {
    dimensions: [usize; 2],
    rgba: Vec<u8>,
}
struct FileComparison {
    path: PathBuf,
    size: u64,
    modified: String,
    created: String,
    sha256: String,
    text: Option<String>,
    image: Option<ImagePreview>,
}
struct Comparison {
    sides: Vec<FileComparison>,
    textures: Vec<Option<egui::TextureHandle>>,
}
enum ResultData {
    Multi(multiroot::MultiAnalysis),
    Workspace(multiroot::WorkspaceSession),
    BatchPlan(workspace_actions::BatchPlan),
    BatchReceipt(workspace_actions::BatchReceipt),
    Duplicates(core::DuplicateAnalysis),
    Similar(similarity::SimilarAnalysis),
    Plan(OperationPlan),
    Receipt(OperationReceipt, bool),
    MergePreview(merging::MergePreview),
    History(Vec<HistoryRecord>),
    Logs(Vec<LogRecord>),
    Overview(Overview),
    Compare(Vec<FileComparison>),
    Health(Vec<(PathBuf, AppResult<recovery::HealthResult>)>),
    Repair(recovery::RepairResult),
    Compact(operations::CompactionResult),
    Update(Option<updater::ReleaseInfo>),
    Prepared(updater::UpdatePlan),
    Classified(OperationPlan, Vec<String>),
    Merged(PathBuf, Option<OperationReceipt>),
    WorkspaceMerged(PathBuf, Option<workspace_actions::BatchReceipt>),
    Message(String),
}
enum WorkerEvent {
    Progress(Progress),
    MultiProgress(multiroot::MultiProgress),
    Finished(AppResult<ResultData>),
}
struct Busy {
    name: String,
    started: Instant,
    cancel: Arc<AtomicBool>,
}

struct MultiFolderInput {
    path: PathBuf,
    selected: bool,
}

#[derive(Clone, Copy)]
enum MultiDuplicateRow {
    Header(usize),
    File(usize, usize),
}

// Explicit release-validation mode. It drives the normal read-only window and
// worker, waits for the external native screenshot, and never starts an update.
struct MultiRootSmoke {
    workspace: bool,
    roots: Vec<PathBuf>,
    options: ScanOptions,
    report: PathBuf,
    capture_done: PathBuf,
    started: Instant,
    launched: bool,
    result_frames: u32,
    ready_at: Option<Instant>,
}

impl MultiRootSmoke {
    fn requested() -> AppResult<Option<Self>> {
        let workspace = std::env::args().any(|argument| argument == "--workspace-smoke");
        if !workspace && !std::env::args().any(|argument| argument == "--multiroot-smoke") {
            return Ok(None);
        }
        let prefix = if workspace {
            "FOLDER_ORGANIZER_WORKSPACE_SMOKE"
        } else {
            "FOLDER_ORGANIZER_MULTI_SMOKE"
        };
        let encoded = std::env::var(format!("{prefix}_ROOTS"))
            .map_err(|_| "Multi-root smoke roots are not configured".to_owned())?;
        let roots: Vec<PathBuf> =
            serde_json::from_str(&encoded).map_err(|error| error.to_string())?;
        if roots.is_empty() || roots.len() > 64 || roots.iter().any(|root| !root.is_absolute()) {
            return Err("Multi-root smoke requires 1–64 absolute fixture folders".into());
        }
        let report = std::env::var_os(format!("{prefix}_REPORT"))
            .map(PathBuf::from)
            .filter(|path| path.is_absolute())
            .ok_or("Multi-root smoke requires an absolute external report path")?;
        let exclude_patterns = match std::env::var(format!("{prefix}_EXCLUDES")) {
            Ok(encoded) => {
                serde_json::from_str::<Vec<String>>(&encoded).map_err(|error| error.to_string())?
            }
            Err(std::env::VarError::NotPresent) => vec![],
            Err(error) => return Err(error.to_string()),
        };
        let mut marker = report.as_os_str().to_os_string();
        marker.push(".capture-done");
        Ok(Some(Self {
            workspace,
            roots,
            options: ScanOptions {
                exclude_patterns,
                ..Default::default()
            },
            report,
            capture_done: PathBuf::from(marker),
            started: Instant::now(),
            launched: false,
            result_frames: 0,
            ready_at: None,
        }))
    }
}

pub struct OrganizerApp {
    settings: Settings,
    root: Option<PathBuf>,
    scan: Option<Arc<ScanResult>>,
    rows: Vec<RowCache>,
    index: HashMap<PathBuf, usize>,
    visible: Vec<usize>,
    query: String,
    quick_filter: usize,
    sort: Sort,
    descending: bool,
    selected: HashSet<PathBuf>,
    selected_bytes: u64,
    selection_bytes_dirty: bool,
    duplicate_rows: Vec<DuplicateRow>,
    duplicates: Vec<DuplicateGroup>,
    duplicate_details: Option<core::DuplicateAnalysis>,
    scan_options: ScanOptions,
    similar: Option<similarity::SimilarAnalysis>,
    view: View,
    groups_actionable: bool,
    status: String,
    error: Option<String>,
    notices: VecDeque<String>,
    receiver: Option<mpsc::Receiver<WorkerEvent>>,
    busy: Option<Busy>,
    progress: Option<Progress>,
    plan: Option<OperationPlan>,
    merge: Option<merging::MergePreview>,
    archive_merge: bool,
    settings_open: bool,
    scan_excludes: String,
    rules_open: bool,
    rules_destination: String,
    rule_copy: bool,
    rule_index: usize,
    rule_editor_index: Option<usize>,
    rule_extensions: String,
    rule_start: String,
    rule_end: String,
    history_open: bool,
    history: Vec<HistoryRecord>,
    history_selected: Option<String>,
    compaction_request: Option<(PathBuf, String)>,
    compaction_result: Option<operations::CompactionResult>,
    logs_open: bool,
    logs: Vec<LogRecord>,
    logs_root: Option<PathBuf>,
    log_query: String,
    activities_open: bool,
    help_open: bool,
    help_query: String,
    overview: Option<Overview>,
    comparison: Option<Comparison>,
    health: Option<Vec<(PathBuf, AppResult<recovery::HealthResult>)>>,
    update: Option<updater::ReleaseInfo>,
    prepared: Option<updater::UpdatePlan>,
    next_update_check: Instant,
    quit_requested: bool,
    smoke_frames: Option<u32>,
    multi_smoke: Option<MultiRootSmoke>,
    multi_open: bool,
    multi_folders: Vec<MultiFolderInput>,
    multi_result: Option<Arc<multiroot::MultiAnalysis>>,
    multi_progress: BTreeMap<String, multiroot::MultiProgress>,
    multi_root_labels: BTreeMap<String, String>,
    multi_rows: Vec<MultiDuplicateRow>,
    multi_workers: usize,
    multi_show_files: bool,
    multi_warnings: Vec<String>,
    multi_warning_total: usize,
    workspace_root: Option<usize>,
    workspace_file: Option<usize>,
    workspace_index: HashMap<PathBuf, usize>,
    workspace_groups: HashMap<PathBuf, usize>,
    workspace_rows: Vec<RowCache>,
    workspace_visible: Vec<usize>,
    workspace_query: String,
    workspace_session: Option<Arc<multiroot::WorkspaceSession>>,
    workspace_stale: bool,
    batch_plan: Option<workspace_actions::BatchPlan>,
    ribbon_tab: usize,
    workspace_extension: Option<String>,
    workspace_types: Vec<(String, usize, u64, usize, u64)>,
}

impl OrganizerApp {
    pub fn new(context: &eframe::CreationContext<'_>) -> Self {
        install_fonts(&context.egui_ctx);
        let mut settings = load_settings();
        let multi_smoke = MultiRootSmoke::requested().unwrap_or_else(|error| {
            eprintln!("Multi-root GUI smoke configuration failed: {error}");
            std::process::exit(1);
        });
        if multi_smoke.as_ref().is_some_and(|smoke| smoke.workspace) {
            settings.dark = true;
        }
        let smoke_frames = (multi_smoke.is_none()
            && (std::env::args().any(|arg| arg == "--gui-smoke")
                || std::env::var("FOLDER_ORGANIZER_RUST_SMOKE").is_ok_and(|value| value == "1")))
        .then_some(0);
        apply_theme(&context.egui_ctx, settings.dark);
        let scan_excludes = settings.scan.exclude_patterns.join("\n");
        let multi_folders = settings
            .multi_roots
            .iter()
            .cloned()
            .map(|path| MultiFolderInput {
                path,
                selected: true,
            })
            .collect();
        Self {
            settings,
            root: None,
            scan: None,
            rows: vec![],
            index: HashMap::new(),
            visible: vec![],
            query: String::new(),
            quick_filter: 0,
            sort: Sort::Name,
            descending: false,
            selected: HashSet::new(),
            selected_bytes: 0,
            selection_bytes_dirty: true,
            duplicate_rows: vec![],
            duplicates: vec![],
            duplicate_details: None,
            scan_options: ScanOptions::default(),
            similar: None,
            view: View::Files,
            groups_actionable: false,
            status: "폴더를 추가하고 체크한 뒤 분석 시작을 누르세요.".into(),
            error: None,
            notices: VecDeque::new(),
            receiver: None,
            busy: None,
            progress: None,
            plan: None,
            merge: None,
            archive_merge: false,
            settings_open: false,
            scan_excludes,
            rules_open: false,
            rules_destination: String::new(),
            rule_copy: true,
            rule_index: 0,
            rule_editor_index: None,
            rule_extensions: String::new(),
            rule_start: String::new(),
            rule_end: String::new(),
            history_open: false,
            history: vec![],
            history_selected: None,
            compaction_request: None,
            compaction_result: None,
            logs_open: false,
            logs: vec![],
            logs_root: None,
            log_query: String::new(),
            activities_open: false,
            help_open: false,
            help_query: String::new(),
            overview: None,
            comparison: None,
            health: None,
            update: None,
            prepared: None,
            next_update_check: Instant::now() + Duration::from_secs(5),
            quit_requested: false,
            smoke_frames,
            multi_smoke,
            multi_open: true,
            multi_folders,
            multi_result: None,
            multi_progress: BTreeMap::new(),
            multi_root_labels: BTreeMap::new(),
            multi_rows: vec![],
            multi_workers: 2,
            multi_show_files: true,
            multi_warnings: vec![],
            multi_warning_total: 0,
            workspace_root: None,
            workspace_file: None,
            workspace_index: HashMap::new(),
            workspace_groups: HashMap::new(),
            workspace_rows: vec![],
            workspace_visible: vec![],
            workspace_query: String::new(),
            workspace_session: None,
            workspace_stale: false,
            batch_plan: None,
            ribbon_tab: 0,
            workspace_extension: None,
            workspace_types: vec![],
        }
    }
    fn note(&mut self, message: impl Into<String>) {
        let line = format!(
            "{}  {}",
            date_text(time_now() as i128 * 1_000_000_000),
            message.into()
        );
        self.notices.push_back(line.clone());
        while self.notices.len() > 2000 {
            self.notices.pop_front();
        }
        let directory = settings_directory();
        if fs::create_dir_all(&directory).is_ok() {
            let path = directory.join("activity.log");
            if fs::symlink_metadata(&path).is_ok_and(|m| m.file_type().is_symlink()) {
                return;
            }
            if path.metadata().is_ok_and(|m| m.len() > 2 * 1024 * 1024) {
                let _ = fs::rename(&path, directory.join("activity.previous.log"));
            }
            if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(path) {
                let _ = writeln!(file, "{line}");
            }
        }
    }
    fn start<F>(&mut self, context: &egui::Context, name: &str, operation: F)
    where
        F: FnOnce(&AtomicBool, &dyn Fn(Progress)) -> AppResult<ResultData> + Send + 'static,
    {
        self.start_worker(context, name, move |cancel, sender, context| {
            let callback = |progress| {
                let _ = sender.send(WorkerEvent::Progress(progress));
                context.request_repaint();
            };
            operation(cancel, &callback)
        });
    }
    fn start_worker<F>(&mut self, context: &egui::Context, name: &str, operation: F)
    where
        F: FnOnce(&AtomicBool, &mpsc::Sender<WorkerEvent>, &egui::Context) -> AppResult<ResultData>
            + Send
            + 'static,
    {
        if self.busy.is_some() {
            return;
        }
        let (sender, receiver) = mpsc::channel();
        let cancel = Arc::new(AtomicBool::new(false));
        self.busy = Some(Busy {
            name: name.into(),
            started: Instant::now(),
            cancel: cancel.clone(),
        });
        self.progress = None;
        self.receiver = Some(receiver);
        self.error = None;
        self.note(format!("시작: {name}"));
        let context = context.clone();
        std::thread::spawn(move || {
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| operation(&cancel, &sender, &context)))
                .unwrap_or_else(|_| Err("작업이 예기치 않게 중단되었습니다. 기록과 원본을 유지하고 실행 로그를 확인하세요.".into()));
            let _ = sender.send(WorkerEvent::Finished(result));
            context.request_repaint();
        });
    }
    fn poll(&mut self) {
        let events: Vec<_> = self
            .receiver
            .as_ref()
            .map(|receiver| receiver.try_iter().collect())
            .unwrap_or_default();
        for event in events {
            match event {
                WorkerEvent::Progress(progress) => self.progress = Some(progress),
                WorkerEvent::MultiProgress(progress) => {
                    if progress.phase == "scan_start" {
                        self.multi_root_labels
                            .insert(progress.root_id.clone(), progress.message.clone());
                    }
                    self.progress = Some(Progress {
                        phase: progress.phase.clone(),
                        completed: progress.completed,
                        total: progress.total,
                        message: progress.message.clone(),
                    });
                    self.multi_progress
                        .insert(progress.root_id.clone(), progress);
                }
                WorkerEvent::Finished(result) => {
                    let name = self.busy.take().map(|job| job.name).unwrap_or_default();
                    self.receiver = None;
                    self.progress = None;
                    match result {
                        Ok(data) => {
                            self.finish(data);
                            self.note(format!("완료: {name}"));
                        }
                        Err(error) => {
                            if self.multi_open
                                && self
                                    .workspace_session
                                    .as_ref()
                                    .is_some_and(|session| !session.actionable())
                            {
                                self.mark_workspace_stale("파일 작업을 완료하지 못했습니다. 기록과 변경 내용을 확인한 뒤 새로 분석하세요.");
                                self.rebuild_workspace_rows();
                            }
                            self.status =
                                "작업을 완료하지 못했습니다. 오류 내용을 확인하세요.".into();
                            self.note(format!("실패: {name} · {error}"));
                            self.error = Some(error);
                        }
                    }
                }
            }
        }
    }
    fn finish(&mut self, data: ResultData) {
        match data {
            ResultData::Workspace(session) => {
                let report = session.report().clone();
                let groups = session.groups().to_vec();
                let actionable = session.actionable();
                self.finish(ResultData::Multi(report));
                self.duplicates = groups;
                self.groups_actionable = actionable;
                self.workspace_session = Some(Arc::new(session));
                self.view = View::Files;
                self.status = format!("분석 완료 · 파일 {}개 · 완전 중복 {}그룹 · 파일을 체크해 검사·정리를 이어가세요{}",
                    self.multi_result.as_ref().map_or(0, |report| report.files.len()), self.duplicates.len(),
                    if actionable { "" } else { " · 일부 미완료: 정리 비활성" });
            }
            ResultData::BatchPlan(plan) => self.batch_plan = Some(plan),
            ResultData::BatchReceipt(batch) => {
                let removed: HashSet<_> = batch
                    .receipts
                    .iter()
                    .flat_map(|receipt| receipt.items.iter())
                    .filter(|item| item.source_removed)
                    .map(|item| item.source.clone())
                    .collect();
                if let Some(report) = &mut self.multi_result {
                    let report = Arc::make_mut(report);
                    report.files.retain(|file| !removed.contains(&file.path));
                    report.duplicates.groups.clear();
                    for root in &mut report.roots {
                        root.file_count = report
                            .files
                            .iter()
                            .filter(|file| file.root_ids.contains(&root.root_id))
                            .count() as u64;
                        root.bytes = report
                            .files
                            .iter()
                            .filter(|file| file.root_ids.contains(&root.root_id))
                            .map(|file| file.size)
                            .sum();
                    }
                }
                self.mark_workspace_stale("정리 기록으로 목록을 갱신했습니다. 새 파일·외부 변경을 포함한 전체 범위는 다시 분석해야 합니다.");
                self.selected.retain(|path| !removed.contains(path));
                self.groups_actionable = false;
                self.duplicates.clear();
                self.multi_rows.clear();
                self.similar = None;
                self.view = View::Files;
                self.selection_bytes_dirty = true;
                self.rebuild_workspace_rows();
                let processed = batch
                    .receipts
                    .iter()
                    .flat_map(|receipt| receipt.items.iter())
                    .filter(|item| item.error.is_none() && item.status != "planned")
                    .count();
                self.status = format!(
                    "{} · 처리 {}개 · {}개 폴더에 복원 기록 저장 · 다음 정리는 새로 분석하세요",
                    if batch.completed {
                        "정리 완료"
                    } else if batch.cancelled {
                        "정리 중단"
                    } else {
                        "일부 작업 미완료"
                    },
                    processed,
                    batch.receipts.len()
                );
                if let Some(error) = batch.error {
                    self.error = Some(error);
                }
            }
            ResultData::Multi(report) => {
                self.workspace_stale = false;
                self.status = format!(
                    "여러 폴더 통합 분석 · 폴더 {}개 · 파일 {}개 · 완전 중복 {}그룹{}",
                    report.roots.len(),
                    report.files.len(),
                    report.duplicates.groups.len(),
                    if report.complete {
                        " · 완료"
                    } else {
                        " · 일부 미완료"
                    }
                );
                self.multi_rows.clear();
                for (group_index, group) in report.duplicates.groups.iter().enumerate() {
                    self.multi_rows.push(MultiDuplicateRow::Header(group_index));
                    self.multi_rows.extend(
                        (0..group.paths.len())
                            .map(|file_index| MultiDuplicateRow::File(group_index, file_index)),
                    );
                }
                self.multi_warning_total = report.notes.len()
                    + report.duplicates.warnings.len()
                    + report
                        .roots
                        .iter()
                        .map(|root| root.warnings.len() + usize::from(root.error.is_some()))
                        .sum::<usize>();
                self.multi_warnings =
                    report
                        .notes
                        .iter()
                        .cloned()
                        .chain(report.roots.iter().flat_map(|root| {
                            root.error
                                .iter()
                                .chain(root.warnings.iter())
                                .map(move |message| {
                                    format!("{}: {message}", root.requested_path.display())
                                })
                        }))
                        .chain(report.duplicates.warnings.iter().map(|warning| {
                            format!("{}: {}", warning.path.display(), warning.message)
                        }))
                        .take(1000)
                        .collect();
                self.multi_result = Some(Arc::new(report));
                self.multi_open = true;
                self.rebuild_workspace_rows();
            }
            ResultData::Duplicates(mut analysis) => {
                self.status = format!(
                    "완전 중복 {}그룹 · 전체 SHA-256과 실제 바이트 검증{} · 읽은 샘플 {} · 전체 해시 {}개",
                    analysis.groups.len(),
                    if analysis.complete { " 완료" } else { " · 일부 파일 미검사" },
                    bytes_text(analysis.metrics.sample_bytes), analysis.metrics.full_hash_files
                );
                if !analysis.complete {
                    self.note(format!("중복 검사 일부 미완료: 주의 {}건. 확인된 그룹만 표시하며 전체 폴더의 중복 없음 판정이 아닙니다.", analysis.warning_count));
                    for warning in analysis.warnings.iter().take(20) {
                        self.note(format!("{}: {}", warning.path.display(), warning.message));
                    }
                }
                self.duplicates = std::mem::take(&mut analysis.groups);
                self.duplicate_details = Some(analysis);
                self.rebuild_duplicate_rows();
                self.groups_actionable = true;
                self.view = View::Duplicates;
            }
            ResultData::Similar(report) => {
                self.status = format!(
                    "유사 후보 {}쌍 · {}쌍 비교{}",
                    report.pairs.len(),
                    report.compared_pairs,
                    if report.complete {
                        ""
                    } else {
                        " · 일부 조합은 미검사"
                    }
                );
                self.similar = Some(report);
                self.view = View::Similar;
            }
            ResultData::Plan(plan) => self.plan = Some(plan),
            ResultData::Classified(plan, warnings) => {
                self.status = format!(
                    "분류 미리보기 {}개 · 경고 {}개",
                    plan.items.len(),
                    warnings.len()
                );
                if !warnings.is_empty() {
                    self.error = Some(warnings.join("\n"));
                }
                self.plan = Some(plan);
            }
            ResultData::WorkspaceMerged(path, receipt) => {
                if let Some(receipt) = receipt {
                    self.finish(ResultData::BatchReceipt(receipt));
                } else {
                    self.mark_workspace_stale(
                        "새 병합본 생성 후 전체 목록을 다시 분석해야 합니다.",
                    );
                    self.status = "원본 유지 · 새 분석으로 현재 목록을 확인하세요".into();
                }
                self.status = format!("새 병합본: {} · {}", path.display(), self.status);
            }
            ResultData::Merged(path, receipt) => {
                if let Some(receipt) = receipt {
                    self.finish(ResultData::Receipt(receipt, false));
                } else {
                    self.status = "원본 유지".into();
                }
                self.status = format!("새 병합본 생성: {} · {}", path.display(), self.status);
            }
            ResultData::MergePreview(preview) => self.merge = Some(preview),
            ResultData::Receipt(receipt, restore) => {
                if self.multi_open {
                    self.mark_workspace_stale(
                        "복원·파일 작업 후 전체 목록을 새로 분석해야 합니다.",
                    );
                    self.rebuild_workspace_rows();
                }
                self.groups_actionable = false;
                self.duplicate_details = None;
                let removed: HashSet<_> = receipt
                    .items
                    .iter()
                    .filter(|item| item.source_removed)
                    .map(|item| item.source.clone())
                    .collect();
                if !restore {
                    if let Some(scan) = &mut self.scan {
                        Arc::make_mut(scan)
                            .files
                            .retain(|entry| !removed.contains(&entry.path));
                    }
                    self.selected.retain(|path| !removed.contains(path));
                    self.rebuild_rows();
                    if let Some(report) = &mut self.similar {
                        report.pairs.retain(|pair| {
                            self.index.contains_key(&pair.left)
                                && self.index.contains_key(&pair.right)
                        });
                    }
                } else {
                    self.selected.clear();
                    self.duplicates.clear();
                    self.duplicate_rows.clear();
                    self.similar = None;
                    self.selection_bytes_dirty = true;
                }
                self.status = format!(
                    "{}{} · 처리 {}개 · 오류 {}개{}",
                    if restore { "복원" } else { "정리" },
                    if receipt.cancelled {
                        " 중단 · 완료된 항목은 기록에 유지"
                    } else {
                        " 완료"
                    },
                    receipt
                        .items
                        .iter()
                        .filter(|item| item.error.is_none())
                        .count(),
                    receipt.errors.len(),
                    if restore {
                        " · 최신 목록은 분석으로 새로 확인하세요"
                    } else {
                        " · 현재 목록을 재탐색 없이 갱신했습니다"
                    }
                );
                if !receipt.errors.is_empty() || !receipt.completed {
                    self.error = Some(format!("일부 항목을 완료하지 못했습니다. 자동 재검사는 하지 않습니다. 변경된 파일을 보존했으므로 실행 로그를 확인한 뒤 다시 분석하세요.\n{}", receipt.errors.join("\n")));
                }
            }
            ResultData::History(records) => {
                self.history = records;
                self.history_open = true;
            }
            ResultData::Logs(records) => {
                self.logs = records;
                self.logs_open = true;
            }
            ResultData::Overview(overview) => self.overview = Some(overview),
            ResultData::Compare(sides) => {
                let textures = (0..sides.len()).map(|_| None).collect();
                self.comparison = Some(Comparison { sides, textures });
            }
            ResultData::Health(results) => self.health = Some(results),
            ResultData::Repair(result) => {
                self.mark_workspace_stale("복구본 생성 후 전체 목록을 새로 분석해야 합니다.");
                self.status = format!(
                    "새 복구본: {} · 복구 항목 {}개",
                    result.output.display(),
                    result.recovered_units
                );
                if !result.issues.is_empty() {
                    self.error = Some(format!(
                        "복구본이 생성됐습니다. 부분 복구 안내:\n{}\n{}",
                        result.explanation,
                        result.issues.join("\n")
                    ));
                }
            }
            ResultData::Compact(result) => {
                self.status = format!(
                    "복원 데이터 최적화 · {}개 파일 · {} → {} · {} 절감 · 원본 보관 유지 안내 {}개",
                    result.files,
                    bytes_text(result.original_bytes),
                    bytes_text(result.stored_bytes),
                    bytes_text(result.saved_bytes),
                    result.warnings.len()
                );
                self.compaction_result = Some(result);
            }
            ResultData::Update(release) => {
                if release.is_none() {
                    self.status = "현재 Rust 배포판에 적용할 새 정식 업데이트가 없습니다.".into();
                }
                self.update = release;
            }
            ResultData::Prepared(plan) => {
                self.prepared = Some(plan);
                self.status =
                    "업데이트 준비 완료 · 파일 작업을 마친 뒤 앱을 종료하면 적용합니다.".into();
            }
            ResultData::Message(message) => self.status = message,
        }
    }
    fn rebuild_rows(&mut self) {
        self.index.clear();
        self.rows.clear();
        if let Some(scan) = &self.scan {
            self.rows.reserve(scan.files.len());
            self.index.reserve(scan.files.len());
            for (index, file) in scan.files.iter().enumerate() {
                self.index.insert(file.path.clone(), index);
                self.rows.push(RowCache {
                    search: file.relative_path.to_lowercase(),
                    modified: date_text(file.modified_ns),
                    created: file
                        .created_ns
                        .map(date_text)
                        .unwrap_or_else(|| "제공되지 않음".into()),
                    size: bytes_text(file.size),
                });
            }
        }
        self.selection_bytes_dirty = true;
        self.rebuild_duplicate_rows();
        self.filter_rows();
    }
    fn rebuild_duplicate_rows(&mut self) {
        self.duplicate_rows.clear();
        for (group_index, group) in self.duplicates.iter().enumerate() {
            let count = group
                .paths
                .iter()
                .filter(|path| self.index.contains_key(*path))
                .count();
            if count == 0 {
                continue;
            }
            self.duplicate_rows
                .push(DuplicateRow::Header(group_index, count));
            for (path_index, path) in group.paths.iter().enumerate() {
                if self.index.contains_key(path) {
                    self.duplicate_rows
                        .push(DuplicateRow::File(group_index, path_index));
                }
            }
        }
    }
    fn filter_rows(&mut self) {
        self.visible.clear();
        let Some(scan) = &self.scan else {
            return;
        };
        let query = self.query.to_lowercase();
        let year_ago = time_now().saturating_sub(365 * 24 * 3600) as i128 * 1_000_000_000;
        self.visible.extend(
            scan.files
                .iter()
                .enumerate()
                .filter(|(index, file)| {
                    (query.is_empty() || self.rows[*index].search.contains(&query))
                        && match self.quick_filter {
                            1 => file.size >= 100 * 1024 * 1024,
                            2 => file.size == 0,
                            3 => file.modified_ns < year_ago,
                            4 => file.kind == "application",
                            _ => true,
                        }
                })
                .map(|(index, _)| index),
        );
        let rows = &self.rows;
        self.visible.sort_unstable_by(|a, b| {
            let (left, right) = (&scan.files[*a], &scan.files[*b]);
            let primary = match self.sort {
                Sort::Name => rows[*a].search.cmp(&rows[*b].search),
                Sort::Size => left.size.cmp(&right.size),
                Sort::Modified => left.modified_ns.cmp(&right.modified_ns),
                Sort::Created => left.created_ns.cmp(&right.created_ns),
            };
            let order = primary.then_with(|| left.relative_path.cmp(&right.relative_path));
            if self.descending {
                order.reverse()
            } else {
                order
            }
        });
    }
    fn pick_folder(&mut self, context: &egui::Context) {
        if self.busy.is_some() {
            return;
        }
        if let Some(path) = rfd::FileDialog::new()
            .set_title("분석할 개인 데이터 폴더 선택")
            .pick_folder()
        {
            self.start_scan(context, path);
        }
    }
    fn start_scan(&mut self, context: &egui::Context, root: PathBuf) {
        if self.busy.is_some() {
            return;
        }
        self.multi_folders = vec![MultiFolderInput {
            path: root.clone(),
            selected: true,
        }];
        self.workspace_root = Some(0);
        self.workspace_folders_changed();
        self.start_multiroot(context, vec![root]);
    }
    fn start_multiroot(&mut self, context: &egui::Context, roots: Vec<PathBuf>) {
        if self.busy.is_some() || roots.is_empty() {
            return;
        }
        self.enter_workspace();
        self.workspace_file = None;
        self.workspace_rows.clear();
        self.workspace_visible.clear();
        self.workspace_index.clear();
        self.workspace_groups.clear();
        self.multi_result = None;
        self.multi_rows.clear();
        self.multi_warnings.clear();
        self.multi_warning_total = 0;
        self.multi_progress.clear();
        self.multi_root_labels.clear();
        let options = self.settings.scan.clone();
        let workers = self.multi_workers;
        self.start_worker(
            context,
            "폴더 작업 공간",
            move |cancel, sender, context| {
                let callback = |progress| {
                    let _ = sender.send(WorkerEvent::MultiProgress(progress));
                    context.request_repaint();
                };
                multiroot::analyze_workspace(
                    &roots,
                    &options,
                    true,
                    workers,
                    cancel,
                    Some(&callback),
                )
                .map(ResultData::Workspace)
            },
        );
    }
    // Global reports never inherit single-root write authority. Keeping this
    // transition explicit also closes previews prepared for an earlier root.
    fn clear_single_authority(&mut self) {
        if let Some(session) = self.workspace_session.take() {
            session.invalidate();
        }
        self.batch_plan = None;
        self.workspace_types.clear();
        self.root = None;
        self.scan = None;
        self.rows.clear();
        self.index.clear();
        self.visible.clear();
        self.selected.clear();
        self.selected_bytes = 0;
        self.selection_bytes_dirty = true;
        self.groups_actionable = false;
        self.duplicates.clear();
        self.duplicate_rows.clear();
        self.duplicate_details = None;
        self.similar = None;
        self.plan = None;
        self.merge = None;
        self.rules_open = false;
        self.history_open = false;
        self.history_selected = None;
        self.compaction_request = None;
        self.health = None;
        self.comparison = None;
        self.overview = None;
        self.compaction_result = None;
        self.logs_open = false;
    }
    fn enter_workspace(&mut self) {
        if self.busy.is_some() {
            return;
        }
        self.clear_single_authority();
        self.multi_open = true;
        self.status = "폴더를 한 개 또는 여러 개 추가한 뒤 분석하세요. 검사와 정리를 같은 결과에서 이어갑니다.".into();
    }
    fn rebuild_workspace_rows(&mut self) {
        self.workspace_rows.clear();
        self.workspace_index.clear();
        self.workspace_groups.clear();
        if let Some(report) = &self.multi_result {
            for (index, group) in report.duplicates.groups.iter().enumerate() {
                for path in &group.paths {
                    self.workspace_groups.insert(path.clone(), index);
                }
            }
            for (index, file) in report.files.iter().enumerate() {
                self.workspace_index.insert(file.path.clone(), index);
                self.workspace_rows.push(RowCache {
                    search: file.path.to_string_lossy().to_lowercase(),
                    modified: file
                        .modified_ns
                        .parse::<i128>()
                        .ok()
                        .map(date_text)
                        .unwrap_or_else(|| "확인 불가".into()),
                    created: file
                        .created_ns
                        .as_deref()
                        .and_then(|value| value.parse::<i128>().ok())
                        .map(date_text)
                        .unwrap_or_else(|| "제공되지 않음".into()),
                    size: bytes_text(file.size),
                });
            }
        }
        self.rebuild_workspace_types();
        self.filter_workspace_rows();
        self.workspace_file = self.workspace_visible.first().copied();
        self.selection_bytes_dirty = true;
    }
    fn rebuild_workspace_types(&mut self) {
        self.workspace_types.clear();
        let Some(report) = &self.multi_result else {
            return;
        };
        let copies: HashSet<_> = report
            .duplicates
            .groups
            .iter()
            .flat_map(|group| group.paths.iter().skip(1))
            .collect();
        let mut types: BTreeMap<String, (usize, u64, usize, u64)> = BTreeMap::new();
        for file in &report.files {
            let entry = types.entry(file.extension.clone()).or_default();
            entry.0 += 1;
            entry.1 = entry.1.saturating_add(file.size);
            if copies.contains(&file.path) {
                entry.2 += 1;
                entry.3 = entry.3.saturating_add(file.size);
            }
        }
        self.workspace_types = types
            .into_iter()
            .map(|(extension, (count, bytes, copies, duplicate_bytes))| {
                (extension, count, bytes, copies, duplicate_bytes)
            })
            .collect();
        self.workspace_types
            .sort_unstable_by(|left, right| right.2.cmp(&left.2).then(left.0.cmp(&right.0)));
        if self
            .workspace_extension
            .as_ref()
            .is_some_and(|selected| !self.workspace_types.iter().any(|item| &item.0 == selected))
        {
            self.workspace_extension = None;
        }
    }
    fn filter_workspace_rows(&mut self) {
        let query = self.workspace_query.trim().to_lowercase();
        let Some(report) = &self.multi_result else {
            self.workspace_visible.clear();
            return;
        };
        self.workspace_visible = self
            .workspace_rows
            .iter()
            .enumerate()
            .filter(|(index, row)| {
                (query.is_empty() || row.search.contains(&query))
                    && self
                        .workspace_extension
                        .as_ref()
                        .is_none_or(|extension| &report.files[*index].extension == extension)
            })
            .map(|(index, _)| index)
            .collect();
        let files = &report.files;
        let rows = &self.workspace_rows;
        let sort = self.sort;
        let descending = self.descending;
        self.workspace_visible.sort_unstable_by(|left, right| {
            let order = match sort {
                Sort::Name => rows[*left].search.cmp(&rows[*right].search),
                Sort::Size => files[*left].size.cmp(&files[*right].size),
                Sort::Modified => files[*left]
                    .modified_ns
                    .parse::<i128>()
                    .unwrap_or_default()
                    .cmp(
                        &files[*right]
                            .modified_ns
                            .parse::<i128>()
                            .unwrap_or_default(),
                    ),
                Sort::Created => files[*left]
                    .created_ns
                    .as_ref()
                    .and_then(|value| value.parse::<i128>().ok())
                    .cmp(
                        &files[*right]
                            .created_ns
                            .as_ref()
                            .and_then(|value| value.parse::<i128>().ok()),
                    ),
            }
            .then_with(|| files[*left].path.cmp(&files[*right].path));
            if descending {
                order.reverse()
            } else {
                order
            }
        });
    }
    fn add_workspace_folders(&mut self) {
        if self.busy.is_some() {
            return;
        }
        if let Some(paths) = rfd::FileDialog::new()
            .set_title("분석할 폴더 추가 · 여러 개 선택 가능")
            .pick_folders()
        {
            let mut changed = false;
            for path in paths {
                if !self.multi_folders.iter().any(|folder| folder.path == path) {
                    if self.multi_folders.len() >= 64 {
                        self.note("최대 64개 폴더까지 등록할 수 있습니다.");
                        break;
                    }
                    self.multi_folders.push(MultiFolderInput {
                        path,
                        selected: true,
                    });
                    self.workspace_root = Some(self.multi_folders.len() - 1);
                    changed = true;
                }
            }
            if changed {
                self.workspace_folders_changed();
            }
        }
    }
    fn workspace_folders_changed(&mut self) {
        if let Some(session) = self.workspace_session.take() {
            session.invalidate();
        }
        self.batch_plan = None;
        self.selected.clear();
        self.groups_actionable = false;
        self.duplicates.clear();
        self.similar = None;
        self.workspace_types.clear();
        self.multi_result = None;
        self.multi_rows.clear();
        self.multi_warnings.clear();
        self.multi_warning_total = 0;
        self.multi_progress.clear();
        self.workspace_file = None;
        self.workspace_index.clear();
        self.workspace_groups.clear();
        self.workspace_rows.clear();
        self.workspace_visible.clear();
        self.settings.multi_roots = self
            .multi_folders
            .iter()
            .map(|folder| folder.path.clone())
            .collect();
        if self.multi_smoke.is_none() {
            save_settings(&self.settings);
        }
        self.status = "폴더 선택이 바뀌었습니다. 분석 시작으로 현재 범위를 확인하세요.".into();
    }
    fn advance_multiroot_smoke(&mut self, context: &egui::Context) {
        let Some(smoke) = self.multi_smoke.as_ref() else {
            return;
        };
        if smoke.started.elapsed() > Duration::from_secs(30) {
            eprintln!("Multi-root GUI smoke timed out before native capture completion");
            std::process::exit(1);
        }
        if !smoke.launched {
            let roots = smoke.roots.clone();
            self.settings.scan = smoke.options.clone();
            self.multi_smoke.as_mut().unwrap().launched = true;
            self.multi_folders = roots
                .iter()
                .cloned()
                .map(|path| MultiFolderInput {
                    path,
                    selected: true,
                })
                .collect();
            self.multi_open = true;
            self.start_multiroot(context, roots);
            return;
        }
        if self.busy.is_some() {
            return;
        }
        if let Some(error) = &self.error {
            eprintln!("Multi-root GUI smoke worker failed: {error}");
            std::process::exit(1);
        }
        let Some(report) = self.multi_result.as_ref() else {
            return;
        };
        if !report.complete || report.cancelled || report.action_authority || !self.multi_open {
            eprintln!("Multi-root GUI smoke did not produce a complete visible read-only result");
            std::process::exit(1);
        }
        let smoke = self.multi_smoke.as_mut().unwrap();
        smoke.result_frames += 1;
        // Presented frames precede the ready file, so the driver can capture
        // actual final results rather than a startup or loading canvas.
        if smoke.ready_at.is_none() && smoke.result_frames >= 8 {
            let saved = if smoke.workspace {
                let selected = self
                    .workspace_file
                    .and_then(|index| report.files.get(index))
                    .map(|file| {
                        serde_json::json!({
                            "name": folder_display_name(&file.path),
                            "path": file.path,
                            "size": file.size,
                            "kind": file.kind,
                            "modified_ns": file.modified_ns,
                            "created_ns": file.created_ns,
                        })
                    });
                let value = serde_json::json!({
                    "schema": "folder-organizer-workspace-smoke-v1",
                    "app_version": VERSION,
                    "edition": "rust",
                    "main_workspace": self.multi_open,
                    "scope_mode": "global",
                    "checked_roots": self.multi_folders.iter().filter(|folder| folder.selected).map(|folder| &folder.path).collect::<Vec<_>>(),
                    "file_rows": report.files.len(),
                    "duplicate_groups": report.duplicates.groups.len(),
                    "action_authority": self.groups_actionable || report.action_authority || self.scan.is_some(),
                    "finished": report.complete && !report.cancelled,
                    "selected_snapshot": selected,
                    "layout": ["search_paths", "results", "file_properties"],
                    "theme": if self.settings.dark { "dark" } else { "light" },
                });
                (|| -> AppResult<()> {
                    let bytes =
                        serde_json::to_vec_pretty(&value).map_err(|error| error.to_string())?;
                    let mut file = OpenOptions::new()
                        .write(true)
                        .create_new(true)
                        .open(&smoke.report)
                        .map_err(|error| error.to_string())?;
                    file.write_all(&bytes).map_err(|error| error.to_string())?;
                    file.sync_all().map_err(|error| error.to_string())
                })()
            } else {
                reporting::export_multiroot_json(report.as_ref(), &smoke.report)
            };
            if let Err(error) = saved {
                eprintln!("Multi-root GUI smoke report failed: {error}");
                std::process::exit(1);
            }
            smoke.ready_at = Some(Instant::now());
        }
        if smoke
            .ready_at
            .is_some_and(|ready| ready.elapsed() >= Duration::from_millis(500))
            && smoke.capture_done.is_file()
        {
            context.send_viewport_cmd(egui::ViewportCommand::Close);
        }
    }
    fn export_multiroot(&mut self, context: &egui::Context) {
        if self.busy.is_some() {
            return;
        }
        let Some(report) = self.multi_result.clone() else {
            return;
        };
        let Some(path) = rfd::FileDialog::new()
            .set_title("통합 분석 JSON 저장 · 검사 폴더 밖의 새 파일")
            .set_file_name("FolderOrganizer-여러폴더-분석.json")
            .save_file()
        else {
            return;
        };
        self.start(context, "여러 폴더 보고서 저장", move |_, _| {
            reporting::export_multiroot_json(report.as_ref(), &path)?;
            Ok(ResultData::Message(format!(
                "읽기 전용 통합 보고서 저장: {}",
                path.display()
            )))
        });
    }
    fn mark_workspace_stale(&mut self, reason: &str) {
        self.workspace_stale = true;
        if let Some(session) = &self.workspace_session {
            session.invalidate();
        }
        self.batch_plan = None;
        self.groups_actionable = false;
        self.duplicates.clear();
        self.multi_rows.clear();
        self.similar = None;
        if let Some(report) = &mut self.multi_result {
            let report = Arc::make_mut(report);
            report.complete = false;
            report.inventory_complete = false;
            report.duplicates.complete = Some(false);
            report.duplicates.groups.clear();
            report.notes.push(reason.to_owned());
            for root in &mut report.roots {
                if root.state == "complete" {
                    root.state = "partial".into();
                }
            }
        }
    }
    fn analysis_available(&self) -> bool {
        if self.multi_open {
            self.multi_result.is_some()
        } else {
            self.scan.is_some()
        }
    }
    fn workspace_actionable(&self) -> bool {
        self.workspace_session
            .as_ref()
            .is_some_and(|session| session.actionable())
    }
    fn action_session(&mut self) -> Option<Arc<multiroot::WorkspaceSession>> {
        if self.busy.is_some() {
            return None;
        }
        match self.workspace_session.clone() {
            Some(session) if session.actionable() => {
                self.batch_plan = None;
                Some(session)
            }
            _ => {
                self.error = Some("현재 폴더를 먼저 분석하세요. 폴더 선택·범위를 바꾸거나 작업한 뒤에는 새 분석이 필요합니다.".into());
                None
            }
        }
    }
    fn analyze_current_folders(&mut self, context: &egui::Context) {
        let roots = self
            .multi_folders
            .iter()
            .filter(|folder| folder.selected)
            .map(|folder| folder.path.clone())
            .collect::<Vec<_>>();
        if roots.is_empty() {
            self.status = "왼쪽에서 분석할 폴더를 체크하세요.".into();
            return;
        }
        self.start_multiroot(context, roots);
    }
    fn select_visible_workspace(&mut self, checked: bool) {
        if self.multi_open {
            if let Some(report) = &self.multi_result {
                for index in &self.workspace_visible {
                    if let Some(file) = report.files.get(*index) {
                        if checked {
                            self.selected.insert(file.path.clone());
                        } else {
                            self.selected.remove(&file.path);
                        }
                    }
                }
            }
        } else if let Some(scan) = &self.scan {
            for index in &self.visible {
                if checked {
                    self.selected.insert(scan.files[*index].path.clone());
                } else {
                    self.selected.remove(&scan.files[*index].path);
                }
            }
        }
        self.selection_bytes_dirty = true;
    }
    fn select_workspace_copies(&mut self) {
        let Some(session) = self
            .workspace_session
            .as_ref()
            .filter(|session| session.actionable())
        else {
            return;
        };
        let by_path = session
            .files()
            .iter()
            .map(|file| (&file.path, file))
            .collect::<HashMap<_, _>>();
        self.selected.clear();
        for group in session.groups() {
            let candidates = group
                .paths
                .iter()
                .filter_map(|path| by_path.get(path).copied())
                .collect::<Vec<_>>();
            let protected = |path: &Path| {
                self.settings
                    .protected
                    .iter()
                    .any(|folder| path.starts_with(folder))
            };
            let protected_members = candidates
                .iter()
                .filter(|file| protected(&file.path))
                .copied()
                .collect::<Vec<_>>();
            let pool = if protected_members.is_empty() {
                &candidates
            } else {
                &protected_members
            };
            let keeper = match self.settings.retention {
                1 => pool.iter().max_by_key(|file| file.modified_ns),
                2 => pool.iter().min_by_key(|file| file.modified_ns),
                3 => pool.iter().min_by_key(|file| file.relative_path.len()),
                4 => pool
                    .iter()
                    .find(|file| {
                        self.settings
                            .preferred
                            .as_ref()
                            .is_some_and(|folder| file.path.starts_with(folder))
                    })
                    .or_else(|| pool.first()),
                _ => pool.first(),
            }
            .map(|file| &file.path);
            for file in candidates {
                if keeper != Some(&file.path) && !protected(&file.path) {
                    self.selected.insert(file.path.clone());
                }
            }
        }
        self.selection_bytes_dirty = true;
        self.view = View::Files;
        self.status = format!(
            "완전 중복 사본 {}개를 체크했습니다. 복사·이동·삭제 전에 경로를 미리 확인하세요.",
            self.selected.len()
        );
    }
    fn prepare_date_or_extension(&mut self, context: &egui::Context, date: bool) {
        let preset = rules::Rule {
            name: if date {
                "수정일별".into()
            } else {
                "확장자별".into()
            },
            target: if date {
                "{수정일:%Y}/{수정일:%m}".into()
            } else {
                "{확장자}".into()
            },
            ..Default::default()
        };
        // Quick presets must not erase the user's existing custom rules.
        if let Some(index) = self
            .settings
            .rules
            .iter()
            .position(|rule| rule.name == preset.name && rule.target == preset.target)
        {
            let rule = self.settings.rules.remove(index);
            self.settings.rules.insert(0, rule);
        } else if self.settings.rules.len() < 100 {
            self.settings.rules.insert(0, preset);
        } else {
            self.error =
                Some("규칙은 최대 100개입니다. 사용하지 않는 규칙을 먼저 정리하세요.".into());
            return;
        }
        self.rule_index = 0;
        self.rule_editor_index = None;
        self.rules_open = true;
        context.request_repaint();
    }
    fn batch_plan_dialog(&mut self, context: &egui::Context) {
        let Some(plan) = &self.batch_plan else {
            return;
        };
        let mut open = true;
        let mut execute = false;
        egui::Window::new("단일·다중 폴더 작업 미리보기").open(&mut open).default_size(egui::vec2(850.0, 560.0)).show(context, |ui| {
            ui.heading(format!("{} · {}개 파일 · {}개 원래 폴더", kind_text(plan.kind()), plan.item_count(), plan.plans().len()));
            if plan.kind() == TransferKind::Delete { ui.colored_label(ACCENT, "직접 체크한 파일을 중복으로 간주해 보관합니다. 완전 중복 자동 판정과는 별개입니다."); }
            ui.label("각 원래 폴더의 작업 기록에서 복원합니다. 같은 이름은 덮어쓰지 않으며 폴더별 결과와 중단 상태를 기록합니다.");
            egui::ScrollArea::vertical().id_salt("workspace-batch-preview").max_height(370.0).show(ui, |ui| {
                for root_plan in plan.plans() {
                    ui.strong(format!("원래 폴더: {}", root_plan.root.display()));
                    for item in &root_plan.items {
                        ui.group(|ui| { ui.label(format!("원본  {}", item.source.display())); ui.label(format!("대상  {}", item.target.display())); });
                    }
                }
            });
            ui.separator();
            ui.label("일부 폴더에서 실패하거나 취소하면 이미 완료된 항목은 유지하며, 나머지를 전부 완료한 것으로 표시하지 않습니다.");
            if ui.add_enabled(self.busy.is_none() && self.workspace_actionable() && plan.item_count() > 0, egui::Button::new(format!("위 {}개 {} 실행", plan.item_count(), kind_text(plan.kind()))).fill(ACCENT)).clicked() { execute = true; }
        });
        if execute {
            let plan = self.batch_plan.take().unwrap();
            self.start(
                context,
                "선택 파일 정리 실행",
                move |cancel, progress| {
                    workspace_actions::execute(&plan, cancel, Some(progress))
                        .map(ResultData::BatchReceipt)
                },
            );
        } else if !open {
            self.batch_plan = None;
        }
    }
    fn checked_paths(&self) -> Vec<PathBuf> {
        if self.multi_open {
            return self
                .multi_result
                .as_ref()
                .map(|report| {
                    report
                        .files
                        .iter()
                        .filter(|file| self.selected.contains(&file.path))
                        .map(|file| file.path.clone())
                        .collect()
                })
                .unwrap_or_default();
        }
        self.scan
            .as_ref()
            .map(|scan| {
                scan.files
                    .iter()
                    .filter(|file| self.selected.contains(&file.path))
                    .map(|file| file.path.clone())
                    .collect()
            })
            .unwrap_or_default()
    }
    fn require_checked(&mut self, minimum: usize) -> Option<Vec<PathBuf>> {
        let paths = self.checked_paths();
        if paths.len() < minimum {
            self.error = Some(format!(
                "파일 목록에서 체크박스로 최소 {minimum}개 파일을 선택하세요."
            ));
            None
        } else {
            Some(paths)
        }
    }
    fn analyze_duplicates(&mut self, context: &egui::Context) {
        if self.multi_open {
            if self
                .workspace_session
                .as_ref()
                .is_some_and(|session| session.actionable())
            {
                self.view = View::Duplicates;
                self.multi_show_files = false;
                self.status = format!("분석에서 검증한 완전 중복 {}그룹을 표시합니다. 추가 전체 검사는 실행하지 않았습니다.", self.duplicates.len());
            } else {
                self.error =
                    Some("현재 폴더를 먼저 분석하세요. 작업 후에는 새 분석이 필요합니다.".into());
            }
            return;
        }
        let Some(scan) = self.scan.clone() else {
            self.error = Some("먼저 폴더를 분석하세요.".into());
            return;
        };
        self.start(context, "완전 중복 검사", move |cancel, progress| {
            core::analyze_duplicates(&scan, cancel, Some(progress)).map(ResultData::Duplicates)
        });
    }
    fn analyze_similar(&mut self, context: &egui::Context) {
        if self.multi_open {
            let Some(session) = self.action_session() else {
                return;
            };
            let options = self.settings.similar.clone();
            self.start(
                context,
                "선택 폴더 전체 유사 후보 비교",
                move |cancel, progress| {
                    workspace_actions::analyze_similar(&session, &options, cancel, Some(progress))
                        .map(ResultData::Similar)
                },
            );
            return;
        }
        let Some(scan) = self.scan.clone() else {
            self.error = Some("먼저 폴더를 분석하세요.".into());
            return;
        };
        let options = self.settings.similar.clone();
        let groups = self.groups_actionable.then(|| self.duplicates.clone());
        let duplicate_warnings = self
            .duplicate_details
            .as_ref()
            .filter(|value| !value.complete)
            .map(|value| value.warnings.clone())
            .unwrap_or_default();
        self.start(context, "유사 후보 비교", move |cancel, progress| {
            let mut report = if let Some(groups) = groups {
                similarity::find_similar_excluding(&scan, &options, &groups, cancel, Some(progress))
            } else {
                similarity::find_similar(&scan, &options, cancel, Some(progress))
            }?;
            if !duplicate_warnings.is_empty() {
                report.complete = false;
                report
                    .warnings
                    .extend(duplicate_warnings.iter().take(20).map(|value| {
                        format!(
                            "중복 제외 검사 일부 미완료: {}: {}",
                            value.path.display(),
                            value.message
                        )
                    }));
            }
            Ok(ResultData::Similar(report))
        });
    }
    fn keep_first(&mut self, context: &egui::Context) {
        if self.multi_open {
            let Some(session) = self.action_session() else {
                return;
            };
            if session.groups().is_empty() {
                self.status = "정리할 완전 중복 그룹이 없습니다.".into();
                return;
            }
            let protected = self.settings.protected.clone();
            self.start(
                context,
                "첫 파일을 남기고 나머지 삭제 보관",
                move |cancel, progress| {
                    workspace_actions::keep_first(&session, &protected, cancel, Some(progress))
                        .map(ResultData::BatchReceipt)
                },
            );
            return;
        }
        if !self.groups_actionable || self.duplicates.is_empty() {
            return;
        }
        let Some(scan) = self.scan.clone() else {
            return;
        };
        let groups = self.duplicates.clone();
        let protected = self.settings.protected.clone();
        self.start(
            context,
            "첫 파일을 남기고 나머지 삭제 보관",
            move |cancel, progress| {
                operations::keep_first(&scan, &groups, &protected, cancel, Some(progress))
                    .map(|receipt| ResultData::Receipt(receipt, false))
            },
        );
    }
    fn prepare_transfer(&mut self, context: &egui::Context, kind: TransferKind) {
        let Some(paths) = self.require_checked(1) else {
            return;
        };
        if self.multi_open {
            let Some(session) = self.action_session() else {
                return;
            };
            let target = if kind == TransferKind::Delete {
                None
            } else {
                rfd::FileDialog::new()
                    .set_title("대상 폴더 선택 · 같은 이름은 따로 보관")
                    .pick_folder()
            };
            if kind != TransferKind::Delete && target.is_none() {
                return;
            }
            self.start(
                context,
                "선택 파일 작업 미리보기 준비",
                move |_, _| {
                    workspace_actions::plan_transfer(&session, &paths, target.as_deref(), kind)
                        .map(ResultData::BatchPlan)
                },
            );
            return;
        }
        let Some(scan) = self.scan.clone() else {
            return;
        };
        let target = if kind == TransferKind::Delete {
            None
        } else {
            rfd::FileDialog::new()
                .set_title("대상 폴더 선택")
                .pick_folder()
        };
        if kind != TransferKind::Delete && target.is_none() {
            return;
        }
        self.start(context, "작업 미리보기 준비", move |_, _| {
            operations::plan_files(&scan, &paths, target.as_deref(), kind).map(ResultData::Plan)
        });
    }
    fn load_history(&mut self, context: &egui::Context) {
        let root = if self.multi_open {
            self.workspace_root
                .and_then(|index| self.multi_folders.get(index))
                .map(|folder| folder.path.clone())
        } else {
            self.root.clone()
        };
        if let Some(root) = root {
            self.root = Some(root.clone());
            self.history_selected = None;
            self.start(context, "작업 기록 읽기", move |_, _| {
                operations::history(&root).map(ResultData::History)
            });
        } else {
            self.error = Some("먼저 기록이 있는 폴더를 선택하세요.".into());
        }
    }
    fn load_logs(&mut self, context: &egui::Context) {
        let root = if self.multi_open {
            self.workspace_root
                .and_then(|index| self.multi_folders.get(index))
                .map(|folder| folder.path.clone())
        } else {
            self.root.clone()
        };
        if let Some(root) = root {
            self.logs_root = Some(root.clone());
            self.start(context, "실행 로그 읽기", move |_, _| {
                operations::logs(&root).map(ResultData::Logs)
            });
        } else {
            self.error = Some("먼저 기록이 있는 폴더를 선택하세요.".into());
        }
    }
    fn select_retention(&mut self) {
        if self.multi_open {
            self.select_workspace_copies();
            return;
        }
        let Some(scan) = &self.scan else {
            return;
        };
        self.selected.clear();
        for group in &self.duplicates {
            let candidates: Vec<_> = group
                .paths
                .iter()
                .filter_map(|path| self.index.get(path).map(|index| &scan.files[*index]))
                .collect();
            let keeper = match self.settings.retention {
                1 => candidates.iter().max_by_key(|entry| entry.modified_ns),
                2 => candidates.iter().min_by_key(|entry| entry.modified_ns),
                3 => candidates
                    .iter()
                    .min_by_key(|entry| entry.relative_path.len()),
                4 => candidates
                    .iter()
                    .find(|entry| {
                        self.settings
                            .preferred
                            .as_ref()
                            .is_some_and(|folder| entry.path.starts_with(folder))
                    })
                    .or_else(|| candidates.first()),
                _ => candidates.first(),
            }
            .map(|entry| &entry.path);
            for entry in candidates {
                if keeper != Some(&entry.path)
                    && !self
                        .settings
                        .protected
                        .iter()
                        .any(|folder| entry.path.starts_with(folder))
                {
                    self.selected.insert(entry.path.clone());
                }
            }
        }
        self.selection_bytes_dirty = true;
        self.status = format!(
            "보존 규칙으로 {}개 사본을 선택했습니다. 미리보기에서 확인하세요.",
            self.selected.len()
        );
        self.view = View::Files;
    }
    fn compare_paths(&mut self, context: &egui::Context, paths: Vec<PathBuf>) {
        if paths.len() != 2 {
            self.error = Some("나란히 비교하려면 파일 두 개를 선택하세요.".into());
            return;
        }
        self.start(
            context,
            "두 파일 비교 자료 준비",
            move |cancel, progress| {
                paths
                    .iter()
                    .map(|path| compare_file(path, cancel, progress))
                    .collect::<AppResult<Vec<_>>>()
                    .map(ResultData::Compare)
            },
        );
    }
}

impl OrganizerApp {
    fn export(&mut self, context: &egui::Context, format: &str) {
        if self.multi_open {
            self.export_multiroot(context);
            return;
        }
        let Some(scan) = self.scan.clone() else {
            return;
        };
        let format = format.to_owned();
        let Some(path) = rfd::FileDialog::new()
            .set_title("새 분석 보고서 저장 · 기존 파일 덮어쓰기 금지")
            .set_file_name(format!("FolderOrganizer-분석.{format}"))
            .save_file()
        else {
            return;
        };
        let duplicates = self.duplicates.clone();
        let options = self.scan_options.clone();
        let analysis = self
            .duplicate_details
            .clone()
            .filter(|_| self.groups_actionable)
            .map(|mut value| {
                value.groups = duplicates;
                value
            });
        let similar = self.similar.clone();
        self.start(context, "분석 보고서 저장", move |_, _| {
            match format.as_str() {
                "csv" => reporting::export_csv(&scan, &path)?,
                "html" => reporting::export_analysis_html(
                    &scan,
                    &options,
                    analysis.as_ref(),
                    &similar,
                    &path,
                )?,
                _ => reporting::export_analysis_json(
                    &scan,
                    &options,
                    analysis.as_ref(),
                    &similar,
                    &path,
                )?,
            }
            Ok(ResultData::Message(format!(
                "보고서 저장 완료: {}",
                path.display()
            )))
        });
    }
    fn show_overview(&mut self, context: &egui::Context) {
        let files = if self.multi_open {
            let Some(session) = &self.workspace_session else {
                return;
            };
            session.files().to_vec()
        } else {
            let Some(scan) = self.scan.clone() else {
                return;
            };
            scan.files.clone()
        };
        self.start(context, "용량·정리 요약", move |_, _| {
            let mut kinds: BTreeMap<String, (u64, usize)> = BTreeMap::new();
            let mut folders: BTreeMap<String, (u64, usize)> = BTreeMap::new();
            let mut largest = std::collections::BinaryHeap::new();
            let mut result = Overview::default();
            let year_ago = time_now().saturating_sub(365 * 24 * 3600) as i128 * 1_000_000_000;
            for file in &files {
                result.bytes = result.bytes.saturating_add(file.size);
                result.files += 1;
                let kind = rules::file_type(file).to_owned();
                let entry = kinds.entry(kind).or_default();
                entry.0 = entry.0.saturating_add(file.size);
                entry.1 += 1;
                let first = Path::new(&file.relative_path)
                    .components()
                    .next()
                    .map(|part| part.as_os_str().to_string_lossy().into_owned())
                    .unwrap_or_default();
                let folder = if Path::new(&file.relative_path)
                    .parent()
                    .is_some_and(|parent| !parent.as_os_str().is_empty())
                {
                    first
                } else {
                    "선택 폴더 바로 안".into()
                };
                let entry = folders.entry(folder).or_default();
                entry.0 = entry.0.saturating_add(file.size);
                entry.1 += 1;
                largest.push(std::cmp::Reverse((file.size, file.path.clone())));
                if largest.len() > 20 {
                    largest.pop();
                }
                if file.size == 0 && result.empty.len() < 1000 {
                    result.empty.push(file.path.clone());
                }
                if file.modified_ns < year_ago {
                    result.old += 1;
                }
            }
            result.kinds = kinds
                .into_iter()
                .map(|(name, (size, count))| (name, size, count))
                .collect();
            result.folders = folders
                .into_iter()
                .map(|(name, (size, count))| (name, size, count))
                .collect();
            result.kinds.sort_unstable_by(|a, b| b.1.cmp(&a.1));
            result.folders.sort_unstable_by(|a, b| b.1.cmp(&a.1));
            result.largest = largest
                .into_sorted_vec()
                .into_iter()
                .map(|value| (value.0 .1, value.0 .0))
                .collect();
            result
                .largest
                .sort_unstable_by_key(|(_, size)| std::cmp::Reverse(*size));
            Ok(ResultData::Overview(result))
        });
    }
    fn prepare_merge(&mut self, context: &egui::Context) {
        let Some(paths) = self.require_checked(2) else {
            return;
        };
        let first = &paths[0];
        let name = format!(
            "{}_병합.{}",
            first.file_stem().unwrap_or_default().to_string_lossy(),
            first.extension().unwrap_or_default().to_string_lossy()
        );
        let Some(output) = rfd::FileDialog::new()
            .set_title("새 병합본 저장 위치 · 기존 파일 덮어쓰기 금지")
            .set_file_name(name)
            .save_file()
        else {
            return;
        };
        self.archive_merge = false;
        self.start(
            context,
            "겹치는 내용 병합 미리보기",
            move |_, _| merging::preview_merge(&paths, &output).map(ResultData::MergePreview),
        );
    }
    fn check_health(&mut self, context: &egui::Context) {
        let Some(paths) = self.require_checked(1) else {
            return;
        };
        if paths.len() > 1000 {
            self.error = Some(
                "건강 검사는 한 번에 최대 1,000개입니다. 필요한 파일을 좁혀 선택하세요.".into(),
            );
            return;
        }
        self.start(
            context,
            "선택 파일 건강 검사",
            move |cancel, progress| {
                let mut results = vec![];
                let total = paths.len() as u64;
                for path in paths {
                    if cancel.load(Ordering::Relaxed) {
                        break;
                    }
                    let result = recovery::check_health(&path);
                    results.push((path.clone(), result));
                    progress(Progress {
                        phase: "health".into(),
                        completed: results.len() as u64,
                        total: Some(total),
                        message: path.display().to_string(),
                    });
                }
                Ok(ResultData::Health(results))
            },
        );
    }
    fn repair(&mut self, context: &egui::Context) {
        let Some(paths) = self.require_checked(1) else {
            return;
        };
        if paths.len() != 1 {
            self.error = Some("새 복구본은 한 파일씩 만듭니다. 파일 하나를 체크하세요.".into());
            return;
        }
        let source = paths[0].clone();
        let extension = source
            .extension()
            .unwrap_or_default()
            .to_string_lossy()
            .to_lowercase();
        let image = ["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp"]
            .contains(&extension.as_str());
        let name = format!(
            "{}_복구.{}",
            source.file_stem().unwrap_or_default().to_string_lossy(),
            if image { "png" } else { &extension }
        );
        let Some(output) = rfd::FileDialog::new()
            .set_title("원본을 유지하고 새 복구본 만들기")
            .set_file_name(name)
            .save_file()
        else {
            return;
        };
        self.start(context, "새 복구본 만들기", move |_, _| {
            recovery::repair_to_new(&source, &output).map(ResultData::Repair)
        });
    }
    fn check_update(&mut self, context: &egui::Context, manual: bool) {
        self.next_update_check = Instant::now() + Duration::from_secs(6 * 60 * 60);
        if !updater::is_auto_update_supported() {
            if manual {
                self.error = Some("자동 업데이트는 완전한 Windows Rust ZIP 배포판에서 지원합니다. Python 배포판과 설치 폴더를 공유하지 마세요.".into());
            }
            return;
        }
        self.start(context, "Rust 정식 업데이트 확인", move |_, _| {
            updater::check_for_update(VERSION).map(ResultData::Update)
        });
    }
    fn prepare_update(&mut self, context: &egui::Context, release: updater::ReleaseInfo) {
        self.start(
            context,
            "Rust 업데이트 다운로드·검증",
            move |cancel, progress| {
                let install = updater::install_directory()?;
                updater::prepare_update(&release, &install, cancel, Some(progress))
                    .map(ResultData::Prepared)
            },
        );
    }
    fn header(&mut self, context: &egui::Context) {
        egui::TopBottomPanel::top("header").show(context, |ui| {
            ui.add_space(3.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new("Folder Organizer").strong().size(17.0));
                ui.label(
                    RichText::new(format!("Rust · {VERSION}"))
                        .small()
                        .color(ACCENT),
                );
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui
                        .small_button(if self.settings.dark {
                            "라이트 모드"
                        } else {
                            "다크 모드"
                        })
                        .clicked()
                    {
                        self.settings.dark = !self.settings.dark;
                        apply_theme(context, self.settings.dark);
                        save_settings(&self.settings);
                    }
                    ui.label(
                        RichText::new("내 PC의 파일을 한 화면에서 분석·검사·정리")
                            .small()
                            .weak(),
                    );
                });
            });
            ui.horizontal(|ui| {
                ui.menu_button("파일", |ui| {
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("폴더 하나 열기…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.pick_folder(context);
                    }
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("폴더 추가…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.add_workspace_folders();
                    }
                    ui.menu_button("최근 폴더", |ui| {
                        for path in self.settings.recent.clone() {
                            if ui
                                .add_enabled(
                                    self.busy.is_none(),
                                    egui::Button::new(path.display().to_string()),
                                )
                                .clicked()
                            {
                                ui.close_menu();
                                self.start_scan(context, path);
                            }
                        }
                    });
                    ui.separator();
                    if ui
                        .add_enabled(
                            self.multi_result.is_some() && self.busy.is_none(),
                            egui::Button::new("분석 보고서 저장…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.export(context, "json");
                    }
                    if ui.button("종료").clicked() {
                        self.quit_requested = true;
                        ui.close_menu();
                    }
                });
                for (index, title) in ["홈", "검사", "정리", "보기"].iter().enumerate() {
                    if ui
                        .selectable_label(self.ribbon_tab == index, *title)
                        .clicked()
                    {
                        self.ribbon_tab = index;
                    }
                }
                ui.menu_button("기록·도구", |ui| {
                    let idle = self.busy.is_none();
                    if ui
                        .add_enabled(
                            idle && self.workspace_root.is_some(),
                            egui::Button::new("선택 폴더 작업 기록·복원…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.load_history(context);
                    }
                    if ui
                        .add_enabled(
                            idle && self.workspace_root.is_some(),
                            egui::Button::new("선택 폴더 실행 로그…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.load_logs(context);
                    }
                    if ui.button("앱 활동·진단 로그…").clicked() {
                        self.activities_open = true;
                        ui.close_menu();
                    }
                    ui.separator();
                    if ui
                        .add_enabled(
                            idle && self.analysis_available(),
                            egui::Button::new("용량·정리 요약…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.show_overview(context);
                    }
                    if ui
                        .add_enabled(
                            idle && self.workspace_actionable() && self.selected.len() >= 2,
                            egui::Button::new("선택 파일의 겹치는 내용 병합…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.prepare_merge(context);
                    }
                    if ui
                        .add_enabled(
                            idle && !self.selected.is_empty(),
                            egui::Button::new("체크한 파일 건강 검사…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.check_health(context);
                    }
                    if ui
                        .add_enabled(
                            idle && self.selected.len() == 1,
                            egui::Button::new("원본 유지·새 복구본 만들기…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.repair(context);
                    }
                    if ui
                        .add_enabled(idle, egui::Button::new("업데이트 확인…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.check_update(context, true);
                    }
                    if ui
                        .checkbox(&mut self.settings.automatic_updates, "자동 업데이트 확인")
                        .changed()
                    {
                        save_settings(&self.settings);
                    }
                });
                ui.menu_button("도움말", |ui| {
                    if ui.button("기능·사용 순서 · F1").clicked() {
                        self.help_open = true;
                        ui.close_menu();
                    }
                    ui.label("파일 체크 → 검사 또는 정리 → 경로 확인 → 실행");
                    ui.label("완전 중복은 검증된 같은 내용, 유사는 직접 검토할 후보입니다.");
                });
            });
            ui.separator();
            self.ribbon(context, ui);
        });
    }
    fn ribbon(&mut self, context: &egui::Context, ui: &mut egui::Ui) {
        let idle = self.busy.is_none();
        let analyzed = self.analysis_available();
        let actionable = self.workspace_actionable();
        let selected = !self.selected.is_empty();
        egui::ScrollArea::horizontal().id_salt("main-action-ribbon").show(ui, |ui| {
            ui.horizontal(|ui| {
                match self.ribbon_tab {
                    0 => {
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle, egui::Button::new("폴더 추가…")).clicked() { self.add_workspace_folders(); }
                                if ui.add_enabled(idle && self.multi_folders.iter().any(|folder| folder.selected), egui::Button::new("분석 시작").fill(ACCENT)).clicked() { self.analyze_current_folders(context); }
                                if ui.add_enabled(!idle, egui::Button::new("중지")).clicked() { if let Some(busy) = &self.busy { busy.cancel.store(true, Ordering::Relaxed); } }
                            });
                            ui.label(RichText::new("검색·분석").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && actionable, egui::Button::new("완전 중복")).clicked() { self.analyze_duplicates(context); }
                                if ui.add_enabled(idle && actionable, egui::Button::new("유사 비교")).clicked() { self.analyze_similar(context); }
                            });
                            ui.label(RichText::new("검사").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            ui.menu_button("체크 선택 ▾", |ui| {
                                if ui.add_enabled(idle && analyzed, egui::Button::new("보이는 파일 모두 체크")).clicked() { self.select_visible_workspace(true); ui.close_menu(); }
                                if ui.button("전체 체크 해제").clicked() { self.selected.clear(); self.selection_bytes_dirty = true; ui.close_menu(); }
                                if ui.add_enabled(idle && actionable, egui::Button::new("보존 규칙으로 중복 사본 체크")).clicked() { self.select_retention(); ui.close_menu(); }
                            });
                            ui.label(RichText::new("작업 대상 표시").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("복사…")).clicked() { self.prepare_transfer(context, TransferKind::Copy); }
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("이동…")).clicked() { self.prepare_transfer(context, TransferKind::Move); }
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("삭제 보관…")).clicked() { self.prepare_transfer(context, TransferKind::Delete); }
                            });
                            ui.label(RichText::new("체크한 파일 정리").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            if ui.add_enabled(idle && actionable && !self.duplicates.is_empty(), egui::Button::new("첫 파일만 남기고 나머지 삭제").fill(ACCENT)).on_hover_text("모든 완전 중복 그룹에서 첫 파일과 보호 파일을 보존하고, 나머지를 원래 폴더의 delete_files로 이동합니다. 추가 전체 검사·확인 창 없이 실행합니다.").clicked() { self.keep_first(context); }
                            ui.label(RichText::new("완전 중복 · 현재 검증 결과 재사용").small().weak());
                        });
                    }
                    1 => {
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && actionable, egui::Button::new("완전 중복 표시")).clicked() { self.analyze_duplicates(context); }
                                if ui.add_enabled(idle && actionable, egui::Button::new("유사 파일 비교")).clicked() { self.analyze_similar(context); }
                                if ui.add_enabled(idle && self.selected.len() == 2, egui::Button::new("선택한 두 파일 나란히 비교")).clicked() { self.compare_paths(context, self.checked_paths()); }
                            });
                            ui.label(RichText::new("분석한 모든 폴더를 함께 검사 · 유사는 삭제 근거가 아닙니다").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            if ui.add_enabled(idle && actionable && !self.duplicates.is_empty(), egui::Button::new("첫 파일만 남기고 나머지 삭제").fill(ACCENT)).on_hover_text("각 완전 중복 그룹의 첫 파일·보호 파일을 남기고 나머지를 원래 폴더의 delete_files로 이동합니다. 추가 전체 검사·확인 창 없이 실행합니다.").clicked() { self.keep_first(context); }
                            ui.label(RichText::new("완전 중복 전체 그룹 · 현재 검증 결과 재사용").small().weak());
                        });
                    }
                    2 => {
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("체크 파일 복사…")).clicked() { self.prepare_transfer(context, TransferKind::Copy); }
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("체크 파일 이동…")).clicked() { self.prepare_transfer(context, TransferKind::Move); }
                                if ui.add_enabled(idle && actionable && selected, egui::Button::new("체크 파일 삭제 보관…")).clicked() { self.prepare_transfer(context, TransferKind::Delete); }
                            });
                            ui.label(RichText::new("실행 전 원본·대상 경로 미리보기").small().weak());
                        });
                        ui.separator();
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && actionable, egui::Button::new("규칙 분류…")).clicked() { self.rules_open = true; }
                                if ui.add_enabled(idle && actionable, egui::Button::new("날짜별…")).clicked() { self.prepare_date_or_extension(context, true); }
                                if ui.add_enabled(idle && actionable, egui::Button::new("확장자별…")).clicked() { self.prepare_date_or_extension(context, false); }
                            });
                            ui.label(RichText::new("조건·{규칙} 매핑 · 복사 기본값").small().weak());
                        });
                    }
                    _ => {
                        ui.vertical(|ui| {
                            ui.horizontal(|ui| {
                                if ui.add_enabled(idle && analyzed, egui::Button::new("보고서 저장…")).clicked() { self.export_multiroot(context); }
                                if ui.button(if self.settings.dark { "라이트 모드" } else { "다크 모드" }).clicked() { self.settings.dark = !self.settings.dark; apply_theme(context, self.settings.dark); save_settings(&self.settings); }
                                if ui.add_enabled(idle, egui::Button::new("검사·보존 설정…")).clicked() { self.settings_open = true; }
                            });
                            ui.label(RichText::new("결과·표시·설정").small().weak());
                        });
                    }
                }
                if self.ribbon_tab < 2 {
                    ui.separator();
                    ui.vertical(|ui| {
                        if ui.add_enabled(idle && actionable, egui::Button::new("규칙 분류…")).clicked() { self.rules_open = true; }
                        ui.label(RichText::new("정리").small().weak());
                    });
                }
            });
        });
    }
    fn footer(&mut self, context: &egui::Context) {
        egui::TopBottomPanel::bottom("status").show(context, |ui| {
            ui.add_space(5.0);
            if let Some(busy) = &self.busy {
                ui.horizontal(|ui| {
                    ui.spinner();
                    ui.label(RichText::new(&busy.name).strong());
                    ui.label(format!(
                        "경과 {:.0}초",
                        busy.started.elapsed().as_secs_f32()
                    ));
                    if ui.button("취소 요청").clicked() {
                        busy.cancel.store(true, Ordering::Relaxed);
                    }
                });
                if let Some(progress) = &self.progress {
                    if let Some(total) = progress.total.filter(|total| *total > 0) {
                        let fraction = (progress.completed as f32 / total as f32).clamp(0.0, 1.0);
                        let remaining = if fraction > 0.01 {
                            busy.started.elapsed().as_secs_f32() * (1.0 - fraction) / fraction
                        } else {
                            0.0
                        };
                        ui.add(egui::ProgressBar::new(fraction).text(format!(
                            "{} / {} · {:.0}%{}",
                            progress.completed,
                            total,
                            fraction * 100.0,
                            if remaining > 0.0 {
                                format!(" · 약 {remaining:.0}초 남음")
                            } else {
                                String::new()
                            }
                        )));
                    } else {
                        ui.label(format!(
                            "발견·처리 {}개 · 전체 수 확인 중",
                            progress.completed
                        ));
                    }
                    ui.label(egui::RichText::new(&progress.message).small());
                }
            } else {
                ui.label(&self.status);
            }
            if self.prepared.is_some() {
                ui.horizontal(|ui| {
                    ui.label(RichText::new("업데이트 준비됨 · 앱 종료 후 적용").color(ACCENT));
                    if ui.button("지금 종료하고 적용").clicked() {
                        self.quit_requested = true;
                    }
                });
            }
            ui.add_space(5.0);
        });
    }
    fn workspace(&mut self, context: &egui::Context) {
        if self.multi_open {
            self.multiroot_workspace(context);
            return;
        }
        egui::CentralPanel::default().show(context, |ui| {
            ui.add_space(10.0);
            ui.horizontal(|ui| {
                ui.vertical(|ui| {
                    ui.heading(if self.scan.is_some() { "내 폴더 살펴보기" } else { "폴더 정리를 시작하세요" });
                    if let Some(path) = &self.root { ui.label(path.display().to_string()); } else { ui.label("크기와 날짜를 확인하고, 중복과 유사 파일을 구분해 정리하세요."); }
                });
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if ui.add_enabled(self.busy.is_none(), egui::Button::new(if self.root.is_some() { "폴더 변경…" } else { "폴더 선택…" }).fill(ACCENT).min_size(egui::vec2(110.0, 34.0))).clicked() { self.pick_folder(context); }
                });
            });
            if ui.add_enabled(self.busy.is_none(), egui::Button::new("← 여러 폴더 통합 분석으로")).clicked() { self.enter_workspace(); }
            ui.add_space(14.0);
            if self.scan.is_none() {
                ui.add_space(45.0); ui.vertical_centered(|ui| {
                    ui.label(RichText::new("1  폴더 선택     2  파일 확인     3  검사·정리").size(22.0));
                    ui.add_space(14.0); ui.label("파일 변경은 선택한 작업으로만 실행합니다. 복사본이 있는 작은 폴더부터 시험해 보세요.");
                    ui.label("완전 중복: 모든 바이트가 같음 · 유사 후보: 직접 내용을 검토할 대상");
                    ui.add_space(18.0); ui.hyperlink_to("기능·지원 범위 안내", "https://github.com/P-JSL/Folder_Organizer");
                }); return;
            }
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.view, View::Files, format!("파일 {}", self.scan.as_ref().map(|scan| scan.files.len()).unwrap_or(0)));
                ui.selectable_value(&mut self.view, View::Duplicates, format!("완전 중복 {}그룹", self.duplicates.len()));
                ui.selectable_value(&mut self.view, View::Similar, format!("유사 후보 {}쌍", self.similar.as_ref().map(|report| report.pairs.len()).unwrap_or(0)));
            }); ui.separator();
            match self.view { View::Files => self.files_view(ui), View::Duplicates => self.duplicates_view(ui, context), View::Similar => self.similar_view(ui, context) }
        });
    }
    fn files_view(&mut self, ui: &mut egui::Ui) {
        let mut changed = false;
        ui.horizontal(|ui| {
            ui.label("찾기");
            changed |= ui
                .add(
                    egui::TextEdit::singleline(&mut self.query)
                        .hint_text("파일 이름 또는 상대 경로")
                        .desired_width(280.0),
                )
                .changed();
            egui::ComboBox::from_id_salt("quick-filter")
                .selected_text(
                    [
                        "전체 파일",
                        "100 MiB 이상",
                        "빈 파일 · 0 B",
                        "수정 후 1년",
                        "응용 프로그램",
                    ][self.quick_filter],
                )
                .show_ui(ui, |ui| {
                    for (index, label) in [
                        "전체 파일",
                        "100 MiB 이상",
                        "빈 파일 · 0 B",
                        "수정 후 1년",
                        "응용 프로그램",
                    ]
                    .iter()
                    .enumerate()
                    {
                        changed |= ui
                            .selectable_value(&mut self.quick_filter, index, *label)
                            .changed();
                    }
                });
            egui::ComboBox::from_id_salt("sort")
                .selected_text(match self.sort {
                    Sort::Name => "이름순",
                    Sort::Size => "크기순",
                    Sort::Modified => "수정일순",
                    Sort::Created => "생성일순",
                })
                .show_ui(ui, |ui| {
                    for (value, label) in [
                        (Sort::Name, "이름순"),
                        (Sort::Size, "크기순"),
                        (Sort::Modified, "수정일순"),
                        (Sort::Created, "생성일순"),
                    ] {
                        changed |= ui.selectable_value(&mut self.sort, value, label).changed();
                    }
                });
            changed |= ui.checkbox(&mut self.descending, "내림차순").changed();
        });
        if changed {
            self.filter_rows();
        }
        let Some(scan) = &self.scan else {
            return;
        };
        if self.selection_bytes_dirty {
            self.selected_bytes = self
                .selected
                .iter()
                .filter_map(|path| self.index.get(path))
                .fold(0u64, |total, index| {
                    total.saturating_add(scan.files[*index].size)
                });
            self.selection_bytes_dirty = false;
        }
        ui.label(
            RichText::new(format!(
                "표시 {}개 / 전체 {}개 · 체크 {}개 · {} · 화면 필터는 검사 범위를 바꾸지 않습니다",
                self.visible.len(),
                scan.files.len(),
                self.selected.len(),
                bytes_text(self.selected_bytes)
            ))
            .small(),
        );
        let row_height = 28.0;
        TableBuilder::new(ui)
            .striped(true)
            .resizable(true)
            .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
            .column(Column::exact(30.0))
            .column(Column::remainder().at_least(200.0))
            .column(Column::initial(100.0))
            .column(Column::initial(150.0))
            .column(Column::initial(150.0))
            .column(Column::initial(85.0))
            .header(30.0, |mut header| {
                for title in [
                    "선택",
                    "파일 · 상대 경로",
                    "용량",
                    "수정일",
                    "생성일",
                    "구분",
                ] {
                    header.col(|ui| {
                        ui.strong(title);
                    });
                }
            })
            .body(|body| {
                body.rows(row_height, self.visible.len(), |mut row| {
                    let index = self.visible[row.index()];
                    let file = &scan.files[index];
                    let cache = &self.rows[index];
                    row.col(|ui| {
                        let mut checked = self.selected.contains(&file.path);
                        if ui.checkbox(&mut checked, "").changed() {
                            self.selection_bytes_dirty = true;
                            if checked {
                                self.selected.insert(file.path.clone());
                            } else {
                                self.selected.remove(&file.path);
                            }
                        }
                    });
                    row.col(|ui| {
                        ui.label(&file.relative_path)
                            .on_hover_text(file.path.display().to_string());
                    });
                    row.col(|ui| {
                        ui.label(&cache.size)
                            .on_hover_text(format!("{} 바이트 · 논리적 파일 크기", file.size));
                    });
                    row.col(|ui| {
                        ui.label(&cache.modified);
                    });
                    row.col(|ui| {
                        ui.label(&cache.created);
                    });
                    row.col(|ui| {
                        ui.label(if file.kind == "application" {
                            "프로그램"
                        } else {
                            rules::file_type(file)
                        });
                    });
                })
            });
    }
    fn duplicates_view(&mut self, ui: &mut egui::Ui, context: &egui::Context) {
        if self.duplicates.is_empty() {
            ui.add_space(20.0);
            ui.label("검사 → 완전 중복 검사로 같은 내용을 찾으세요. 크기·전체 해시·실제 바이트를 검증합니다.");
            return;
        }
        if self.groups_actionable {
            ui.group(|ui| {
                ui.label(RichText::new("각 그룹의 첫 파일은 남기고 나머지를 한 번에 정리").strong());
                ui.label("첫 파일과 보호 파일은 유지합니다. 나머지는 delete_files로 이동하며 작업 기록에서 복원할 수 있습니다.");
                ui.label(RichText::new("이 버튼은 추가 확인·전체 재검사 없이 바로 실행합니다.").small());
                if ui.add_enabled(self.busy.is_none(), egui::Button::new("첫 번째 파일만 남기고 나머지 삭제").fill(ACCENT)).clicked() { self.keep_first(context); }
            });
        } else {
            ui.label(RichText::new("정리 결과 · 처리한 파일은 목록에서 제외했습니다. 새로운 정리는 명시적으로 다시 검사하세요.").color(ACCENT));
        }
        let mut compare = None;
        egui::ScrollArea::vertical().show_rows(ui, 34.0, self.duplicate_rows.len(), |ui, range| {
            for row_index in range {
                match self.duplicate_rows[row_index] {
                    DuplicateRow::Header(group_index, count) => {
                        let group = &self.duplicates[group_index];
                        ui.horizontal(|ui| {
                            ui.strong(format!(
                                "그룹 {} · 현재 파일 {}개 · 파일당 {}",
                                group_index + 1,
                                count,
                                bytes_text(group.size)
                            ))
                            .on_hover_text(format!(
                                "SHA-256 {} · 전체 해시와 실제 바이트 일치",
                                group.sha256
                            ));
                            if ui
                                .add_enabled(
                                    self.busy.is_none() && self.groups_actionable && count >= 2,
                                    egui::Button::new("첫 두 파일 나란히 비교"),
                                )
                                .clicked()
                            {
                                compare = Some(group.paths[..2].to_vec());
                            }
                        });
                    }
                    DuplicateRow::File(group_index, path_index) => {
                        let path = &self.duplicates[group_index].paths[path_index];
                        ui.horizontal(|ui| {
                            ui.add_space(14.0);
                            let mut checked = self.selected.contains(path);
                            if ui.checkbox(&mut checked, "").changed() {
                                self.selection_bytes_dirty = true;
                                if checked {
                                    self.selected.insert(path.clone());
                                } else {
                                    self.selected.remove(path);
                                }
                            }
                            if path_index == 0 {
                                ui.label(RichText::new("첫 파일 · 보존").color(ACCENT));
                            }
                            if self
                                .settings
                                .protected
                                .iter()
                                .any(|folder| path.starts_with(folder))
                            {
                                ui.label("보호");
                            }
                            ui.label(path.display().to_string());
                        });
                    }
                }
            }
        });
        if let Some(paths) = compare {
            self.compare_paths(context, paths);
        }
    }
    fn similar_view(&mut self, ui: &mut egui::Ui, context: &egui::Context) {
        let Some(report) = &self.similar else {
            ui.label("검사 → 유사 파일 비교에서 이름·텍스트·이미지 근거를 살펴보세요.");
            return;
        };
        ui.label(
            RichText::new("유사도는 삭제 근거가 아닙니다. 직접 비교하고 정리할 파일을 체크하세요.")
                .color(ACCENT),
        );
        ui.label(format!(
            "비교 {}쌍 / 가능한 {}쌍 · {} · 경고 {}개",
            report.compared_pairs,
            report.possible_pairs,
            if report.complete {
                "설정 범위 검사 완료"
            } else {
                "빠른 후보 또는 비교 한도 · 미검사 조합 있음"
            },
            report.warnings.len()
        ));
        let mut compare = None;
        egui::ScrollArea::vertical().show_rows(ui, 96.0, report.pairs.len(), |ui, range| {
            for index in range {
                let pair = &report.pairs[index];
                ui.group(|ui| {
                    ui.horizontal(|ui| {
                        ui.strong(format!("후보 {}", index + 1));
                        ui.label(format!(
                            "이름 {:.0}% · 텍스트 {} · 이미지 {}",
                            pair.name_score * 100.0,
                            score_text(pair.text_score),
                            score_text(pair.image_score)
                        ));
                        if ui
                            .add_enabled(self.busy.is_none(), egui::Button::new("나란히 비교"))
                            .clicked()
                        {
                            compare = Some(vec![pair.left.clone(), pair.right.clone()]);
                        }
                    });
                    for path in [&pair.left, &pair.right] {
                        ui.horizontal(|ui| {
                            let mut checked = self.selected.contains(path);
                            if ui.checkbox(&mut checked, "").changed() {
                                self.selection_bytes_dirty = true;
                                if checked {
                                    self.selected.insert(path.clone());
                                } else {
                                    self.selected.remove(path);
                                }
                            }
                            ui.label(path.display().to_string());
                        });
                    }
                    ui.label(RichText::new(&pair.reason).small());
                });
            }
        });
        if let Some(paths) = compare {
            self.compare_paths(context, paths);
        }
    }
}

impl OrganizerApp {
    fn settings_dialog(&mut self, context: &egui::Context) {
        let mut open = self.settings_open;
        if self.busy.is_some() {
            egui::Window::new("검사 범위 · 보존 설정")
                .open(&mut open)
                .show(context, |ui| {
                    ui.label("진행 중인 작업이 끝난 뒤 설정을 바꿀 수 있습니다.");
                });
            self.settings_open = open;
            return;
        }
        let previous_scope = serde_json::to_string(&self.settings.scan).unwrap_or_default();
        let mut changed = false;
        egui::Window::new("검사 범위 · 보존 설정").open(&mut open).default_width(650.0).show(context, |ui| {
            egui::ScrollArea::vertical().max_height(650.0).show(ui, |ui| {
                ui.heading("다음 분석에 적용할 범위");
                changed |= ui.checkbox(&mut self.settings.scan.recursive, "하위 폴더 포함").changed();
                changed |= ui.checkbox(&mut self.settings.scan.include_hidden, "숨김 파일 포함").changed();
                ui.horizontal(|ui| { ui.label("최소 크기 · 바이트"); changed |= ui.add(egui::DragValue::new(&mut self.settings.scan.min_size).range(0..=u64::MAX)).changed(); });
                let mut limit = self.settings.scan.max_size.is_some();
                if ui.checkbox(&mut limit, "최대 파일 크기 제한").changed() { self.settings.scan.max_size = limit.then_some(1024 * 1024 * 1024); changed = true; }
                if let Some(maximum) = &mut self.settings.scan.max_size { changed |= ui.add(egui::DragValue::new(maximum).range(0..=u64::MAX).suffix(" 바이트")).changed(); }
                ui.label("제외 패턴 · 한 줄에 하나 · * ? 지원");
                if ui.add(egui::TextEdit::multiline(&mut self.scan_excludes).desired_rows(3).desired_width(f32::INFINITY)).changed() { self.settings.scan.exclude_patterns = self.scan_excludes.lines().map(str::trim).filter(|s| !s.is_empty()).map(str::to_owned).collect(); changed = true; }
                ui.separator(); ui.heading("유사 후보 근거와 비교 한도");
                changed |= ui.checkbox(&mut self.settings.similar.compare_names, "파일 이름").changed();
                changed |= ui.checkbox(&mut self.settings.similar.compare_text, "제한된 텍스트 내용").changed();
                changed |= ui.checkbox(&mut self.settings.similar.compare_images, "이미지 특징").changed();
                changed |= ui.add(egui::Slider::new(&mut self.settings.similar.name_threshold, 0.0..=1.0).text("이름 기준")).changed();
                changed |= ui.add(egui::Slider::new(&mut self.settings.similar.text_threshold, 0.0..=1.0).text("텍스트 기준")).changed();
                changed |= ui.add(egui::Slider::new(&mut self.settings.similar.image_threshold, 0.0..=1.0).text("이미지 기준")).changed();
                ui.horizontal(|ui| { ui.label("최대 비교 쌍"); changed |= ui.add(egui::DragValue::new(&mut self.settings.similar.max_pairs).range(1..=10_000_000)).changed(); });
                ui.label("큰 폴더는 빠른 후보와 비교 한도로 일부 조합을 놓칠 수 있습니다. 완전 중복은 별도로 제외합니다.");
                ui.separator(); ui.heading("완전 중복 보존 규칙");
                let labels = ["표시된 첫 파일", "가장 최근 수정", "가장 오래된 수정", "가장 짧은 상대 경로", "선호 폴더 우선"];
                egui::ComboBox::from_id_salt("retention").selected_text(labels[self.settings.retention.min(4)]).show_ui(ui, |ui| { for (index, label) in labels.iter().enumerate() { changed |= ui.selectable_value(&mut self.settings.retention, index, *label).changed(); } });
                ui.label("이 규칙은 '보존 규칙으로 사본 선택'에 적용합니다. 첫 파일 제외 즉시 정리는 표시된 첫 파일을 유지합니다.");
                ui.horizontal(|ui| { ui.label(format!("선호 폴더: {}", self.settings.preferred.as_ref().map(|p| p.display().to_string()).unwrap_or_else(|| "없음".into()))); if ui.button("선택…").clicked() { if let Some(path) = rfd::FileDialog::new().set_title("선호 원본 폴더").pick_folder() { self.settings.preferred = Some(path); changed = true; } } if ui.button("해제").clicked() { self.settings.preferred = None; changed = true; } });
                ui.strong("보호 폴더 · 자동 분류와 자동 선택에서 보존");
                let mut remove = None;
                for (index, path) in self.settings.protected.iter().enumerate() { ui.horizontal(|ui| { ui.label(path.display().to_string()); if ui.small_button("제거").clicked() { remove = Some(index); } }); }
                if let Some(index) = remove { self.settings.protected.remove(index); changed = true; }
                if ui.button("보호 폴더 추가…").clicked() { if let Some(path) = rfd::FileDialog::new().set_title("보호할 폴더").pick_folder() { if !self.settings.protected.contains(&path) { self.settings.protected.push(path); changed = true; } } }
                ui.label("직접 체크한 파일의 수동 작업은 경로 미리보기에서 별도로 확인하세요.");
            });
        });
        self.settings_open = open;
        if changed {
            self.batch_plan = None;
            if self.multi_open
                && previous_scope != serde_json::to_string(&self.settings.scan).unwrap_or_default()
            {
                self.workspace_folders_changed();
            }
            save_settings(&self.settings);
        }
    }
    fn rules_dialog(&mut self, context: &egui::Context) {
        let mut open = self.rules_open;
        let mut preview = false;
        let mut changed = false;
        egui::Window::new("규칙으로 자동 분류").open(&mut open).default_size(egui::vec2(800.0, 660.0)).show(context, |ui| {
            ui.label(rules::GUIDELINES);
            ui.horizontal(|ui| {
                ui.label("대상 폴더"); ui.text_edit_singleline(&mut self.rules_destination);
                if ui.button("선택…").clicked() { if let Some(path) = rfd::FileDialog::new().pick_folder() { self.rules_destination = path.display().to_string(); } }
            });
            ui.horizontal(|ui| { ui.radio_value(&mut self.rule_copy, true, "복사 · 권장"); ui.radio_value(&mut self.rule_copy, false, "이동"); });
            ui.separator();
            ui.horizontal(|ui| {
                if ui.button("빈 규칙 추가").clicked() && self.settings.rules.len() < 100 { self.settings.rules.push(rules::Rule::default()); self.rule_index = self.settings.rules.len() - 1; self.rule_editor_index = None; changed = true; }
                ui.menu_button("기본 규칙 추가", |ui| { for (_, rule) in rules::presets() { if ui.button(&rule.name).clicked() && self.settings.rules.len() < 100 { self.settings.rules.push(rule); self.rule_index = self.settings.rules.len() - 1; self.rule_editor_index = None; changed = true; ui.close_menu(); } } });
                if ui.add_enabled(self.rule_index > 0, egui::Button::new("위로")).clicked() { self.settings.rules.swap(self.rule_index, self.rule_index - 1); self.rule_index -= 1; changed = true; }
                if ui.add_enabled(self.rule_index + 1 < self.settings.rules.len(), egui::Button::new("아래로")).clicked() { self.settings.rules.swap(self.rule_index, self.rule_index + 1); self.rule_index += 1; changed = true; }
                if ui.add_enabled(!self.settings.rules.is_empty(), egui::Button::new("규칙 삭제")).clicked() { self.settings.rules.remove(self.rule_index.min(self.settings.rules.len() - 1)); self.rule_index = self.rule_index.saturating_sub(1); self.rule_editor_index = None; changed = true; }
            });
            egui::ScrollArea::vertical().max_height(430.0).show(ui, |ui| {
                for (index, rule) in self.settings.rules.iter().enumerate() { if ui.selectable_label(index == self.rule_index, format!("{}  {}{}", index + 1, if rule.enabled { "" } else { "[꺼짐] " }, rule.name)).clicked() { self.rule_index = index; } }
                ui.separator();
                if let Some(rule) = self.settings.rules.get_mut(self.rule_index) {
                    if self.rule_editor_index != Some(self.rule_index) { self.rule_extensions = rule.extensions.join(", "); self.rule_start = rule.start_date.clone().unwrap_or_default(); self.rule_end = rule.end_date.clone().unwrap_or_default(); self.rule_editor_index = Some(self.rule_index); }
                    changed |= ui.checkbox(&mut rule.enabled, "규칙 사용").changed();
                    egui::Grid::new("rule-editor").num_columns(2).spacing(egui::vec2(10.0, 9.0)).show(ui, |ui| {
                        ui.label("규칙 이름"); changed |= ui.text_edit_singleline(&mut rule.name).changed(); ui.end_row();
                        ui.label("폴더 매핑"); changed |= ui.add(egui::TextEdit::singleline(&mut rule.target).desired_width(480.0)).changed(); ui.end_row();
                        ui.label("파일 이름 · * ?"); changed |= ui.text_edit_singleline(&mut rule.name_pattern).changed(); ui.end_row();
                        ui.label("확장자 · 쉼표 구분"); if ui.text_edit_singleline(&mut self.rule_extensions).changed() { rule.extensions = self.rule_extensions.split([',', ';', ' ']).map(str::trim).filter(|v| !v.is_empty()).map(|v| v.trim_start_matches('.').to_owned()).collect(); changed = true; } ui.end_row();
                        ui.label("원래 하위 폴더 조건"); changed |= ui.text_edit_singleline(&mut rule.source_folder).changed(); ui.end_row();
                        ui.label("최소 크기 · 바이트"); changed |= ui.add(egui::DragValue::new(&mut rule.min_size).range(0..=u64::MAX)).changed(); ui.end_row();
                        ui.label("최대 크기"); ui.horizontal(|ui| { let mut bounded = rule.max_size.is_some(); if ui.checkbox(&mut bounded, "제한").changed() { rule.max_size = bounded.then_some(1024 * 1024 * 1024); changed = true; } if let Some(value) = &mut rule.max_size { changed |= ui.add(egui::DragValue::new(value).range(0..=u64::MAX).suffix(" B")).changed(); } }); ui.end_row();
                        ui.label("날짜 조건 기준"); ui.horizontal(|ui| { changed |= ui.radio_value(&mut rule.date_basis, "modified".into(), "수정일").changed(); changed |= ui.radio_value(&mut rule.date_basis, "created".into(), "실제 생성일").changed(); }); ui.end_row();
                        ui.label("시작일 · YYYY-MM-DD"); if ui.text_edit_singleline(&mut self.rule_start).changed() { rule.start_date = (!self.rule_start.trim().is_empty()).then(|| self.rule_start.trim().to_owned()); changed = true; } ui.end_row();
                        ui.label("종료일 · YYYY-MM-DD"); if ui.text_edit_singleline(&mut self.rule_end).changed() { rule.end_date = (!self.rule_end.trim().is_empty()).then(|| self.rule_end.trim().to_owned()); changed = true; } ui.end_row();
                    });
                    egui::CollapsingHeader::new("한글·영문 매핑 기호 도움말").show(ui, |ui| { for (token, description) in rules::mapping_tokens() { ui.label(format!("{token}  {description}")); } ui.label("영문: {rule} {type} {ext} {source_folder} {name} {year} {month} {day} {date} {quarter} {modified:%Y/%m} {created:%Y-%m-%d}"); });
                }
            });
            ui.separator();
            if ui.add_enabled(self.busy.is_none() && (self.workspace_actionable() || !self.multi_open && self.scan.is_some()) && !self.settings.rules.is_empty(), egui::Button::new("조건을 적용하고 전체 경로 미리보기").fill(ACCENT)).clicked() { preview = true; }
        });
        self.rules_open = open;
        if changed {
            save_settings(&self.settings);
        }
        if preview {
            self.prepare_rule_preview(context);
        }
    }
    fn prepare_rule_preview(&mut self, context: &egui::Context) {
        let destination = PathBuf::from(&self.rules_destination);
        if self.multi_open {
            let Some(session) = self.action_session() else {
                return;
            };
            let active_rules = self.settings.rules.clone();
            let protected = self.settings.protected.clone();
            let kind = if self.rule_copy {
                TransferKind::Copy
            } else {
                TransferKind::Move
            };
            self.rules_open = false;
            self.start(
                context,
                "전체 분석 폴더 규칙 분류 미리보기",
                move |_, _| {
                    workspace_actions::plan_classification(
                        &session,
                        &active_rules,
                        &destination,
                        kind,
                        &protected,
                    )
                    .map(ResultData::BatchPlan)
                },
            );
            return;
        }
        let Some(scan) = self.scan.clone() else {
            return;
        };
        let mut rules = self.settings.rules.clone();
        for rule in &mut rules {
            rule.protected_folders
                .extend(self.settings.protected.clone());
        }
        let kind = if self.rule_copy {
            TransferKind::Copy
        } else {
            TransferKind::Move
        };
        self.start(context, "규칙 분류 미리보기 준비", move |_, _| {
            let classified = rules::classification_preview(&scan, &rules, &destination)?;
            let plan = operations::plan_mapped(&scan, &classified.mappings(), kind)?;
            Ok(ResultData::Classified(plan, classified.warnings))
        });
    }
    fn plan_dialog(&mut self, context: &egui::Context) {
        if self.multi_open {
            self.batch_plan_dialog(context);
            return;
        }
        let Some(plan) = &self.plan else {
            return;
        };
        let mut open = true;
        let mut execute = false;
        egui::Window::new(if plan.kind == TransferKind::Delete { "선택 항목을 중복으로 간주 · 수동 확인" } else { "파일 작업 미리보기" }).open(&mut open).default_size(egui::vec2(820.0, 520.0)).show(context, |ui| {
            ui.heading(format!("{} · {}개 · {}", kind_text(plan.kind), plan.items.len(), bytes_text(plan.items.iter().map(|item| item.size).sum())));
            if plan.kind == TransferKind::Delete { ui.colored_label(ACCENT, "수동 선택은 완전 중복 검증을 뜻하지 않습니다. 아래 파일을 delete_files로 이동합니다."); }
            ui.label("대상 파일을 덮어쓰지 않습니다. 작업 전에 원본 변경 여부를 확인하며 기록에서 복원할 수 있습니다.");
            egui::ScrollArea::vertical().max_height(350.0).show_rows(ui, 62.0, plan.items.len(), |ui, range| { for index in range { let item = &plan.items[index]; ui.group(|ui| { ui.label(format!("원본  {}", item.source.display())); ui.label(format!("대상  {}", item.target.display())); }); } });
            ui.separator();
            if ui.add_enabled(self.busy.is_none() && !plan.items.is_empty(), egui::Button::new(format!("위 {}개 {} 실행", plan.items.len(), kind_text(plan.kind))).fill(ACCENT)).clicked() { execute = true; }
        });
        if execute {
            let plan = self.plan.take().unwrap();
            self.start(context, "파일 작업 실행", move |cancel, progress| {
                operations::execute(&plan, cancel, Some(progress))
                    .map(|receipt| ResultData::Receipt(receipt, false))
            });
        } else if !open {
            self.plan = None;
        }
    }
    fn merge_dialog(&mut self, context: &egui::Context) {
        let can_execute = self.busy.is_none() && (!self.multi_open || self.workspace_actionable());
        let Some(preview) = &self.merge else {
            return;
        };
        let mut open = true;
        let mut execute = false;
        egui::Window::new("같은 문단·행을 한 번씩 남긴 새 병합본").open(&mut open).default_width(720.0).show(context, |ui| {
            ui.label(format!("새 파일: {}", preview.output.display()));
            ui.label(format!("{}개 파일 · 전체 {}{} → 고유 {}{} · 중복 {}{} · 새 파일 {}", preview.sources.len(), preview.total_units, preview.unit, preview.unique_units, preview.unit, preview.removed_units, preview.unit, bytes_text(preview.output_bytes as u64)));
            ui.label("문단 또는 표의 행이 정확히 같을 때만 합칩니다. 내용의 의미·충돌은 판단하지 않습니다.");
            egui::ScrollArea::vertical().max_height(240.0).show(ui, |ui| { for source in &preview.sources { ui.label(source.path.display().to_string()); } });
            ui.separator(); ui.checkbox(&mut self.archive_merge, "새 병합본이 검증된 뒤 원본을 delete_files로 보관");
            ui.label(if self.archive_merge { "원본 보관은 별도 작업 기록에 남으며 복원할 수 있습니다. 새 병합본은 유지합니다." } else { "기본 동작: 원본을 유지하고 새 파일만 만듭니다." });
            if ui.add_enabled(can_execute, egui::Button::new("위 내용으로 새 병합본 만들기").fill(ACCENT)).clicked() { execute = true; }
        });
        if execute {
            let preview = self.merge.take().unwrap();
            let archive = self.archive_merge;
            if self.multi_open {
                let Some(session) = self.action_session() else {
                    return;
                };
                self.start(
                    context,
                    "선택 폴더의 새 병합본 생성",
                    move |cancel, progress| {
                        if cancel.load(Ordering::Relaxed) {
                            return Err("취소했습니다. 원본을 유지했습니다.".into());
                        }
                        let paths = preview
                            .sources
                            .iter()
                            .map(|source| source.path.clone())
                            .collect::<Vec<_>>();
                        let retirement = if archive {
                            Some(workspace_actions::plan_transfer(
                                &session,
                                &paths,
                                None,
                                TransferKind::Delete,
                            )?)
                        } else {
                            None
                        };
                        let output = merging::execute_merge(&preview)?;
                        let receipt = retirement
                            .as_ref()
                            .map(|plan| workspace_actions::execute(plan, cancel, Some(progress)))
                            .transpose()
                            .map_err(|error| {
                                format!(
                                    "새 병합본 {} 생성 완료. 원본 보관 오류: {error}",
                                    output.display()
                                )
                            })?;
                        Ok(ResultData::WorkspaceMerged(output, receipt))
                    },
                );
                return;
            }
            let scan = self.scan.clone();
            self.start(context, "새 병합본 생성", move |cancel, progress| {
                if cancel.load(Ordering::Relaxed) {
                    return Err("취소했습니다. 원본을 유지했습니다.".into());
                }
                let output = merging::execute_merge(&preview)?;
                let receipt = if archive {
                    let scan = scan.ok_or_else(|| {
                        format!(
                            "병합본 {} 생성 완료. 분석 목록이 없어 원본은 유지했습니다.",
                            output.display()
                        )
                    })?;
                    let paths: Vec<_> = preview
                        .sources
                        .iter()
                        .map(|source| source.path.clone())
                        .collect();
                    let plan = operations::plan_files(&scan, &paths, None, TransferKind::Delete)
                        .map_err(|error| {
                            format!(
                                "병합본 {} 생성 완료. 원본 보관 준비 실패: {error}",
                                output.display()
                            )
                        })?;
                    Some(
                        operations::execute(&plan, cancel, Some(progress)).map_err(|error| {
                            format!(
                                "병합본 {} 생성 완료. 원본 보관 오류: {error}",
                                output.display()
                            )
                        })?,
                    )
                } else {
                    None
                };
                Ok(ResultData::Merged(output, receipt))
            });
        } else if !open {
            self.merge = None;
        }
    }
    fn history_dialog(&mut self, context: &egui::Context) {
        let mut open = self.history_open;
        let mut restore = false;
        let mut compact = false;
        let mut reload = false;
        egui::Window::new("Rust 작업 기록 · 복원").open(&mut open).default_size(egui::vec2(850.0, 550.0)).show(context, |ui| {
            if let Some(root) = &self.root { ui.add(egui::Label::new(format!("기록 폴더: {}", root.display())).wrap()); }
            ui.label("선택한 작업의 파일을 원래 경로로 복원합니다. 기존 파일은 덮어쓰지 않습니다. Python 기록은 별도입니다.");
            if ui.add_enabled(self.busy.is_none(), egui::Button::new("기록 새로 읽기")).clicked() { reload = true; }
            egui::ScrollArea::vertical().max_height(350.0).show(ui, |ui| {
                for record in &self.history {
                    let label = format!("{} · {} · {}개 · {} · {}{}", date_text(record.created_at as i128 * 1_000_000_000), kind_text(record.kind), record.files, bytes_text(record.bytes), record.status, if record.restored { " · 복원됨" } else { "" });
                    if ui.selectable_label(self.history_selected.as_ref() == Some(&record.id), label).on_hover_text(format!("작업 {} · 압축 저장 {}", record.id, bytes_text(record.packed_bytes))).clicked() { self.history_selected = Some(record.id.clone()); }
                }
                if self.history.is_empty() { ui.label("이 폴더에 기록된 Rust 파일 작업이 없습니다."); }
            });
            let selected = self.history_selected.as_ref().and_then(|id| self.history.iter().find(|record| &record.id == id));
            ui.separator();
            ui.horizontal(|ui| {
                if ui.add_enabled(self.busy.is_none() && selected.is_some_and(|record| !record.restored), egui::Button::new("선택 작업 복원").fill(ACCENT)).clicked() { restore = true; }
                if ui.add_enabled(self.busy.is_none() && selected.is_some_and(|record| !record.restored && record.kind != TransferKind::Copy), egui::Button::new("선택 작업의 복원 데이터 최적화")).clicked() { compact = true; }
            });
            ui.label("최적화는 보관 원문을 검증한 뒤 조각 중복 제거·무손실 압축합니다. 기록과 보관 데이터를 함께 유지하세요.");
        });
        self.history_open = open;
        if reload {
            self.load_history(context);
        }
        if let (Some(root), Some(id)) = (self.root.clone(), self.history_selected.clone()) {
            if restore {
                self.start(context, "선택 작업 복원", move |cancel, progress| {
                    operations::undo(&root, &id, cancel, Some(progress))
                        .map(|receipt| ResultData::Receipt(receipt, true))
                });
            } else if compact {
                self.compaction_request = Some((root, id));
            }
        }
    }
    fn compaction_dialog(&mut self, context: &egui::Context) {
        if let Some((root, id)) = &self.compaction_request {
            let mut open = true;
            let mut execute = false;
            egui::Window::new("복원 데이터 최적화 미리보기").open(&mut open).default_width(650.0).show(context, |ui| {
                if let Some(record) = self.history.iter().find(|record| &record.id == id) { ui.heading(format!("{} · {}개 · {}", date_text(record.created_at as i128 * 1_000_000_000), record.files, bytes_text(record.bytes))); }
                ui.label(format!("기록 폴더: {}", root.display()));
                ui.label("보관 원문을 검증한 뒤 중복 조각을 줄이고 무손실 압축합니다. 효과가 없으면 원본 보관을 유지합니다.");
                ui.colored_label(ACCENT, COMPACTION_SCOPE);
                ui.label("추가 데이터 스트림·파일 암호화 등 현재 압축 형식으로 보존할 수 없는 속성이 있거나 확인할 수 없으면, 해당 파일은 원본 보관을 유지하고 이유를 결과에 표시합니다.");
                ui.separator();
                if ui.add_enabled(self.busy.is_none(), egui::Button::new("위 범위로 선택 작업 최적화 실행").fill(ACCENT)).clicked() { execute = true; }
            });
            if execute {
                let (root, id) = self.compaction_request.take().unwrap();
                self.start(
                    context,
                    "복원 데이터 최적화",
                    move |cancel, progress| {
                        operations::compact(&root, &id, cancel, Some(progress))
                            .map(ResultData::Compact)
                    },
                );
            } else if !open {
                self.compaction_request = None;
            }
        }
        if let Some(result) = &self.compaction_result {
            let mut open = true;
            egui::Window::new("복원 데이터 최적화 결과")
                .open(&mut open)
                .default_width(720.0)
                .show(context, |ui| {
                    ui.heading(format!(
                        "{}개 파일 · {} 절감",
                        result.files,
                        bytes_text(result.saved_bytes)
                    ));
                    ui.label(format!(
                        "원래 {} → 저장 {}",
                        bytes_text(result.original_bytes),
                        bytes_text(result.stored_bytes)
                    ));
                    ui.label(COMPACTION_SCOPE);
                    if !result.warnings.is_empty() {
                        ui.separator();
                        ui.colored_label(ACCENT, "원본 보관 유지 · 파일별 이유");
                        egui::ScrollArea::vertical()
                            .max_height(320.0)
                            .show(ui, |ui| {
                                for warning in &result.warnings {
                                    ui.label(warning);
                                    ui.add_space(5.0);
                                }
                            });
                    }
                });
            if !open {
                self.compaction_result = None;
            }
        }
    }
    fn logs_dialog(&mut self, context: &egui::Context) {
        let mut open = self.logs_open;
        let mut reload = false;
        egui::Window::new("파일 실행 로그")
            .open(&mut open)
            .default_size(egui::vec2(880.0, 550.0))
            .show(context, |ui| {
                if let Some(root) = &self.logs_root {
                    ui.add(egui::Label::new(format!("로그 폴더: {}", root.display())).wrap());
                }
                ui.horizontal(|ui| {
                    ui.label("찾기");
                    ui.text_edit_singleline(&mut self.log_query);
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("새로 읽기"))
                        .clicked()
                    {
                        reload = true;
                    }
                });
                let query = self.log_query.to_lowercase();
                let rows: Vec<_> = self
                    .logs
                    .iter()
                    .filter(|row| {
                        query.is_empty()
                            || format!(
                                "{} {} {} {} {}",
                                row.operation_id, row.action, row.path, row.status, row.message
                            )
                            .to_lowercase()
                            .contains(&query)
                    })
                    .collect();
                ui.label(format!(
                    "{}개 기록 · 선택한 폴더의 Rust 작업만 표시",
                    rows.len()
                ));
                egui::ScrollArea::vertical().show_rows(ui, 64.0, rows.len(), |ui, range| {
                    for index in range {
                        let row = rows[index];
                        ui.group(|ui| {
                            ui.label(format!(
                                "{} · {} · {} · {}",
                                date_text(row.time as i128 * 1_000_000_000),
                                row.operation_id,
                                row.action,
                                row.status
                            ));
                            ui.label(format!("{}  {}", row.path, row.message));
                        });
                    }
                });
            });
        self.logs_open = open;
        if reload {
            self.load_logs(context);
        }
    }
    fn activities_dialog(&mut self, context: &egui::Context) {
        let mut open = self.activities_open;
        egui::Window::new("앱 활동 · 진단")
            .open(&mut open)
            .default_size(egui::vec2(850.0, 540.0))
            .show(context, |ui| {
                ui.label(format!(
                    "Folder Organizer Rust {VERSION} · {} {}",
                    std::env::consts::OS,
                    std::env::consts::ARCH
                ));
                ui.label(format!(
                    "설정·활동 로그: {}",
                    settings_directory().display()
                ));
                if let Some(scan) = &self.scan {
                    ui.label(format!(
                        "분석: 방문 {} · 메타데이터 {} · 폴더 {} · 제외 {} · {} ms",
                        scan.metrics.visited_entries,
                        scan.metrics.metadata_reads,
                        scan.metrics.directories,
                        scan.metrics.filtered_files,
                        scan.metrics.elapsed_ms
                    ));
                    egui::CollapsingHeader::new(format!("분석 경고 {}개", scan.warnings.len()))
                        .show(ui, |ui| {
                            egui::ScrollArea::vertical().max_height(150.0).show_rows(
                                ui,
                                22.0,
                                scan.warnings.len(),
                                |ui, range| {
                                    for index in range {
                                        ui.label(&scan.warnings[index]);
                                    }
                                },
                            );
                        });
                }
                if let Some(report) = &self.similar {
                    egui::CollapsingHeader::new(format!(
                        "유사 비교 경고 {}개 · 완전 중복 제외 {}쌍",
                        report.warnings.len(),
                        report.excluded_exact_pairs
                    ))
                    .show(ui, |ui| {
                        for warning in &report.warnings {
                            ui.label(warning);
                        }
                    });
                }
                if ui.button("진단 내용 복사").clicked() {
                    let mut diagnostic = format!(
                        "Folder Organizer Rust {VERSION}\n{} {}\n{}\n",
                        std::env::consts::OS,
                        std::env::consts::ARCH,
                        self.status
                    );
                    for line in &self.notices {
                        diagnostic.push_str(line);
                        diagnostic.push('\n');
                    }
                    ui.ctx().copy_text(diagnostic);
                }
                ui.separator();
                egui::ScrollArea::vertical()
                    .stick_to_bottom(true)
                    .show(ui, |ui| {
                        for line in &self.notices {
                            ui.label(line);
                        }
                    });
            });
        self.activities_open = open;
    }
    fn help_dialog(&mut self, context: &egui::Context) {
        let mut open = self.help_open;
        egui::Window::new("기능 사용 안내")
            .open(&mut open)
            .default_size(egui::vec2(760.0, 600.0))
            .show(context, |ui| {
                ui.horizontal(|ui| {
                    ui.label("도움말 찾기");
                    ui.text_edit_singleline(&mut self.help_query);
                });
                let query = self.help_query.to_lowercase();
                egui::ScrollArea::vertical().show(ui, |ui| {
                    for (title, body) in HELP {
                        if query.is_empty()
                            || title.to_lowercase().contains(&query)
                            || body.to_lowercase().contains(&query)
                        {
                            egui::CollapsingHeader::new(*title)
                                .default_open(!query.is_empty())
                                .show(ui, |ui| {
                                    ui.label(*body);
                                });
                        }
                    }
                });
            });
        self.help_open = open;
    }
    fn multiroot_workspace(&mut self, context: &egui::Context) {
        let available = self.busy.is_none();
        let mut folders_changed = false;
        egui::SidePanel::left("workspace-search-paths").resizable(true).default_width(236.0).width_range(210.0..=380.0).show(context, |ui| {
            ui.add_space(8.0);
            ui.heading("검색 폴더");
            ui.label(RichText::new("체크한 폴더를 하나의 결과로 분석합니다").small().weak());
            ui.horizontal(|ui| {
                if ui.add_enabled(available, egui::Button::new("＋ 추가")).clicked() { self.add_workspace_folders(); }
                if ui.add_enabled(available && self.workspace_root.is_some(), egui::Button::new("목록에서 제거")).on_hover_text("검색 목록에서만 제거합니다. 실제 폴더는 삭제하지 않습니다.").clicked() {
                    if let Some(index) = self.workspace_root.take() {
                        if index < self.multi_folders.len() { self.multi_folders.remove(index); folders_changed = true; }
                    }
                }
            });
            ui.horizontal(|ui| {
                if ui.add_enabled(available, egui::Button::new("전체 체크")).clicked() {
                    for folder in &mut self.multi_folders { folders_changed |= !folder.selected; folder.selected = true; }
                }
                if ui.add_enabled(available, egui::Button::new("해제")).clicked() {
                    for folder in &mut self.multi_folders { folders_changed |= folder.selected; folder.selected = false; }
                }
            });
            ui.separator();
            if self.multi_folders.is_empty() { ui.label("폴더를 추가하세요. 내 PC·연결된 NAS 폴더를 선택할 수 있습니다."); }
            egui::ScrollArea::vertical().id_salt("workspace-root-list").max_height((ui.available_height() * 0.48).max(110.0)).show(ui, |ui| {
                for (index, folder) in self.multi_folders.iter_mut().enumerate() {
                    ui.horizontal(|ui| {
                        folders_changed |= ui.add_enabled(available, egui::Checkbox::without_text(&mut folder.selected)).changed();
                        let response = ui.add(egui::Button::new(folder_display_name(&folder.path)).selected(self.workspace_root == Some(index)).truncate());
                        if response.clicked() { self.workspace_root = Some(index); }
                        response.on_hover_text(folder.path.display().to_string());
                    });
                    if let Some(root) = self.multi_result.as_ref().and_then(|report| report.roots.iter().find(|root| root.requested_path == folder.path)) {
                        ui.label(RichText::new(format!("{} · {}개", multi_state_label(&root.state), root.file_count)).small().weak());
                    }
                }
            });
            let selected_roots = self.multi_folders.iter().filter(|folder| folder.selected).count();
            ui.small(format!("체크 {selected_roots} / 등록 {}개", self.multi_folders.len()));
            if let Some(root) = self.workspace_root.and_then(|index| self.multi_folders.get(index)) {
                ui.add(egui::Label::new(root.path.display().to_string()).wrap().selectable(true));
            }
            ui.separator();
            ui.heading("검색 필터");
            egui::CollapsingHeader::new("폴더·파일 범위").default_open(true).show(ui, |ui| {
                folders_changed |= ui.add_enabled(available, egui::Checkbox::new(&mut self.settings.scan.recursive, "하위 폴더 포함")).changed();
                folders_changed |= ui.add_enabled(available, egui::Checkbox::new(&mut self.settings.scan.include_hidden, "숨김 파일 포함")).changed();
                if ui.add_enabled(available, egui::Button::new("이름·날짜·크기·제외 설정…")).clicked() { self.settings_open = true; }
                ui.label(RichText::new("설정한 조건은 다음 분석에 적용합니다.").small().weak());
            });
            egui::CollapsingHeader::new("분석 속도").show(ui, |ui| {
                ui.horizontal(|ui| { ui.label("동시 탐색"); ui.add_enabled(available, egui::DragValue::new(&mut self.multi_workers).range(1..=4)); });
                ui.label(RichText::new("같은 장치·NAS 서버와 파일 내용 검사는 순서대로 읽습니다.").small().weak());
            });
            ui.separator();
            ui.small("폴더가 한 개여도 같은 방식입니다. 결과의 파일을 체크하고 위 리본에서 검사·정리를 실행하세요.");
            if ui.add_enabled(available && self.workspace_root.is_some(), egui::Button::new("선택 폴더 기록·복원…")).clicked() { self.load_history(context); }
        });
        if folders_changed {
            self.workspace_folders_changed();
        }
        self.workspace_properties(context);
        egui::TopBottomPanel::bottom("workspace-file-types")
            .resizable(true)
            .default_height(145.0)
            .min_height(94.0)
            .show(context, |ui| {
                ui.horizontal(|ui| {
                    ui.strong("파일 종류별 요약");
                    if ui
                        .selectable_label(self.workspace_extension.is_none(), "전체 보기")
                        .clicked()
                    {
                        self.workspace_extension = None;
                        self.filter_workspace_rows();
                    }
                    ui.label(
                        RichText::new("종류를 클릭하면 목록을 좁힙니다")
                            .small()
                            .weak(),
                    );
                });
                let summary_height = ui.available_height().max(36.0);
                TableBuilder::new(ui)
                    .id_salt("workspace-type-summary")
                    .striped(true)
                    .resizable(true)
                    .column(Column::remainder())
                    .column(Column::initial(75.0))
                    .column(Column::initial(100.0))
                    .column(Column::initial(75.0))
                    .column(Column::initial(105.0))
                    .max_scroll_height(summary_height)
                    .header(25.0, |mut header| {
                        for title in ["종류", "파일 수", "파일 용량", "중복 사본", "중복 용량"]
                        {
                            header.col(|ui| {
                                ui.strong(title);
                            });
                        }
                    })
                    .body(|body| {
                        body.rows(25.0, self.workspace_types.len(), |mut row| {
                            let (extension, count, size, duplicates, duplicate_size) =
                                self.workspace_types[row.index()].clone();
                            row.col(|ui| {
                                if ui
                                    .selectable_label(
                                        self.workspace_extension.as_ref() == Some(&extension),
                                        if extension.is_empty() {
                                            "확장자 없음"
                                        } else {
                                            &extension
                                        },
                                    )
                                    .clicked()
                                {
                                    self.workspace_extension = Some(extension);
                                    self.filter_workspace_rows();
                                }
                            });
                            row.col(|ui| {
                                ui.label(count.to_string());
                            });
                            row.col(|ui| {
                                ui.label(bytes_text(size));
                            });
                            row.col(|ui| {
                                ui.label(duplicates.to_string());
                            });
                            row.col(|ui| {
                                ui.label(bytes_text(duplicate_size));
                            });
                        });
                    });
            });
        egui::CentralPanel::default().show(context, |ui| {
            let Some(report) = self.multi_result.clone() else {
                if let Some(busy) = &self.busy {
                    ui.horizontal(|ui| { ui.spinner(); ui.heading("폴더를 분석하고 있습니다"); });
                    ui.label(format!("원본을 유지하며 조회·완전 중복 검사 중 · {:.1}초", busy.started.elapsed().as_secs_f32()));
                    for (root_id, progress) in &self.multi_progress { ui.label(format!("{} · {} · {}개", self.multi_root_labels.get(root_id).map(String::as_str).unwrap_or("폴더 간 검사"), multi_phase_label(&progress.phase), progress.completed)); }
                } else {
                    ui.heading("파일을 한 화면에서 분석·검사·정리하세요");
                    ui.add_space(14.0);
                    for (step, title, description) in [("1", "폴더 추가", "검색 폴더를 한 개 또는 여러 개 등록하고 체크합니다."), ("2", "분석 시작", "크기·날짜와 폴더 사이의 완전 중복을 확인합니다."), ("3", "검사·정리", "파일을 체크해 복사·이동·분류하거나 검증된 중복을 정리합니다.")] {
                        ui.group(|ui| { ui.horizontal(|ui| { ui.label(RichText::new(step).color(ACCENT).size(20.0)); ui.vertical(|ui| { ui.strong(title); ui.label(description); }); }); }); ui.add_space(8.0);
                    }
                }
                return;
            };
            if self.selection_bytes_dirty {
                self.selected_bytes = report.files.iter().filter(|file| self.selected.contains(&file.path)).fold(0u64, |total, file| total.saturating_add(file.size));
                self.selection_bytes_dirty = false;
            }
            ui.horizontal(|ui| {
                ui.heading("분석 결과"); ui.label(format!("{}개 파일 · 체크 {}개 · {}", report.files.len(), self.selected.len(), bytes_text(self.selected_bytes)));
            });
            if self.workspace_stale { ui.colored_label(ACCENT, "파일 작업 후의 목록입니다. 다음 정리·최신 보고서는 새 분석 후 확인하세요."); }
            else if !report.complete { ui.colored_label(ACCENT, if report.cancelled { "중지된 부분 결과 · 전체 범위의 중복 없음 판정이 아닙니다" } else { "일부 폴더·파일을 확인하지 못했습니다. 정리 전에 새 분석으로 범위를 확인하세요." }); }
            else if !self.workspace_actionable() { ui.colored_label(ACCENT, "작업 후 갱신한 목록입니다. 다음 정리는 새 분석 후 실행하세요."); }
            ui.horizontal(|ui| {
                ui.selectable_value(&mut self.view, View::Files, format!("전체 파일 ({})", report.files.len()));
                ui.selectable_value(&mut self.view, View::Duplicates, format!("완전 중복 ({})", report.duplicates.groups.len()));
                ui.selectable_value(&mut self.view, View::Similar, format!("유사 후보 ({})", self.similar.as_ref().map_or(0, |value| value.pairs.len())));
            });
            if self.view == View::Similar { self.similar_view(ui, context); return; }
            if self.view == View::Files {
                ui.horizontal(|ui| {
                    ui.label("찾기"); let mut changed = ui.add(egui::TextEdit::singleline(&mut self.workspace_query).hint_text("파일 이름 또는 경로").desired_width((ui.available_width() - 170.0).max(100.0))).changed();
                    egui::ComboBox::from_id_salt("workspace-sort").selected_text(match self.sort { Sort::Name => "이름순", Sort::Size => "크기순", Sort::Modified => "수정일순", Sort::Created => "생성일순" }).show_ui(ui, |ui| {
                        for (sort, name) in [(Sort::Name, "이름순"), (Sort::Size, "크기순"), (Sort::Modified, "수정일순"), (Sort::Created, "생성일순")] { changed |= ui.selectable_value(&mut self.sort, sort, name).changed(); }
                    });
                    changed |= ui.checkbox(&mut self.descending, "역순").changed(); if changed { self.filter_workspace_rows(); }
                });
                ui.small(format!("표시 {} / 전체 {}개 · 체크박스는 실제 정리 대상입니다 · 필터로 숨긴 체크도 유지됩니다", self.workspace_visible.len(), report.files.len()));
                let table_height = ui.available_height().max(80.0);
                TableBuilder::new(ui).id_salt("workspace-file-table").striped(true).resizable(true)
                    .cell_layout(egui::Layout::left_to_right(egui::Align::Center))
                    .column(Column::exact(28.0)).column(Column::remainder().at_least(125.0)).column(Column::initial(80.0))
                    .column(Column::initial(130.0)).column(Column::initial(75.0)).column(Column::initial(130.0))
                    .max_scroll_height(table_height)
                    .header(28.0, |mut header| { for title in ["✓", "이름", "용량", "수정일", "중복 그룹", "폴더"] { header.col(|ui| { ui.strong(title); }); } })
                    .body(|body| { body.rows(28.0, self.workspace_visible.len(), |mut row| {
                        let index = self.workspace_visible[row.index()]; let file = &report.files[index]; let cache = &self.workspace_rows[index];
                        row.set_selected(self.workspace_file == Some(index));
                        row.col(|ui| { let mut checked = self.selected.contains(&file.path); if ui.add_enabled(available, egui::Checkbox::without_text(&mut checked)).changed() { if checked { self.selected.insert(file.path.clone()); } else { self.selected.remove(&file.path); } self.selection_bytes_dirty = true; } });
                        row.col(|ui| { if ui.add(egui::Button::new(folder_display_name(&file.path)).selected(self.workspace_file == Some(index)).truncate()).on_hover_text(file.path.display().to_string()).clicked() { self.workspace_file = Some(index); } });
                        row.col(|ui| { ui.label(&cache.size); }); row.col(|ui| { ui.label(&cache.modified); });
                        row.col(|ui| { ui.label(self.workspace_groups.get(&file.path).map(|group| format!("{}", group + 1)).unwrap_or_else(|| "—".into())); });
                        row.col(|ui| { ui.add(egui::Label::new(file.path.parent().map(|path| path.display().to_string()).unwrap_or_default()).truncate()).on_hover_text(file.path.display().to_string()); });
                    }); });
            } else {
                ui.small("SHA-256·실제 바이트로 검증했습니다. 첫 파일은 더 좋은 원본이라는 뜻이 아닙니다.");
                if self.multi_rows.is_empty() { ui.label("확인된 완전 중복 그룹이 없습니다."); }
                egui::ScrollArea::vertical().id_salt("workspace-duplicate-table").show_rows(ui, 31.0, self.multi_rows.len(), |ui, range| {
                    for row in range { match self.multi_rows[row] {
                        MultiDuplicateRow::Header(group_index) => { let group = &report.duplicates.groups[group_index]; ui.label(RichText::new(format!("그룹 {} · {}개 · 파일당 {}", group_index + 1, group.paths.len(), bytes_text(group.size))).strong().color(ACCENT)); }
                        MultiDuplicateRow::File(group_index, file_index) => {
                            let path = &report.duplicates.groups[group_index].paths[file_index]; let index = self.workspace_index.get(path).copied();
                            ui.horizontal(|ui| { let mut checked = self.selected.contains(path); if ui.add_enabled(available, egui::Checkbox::without_text(&mut checked)).changed() { if checked { self.selected.insert(path.clone()); } else { self.selected.remove(path); } self.selection_bytes_dirty = true; }
                                if ui.add(egui::Button::new(format!("{}{}", if file_index == 0 { "첫 파일 · " } else { "" }, path.display())).selected(self.workspace_file == index && index.is_some()).truncate()).on_hover_text(path.display().to_string()).clicked() { self.workspace_file = index; }
                            });
                        }
                    }}
                });
            }
            if self.multi_warning_total > 0 { egui::CollapsingHeader::new(format!("분석 안내 {}건", self.multi_warning_total)).show(ui, |ui| { egui::ScrollArea::vertical().id_salt("workspace-warnings").max_height(90.0).show_rows(ui, 24.0, self.multi_warnings.len(), |ui, range| { for index in range { ui.add(egui::Label::new(&self.multi_warnings[index]).truncate()).on_hover_text(&self.multi_warnings[index]); } }); }); }
        });
    }
    fn workspace_properties(&self, context: &egui::Context) {
        egui::SidePanel::right("workspace-file-properties").resizable(true).default_width(260.0).width_range(220.0..=420.0).show(context, |ui| {
            ui.add_space(8.0);
            ui.heading("파일 정보");
            ui.separator();
            let Some(report) = &self.multi_result else { ui.label("분석 후 결과에서 파일을 선택하세요."); return; };
            let Some(index) = self.workspace_file else { ui.label("결과에서 파일을 선택하세요."); return; };
            let Some(file) = report.files.get(index) else { return; };
            let cache = &self.workspace_rows[index];
            egui::ScrollArea::vertical().id_salt("workspace-properties-scroll").show(ui, |ui| {
                ui.label(RichText::new(folder_display_name(&file.path)).strong().size(17.0));
                for (label, value) in [
                    ("전체 경로", file.path.display().to_string()),
                    ("용량", format!("{} · {} 바이트", cache.size, file.size)),
                    ("수정일", cache.modified.clone()),
                    ("생성일", cache.created.clone()),
                    ("종류", format!("{} · {}", file.kind, if file.extension.is_empty() { "확장자 없음" } else { &file.extension })),
                ] {
                    ui.add_space(9.0); ui.label(RichText::new(label).small().weak());
                    ui.add(egui::Label::new(value).wrap().selectable(true));
                }
                ui.add_space(9.0); ui.label(RichText::new("포함된 분석 폴더").small().weak());
                for root in report.roots.iter().filter(|root| file.root_ids.contains(&root.root_id)) {
                    ui.add(egui::Label::new(root.requested_path.display().to_string()).wrap());
                }
                if let Some(group) = self.workspace_groups.get(&file.path).and_then(|index| report.duplicates.groups.get(*index)) {
                    ui.separator(); ui.strong(format!("완전 중복 · {}개 파일", group.paths.len()));
                    ui.small("SHA-256"); ui.add(egui::Label::new(&group.sha256).wrap().selectable(true));
                    ui.label("파일 내용과 실제 바이트가 같습니다. 이름·날짜는 다를 수 있습니다.");
                }
                ui.add_space(12.0); ui.separator();
                ui.label(RichText::new("분석 시점의 정보입니다. 이 화면은 파일을 추가로 읽거나 변경하지 않습니다.").small().weak());
            });
        });
    }
    fn overview_dialog(&mut self, context: &egui::Context) {
        let Some(overview) = &self.overview else {
            return;
        };
        let mut open = true;
        egui::Window::new("용량·정리 요약").open(&mut open).default_size(egui::vec2(750.0, 600.0)).show(context, |ui| {
            ui.heading(format!("파일 {}개 · {}", overview.files, bytes_text(overview.bytes)));
            ui.label(format!("빈 파일 {}개{} · 수정 후 1년 {}개", overview.empty.len(), if overview.empty.len() == 1000 { "+ · 경로는 1,000개까지" } else { "" }, overview.old));
            ui.label("표시된 용량은 논리적 파일 크기 합계입니다. 하드 링크·압축·NAS 실제 점유 공간과 다를 수 있습니다.");
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.heading("종류별");
                for (name, size, count) in &overview.kinds { ui.horizontal(|ui| { ui.label(format!("{name} · {count}개")); ui.add(egui::ProgressBar::new(if overview.bytes == 0 { 0.0 } else { *size as f32 / overview.bytes as f32 }).desired_width(350.0).text(bytes_text(*size))); }); }
                ui.separator(); ui.heading("상위 폴더별"); for (name, size, count) in &overview.folders { ui.label(format!("{name} · {count}개 · {}", bytes_text(*size))); }
                ui.separator(); ui.heading("큰 파일 20개"); for (path, size) in &overview.largest { ui.label(format!("{} · {}", bytes_text(*size), path.display())); }
                egui::CollapsingHeader::new("빈 파일 경로").show(ui, |ui| { for path in &overview.empty { ui.label(path.display().to_string()); } });
            });
        });
        if !open {
            self.overview = None;
        }
    }
    fn comparison_dialog(&mut self, context: &egui::Context) {
        let Some(comparison) = &mut self.comparison else {
            return;
        };
        let mut open = true;
        for (index, side) in comparison.sides.iter().enumerate() {
            if comparison.textures[index].is_none() {
                if let Some(image) = &side.image {
                    comparison.textures[index] = Some(context.load_texture(
                        format!("comparison-{index}-{}", side.sha256),
                        egui::ColorImage::from_rgba_unmultiplied(image.dimensions, &image.rgba),
                        egui::TextureOptions::LINEAR,
                    ));
                }
            }
        }
        egui::Window::new("두 파일 나란히 비교 · 읽기 전용").open(&mut open).default_size(egui::vec2(1080.0, 680.0)).show(context, |ui| {
            let equal = comparison.sides.len() == 2 && comparison.sides[0].sha256 == comparison.sides[1].sha256;
            ui.label(if equal { "현재 전체 SHA-256이 같습니다. 완전 중복 정리는 별도 중복 검사의 실제 바이트 검증 결과를 사용합니다." } else { "전체 SHA-256이 다릅니다. 유사 후보는 서로 다른 파일일 수 있습니다." });
            ui.columns(2, |columns| { for (index, side) in comparison.sides.iter().enumerate() { let ui = &mut columns[index]; ui.strong(side.path.file_name().unwrap_or_default().to_string_lossy()); ui.label(side.path.display().to_string()); ui.label(format!("{} · 수정 {} · 생성 {}", bytes_text(side.size), side.modified, side.created)); ui.label(RichText::new(format!("SHA-256 {}", side.sha256)).small()); ui.separator();
                egui::ScrollArea::both().id_salt(("comparison", index)).max_height(480.0).show(ui, |ui| {
                    if let Some(texture) = &comparison.textures[index] { ui.add(egui::Image::new(texture).max_size(egui::vec2(ui.available_width(), 440.0)).maintain_aspect_ratio(true)); }
                    if let Some(text) = &side.text { ui.label(RichText::new(text).monospace()); } else if side.image.is_none() { ui.label("지원하는 UTF-8 텍스트·이미지 미리보기가 없습니다. 경로·크기·날짜·전체 해시를 비교하세요."); }
                });
            } });
        });
        if !open {
            self.comparison = None;
        }
    }
    fn health_dialog(&mut self, context: &egui::Context) {
        let Some(results) = &self.health else {
            return;
        };
        let mut open = true;
        egui::Window::new("선택 파일 건강 검사 결과").open(&mut open).default_size(egui::vec2(820.0, 580.0)).show(context, |ui| {
            ui.label("파일 구조를 검사한 결과이며 모든 내용의 정확성을 보장하지 않습니다. 원본 유지 · 새 복구본은 도구 메뉴에서 만듭니다.");
            egui::ScrollArea::vertical().show(ui, |ui| { for (path, result) in results { egui::CollapsingHeader::new(format!("{} · {}", if result.as_ref().is_ok_and(|result| result.healthy) { "정상 구조" } else { "확인 필요" }, path.display())).default_open(result.as_ref().is_err() || result.as_ref().is_ok_and(|result| !result.healthy)).show(ui, |ui| { match result { Ok(result) => { ui.label(format!("{} · {} · 검사 {}개 · 복구 가능 {}개", result.format, result.status, result.checked_units, result.recoverable_units)); ui.label(&result.explanation); for issue in &result.issues { ui.label(issue); } }, Err(error) => { ui.colored_label(ACCENT, error); } } }); } });
        });
        if !open {
            self.health = None;
        }
    }
    fn update_dialog(&mut self, context: &egui::Context) {
        let Some(release) = &self.update else {
            return;
        };
        let mut open = true;
        let mut prepare = false;
        egui::Window::new("Rust 배포판 업데이트").open(&mut open).default_width(560.0).show(context, |ui| {
            ui.heading(format!("새 Rust 버전 {}", release.version)); ui.label(format!("현재 {VERSION} · 다운로드 {}", bytes_text(release.zip_size)));
            ui.label("Rust ZIP·체크섬·배포판 표식을 확인한 뒤 별도 도우미가 앱 종료 후 적용합니다. 개인 데이터 폴더에는 설치하지 마세요.");
            ui.hyperlink_to("릴리스 설명 보기", &release.release_url);
            if ui.add_enabled(self.busy.is_none(), egui::Button::new("다운로드하고 검증").fill(ACCENT)).clicked() { prepare = true; }
        });
        if prepare {
            let release = self.update.take().unwrap();
            self.prepare_update(context, release);
        } else if !open {
            self.update = None;
        }
    }
    fn dialogs(&mut self, context: &egui::Context) {
        if self.settings_open {
            self.settings_dialog(context);
        }
        if self.rules_open {
            self.rules_dialog(context);
        }
        self.plan_dialog(context);
        self.merge_dialog(context);
        if self.history_open {
            self.history_dialog(context);
        }
        self.compaction_dialog(context);
        if self.logs_open {
            self.logs_dialog(context);
        }
        if self.activities_open {
            self.activities_dialog(context);
        }
        if self.help_open {
            self.help_dialog(context);
        }
        self.overview_dialog(context);
        self.comparison_dialog(context);
        self.health_dialog(context);
        self.update_dialog(context);
        if self.error.is_some() {
            let mut open = true;
            let mut dismiss = false;
            egui::Window::new("작업 안내")
                .open(&mut open)
                .default_width(650.0)
                .show(context, |ui| {
                    egui::ScrollArea::vertical()
                        .max_height(400.0)
                        .show(ui, |ui| {
                            ui.label(self.error.as_deref().unwrap_or_default());
                        });
                    if ui.button("확인").clicked() {
                        dismiss = true;
                    }
                });
            if !open || dismiss {
                self.error = None;
            }
        }
    }
}

fn folder_display_name(path: &Path) -> String {
    path.file_name()
        .filter(|name| !name.is_empty())
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.display().to_string())
}

fn multi_state_label(state: &str) -> &str {
    match state {
        "waiting" => "대기",
        "scanning" | "scan" => "탐색 중",
        "complete" => "완료",
        "partial" => "일부 미완료",
        "cancelled" => "취소",
        "failed" => "실패",
        _ => "상태 확인 필요",
    }
}

fn multi_phase_label(phase: &str) -> &str {
    match phase {
        "scan_start" | "scan" => "파일 탐색",
        "duplicates_sample" => "대형 파일 샘플 확인",
        "duplicates_hash" => "전체 SHA-256 계산",
        "duplicates_verify" => "실제 바이트 확인",
        "duplicates" => "완전 중복 확인",
        _ => "분석 진행",
    }
}

fn score_text(score: Option<f32>) -> String {
    score
        .map(|score| format!("{:.0}%", score * 100.0))
        .unwrap_or_else(|| "미적용".into())
}

fn compare_file(
    path: &Path,
    cancel: &AtomicBool,
    progress: &dyn Fn(Progress),
) -> AppResult<FileComparison> {
    let mut file = core::safe_open(path)?;
    let snapshot = core::snapshot_open_file(&file)?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0; 1024 * 1024];
    let mut completed = 0;
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("파일 비교를 취소했습니다.".into());
        }
        let count = file.read(&mut buffer).map_err(|error| error.to_string())?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
        completed += count as u64;
        progress(Progress {
            phase: "compare".into(),
            completed,
            total: Some(snapshot.size),
            message: path.display().to_string(),
        });
    }
    if completed != snapshot.size
        || core::snapshot_open_file(&file)? != snapshot
        || core::snapshot(path)? != snapshot
    {
        return Err(format!(
            "해시를 읽는 중 파일이 변경되었습니다: {}",
            path.display()
        ));
    }
    let sha256 = format!("{:x}", hasher.finalize());
    let metadata = fs::metadata(path).map_err(|error| error.to_string())?;
    let created = metadata
        .created()
        .ok()
        .map(|time| date_text(core::time_ns(time)))
        .unwrap_or_else(|| "제공되지 않음".into());
    let extension = path
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    let text = if [
        "txt", "md", "csv", "tsv", "json", "xml", "log", "rs", "py", "html", "css", "js", "yaml",
        "yml", "toml", "ini",
    ]
    .contains(&extension.as_str())
    {
        let mut bytes = vec![];
        core::safe_open(path)?
            .take(256 * 1024)
            .read_to_end(&mut bytes)
            .map_err(|error| error.to_string())?;
        std::str::from_utf8(&bytes)
            .ok()
            .filter(|value| !value.contains('\0'))
            .map(|value| {
                format!(
                    "{}{}",
                    value,
                    if snapshot.size > bytes.len() as u64 {
                        "\n\n[미리보기는 처음 256 KiB까지]"
                    } else {
                        ""
                    }
                )
            })
    } else {
        None
    };
    let image = if ["png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp"]
        .contains(&extension.as_str())
        && snapshot.size <= 32 * 1024 * 1024
    {
        let mut reader = image::ImageReader::new(std::io::BufReader::new(core::safe_open(path)?))
            .with_guessed_format()
            .map_err(|error| error.to_string())?;
        let mut limits = image::Limits::default();
        limits.max_image_width = Some(8192);
        limits.max_image_height = Some(8192);
        limits.max_alloc = Some(64 * 1024 * 1024);
        reader.limits(limits);
        reader.decode().ok().map(|image| {
            let rgba = image.thumbnail(1200, 1200).to_rgba8();
            ImagePreview {
                dimensions: [rgba.width() as usize, rgba.height() as usize],
                rgba: rgba.into_raw(),
            }
        })
    } else {
        None
    };
    if core::snapshot(path)? != snapshot {
        return Err(format!(
            "비교 자료를 읽는 중 파일이 변경되었습니다: {}",
            path.display()
        ));
    }
    Ok(FileComparison {
        path: path.to_path_buf(),
        size: snapshot.size,
        modified: date_text(snapshot.modified_ns),
        created,
        sha256,
        text,
        image,
    })
}

fn apply_theme(context: &egui::Context, dark: bool) {
    context.set_theme(if dark {
        egui::Theme::Dark
    } else {
        egui::Theme::Light
    });
    context.set_visuals(if dark {
        egui::Visuals::dark()
    } else {
        egui::Visuals::light()
    });
    let mut style = (*context.style()).clone();
    style.spacing.item_spacing = egui::vec2(10.0, 7.0);
    style.spacing.button_padding = egui::vec2(12.0, 7.0);
    style.visuals.selection.bg_fill = ACCENT.gamma_multiply(0.55);
    context.set_style(style);
}
fn install_fonts(context: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    let mut candidates = vec![
        PathBuf::from("C:/Windows/Fonts/malgun.ttf"),
        PathBuf::from("C:/Windows/Fonts/gulim.ttc"),
        PathBuf::from("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        PathBuf::from("/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf"),
        PathBuf::from("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    ];
    if let Some(windows) = std::env::var_os("WINDIR") {
        candidates.insert(0, PathBuf::from(windows).join("Fonts/malgun.ttf"));
    }
    for path in candidates {
        if let Ok(bytes) = fs::read(path) {
            if bytes.len() > 32 * 1024 * 1024 {
                continue;
            }
            fonts
                .font_data
                .insert("korean".into(), Arc::new(egui::FontData::from_owned(bytes)));
            for family in [egui::FontFamily::Proportional, egui::FontFamily::Monospace] {
                fonts
                    .families
                    .entry(family)
                    .or_default()
                    .insert(0, "korean".into());
            }
            break;
        }
    }
    context.set_fonts(fonts);
}

impl eframe::App for OrganizerApp {
    fn update(&mut self, context: &egui::Context, _frame: &mut eframe::Frame) {
        self.poll();
        if context.input(|input| input.key_pressed(egui::Key::F1)) {
            self.help_open = true;
        }
        if context.input(|input| input.viewport().close_requested()) {
            self.quit_requested = true;
        }
        if self.quit_requested && self.busy.is_some() {
            if let Some(busy) = &self.busy {
                busy.cancel.store(true, Ordering::Relaxed);
            }
            context.send_viewport_cmd(egui::ViewportCommand::CancelClose);
            self.status = "안전한 중단 지점과 작업 기록 저장을 기다린 뒤 종료합니다.".into();
        } else if self.quit_requested {
            if let Some(plan) = self.prepared.take() {
                if let Err(error) = updater::start_update_helper(&plan) {
                    self.quit_requested = false;
                    context.send_viewport_cmd(egui::ViewportCommand::CancelClose);
                    self.error = Some(format!(
                        "업데이트 도우미를 시작하지 못했습니다. 앱을 유지합니다.\n{error}"
                    ));
                }
            }
            if self.quit_requested {
                if self.multi_smoke.is_none() {
                    save_settings(&self.settings);
                }
                context.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        }
        if self.settings.automatic_updates
            && self.busy.is_none()
            && self.prepared.is_none()
            && self.update.is_none()
            && self.smoke_frames.is_none()
            && self.multi_smoke.is_none()
            && Instant::now() >= self.next_update_check
        {
            self.check_update(context, false);
        }
        self.header(context);
        self.footer(context);
        self.workspace(context);
        self.dialogs(context);
        self.advance_multiroot_smoke(context);
        if let Some(frames) = &mut self.smoke_frames {
            *frames += 1;
            // update callbacks after the first are issued only after the native
            // event loop has presented previous frames; never bypass creation.
            if *frames >= 8 {
                context.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            context.request_repaint_after(Duration::from_millis(100));
        } else if self.busy.is_some() || self.quit_requested || self.multi_smoke.is_some() {
            context.request_repaint_after(Duration::from_millis(80));
        } else {
            context.request_repaint_after(Duration::from_secs(1));
        }
    }
    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        if self.multi_smoke.is_none() {
            save_settings(&self.settings);
        }
    }
}

fn desktop_renderer_options() -> (eframe::Renderer, eframe::egui_wgpu::WgpuConfiguration) {
    if !cfg!(windows) {
        return (eframe::Renderer::Glow, Default::default());
    }

    use eframe::{egui_wgpu, wgpu};
    let mut setup = egui_wgpu::WgpuSetupCreateNew::default();
    // RDP and GPU-less Windows machines can expose only OpenGL 1.1. DX12
    // enumerates WARP as a CPU adapter, so retain it after compatible GPUs.
    setup.instance_descriptor.backends = wgpu::Backends::DX12;
    // FXC is supplied by Windows; the portable ZIP needs no DXC sidecar DLLs.
    setup
        .instance_descriptor
        .backend_options
        .dx12
        .shader_compiler = wgpu::Dx12Compiler::Fxc;
    let device_descriptor = setup.device_descriptor.clone();
    setup.native_adapter_selector = Some(Arc::new(move |adapters, surface| {
        let chosen = adapters
            .iter()
            .filter(|adapter| {
                let required = device_descriptor(adapter);
                surface.is_none_or(|surface| adapter.is_surface_supported(surface))
                    && required.required_limits.check_limits(&adapter.limits())
                    && adapter.features().contains(required.required_features)
            })
            .min_by_key(|adapter| match adapter.get_info().device_type {
                wgpu::DeviceType::DiscreteGpu => 0,
                wgpu::DeviceType::IntegratedGpu => 1,
                wgpu::DeviceType::VirtualGpu => 2,
                wgpu::DeviceType::Other => 3,
                wgpu::DeviceType::Cpu => 4,
            })
            .cloned()
            .ok_or_else(|| {
                "DirectX 12 화면을 표시할 수 있는 GPU 또는 Windows WARP 소프트웨어 어댑터가 없습니다. Windows 그래픽 드라이버와 원격 세션 설정을 확인하세요.".to_owned()
            })?;
        let info = chosen.get_info();
        eprintln!(
            "Folder Organizer Rust renderer: Wgpu/DX12; adapter: {} ({:?}); software fallback: {}",
            info.name,
            info.device_type,
            info.device_type == wgpu::DeviceType::Cpu
        );
        Ok(chosen)
    }));
    (
        eframe::Renderer::Wgpu,
        egui_wgpu::WgpuConfiguration {
            wgpu_setup: setup.into(),
            ..Default::default()
        },
    )
}

pub fn run() -> eframe::Result<()> {
    let (renderer, wgpu_options) = desktop_renderer_options();
    let options = eframe::NativeOptions {
        renderer,
        wgpu_options,
        viewport: egui::ViewportBuilder::default()
            .with_title(format!("Folder Organizer Rust {VERSION}"))
            .with_inner_size([1250.0, 820.0])
            .with_min_inner_size([900.0, 600.0]),
        ..Default::default()
    };
    eframe::run_native(
        "Folder Organizer Rust",
        options,
        Box::new(|context| Ok(Box::new(OrganizerApp::new(context)))),
    )
}

#[cfg(test)]
mod v090_workspace_tests {
    use super::*;

    fn app() -> (OrganizerApp, egui::Context) {
        let context = egui::Context::default();
        let mut app = OrganizerApp::new(&eframe::CreationContext::_new_kittest(context.clone()));
        app.settings = Settings::default();
        app.settings.automatic_updates = false;
        app.multi_folders.clear();
        apply_theme(&context, true);
        (app, context)
    }
    fn snapshot_report() -> multiroot::MultiAnalysis {
        let files = [
            "/snapshot-only/자료/report.txt",
            "/snapshot-only/백업/report.txt",
        ]
        .iter()
        .map(|path| multiroot::MultiFile {
            path: PathBuf::from(path),
            root_ids: vec!["root-1".into()],
            owner_root_id: "root-1".into(),
            relative_paths: BTreeMap::new(),
            size: 42,
            kind: "file".into(),
            extension: ".txt".into(),
            modified_ns: "1730000000000000000".into(),
            created_ns: None,
        })
        .collect::<Vec<_>>();
        multiroot::MultiAnalysis {
            schema: "fixture-v090-ui".into(),
            app_version: VERSION.into(),
            action_authority: false,
            complete: true,
            inventory_complete: true,
            cancelled: false,
            roots: vec![],
            notes: vec![],
            duplicate_inputs: vec![],
            duplicates: multiroot::MultiDuplicates {
                requested: true,
                complete: Some(true),
                cancelled: false,
                warnings: vec![],
                warning_count: 0,
                warnings_truncated: false,
                metrics: Default::default(),
                groups: vec![multiroot::ReadOnlyGroup {
                    paths: files.iter().map(|file| file.path.clone()).collect(),
                    size: 42,
                    sha256: "abc123".into(),
                    root_ids: vec!["root-1".into()],
                    group_source_count: 1,
                    action_authority: false,
                }],
            },
            files,
            timings: multiroot::Timings {
                elapsed_seconds: 0.1,
                scan_seconds: 0.05,
                duplicate_seconds: 0.05,
            },
            scheduling: multiroot::Scheduling {
                max_workers: 2,
                policy: "fixture".into(),
            },
        }
    }
    fn draw(app: &mut OrganizerApp, context: &egui::Context) -> String {
        let output = context.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(1250.0, 820.0),
                )),
                ..Default::default()
            },
            |context| {
                app.header(context);
                app.footer(context);
                app.workspace(context);
                app.dialogs(context);
            },
        );
        fn collect(shape: &egui::epaint::Shape, text: &mut String) {
            match shape {
                egui::epaint::Shape::Text(shape) => {
                    text.push_str(shape.galley.text());
                    text.push('\n');
                }
                egui::epaint::Shape::Vec(shapes) => {
                    for shape in shapes {
                        collect(shape, text);
                    }
                }
                _ => {}
            }
        }
        let mut text = String::new();
        for shape in output.shapes {
            collect(&shape.shape, &mut text);
        }
        text
    }
    #[test]
    fn default_main_workspace_embeds_search_results_and_properties_in_dark_mode() {
        let (mut app, context) = app();
        assert!(app.multi_open);
        let text = draw(&mut app, &context);
        for title in [
            "분석할 폴더",
            "파일 정보",
            "여러 폴더를 한눈에 비교하세요",
            "분석 시작",
            "통합 분석 · 읽기 전용",
        ] {
            assert!(
                text.contains(title),
                "main workspace is missing {title}: {text}"
            );
        }
        assert_eq!(
            context.options(|options| options.theme_preference),
            egui::ThemePreference::Dark
        );
        assert!(app.scan.is_none());
        assert!(app.require_checked(1).is_none());
    }
    #[test]
    fn main_scope_route_clears_single_root_authority_and_pending_selection() {
        let (mut app, _) = app();
        app.multi_open = false;
        app.root = Some(PathBuf::from("/previous-folder"));
        app.scan = Some(Arc::new(ScanResult {
            root: app.root.clone().unwrap(),
            files: vec![],
            warnings: vec![],
            cancelled: false,
            metrics: Default::default(),
        }));
        app.groups_actionable = true;
        app.selected
            .insert(PathBuf::from("/previous-folder/old.txt"));
        app.rules_open = true;
        app.history_open = true;
        app.enter_workspace();
        assert!(app.multi_open);
        assert!(app.root.is_none() && app.scan.is_none() && app.selected.is_empty());
        assert!(!app.groups_actionable && !app.rules_open && !app.history_open);
        assert!(app.require_checked(1).is_none());
    }
    #[test]
    fn busy_single_root_job_cannot_switch_into_global_workspace() {
        let (mut app, _) = app();
        app.multi_open = false;
        app.root = Some(PathBuf::from("/active-folder"));
        app.busy = Some(Busy {
            name: "fixture-only".into(),
            started: Instant::now(),
            cancel: Arc::new(AtomicBool::new(false)),
        });
        app.enter_workspace();
        assert!(!app.multi_open);
        assert_eq!(app.root, Some(PathBuf::from("/active-folder")));
    }
    #[test]
    fn snapshot_only_inspector_and_cached_filter_render_without_reopening_files() {
        let (mut app, context) = app();
        app.finish(ResultData::Multi(snapshot_report()));
        assert_eq!(app.workspace_index.len(), 2);
        assert_eq!(app.workspace_groups.len(), 2);
        assert_eq!(app.workspace_file, Some(0));
        let text = draw(&mut app, &context);
        assert!(text.contains("report.txt") && text.contains("42 바이트"));
        assert!(text.contains("abc123"));
        assert!(text.contains("제공되지 않음"));
        app.workspace_query = "백업".into();
        app.filter_workspace_rows();
        assert_eq!(app.workspace_visible, vec![1]);
        let mut partial = snapshot_report();
        partial.complete = false;
        partial.cancelled = true;
        app.finish(ResultData::Multi(partial));
        assert!(draw(&mut app, &context).contains("중지된 부분 결과"));
        assert!(!app.groups_actionable && app.scan.is_none());
    }
}

#[cfg(test)]
mod next_workspace_ui_tests {
    use super::*;

    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let mut id = [0u8; 12];
            getrandom::getrandom(&mut id).unwrap();
            let name: String = id.iter().map(|value| format!("{value:02x}")).collect();
            let path = std::env::temp_dir().join(format!("organizer-next-ui-{name}"));
            fs::create_dir(&path).unwrap();
            Self(path.canonicalize().unwrap())
        }
        fn file(&self, relative: &str, content: &[u8]) -> PathBuf {
            let path = self.0.join(relative);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, content).unwrap();
            path
        }
        fn roots(&self) -> Vec<PathBuf> {
            ["문서", "백업"]
                .iter()
                .map(|name| {
                    let root = self.0.join(name);
                    fs::create_dir_all(&root).unwrap();
                    root
                })
                .collect()
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn app() -> (OrganizerApp, egui::Context) {
        let context = egui::Context::default();
        let mut app = OrganizerApp::new(&eframe::CreationContext::_new_kittest(context.clone()));
        app.settings = Settings::default();
        app.settings.automatic_updates = false;
        app.multi_folders.clear();
        apply_theme(&context, true);
        (app, context)
    }
    fn analyze(roots: &[PathBuf]) -> multiroot::WorkspaceSession {
        multiroot::analyze_workspace(
            roots,
            &ScanOptions::default(),
            true,
            2,
            &AtomicBool::new(false),
            None,
        )
        .unwrap()
    }
    fn draw(app: &mut OrganizerApp, context: &egui::Context) -> String {
        let output = context.run(
            egui::RawInput {
                screen_rect: Some(egui::Rect::from_min_size(
                    egui::Pos2::ZERO,
                    egui::vec2(1380.0, 900.0),
                )),
                ..Default::default()
            },
            |context| {
                app.header(context);
                app.footer(context);
                app.workspace(context);
                app.dialogs(context);
            },
        );
        fn collect(shape: &egui::epaint::Shape, text: &mut String) {
            match shape {
                egui::epaint::Shape::Text(shape) => {
                    text.push_str(shape.galley.text());
                    text.push('\n');
                }
                egui::epaint::Shape::Vec(shapes) => {
                    for shape in shapes {
                        collect(shape, text);
                    }
                }
                _ => {}
            }
        }
        let mut text = String::new();
        for shape in output.shapes {
            collect(&shape.shape, &mut text);
        }
        text
    }
    fn settle(app: &mut OrganizerApp) {
        let deadline = Instant::now() + Duration::from_secs(15);
        while app.busy.is_some() && Instant::now() < deadline {
            app.poll();
            std::thread::sleep(Duration::from_millis(5));
        }
        assert!(app.busy.is_none(), "new workspace worker did not finish");
        assert!(app.error.is_none(), "{:?}", app.error);
    }

    #[test]
    fn one_or_many_roots_finish_in_the_same_action_capable_workspace() {
        let fixture = Fixture::new();
        let roots = fixture.roots();
        fixture.file("문서/original.txt", b"document bytes");
        fixture.file("백업/copy.txt", b"document bytes");
        let (mut app, _) = app();
        for selected_roots in [&roots[..1], &roots[..]] {
            app.finish(ResultData::Workspace(analyze(selected_roots)));
            assert!(app.multi_open && app.workspace_actionable());
            assert!(
                app.scan.is_none(),
                "a synthetic single scan must not be manufactured"
            );
            assert_eq!(
                app.multi_result.as_ref().unwrap().roots.len(),
                selected_roots.len()
            );
            app.select_visible_workspace(true);
            assert_eq!(app.checked_paths().len(), selected_roots.len());
            app.selected.clear();
        }
    }

    #[test]
    fn ribbon_checkboxes_snapshot_inspector_and_cached_type_filter_share_the_results() {
        let fixture = Fixture::new();
        let roots = fixture.roots();
        let first = fixture.file("문서/original.txt", b"document bytes");
        fixture.file("백업/copy.txt", b"document bytes");
        fixture.file("문서/data.bin", b"binary");
        let (mut app, context) = app();
        app.finish(ResultData::Workspace(analyze(&roots)));
        let text_kind = app
            .workspace_types
            .iter()
            .find(|row| row.0.ends_with("txt"))
            .unwrap()
            .clone();
        assert_eq!((text_kind.1, text_kind.3), (2, 1));
        assert_eq!(text_kind.4, b"document bytes".len() as u64);
        app.select_visible_workspace(true);
        app.workspace_extension = Some(text_kind.0);
        app.filter_workspace_rows();
        assert_eq!(app.workspace_visible.len(), 2);
        assert_eq!(
            app.checked_paths().len(),
            3,
            "display filtering must retain hidden checks"
        );
        app.workspace_file = app.workspace_index.get(&first).copied();
        fs::remove_file(&first).unwrap();
        // The render consumes captured metadata even when the source is offline.
        let text = draw(&mut app, &context);
        for title in [
            "검색 폴더",
            "검색 필터",
            "파일 정보",
            "파일 종류별 요약",
            "분석 결과",
            "original.txt",
        ] {
            assert!(
                text.contains(title),
                "new workspace is missing {title}: {text}"
            );
        }
        assert!(!text.contains("이 폴더에서 정리"));
        assert!(!text.contains("통합 결과는 읽기 전용"));
    }

    #[test]
    fn workspace_receipts_update_the_inventory_without_rescan_and_disable_stale_actions() {
        let fixture = Fixture::new();
        let roots = fixture.roots();
        let keeper = fixture.file("문서/original.txt", b"document bytes");
        let copy = fixture.file("백업/copy.txt", b"document bytes");
        let (mut app, _) = app();
        app.finish(ResultData::Workspace(analyze(&roots)));
        app.selected.insert(copy.clone());
        let session = app.workspace_session.clone().unwrap();
        let plan =
            workspace_actions::plan_transfer(&session, &[copy.clone()], None, TransferKind::Delete)
                .unwrap();
        let receipt = workspace_actions::execute(&plan, &AtomicBool::new(false), None).unwrap();
        app.finish(ResultData::BatchReceipt(receipt));
        assert_eq!(app.multi_result.as_ref().unwrap().files.len(), 1);
        assert_eq!(app.multi_result.as_ref().unwrap().files[0].path, keeper);
        assert!(!app.selected.contains(&copy));
        assert!(app.duplicates.is_empty() && app.multi_rows.is_empty());
        assert!(!app.workspace_actionable() && app.workspace_stale);
        let report = app.multi_result.as_ref().unwrap();
        assert!(!report.complete && !report.inventory_complete);
        assert_eq!(report.duplicates.complete, Some(false));
        assert!(report.roots.iter().all(|root| root.state == "partial"));
        let exported = serde_json::to_value(report.as_ref()).unwrap();
        assert_eq!(exported["complete"], false);
        assert_eq!(exported["action_authority"], false);
        assert_eq!(
            app.workspace_types.iter().map(|row| row.1).sum::<usize>(),
            1
        );
    }

    #[test]
    fn quick_classification_presets_preserve_custom_rules_and_do_not_duplicate_the_preset() {
        let (mut app, context) = app();
        let original = rules::Rule {
            name: "내 문서 규칙".into(),
            target: "문서/{규칙}".into(),
            ..Default::default()
        };
        app.settings.rules = vec![original.clone()];
        app.prepare_date_or_extension(&context, true);
        assert_eq!(app.settings.rules.len(), 2);
        assert_eq!(app.settings.rules[1].name, original.name);
        assert_eq!(app.settings.rules[1].target, original.target);
        assert_eq!(app.settings.rules[0].target, "{수정일:%Y}/{수정일:%m}");
        app.prepare_date_or_extension(&context, true);
        assert_eq!(app.settings.rules.len(), 2);
        assert_eq!(app.settings.rules[1].name, original.name);
    }

    #[test]
    fn rule_preview_routes_the_current_workspace_to_all_real_root_plans() {
        let fixture = Fixture::new();
        let roots = fixture.roots();
        fixture.file("문서/a.txt", b"A");
        fixture.file("백업/b.txt", b"B");
        let target = fixture.0.join("classified");
        fs::create_dir(&target).unwrap();
        let (mut app, context) = app();
        app.finish(ResultData::Workspace(analyze(&roots)));
        app.settings.rules = vec![rules::Rule {
            name: "확장자".into(),
            target: "{확장자}".into(),
            ..Default::default()
        }];
        app.rules_destination = target.display().to_string();
        app.rule_copy = true;
        app.prepare_rule_preview(&context);
        settle(&mut app);
        let plan = app
            .batch_plan
            .as_ref()
            .expect("global rule preview must produce a batch plan");
        assert_eq!(plan.item_count(), 2);
        assert_eq!(plan.plans().len(), 2);
        assert!(plan.plans().iter().all(|plan| roots.contains(&plan.root)));
        assert!(app.scan.is_none());
        app.enter_workspace();
        assert!(app.batch_plan.is_none() && !app.workspace_actionable());
    }
}
