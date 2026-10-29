//! Read-only console interface for the native Rust edition.
use crate::{core, reporting, similarity, AppResult, DuplicateGroup, ScanOptions};
use std::path::PathBuf;
use std::sync::atomic::AtomicBool;

const HELP: &str = "Folder Organizer Rust — 로컬 파일 분석 (파일 변경 없음)\n\n사용법: FolderOrganizerRustCLI <폴더> [옵션]\n  --duplicates       SHA-256 및 실제 바이트로 완전 중복 검사\n  --similar          이름·텍스트·이미지 유사 후보 (삭제 권한 아님)\n  --hidden           숨김 파일 포함\n  --no-recursive     선택 폴더만 분석\n  --exclude <패턴>   제외할 상대 경로·이름 (반복 가능)\n  --min-size <B>     최소 파일 크기\n  --max-size <B>     최대 파일 크기\n  --csv <새 파일>    파일 목록 내보내기\n  --json <새 파일>   검사 범위·근거·경고 내보내기\n  --html <새 파일>   읽기용 분석 보고서\n  --version          버전 표시\n보고서는 기존 파일을 덮어쓰지 않습니다.";

pub fn run(arguments: Vec<String>) -> AppResult<i32> {
    let mut args = arguments.into_iter().filter(|value| value != "--cli");
    let mut folder = None;
    let mut options = ScanOptions::default();
    let (mut duplicates, mut similar) = (false, false);
    let (mut csv, mut json, mut html) = (None, None, None);
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
    let groups: Vec<DuplicateGroup> = if duplicates || similar {
        core::find_duplicates(&scan, &cancel, None)?
    } else {
        vec![]
    };
    let matches = if similar {
        Some(similarity::find_similar_excluding(
            &scan,
            &similarity::SimilarOptions::default(),
            &groups,
            &cancel,
            None,
        )?)
    } else {
        None
    };
    if let Some(path) = csv {
        reporting::export_csv(&scan, &path)?;
    }
    if let Some(path) = json {
        reporting::export_json(&scan, &groups, &matches, &path)?;
    }
    if let Some(path) = html {
        reporting::export_html(&scan, &groups, &matches, &path)?;
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
    if let Some(matches) = &matches {
        for warning in &matches.warnings {
            eprintln!("주의: {warning}");
        }
    }
    Ok(if scan.cancelled { 2 } else { 0 })
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
