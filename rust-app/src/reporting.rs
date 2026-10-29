//! Exclusive, escaped local reports; no file contents or previews are exported.
use crate::core::{AppResult, ScanResult};
use crate::merging::safe_output;
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
}
pub fn export_json<D: Serialize, S: Serialize>(
    scan: &ScanResult,
    duplicates: &D,
    similar: &S,
    path: &Path,
) -> AppResult<()> {
    let mut output = writer(path)?;
    serde_json::to_writer_pretty(&mut output,&Report{app_id:"P-JSL/Folder_Organizer.Rust",app_version:env!("CARGO_PKG_VERSION"),engine:"Rust",scope:"목록·판정 근거·경고만 포함합니다. 파일 내용·이미지 미리보기·실행 권한은 포함하지 않습니다.",scan,duplicates,similar}).map_err(|e|e.to_string())?;
    output.write_all(b"\n").map_err(|e| e.to_string())?;
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
