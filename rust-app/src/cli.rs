//! Read-only console interface for the native Rust edition.
use crate::{core, reporting, similarity, AppResult, DuplicateGroup, ScanOptions};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;

const HELP: &str = concat!(
    "Folder Organizer Rust — 로컬 파일 분석 (파일 변경 없음)\n\n",
    "사용법: FolderOrganizerRustCLI <폴더> [옵션]\n",
    "  --duplicates       SHA-256 및 실제 바이트로 완전 중복 검사\n",
    "  --similar          이름·텍스트·이미지 유사 후보 (삭제 권한 아님)\n",
    "  --similar-method <name|text|image>  비교 근거 (반복 가능, 기본 모두)\n",
    "  --name-threshold <0.5~1>   이름 기준 (기본 0.82)\n",
    "  --text-threshold <0.5~1>   텍스트 기준 (기본 0.90)\n",
    "  --image-threshold <0.5~1>  이미지 기준 (기본 0.90)\n",
    "  --max-pairs <1~100000>     비교쌍 한도 (기본 25000)\n",
    "  --hidden           숨김 파일 포함\n",
    "  --no-recursive     선택 폴더만 분석\n",
    "  --exclude <패턴>   제외할 상대 경로·이름 (반복 가능)\n",
    "  --min-size <B>     최소 파일 크기\n",
    "  --max-size <B>     최대 파일 크기\n",
    "  --csv <새 파일>    파일 목록 내보내기\n",
    "  --json <새 파일>   검사 범위·근거·경고 내보내기\n",
    "  --csv-common <새 파일>   두 엔진 공통 CSV 형식\n",
    "  --json-common <새 파일>  두 엔진 공통 JSON 형식\n",
    "  --html <새 파일>   읽기용 분석 보고서\n",
    "  --version          버전 표시\n",
    "보고서는 기존 파일을 덮어쓰지 않습니다. 유사 후보 한도가 작으면 일부 파일을 놓칠 수 있습니다."
);

fn similarity_threshold(value: Option<String>, option: &str) -> AppResult<f32> {
    let message = format!("{option} 기준은 0.5~1 사이의 유한한 숫자여야 합니다.");
    let value: f64 = value
        .ok_or_else(|| format!("{option} 값이 필요합니다."))?
        .parse()
        .map_err(|_| message.clone())?;
    if !value.is_finite() || !(0.5..=1.0).contains(&value) {
        return Err(message);
    }
    Ok(value as f32)
}

pub fn run(arguments: Vec<String>) -> AppResult<i32> {
    const VALUE_OPTIONS: &[&str] = &[
        "--similar-method",
        "--name-threshold",
        "--text-threshold",
        "--image-threshold",
        "--max-pairs",
        "--exclude",
        "--min-size",
        "--max-size",
        "--csv",
        "--json",
        "--csv-common",
        "--json-common",
        "--html",
    ];
    let mut args = arguments.into_iter().flat_map(|value| {
        if let Some((option, setting)) = value.split_once('=') {
            if VALUE_OPTIONS.contains(&option) {
                return vec![option.to_owned(), setting.to_owned()];
            }
        }
        vec![value]
    });
    let mut folder = None;
    let mut options = ScanOptions::default();
    let mut similar_options = similarity::SimilarOptions::default();
    let mut explicit_methods = false;
    let (mut duplicates, mut similar) = (false, false);
    let (mut csv, mut json, mut html) = (None, None, None);
    let (mut common_csv, mut common_json) = (None, None);
    while let Some(argument) = args.next() {
        match argument.as_str() {
            "--help" | "-h" => {
                println!("{HELP}");
                return Ok(0);
            }
            "--version" | "-V" => {
                println!("Folder Organizer Rust {}", env!("CARGO_PKG_VERSION"));
                return Ok(0);
            }
            "--duplicates" => duplicates = true,
            "--similar" => similar = true,
            "--cli" => {}
            "--similar-method" => {
                let method = args.next().ok_or("--similar-method 값이 필요합니다.")?;
                if !explicit_methods {
                    similar_options.compare_names = false;
                    similar_options.compare_text = false;
                    similar_options.compare_images = false;
                    explicit_methods = true;
                }
                match method.as_str() {
                    "name" => similar_options.compare_names = true,
                    "text" => similar_options.compare_text = true,
                    "image" => similar_options.compare_images = true,
                    _ => {
                        return Err(
                            "--similar-method 값은 name, text, image 중 하나여야 합니다.".into(),
                        )
                    }
                }
            }
            "--name-threshold" => {
                similar_options.name_threshold =
                    similarity_threshold(args.next(), "--name-threshold")?
            }
            "--text-threshold" => {
                similar_options.text_threshold =
                    similarity_threshold(args.next(), "--text-threshold")?
            }
            "--image-threshold" => {
                similar_options.image_threshold =
                    similarity_threshold(args.next(), "--image-threshold")?
            }
            "--max-pairs" => {
                let pairs = args
                    .next()
                    .ok_or("--max-pairs 값이 필요합니다.")?
                    .parse::<usize>()
                    .map_err(|_| "--max-pairs 값은 1~100,000 사이의 정수여야 합니다.")?;
                if !(1..=100_000).contains(&pairs) {
                    return Err("--max-pairs 값은 1~100,000 사이의 정수여야 합니다.".into());
                }
                similar_options.max_pairs = pairs;
            }
            "--hidden" => options.include_hidden = true,
            "--no-recursive" => options.recursive = false,
            "--exclude" => options
                .exclude_patterns
                .push(args.next().ok_or("--exclude 값이 필요합니다.")?),
            "--min-size" => {
                options.min_size = args
                    .next()
                    .ok_or("--min-size 값이 필요합니다.")?
                    .parse()
                    .map_err(|_| "최소 크기는 0 이상의 정수여야 합니다.")?
            }
            "--max-size" => {
                options.max_size = Some(
                    args.next()
                        .ok_or("--max-size 값이 필요합니다.")?
                        .parse()
                        .map_err(|_| "최대 크기는 0 이상의 정수여야 합니다.")?,
                )
            }
            "--csv" => {
                csv = Some(PathBuf::from(
                    args.next().ok_or("--csv 파일 경로가 필요합니다.")?,
                ))
            }
            "--json" => {
                json = Some(PathBuf::from(
                    args.next().ok_or("--json 파일 경로가 필요합니다.")?,
                ))
            }
            "--csv-common" => {
                common_csv = Some(PathBuf::from(
                    args.next().ok_or("--csv-common 파일 경로가 필요합니다.")?,
                ))
            }
            "--json-common" => {
                common_json = Some(PathBuf::from(
                    args.next().ok_or("--json-common 파일 경로가 필요합니다.")?,
                ))
            }
            "--html" => {
                html = Some(PathBuf::from(
                    args.next().ok_or("--html 파일 경로가 필요합니다.")?,
                ))
            }
            value if value.starts_with('-') => return Err(format!("알 수 없는 옵션: {value}")),
            value => {
                if folder.replace(PathBuf::from(value)).is_some() {
                    return Err("분석할 폴더는 하나만 지정하세요.".into());
                }
            }
        }
    }
    let folder = folder.ok_or_else(|| HELP.to_owned())?;
    let cancel = AtomicBool::new(false);
    let scan = core::scan(&folder, &options, &cancel, None)?;
    let analysis = if duplicates || similar {
        Some(core::analyze_duplicates(&scan, &cancel, None)?)
    } else {
        None
    };
    let groups: &[DuplicateGroup] = analysis
        .as_ref()
        .map_or(&[], |value| value.groups.as_slice());
    let mut matches = if similar {
        Some(similarity::find_similar_excluding(
            &scan,
            &similar_options,
            groups,
            &cancel,
            None,
        )?)
    } else {
        None
    };
    if let (Some(analysis), Some(matches)) = (&analysis, &mut matches) {
        matches.complete &= analysis.complete;
        if !analysis.complete {
            matches
                .warnings
                .extend(analysis.warnings.iter().take(100).map(|warning| {
                    format!(
                        "중복 제외 검사 일부 미완료: {}: {}",
                        warning.path.display(),
                        warning.message
                    )
                }));
        }
    }
    if let Some(path) = csv {
        reporting::export_csv(&scan, &path)?;
    }
    if let Some(path) = json {
        reporting::export_analysis_json(&scan, &options, analysis.as_ref(), &matches, &path)?;
    }
    if let Some(path) = common_csv {
        reporting::export_common_csv(&scan, &path)?;
    }
    if let Some(path) = common_json {
        reporting::export_common_json(
            &scan,
            &options,
            if duplicates { analysis.as_ref() } else { None },
            &matches,
            &path,
        )?;
    }
    if let Some(path) = html {
        reporting::export_analysis_html(&scan, &options, analysis.as_ref(), &matches, &path)?;
    }
    println!(
        "파일 {}개 · {}바이트 · 완전 중복 그룹 {}개 · 유사 후보 {}쌍",
        scan.files.len(),
        scan.total_size(),
        groups.len(),
        matches.as_ref().map_or(0, |value| value.pairs.len())
    );
    for warning in &scan.warnings {
        eprintln!("주의: {warning}");
    }
    if let Some(analysis) = &analysis {
        for warning in &analysis.warnings {
            eprintln!(
                "주의 [{}]: {}: {}",
                warning.phase,
                warning.path.display(),
                warning.message
            );
        }
    }
    if let Some(matches) = &matches {
        for warning in &matches.warnings {
            eprintln!("주의: {warning}");
        }
    }
    Ok(
        if scan.cancelled
            || !scan.warnings.is_empty()
            || analysis.as_ref().is_some_and(|value| !value.complete)
            || matches
                .as_ref()
                .is_some_and(|value| value.cancelled || value.feature_failures > 0)
        {
            2
        } else {
            0
        },
    )
}

pub fn main() -> i32 {
    match run(std::env::args_os()
        .skip(1)
        .map(|value| value.to_string_lossy().into_owned())
        .collect())
    {
        Ok(code) => code,
        Err(error) => {
            eprintln!("{error}");
            1
        }
    }
}
