//! Bounded UTF-8 paragraph/row merging. Source files are never retired here.
use crate::core::{self, AppResult, FileSnapshot};
use hmac::{Hmac, Mac};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::OnceLock;

pub const MAX_FILES: usize = 20;
pub const MAX_FILE_BYTES: u64 = 8 * 1024 * 1024;
pub const MAX_TOTAL_BYTES: usize = 32 * 1024 * 1024;
const MAX_UNITS: usize = 200_000;
const MAX_CELLS: usize = 1_000_000;
static KEY: OnceLock<[u8; 32]> = OnceLock::new();

#[derive(Debug, Clone, Serialize)]
pub struct MergeSource {
    pub path: PathBuf,
    pub snapshot: FileSnapshot,
    pub sha256: String,
}
#[derive(Debug, Clone, Serialize)]
pub struct MergePreview {
    pub sources: Vec<MergeSource>,
    pub output: PathBuf,
    pub total_units: usize,
    pub unique_units: usize,
    pub removed_units: usize,
    pub unit: String,
    pub output_bytes: usize,
    pub output_sha256: String,
    #[serde(skip)]
    payload: Vec<u8>,
    #[serde(skip)]
    proof: String,
}
fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn key() -> AppResult<&'static [u8; 32]> {
    if KEY.get().is_none() {
        let mut key = [0u8; 32];
        getrandom::getrandom(&mut key)
            .map_err(|e| format!("보안 난수를 준비할 수 없습니다: {e}"))?;
        let _ = KEY.set(key);
    }
    KEY.get()
        .ok_or_else(|| "병합 인증 키를 준비할 수 없습니다.".into())
}
fn signature(preview: &MergePreview) -> AppResult<String> {
    let data = serde_json::to_vec(&(
        &preview.sources,
        &preview.output,
        preview.total_units,
        preview.unique_units,
        &preview.unit,
        preview.output_bytes,
        &preview.output_sha256,
    ))
    .map_err(|e| e.to_string())?;
    let mut mac = Hmac::<Sha256>::new_from_slice(key()?).map_err(|e| e.to_string())?;
    mac.update(&data);
    Ok(format!("{:x}", mac.finalize().into_bytes()))
}

pub(crate) fn safe_source_path(path: &Path) -> AppResult<()> {
    if !path.is_absolute() || path.components().any(|c| matches!(c, Component::ParentDir)) {
        return Err("원본은 .. 없는 절대 경로여야 합니다.".into());
    }
    for parent in path.ancestors() {
        let meta = fs::symlink_metadata(parent).map_err(|e| e.to_string())?;
        if core::is_link_or_reparse(&meta) {
            return Err("링크·재분석 지점의 원본은 처리하지 않습니다.".into());
        }
    }
    Ok(())
}

pub(crate) fn safe_output(path: &Path) -> AppResult<()> {
    if !path.is_absolute()
        || path.file_name().is_none()
        || path.components().any(|c| matches!(c, Component::ParentDir))
    {
        return Err("출력은 .. 없는 절대 파일 경로여야 합니다.".into());
    }
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    if name
        .chars()
        .any(|c| c.is_control() || "<>:\"/\\|?*".contains(c))
        || name.ends_with([' ', '.'])
    {
        return Err("Windows에서 안전하지 않은 출력 파일 이름입니다.".into());
    }
    let base = name.split('.').next().unwrap_or("").to_ascii_lowercase();
    if ["con", "prn", "aux", "nul"].contains(&base.as_str())
        || (base.len() == 4
            && (base.starts_with("com") || base.starts_with("lpt"))
            && matches!(base.as_bytes()[3], b'1'..=b'9'))
    {
        return Err("예약된 출력 파일 이름입니다.".into());
    }
    for parent in path.ancestors() {
        match fs::symlink_metadata(parent) {
            Ok(meta) => {
                if meta.file_type().is_symlink() {
                    return Err("링크 경로에는 결과를 만들 수 없습니다.".into());
                }
                #[cfg(windows)]
                {
                    use std::os::windows::fs::MetadataExt;
                    if meta.file_attributes() & 0x400 != 0 {
                        return Err("재분석 지점에는 결과를 만들 수 없습니다.".into());
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.to_string()),
        }
    }
    if !path.parent().is_some_and(Path::is_dir) {
        return Err("출력 폴더가 존재해야 합니다.".into());
    }
    if path.exists() {
        return Err("출력 파일이 이미 존재합니다. 덮어쓰지 않습니다.".into());
    }
    Ok(())
}
fn read_source(path: &Path) -> AppResult<(String, MergeSource)> {
    safe_source_path(path)?;
    let mut file = core::safe_open(path)?;
    let before = core::snapshot_open_file(&file)?;
    if before.size > MAX_FILE_BYTES {
        return Err("병합은 파일당 8 MiB까지 지원합니다.".into());
    }
    let mut bytes = Vec::with_capacity(before.size as usize);
    (&mut file)
        .take(MAX_FILE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    let after = core::snapshot_open_file(&file)?;
    safe_source_path(path)?;
    if before != after || after.size != bytes.len() as u64 || core::snapshot(path)? != after {
        return Err("읽는 중 원본이 변경되었습니다.".into());
    }
    let utf8 = bytes.strip_prefix(&[0xef, 0xbb, 0xbf]).unwrap_or(&bytes);
    let text = std::str::from_utf8(utf8).map_err(|_| "UTF-8 텍스트만 병합할 수 있습니다.")?;
    if text
        .chars()
        .any(|c| c.is_control() && !matches!(c, '\t' | '\n' | '\r'))
    {
        return Err("제어 문자 또는 바이너리 내용은 병합할 수 없습니다.".into());
    }
    Ok((
        text.to_owned(),
        MergeSource {
            path: path.to_path_buf(),
            snapshot: after,
            sha256: digest(&bytes),
        },
    ))
}
fn paragraphs(text: &str) -> AppResult<Vec<String>> {
    let mut blocks = Vec::new();
    let mut lines = Vec::new();
    let mut fence: Option<(char, usize)> = None;
    for line in text.lines() {
        let trimmed = line.trim_start_matches(' ');
        let indent = line.len() - trimmed.len();
        if let Some((ch, count)) = fence {
            lines.push(line);
            let run = trimmed.chars().take_while(|c| *c == ch).count();
            if indent <= 3 && run >= count && trimmed[run..].trim().is_empty() {
                fence = None;
            }
            continue;
        }
        if line.trim_matches([' ', '\t']).is_empty() {
            if !lines.is_empty() {
                if blocks.len() >= MAX_UNITS {
                    return Err("문단은 200,000개까지 지원합니다.".into());
                }
                blocks.push(lines.join("\n"));
                lines.clear();
            }
            continue;
        }
        if indent <= 3 {
            if let Some(ch) = trimmed.chars().next().filter(|c| matches!(c, '`' | '~')) {
                let count = trimmed.chars().take_while(|c| *c == ch).count();
                if count >= 3 {
                    fence = Some((ch, count));
                }
            }
        }
        lines.push(line);
    }
    if !lines.is_empty() {
        if blocks.len() >= MAX_UNITS {
            return Err("문단은 200,000개까지 지원합니다.".into());
        }
        blocks.push(lines.join("\n"));
    }
    Ok(blocks)
}
fn merge_text(documents: &[String]) -> AppResult<(Vec<u8>, usize, usize, String)> {
    let mut seen = HashSet::new();
    let mut unique = Vec::new();
    let mut total = 0;
    for text in documents {
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        for block in paragraphs(&text)? {
            total += 1;
            if total > MAX_UNITS {
                return Err("문단·행은 200,000개까지 지원합니다.".into());
            }
            if seen.insert(block.clone()) {
                unique.push(block);
            }
        }
    }
    let count = unique.len();
    let payload = if unique.is_empty() {
        String::new()
    } else {
        unique.join("\n\n") + "\n"
    };
    Ok((payload.into_bytes(), total, count, "문단".into()))
}
fn table_syntax(text: &str, delimiter: u8) -> AppResult<()> {
    // csv's parser intentionally accepts some malformed quoting. Require the
    // documented strict table grammar before deduplicating whole cell rows.
    let bytes = text.as_bytes();
    let (mut state, mut index, mut record_started) = (0u8, 0usize, false);
    while index < bytes.len() {
        let byte = bytes[index];
        if state == 2 {
            if byte == b'"' {
                state = 3;
            }
            index += 1;
            continue;
        }
        if state == 3 && byte == b'"' {
            state = 2;
            index += 1;
            continue;
        }
        if byte == b'\r' || byte == b'\n' {
            if !record_started {
                return Err("빈 CSV·TSV 행의 열 수를 확인하세요.".into());
            }
            state = 0;
            record_started = false;
            if byte == b'\r' && bytes.get(index + 1) == Some(&b'\n') {
                index += 1;
            }
            index += 1;
            continue;
        }
        if byte == delimiter {
            state = 0;
            record_started = true;
            index += 1;
            continue;
        }
        if state == 3 {
            return Err("닫는 따옴표 뒤에는 구분자나 줄바꿈만 사용할 수 있습니다.".into());
        }
        if byte == b'"' {
            if state != 0 {
                return Err("따옴표는 셀 시작에서만 사용할 수 있습니다.".into());
            }
            state = 2;
        } else {
            state = 1;
        }
        record_started = true;
        index += 1;
    }
    if state == 2 {
        return Err("닫히지 않은 CSV·TSV 따옴표가 있습니다.".into());
    }
    Ok(())
}
fn merge_table(documents: &[String], delimiter: u8) -> AppResult<(Vec<u8>, usize, usize, String)> {
    let mut header = None;
    let mut seen = HashSet::new();
    let mut rows = Vec::new();
    let (mut total, mut cells) = (0, 0);
    for text in documents {
        table_syntax(text, delimiter)?;
        let mut reader = csv::ReaderBuilder::new()
            .delimiter(delimiter)
            .has_headers(true)
            .flexible(false)
            .from_reader(text.as_bytes());
        let current = reader
            .headers()
            .map_err(|e| format!("표 헤더 오류: {e}"))?
            .clone();
        if current.is_empty() || current.len() > 1000 {
            return Err("표는 비어 있지 않은 헤더와 최대 1,000개 열이 필요합니다.".into());
        }
        if let Some(old) = &header {
            if old != &current {
                return Err("표의 열 이름과 순서가 같아야 합니다.".into());
            }
        } else {
            header = Some(current);
        }
        for record in reader.records() {
            let row = record.map_err(|e| format!("표 구문 오류: {e}"))?;
            total += 1;
            cells += row.len();
            if total > MAX_UNITS || cells > MAX_CELLS {
                return Err("표의 행·셀 수 제한을 초과했습니다.".into());
            }
            let key = row.iter().map(str::to_owned).collect::<Vec<_>>();
            if seen.insert(key) {
                rows.push(row);
            }
        }
    }
    let mut writer = csv::WriterBuilder::new()
        .delimiter(delimiter)
        .quote_style(csv::QuoteStyle::Always)
        .terminator(csv::Terminator::Any(b'\n'))
        .from_writer(Vec::new());
    if let Some(h) = header {
        writer.write_record(&h).map_err(|e| e.to_string())?;
    }
    for row in &rows {
        writer.write_record(row).map_err(|e| e.to_string())?;
    }
    let bytes = writer.into_inner().map_err(|e| e.to_string())?;
    Ok((bytes, total, rows.len(), "행".into()))
}
pub fn preview_merge(paths: &[PathBuf], output: &Path) -> AppResult<MergePreview> {
    if paths.len() < 2 || paths.len() > MAX_FILES {
        return Err("병합할 파일은 2~20개입니다.".into());
    }
    safe_output(output)?;
    let mut documents = Vec::new();
    let mut sources = Vec::new();
    let mut unique = HashSet::new();
    let mut total_bytes = 0;
    let suffix = paths[0]
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_ascii_lowercase();
    if !["txt", "md", "csv", "tsv"].contains(&suffix.as_str()) {
        return Err("UTF-8 TXT·Markdown·CSV·TSV만 병합할 수 있습니다.".into());
    }
    if output
        .extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_ascii_lowercase()
        != suffix
    {
        return Err("출력 확장자는 첫 파일과 같아야 합니다.".into());
    }
    for path in paths {
        let absolute = path.canonicalize().map_err(|e| e.to_string())?;
        if !unique.insert(absolute.clone()) || absolute == output {
            return Err("병합 원본은 서로 다르고 출력과도 달라야 합니다.".into());
        }
        let ext = path
            .extension()
            .unwrap_or_default()
            .to_string_lossy()
            .to_ascii_lowercase();
        if (suffix == "csv" || suffix == "tsv") && ext != suffix
            || !(suffix == "csv" || suffix == "tsv") && !matches!(ext.as_str(), "txt" | "md")
        {
            return Err("표의 파일 형식은 같아야 하며 텍스트와 표를 섞을 수 없습니다.".into());
        }
        let (text, source) = read_source(path)?;
        total_bytes += source.snapshot.size as usize;
        if total_bytes > MAX_TOTAL_BYTES {
            return Err("병합 원본 합계는 32 MiB까지 지원합니다.".into());
        }
        documents.push(text);
        sources.push(source);
    }
    let (bytes, total, unique, unit) = if suffix == "csv" || suffix == "tsv" {
        merge_table(&documents, if suffix == "tsv" { b'\t' } else { b',' })?
    } else {
        merge_text(&documents)?
    };
    if bytes.len() > MAX_TOTAL_BYTES {
        return Err("병합 출력이 32 MiB 제한을 초과합니다.".into());
    }
    let mut preview = MergePreview {
        sources,
        output: output.to_path_buf(),
        total_units: total,
        unique_units: unique,
        removed_units: total - unique,
        unit,
        output_bytes: bytes.len(),
        output_sha256: digest(&bytes),
        payload: bytes,
        proof: String::new(),
    };
    preview.proof = signature(&preview)?;
    Ok(preview)
}
pub fn execute_merge(preview: &MergePreview) -> AppResult<PathBuf> {
    if signature(preview)? != preview.proof
        || digest(&preview.payload) != preview.output_sha256
        || preview.payload.len() != preview.output_bytes
        || preview.removed_units != preview.total_units.saturating_sub(preview.unique_units)
    {
        return Err("병합 미리보기 인증을 확인할 수 없습니다.".into());
    }
    safe_output(&preview.output)?;
    for source in &preview.sources {
        let (_, now) = read_source(&source.path)?;
        if now.snapshot != source.snapshot || now.sha256 != source.sha256 {
            return Err("미리보기 후 원본이 변경되어 병합을 중단합니다.".into());
        }
    }
    let mut output = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&preview.output)
        .map_err(|e| format!("새 병합본을 만들 수 없습니다: {e}"))?;
    output
        .write_all(&preview.payload)
        .and_then(|_| output.sync_all())
        .map_err(|e| format!("병합본 쓰기 실패. 원본은 유지됩니다: {e}"))?;
    let written = core::snapshot_open_file(&output)?;
    verify_written_output(
        &mut output,
        &preview.output,
        &written,
        preview.output_bytes,
        &preview.output_sha256,
    )?;
    Ok(preview.output.clone())
}

fn read_output_bounded(reader: &mut impl Read, expected_bytes: usize) -> AppResult<Vec<u8>> {
    let limit = expected_bytes
        .checked_add(1)
        .ok_or("출력 크기 한도를 초과했습니다.")?;
    let mut bytes = Vec::with_capacity(expected_bytes);
    reader
        .take(limit as u64)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() != expected_bytes {
        return Err(
            "검증 중 병합본의 크기가 변경되었습니다. 예상하지 못한 바이트는 보존합니다.".into(),
        );
    }
    Ok(bytes)
}
fn verify_written_output(
    output: &mut File,
    path: &Path,
    written: &FileSnapshot,
    expected_bytes: usize,
    expected_hash: &str,
) -> AppResult<()> {
    safe_source_path(path)?;
    if written.size != expected_bytes as u64
        || core::snapshot_open_file(output)? != *written
        || core::snapshot(path)? != *written
    {
        return Err(
            "생성한 병합본의 파일 경로·내용이 변경되었습니다. 파일을 덮어쓰거나 제거하지 않습니다."
                .into(),
        );
    }
    output.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let bytes = read_output_bounded(output, expected_bytes)?;
    if core::snapshot_open_file(output)? != *written
        || core::snapshot(path)? != *written
        || digest(&bytes) != expected_hash
    {
        return Err("병합본 검증 실패. 원본과 예상하지 못한 변경본은 유지됩니다.".into());
    }
    Ok(())
}

#[cfg(test)]
mod verification_tests {
    use super::*;
    #[test]
    fn growing_output_reader_is_bounded_by_signed_expected_length() {
        struct Growing {
            read: usize,
        }
        impl Read for Growing {
            fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
                bytes.fill(b'x');
                self.read += bytes.len();
                Ok(bytes.len())
            }
        }
        let mut growing = Growing { read: 0 };
        assert!(read_output_bounded(&mut growing, 37).is_err());
        assert_eq!(growing.read, 38);
    }
    #[test]
    fn replaced_output_is_refused_and_both_files_are_preserved() {
        let id = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root =
            std::env::temp_dir().join(format!("merge-output-race-{}-{id}", std::process::id()));
        fs::create_dir(&root).unwrap();
        let path = root.join("merged.txt");
        let captured = root.join("captured.txt");
        let mut output = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        output.write_all(b"original merged data").unwrap();
        output.sync_all().unwrap();
        let written = core::snapshot_open_file(&output).unwrap();
        fs::rename(&path, &captured).unwrap();
        fs::write(&path, b"unexpected replacement").unwrap();
        assert!(verify_written_output(
            &mut output,
            &path,
            &written,
            20,
            &digest(b"original merged data")
        )
        .is_err());
        assert_eq!(fs::read(&path).unwrap(), b"unexpected replacement");
        assert_eq!(fs::read(&captured).unwrap(), b"original merged data");
        drop(output);
        fs::remove_dir_all(root).unwrap();
    }
}
