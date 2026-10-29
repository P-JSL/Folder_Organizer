//! Bounded health checks and non-destructive new-file recovery, not disk undelete.
use crate::core::{self, AppResult, FileSnapshot};
use crate::merging::{safe_output, safe_source_path};
use image::{AnimationDecoder, DynamicImage, ImageFormat, ImageReader};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashSet};
use std::fs::OpenOptions;
use std::io::{BufReader, Cursor, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};

const MAX_INPUT: u64 = 128 * 1024 * 1024;
const MAX_ENTRY: u64 = 64 * 1024 * 1024;
const MAX_TOTAL: u64 = 256 * 1024 * 1024;
const MAX_ENTRIES: usize = 10_000;
const MAX_PIXELS: u64 = 40_000_000;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HealthResult {
    pub path: PathBuf,
    pub format: String,
    pub status: String,
    pub healthy: bool,
    pub issues: Vec<String>,
    pub checked_units: usize,
    pub recoverable_units: usize,
    pub explanation: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RepairResult {
    pub source: PathBuf,
    pub output: PathBuf,
    pub recovered_units: usize,
    pub issues: Vec<String>,
    pub explanation: String,
}
fn suffix(path: &Path) -> String {
    path.extension()
        .unwrap_or_default()
        .to_string_lossy()
        .to_ascii_lowercase()
}
fn source(path: &Path) -> AppResult<(Vec<u8>, FileSnapshot)> {
    safe_source_path(path)?;
    let mut f = core::safe_open(path)?;
    let before = core::snapshot_open_file(&f)?;
    if before.size > MAX_INPUT {
        return Err("복구 검사는 128 MiB 이하 파일만 지원합니다.".into());
    }
    let mut bytes = Vec::with_capacity(before.size as usize);
    (&mut f)
        .take(MAX_INPUT + 1)
        .read_to_end(&mut bytes)
        .map_err(|e| e.to_string())?;
    if bytes.len() as u64 != before.size
        || core::snapshot_open_file(&f)? != before
        || core::snapshot(path)? != before
    {
        return Err("검사 중 원본이 변경되었습니다.".into());
    }
    Ok((bytes, before))
}
fn archive_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 4096
        && !name.starts_with('/')
        && !name.contains(['\\', ':', '\0'])
        && name
            .trim_end_matches('/')
            .split('/')
            .all(|p| !p.is_empty() && p != "." && p != "..")
}
fn entry_limit(size: u64, compressed: u64) -> AppResult<()> {
    if size > MAX_ENTRY || size > compressed.max(1).saturating_mul(500) {
        return Err("압축 항목의 크기·압축률 제한을 초과했습니다.".into());
    }
    Ok(())
}
fn xml_problem(name: &str, payload: &[u8]) -> Option<String> {
    if !(name.to_ascii_lowercase().ends_with(".xml")
        || name.to_ascii_lowercase().ends_with(".rels"))
    {
        return None;
    }
    if payload.len() > 8 * 1024 * 1024 {
        return Some(format!("XML 크기 제한: {name}"));
    }
    let upper = String::from_utf8_lossy(payload).to_ascii_uppercase();
    if upper.contains("<!DOCTYPE") || upper.contains("<!ENTITY") {
        return Some(format!("엔터티가 포함된 XML 제외: {name}"));
    }
    let mut reader = quick_xml::Reader::from_reader(payload);
    reader.config_mut().check_end_names = true;
    let mut buffer = Vec::new();
    let mut depth = 0usize;
    let mut roots = 0usize;
    loop {
        match reader.read_event_into(&mut buffer) {
            Ok(quick_xml::events::Event::Start(_)) => {
                if depth == 0 {
                    roots += 1;
                }
                depth += 1;
                if depth > 128 {
                    return Some(format!("XML 중첩 제한: {name}"));
                }
            }
            Ok(quick_xml::events::Event::Empty(_)) => {
                if depth == 0 {
                    roots += 1;
                }
            }
            Ok(quick_xml::events::Event::End(_)) => {
                if depth == 0 {
                    return Some(format!("XML 구문 손상: {name}"));
                }
                depth -= 1;
            }
            Ok(quick_xml::events::Event::Text(text)) => {
                if text.unescape().is_err() {
                    return Some(format!("XML 엔터티·텍스트 손상: {name}"));
                }
            }
            Ok(quick_xml::events::Event::Eof) => break,
            Err(_) => return Some(format!("XML 구문 손상: {name}")),
            _ => {}
        }
        buffer.clear();
    }
    if depth != 0 || roots != 1 {
        Some(format!("XML 루트·종료 구조 손상: {name}"))
    } else {
        None
    }
}
fn office_issues(ext: &str, entries: &BTreeMap<String, Vec<u8>>) -> Vec<String> {
    let main = match ext {
        "docx" | "docm" => "word/document.xml",
        "xlsx" | "xlsm" => "xl/workbook.xml",
        "pptx" | "pptm" => "ppt/presentation.xml",
        _ => return vec![],
    };
    let mut issues = Vec::new();
    for name in ["[Content_Types].xml", "_rels/.rels", main] {
        if !entries.contains_key(name) {
            issues.push(format!("필수 Office 항목 누락: {name}"));
        }
    }
    for (name, payload) in entries {
        if let Some(issue) = xml_problem(name, payload) {
            issues.push(issue);
        }
    }
    issues
}
fn archive(data: &[u8]) -> AppResult<(BTreeMap<String, Vec<u8>>, Vec<String>, usize)> {
    let mut archive = match zip::ZipArchive::new(Cursor::new(data)) {
        Ok(a) => a,
        Err(_) => return salvage_local(data),
    };
    if archive.len() > MAX_ENTRIES {
        return Err("압축 항목은 10,000개까지 검사합니다.".into());
    }
    let mut entries = BTreeMap::new();
    let mut issues = Vec::new();
    let mut seen = HashSet::new();
    let mut total = 0u64;
    let count = archive.len();
    for index in 0..count {
        let mut file = match archive.by_index(index) {
            Ok(f) => f,
            Err(e) => {
                issues.push(format!("항목을 읽을 수 없음 #{index}: {e}"));
                continue;
            }
        };
        let name = match std::str::from_utf8(file.name_raw()) {
            Ok(s) => s.to_owned(),
            Err(_) => {
                issues.push(format!("지원하지 않는 항목 이름 인코딩 #{index}"));
                continue;
            }
        };
        if !archive_name(&name)
            || !seen.insert(name.to_lowercase())
            || file.unix_mode().is_some_and(|m| m & 0o170000 == 0o120000)
        {
            issues.push(format!("안전하지 않거나 중복된 항목 제외: {name}"));
            continue;
        }
        entry_limit(file.size(), file.compressed_size())?;
        total = total
            .checked_add(file.size())
            .ok_or("전체 압축 크기 초과")?;
        if total > MAX_TOTAL {
            return Err("전체 압축 해제 크기 256 MiB 제한을 초과했습니다.".into());
        }
        if file.is_dir() {
            entries.insert(name, Vec::new());
            continue;
        }
        let expected = file.size();
        let mut bytes = Vec::with_capacity(expected as usize);
        match (&mut file).take(MAX_ENTRY + 1).read_to_end(&mut bytes) {
            Ok(_) if bytes.len() as u64 == expected => {
                entries.insert(name, bytes);
            }
            Ok(_) => issues.push(format!("크기 불일치 항목 제외: {name}")),
            Err(e) => issues.push(format!("CRC·내용 오류 항목 제외: {name} ({e})")),
        }
    }
    Ok((entries, issues, count))
}
fn le16(b: &[u8], at: usize) -> Option<u16> {
    Some(u16::from_le_bytes(b.get(at..at + 2)?.try_into().ok()?))
}
fn le32(b: &[u8], at: usize) -> Option<u32> {
    Some(u32::from_le_bytes(b.get(at..at + 4)?.try_into().ok()?))
}
fn salvage_local(data: &[u8]) -> AppResult<(BTreeMap<String, Vec<u8>>, Vec<String>, usize)> {
    let mut entries = BTreeMap::new();
    let mut issues = vec!["중앙 디렉터리 손상: 크기·CRC가 확인되는 로컬 항목만 복구합니다.".into()];
    let mut offset = 0usize;
    let mut examined = 0usize;
    let mut total = 0u64;
    let mut seen = HashSet::new();
    while offset + 30 <= data.len() && examined < MAX_ENTRIES {
        let Some(found) = data[offset..]
            .windows(4)
            .position(|w| w == b"PK\x03\x04")
            .map(|n| n + offset)
        else {
            break;
        };
        offset = found + 4;
        examined += 1;
        let Some(flags) = le16(data, found + 6) else {
            continue;
        };
        let Some(method) = le16(data, found + 8) else {
            continue;
        };
        let Some(crc) = le32(data, found + 14) else {
            continue;
        };
        let Some(compressed) = le32(data, found + 18) else {
            continue;
        };
        let Some(size) = le32(data, found + 22) else {
            continue;
        };
        let Some(name_len) = le16(data, found + 26) else {
            continue;
        };
        let Some(extra_len) = le16(data, found + 28) else {
            continue;
        };
        let start = found + 30 + name_len as usize + extra_len as usize;
        let Some(end) = start.checked_add(compressed as usize) else {
            continue;
        };
        if name_len == 0 || name_len > 4096 || end > data.len() || start > data.len() {
            continue;
        }
        let Ok(name) = std::str::from_utf8(&data[found + 30..found + 30 + name_len as usize])
        else {
            continue;
        };
        if !archive_name(name) || !seen.insert(name.to_lowercase()) {
            continue;
        }
        if flags & 9 != 0 || !matches!(method, 0 | 8) {
            issues.push(format!("크기 경계·암호화·압축 방식으로 제외: {name}"));
            continue;
        }
        entry_limit(size as u64, compressed as u64)?;
        total += size as u64;
        if total > MAX_TOTAL {
            return Err("복구 크기 제한을 초과했습니다.".into());
        }
        let compressed_data = &data[start..end];
        let mut payload = Vec::new();
        if method == 0 {
            payload.extend_from_slice(compressed_data);
        } else {
            let reader = flate2::read::DeflateDecoder::new(compressed_data);
            if reader
                .take(MAX_ENTRY + 1)
                .read_to_end(&mut payload)
                .is_err()
            {
                continue;
            }
        }
        if payload.len() != size as usize || crc32fast::hash(&payload) != crc {
            issues.push(format!("크기·CRC를 검증하지 못한 항목 제외: {name}"));
            continue;
        }
        entries.insert(name.into(), payload);
        offset = end;
    }
    if entries.is_empty() {
        return Err("크기·CRC를 검증할 수 있는 복구 항목이 없습니다.".into());
    }
    Ok((entries, issues, examined))
}
fn image_data(data: &[u8], ext: &str) -> AppResult<(DynamicImage, Vec<String>)> {
    let mut reader = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(|e| e.to_string())?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(MAX_PIXELS as u32);
    limits.max_image_height = Some(MAX_PIXELS as u32);
    limits.max_alloc = Some(MAX_TOTAL);
    reader.limits(limits);
    let dimensions = ImageReader::new(Cursor::new(data))
        .with_guessed_format()
        .map_err(|e| e.to_string())?
        .into_dimensions()
        .map_err(|e| e.to_string())?;
    if dimensions.0 as u64 * dimensions.1 as u64 > MAX_PIXELS {
        return Err("이미지는 40 MP까지 지원합니다.".into());
    }
    let mut warnings = Vec::new();
    if ext == "gif" {
        let decoder = image::codecs::gif::GifDecoder::new(BufReader::new(Cursor::new(data)))
            .map_err(|e| e.to_string())?;
        let mut total = 0u64;
        let mut count = 0usize;
        for frame in decoder.into_frames() {
            let frame = frame.map_err(|e| e.to_string())?;
            count += 1;
            let b = frame.buffer();
            total += b.width() as u64 * b.height() as u64;
            if count > 1000 || total > 80_000_000 {
                return Err("애니메이션 프레임·총 픽셀 제한을 초과했습니다.".into());
            }
        }
        if count > 1 {
            warnings.push(
                "이미지 복구는 첫 프레임을 새 PNG로 보존합니다. 원본 애니메이션은 유지하세요."
                    .into(),
            );
        }
    } else if matches!(ext, "tif" | "tiff" | "webp") {
        warnings.push("다중 페이지·애니메이션 형식은 첫 이미지 검증·PNG 보존 범위입니다.".into());
    }
    Ok((
        reader
            .decode()
            .map_err(|e| format!("이미지 디코딩 오류: {e}"))?,
        warnings,
    ))
}
fn pdf(data: &[u8]) -> AppResult<lopdf::Document> {
    if data.len() > 16 * 1024 * 1024 {
        return Err("PDF 재작성은 16 MiB까지 지원합니다.".into());
    }
    // Object/xref stream decoders have no allocation budget in this parser.
    // Reject that subset before parsing, including PDF name escape spellings.
    let mut names = Vec::new();
    let mut index = 0;
    while index < data.len() {
        if data[index] != b'/' {
            index += 1;
            continue;
        }
        index += 1;
        let mut name = Vec::new();
        while index < data.len() && !b" \t\r\n\x0c\x00/[]<>()%".contains(&data[index]) {
            if data[index] == b'#' && index + 2 < data.len() {
                if let Ok(s) = std::str::from_utf8(&data[index + 1..index + 3]) {
                    if let Ok(v) = u8::from_str_radix(s, 16) {
                        name.push(v);
                        index += 3;
                        continue;
                    }
                }
            }
            name.push(data[index]);
            index += 1;
        }
        names.push(name);
    }
    if names
        .iter()
        .any(|name| name == b"ObjStm" || name == b"XRef")
    {
        return Err("압축 객체·xref 스트림 PDF는 메모리 제한을 보장할 수 없어 이 버전에서 재작성하지 않습니다.".into());
    }
    let document = lopdf::Document::load_mem(data).map_err(|e| format!("PDF 구조 오류: {e}"))?;
    if document.is_encrypted() {
        return Err("암호화 PDF는 비밀번호 없이 복구하지 않습니다.".into());
    }
    if document.get_pages().len() > 2000 || document.objects.len() > 100_000 {
        return Err("PDF 페이지·객체 수 제한을 초과했습니다.".into());
    }
    Ok(document)
}
pub fn check_health(path: &Path) -> AppResult<HealthResult> {
    let (data, _) = source(path)?;
    let ext = suffix(path);
    let mut result = HealthResult {
        path: path.to_path_buf(),
        format: ext.clone(),
        status: "healthy".into(),
        healthy: true,
        issues: vec![],
        checked_units: 1,
        recoverable_units: 1,
        explanation:
            "읽을 수 있는 구조를 검사합니다. 원본의 모든 내용이 보존됐다는 보장은 아닙니다.".into(),
    };
    let outcome: AppResult<()> = match ext.as_str() {
        "zip" | "docx" | "xlsx" | "pptx" | "docm" | "xlsm" | "pptm" => {
            archive(&data).map(|(entries, mut issues, count)| {
                issues.extend(office_issues(&ext, &entries));
                result.checked_units = count;
                result.recoverable_units = entries.len();
                result.issues = issues;
            })
        }
        "png" | "jpg" | "jpeg" | "gif" | "bmp" | "tif" | "tiff" | "webp" | "ico" => {
            image_data(&data, &ext).map(|(_, warnings)| {
                result.issues = warnings;
            })
        }
        "pdf" => pdf(&data).map(|document| {
            result.checked_units = document.get_pages().len();
            result.recoverable_units = result.checked_units;
        }),
        _ => {
            result.status = "unsupported".into();
            result.healthy = false;
            result.recoverable_units = 0;
            return Ok(result);
        }
    };
    if let Err(error) = outcome {
        result.healthy = false;
        result.status = "damaged_or_unsupported".into();
        result.issues.push(error);
        result.recoverable_units = 0;
    } else if !result.issues.is_empty() {
        result.healthy = false;
        result.status = "review".into();
    }
    Ok(result)
}
pub fn repair_to_new(path: &Path, destination: &Path) -> AppResult<RepairResult> {
    safe_output(destination)?;
    let (data, snapshot) = source(path)?;
    let ext = suffix(path);
    let mut output = Vec::new();
    let mut issues = Vec::new();
    let units;
    match ext.as_str() {
        "zip" | "docx" | "xlsx" | "pptx" | "docm" | "xlsm" | "pptm" => {
            if !destination
                .extension()
                .unwrap_or_default()
                .to_string_lossy()
                .eq_ignore_ascii_case(&ext)
            {
                return Err("압축 복구본의 확장자는 원본과 같아야 합니다.".into());
            }
            let (entries, mut found, _) = archive(&data)?;
            found.extend(office_issues(&ext, &entries));
            issues = found;
            units = entries.len();
            if units == 0 {
                return Err("보존할 수 있는 압축 항목이 없습니다.".into());
            }
            let mut writer = zip::ZipWriter::new(Cursor::new(Vec::new()));
            let options = zip::write::SimpleFileOptions::default()
                .compression_method(zip::CompressionMethod::Deflated);
            for (name, payload) in entries {
                if name.ends_with('/') {
                    writer
                        .add_directory(name, options)
                        .map_err(|e| e.to_string())?;
                } else {
                    writer
                        .start_file(name, options)
                        .map_err(|e| e.to_string())?;
                    writer.write_all(&payload).map_err(|e| e.to_string())?;
                }
            }
            output = writer.finish().map_err(|e| e.to_string())?.into_inner();
            let _ = archive(&output)?;
        }
        "png" | "jpg" | "jpeg" | "gif" | "bmp" | "tif" | "tiff" | "webp" | "ico" => {
            if suffix(destination) != "png" {
                return Err("이미지 복구본은 새 .png 파일로 저장합니다.".into());
            }
            let (image, warnings) = image_data(&data, &ext)?;
            issues = warnings;
            let mut stream = Cursor::new(Vec::new());
            image
                .write_to(&mut stream, ImageFormat::Png)
                .map_err(|e| e.to_string())?;
            output = stream.into_inner();
            let _ = image_data(&output, "png")?;
            units = 1;
        }
        "pdf" => {
            if suffix(destination) != "pdf" {
                return Err("PDF 복구본은 .pdf로 저장합니다.".into());
            }
            let mut document = pdf(&data)?;
            units = document.get_pages().len();
            document.save_to(&mut output).map_err(|e| e.to_string())?;
            let _ = pdf(&output)?;
        }
        _ => {
            return Err(
                "지원하는 ZIP·Office ZIP·이미지·PDF만 새 복구본으로 만들 수 있습니다.".into(),
            )
        }
    }
    if output.len() as u64 > MAX_TOTAL {
        return Err("복구 출력의 크기 제한을 초과했습니다.".into());
    }
    if core::snapshot(path)? != snapshot {
        return Err("검사 후 원본이 변경되어 출력을 중단합니다.".into());
    }
    safe_output(destination)?;
    let mut file = OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(destination)
        .map_err(|e| e.to_string())?;
    file.write_all(&output)
        .and_then(|_| file.sync_all())
        .map_err(|e| format!("새 복구본 쓰기 실패. 원본은 유지됩니다: {e}"))?;
    let written = core::snapshot_open_file(&file)?;
    if written.size != output.len() as u64 || core::snapshot(destination)? != written {
        return Err("생성한 복구본이 검증 전에 변경되었습니다. 변경된 바이트는 보존합니다.".into());
    }
    file.seek(SeekFrom::Start(0)).map_err(|e| e.to_string())?;
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 65536];
    let mut read = 0usize;
    {
        let mut bounded = (&mut file).take(output.len() as u64 + 1);
        loop {
            let n = bounded.read(&mut buffer).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            read += n;
            hasher.update(&buffer[..n]);
        }
    }
    if read != output.len()
        || hasher.finalize().as_slice() != Sha256::digest(&output).as_slice()
        || core::snapshot_open_file(&file)? != written
        || core::snapshot(destination)? != written
    {
        return Err(
            "새 복구본의 바이트·경로 검증에 실패했습니다. 원본과 변경된 파일은 유지됩니다.".into(),
        );
    }
    Ok(RepairResult{source:path.to_path_buf(),output:destination.to_path_buf(),recovered_units:units,issues,explanation:"읽을 수 있는 내용만 새 파일로 보존했습니다. 원본을 유지하고 결과를 직접 확인하세요. 삭제 섹터·없어진 바이트를 복원하는 기능은 아닙니다.".into()})
}
