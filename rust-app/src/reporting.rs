//! Exclusive, escaped local reports; no file contents or previews are exported.
use crate::core::{
    AppResult, DuplicateAnalysis, DuplicateGroup, DuplicateMetrics, DuplicateWarning, ScanOptions,
    ScanResult,
};
use crate::merging::safe_output;
use crate::similarity::SimilarAnalysis;
use serde::Serialize;
use std::fs::OpenOptions;
use std::io::{BufWriter, Write};
use std::path::Path;

fn writer(path: &Path) -> AppResult<BufWriter<std::fs::File>> {
    safe_output(path)?;
    Ok(BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .map_err(|e| e.to_string())?,
    ))
}
fn finish(mut output: BufWriter<std::fs::File>) -> AppResult<()> {
    output.flush().map_err(|e| e.to_string())?;
    output.get_ref().sync_all().map_err(|e| e.to_string())
}
fn spreadsheet_cell(value: &str) -> String {
    if value.starts_with(['=', '+', '-', '@', '\t', '\r', '\n']) {
        format!("'{value}")
    } else {
        value.to_owned()
    }
}
pub fn export_csv(scan: &ScanResult, path: &Path) -> AppResult<()> {
    let output = writer(path)?;
    let mut csv = csv::WriterBuilder::new()
        .quote_style(csv::QuoteStyle::Always)
        .from_writer(output);
    csv.write_record([
        "상대 경로",
        "파일 이름",
        "바이트",
        "수정 시각(ns)",
        "생성 시각(ns, 제공될 때)",
        "종류",
        "확장자",
    ])
    .map_err(|e| e.to_string())?;
    for file in &scan.files {
        csv.write_record([
            spreadsheet_cell(&file.relative_path),
            spreadsheet_cell(&file.name),
            file.size.to_string(),
            file.modified_ns.to_string(),
            file.created_ns.map(|n| n.to_string()).unwrap_or_default(),
            file.kind.clone(),
            spreadsheet_cell(&file.extension),
        ])
        .map_err(|e| e.to_string())?;
    }
    finish(csv.into_inner().map_err(|e| e.to_string())?)
}
#[derive(Serialize)]
struct Report<'a, D: Serialize, S: Serialize> {
    app_id: &'static str,
    app_version: &'static str,
    engine: &'static str,
    scope: &'static str,
    scan: &'a ScanResult,
    duplicates: &'a D,
    similar: &'a S,
    #[serde(skip_serializing_if = "Option::is_none")]
    scan_options: Option<&'a ScanOptions>,
    #[serde(skip_serializing_if = "Option::is_none")]
    duplicate_analysis: Option<DuplicateSummary<'a>>,
}
#[derive(Serialize)]
struct DuplicateSummary<'a> {
    requested: bool,
    complete: bool,
    warnings: &'a [DuplicateWarning],
    metrics: &'a DuplicateMetrics,
    warning_count: u64,
    warnings_truncated: bool,
}
pub fn export_json<D: Serialize, S: Serialize>(
    scan: &ScanResult,
    duplicates: &D,
    similar: &S,
    path: &Path,
) -> AppResult<()> {
    let mut output = writer(path)?;
    serde_json::to_writer_pretty(&mut output,&Report{app_id:"P-JSL/Folder_Organizer.Rust",app_version:env!("CARGO_PKG_VERSION"),engine:"Rust",scope:"목록·판정 근거·경고만 포함합니다. 파일 내용·이미지 미리보기·실행 권한은 포함하지 않습니다.",scan,duplicates,similar,scan_options:None,duplicate_analysis:None}).map_err(|e|e.to_string())?;
    output.write_all(b"\n").map_err(|e| e.to_string())?;
    finish(output)
}

pub fn export_analysis_json(
    scan: &ScanResult,
    options: &ScanOptions,
    analysis: Option<&DuplicateAnalysis>,
    similar: &Option<SimilarAnalysis>,
    path: &Path,
) -> AppResult<()> {
    let empty = Vec::<DuplicateGroup>::new();
    let groups = analysis.map(|value| &value.groups).unwrap_or(&empty);
    let mut output = writer(path)?;
    let report = Report {
        app_id: "P-JSL/Folder_Organizer.Rust",
        app_version: env!("CARGO_PKG_VERSION"),
        engine: "Rust",
        scope: "판정 근거와 부분 오류를 포함합니다. 파일 내용·실행 권한은 내보내지 않습니다.",
        scan,
        duplicates: groups,
        similar,
        scan_options: Some(options),
        duplicate_analysis: analysis.map(|value| DuplicateSummary {
            requested: true,
            complete: value.complete,
            warnings: &value.warnings,
            metrics: &value.metrics,
            warning_count: value.warning_count,
            warnings_truncated: value.warnings_truncated,
        }),
    };
    serde_json::to_writer_pretty(&mut output, &report).map_err(|error| error.to_string())?;
    output.write_all(b"\n").map_err(|error| error.to_string())?;
    finish(output)
}

fn report_relative(path: &Path, root: &Path) -> AppResult<String> {
    let relative = path
        .strip_prefix(root)
        .map_err(|_| "보고서 경로가 분석 폴더 밖에 있습니다.")?;
    Ok(relative
        .components()
        .map(|part| part.as_os_str().to_string_lossy())
        .collect::<Vec<_>>()
        .join("/"))
}

pub fn common_report(
    scan: &ScanResult,
    options: &ScanOptions,
    analysis: Option<&DuplicateAnalysis>,
    similar: &Option<SimilarAnalysis>,
) -> AppResult<serde_json::Value> {
    use serde_json::json;
    let mut files = Vec::with_capacity(scan.files.len());
    for file in &scan.files {
        files.push(json!({"relative_path":report_relative(&file.path,&scan.root)?,"size":file.size,
            "modified_ns":file.modified_ns.to_string(),"created_ns":file.created_ns.map(|value|value.to_string()),
            "kind":file.kind,"extension":file.extension}));
    }
    let mut groups = vec![];
    let mut warnings = vec![];
    if let Some(analysis) = analysis {
        for group in &analysis.groups {
            let mut paths = group
                .paths
                .iter()
                .map(|path| report_relative(path, &scan.root))
                .collect::<AppResult<Vec<_>>>()?;
            paths.sort();
            groups.push(json!({"paths":paths,"size":group.size,"sha256":group.sha256}));
        }
        for warning in &analysis.warnings {
            warnings.push(json!({"path":report_relative(&warning.path,&scan.root).unwrap_or_else(|_|warning.path.to_string_lossy().into_owned()),
                "phase":warning.phase,"message":warning.message}));
        }
    }
    let mut pairs = vec![];
    if let Some(similar) = similar {
        for pair in &similar.pairs {
            pairs.push(json!({"left":report_relative(&pair.left,&scan.root)?,"right":report_relative(&pair.right,&scan.root)?,
                "name_score":similar.options.compare_names.then_some(pair.name_score),"text_score":pair.text_score,
                "image_score":pair.image_score,"byte_identical":serde_json::Value::Null}));
        }
    }
    let inventory_complete = !scan.cancelled && scan.warnings.is_empty();
    let methods = similar
        .as_ref()
        .map(|value| {
            [
                ("name", value.options.compare_names),
                ("text", value.options.compare_text),
                ("image", value.options.compare_images),
            ]
            .into_iter()
            .filter_map(|(name, enabled)| enabled.then_some(name))
            .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    Ok(json!({
        "schema":"folder-organizer.common.v1","app_version":env!("CARGO_PKG_VERSION"),"engine":"Rust","action_authority":false,
        "scope":options,
        "inventory":{"files":files,"file_count":scan.files.len(),"total_bytes":scan.total_size(),
            "complete":inventory_complete,"cancelled":scan.cancelled,"warnings":scan.warnings},
        "duplicates":{"requested":analysis.is_some(),"complete":analysis.map(|value|inventory_complete&&value.complete),
            "groups":groups,"warnings":warnings,"warning_count":analysis.map_or(0,|value|value.warning_count),
            "warnings_truncated":analysis.is_some_and(|value|value.warnings_truncated),
            "metrics":analysis.map(|value|serde_json::to_value(&value.metrics)).transpose().map_err(|error|error.to_string())?.unwrap_or_else(||json!({}))},
        "similarity":{"requested":similar.is_some(),"pairs":pairs,
            "complete":similar.as_ref().map(|value|inventory_complete&&value.complete),
            "approximate":similar.as_ref().is_some_and(|value|value.candidate_index.enabled),
            "methods":methods,"thresholds":similar.as_ref().map(|value|json!({"name":value.options.name_threshold,
            "text":value.options.text_threshold,"image":value.options.image_threshold})).unwrap_or_else(||json!({})),
            "compared_pairs":similar.as_ref().map_or(0,|value|value.compared_pairs),
            "max_pairs":similar.as_ref().map_or(25000,|value|value.options.max_pairs),
            "warnings":similar.as_ref().map(|value|value.warnings.as_slice()).unwrap_or(&[]),
            "candidate_index":similar.as_ref().map(|value|serde_json::to_value(&value.candidate_index)).transpose().map_err(|error|error.to_string())?.unwrap_or_else(||json!({}))}
    }))
}

pub fn export_common_json(
    scan: &ScanResult,
    options: &ScanOptions,
    analysis: Option<&DuplicateAnalysis>,
    similar: &Option<SimilarAnalysis>,
    path: &Path,
) -> AppResult<()> {
    let report = common_report(scan, options, analysis, similar)?;
    let mut output = writer(path)?;
    serde_json::to_writer_pretty(&mut output, &report).map_err(|error| error.to_string())?;
    output.write_all(b"\n").map_err(|error| error.to_string())?;
    finish(output)
}

pub fn export_common_csv(scan: &ScanResult, path: &Path) -> AppResult<()> {
    let mut csv = csv::WriterBuilder::new()
        .quote_style(csv::QuoteStyle::Always)
        .terminator(csv::Terminator::Any(b'\n'))
        .from_writer(writer(path)?);
    csv.write_record([
        "relative_path",
        "size",
        "modified_ns",
        "created_ns",
        "kind",
        "extension",
    ])
    .map_err(|error| error.to_string())?;
    for file in &scan.files {
        csv.write_record([
            spreadsheet_cell(&report_relative(&file.path, &scan.root)?),
            file.size.to_string(),
            file.modified_ns.to_string(),
            file.created_ns
                .map(|value| value.to_string())
                .unwrap_or_default(),
            file.kind.clone(),
            spreadsheet_cell(&file.extension),
        ])
        .map_err(|error| error.to_string())?;
    }
    finish(csv.into_inner().map_err(|error| error.to_string())?)
}

pub fn export_analysis_html(
    scan: &ScanResult,
    options: &ScanOptions,
    analysis: Option<&DuplicateAnalysis>,
    similar: &Option<SimilarAnalysis>,
    path: &Path,
) -> AppResult<()> {
    let mut report = common_report(scan, options, analysis, similar)?;
    for (section, key, limit) in [
        ("inventory", "files", 2000),
        ("duplicates", "groups", 1000),
        ("similarity", "pairs", 1000),
    ] {
        if let Some(values) = report[section][key].as_array_mut() {
            values.truncate(limit);
        }
    }
    let partial = !(!scan.cancelled && scan.warnings.is_empty())
        || analysis.is_some_and(|value| !value.complete)
        || similar
            .as_ref()
            .is_some_and(|value| !value.complete || value.cancelled);
    let mut output = writer(path)?;
    write!(output,"<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\"><title>Folder Organizer 분석 보고서</title><style>body{{font:15px system-ui;margin:32px;color:#203049;background:#f5f7fb}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><h1>Folder Organizer 분석 보고서</h1><p>부분 검사: {partial} · 파일 {}개 · 완전 중복 {}그룹 · 전체 해시 파일 {}개</p><p>유사 후보는 삭제 근거가 아닙니다. 아래 목록은 파일 2,000개·각 결과 1,000개까지 표시합니다. 전체 결과는 JSON을 사용하세요.</p><pre>{}</pre></html>",
        scan.files.len(),analysis.map_or(0,|value|value.groups.len()),analysis.map_or(0,|value|value.metrics.full_hash_files),
        escape(&serde_json::to_string_pretty(&report).map_err(|error|error.to_string())?)).map_err(|error|error.to_string())?;
    finish(output)
}
fn escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(ch),
        }
    }
    out
}
pub fn export_html<D: Serialize, S: Serialize>(
    scan: &ScanResult,
    duplicates: &D,
    similar: &S,
    path: &Path,
) -> AppResult<()> {
    let mut out = writer(path)?;
    write!(out,"<!doctype html><html lang=\"ko\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\"><title>Folder Organizer 분석 보고서</title><style>body{{font:15px system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#203049;background:#f5f7fb}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;text-align:left;border-bottom:1px solid #d7deea;overflow-wrap:anywhere}}pre{{white-space:pre-wrap;background:#fff;padding:16px}}h1{{font-size:27px}}.note{{padding:16px;background:#e7edf8}}</style><h1>Folder Organizer 분석 보고서</h1><p>{}</p><p class=\"note\">논리적 파일 크기 합계입니다. 유사 후보는 삭제 근거가 아니며 파일 내용·이미지 미리보기는 포함하지 않습니다. 부분 검사: {}</p><p>파일 {}개 · {}바이트</p><table><thead><tr><th>상대 경로</th><th>바이트</th><th>종류</th></tr></thead><tbody>",escape(&scan.root.to_string_lossy()),scan.cancelled,scan.files.len(),scan.total_size()).map_err(|e|e.to_string())?;
    for file in &scan.files {
        write!(
            out,
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
            escape(&file.relative_path),
            file.size,
            escape(&file.kind)
        )
        .map_err(|e| e.to_string())?;
    }
    write!(out,"</tbody></table><h2>완전 중복의 근거</h2><pre>{}</pre><h2>유사 후보·검사 범위</h2><pre>{}</pre><h2>경고</h2><pre>{}</pre></html>",escape(&serde_json::to_string_pretty(duplicates).map_err(|e|e.to_string())?),escape(&serde_json::to_string_pretty(similar).map_err(|e|e.to_string())?),escape(&scan.warnings.join("\n"))).map_err(|e|e.to_string())?;
    finish(out)
}
