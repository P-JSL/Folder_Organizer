//! Bounded name/text candidates. Similarity is never permission to delete.
use crate::core::{
    find_duplicates, safe_open, snapshot, snapshot_open_file, validate_group, AppResult,
    DuplicateGroup, FileEntry, Progress, ScanResult,
};
use image::{ImageDecoder, ImageReader};
use serde::{Deserialize, Serialize};
use std::collections::{BinaryHeap, HashMap, HashSet, VecDeque};
use std::io::{Cursor, Read};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

const MAX_INDEX_KEYS: usize = 65_536;
const MAX_POSTINGS: usize = 32;
const MAX_KEYS_PER_FILE: usize = 64;
const MAX_CANDIDATES_PER_FILE: usize = 32;
const MAX_TEXT_BYTES: usize = 256 * 1024;
const MAX_WORDS: usize = 4096;
const TEXT_CACHE_ENTRIES: usize = 256;
const IMAGE_CACHE_ENTRIES: usize = 4096;
const MAX_IMAGE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_IMAGE_PIXELS: u64 = 40_000_000;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarOptions {
    pub compare_names: bool,
    pub compare_text: bool,
    pub compare_images: bool,
    pub name_threshold: f32,
    pub text_threshold: f32,
    pub image_threshold: f32,
    pub max_pairs: usize,
}

impl Default for SimilarOptions {
    fn default() -> Self {
        Self {
            compare_names: true,
            compare_text: true,
            compare_images: true,
            name_threshold: 0.82,
            text_threshold: 0.90,
            image_threshold: 0.90,
            max_pairs: 25_000,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarPair {
    pub left: PathBuf,
    pub right: PathBuf,
    pub name_score: f32,
    pub text_score: Option<f32>,
    pub image_score: Option<f32>,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SimilarAnalysis {
    pub pairs: Vec<SimilarPair>,
    pub compared_pairs: usize,
    pub possible_pairs: u128,
    pub excluded_exact_pairs: usize,
    pub complete: bool,
    pub cancelled: bool,
    pub warnings: Vec<String>,
}

fn name_keys(entry: &FileEntry) -> Vec<String> {
    let stem = entry
        .path
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy()
        .to_lowercase();
    let chars: Vec<char> = stem
        .chars()
        .filter(|ch| ch.is_alphanumeric())
        .take(256)
        .collect();
    if chars.len() < 2 {
        return vec![format!("n:{}", chars.into_iter().collect::<String>())];
    }
    let mut keys = HashSet::new();
    for pair in chars.windows(2) {
        keys.insert(format!("n:{}{}", pair[0], pair[1]));
        if keys.len() >= MAX_KEYS_PER_FILE {
            break;
        }
    }
    let mut keys: Vec<_> = keys.into_iter().collect();
    keys.sort();
    keys
}

fn dice(left: &[String], right: &[String]) -> f32 {
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    let (mut a, mut b, mut equal) = (0, 0, 0);
    while a < left.len() && b < right.len() {
        match left[a].cmp(&right[b]) {
            std::cmp::Ordering::Less => a += 1,
            std::cmp::Ordering::Greater => b += 1,
            std::cmp::Ordering::Equal => {
                equal += 1;
                a += 1;
                b += 1;
            }
        }
    }
    2.0 * equal as f32 / (left.len() + right.len()) as f32
}

fn pair_rank(a: usize, b: usize) -> u64 {
    // Stable non-cryptographic sampling rank; this never establishes equality.
    let mut value = (a as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15) ^ b as u64;
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn text_words(entry: &FileEntry) -> AppResult<Option<Vec<String>>> {
    if ![
        ".txt", ".md", ".csv", ".tsv", ".log", ".json", ".toml", ".yaml", ".yml", ".xml", ".ini",
        ".cfg",
    ]
    .contains(&entry.extension.as_str())
    {
        return Ok(None);
    }
    let mut file = safe_open(&entry.path)?;
    let before = snapshot_open_file(&file)?;
    if before.size != entry.size
        || before.modified_ns != entry.modified_ns
        || entry
            .identity
            .as_ref()
            .is_some_and(|identity| identity != &before)
    {
        return Err(format!(
            "목록 분석 후 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut bytes = Vec::with_capacity(MAX_TEXT_BYTES.min(before.size as usize));
    (&mut file)
        .take(MAX_TEXT_BYTES as u64)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if snapshot_open_file(&file)? != before || snapshot(&entry.path)? != before {
        return Err(format!(
            "텍스트를 읽는 동안 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let value = match std::str::from_utf8(&bytes) {
        Ok(value) => value,
        Err(error) if error.error_len().is_none() && before.size > MAX_TEXT_BYTES as u64 => {
            std::str::from_utf8(&bytes[..error.valid_up_to()]).map_err(|error| error.to_string())?
        }
        Err(_) => return Ok(None),
    };
    let words: HashSet<String> = value
        .split(|ch: char| !ch.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .take(MAX_WORDS)
        .map(str::to_lowercase)
        .collect();
    let mut words: Vec<_> = words.into_iter().collect();
    words.sort();
    Ok(Some(words))
}

fn is_image(entry: &FileEntry) -> bool {
    [
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico",
    ]
    .contains(&entry.extension.as_str())
}

struct ImageSignature {
    hash: u64,
    pixels: Vec<u8>,
    aspect: f32,
}

fn image_signature(entry: &FileEntry) -> AppResult<Option<ImageSignature>> {
    if !is_image(entry) {
        return Ok(None);
    }
    let mut file = safe_open(&entry.path)?;
    let before = snapshot_open_file(&file)?;
    if before.size > MAX_IMAGE_BYTES {
        return Err(format!(
            "64 MiB 초과 이미지 비교 건너뜀: {}",
            entry.path.display()
        ));
    }
    if before.size != entry.size
        || before.modified_ns != entry.modified_ns
        || entry
            .identity
            .as_ref()
            .is_some_and(|identity| identity != &before)
    {
        return Err(format!(
            "목록 분석 후 이미지가 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut bytes = Vec::with_capacity(before.size as usize);
    (&mut file)
        .take(MAX_IMAGE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| error.to_string())?;
    if bytes.len() as u64 != before.size
        || snapshot_open_file(&file)? != before
        || snapshot(&entry.path)? != before
    {
        return Err(format!(
            "읽는 동안 이미지가 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    let mut reader = ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .map_err(|error| error.to_string())?;
    let mut limits = image::Limits::default();
    limits.max_alloc = Some(192 * 1024 * 1024);
    reader.limits(limits);
    let mut decoder = reader
        .into_decoder()
        .map_err(|error| format!("이미지 읽기 실패 {}: {error}", entry.path.display()))?;
    let (width, height) = decoder.dimensions();
    if width == 0 || height == 0 || width as u64 * height as u64 > MAX_IMAGE_PIXELS {
        return Err(format!(
            "40 MP 초과 또는 크기 없는 이미지 건너뜀: {}",
            entry.path.display()
        ));
    }
    let orientation = decoder.orientation().map_err(|error| error.to_string())?;
    let mut image =
        image::DynamicImage::from_decoder(decoder).map_err(|error| error.to_string())?;
    image.apply_orientation(orientation);
    let aspect = image.width() as f32 / image.height() as f32;
    let tiny = image
        .resize_exact(9, 8, image::imageops::FilterType::Triangle)
        .to_rgba8();
    let luminance = |pixel: &image::Rgba<u8>| -> u32 {
        let alpha = pixel[3] as u32;
        let r = (pixel[0] as u32 * alpha + 255 * (255 - alpha)) / 255;
        let g = (pixel[1] as u32 * alpha + 255 * (255 - alpha)) / 255;
        let b = (pixel[2] as u32 * alpha + 255 * (255 - alpha)) / 255;
        299 * r + 587 * g + 114 * b
    };
    let mut hash = 0_u64;
    for y in 0..8 {
        for x in 0..8 {
            hash <<= 1;
            hash |= (luminance(tiny.get_pixel(x, y)) > luminance(tiny.get_pixel(x + 1, y))) as u64;
        }
    }
    let color = image
        .resize_exact(8, 8, image::imageops::FilterType::Triangle)
        .to_rgba8();
    let mut pixels = Vec::with_capacity(256);
    for pixel in color.pixels() {
        let alpha = pixel[3] as u32;
        for &channel in &pixel.0[..3] {
            pixels.push(((channel as u32 * alpha + 255 * (255 - alpha)) / 255) as u8);
        }
        pixels.push(pixel[3]);
    }
    if snapshot(&entry.path)? != before {
        return Err(format!(
            "이미지 해석 중 파일이 바뀌었습니다: {}",
            entry.path.display()
        ));
    }
    Ok(Some(ImageSignature {
        hash,
        pixels,
        aspect,
    }))
}

fn image_score(left: &ImageSignature, right: &ImageSignature) -> f32 {
    let structure = 1.0 - (left.hash ^ right.hash).count_ones() as f32 / 64.0;
    let color = 1.0
        - left
            .pixels
            .iter()
            .zip(&right.pixels)
            .map(|(a, b)| a.abs_diff(*b) as u32)
            .sum::<u32>() as f32
            / (256.0 * 255.0);
    let aspect = (left.aspect / right.aspect).min(right.aspect / left.aspect);
    // Uniform but differently colored pictures cannot receive a high score
    // solely because their difference hashes are both constant.
    (structure * 0.55 + color * 0.35 + aspect * 0.10).clamp(0.0, 1.0)
}

/// Visit the full inventory, sample bounded indexed candidates, and show its
/// coverage. Full pair enumeration is used only when it fits the pair budget.
pub fn find_similar(
    scan: &ScanResult,
    options: &SimilarOptions,
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<SimilarAnalysis> {
    let exact = find_duplicates(scan, cancel, progress)?;
    find_similar_excluding(scan, options, &exact, cancel, progress)
}

/// A GUI may pass its existing authenticated exact results to keep those pairs
/// out of the similar-candidate list without another content scan.
pub fn find_similar_excluding(
    scan: &ScanResult,
    options: &SimilarOptions,
    exact_groups: &[DuplicateGroup],
    cancel: &AtomicBool,
    progress: Option<&dyn Fn(Progress)>,
) -> AppResult<SimilarAnalysis> {
    if !options.compare_names && !options.compare_text && !options.compare_images {
        return Err("이름·텍스트·이미지 비교를 하나 이상 선택하세요.".into());
    }
    if !options.name_threshold.is_finite()
        || !options.text_threshold.is_finite()
        || !options.image_threshold.is_finite()
        || !(0.0..=1.0).contains(&options.name_threshold)
        || !(0.0..=1.0).contains(&options.text_threshold)
        || !(0.0..=1.0).contains(&options.image_threshold)
        || options.max_pairs == 0
        || options.max_pairs > 100_000
    {
        return Err("유사도 기준은 0~1, 비교 한도는 1~100,000이어야 합니다.".into());
    }
    let count = scan.files.len();
    let mut exact_membership = HashMap::new();
    for (index, group) in exact_groups.iter().enumerate() {
        validate_group(group)?;
        for path in &group.paths {
            exact_membership.insert(path.clone(), index);
        }
    }
    let possible = count as u128 * count.saturating_sub(1) as u128 / 2;
    let exhaustive = possible <= options.max_pairs as u128;
    let mut pairs = Vec::new();
    let mut cancelled = scan.cancelled;
    if exhaustive {
        'outer: for left in 0..count {
            if cancel.load(Ordering::Relaxed) {
                cancelled = true;
                break;
            }
            for right in left + 1..count {
                pairs.push((left, right));
                if cancel.load(Ordering::Relaxed) {
                    cancelled = true;
                    break 'outer;
                }
            }
        }
    } else {
        let mut index: HashMap<String, VecDeque<usize>> = HashMap::new();
        let mut sample = BinaryHeap::new();
        for (current, entry) in scan.files.iter().enumerate() {
            if cancel.load(Ordering::Relaxed) {
                cancelled = true;
                break;
            }
            let keys = name_keys(entry);
            let mut candidates = HashSet::new();
            for key in &keys {
                if let Some(postings) = index.get(key) {
                    for &previous in postings.iter().rev() {
                        candidates.insert(previous);
                        if candidates.len() >= MAX_CANDIDATES_PER_FILE {
                            break;
                        }
                    }
                }
                if candidates.len() >= MAX_CANDIDATES_PER_FILE {
                    break;
                }
            }
            // Content-only comparisons need candidates even with unrelated
            // names. A rolling extension bucket adds a bounded sample.
            if options.compare_text && candidates.len() < MAX_CANDIDATES_PER_FILE {
                if let Some(postings) = index.get(&format!("e:{}", entry.extension)) {
                    for &previous in postings.iter().rev() {
                        candidates.insert(previous);
                        if candidates.len() >= MAX_CANDIDATES_PER_FILE {
                            break;
                        }
                    }
                }
            }
            if options.compare_images
                && is_image(entry)
                && candidates.len() < MAX_CANDIDATES_PER_FILE
            {
                if let Some(postings) = index.get("media:image") {
                    for &previous in postings.iter().rev() {
                        candidates.insert(previous);
                        if candidates.len() >= MAX_CANDIDATES_PER_FILE {
                            break;
                        }
                    }
                }
            }
            for previous in candidates {
                let candidate = (pair_rank(previous, current), previous, current);
                if sample.len() < options.max_pairs {
                    sample.push(candidate);
                } else if sample.peek().is_some_and(|top| candidate < *top) {
                    sample.pop();
                    sample.push(candidate);
                }
            }
            let mut keys = keys;
            if options.compare_text {
                keys.push(format!("e:{}", entry.extension));
            }
            if options.compare_images && is_image(entry) {
                keys.push("media:image".into());
            }
            for key in keys {
                if !index.contains_key(&key) && index.len() >= MAX_INDEX_KEYS {
                    continue;
                }
                let postings = index.entry(key).or_default();
                if postings.len() >= MAX_POSTINGS {
                    postings.pop_front();
                }
                postings.push_back(current);
            }
        }
        pairs = sample.into_iter().map(|(_, a, b)| (a, b)).collect();
        pairs.sort_unstable();
    }
    let mut report = SimilarAnalysis {
        pairs: vec![],
        compared_pairs: 0,
        possible_pairs: possible,
        excluded_exact_pairs: 0,
        complete: exhaustive && !cancelled,
        cancelled,
        warnings: vec![],
    };
    if !exhaustive {
        report.warnings.push("전체 목록에서 제한된 이름·확장자 후보를 추린 결과입니다. 일부 유사 파일을 놓칠 수 있습니다.".into());
    }
    if options.compare_text {
        report.warnings.push("텍스트는 UTF-8 앞 256 KiB·4,096단어의 어휘 집합을 비교합니다. 문장 의미나 전체 문서 동일성을 판정하지 않습니다.".into());
    }
    if options.compare_images {
        report.warnings.push("이미지는 40 MP·64 MiB 이하의 첫 프레임을 방향 보정 후 축소 구조·색·투명도·종횡비로 비교합니다. 전체 움직임·회전·크롭·의미를 판정하지 않습니다.".into());
    }
    report.warnings.push(
        "유사도 100%도 완전 중복의 증거가 아닙니다. 유사 결과로 자동 삭제하지 않습니다.".into(),
    );
    let mut text_cache: HashMap<usize, Option<Vec<String>>> = HashMap::new();
    let mut cache_order = VecDeque::new();
    let mut image_cache: HashMap<usize, Option<ImageSignature>> = HashMap::new();
    let mut image_order = VecDeque::new();
    let pair_count = pairs.len();
    for (left, right) in pairs {
        if cancel.load(Ordering::Relaxed) {
            report.cancelled = true;
            report.complete = false;
            break;
        }
        let a = &scan.files[left];
        let b = &scan.files[right];
        if exact_membership
            .get(&a.path)
            .is_some_and(|group| exact_membership.get(&b.path) == Some(group))
        {
            report.excluded_exact_pairs += 1;
            continue;
        }
        let name_score = if options.compare_names {
            dice(&name_keys(a), &name_keys(b))
        } else {
            0.0
        };
        let mut text_score = None;
        let mut image_similarity = None;
        if options.compare_text {
            for id in [left, right] {
                if !text_cache.contains_key(&id) {
                    if text_cache.len() >= TEXT_CACHE_ENTRIES {
                        while let Some(old) = cache_order.pop_front() {
                            if old == left || old == right {
                                cache_order.push_back(old);
                                continue;
                            }
                            text_cache.remove(&old);
                            break;
                        }
                    }
                    let words = match text_words(&scan.files[id]) {
                        Ok(value) => value,
                        Err(error) => {
                            if report.warnings.len() < 100 {
                                report.warnings.push(error);
                            }
                            None
                        }
                    };
                    text_cache.insert(id, words);
                    cache_order.push_back(id);
                }
            }
            if let (Some(Some(a)), Some(Some(b))) = (text_cache.get(&left), text_cache.get(&right))
            {
                text_score = Some(dice(a, b));
            }
        }
        if options.compare_images && is_image(a) && is_image(b) {
            for id in [left, right] {
                if !image_cache.contains_key(&id) {
                    if image_cache.len() >= IMAGE_CACHE_ENTRIES {
                        while let Some(old) = image_order.pop_front() {
                            if old == left || old == right {
                                image_order.push_back(old);
                                continue;
                            }
                            image_cache.remove(&old);
                            break;
                        }
                    }
                    let signature = match image_signature(&scan.files[id]) {
                        Ok(value) => value,
                        Err(error) => {
                            if report.warnings.len() < 100 {
                                report.warnings.push(error);
                            }
                            None
                        }
                    };
                    image_cache.insert(id, signature);
                    image_order.push_back(id);
                }
            }
            if let (Some(Some(a)), Some(Some(b))) =
                (image_cache.get(&left), image_cache.get(&right))
            {
                image_similarity = Some(image_score(a, b));
            }
        }
        report.compared_pairs += 1;
        if (options.compare_names && name_score >= options.name_threshold)
            || text_score.is_some_and(|score| score >= options.text_threshold)
        {
            // Image evidence is reported independently; no weak signal hides
            // another enabled criterion.
            report.pairs.push(SimilarPair {
                left: a.path.clone(),
                right: b.path.clone(),
                name_score,
                text_score,
                image_score: image_similarity,
                reason: "이름/제한된 텍스트/이미지 유사 후보 · 삭제 근거 아님".into(),
            });
        } else if image_similarity.is_some_and(|score| score >= options.image_threshold) {
            report.pairs.push(SimilarPair {
                left: a.path.clone(),
                right: b.path.clone(),
                name_score,
                text_score,
                image_score: image_similarity,
                reason: "이미지 유사 후보 · 삭제 근거 아님".into(),
            });
        }
        if report.compared_pairs % 32 == 0 || report.compared_pairs == pair_count {
            if let Some(callback) = progress {
                callback(Progress {
                    phase: "similarity".into(),
                    completed: report.compared_pairs as u64,
                    total: Some(pair_count as u64),
                    message: "유사 후보를 비교하고 있습니다.".into(),
                });
            }
        }
    }
    Ok(report)
}
