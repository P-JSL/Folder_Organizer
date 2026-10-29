//! Ordered, read-only classification. All mutations use the journalled executor.
use crate::core::{AppResult, FileEntry, ScanResult};
use chrono::{DateTime, Datelike, Local, NaiveDate, TimeZone, Timelike};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

pub const GUIDELINES: &str = "규칙은 위에서 아래로 첫 일치 하나만 적용합니다. 처음에는 복사로 시험하세요. {규칙}/{종류}/{수정일:%Y/%m}처럼 폴더를 매핑합니다. 생성일이 제공되지 않는 파일은 생성일 규칙에서 건너뜁니다. 같은 이름은 덮어쓰지 않으며 미리보기 후 실행합니다.";

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct Rule {
    pub enabled: bool,
    pub name: String,
    pub target: String,
    pub name_pattern: String,
    pub extensions: Vec<String>,
    pub source_folder: String,
    pub min_size: u64,
    pub max_size: Option<u64>,
    pub date_basis: String,
    pub start_date: Option<String>,
    pub end_date: Option<String>,
    pub protected_folders: Vec<PathBuf>,
}
impl Default for Rule {
    fn default() -> Self {
        Self {
            enabled: true,
            name: "파일 종류별 분류".into(),
            target: "{type}/{source_folder}".into(),
            name_pattern: "*".into(),
            extensions: vec![],
            source_folder: String::new(),
            min_size: 0,
            max_size: None,
            date_basis: "modified".into(),
            start_date: None,
            end_date: None,
            protected_folders: vec![],
        }
    }
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClassificationItem {
    pub source: PathBuf,
    pub target: PathBuf,
    pub rule_name: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ClassificationPlan {
    pub root: PathBuf,
    pub destination: PathBuf,
    pub items: Vec<ClassificationItem>,
    pub warnings: Vec<String>,
}
impl ClassificationPlan {
    pub fn mappings(&self) -> Vec<(PathBuf, PathBuf)> {
        self.items
            .iter()
            .map(|i| (i.source.clone(), i.target.clone()))
            .collect()
    }
}

pub fn presets() -> Vec<(String, Rule)> {
    [
        ("type", "종류별 분류", "{type}/{source_folder}", "modified"),
        (
            "date",
            "수정 연월별 분류",
            "{year}/{month}/{source_folder}",
            "modified",
        ),
        (
            "daily",
            "수정 날짜별 분류",
            "{date}/{source_folder}",
            "modified",
        ),
        (
            "created_date",
            "생성 연월별 분류",
            "{created:%Y/%m}/{source_folder}",
            "created",
        ),
        (
            "quarter",
            "분기별 분류",
            "{year}/{quarter}분기/{source_folder}",
            "modified",
        ),
        (
            "folder",
            "기존 폴더 구조",
            "폴더별/{source_folder}",
            "modified",
        ),
        (
            "extension",
            "확장자별 분류",
            "{ext}/{source_folder}",
            "modified",
        ),
        (
            "rule",
            "규칙 이름별 분류",
            "{rule}/{type}/{source_folder}",
            "modified",
        ),
    ]
    .into_iter()
    .map(|(key, name, target, basis)| {
        (
            key.into(),
            Rule {
                name: name.into(),
                target: target.into(),
                date_basis: basis.into(),
                ..Rule::default()
            },
        )
    })
    .collect()
}

pub fn mapping_tokens() -> &'static [(&'static str, &'static str)] {
    &[
        ("{규칙}", "규칙 이름"),
        ("{종류}", "사진·문서 등 종류"),
        ("{확장자}", "점 없는 확장자"),
        ("{원래폴더}", "원래 하위 폴더"),
        ("{이름}", "확장자 없는 이름"),
        ("{연도}", "기준 연도"),
        ("{월}", "기준 월"),
        ("{날짜}", "기준 날짜"),
        ("{분기}", "기준 분기"),
        ("{수정일:%Y/%m}", "수정 연도와 월 폴더"),
        ("{생성일:%Y-%m-%d}", "실제 생성일"),
    ]
}

fn canonical(token: &str) -> String {
    for (prefix, basis) in [("수정", "modified"), ("생성", "created")] {
        if let Some(part) = token.strip_prefix(prefix) {
            let part = match part {
                "연도" => "year",
                "월" => "month",
                "날" => "day",
                "날짜" => "date",
                "요일" => "weekday",
                "분기" => "quarter",
                "주차" => "week",
                "시" => "hour",
                "분" => "minute",
                "초" => "second",
                _ => "",
            };
            if !part.is_empty() {
                return format!("{basis}_{part}");
            }
        }
    }
    match token {
        "종류" => "type",
        "확장자" => "ext",
        "원래폴더" => "source_folder",
        "규칙" => "rule",
        "이름" | "stem" => "name",
        "연도" | "년" => "year",
        "월" => "month",
        "일" => "day",
        "날짜" => "date",
        "요일" => "weekday",
        "분기" => "quarter",
        "주차" => "week",
        "시" => "hour",
        "분" => "minute",
        "초" => "second",
        "수정일" => "modified",
        "생성일" => "created",
        other => return other.to_owned(),
    }
    .to_owned()
}
fn safe_component(value: &str, fallback: &str) -> String {
    let cleaned: String = value
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .take(100)
        .collect();
    let clean = cleaned.trim_matches([' ', '.']);
    if clean.is_empty() {
        return fallback.to_owned();
    }
    let lower = clean.split('.').next().unwrap_or("").to_ascii_lowercase();
    if ["con", "prn", "aux", "nul"].contains(&lower.as_str())
        || (lower.len() == 4
            && (lower.starts_with("com") || lower.starts_with("lpt"))
            && matches!(lower.as_bytes()[3], b'1'..=b'9'))
        || clean.starts_with(".folder-organizer")
    {
        format!("_{clean}")
    } else {
        clean.into()
    }
}
fn relative_path(value: &str) -> AppResult<PathBuf> {
    if value.is_empty() || value.len() > 1024 || value.starts_with('/') || value.starts_with('\\') {
        return Err("폴더 매핑은 비어 있지 않은 상대 경로여야 합니다.".into());
    }
    let normalized = value.replace('\\', "/");
    let mut output = PathBuf::new();
    for part in normalized.split('/') {
        if part.is_empty()
            || part == "."
            || part == ".."
            || part
                .chars()
                .any(|c| c.is_control() || "<>:\"|?*".contains(c))
            || part.ends_with([' ', '.'])
            || part.starts_with(".folder-organizer")
            || ["delete_files", "#recycle", "@recycle", "$recycle.bin"]
                .contains(&part.to_ascii_lowercase().as_str())
        {
            return Err("안전하지 않거나 앱 보관소에 해당하는 폴더 매핑입니다.".into());
        }
        if safe_component(part, "") != part {
            return Err("Windows 예약 이름은 폴더에 사용할 수 없습니다.".into());
        }
        output.push(part);
    }
    Ok(output)
}
fn moment(entry: &FileEntry, basis: &str) -> AppResult<DateTime<Local>> {
    let ns = match basis {
        "modified" => entry.modified_ns,
        "created" => entry
            .created_ns
            .ok_or("생성일이 제공되지 않아 해당 규칙을 건너뜁니다.")?,
        _ => return Err("날짜 기준은 modified 또는 created입니다.".into()),
    };
    let sec =
        i64::try_from(ns.div_euclid(1_000_000_000)).map_err(|_| "날짜 범위를 초과했습니다.")?;
    Local
        .timestamp_opt(sec, ns.rem_euclid(1_000_000_000) as u32)
        .single()
        .ok_or_else(|| "날짜를 변환할 수 없습니다.".into())
}
fn format_date(date: DateTime<Local>, format: &str) -> AppResult<String> {
    let mut chars = format.chars();
    while let Some(ch) = chars.next() {
        if ch == '%'
            && !matches!(
                chars.next(),
                Some('Y' | 'm' | 'd' | 'H' | 'M' | 'S' | 'j' | 'u' | 'V' | 'G' | '%')
            )
        {
            return Err("날짜 형식은 %Y %m %d %H %M %S %j %u %V %G %%만 지원합니다.".into());
        }
    }
    Ok(date.format(format).to_string())
}
pub fn file_type(entry: &FileEntry) -> &'static str {
    match entry
        .extension
        .trim_start_matches('.')
        .to_ascii_lowercase()
        .as_str()
    {
        "jpg" | "jpeg" | "png" | "webp" | "gif" | "bmp" | "tif" | "tiff" | "heic" => "사진",
        "pdf" | "doc" | "docx" | "xls" | "xlsx" | "ppt" | "pptx" | "txt" | "md" | "csv" | "hwp"
        | "hwpx" | "odt" | "rtf" => "문서",
        "mp4" | "mov" | "mkv" | "avi" | "webm" | "wmv" => "동영상",
        "mp3" | "flac" | "wav" | "aac" | "m4a" | "ogg" => "음악",
        "zip" | "7z" | "rar" | "tar" | "gz" => "압축 파일",
        "exe" | "msi" | "msix" | "appx" | "dmg" | "pkg" | "deb" | "rpm" => "설치 파일",
        _ => "기타",
    }
}
pub fn render_target(rule: &Rule, entry: &FileEntry) -> AppResult<PathBuf> {
    if rule.target.is_empty() || rule.target.len() > 512 {
        return Err("폴더 템플릿은 1~512자여야 합니다.".into());
    }
    let mut result = String::new();
    let mut input = rule.target.chars().peekable();
    while let Some(ch) = input.next() {
        if ch == '}' {
            return Err("짝이 맞지 않는 } 기호입니다.".into());
        }
        if ch != '{' {
            result.push(ch);
            continue;
        }
        let mut token = String::new();
        let mut ended = false;
        for c in input.by_ref() {
            if c == '}' {
                ended = true;
                break;
            }
            if c == '{' {
                return Err("중첩된 폴더 기호는 지원하지 않습니다.".into());
            }
            token.push(c);
        }
        if !ended {
            return Err("짝이 맞지 않는 { 기호입니다.".into());
        }
        let (key, spec) = token.split_once(':').unwrap_or((&token, ""));
        let key = canonical(key);
        let value = match key.as_str() {
            "type" => file_type(entry).into(),
            "ext" => safe_component(entry.extension.trim_start_matches('.'), "확장자 없음"),
            "source_folder" => {
                let p = Path::new(&entry.relative_path)
                    .parent()
                    .unwrap_or(Path::new(""));
                if p.as_os_str().is_empty() {
                    "루트".into()
                } else {
                    p.to_string_lossy().replace('\\', "/")
                }
            }
            "rule" => safe_component(&rule.name, "규칙"),
            "name" => safe_component(
                Path::new(&entry.name)
                    .file_stem()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .as_ref(),
                "파일",
            ),
            _ => {
                let (basis, part) = if key == "modified" || key == "created" {
                    (key.as_str(), "date")
                } else if let Some(p) = key.strip_prefix("modified_") {
                    ("modified", p)
                } else if let Some(p) = key.strip_prefix("created_") {
                    ("created", p)
                } else {
                    (rule.date_basis.as_str(), key.as_str())
                };
                let date = moment(entry, basis)?;
                if !spec.is_empty() {
                    if !matches!(key.as_str(), "modified" | "created") {
                        return Err("형식 지정은 수정일·생성일에만 사용할 수 있습니다.".into());
                    }
                    format_date(date, spec)?
                } else {
                    match part {
                        "year" => format!("{:04}", date.year()),
                        "month" => format!("{:02}", date.month()),
                        "day" => format!("{:02}", date.day()),
                        "date" => date.format("%Y-%m-%d").to_string(),
                        "weekday" => ["월", "화", "수", "목", "금", "토", "일"]
                            [date.weekday().num_days_from_monday() as usize]
                            .into(),
                        "quarter" => ((date.month0() / 3) + 1).to_string(),
                        "week" => format!("{:02}", date.iso_week().week()),
                        "hour" => format!("{:02}", date.hour()),
                        "minute" => format!("{:02}", date.minute()),
                        "second" => format!("{:02}", date.second()),
                        _ => return Err(format!("지원하지 않는 기호입니다: {{{key}}}")),
                    }
                }
            }
        };
        if !spec.is_empty() && !matches!(key.as_str(), "modified" | "created") {
            return Err("날짜가 아닌 기호에 형식을 지정할 수 없습니다.".into());
        }
        result.push_str(&value);
    }
    relative_path(&result)
}

/// Linear-space wildcard matching; supports the documented * and ? subset.
pub fn wildcard_match(pattern: &str, value: &str) -> bool {
    let p: Vec<char> = pattern.to_lowercase().chars().collect();
    let v: Vec<char> = value.to_lowercase().chars().collect();
    let (mut pi, mut vi, mut star, mut retry) = (0, 0, None, 0);
    while vi < v.len() {
        if pi < p.len() && (p[pi] == '?' || p[pi] == v[vi]) {
            pi += 1;
            vi += 1;
        } else if pi < p.len() && p[pi] == '*' {
            star = Some(pi);
            pi += 1;
            retry = vi;
        } else if let Some(s) = star {
            retry += 1;
            vi = retry;
            pi = s + 1;
        } else {
            return false;
        }
    }
    while pi < p.len() && p[pi] == '*' {
        pi += 1;
    }
    pi == p.len()
}
fn date_condition(value: &Option<String>) -> AppResult<Option<NaiveDate>> {
    value
        .as_ref()
        .filter(|s| !s.trim().is_empty())
        .map(|s| {
            NaiveDate::parse_from_str(s, "%Y-%m-%d")
                .map_err(|_| "날짜 조건은 YYYY-MM-DD 형식입니다.".into())
        })
        .transpose()
}
fn validate(rule: &Rule) -> AppResult<()> {
    if rule.name.trim().is_empty()
        || rule.name.len() > 200
        || rule.name_pattern.len() > 512
        || rule.extensions.len() > 100
        || rule.max_size.is_some_and(|n| n < rule.min_size)
    {
        return Err("규칙 이름·조건·크기 범위를 확인하세요.".into());
    }
    if !matches!(rule.date_basis.as_str(), "modified" | "created") {
        return Err("날짜 기준을 확인하세요.".into());
    }
    let (start, end) = (
        date_condition(&rule.start_date)?,
        date_condition(&rule.end_date)?,
    );
    if start.zip(end).is_some_and(|(a, b)| a > b) {
        return Err("시작일이 종료일보다 늦습니다.".into());
    }
    if !rule.source_folder.is_empty() {
        relative_path(&rule.source_folder)?;
    }
    Ok(())
}
pub fn classification_preview(
    scan: &ScanResult,
    rules: &[Rule],
    destination: &Path,
) -> AppResult<ClassificationPlan> {
    if scan.cancelled {
        return Err("부분 검사 결과로 자동 분류할 수 없습니다.".into());
    }
    if rules.is_empty() || rules.len() > 100 {
        return Err("분류 규칙은 1~100개입니다.".into());
    }
    if !destination.is_absolute() {
        return Err("대상은 절대 경로여야 합니다.".into());
    }
    for component in destination.components() {
        if matches!(component, Component::ParentDir) {
            return Err("대상 경로에 ..을 사용할 수 없습니다.".into());
        }
    }
    for rule in rules {
        validate(rule)?;
    }
    let mut plan = ClassificationPlan {
        root: scan.root.clone(),
        destination: destination.to_path_buf(),
        items: vec![],
        warnings: vec![],
    };
    let mut targets = HashSet::new();
    for entry in &scan.files {
        if destination != scan.root
            && destination.starts_with(&scan.root)
            && entry.path.starts_with(destination)
        {
            continue;
        }
        if Path::new(&entry.relative_path).components().any(|p| {
            p.as_os_str()
                .to_string_lossy()
                .to_lowercase()
                .ends_with(".app")
        }) {
            continue;
        }
        for rule in rules.iter().filter(|r| r.enabled) {
            if rule
                .protected_folders
                .iter()
                .any(|p| entry.path.starts_with(p))
                || entry.size < rule.min_size
                || rule.max_size.is_some_and(|n| entry.size > n)
                || !wildcard_match(&rule.name_pattern, &entry.name)
            {
                continue;
            }
            if !rule.extensions.is_empty()
                && !rule.extensions.iter().any(|e| {
                    e.trim_start_matches('.')
                        .eq_ignore_ascii_case(entry.extension.trim_start_matches('.'))
                })
            {
                continue;
            }
            let rel = entry.relative_path.replace('\\', "/");
            let source = rule.source_folder.replace('\\', "/");
            if !source.is_empty() && !(rel.starts_with(&(source.clone() + "/"))) {
                continue;
            }
            if rule.start_date.is_some() || rule.end_date.is_some() {
                let date = match moment(entry, &rule.date_basis) {
                    Ok(d) => d.date_naive(),
                    Err(e) => {
                        plan.warnings.push(format!("{}: {e}", entry.relative_path));
                        break;
                    }
                };
                if date_condition(&rule.start_date)?.is_some_and(|d| date < d)
                    || date_condition(&rule.end_date)?.is_some_and(|d| date > d)
                {
                    continue;
                }
            }
            let folder = match render_target(rule, entry) {
                Ok(p) => p,
                Err(e) => {
                    plan.warnings.push(format!("{}: {e}", entry.relative_path));
                    break;
                }
            };
            let target = destination.join(folder).join(&entry.name);
            if target == entry.path {
                break;
            }
            if !targets.insert(target.to_string_lossy().to_lowercase()) {
                plan.warnings
                    .push(format!("대상 경로 충돌로 제외: {}", entry.relative_path));
                break;
            }
            plan.items.push(ClassificationItem {
                source: entry.path.clone(),
                target,
                rule_name: rule.name.clone(),
            });
            break;
        }
    }
    Ok(plan)
}
