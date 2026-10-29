//! Native desktop presentation. Filesystem work never runs in the paint loop.
use crate::core::{self, AppResult, DuplicateGroup, Progress, ScanOptions, ScanResult};
use crate::operations::{
    self, HistoryRecord, LogRecord, OperationPlan, OperationReceipt, TransferKind,
};
use crate::{merging, recovery, reporting, rules, similarity, updater};
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
    ("자동 업데이트", "Rust 배포판은 Rust ZIP·체크섬·버전 표식을 확인합니다. Python 배포판과 섞어 설치하지 않습니다. 업데이트는 파일 작업을 마친 뒤 앱이 종료된 후 별도 도우미가 적용합니다. 소스 실행에서는 지원하지 않습니다."),
];

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Settings {
    dark: bool,
    recent: Vec<PathBuf>,
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
    Scan(ScanResult, ScanOptions),
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
    Message(String),
}
enum WorkerEvent {
    Progress(Progress),
    Finished(AppResult<ResultData>),
}
struct Busy {
    name: String,
    started: Instant,
    cancel: Arc<AtomicBool>,
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
}

impl OrganizerApp {
    pub fn new(context: &eframe::CreationContext<'_>) -> Self {
        install_fonts(&context.egui_ctx);
        let settings = load_settings();
        apply_theme(&context.egui_ctx, settings.dark);
        let scan_excludes = settings.scan.exclude_patterns.join("\n");
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
            status: "폴더를 선택하면 파일을 읽기 전용으로 분석합니다.".into(),
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
            smoke_frames: (std::env::args().any(|arg| arg == "--gui-smoke")
                || std::env::var("FOLDER_ORGANIZER_RUST_SMOKE").is_ok_and(|value| value == "1"))
            .then_some(0),
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
            let callback = |progress| {
                let _ = sender.send(WorkerEvent::Progress(progress));
                context.request_repaint();
            };
            let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| operation(&cancel, &callback)))
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
            ResultData::Scan(scan, options) => {
                self.scan_options = options;
                self.root = Some(scan.root.clone());
                self.settings.recent.retain(|path| path != &scan.root);
                self.settings.recent.insert(0, scan.root.clone());
                self.settings.recent.truncate(10);
                save_settings(&self.settings);
                self.status = format!(
                    "{}개 파일 · {} · 경고 {}개{}",
                    scan.files.len(),
                    bytes_text(scan.total_size()),
                    scan.warnings.len(),
                    if scan.cancelled {
                        " · 취소된 부분 결과"
                    } else {
                        ""
                    }
                );
                self.scan = Some(Arc::new(scan));
                self.selected.clear();
                self.duplicates.clear();
                self.duplicate_details = None;
                self.similar = None;
                self.groups_actionable = false;
                self.view = View::Files;
                self.rebuild_rows();
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
        let options = self.settings.scan.clone();
        self.root = Some(root.clone());
        self.start(context, "폴더 분석", move |cancel, progress| {
            core::scan(&root, &options, cancel, Some(progress))
                .map(|scan| ResultData::Scan(scan, options))
        });
    }
    fn checked_paths(&self) -> Vec<PathBuf> {
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
        let Some(scan) = self.scan.clone() else {
            self.error = Some("먼저 폴더를 분석하세요.".into());
            return;
        };
        self.start(context, "완전 중복 검사", move |cancel, progress| {
            core::analyze_duplicates(&scan, cancel, Some(progress)).map(ResultData::Duplicates)
        });
    }
    fn analyze_similar(&mut self, context: &egui::Context) {
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
        if let Some(root) = self.root.clone() {
            self.start(context, "작업 기록 읽기", move |_, _| {
                operations::history(&root).map(ResultData::History)
            });
        } else {
            self.error = Some("먼저 기록이 있는 폴더를 선택하세요.".into());
        }
    }
    fn load_logs(&mut self, context: &egui::Context) {
        if let Some(root) = self.root.clone() {
            self.start(context, "실행 로그 읽기", move |_, _| {
                operations::logs(&root).map(ResultData::Logs)
            });
        } else {
            self.error = Some("먼저 기록이 있는 폴더를 선택하세요.".into());
        }
    }
    fn select_retention(&mut self) {
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
        let Some(scan) = self.scan.clone() else {
            return;
        };
        self.start(context, "용량·정리 요약", move |_, _| {
            let mut kinds: BTreeMap<String, (u64, usize)> = BTreeMap::new();
            let mut folders: BTreeMap<String, (u64, usize)> = BTreeMap::new();
            let mut largest = std::collections::BinaryHeap::new();
            let mut result = Overview::default();
            let year_ago = time_now().saturating_sub(365 * 24 * 3600) as i128 * 1_000_000_000;
            for file in &scan.files {
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
            ui.add_space(5.0);
            ui.horizontal(|ui| {
                ui.label(RichText::new("Folder Organizer").size(22.0).strong());
                ui.label(RichText::new(format!("Rust · {VERSION}")).color(ACCENT));
            });
            egui::menu::bar(ui, |ui| {
                ui.menu_button("파일", |ui| {
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("폴더 선택…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.pick_folder(context);
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
                    ui.menu_button("분석 보고서 내보내기", |ui| {
                        for (format, label) in [
                            ("csv", "CSV 파일 목록…"),
                            ("json", "JSON 판정 근거…"),
                            ("html", "HTML 읽기 보고서…"),
                        ] {
                            if ui
                                .add_enabled(
                                    self.scan.is_some() && self.busy.is_none(),
                                    egui::Button::new(label),
                                )
                                .clicked()
                            {
                                ui.close_menu();
                                self.export(context, format);
                            }
                        }
                    });
                    ui.separator();
                    if ui.button("종료").clicked() {
                        ui.close_menu();
                        self.quit_requested = true;
                    }
                });
                ui.menu_button("검사", |ui| {
                    let available = self.scan.is_some() && self.busy.is_none();
                    if ui
                        .add_enabled(
                            self.root.is_some() && self.busy.is_none(),
                            egui::Button::new("폴더 다시 분석"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        if let Some(path) = self.root.clone() {
                            self.start_scan(context, path);
                        }
                    }
                    if ui
                        .add_enabled(available, egui::Button::new("완전 중복 검사"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.analyze_duplicates(context);
                    }
                    if ui
                        .add_enabled(available, egui::Button::new("유사 파일 비교"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.analyze_similar(context);
                    }
                    if ui
                        .add_enabled(available, egui::Button::new("용량·정리 요약"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.show_overview(context);
                    }
                    ui.separator();
                    if ui.button("검사 범위 · 보존 설정…").clicked() {
                        self.settings_open = true;
                        ui.close_menu();
                    }
                });
                ui.menu_button("정리", |ui| {
                    let available = self.scan.is_some() && self.busy.is_none();
                    if ui
                        .add_enabled(
                            available && self.groups_actionable && !self.duplicates.is_empty(),
                            egui::Button::new("첫 번째 파일만 남기고 나머지 삭제"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.keep_first(context);
                    }
                    if ui
                        .add_enabled(
                            available && !self.duplicates.is_empty(),
                            egui::Button::new("보존 규칙으로 사본 선택"),
                        )
                        .clicked()
                    {
                        self.select_retention();
                        ui.close_menu();
                    }
                    ui.separator();
                    if ui
                        .add_enabled(available, egui::Button::new("규칙으로 자동 분류…"))
                        .clicked()
                    {
                        self.rules_open = true;
                        ui.close_menu();
                    }
                    if ui
                        .add_enabled(
                            available && !self.selected.is_empty(),
                            egui::Button::new("체크한 파일 복사…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.prepare_transfer(context, TransferKind::Copy);
                    }
                    if ui
                        .add_enabled(
                            available && !self.selected.is_empty(),
                            egui::Button::new("체크한 파일 이동…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.prepare_transfer(context, TransferKind::Move);
                    }
                    if ui
                        .add_enabled(
                            available && self.selected.len() >= 2,
                            egui::Button::new("체크한 파일 내용 병합…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.prepare_merge(context);
                    }
                    ui.separator();
                    if ui
                        .add_enabled(
                            available && !self.selected.is_empty(),
                            egui::Button::new("선택 항목을 중복으로 간주…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.prepare_transfer(context, TransferKind::Delete);
                    }
                });
                ui.menu_button("기록", |ui| {
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("작업 기록 · 복원…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.load_history(context);
                    }
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("실행 로그…"))
                        .clicked()
                    {
                        ui.close_menu();
                        self.load_logs(context);
                    }
                    if ui.button("앱 활동 · 진단…").clicked() {
                        self.activities_open = true;
                        ui.close_menu();
                    }
                });
                ui.menu_button("보기", |ui| {
                    if ui
                        .selectable_label(self.settings.dark, "다크 모드 · 기본")
                        .clicked()
                    {
                        self.settings.dark = true;
                        apply_theme(context, true);
                        save_settings(&self.settings);
                    }
                    if ui
                        .selectable_label(!self.settings.dark, "라이트 모드")
                        .clicked()
                    {
                        self.settings.dark = false;
                        apply_theme(context, false);
                        save_settings(&self.settings);
                    }
                    ui.separator();
                    if ui.button("보이는 파일 모두 체크").clicked() {
                        if let Some(scan) = &self.scan {
                            for index in &self.visible {
                                self.selected.insert(scan.files[*index].path.clone());
                            }
                        }
                        self.selection_bytes_dirty = true;
                    }
                    if ui.button("전체 선택 해제").clicked() {
                        self.selected.clear();
                        self.selection_bytes_dirty = true;
                    }
                    if ui.button("선택한 두 파일 나란히 비교").clicked() {
                        let paths = self.checked_paths();
                        ui.close_menu();
                        self.compare_paths(context, paths);
                    }
                });
                ui.menu_button("도구", |ui| {
                    if ui
                        .add_enabled(
                            self.busy.is_none() && !self.selected.is_empty(),
                            egui::Button::new("체크한 파일 건강 검사…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.check_health(context);
                    }
                    if ui
                        .add_enabled(
                            self.busy.is_none() && !self.selected.is_empty(),
                            egui::Button::new("원본 유지 · 새 복구본 만들기…"),
                        )
                        .clicked()
                    {
                        ui.close_menu();
                        self.repair(context);
                    }
                });
                ui.menu_button("도움말", |ui| {
                    if ui.button("기능 사용 안내 · F1").clicked() {
                        self.help_open = true;
                        ui.close_menu();
                    }
                    if ui
                        .add_enabled(self.busy.is_none(), egui::Button::new("Rust 업데이트 확인"))
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
                    ui.separator();
                    ui.label(format!("Rust 네이티브 · {VERSION}"));
                    ui.label("개인 파일은 로컬에서 처리합니다.");
                });
            });
            ui.add_space(4.0);
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
            if ui.add_enabled(self.busy.is_none() && self.scan.is_some() && !self.settings.rules.is_empty(), egui::Button::new("조건을 적용하고 전체 경로 미리보기").fill(ACCENT)).clicked() { preview = true; }
        });
        self.rules_open = open;
        if changed {
            save_settings(&self.settings);
        }
        if preview {
            let Some(scan) = self.scan.clone() else {
                return;
            };
            let destination = PathBuf::from(&self.rules_destination);
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
    }
    fn plan_dialog(&mut self, context: &egui::Context) {
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
            if ui.add_enabled(self.busy.is_none(), egui::Button::new("위 내용으로 새 병합본 만들기").fill(ACCENT)).clicked() { execute = true; }
        });
        if execute {
            let preview = self.merge.take().unwrap();
            let archive = self.archive_merge;
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
                save_settings(&self.settings);
                context.send_viewport_cmd(egui::ViewportCommand::Close);
            }
        }
        if self.settings.automatic_updates
            && self.busy.is_none()
            && self.prepared.is_none()
            && self.update.is_none()
            && self.smoke_frames.is_none()
            && Instant::now() >= self.next_update_check
        {
            self.check_update(context, false);
        }
        self.header(context);
        self.footer(context);
        self.workspace(context);
        self.dialogs(context);
        if let Some(frames) = &mut self.smoke_frames {
            *frames += 1;
            // update callbacks after the first are issued only after the native
            // event loop has presented previous frames; never bypass creation.
            if *frames >= 8 {
                context.send_viewport_cmd(egui::ViewportCommand::Close);
            }
            context.request_repaint_after(Duration::from_millis(100));
        } else if self.busy.is_some() || self.quit_requested {
            context.request_repaint_after(Duration::from_millis(80));
        } else {
            context.request_repaint_after(Duration::from_secs(1));
        }
    }
    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        save_settings(&self.settings);
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
